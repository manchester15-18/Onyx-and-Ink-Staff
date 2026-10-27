"""Discord staff workspace connector: local gateway intake plus REST delivery.

Inbound files: Discord message attachments are downloaded to
work/discord-uploads/ and passed to AgentChat.ask(), so the agent sees
the same files the dashboard chat sees (images as vision input, text
and documents as attachment text). Outbound files: files an agent
created (work/artifacts/, referenced in its answer as an
/api/artifact?id= link or a work/artifacts/ path) are uploaded back
to the same Discord channel as real attachments, so a PNG, SVG, PDF,
CSV or similar is directly downloadable in Discord instead of a
dashboard-only link.
"""
import json, re, time
from pathlib import Path
import requests
from dotenv import dotenv_values

STAFF=("Morgan","Avery","Jordan","Cameron")
API="https://discord.com/api/v10"
# Suffixes AgentChat.prepare() can actually read. Anything else is
# reported to the user as skipped, never silently dropped and never
# passed to ask() where it would fail the whole message.
INBOUND_SUFFIXES={".png",".jpg",".jpeg",".webp",".pdf",".docx",".txt",".md",".csv",".svg",".json"}
OUTBOUND_SUFFIXES={".png",".jpg",".jpeg",".webp",".svg",".pdf",".csv",".doc",".docx",".txt",".md",".xls",".xlsx"}
MAX_INBOUND_FILES=5
MAX_INBOUND_BYTES=10_000_000
MAX_INBOUND_TOTAL=15_000_000
MAX_OUTBOUND_FILES=5
MAX_OUTBOUND_BYTES=25_000_000

class DiscordBridge:
    def __init__(self, root, chat, stop):
        self.root=Path(root); self.chat=chat; self.stop=stop

    def config(self): return dotenv_values(self.root/'.env')
    def state_path(self): return self.root/'work'/'discord-state.json'
    def state(self):
        try:return json.loads(self.state_path().read_text())
        except (OSError,ValueError,TypeError):return {'enabled':False,'connected':False,'lastError':'','lastMessageAt':''}
    def save(self,state):
        path=self.state_path();path.parent.mkdir(exist_ok=True)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(state));temp.chmod(0o600);temp.replace(path)
    def enabled(self):
        cfg=self.config();return str(cfg.get('DISCORD_ENABLED','')).lower()=='true' and bool(cfg.get('DISCORD_BOT_TOKEN'))
    def token(self,agent=''):
        cfg=self.config();return cfg.get('DISCORD_'+agent.upper()+'_BOT_TOKEN','') if agent else cfg.get('DISCORD_BOT_TOKEN','')
    def channel(self,agent='', private=False):
        cfg=self.config()
        if private and agent:
            return str(cfg.get('DISCORD_'+agent.upper()+'_CHANNEL_ID','') or cfg.get('DISCORD_AGENT_REQUESTS_CHANNEL_ID','')).strip()
        return str(cfg.get('DISCORD_STAFF_CHANNEL_ID','')).strip()
    @staticmethod
    def valid_channel(value): return bool(re.fullmatch(r'\d{16,22}',str(value)))
    def headers(self,token): return {'Authorization':'Bot '+token,'User-Agent':'OnyxAndInkStaff/1.0'}
    def api(self,method,path,token,**kwargs):
        try:r=requests.request(method,API+path,headers=self.headers(token),timeout=35,**kwargs)
        except requests.RequestException as e:raise RuntimeError('Discord could not be reached. Check this Windows PC connection.') from e
        if r.status_code==401:raise RuntimeError('Discord rejected the bot token.')
        if r.status_code==403:raise RuntimeError('Discord denied this action. Give the bot View Channel, Send Messages, Attach Files, and Create Public Threads.')
        if r.status_code==429:raise RuntimeError('Discord is rate-limiting the bot. Try again shortly.')
        if not r.ok:raise RuntimeError('Discord returned an error ('+str(r.status_code)+').')
        return r.json() if r.content else {}
    @staticmethod
    def clean_filename(name):
        name=Path(str(name).replace('\\','/')).name
        name=re.sub(r'[\x00-\x1f\x7f]','',name)[:140]
        return name if name not in ('','.','..') else 'attachment'
    def download_attachments(self,data,token):
        """Download a Discord message's attachments for AgentChat.

        Returns (paths, skipped_notes). Unsupported or oversize files
        are reported in skipped_notes instead of failing the message,
        so one bad file never blocks the rest of the conversation.
        """
        folder=self.root/'work'/'discord-uploads';folder.mkdir(parents=True,exist_ok=True)
        paths=[];skipped=[];total=0
        message_id=str(data.get('id','') or 'message')
        for item in (data.get('attachments') or [])[:MAX_INBOUND_FILES]:
            name=self.clean_filename(item.get('filename','attachment'))
            suffix=Path(name).suffix.lower()
            if suffix not in INBOUND_SUFFIXES:
                skipped.append(name+' (unsupported file type)');continue
            size=item.get('size')
            if isinstance(size,int) and size>MAX_INBOUND_BYTES:
                skipped.append(name+' (over 10 MB)');continue
            url=item.get('url') or item.get('proxy_url')
            if not url:
                skipped.append(name+' (no download link)');continue
            try:
                response=requests.get(url,headers={'Authorization':'Bot '+token},timeout=35)
                if response.status_code in (401,403):
                    response=requests.get(url,timeout=35)
                if not response.ok:raise RuntimeError('HTTP '+str(response.status_code))
                content=response.content
            except Exception:
                skipped.append(name+' (download failed)');continue
            if not content or len(content)>MAX_INBOUND_BYTES or total+len(content)>MAX_INBOUND_TOTAL:
                skipped.append(name+' (over the combined 15 MB limit)');continue
            path=folder/(message_id+'--'+name)
            path.write_bytes(content);path.chmod(0o600)
            paths.append(path);total+=len(content)
        if len(data.get('attachments') or [])>MAX_INBOUND_FILES:
            skipped.append('only the first '+str(MAX_INBOUND_FILES)+' attachments were forwarded')
        return paths,skipped
    def resolve_answer_files(self,answer):
        """Resolve agent-created files named in an answer to local paths.

        Only files under work/artifacts/ are eligible: that is where
        Actions.save() puts every agent-created PNG, SVG, PDF, CSV,
        document and report. Uploaded user files, .env, and anything
        outside that folder are never auto-uploaded, even if named.
        """
        folder=(self.root/'work'/'artifacts').resolve()
        found=[]
        for identifier in re.findall(r'/api/artifact\?id=([a-f0-9]{32})',str(answer)):
            matches=list(folder.glob(identifier+'.*'))
            if len(matches)==1:found.append(matches[0])
        for match in re.finditer(r'\bwork/artifacts/[A-Za-z0-9_.\-/]+\.(png|jpe?g|webp|svg|pdf|csv|docx?|txt|md|xlsx?)',str(answer),re.I):
            found.append(self.root/match.group(0))
        resolved=[]
        for path in found:
            try:
                resolved_path=path.resolve()
                if not resolved_path.is_relative_to(folder) or not resolved_path.is_file():continue
                if resolved_path.suffix.lower() not in OUTBOUND_SUFFIXES:continue
                if resolved_path.stat().st_size>MAX_OUTBOUND_BYTES:continue
                if resolved_path not in resolved:resolved.append(resolved_path)
            except (OSError,ValueError):continue
        return resolved[:MAX_OUTBOUND_FILES]
    def post_file(self,channel,token,path,agent):
        with Path(path).open('rb') as stream:
            self.api('POST','/channels/'+channel+'/messages',token,
                     files={'files[0]':(Path(path).name,stream)},
                     data={'payload_json':json.dumps({'content':'**'+agent+'** shared a file: '+Path(path).name,'allowed_mentions':{'parse':[]}})})
    def check(self,token=None):
        token=token or self.token()
        if not token:raise ValueError('Enter a Discord bot token.')
        me=self.api('GET','/users/@me',token)
        return {'id':str(me.get('id','')),'name':str(me.get('username','Discord bot'))}
    def provision_workspace(self,guild,token=None):
        """Create a predictable staff channel layout in an existing Discord server."""
        if not self.valid_channel(guild):raise ValueError('Enter the existing Discord server ID.')
        token=token or self.token()
        if not token:raise ValueError('Save a Discord bot token first.')
        existing=self.api('GET','/guilds/'+str(guild)+'/channels',token)
        by_name={str(item.get('name','')).lower():str(item.get('id','')) for item in existing}
        def ensure(name,kind=0,parent=None):
            if name.lower() in by_name:return by_name[name.lower()]
            body={'name':name,'type':kind}
            if parent:body['parent_id']=parent
            made=self.api('POST','/guilds/'+str(guild)+'/channels',token,json=body)
            by_name[name.lower()]=str(made['id']);return by_name[name.lower()]
        category=ensure('Onyx & Ink Staff',4)
        staff=ensure('staff-floor',0,category);requests=ensure('agent-requests',0,category);briefs=ensure('daily-briefs',0,category);projects=ensure('projects',0,category)
        roles={agent:ensure(agent.lower(),0,category) for agent in STAFF}
        return {'guild':str(guild),'staff':staff,'requests':requests,'briefs':briefs,'projects':projects,'agents':roles}
    def send(self, sender, text, files=(), recipient=''):
        if not self.enabled():raise ValueError('Enable Discord and save its shared bot token in Settings.')
        token=self.token(sender) or self.token()
        channel=self.channel(recipient,private=bool(recipient)) or self.channel()
        if not self.valid_channel(channel):raise ValueError('Set a valid Discord staff channel ID in Settings.')
        prefix='**'+sender+'**'
        if recipient:prefix+=' → **'+recipient+'**'
        content=(prefix+'\n'+str(text).strip())[:2000]
        result=self.api('POST','/channels/'+channel+'/messages',token,json={'content':content,'allowed_mentions':{'parse':[]}})
        for path in files:
            with Path(path).open('rb') as stream:
                self.api('POST','/channels/'+channel+'/messages',token,files={'files[0]':(Path(path).name,stream)},data={'payload_json':json.dumps({'content':prefix+' shared a file: '+Path(path).name,'allowed_mentions':{'parse':[]}})})
        return {'result':'Discord '+('private staff message' if recipient else 'staff-floor message')+' posted'+(' with '+str(len(files))+' file(s).' if files else '.'),'channel':channel,'message_id':str(result.get('id',''))}
    def project_thread(self, project, briefing):
        """Create a stable project discussion thread in the staff floor when allowed."""
        if not self.enabled():return None
        path=self.root/'work'/'discord-project-threads.json'
        try:items=json.loads(path.read_text())
        except (OSError,ValueError):items={}
        if project in items:return items[project]
        channel=self.channel()
        if not self.valid_channel(channel):return None
        token=self.token('Morgan') or self.token()
        message=self.api('POST','/channels/'+channel+'/messages',token,json={'content':'**Morgan · Project workspace**\n'+project+'\n'+briefing[:1200],'allowed_mentions':{'parse':[]}})
        try:
            thread=self.api('POST','/channels/'+channel+'/messages/'+str(message['id'])+'/threads',token,json={'name':project[:90],'auto_archive_duration':1440})
            items[project]=str(thread['id']);path.write_text(json.dumps(items));path.chmod(0o600);return items[project]
        except RuntimeError:return channel
    def daily_brief(self,text):
        cfg=self.config();channel=str(cfg.get('DISCORD_DAILY_BRIEF_CHANNEL_ID','')) or self.channel()
        if not self.valid_channel(channel):raise ValueError('Set a Discord daily brief channel ID or staff channel ID.')
        token=self.token('Morgan') or self.token()
        old=cfg.get('DISCORD_STAFF_CHANNEL_ID','');
        result=self.api('POST','/channels/'+channel+'/messages',token,json={'content':'**Morgan · Daily brief**\n'+text[:1900],'allowed_mentions':{'parse':[]}})
        return result
    def route_agent(self,message,channel_id):
        cfg=self.config()
        for agent in STAFF:
            if str(cfg.get('DISCORD_'+agent.upper()+'_CHANNEL_ID',''))==str(channel_id):return agent, message
        hit=re.match(r'^\s*(Morgan|Avery|Jordan|Cameron)\b\s*[:,]?\s*',message,re.I)
        if hit:return hit[1].title(),message[hit.end():]
        return 'Morgan',message
    def run(self):
        """Gateway intake keeps Discord usable from this local Windows dashboard."""
        try:import websocket
        except ImportError:
            state=self.state();state.update({'connected':False,'lastError':'Discord listener needs websocket-client. Rebuild the dashboard.'});self.save(state);return
        sequence=None
        while not self.stop.wait(3):
            if not self.enabled():continue
            token=self.token()
            try:
                ws=websocket.create_connection('wss://gateway.discord.gg/?v=10&encoding=json',timeout=15)
                hello=json.loads(ws.recv());interval=hello['d']['heartbeat_interval']/1000
                ws.send(json.dumps({'op':2,'d':{'token':token,'intents':33281,'properties':{'os':'windows','browser':'onyx-and-ink','device':'onyx-and-ink'}}}))
                state=self.state();state.update({'enabled':True,'connected':True,'lastError':''});self.save(state)
                next_beat=time.monotonic()+interval
                while not self.stop.is_set() and self.enabled():
                    if time.monotonic()>=next_beat:
                        ws.send(json.dumps({'op':1,'d':sequence}));next_beat=time.monotonic()+interval
                    ws.settimeout(1)
                    try:packet=json.loads(ws.recv())
                    except Exception:continue
                    sequence=packet.get('s',sequence)
                    if packet.get('op')==7:break
                    if packet.get('t')!='MESSAGE_CREATE':continue
                    data=packet.get('d',{});author=data.get('author',{})
                    if author.get('bot'):continue
                    content=str(data.get('content','')).strip();channel=str(data.get('channel_id',''))
                    attachments=data.get('attachments') or []
                    if (not content and not attachments) or len(content)>4000:continue
                    agent,body=self.route_agent(content,channel)
                    try:
                        self.api('POST','/channels/'+channel+'/typing',token)
                        paths,skipped=self.download_attachments(data,token)
                        if not body.strip():body='Please describe or analyze the attached files.'
                        if skipped:body+='\nNote: the following Discord attachments were not forwarded to the agent: '+'; '.join(skipped)+'.'
                        answer=self.chat.ask(agent,body,'discord:'+str(data.get('id','')),source='discord',attachments=paths)
                        self.api('POST','/channels/'+channel+'/messages',self.token(agent) or token,json={'content':'**'+agent+'**\n'+answer[:1900],'allowed_mentions':{'parse':[]}})
                        failed=[]
                        for path in self.resolve_answer_files(answer):
                            try:self.post_file(channel,self.token(agent) or token,path,agent)
                            except Exception:failed.append(Path(path).name)
                        if failed:
                            self.api('POST','/channels/'+channel+'/messages',self.token(agent) or token,json={'content':'**'+agent+'** could not upload: '+', '.join(failed),'allowed_mentions':{'parse':[]}})
                        state=self.state();state.update({'connected':True,'lastMessageAt':time.strftime('%Y-%m-%dT%H:%M:%S')});self.save(state)
                    except Exception as error:
                        state=self.state();state.update({'connected':False,'lastError':str(error)[:180]});self.save(state)
                ws.close()
            except Exception as error:
                state=self.state();state.update({'connected':False,'lastError':str(error)[:180]});self.save(state)
