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

ROLES={'Morgan':'gemini','Avery':'gemini','Jordan':'gemini','Cameron':'gemini'}
DEFAULTS={'groq':'qwen/qwen3.8-27b','cerebras':'gpt-oss-120b','gemini':'gemini-2.5-flash',
          'cloudflare':'@cf/qwen/qwen3-30b-a3b-fp8','mistral':'codestral-latest','openrouter':'openrouter/free'}
KEYS={'groq':'GROQ_API_KEY','cerebras':'CEREBRAS_API_KEY','gemini':'GEMINI_API_KEY',
      'cloudflare':'CLOUDFLARE_API_TOKEN','mistral':'MISTRAL_API_KEY','openrouter':'OPENROUTER_API_KEY'}

class RoutingError(RuntimeError):pass

def enabled(cfg,provider):
    if cfg.get(provider.upper()+'_ENABLED','true').lower()=='false':return False
    if not cfg.get(KEYS[provider]):return False
    if provider=='cloudflare':return bool(cfg.get('CLOUDFLARE_ACCOUNT_ID')) and cfg.get('CLOUDFLARE_FREE_PLAN_CONFIRMED')=='true'
    if provider in ('gemini','mistral'):
        if cfg.get('AI_PAID_USAGE_ALLOWED')=='true':return True
        if cfg.get(provider.upper()+'_FREE_TIER_CONFIRMED')!='true':return False
        if provider=='gemini':return cfg.get('GEMINI_MODEL',DEFAULTS['gemini']) in ('gemini-2.5-flash','gemini-2.5-flash-lite','gemini-3.8-flash','gemini-3.5-flash-lite')
        return True
    return True

def routes(cfg,agent):
    primary=cfg.get(agent.upper()+'_PROVIDER',ROLES[agent]).lower()
    if primary not in DEFAULTS:raise ValueError('Unknown model provider for '+agent)
    # Keep each role on its own primary pool. Groq remains available for an
    # outage, but never absorbs ordinary traffic from every other department.
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
    def is_cooling_down(self,provider):
        folder=self.root/'work';folder.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(folder/'model-usage.sqlite3')) as db:
            db.execute('CREATE TABLE IF NOT EXISTS cooldown(provider TEXT PRIMARY KEY,until REAL)')
            row=db.execute('SELECT until FROM cooldown WHERE provider=?',(provider,)).fetchone()
            return bool(row and row[0]>time.time())
    def request(self,provider,messages):
        cfg=self.cfg;model=cfg.get(provider.upper()+'_MODEL',DEFAULTS[provider])
        headers={};body={'model':model,'messages':messages,'temperature':.3,'max_tokens':self.max_tokens}
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
        response=requests.post(url,headers=headers,json=body,timeout=45)
        if not response.ok:
            if response.status_code==429:
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
            if '<think>' in answer:answer=''
        if not isinstance(answer,str) or not answer.strip():raise RoutingError(provider+' returned no visible answer')
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
                answer=self.request(provider,messages);self.last_provider=provider
                path=self.root/'work'/'model-routing-events.jsonl'
                path.parent.mkdir(parents=True,exist_ok=True)
                with path.open('a') as log:log.write(json.dumps({'agent':self.agent,'provider':provider,'fallback':provider!=self.cfg.get(self.agent.upper()+'_PROVIDER',ROLES[self.agent]),'time':time.time()})+'\n')
                return answer
            except requests.RequestException:
                self.cooldown(provider,60);failures.append(provider)
            except (RoutingError,ValueError,KeyError,IndexError):failures.append(provider)
        raise RoutingError('No AI provider answered for '+self.agent+'. Check connections and quotas. Tried: '+(', '.join(failures) or 'none configured'))
