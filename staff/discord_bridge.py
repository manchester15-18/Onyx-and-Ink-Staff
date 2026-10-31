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

Voice: /voice (as a message or a slash command) gets a spoken reply
in the answering agent's own designed voice, as an MP3 attachment
alongside the text so the conversation stays auditable. A reply that
looks cut off mid-sentence is re-asked once before it is spoken, the
same fragment guard the Discord voice flow uses. Slash commands are
registered on the shared bot application (/voice, /morgan, /avery,
/jordan, /cameron, /help); replies and typing use the answering
agent's own bot token when DISCORD_<AGENT>_BOT_TOKEN is set, so the
message author reads Avery, Morgan, and so on instead of the shared
staff bot. With only the shared token set, the author stays the
shared bot and the agent name appears as a bold prefix.
"""
import io, json, os, re, threading, time, uuid
from pathlib import Path
import requests
from dotenv import dotenv_values
from staff.agent_chat import is_truncated_reply, parse_voice_command

STAFF=("Morgan","Avery","Jordan","Cameron")
API="https://discord.com/api/v10"
# Suffixes AgentChat.prepare() can actually read. Anything else is
# reported to the user as skipped, never silently dropped and never
# passed to ask() where it would fail the whole message.
INBOUND_SUFFIXES={".png",".jpg",".jpeg",".webp",".gif",".pdf",".docx",".txt",".md",".csv",".svg",".json"}
# Spoken attachments: downloaded separately from readable files, never
# passed to ask() as attachments (the text pipeline rejects audio).
# They are transcribed first, and a transcribed turn is answered by
# voice, the same rule as /voice.
AUDIO_SUFFIXES={".ogg",".oga",".opus",".mp3",".wav",".m4a"}
OUTBOUND_SUFFIXES={".png",".jpg",".jpeg",".webp",".svg",".pdf",".csv",".doc",".docx",".txt",".md",".xls",".xlsx"}
MAX_INBOUND_FILES=5
MAX_INBOUND_BYTES=10_000_000
MAX_INBOUND_TOTAL=15_000_000
MAX_OUTBOUND_FILES=5
MAX_OUTBOUND_BYTES=25_000_000
MAX_AUDIO_BYTES=15_000_000
# Slash commands registered on the shared bot application. Per-agent
# bots do not register their own copies: the commands would otherwise
# appear once per bot in the same menu. Replies still go out under the
# answering agent's own token when one is configured.
DISCORD_COMMANDS=[
    {'name':'voice','description':'Reply with a voice message','options':[{'name':'message','description':'What to say to the agent','type':3,'required':True}]},
    {'name':'morgan','description':'Ask Morgan, Operations','options':[{'name':'message','description':'What to ask Morgan','type':3,'required':True}]},
    {'name':'avery','description':'Ask Avery, Marketing','options':[{'name':'message','description':'What to ask Avery','type':3,'required':True}]},
    {'name':'jordan','description':'Ask Jordan, IT and storefront development','options':[{'name':'message','description':'What to ask Jordan','type':3,'required':True}]},
    {'name':'cameron','description':'Ask Cameron, Legal and HR','options':[{'name':'message','description':'What to ask Cameron','type':3,'required':True}]},
    {'name':'help','description':'How to talk to the staff agents'},
    {'name':'join-voice','description':'Have an agent join a voice channel','options':[{'name':'agent','description':'Which agent joins the call','type':3,'required':True,'choices':[{'name':'Morgan','value':'Morgan'},{'name':'Avery','value':'Avery'},{'name':'Jordan','value':'Jordan'},{'name':'Cameron','value':'Cameron'}]},{'name':'channel','description':'Voice channel to join','type':7,'channel_types':[2],'required':True}]},
    {'name':'leave-voice','description':'Have an agent leave the voice channel','options':[{'name':'agent','description':'Which agent leaves the call','type':3,'required':True,'choices':[{'name':'Morgan','value':'Morgan'},{'name':'Avery','value':'Avery'},{'name':'Jordan','value':'Jordan'},{'name':'Cameron','value':'Cameron'}]}]},
]

class DiscordBridge:
    """One Discord bot on the shared staff codebase.

    scope='Morgan' is the command host: it owns the staff floor, the
    slash-command menu, and (as a fallback) any agent channel whose
    agent has no token yet. scope='' is the retired shared bot: it
    spawns scoped bridges but owns no channel. scope='Avery' (and so
    on) is that agent's own bot: it owns only its own channel, threads
    under it, plus its own DMs, and its typing, replies, and
    voice-call presence all carry that agent's identity. Same code,
    same voices, same tools — the token is what makes the bot.
    """
    def __init__(self, root, chat, stop, scope=''):
        self.root=Path(root); self.chat=chat; self.stop=stop; self.scope=scope; self._thread_parents={}
        self._registered_token=None; self._agent_bridges={}; self._voice_supervisor=None; self._last_spawn=0

    def config(self): return dotenv_values(self.root/'.env')
    def state_path(self):
        suffix=('-'+self.scope.lower()) if self.scope else ''
        return self.root/'work'/('discord-state'+suffix+'.json')
    def state(self):
        try:return json.loads(self.state_path().read_text())
        except (OSError,ValueError,TypeError):return {'enabled':False,'connected':False,'lastError':'','lastMessageAt':''}
    def save(self,state):
        path=self.state_path();path.parent.mkdir(exist_ok=True)
        temp=path.with_suffix('.tmp');temp.write_text(json.dumps(state));temp.chmod(0o600);temp.replace(path)
    def enabled(self):
        cfg=self.config()
        if str(cfg.get('DISCORD_ENABLED','')).lower()!='true':return False
        return bool(self.token(self.scope) if self.scope else self.token())
    def gateway_token(self):
        return self.token(self.scope) if self.scope else self.token()
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
    def download_attachments(self,data,token,suffixes=None):
        """Download a Discord message's attachments for AgentChat.

        Returns (paths, skipped_notes). Unsupported or oversize files
        are reported in skipped_notes instead of failing the message,
        so one bad file never blocks the rest of the conversation.
        Pass suffixes=AUDIO_SUFFIXES to download spoken attachments
        for transcription instead of readable files.
        """
        allowed=suffixes if suffixes is not None else INBOUND_SUFFIXES
        folder=self.root/'work'/'discord-uploads';folder.mkdir(parents=True,exist_ok=True)
        paths=[];skipped=[];total=0
        message_id=str(data.get('id','') or 'message')
        for item in (data.get('attachments') or [])[:MAX_INBOUND_FILES]:
            name=self.clean_filename(item.get('filename','attachment'))
            suffix=Path(name).suffix.lower()
            if suffix not in allowed:
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
    def app_id(self,token):
        return str(self.api('GET','/applications/@me',token).get('id',''))
    def register_commands(self,token=None):
        """Register the slash-command menu on Morgan's bot application (the command host).

        Bulk PUT replaces that application's whole command list, so it
        runs once per token value (see _maybe_register_commands), not
        on every gateway reconnect.
        """
        token=token or self.token()
        if not token:raise ValueError('Enter a Discord bot token.')
        app=self.app_id(token)
        if not app:raise RuntimeError('Discord did not return an application ID.')
        return self.api('PUT','/applications/'+app+'/commands',token,json=DISCORD_COMMANDS)
    def _maybe_register_commands(self,token):
        if getattr(self,'_registered_token',None)==token:return
        try:self.register_commands(token)
        except Exception as error:
            state=self.state();state.update({'lastError':'Discord command registration failed: '+str(error)[:120]});self.save(state)
        self._registered_token=token
    def ogg_to_mp3(self,ogg_bytes):
        """Convert spoken Ogg Opus bytes to MP3, which every Discord client plays inline."""
        import shutil, subprocess, tempfile
        if len(ogg_bytes)>MAX_AUDIO_BYTES:raise ValueError('Generated audio exceeds the file limit.')
        ffmpeg=shutil.which('ffmpeg')
        if not ffmpeg:raise RuntimeError('Audio conversion needs ffmpeg, which is not installed in the dashboard container.')
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'input.ogg';dst=Path(tmp)/'output.mp3'
            src.write_bytes(ogg_bytes)
            result=subprocess.run([ffmpeg,'-y','-v','error','-i',str(src),'-vn','-c:a','libmp3lame',str(dst)],capture_output=True,text=True,timeout=60)
            if result.returncode!=0 or not dst.exists() or dst.stat().st_size==0:raise RuntimeError('Audio conversion to MP3 failed.')
            data=dst.read_bytes()
        if len(data)>MAX_AUDIO_BYTES:raise ValueError('Converted audio exceeds the file limit.')
        return data
    def post_audio(self,channel,token,mp3_bytes,agent,caption):
        self.api('POST','/channels/'+channel+'/messages',token,
                 files={'files[0]':('voice-reply.mp3',io.BytesIO(mp3_bytes))},
                 data={'payload_json':json.dumps({'content':caption,'allowed_mentions':{'parse':[]}})})
    def voice_reply(self,channel,token,agent,answer):
        """Speak an answer in the agent's voice and post it as audio plus text.

        The text stays in the channel so a voice reply is auditable
        like any other staff message. Raises on any TTS or send
        failure so the caller can fall back to a text-only reply.
        """
        from staff.voice import speak, log_voice_send
        spoken=speak(self.root,agent,answer)
        mp3=self.ogg_to_mp3(spoken['data'])
        self.post_audio(channel,token,mp3,agent,'**'+agent+'**\n'+answer[:1900])
        try:log_voice_send(self.root,agent,'discord-voice-reply',channel,answer,spoken.get('duration_seconds'),provider=spoken.get('provider'))
        except Exception:pass
        return spoken
    def answer_with_fragment_guard(self,agent,body,request_id,paths):
        """Ask for a voice turn, re-asking once when the answer is cut off.

        Mirrors the Discord voice flow: a fragment spoken aloud cannot
        be unsent, so a truncated first answer is replaced only by a
        complete retry, and the original is kept when the retry also
        fragments. Voice turns never run the tool loop.
        """
        answer=self.chat.ask(agent,body,request_id,source='discord',attachments=paths,no_tools=True)
        if is_truncated_reply(answer):
            try:
                retry=self.chat.ask(agent,body+'\n\n(Your previous reply came out cut off mid-sentence. Please answer again in full, in complete sentences.)',str(uuid.uuid4()),source='discord',attachments=paths,no_tools=True)
            except Exception:
                retry=''
            if retry and retry.strip() and not is_truncated_reply(retry):
                answer=retry
        return answer
    def followup(self,token,interaction,content):
        app=self.app_id(token)
        return self.api('POST','/webhooks/'+app+'/'+str(interaction.get('token','')),token,json={'content':content,'allowed_mentions':{'parse':[]}})
    def followup_voice(self,token,interaction,agent,answer):
        from staff.voice import speak, log_voice_send
        spoken=speak(self.root,agent,answer)
        mp3=self.ogg_to_mp3(spoken['data'])
        app=self.app_id(token)
        result=self.api('POST','/webhooks/'+app+'/'+str(interaction.get('token','')),token,
                 files={'files[0]':('voice-reply.mp3',io.BytesIO(mp3))},
                 data={'payload_json':json.dumps({'content':'**'+agent+'**\n'+answer[:1900],'allowed_mentions':{'parse':[]}})})
        try:log_voice_send(self.root,agent,'discord-voice-reply',str(interaction.get('channel_id','')),answer,spoken.get('duration_seconds'),provider=spoken.get('provider'))
        except Exception:pass
        return result
    def handle_message(self,data,token):
        """Handle one gateway MESSAGE_CREATE the way run() used to inline.

        Typing and replies use the answering agent's own bot token
        when one is configured, so the Discord author reads Avery,
        Morgan, and so on; otherwise they fall back to the receiving bot's
        token and the bold agent prefix inside the message.
        """
        author=data.get('author',{})
        if author.get('bot'):return
        content=str(data.get('content','')).strip();channel=str(data.get('channel_id',''))
        attachments=data.get('attachments') or []
        stickers=[str(s.get('name','')).strip() for s in (data.get('sticker_items') or []) if str(s.get('name','')).strip()]
        content=re.sub(r'<:([A-Za-z0-9_]+):\d+>',r':\1:',content)
        if (not content and not attachments and not stickers) or len(content)>4000:return
        agent,body=self.route_agent(content,channel,default=self.scope or 'Morgan',user_id=str(author.get('id','')))
        if stickers:body=(body+'\n' if body.strip() else '')+'[User sent sticker(s): '+', '.join(stickers)+']'
        voice_reply,body=parse_voice_command(body,False)
        agent_token=self.token(agent) or token
        if voice_reply and not body.strip() and not attachments:
            self.api('POST','/channels/'+channel+'/messages',agent_token,json={'content':'**'+agent+'**\nSend /voice followed by your message and I will reply with a voice message.','allowed_mentions':{'parse':[]}})
            return
        try:
            self.api('POST','/channels/'+channel+'/typing',agent_token)
            spoken_items=[];other_items=[]
            for item in attachments:
                (spoken_items if Path(str(item.get('filename',''))).suffix.lower() in AUDIO_SUFFIXES else other_items).append(item)
            paths,skipped=self.download_attachments({'id':data.get('id'),'attachments':other_items},token)
            audio_paths,audio_skipped=self.download_attachments({'id':data.get('id'),'attachments':spoken_items},token,suffixes=AUDIO_SUFFIXES)
            skipped+=audio_skipped
            transcripts=[]
            for path in audio_paths:
                try:
                    from staff.voice import transcribe
                    transcripts.append(transcribe(self.root,path.read_bytes(),path.suffix.lower()))
                except Exception:
                    self.api('POST','/channels/'+channel+'/messages',agent_token,json={'content':'**'+agent+'**\nI could not transcribe that voice message. Send it again or type it out and I will answer.','allowed_mentions':{'parse':[]}})
                    return
            if transcripts:
                voice_reply=True
                block='\n'.join('Voice message transcribed: "'+t+'"' for t in transcripts)
                body=(body+'\n'+block) if body.strip() else block
            if not body.strip():body='Please describe or analyze the attached files.'
            if skipped:body+='\nNote: the following Discord attachments were not forwarded to the agent: '+'; '.join(skipped)+'.'
            if voice_reply:
                answer=self.answer_with_fragment_guard(agent,body,'discord:'+str(data.get('id','')),paths)
                if not answer.strip():
                    self.api('POST','/channels/'+channel+'/messages',agent_token,json={'content':'**'+agent+'**\nI did not have a reply to speak just now. Send your message again and I will answer by voice.','allowed_mentions':{'parse':[]}})
                else:
                    try:self.voice_reply(channel,agent_token,agent,answer)
                    except Exception:
                        self.api('POST','/channels/'+channel+'/messages',agent_token,json={'content':'**'+agent+'**\n'+answer[:1900],'allowed_mentions':{'parse':[]}})
            else:
                answer=self.chat.ask(agent,body,'discord:'+str(data.get('id','')),source='discord',attachments=paths)
                self.api('POST','/channels/'+channel+'/messages',agent_token,json={'content':'**'+agent+'**\n'+answer[:1900],'allowed_mentions':{'parse':[]}})
                failed=[]
                for path in self.resolve_answer_files(answer):
                    try:self.post_file(channel,agent_token,path,agent)
                    except Exception:failed.append(Path(path).name)
                if failed:
                    self.api('POST','/channels/'+channel+'/messages',agent_token,json={'content':'**'+agent+'**\n'+agent+' could not upload: '+', '.join(failed),'allowed_mentions':{'parse':[]}})
            state=self.state();state.update({'connected':True,'lastMessageAt':time.strftime('%Y-%m-%dT%H:%M:%S')});self.save(state)
        except Exception as error:
            state=self.state();state.update({'lastError':str(error)[:180]});self.save(state)
    def handle_interaction(self,data,token):
        """Handle one slash-command interaction: ack fast, answer, follow up.

        The deferred ack (type 5) goes out before any model call so
        the 3-second interaction window is never the bottleneck; the
        answer arrives as a followup message. Unknown commands and bot
        users are ignored.
        """
        if data.get('type')!=2:return
        user=(data.get('member') or {}).get('user') or data.get('user') or {}
        if user.get('bot'):return
        cmd=str((data.get('data') or {}).get('name',''))
        options={str(o.get('name','')):o.get('value') for o in (data.get('data') or {}).get('options') or []}
        channel=str(data.get('channel_id',''))
        try:
            self.api('POST','/interactions/'+str(data.get('id',''))+'/'+str(data.get('token',''))+'/callback',token,json={'type':5})
        except Exception as error:
            state=self.state();state.update({'lastError':str(error)[:180]});self.save(state);return
        try:
            if cmd=='help':
                self.followup(token,data,'**Onyx & Ink Staff**\nUse /voice followed by a message for a spoken reply, or /morgan, /avery, /jordan, /cameron to talk to one agent. In an agent channel, plain messages go to that agent; in the staff floor, start with an agent name, like "Avery: ...". Send a voice message in chat and the agent answers by voice. Use /join-voice to bring an agent into a voice channel, /leave-voice to send them out.')
                return
            agent_map={name.lower():name for name in STAFF}
            if cmd in agent_map:
                agent=agent_map[cmd];body=str(options.get('message','') or '')
                answer=self.chat.ask(agent,body,'discord:'+str(data.get('id','')),source='discord',attachments=[])
                self.followup(token,data,'**'+agent+'**\n'+answer[:1900])
                return
            if cmd=='voice':
                body=str(options.get('message','') or '')
                voice_user=(data.get('member') or {}).get('user') or data.get('user') or {}
                agent,_body=self.route_agent(body,channel,user_id=str(voice_user.get('id','')))
                answer=self.answer_with_fragment_guard(agent,body,'discord:'+str(data.get('id','')),[])
                if not answer.strip():
                    self.followup(token,data,'**'+agent+'**\nI did not have a reply to speak just now. Send your message again and I will answer by voice.')
                    return
                try:self.followup_voice(token,data,agent,answer)
                except Exception:
                    self.followup(token,data,'**'+agent+'**\n'+answer[:1900])
                return
            if cmd in ('join-voice','leave-voice'):
                agent=str(options.get('agent','') or '').title()
                if agent not in STAFF:
                    self.followup(token,data,'Pick one of Morgan, Avery, Jordan, or Cameron.')
                    return
                supervisor=self.voice_supervisor()
                if cmd=='join-voice':
                    if not self.config().get('DISCORD_'+agent.upper()+'_BOT_TOKEN'):
                        self.followup(token,data,'**'+agent+'** does not have their own Discord bot token yet. Add it in dashboard Settings → Discord, then try again — a voice call needs the agent to join as themself, not as the shared staff bot.')
                        return
                    voice_channel=str(options.get('channel','') or '')
                    guild=str(data.get('guild_id','') or '')
                    if not self.valid_channel(voice_channel) or not self.valid_channel(guild):
                        self.followup(token,data,'Pick a voice channel in this server and try again.')
                        return
                    supervisor.request_join(agent,guild,voice_channel,text_channel=channel)
                    self.followup(token,data,'**'+agent+'** is joining the voice channel. Say hello when you see them there.')
                else:
                    supervisor.request_leave(agent)
                    self.followup(token,data,'**'+agent+'** is leaving the voice channel.')
                return
        except Exception as error:
            state=self.state();state.update({'lastError':str(error)[:180]});self.save(state)
            try:self.followup(token,data,'That command hit an error. Try it again in a moment.')
            except Exception:pass
    def check(self,token=None):
        token=token or self.gateway_token()
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
    def sender_ready(self, sender=''):
        """True when Discord is enabled and this sender (or the retired shared bot) has a token."""
        cfg=self.config()
        if str(cfg.get('DISCORD_ENABLED','')).lower()!='true':return False
        return bool(self.token(sender) or self.token())

    def send(self, sender, text, files=(), recipient=''):
        if not self.sender_ready(sender):raise ValueError('Enable Discord and save a bot token in Settings.')
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
    def create_poll(self, channel, token, question, answers, duration_hours=24):
        """Post a native Discord poll. Answers: 2-10 short options."""
        answers = [str(a)[:55] for a in answers if str(a).strip()]
        if not (2 <= len(answers) <= 10):
            raise ValueError('A Discord poll needs 2-10 answers.')
        try:
            duration = max(1, min(768, int(duration_hours)))
        except (TypeError, ValueError):
            duration = 24
        poll = {'question': {'text': str(question)[:300]},
                'answers': [{'answer_id': i + 1, 'poll_media': {'text': a}}
                            for i, a in enumerate(answers)],
                'duration': duration, 'allow_multiselect': False}
        return self.api('POST', '/channels/' + str(channel) + '/messages', token, json={'poll': poll})

    def read_poll(self, channel, message_id, token):
        """Read a posted poll's question, answers, and vote counts."""
        msg = self.api('GET', '/channels/' + str(channel) + '/messages/' + str(message_id), token)
        poll = msg.get('poll') or {}
        counts = {str(c.get('id')): c.get('count', 0)
                  for c in (poll.get('results') or {}).get('answer_counts', [])}
        return {'question': (poll.get('question') or {}).get('text', ''),
                'answers': [{'id': a.get('answer_id'),
                             'text': (a.get('poll_media') or {}).get('text', ''),
                             'votes': counts.get(str(a.get('answer_id')), 0)}
                            for a in poll.get('answers', [])],
                'finalized': (poll.get('results') or {}).get('is_finalized', False)}

    def react(self, channel, message_id, emoji, token):
        """Add a reaction to a message. Emoji may be unicode or a custom :name:."""
        from urllib.parse import quote
        return self.api('PUT', '/channels/' + str(channel) + '/messages/' + str(message_id)
                        + '/reactions/' + quote(str(emoji), safe='') + '/@me', token)

    def send_sticker(self, channel, token, guild_id, name):
        """Send a server sticker by name (bots can only send guild stickers)."""
        stickers = self.api('GET', '/guilds/' + str(guild_id) + '/stickers', token)
        want = str(name).strip().lower()
        for st in stickers:
            if str(st.get('name', '')).lower() == want:
                return self.api('POST', '/channels/' + str(channel) + '/messages', token,
                                json={'sticker_ids': [st['id']], 'allowed_mentions': {'parse': []}})
        raise ValueError("No sticker named '" + str(name) + "' in this server.")

    def handle_reaction(self, data):
        """Log a reaction on an owned channel. No replies, so no reaction spam."""
        channel = str(data.get('channel_id', ''))
        if data.get('guild_id') and not self._owns_channel(channel, data.get('guild_id')):
            return
        user = (data.get('member') or {}).get('user') or {}
        if user.get('bot'):
            return
        row = {'time': time.strftime('%Y-%m-%dT%H:%M:%S'), 'channel': channel,
               'message': str(data.get('message_id', '')),
               'emoji': (data.get('emoji') or {}).get('name', ''),
               'user': str(user.get('id', ''))}
        try:
            with (self.root / 'work' / 'discord-reactions.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(row) + '\n')
        except OSError:
            pass

    def _thread_parent(self, channel):
        """Parent channel of a thread, cached per bridge. None when unknown or unreachable."""
        if channel in self._thread_parents:
            return self._thread_parents[channel]
        parent = None
        try:
            info = self.api('GET', '/channels/' + str(channel), self.gateway_token())
            parent = info.get('parent_id')
        except Exception:
            parent = None
        self._thread_parents[channel] = parent
        return parent

    def _owns_channel(self, channel, guild_id):
        """True when this bot instance owns a guild channel (or a thread under it).

        DMs need no rule: a gateway only ever sees its own bot's DMs.
        An agent's own bot owns its channel and threads under it; an
        agent channel without a token falls back to Morgan, the
        command host; Morgan also owns the staff floor and anything
        else. Without this, every bot in the channel would answer the
        same message.
        """
        if not guild_id:
            return True
        channel = str(channel)
        cfg = self.config()
        for agent in STAFF:
            if str(cfg.get('DISCORD_' + agent.upper() + '_CHANNEL_ID', '')) == channel:
                if agent == self.scope:
                    return True
                if cfg.get('DISCORD_' + agent.upper() + '_BOT_TOKEN'):
                    return False
                return self.scope == 'Morgan'
        if str(cfg.get('DISCORD_STAFF_CHANNEL_ID', '')) == channel:
            return self.scope == 'Morgan' and bool(cfg.get('DISCORD_MORGAN_BOT_TOKEN'))
        parent = self._thread_parent(channel)
        if parent:
            if str(cfg.get('DISCORD_STAFF_CHANNEL_ID', '')) == str(parent):
                return self.scope == 'Morgan' and bool(cfg.get('DISCORD_MORGAN_BOT_TOKEN'))
            for agent in STAFF:
                if str(cfg.get('DISCORD_' + agent.upper() + '_CHANNEL_ID', '')) == str(parent):
                    if agent == self.scope:
                        return True
                    if cfg.get('DISCORD_' + agent.upper() + '_BOT_TOKEN'):
                        return False
                    return self.scope == 'Morgan'
        return self.scope == 'Morgan' and bool(cfg.get('DISCORD_MORGAN_BOT_TOKEN'))

    def owns_message(self, data):
        return self._owns_channel(data.get('channel_id', ''), data.get('guild_id'))

    # Conversation follower: staff-floor (and other shared-channel) messages
    # that name no agent are routed by topic, and otherwise stay with the
    # agent this user last talked to in that channel. Word-boundary
    # patterns only, so "artwork" never trips Morgan's "work" and
    # "website" never trips Jordan's "site". A tie, or no hit at all,
    # falls through to the follower, then to the default (Morgan).
    TOPIC_KEYWORDS = {
        'Cameron': [r'\brefund\b', r'\bprivacy\b', r'\blegal\b',
                     r'\bpolic(?:y|ies)\b', r'\bcontract\b', r'\bcompliance\b',
                     r'\bhiring\b', r'\bemployee\b', r'\bhandbook\b',
                     r'\breturn policy\b', r'\bshipping policy\b',
                     r'\bterms of service\b', r'\bhr\b'],
        'Jordan': [r'\bwebsite\b', r'\bgithub\b', r'\bcloudflare\b',
                   r'\bpreview\b', r'\bstorefront\b', r'\bcheckout\b',
                   r'\bsquare\b', r'\bdomain\b', r'\bdeploy\b',
                   r'\bcountdown\b', r'\brepo\b', r'\bcodebase\b',
                   r'\bbug\b', r'\bsite\b'],
        'Avery': [r'\bdesign\b', r'\btumbler\b', r'\bwrap\b', r'\bmockup\b',
                  r'\bvisual\b', r'\bcolou?rs?\b', r'\bart\b', r'\bartworks?\b',
                  r'\bmarketing\b', r'\bphoto\b', r'\bpicture\b',
                  r'\bgraphic\b', r'\bpng\b', r'\bjpg\b', r'\bimage\b'],
        'Morgan': [r'\blaunch\b', r'\boperations?\b', r'\bstatus\b',
                   r'\bschedule\b', r'\bteam\b', r'\bmeeting\b',
                   r'\bplan\b', r'\bprogress\b', r'\bupdate\b',
                   r'\blooking\b', r'\beverything\b', r'\btrack\b',
                   r'\bworking\b', r'\bwork\b'],
    }
    FOLLOWER_TTL_SECONDS = 3600

    def _follower_path(self):
        return Path(self.root) / 'work' / 'discord-follower.json'

    def _follower_load(self):
        try:
            data = json.loads(self._follower_path().read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _follower_save(self, data):
        try:
            path = self._follower_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(data))
            os.replace(tmp, path)
        except OSError:
            pass

    def _follower_get(self, channel_id, user_id):
        if not user_id:
            return None
        entry = self._follower_load().get(str(channel_id) + ':' + str(user_id))
        if not isinstance(entry, dict):
            return None
        if entry.get('agent') not in STAFF:
            return None
        try:
            fresh = (time.time() - float(entry.get('ts', 0))) < self.FOLLOWER_TTL_SECONDS
        except (TypeError, ValueError):
            fresh = False
        return entry.get('agent') if fresh else None

    def _follower_set(self, channel_id, user_id, agent):
        if not user_id or agent not in STAFF:
            return
        data = self._follower_load()
        data[str(channel_id) + ':' + str(user_id)] = {'agent': agent, 'ts': time.time()}
        self._follower_save(data)

    def _topic_winner(self, message):
        scores = {}
        for agent, patterns in self.TOPIC_KEYWORDS.items():
            hits = sum(1 for pattern in patterns if re.search(pattern, message, re.I))
            if hits:
                scores[agent] = hits
        if not scores:
            return None
        best = max(scores.values())
        winners = [agent for agent, score in scores.items() if score == best]
        return winners[0] if len(winners) == 1 else None

    def route_agent(self,message,channel_id,default='Morgan',user_id=''):
        cfg=self.config()
        for agent in STAFF:
            if str(cfg.get('DISCORD_'+agent.upper()+'_CHANNEL_ID',''))==str(channel_id):return agent, message
        hit=re.match(r'^\s*(Morgan|Avery|Jordan|Cameron)\b\s*[:,]?\s*',message,re.I)
        if hit:
            agent=hit[1].title()
            self._follower_set(channel_id,user_id,agent)
            return agent,message[hit.end():]
        winner=self._topic_winner(message)
        if winner:
            self._follower_set(channel_id,user_id,winner)
            return winner,message
        followed=self._follower_get(channel_id,user_id)
        if followed:
            self._follower_set(channel_id,user_id,followed)
            return followed,message
        self._follower_set(channel_id,user_id,default)
        return default,message
    def _spawn_missing_agent_bridges(self,thread_factory=None):
        """Start one gateway thread per configured agent bot, once each.

        Called by the shared bridge at startup and about once a
        minute while connected, so pasting a token into Settings
        brings that agent's bot online without a dashboard restart.
        A bridge whose token is later removed idles in its own loop
        and revives if the token returns.
        """
        import threading as _threading
        factory=thread_factory or _threading.Thread
        cfg=self.config()
        for agent in STAFF:
            if agent in self._agent_bridges:continue
            if not cfg.get('DISCORD_'+agent.upper()+'_BOT_TOKEN'):continue
            bridge=DiscordBridge(self.root,self.chat,self.stop,scope=agent)
            factory(target=bridge.run,daemon=True).start()
            self._agent_bridges[agent]=bridge
    def voice_supervisor(self):
        """Lazily start the voice-call supervisor thread (command host bridge only)."""
        if self._voice_supervisor is not None:return self._voice_supervisor
        from staff.discord_voice import DiscordVoiceSupervisor
        supervisor=DiscordVoiceSupervisor(self.root,self.chat,self.stop)
        supervisor.start()
        self._voice_supervisor=supervisor
        return supervisor
    def run(self):
        """Gateway intake keeps Discord usable from this local Windows dashboard."""
        try:import websocket
        except ImportError:
            state=self.state();state.update({'connected':False,'lastError':'Discord listener needs websocket-client. Rebuild the dashboard.'});self.save(state);return
        sequence=None
        while not self.stop.wait(3):
            if not self.scope:
                self._spawn_missing_agent_bridges()
            if not self.enabled():continue
            token=self.gateway_token()
            if self.scope=='Morgan':
                self._maybe_register_commands(token)
            try:
                ws=websocket.create_connection('wss://gateway.discord.gg/?v=10&encoding=json',timeout=15)
                hello=json.loads(ws.recv());interval=hello['d']['heartbeat_interval']/1000
                ws.send(json.dumps({'op':2,'d':{'token':token,'intents':34305,'properties':{'os':'windows','browser':'onyx-and-ink','device':'onyx-and-ink'}}}))
                state=self.state();state.update({'enabled':True,'connected':True,'lastError':''});self.save(state)
                next_beat=time.monotonic()+interval
                while not self.stop.is_set() and self.enabled():
                    if time.monotonic()>=next_beat:
                        ws.send(json.dumps({'op':1,'d':sequence}));next_beat=time.monotonic()+interval
                    if not self.scope and time.monotonic()-self._last_spawn>=60:
                        self._spawn_missing_agent_bridges();self._last_spawn=time.monotonic()
                    ws.settimeout(1)
                    try:packet=json.loads(ws.recv())
                    except websocket.WebSocketConnectionClosedException:
                        state=self.state();state.update({'connected':False,'lastError':'Discord closed the gateway connection.'});self.save(state)
                        try:ws.close()
                        except Exception:pass
                        break
                    except Exception:continue
                    sequence=packet.get('s',sequence)
                    if packet.get('op')==7:break
                    if packet.get('t')=='INTERACTION_CREATE':
                        self.handle_interaction(packet.get('d',{}),token)
                        continue
                    if packet.get('t')=='MESSAGE_REACTION_ADD':
                        self.handle_reaction(packet.get('d',{}))
                        continue
                    if packet.get('t')!='MESSAGE_CREATE':continue
                    if self.owns_message(packet.get('d',{})):
                        self.handle_message(packet.get('d',{}),token)
                ws.close()
            except Exception as error:
                state=self.state();state.update({'connected':False,'lastError':str(error)[:180]});self.save(state)
