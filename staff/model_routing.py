"""Role-specific cloud providers with shared budgets and a free cloud final fallback."""
import json
import math
import os
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
import requests
from crewai import BaseLLM

# OpenRouter's free router is the shared primary pool.  Role-specific provider
# settings may still override this intentionally, and all configured cloud
# providers remain available as fallbacks.
ROLES={'Morgan':'openrouter','Avery':'openrouter','Jordan':'openrouter','Cameron':'openrouter'}
DEFAULTS={'groq':'qwen/qwen3.8-27b','cerebras':'gpt-oss-120b','gemini':'gemini-2.5-flash',
          'cloudflare':'@cf/qwen/qwen3-30b-a3b-fp8','mistral':'codestral-latest','openrouter':'openrouter/free'}
KEYS={'groq':'GROQ_API_KEY','cerebras':'CEREBRAS_API_KEY','gemini':'GEMINI_API_KEY',
      'cloudflare':'CLOUDFLARE_API_TOKEN','mistral':'MISTRAL_API_KEY','openrouter':'OPENROUTER_API_KEY'}

# OpenRouter models known to accept image input. Any other :free model is
# tried for text, but vision requests fall back to openrouter/free rather
# than being sent to a text-only endpoint and failing silently.
OPENROUTER_VISION_MODELS={'openrouter/free','nvidia/nemotron-3.5-super-vl:free'}

# Per-model OpenRouter limits, verified Oct 27, 2026 from GET /v1/models:
# (context_length, top_provider.max_completion_tokens). These are NOT API
# request-rate limits - each model gets its own ceiling, and a request's
# max_tokens is capped at that model's ceiling AND at
# context_length - estimated_prompt_tokens - headroom, per OpenRouter's
# docs: max_tokens' maximum is context length minus prompt length.
# openrouter/free publishes no single max_completion (it is a router), so
# it gets a conservative 32768. Unknown :free models get a conservative
# (128000, 16384) fallback - free models rotate, so a hardcoded table must
# never be treated as a guarantee; a 404 still means the ladder moves on.
OPENROUTER_MODEL_LIMITS={
    'nvidia/nemotron-3.5-super-vl:free': (1000000, 131072),
    'nvidia/nemotron-3.5-lightning:free': (1000000, 65536),
    'google/gemma-4-31b-it:free': (262144, 32768),
    'nvidia/nemotron-3-ultra-550b-a55b:free': (1000000, 65536),
    'cohere/north-mini-code:free': (256000, 64000),
    'liquid/lfm-2.5-2.6b:free': (65536, 8192),
    'dots-studio/dots-3-note-preview:free':(512000,460800),
    'openrouter/free': (200000, 32768),
}
OPENROUTER_DEFAULT_LIMITS=(128000, 16384)
# Reasoning tokens count toward max_tokens on OpenRouter, so a tiny budget
# can be consumed entirely by reasoning and return empty content (seen
# live Oct 27: max_tokens=20 gave 'no visible answer' on both Nemotron
# models, 200 worked). Never send an OpenRouter request below this floor.
OPENROUTER_REASONING_FLOOR=512
OPENROUTER_HEADROOM=256
# Latency, Oct 28 2026: one stalled free model used to hold a whole cycle
# for up to 45s, and the next agent then retried the same slow model. A
# 30s per-request cap plus a cycle-scoped cooldown on timeout/empty
# answers bounds a stalled attempt without removing any fallback route:
# a timeout still falls through to the next provider/model in this call,
# and the next agent skips the slow model for the rest of the cycle.
REQUEST_TIMEOUT_SECONDS=30
SLOW_MODEL_COOLDOWN_SECONDS=300

def openrouter_estimate_prompt_tokens(messages):
    characters=0;images=0
    for item in messages:
        content=item.get('content','')
        if isinstance(content,str):characters+=len(content)
        elif isinstance(content,list):
            for block in content:
                if block.get('type')=='text':characters+=len(block.get('text',''))
                else:images+=1
    return math.ceil(characters/3)+images*2048

def gemma_cooldown_seconds(cfg):
    try:return max(60,min(86400,int(cfg.get('OPENROUTER_GEMMA_COOLDOWN_SECONDS',21600))))
    except (ValueError,TypeError):return 21600

def openrouter_effective_max_tokens(model,requested,prompt_tokens):
    context,max_completion=OPENROUTER_MODEL_LIMITS.get(model,OPENROUTER_DEFAULT_LIMITS)
    ceiling=min(max_completion,context-prompt_tokens-OPENROUTER_HEADROOM)
    if ceiling<OPENROUTER_REASONING_FLOOR:
        raise RoutingError('OpenRouter model '+model+' has no headroom: prompt ~'+str(prompt_tokens)+' tokens, context '+str(context))
    return max(OPENROUTER_REASONING_FLOOR,min(int(requested),ceiling))

def openrouter_models(cfg,vision=False):
    raw=str(cfg.get('OPENROUTER_MODELS','') or '').strip()
    candidates=[item.strip() for item in raw.split(',') if item.strip()] if raw else [cfg.get('OPENROUTER_MODEL',DEFAULTS['openrouter'])]
    models=[model for model in candidates if model=='openrouter/free' or model.endswith(':free')]
    if vision:
        vision_models=[model for model in models if model in OPENROUTER_VISION_MODELS]
        return vision_models or ['openrouter/free']
    return models

def gemini_models(cfg):
    raw=str(cfg.get('GEMINI_MODELS','') or '').strip()
    if raw:
        return list(dict.fromkeys(item.strip() for item in raw.split(',') if item.strip()))
    primary=cfg.get('GEMINI_MODEL',DEFAULTS['gemini'])
    return list(dict.fromkeys([primary,'gemma-4-31b-it','gemma-4-26b-a4b-it']))

class RoutingError(RuntimeError):pass

def enabled(cfg,provider):
    if cfg.get(provider.upper()+'_ENABLED','true').lower()=='false':return False
    if not cfg.get(KEYS[provider]):return False
    if provider=='cloudflare':return bool(cfg.get('CLOUDFLARE_ACCOUNT_ID')) and cfg.get('CLOUDFLARE_FREE_PLAN_CONFIRMED')=='true'
    if provider in ('gemini','mistral'):
        if cfg.get('AI_PAID_USAGE_ALLOWED')=='true':return True
        if cfg.get(provider.upper()+'_FREE_TIER_CONFIRMED')!='true':return False
        if provider=='gemini':return any(m in ('gemini-2.5-flash','gemini-2.5-flash-lite','gemini-3.8-flash','gemini-3.5-flash-lite') or m.startswith('gemma-') for m in gemini_models(cfg))
        return True
    return True

def routes(cfg,agent):
    primary=cfg.get(agent.upper()+'_PROVIDER',ROLES[agent]).lower()
    if primary not in DEFAULTS:raise ValueError('Unknown model provider for '+agent)
    # Keep a broad cloud-only fallback chain.  The requested provider is always
    # first; repeated entries are removed below.
    fallbacks={
        'Morgan':['gemini','cerebras','cloudflare','mistral','openrouter','groq'],
        'Avery':['gemini','cloudflare','cerebras','mistral','openrouter','groq'],
        'Jordan':['gemini','mistral','cloudflare','cerebras','openrouter','groq'],
        'Cameron':['gemini','cerebras','cloudflare','mistral','openrouter','groq'],
    }
    order=[primary]+fallbacks[agent]
    return list(dict.fromkeys(provider for provider in order if enabled(cfg,provider)))

def routing_status(cfg):
    cfg={**os.environ,**cfg}
    cfg={**cfg,**{agent.upper()+'_PROVIDER':ROLES[agent] for agent in ROLES if cfg.get(agent.upper()+'_PROVIDER',ROLES[agent]).lower() not in DEFAULTS}}
    return {agent:{'primary':cfg.get(agent.upper()+'_PROVIDER',primary),
                   'availableRoutes':routes(cfg,agent),
                   'models':{p:cfg.get(p.upper()+'_MODEL',DEFAULTS[p]) for p in routes(cfg,agent)}}
            for agent,primary in ROLES.items()}

class RoutedLLM(BaseLLM):
    def __init__(self,root,cfg,agent,max_tokens=1000,priority='interactive'):
        super().__init__(model='role/'+agent,temperature=.3)
        self.root=Path(root);self.cfg={**os.environ,**cfg};self.agent=agent;self.max_tokens=max_tokens
        if priority not in ('interactive','background'):raise ValueError('Unknown routing priority')
        self.priority=priority
        self.last_provider=None
    def for_agent(self,agent):return RoutedLLM(self.root,self.cfg,agent,self.max_tokens,self.priority)
    def supports_function_calling(self):return False
    def supports_stop_words(self):return False
    def get_context_window_size(self):return 32000
    def close(self):pass
    def reserve(self,provider,messages):
        """Reserve estimated input/output across processes before sending a request."""
        prefix=provider.upper();characters=0;images=0
        for item in messages:
            content=item['content']
            if isinstance(content,str):characters+=len(content)
            else:
                for block in content:
                    if block['type']=='text':characters+=len(block['text'])
                    else:images+=1
        tokens=math.ceil(characters/3)+images*2048+self.max_tokens+100
        defaults={'groq':(25,8000,200000),'cerebras':(5,30000,900000),'gemini':(2,16000,50000),
                  'cloudflare':(5,16000,50000),'mistral':(2,16000,50000),'openrouter':(5,16000,50000)}
        rpm,tpm,tpd=(int(self.cfg.get(prefix+'_'+key,default)) for key,default in zip(('RPM','TPM','TPD'),defaults[provider]))
        now=time.time();day=int(now//86400);folder=self.root/'work';folder.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(folder/'model-usage.sqlite3',timeout=10)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS usage(provider TEXT,stamp REAL,day INTEGER,tokens INTEGER,priority TEXT NOT NULL DEFAULT 'background')")
            columns={row[1] for row in db.execute('PRAGMA table_info(usage)')}
            if 'priority' not in columns:
                # Older rows are autonomous estimates.  Classifying them as
                # background prevents a past staff run from blocking a live chat.
                db.execute("ALTER TABLE usage ADD COLUMN priority TEXT NOT NULL DEFAULT 'background'")
            db.execute('CREATE TABLE IF NOT EXISTS cooldown(provider TEXT PRIMARY KEY,until REAL)');db.commit()
            db.execute('BEGIN IMMEDIATE')
            cooldown=db.execute('SELECT until FROM cooldown WHERE provider=?',(provider,)).fetchone()
            if cooldown and cooldown[0]>now:raise RoutingError(provider+' is cooling down')
            count,total=db.execute('SELECT count(*),coalesce(sum(tokens),0) FROM usage WHERE provider=? AND stamp>? AND priority=?',(provider,now-60,self.priority)).fetchone()
            daily=db.execute('SELECT coalesce(sum(tokens),0) FROM usage WHERE provider=? AND day=? AND priority=?',(provider,day,self.priority)).fetchone()[0]
            # Autonomous staff no longer repeats unchanged project work. Keep a small
            # reserve for live chats, but do not reject every fallback before a request
            # reaches a healthy provider.
            if self.priority=='background':
                rpm=max(1,rpm-1)
                tpm=max(tokens,min(tpm,max(1,(tpm*3)//4)))
                tpd=max(tokens,min(tpd,max(1,(tpd*19)//20)))
            if count>=rpm or total+tokens>tpm or daily+tokens>tpd:raise RoutingError(provider+' local budget exhausted')
            db.execute('DELETE FROM usage WHERE day<?',(day-1,))
            db.execute('INSERT INTO usage(provider,stamp,day,tokens,priority) VALUES (?,?,?,?,?)',(provider,now,day,tokens,self.priority));db.commit()
    def cooldown(self,provider,seconds):
        folder=self.root/'work';folder.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(folder/'model-usage.sqlite3')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cooldown(provider TEXT PRIMARY KEY,until REAL)')
            db.execute('INSERT OR REPLACE INTO cooldown VALUES (?,?)',(provider,time.time()+seconds));db.commit()
    def set_model_cooldown(self,model,seconds):
        folder=self.root/'work';folder.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(folder/'model-usage.sqlite3')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS model_cooldown(model TEXT PRIMARY KEY,until REAL)')
            db.execute('INSERT OR REPLACE INTO model_cooldown VALUES (?,?)',(model,time.time()+seconds));db.commit()
    def is_model_cooling_down(self,model):
        folder=self.root/'work';folder.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(folder/'model-usage.sqlite3')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS model_cooldown(model TEXT PRIMARY KEY,until REAL)')
            row=db.execute('SELECT until FROM model_cooldown WHERE model=?',(model,)).fetchone()
            return bool(row and row[0]>time.time())
    def is_cooling_down(self,provider):
        folder=self.root/'work';folder.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(folder/'model-usage.sqlite3')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cooldown(provider TEXT PRIMARY KEY,until REAL)')
            row=db.execute('SELECT until FROM cooldown WHERE provider=?',(provider,)).fetchone()
            return bool(row and row[0]>time.time())
    def request(self,provider,messages,model=None):
        cfg=self.cfg;model=model or cfg.get(provider.upper()+'_MODEL',DEFAULTS[provider])
        headers={};body={'model':model,'messages':messages,'temperature':.3,'max_tokens':self.max_tokens}
        if provider=='openrouter':body['max_tokens']=openrouter_effective_max_tokens(model,self.max_tokens,openrouter_estimate_prompt_tokens(messages))
        if provider=='cloudflare':
            if 'qwen3' in model.lower():
                messages=[dict(item) for item in messages]
                for item in reversed(messages):
                    if item.get('role')=='user':
                        item['content']+='\n/no_think';break
                body['messages']=messages
            url='https://api.cloudflare.com/client/v4/accounts/'+cfg['CLOUDFLARE_ACCOUNT_ID']+'/ai/run/'+model
            body.pop('model');headers={'Authorization':'Bearer '+cfg[KEYS[provider]]}
        else:
            bases={'groq':'https://api.groq.com/openai/v1','cerebras':'https://api.cerebras.ai/v1',
                   'gemini':'https://generativelanguage.googleapis.com/v1beta/openai','mistral':'https://api.mistral.ai/v1','openrouter':'https://openrouter.ai/api/v1'}
            url=bases[provider]+'/chat/completions';headers={'Authorization':'Bearer '+cfg[KEYS[provider]]}
            if provider=='groq' and model.startswith('qwen/qwen3'):body['reasoning_effort']='none'
            if provider=='cerebras':body['reasoning_effort']='low'
            if provider=='openrouter':
                if model!='openrouter/free' and not model.endswith(':free'):raise RoutingError('Final fallback requires a free cloud model')
                body['provider']={'max_price':{'prompt':0,'completion':0,'request':0},'allow_fallbacks':True}
        try:
            response=requests.post(url,headers=headers,json=body,timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException:
            # Fail fast, and do not let every subsequent agent in this
            # cycle re-stall on the same slow model. A timeout is
            # transient, so this is a short cycle-scoped cooldown, not
            # the 24h terminal-error cooldown below. The caller still
            # falls through to the next provider/model in this call.
            # Never include the exception text: it may carry a key or URL.
            if provider in ('openrouter','gemini') and not self.is_model_cooling_down(model):
                self.set_model_cooldown(model,SLOW_MODEL_COOLDOWN_SECONDS)
            raise
        if not response.ok:
            if provider=='gemini':
                if response.status_code==429:self.set_model_cooldown(model,gemma_cooldown_seconds(self.cfg) if model.startswith('gemma-') else 60)
                elif response.status_code in (401,403,404):self.set_model_cooldown(model,86400)
                elif response.status_code in (500,502,503,504):self.set_model_cooldown(model,60)
                raise RoutingError(provider+' returned HTTP '+str(response.status_code))
            if provider=='openrouter':
                # Per-model, not provider-wide: a dead or throttled free model must
                # not block the other free models on the next call.
                if response.status_code==429:self.set_model_cooldown(model,gemma_cooldown_seconds(self.cfg) if model.startswith('google/gemma') else 60)
                elif response.status_code in (401,403,404):self.set_model_cooldown(model,86400)
                elif response.status_code in (500,502,503,504):self.set_model_cooldown(model,60)
            elif response.status_code==429:
                try:wait=max(60,min(86400,float(response.headers.get('Retry-After',60))))
                except ValueError:wait=60
                self.cooldown(provider,wait)
            elif response.status_code in (401,403,404):self.cooldown(provider,86400)
            elif response.status_code in (500,502,503,504):self.cooldown(provider,60)
            raise RoutingError(provider+' returned HTTP '+str(response.status_code))
        data=response.json()
        if provider=='cloudflare':answer=data.get('result',{}).get('response','')
        else:answer=data.get('choices',[{}])[0].get('message',{}).get('content','')
        if isinstance(answer,str):
            answer=re.sub(r'<think>.*?</think>', '', answer, flags=re.DOTALL).strip()
            answer=re.sub(r'<thought>.*?</thought>', '', answer, flags=re.DOTALL).strip()
            if '<think>' in answer or '<thought>' in answer:answer=''
        if not isinstance(answer,str) or not answer.strip():
            # An empty answer is usually a spent reasoning budget on that
            # model, not a provider outage. Cool it for the rest of the
            # cycle so the next agent does not pay for the same empty
            # reply; the fallback chain is untouched.
            if provider in ('openrouter','gemini'):
                if not self.is_model_cooling_down(model):self.set_model_cooldown(model,SLOW_MODEL_COOLDOWN_SECONDS)
            else:
                self.cooldown(provider,SLOW_MODEL_COOLDOWN_SECONDS)
            raise RoutingError(provider+' returned no visible answer')
        return answer
    def call(self,messages,tools=None,callbacks=None,available_functions=None,**kwargs):
        if isinstance(messages,str):messages=[{'role':'user','content':messages}]
        vision=False
        for item in messages:
            content=item.get('content')
            if isinstance(content,str):continue
            if not isinstance(content,list) or not content:raise RoutingError('Invalid multimodal content')
            for block in content:
                if block.get('type')=='text' and isinstance(block.get('text'),str):continue
                url=block.get('image_url',{}).get('url','')
                if block.get('type')!='image_url' or not re.fullmatch(r'data:image/jpeg;base64,[A-Za-z0-9+/=]+',url):raise RoutingError('Invalid multimodal image')
                vision=True
        if not vision:
            messages=[{**item,'content':'\n'.join(b['text'] for b in item['content']) if isinstance(item['content'],list) else item['content']} for item in messages]
        failures=[]
        for provider in routes(self.cfg,self.agent):
            model=self.cfg.get(provider.upper()+'_MODEL',DEFAULTS[provider])
            if vision and not (provider in ('gemini','openrouter') or provider=='groq' and model=='qwen/qwen3.8-27b'):
                continue
            if self.is_cooling_down(provider):
                failures.append(provider)
                continue
            try:
                # Provider-side limits are authoritative for live conversations.
                # The local estimate can be stale after a restart or diagnostics
                # and must never make every healthy route appear unavailable.
                if self.priority=='background':self.reserve(provider,messages)
                if provider=='openrouter':
                    answer=None
                    for candidate in openrouter_models(self.cfg,vision=vision):
                        if self.is_model_cooling_down(candidate):continue
                        try:answer=self.request(provider,messages,model=candidate);used_model=candidate;break
                        except (RoutingError,ValueError,KeyError,IndexError,requests.RequestException):
                            # Fail fast for the whole cycle: a model that
                            # failed for any reason this call must not be
                            # retried by the next agent. Longer terminal
                            # cooldowns set inside request() are kept.
                            if not self.is_model_cooling_down(candidate):self.set_model_cooldown(candidate,SLOW_MODEL_COOLDOWN_SECONDS)
                            continue
                    if answer is None:raise RoutingError('No OpenRouter free model answered')
                elif provider=='gemini':
                    answer=None
                    for candidate in gemini_models(self.cfg):
                        if self.is_model_cooling_down(candidate):continue
                        try:answer=self.request(provider,messages,model=candidate);used_model=candidate;break
                        except (RoutingError,ValueError,KeyError,IndexError,requests.RequestException):
                            if not self.is_model_cooling_down(candidate):self.set_model_cooldown(candidate,SLOW_MODEL_COOLDOWN_SECONDS)
                            continue
                    if answer is None:raise RoutingError('No Gemini model answered')
                else:
                    answer=self.request(provider,messages);used_model=self.cfg.get(provider.upper()+'_MODEL',DEFAULTS[provider])
                self.last_provider=provider
                path=self.root/'work'/'model-routing-events.jsonl'
                path.parent.mkdir(parents=True,exist_ok=True)
                with path.open('a') as log:log.write(json.dumps({'agent':self.agent,'provider':provider,'model':used_model,'fallback':provider!=self.cfg.get(self.agent.upper()+'_PROVIDER',ROLES[self.agent]),'time':time.time()})+'\n')
                return answer
            except requests.RequestException:
                self.cooldown(provider,60);failures.append(provider)
            except (RoutingError,ValueError,KeyError,IndexError):failures.append(provider)
        raise RoutingError('No AI provider answered for '+self.agent+'. Check connections and quotas. Tried: '+(', '.join(failures) or 'none configured'))
