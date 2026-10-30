"""Local staff dashboard. Bound to loopback; never exposes credentials."""
import json
from contextlib import closing, nullcontext
import os
import re
from pathlib import Path
import secrets
import ssl
import sqlite3
import subprocess
import sys
import threading
import signal
import time
from urllib.parse import urlparse, parse_qs
import fcntl
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dotenv import dotenv_values, set_key
from staff.dashboard_mail import Mailbox
from staff.staff_email import StaffMail,signature_settings,save_signature_settings,reset_signature_settings,valid_address
from staff.report_format import normalize_report
from staff.delivery_tracking import check_delivery
from staff.inbox_state import InboxState
from staff.staff_autonomy import StaffAutonomy
from staff.report_projects import create_report, delete_project, delete_report, ensure_project, projects, consolidate_assignment_reports, project_overview, save_project_overview
from staff.staff_tasks import status as staff_task_status
from staff.report_records import export_google_doc, metadata_many as report_metadata_many, report_pdf

os.environ['CREWAI_TELEMETRY_DISABLED']='true'
os.environ['CREWAI_TRACING_ENABLED']='false'
os.environ['OTEL_SDK_DISABLED']='true'
ROOT = Path(__file__).resolve().parent
CLOUD_MODE = os.environ.get('ONYX_CLOUD_MODE','').lower() in ('1','true','yes')
PUBLIC_ORIGIN = os.environ.get('ONYX_PUBLIC_ORIGIN','').rstrip('/')
APP_HOST = os.environ.get('ONYX_BIND_HOST','0.0.0.0' if CLOUD_MODE else '127.0.0.1')
APP_PORT = int(os.environ.get('PORT','8765'))
STAFF = [('Morgan','Chief Operating Officer','MORGAN_EMAIL'),('Avery','Marketing Lead','AVERY_EMAIL'),('Jordan','IT & Storefront Development Lead','JORDAN_EMAIL'),('Cameron','Legal & HR Lead','CAMERON_EMAIL')]
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
LOCKED_REQUESTS={'/api/model-settings','/api/mode','/api/autonomy','/api/signatures','/api/report-project','/api/project-overview','/api/create-report','/api/delete-report','/api/discord','/api/discord-provision'}
MONITOR = None
WORKSPACE_SETUP = None
MAILBOX = Mailbox(ROOT)
STOP = threading.Event()
DESIRED = ROOT/'work'/'monitor-enabled.json'
from staff.dashboard_access import Access
from staff.agent_chat import AgentChat, TelegramBridge
from staff.discord_bridge import DiscordBridge
from staff.agent_actions import Actions
from staff.model_routing import routing_status, DEFAULTS
ACCESS = Access(ROOT)
CHAT = AgentChat(ROOT)
TELEGRAM = TelegramBridge(ROOT,CHAT,STOP)
TELEGRAM_AGENTS={name:TelegramBridge(ROOT,CHAT,STOP,name,'TELEGRAM_'+name.upper()+'_BOT_TOKEN') for name,_,_ in STAFF}
DISCORD = DiscordBridge(ROOT,CHAT,STOP)
AUTONOMY = StaffAutonomy(ROOT)


def provider_status(name):
    try:return json.loads((ROOT/'work'/(name+'-status.json')).read_text())
    except (OSError,ValueError):return {}


def workspace_status():
    if WORKSPACE_SETUP and WORKSPACE_SETUP.poll() is None:
        return {'state':'authorizing','message':'Google sign-in is open on this Windows PC.'}
    token=ROOT/'work'/'google-workspace-token.json';saved=provider_status('workspace')
    if not token.exists():
        if saved.get('state')=='failed' and 'outside the configured Workspace domain' in saved.get('message',''):
            return {'state':'failed','message':'Reconnect Google Workspace once to apply the updated account verification.'}
        return saved if saved.get('state')=='failed' else {'state':'not_connected','message':'Google Workspace sign-in needed.'}
    try:
        from google.oauth2.credentials import Credentials
        credentials=Credentials.from_authorized_user_file(str(token))
        if not credentials.refresh_token:raise ValueError()
        if 'https://www.googleapis.com/auth/drive' not in (credentials.scopes or []):
            return {'state':'failed','message':'Reconnect Google Workspace to allow staff to locate approved Drive design files.'}
        return {'state':'connected','account':saved.get('account',''),'checked':saved.get('checked',''),'message':saved.get('message','Authorization saved locally.')}
    except Exception:return {'state':'failed','message':'Saved Workspace authorization needs renewal.'}


def monitor_active():
    path = ROOT/'work'/'inbox-monitor.lock'
    if not path.exists(): return False
    with path.open('a') as stream:
        try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return True
        fcntl.flock(stream,fcntl.LOCK_UN)
    return False


def monitor_pid():
    result=subprocess.run(['/usr/sbin/lsof','-t',str(ROOT/'work'/'inbox-monitor.lock')],capture_output=True,text=True)
    for value in result.stdout.split():
        if not value.isdigit():continue
        pid=int(value)
        command=subprocess.run(['/bin/ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
        if 'inbox_monitor.py' in command and pid != os.getpid():return pid
    return None


def desired():
    try:return json.loads(DESIRED.read_text()).get('enabled',False)
    except (OSError,ValueError):return False


def set_desired(value):
    DESIRED.parent.mkdir(exist_ok=True)
    temporary=DESIRED.with_suffix('.tmp');temporary.write_text(json.dumps({'enabled':value}));temporary.replace(DESIRED)


def start_monitor():
    global MONITOR
    if monitor_active() or (MONITOR and MONITOR.poll() is None):return
    config=dotenv_values(ROOT/'.env')
    if config.get('STAFF_EMAIL_MODE','off')=='off':raise ValueError('Choose Draft or Live before starting.')
    if not (ROOT/'work'/'google-mail-token.json').exists():raise ValueError('Complete Google sign-in first.')
    env=dict(os.environ)
    for key in config:env.pop(key,None)
    with (ROOT/'work'/'dashboard-monitor.log').open('ab') as output:
        MONITOR=subprocess.Popen([sys.executable,'-u',str(ROOT/'inbox_monitor.py')],cwd=ROOT,stdout=output,stderr=output,env=env)


def supervise():
    while not STOP.wait(10):
        with LOCK:
            if desired():
                try:start_monitor()
                except (ValueError,OSError):pass
            try:AUTONOMY.tick()
            except (ValueError,OSError):pass
            try:post_daily_brief()
            except Exception:pass
            try:post_daily_staff_handoffs()
            except Exception:pass


def post_daily_brief(force=False):
    """Publish one concise Morgan assignment brief each local day to a channel."""
    config=dotenv_values(ROOT/'.env');target=str(config.get('TELEGRAM_DAILY_BRIEF_CHAT','')).strip()
    token=config.get('TELEGRAM_MORGAN_BOT_TOKEN','')
    discord_ready=DISCORD.enabled() and bool(str(config.get('DISCORD_DAILY_BRIEF_CHANNEL_ID','')).strip() or str(config.get('DISCORD_STAFF_CHANNEL_ID','')).strip())
    if (not target or not token) and not discord_ready:return False
    if target and not re.fullmatch(r'(?:-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31})',target):return False
    from zoneinfo import ZoneInfo
    now=datetime.now(ZoneInfo('America/New_York'));scheduled=str(config.get('TELEGRAM_DAILY_BRIEF_TIME','09:00')).strip()
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',scheduled):scheduled='09:00'
    path=ROOT/'work'/'telegram-daily-brief.json'
    try:state=json.loads(path.read_text())
    except (OSError,ValueError,TypeError):state={}
    if not force and (now.strftime('%H:%M')<scheduled or state.get('day')==now.date().isoformat()):return False
    project,name,folder=active_project(ROOT)
    lines=['Morgan Daily Brief — '+now.strftime('%b %-d, %Y'),'', 'Project: '+name]
    overview=project_overview(ROOT,project)['body']
    goals=next((part.strip() for part in overview.split('## Goals',1)[-1].split('##',1) if part.strip()),'Review the project overview.')
    lines.extend(['', 'Priority:',goals[:700]])
    lines.extend(['', 'Assignments:'])
    roles={'morgan-operations.md':'Morgan · Operations','avery-marketing.md':'Avery · Marketing','jordan-web-it.md':'Jordan · Web / IT','cameron-hr-legal.md':'Cameron · HR / Legal'}
    for filename,label in roles.items():
        report=folder/'agent-reports'/filename
        if report.exists():
            text=report.read_text(encoding='utf-8').strip().replace('\n',' ')
            lines.append('• '+label+': '+text[:420])
        else:lines.append('• '+label+': review the project overview and record the next assignment.')
    lines.extend(['', 'Reply in the appropriate agent chat with blockers or completed work.'])
    text='\n'.join(lines)[:3900]
    if target and token: TELEGRAM_AGENTS['Morgan'].api(token,'sendMessage',{'chat_id':target,'text':text})
    if discord_ready: DISCORD.daily_brief(text)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps({'day':now.date().isoformat(),'posted':now.isoformat(),'chat':target}));temp.chmod(0o600);temp.replace(path)
    return True


def post_daily_staff_handoffs(force=False):
    """Let Morgan open one bounded, targeted staff-group check-in each day."""
    config=dotenv_values(ROOT/'.env');target=str(config.get('TELEGRAM_AGENT_GROUP_CHAT','')).strip()
    token=config.get('TELEGRAM_MORGAN_BOT_TOKEN','')
    if not target or not token or not re.fullmatch(r'-?\d{5,20}',target):return False
    from zoneinfo import ZoneInfo
    now=datetime.now(ZoneInfo('America/New_York'));scheduled=str(config.get('TELEGRAM_AGENT_HANDOFF_TIME','09:15')).strip()
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',scheduled):scheduled='09:15'
    path=ROOT/'work'/'telegram-staff-handoff.json'
    try:state=json.loads(path.read_text())
    except (OSError,ValueError,TypeError):state={}
    if not force and (now.strftime('%H:%M')<scheduled or state.get('day')==now.date().isoformat()):return False
    project,name,folder=active_project(ROOT)
    roles={'Avery':'avery-marketing.md','Jordan':'jordan-web-it.md','Cameron':'cameron-hr-legal.md'}
    bridge=TELEGRAM_AGENTS['Morgan'];sent=[]
    for agent,filename in roles.items():
        report=folder/'agent-reports'/filename
        context=report.read_text(encoding='utf-8').strip().replace('\n',' ')[:700] if report.exists() else 'No saved report yet; review the project overview.'
        message='[[ONYX_STAFF target='+agent+' hops=1]] Daily check-in for '+name+'. Review your current assignment: '+context+' State your next concrete action and any blocker for Morgan.'
        bridge.api(token,'sendMessage',{'chat_id':target,'text':message})
        sent.append(agent)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps({'day':now.date().isoformat(),'posted':now.isoformat(),'chat':target,'agents':sent}));temp.chmod(0o600);temp.replace(path)
    return True


def stop_monitor():
    global MONITOR
    set_desired(False)
    pid=MONITOR.pid if MONITOR and MONITOR.poll() is None else monitor_pid()
    if pid:os.kill(pid,signal.SIGINT)
    deadline=time.monotonic()+10
    while monitor_active() and time.monotonic()<deadline:time.sleep(.1)
    if monitor_active():raise RuntimeError('Monitor is still stopping. Wait and refresh.')


def clean(text, config=None):
    for key,value in (config or dotenv_values(ROOT/'.env')).items():
        if value and any(word in key.upper() for word in ('KEY','PASSWORD','SECRET','TOKEN')):
            text=text.replace(value,'[REDACTED]')
    import re
    return re.sub(r'(?:gsk_|AIza)[A-Za-z0-9_-]+','[REDACTED]',text)


def report_display_name(path,relative):
    labels={'morgan-operations':'Morgan · Operations','avery-marketing':'Avery · Marketing','jordan-web-it':'Jordan · Web Development / IT','cameron-hr-legal':'Cameron · HR / Legal','marketing_campaign':'Marketing Campaign','web_dev_specs':'Web Development / IT','legal_terms':'HR / Legal','operational_plan':'Operations Plan'}
    title=labels.get(path.stem,path.stem.rsplit('-',1)[0].replace('_',' ').replace('-',' ').title())
    if path.parent.parent.name!='assignments':return title
    assignment=path.parent.name
    if assignment=='legacy-import':return 'Imported · '+title
    try:
        when=datetime.strptime('-'.join(assignment.split('-')[:2]),'%Y%m%d-%H%M%S')
        return when.strftime('%b %d, %Y · %I:%M %p').replace(' 0',' ')+' · '+title
    except ValueError:return title


def snapshot():
    config=dotenv_values(ROOT/'.env')
    activity=[]
    outbox=ROOT/'work'/'email-outbox'
    folders=sorted((path for path in outbox.iterdir() if path.is_dir()),key=lambda path:path.stat().st_mtime,reverse=True)[:20] if outbox.exists() else []
    files=[]
    for folder in folders:
        files.extend(folder.glob('*.eml'))
        if len(files)>=15:break
    files=sorted(files,key=lambda path:path.stat().st_mtime,reverse=True)[:15]
    for path in files:
        try:
            msg=BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            status=path.with_suffix('.status')
            activity.append({'id':str(path.relative_to(ROOT)), 'time':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),
                'sender':str(msg.get('From','')), 'to':str(msg.get('To','')), 'subject':clean(str(msg.get('Subject','')),config),
                'receipt':json.loads(path.with_suffix('.receipt.json').read_text()) if path.with_suffix('.receipt.json').exists() else None,
                'kind':str(msg.get('X-Onyx-Ink-Kind','message')), 'status':status.read_text().strip() if status.exists() else 'draft',
                'diagnostic':path.with_suffix('.diagnostic').read_text().strip() if path.with_suffix('.diagnostic').exists() else '',
                'body':clean((msg.get_body(preferencelist=('plain',)).get_content() if msg.get_body(preferencelist=('plain',)) else '')[:30000],config)})
        except (OSError,ValueError): continue
    processed=[]
    db=ROOT/'work'/'inbox-monitor.sqlite3'
    if db.exists():
        try:
            with closing(sqlite3.connect(f'file:{db}?mode=ro',uri=True)) as connection:
                processed=[{'uid':r[0],'status':r[1]} for r in connection.execute('SELECT uid,state FROM processed ORDER BY rowid DESC LIMIT 10')]
        except sqlite3.Error: pass
    reports=[]
    project_list=projects(ROOT)
    project_overviews={item['id']:project_overview(ROOT,item['id'])['body'] for item in project_list}
    report_root=ROOT/'reports'
    # The filesystem is the source of truth. Metadata enriches a report after it
    # is found; it must never decide which saved staff work is visible.
    report_paths=[path.resolve() for path in report_root.rglob('*.md')
                  if 'archive' not in path.relative_to(report_root).parts
                  and path.relative_to(report_root).parts.count('agent-reports') <= 1]
    report_paths=sorted(report_paths,key=lambda item:item.stat().st_mtime,reverse=True)
    report_meta=report_metadata_many(ROOT,report_paths)
    for path in report_paths:
        relative=path.relative_to(ROOT/'reports');project=relative.parts[0]
        owner={'morgan-assignments-legacy':'Morgan','marketing_campaign':'Avery','web_dev_specs':'Jordan','legal_terms':'Cameron','operational_plan':'Morgan','morgan-operations':'Morgan','avery-marketing':'Avery','jordan-web-it':'Jordan','cameron-hr-legal':'Cameron'}.get(path.stem,'Unassigned')
        report_group=next((group for group in ('agent-drafts','manual') if group in relative.parts),None)
        if report_group:
            try:owner=relative.parts[relative.parts.index(report_group)+1].title()
            except IndexError:pass
        meta=report_meta[str(path.relative_to(ROOT/'reports'))]
        reports.append({'id':str(relative),'name':meta['title'],'agent':owner,'project':project,
                        'created':meta['created'],'edited':meta['edited'],'googleUrl':meta.get('googleUrl',''),
                        'googleStatus':meta.get('googleStatus',''),'body':clean(normalize_report(path.read_text()[:12000]),config)})
    tools=Actions(ROOT,'Morgan','dashboard-status')
    artifacts=tools.files()
    addresses={n:config.get(k,'') for n,_,k in STAFF};website=config.get('BUSINESS_WEBSITE','https://onyxandink.org')
    try:work_status=json.loads((ROOT/'work'/'staff-work-status.json').read_text())
    except (OSError,ValueError,TypeError):work_status={}
    model_settings={provider:{'model':config.get(provider.upper()+'_MODEL',DEFAULTS[provider]),'freeConfirmed':config.get(provider.upper()+'_FREE_TIER_CONFIRMED')=='true'} for provider in ('cerebras','gemini','mistral','openrouter')}
    return {'modelSettings':model_settings,'modelRouting':routing_status(config),'cleanupDays':InboxState(ROOT).days(),'artifacts':artifacts,'actions':tools.recent(),'integrations':{'workspace':workspace_status(),'cloudflare':bool(config.get('CLOUDFLARE_API_TOKEN') and config.get('CLOUDFLARE_ACCOUNT_ID')),'freePlan':config.get('CLOUDFLARE_FREE_PLAN_CONFIRMED')=='true','cloudflareTest':provider_status('cloudflare'),'tavily':bool(config.get('TAVILY_API_KEY')),'tavilyTest':provider_status('tavily'),'square':bool(config.get('SQUARE_ACCESS_TOKEN')),'squareEnvironment':config.get('SQUARE_ENVIRONMENT','sandbox'),'cloudflarePagesProject':config.get('CLOUDFLARE_PAGES_PROJECT','')},'mode':config.get('STAFF_EMAIL_MODE','off'),'internalMode':config.get('STAFF_INTERNAL_EMAIL_MODE','draft'),'monitor':monitor_active(),'managed':bool(monitor_active()),'enabled':desired(),'autonomy':AUTONOMY.status(),'workStatus':work_status,'staffTasks':staff_task_status(ROOT),
        'authorized':(ROOT/'work'/'google-mail-token.json').exists(),'agents':[{'name':n,'role':r,'email':config.get(k,'')} for n,r,k in STAFF],'businessWebsite':config.get('BUSINESS_WEBSITE','https://onyxandink.org'),'importantCc':config.get('IMPORTANT_CC_EMAIL',''),
        'signatures':signature_settings(ROOT,addresses,website),
        'wifi':{'enabled':True if CLOUD_MODE else bool(ACCESS.settings().get('enabled')),'url':PUBLIC_ORIGIN if CLOUD_MODE else next(('https://'+host+':8766' for host in ACCESS.settings().get('hosts',[]) if host not in ('localhost','127.0.0.1')),''),'cloud':CLOUD_MODE},'telegram':{'configured':bool(config.get('TELEGRAM_BOT_TOKEN')),'enabled':TELEGRAM.state().get('enabled',False),'paired':len(TELEGRAM.state().get('users',{})),'staffGroup':config.get('TELEGRAM_AGENT_GROUP_CHAT',''),'dailyBrief':{'chat':config.get('TELEGRAM_DAILY_BRIEF_CHAT',''),'time':config.get('TELEGRAM_DAILY_BRIEF_TIME','09:00'),'configured':bool(config.get('TELEGRAM_DAILY_BRIEF_CHAT'))},'agents':{name:{'configured':bool(config.get('TELEGRAM_'+name.upper()+'_BOT_TOKEN')),'enabled':bridge.state().get('enabled',False),'paired':len(bridge.state().get('users',{}))} for name,bridge in TELEGRAM_AGENTS.items()}},'discord':{'configured':bool(config.get('DISCORD_BOT_TOKEN')),'enabled':str(config.get('DISCORD_ENABLED','')).lower()=='true','guild':config.get('DISCORD_GUILD_ID',''),'staffChannel':config.get('DISCORD_STAFF_CHANNEL_ID',''),'requestsChannel':config.get('DISCORD_AGENT_REQUESTS_CHANNEL_ID',''),'dailyBriefChannel':config.get('DISCORD_DAILY_BRIEF_CHANNEL_ID',''),'agents':{name:{'channel':config.get('DISCORD_'+name.upper()+'_CHANNEL_ID',''),'configured':bool(config.get('DISCORD_'+name.upper()+'_BOT_TOKEN'))} for name,_,_ in STAFF},'state':DISCORD.state()},'activity':activity,'processed':processed,'reports':reports,'reportProjects':project_list,'projectOverviews':project_overviews,'activeReportProject':next((item['id'] for item in project_list if item['active']),''),'token':TOKEN,'mailboxes':[{'name':'all','label':'All inboxes','email':''},{'name':'Shared','label':'Shared inbox','email':config.get('GOOGLE_MAIL_USER','')},{'name':'Owner','label':'James · CEO','email':config.get('OWNER_EMAIL','')},{'name':'Jaunee','label':'Jaunee · Vice President','email':config.get('IMPORTANT_CC_EMAIL','')},*[{'name':n,'label':n+' · '+r,'email':config.get(k,'')} for n,r,k in STAFF]]}


def outbox_file(identifier):
    path=(ROOT/str(identifier)).resolve()
    root=(ROOT/'work'/'email-outbox').resolve()
    if not path.is_relative_to(root) or path.suffix!='.eml' or not path.is_file():
        raise ValueError()
    return path


def mail_from_config(config, mode=None):
    return StaffMail.from_env(ROOT, mode=mode, config=config)


def configure_autonomy(data):
    """Apply settings and send exactly one notice for an off-to-on transition."""
    was_enabled=AUTONOMY.load().get('enabled') is True
    state=AUTONOMY.configure(data)
    if not state['enabled']:
        AUTONOMY.stop()
    else:
        AUTONOMY.tick()
    confirmation=''
    if state['enabled'] and not was_enabled:
        schedule=(state['start']+'–'+state['stop']) if state['start'] and state['stop'] else 'All day until you turn autonomous staff off'
        objective=state['objective'].strip()
        body=('CEO James and Vice President Jaunee,\n\nAutonomous staff is now on.\n\n'
              f'Working hours: {schedule}\nObjective: {objective[:500]}\n\n'
              'Morgan, Avery, Jordan, and Cameron will continue independent assignments during these hours. '
              'You will still receive a separate email if a run needs attention.')
        try:
            mail=mail_from_config(dotenv_values(ROOT/'.env'))
            recipients=['Owner']+(['Jaunee'] if valid_address(mail.addresses.get('Jaunee','')) else [])
            confirmation=mail.deliver('Morgan',recipients,'Autonomous staff is now on',body,kind='message',important=True)
        except (ValueError,OSError):
            confirmation='Autonomous staff started, but the activation email could not be prepared.'
    message='Autonomous staff settings saved.'
    if confirmation:
        message='Autonomous staff enabled. '+('Activation email accepted by the mail server.' if 'accepted by mail server' in confirmation else confirmation)
    return state,message


def live_terminal():
    base=ROOT/'work'/'live-terminal'
    current={}
    try:current=json.loads((base/'current.json').read_text())
    except Exception:pass
    output=''
    try:output=(base/'output.log').read_bytes()[-102400:].decode(errors='replace')
    except Exception:pass
    queued=''
    try:queued=json.loads((base/'request.json').read_text()).get('command','')
    except Exception:pass
    watcherAlive=False
    try:
        import time as _time
        watcherAlive=(_time.time()-(base/'watcher.json').stat().st_mtime)<15
    except Exception:pass
    return {'status':current.get('status','idle'),'command':current.get('command',''),'startedAt':current.get('startedAt'),'endedAt':current.get('endedAt'),'exitCode':current.get('exitCode'),'output':output,'queued':queued,'watcherAlive':watcherAlive}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def allowed(self):
        return self.headers.get('Host') in ACCESS.hosts()
    def reply(self,code,data,kind='application/json',extra_headers=None):
        body=json.dumps(data).encode() if kind=='application/json' else data
        self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        for key,value in (extra_headers or {}).items():self.send_header(key,value)
        self.end_headers();self.wfile.write(body)
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/workspace-oauth-callback':
            query=parse_qs(urlparse(self.path).query)
            state=query.get('state',[''])[0]
            code=query.get('code',[''])[0]
            if not state or not code:return self.reply(400,b'<h2>Google sign-in could not be completed.</h2>','text/html; charset=utf-8')
            target=ROOT/'work'/'workspace-auth-response.json';temporary=target.with_suffix('.tmp')
            temporary.write_text(json.dumps({'state':state,'query':urlparse(self.path).query}));temporary.chmod(0o600);temporary.replace(target)
            return self.reply(200,b'<h2>Google Workspace connected.</h2><p>You may close this tab and return to Onyx &amp; Ink.</p>','text/html; charset=utf-8')
        if not self.allowed():return self.reply(403,{'error':'Local access only.'})
        if path=='/healthz':return self.reply(200,{'status':'ok'})
        if path=='/app.js' or path=='/style.css':pass
        elif path=='/ca.crt':
            return self.reply(200,(ROOT/'work'/'certificates'/'onyx-dashboard-ca.crt').read_bytes(),'application/x-x509-ca-cert')
        elif not ACCESS.authenticated(self):
            if path.startswith('/api/'):return self.reply(401,{'error':'Sign in to the dashboard.'})
            return self.reply(200,(ROOT/'dashboard'/'login.html').read_bytes(),'text/html; charset=utf-8')
        if path=='/api/status':return self.reply(200,snapshot())
        if path=='/api/live-terminal':return self.reply(200,live_terminal())
        if path=='/api/attachment':
            try:
                args=parse_qs(urlparse(self.path).query);name,content=MAILBOX.attachment(args.get('id',[''])[0],args.get('part',[''])[0])
                from urllib.parse import quote
                return self.reply(200,content,'application/octet-stream',{'Content-Disposition':"attachment; filename*=UTF-8''"+quote(name,safe='')})
            except (ValueError,RuntimeError):return self.reply(400,{'error':'Attachment unavailable.'})
        if path=='/api/artifact':
            try:
                tools=Actions(ROOT,'Morgan','download')
                identifier=parse_qs(urlparse(self.path).query).get('id',[''])[0]
                from urllib.parse import quote as _quote
                try:
                    row=next((f for f in tools.files() if f['file_id']==identifier),None)
                except Exception:
                    row=None
                if row and row.get('url','').startswith('https://'):
                    self.send_response(302);self.send_header('Location',row['url']);self.send_header('Cache-Control','no-store');self.end_headers();return
                file=tools.file(identifier)
                filename=(row['filename'] if row and row.get('filename') else file.name)
                kind={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp','.gif':'image/gif','.svg':'image/svg+xml','.pdf':'application/pdf','.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','.md':'text/markdown; charset=utf-8','.txt':'text/plain; charset=utf-8','.wav':'audio/wav','.mp3':'audio/mpeg','.aac':'audio/aac','.flac':'audio/flac','.ogg':'audio/ogg','.mp4':'video/mp4','.webm':'video/webm','.mov':'video/quicktime','.mkv':'video/x-matroska'}.get(file.suffix.lower(),'application/octet-stream')
                return self.reply(200,file.read_bytes(),kind,{'Content-Disposition':"attachment; filename*=UTF-8''"+_quote(filename,safe='')})
            except (ValueError,OSError):return self.reply(404,{'error':'Generated file not found.'})
        if path=='/api/report-pdf':
            try:
                from urllib.parse import quote
                identifier=parse_qs(urlparse(self.path).query).get('id',[''])[0]
                title,content=report_pdf(ROOT,identifier)
                filename=re.sub(r'[^A-Za-z0-9._-]+','-',title).strip('-')[:100] or 'report'
                return self.reply(200,content,'application/pdf',{'Content-Disposition':"attachment; filename*=UTF-8''"+quote(filename+'.pdf',safe='')})
            except (ValueError,OSError):return self.reply(404,{'error':'Report not found.'})
        names={'/':'index.html','/overview':'index.html','/inbox':'index.html','/activity':'index.html','/reports':'index.html','/settings':'index.html','/chat':'index.html','/files':'index.html','/live':'index.html','/app.js':'app.js','/style.css':'style.css','/preview-modern.html':'preview-modern.html'}
        if path not in names:return self.reply(404,{'error':'Not found.'})
        name=names[path];kind={'html':'text/html; charset=utf-8','js':'text/javascript','css':'text/css'}[name.split('.')[-1]]
        self.reply(200,(ROOT/'dashboard'/name).read_bytes(),kind)
    def do_POST(self):
        global MONITOR, WORKSPACE_SETUP
        origin=self.headers.get('Origin','')
        allowed_origin=origin in {
            *('http://'+host for host in ACCESS.hosts()),
            *('https://'+host for host in ACCESS.hosts()),
        } | ({PUBLIC_ORIGIN} if PUBLIC_ORIGIN else set())
        if self.path=='/api/login' and self.allowed() and allowed_origin:
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length<0 or length>2048:raise ValueError()
                data=json.loads(self.rfile.read(length));session=ACCESS.login(str(data.get('password','')),self.client_address[0])
                if not session:return self.reply(401,{'error':'Incorrect password or too many attempts. Try again later.'})
                secure='; Secure' if isinstance(self.connection,ssl.SSLSocket) or CLOUD_MODE else ''
                self.send_response(200);self.send_header('Set-Cookie','onyx_session='+session+'; Path=/; HttpOnly'+secure+'; SameSite=Strict; Max-Age=43200');self.send_header('Content-Type','application/json');self.send_header('Content-Length','11');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b'{"ok":true}');return
            except Exception:return self.reply(400,{'error':'Invalid login.'})
        if not self.allowed() or not allowed_origin or self.headers.get('X-Dashboard-Token')!=TOKEN or not ACCESS.authenticated(self):
            return self.reply(403,{'error':'Local dashboard authorization required.'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            maximum=14_100_000 if self.path=='/api/upload' else 40000
            if length>maximum or length<0:raise ValueError()
            data=json.loads(self.rfile.read(length))
            if self.path=='/api/live-terminal-run':
                command=str(data.get('command','')).strip()
                if not command or len(command)>2000:return self.reply(400,{'error':'Enter a command (max 2000 characters).'})
                if any(ch in command for ch in ('&','|',';','>','<','`','$','(',')')):return self.reply(400,{'error':'One simple command only - no pipes, redirection, chaining, or substitutions.'})
                lowered=command.lower()
                if any(word in lowered for word in ('.env','authorized_keys','wifi-password','api_key','apikey','password','secret','token','ts_authkey')):return self.reply(400,{'error':'That command looks like it could print a secret. It was not queued.'})
                if any(word in lowered for word in ('del ','remove-item','format ','shutdown','restart-computer')):return self.reply(400,{'error':'That command is blocked in the Live command box. Run it directly on the PC if you really mean it.'})
                base=ROOT/'work'/'live-terminal';base.mkdir(parents=True,exist_ok=True)
                try:current=json.loads((base/'current.json').read_text())
                except Exception:current={}
                if current.get('status')=='running':
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        started=_dt.fromisoformat(str(current.get('startedAt','')).replace('Z','+00:00'))
                        stale=(_dt.now(_tz.utc)-started).total_seconds()>900
                    except Exception:stale=False
                    if not stale:return self.reply(409,{'error':'A command is already running. Wait for it to finish.'})
                if (base/'request.json').exists():return self.reply(409,{'error':'A command is already queued.'})
                tmp=base/'request.json.tmp';tmp.write_text(json.dumps({'command':command,'status':'pending'}));tmp.replace(base/'request.json')
                return self.reply(200,{'queued':True,'command':command})
            if self.path=='/api/chat-history':return self.reply(200,{'messages':CHAT.history(data.get('agent','Morgan'))})
            if self.path=='/api/chat':
                try:
                    paths=InboxState(ROOT).attachment_paths(data.get('attachments',[]))
                    answer=CHAT.ask(data.get('agent','Morgan'),str(data.get('body','')),str(data.get('requestId','')),attachments=paths)
                except ValueError as error:return self.reply(400,{'error':str(error)})
                return self.reply(200,{'answer':answer})
            if self.path=='/api/draft-reply':
                return self.reply(200,{'answer':CHAT.draft_reply(data.get('agent','Morgan'),str(data.get('body','')))})
            with (LOCK if self.path in LOCKED_REQUESTS else nullcontext()):
                if self.path=='/api/model-settings':
                    allowed={'CEREBRAS_API_KEY','CEREBRAS_MODEL','GEMINI_API_KEY','GEMINI_MODEL','GEMINI_FREE_TIER_CONFIRMED','MISTRAL_API_KEY','MISTRAL_MODEL','MISTRAL_FREE_TIER_CONFIRMED','OPENROUTER_API_KEY','OPENROUTER_MODEL'}
                    values=data.get('settings',{})
                    if not isinstance(values,dict) or set(values)-allowed:raise ValueError()
                    clean={}
                    for key,value in values.items():
                        if not isinstance(value,str) or len(value)>512 or '\n' in value or '\r' in value:raise ValueError()
                        value=value.strip()
                        if key.endswith('_CONFIRMED') and value not in ('true','false'):raise ValueError()
                        if key.endswith('_MODEL') and not re.fullmatch(r'[A-Za-z0-9_./:@-]{1,150}',value):raise ValueError()
                        if key.endswith('_API_KEY') and not value:continue
                        clean[key]=value
                    model=clean.get('OPENROUTER_MODEL','openrouter/free')
                    if model!='openrouter/free' and not model.endswith(':free'):return self.reply(400,{'error':'The final fallback must be openrouter/free or a :free model.'})
                    for key,value in clean.items():set_key(str(ROOT/'.env'),key,value,quote_mode='always')
                    if clean.get('CEREBRAS_API_KEY'):set_key(str(ROOT/'.env'),'CEREBRAS_ENABLED','true',quote_mode='never')
                    (ROOT/'.env').chmod(0o600)
                    return self.reply(200,{'message':'Cloud model settings saved. Keys stay on this Windows PC. A replacement Cerebras key automatically re-enables its route.'})
                elif self.path=='/api/wifi-password':
                    if not ACCESS.host_pc(self):return self.reply(403,{'error':'Open http://localhost:8765 on the host Windows PC to view the password.'})
                    try:return self.reply(200,{'password':ACCESS.local_password()})
                    except RuntimeError as error:return self.reply(409,{'error':str(error)})
                elif self.path in ('/api/telegram','/api/telegram-check'):
                    token=str(data.get('token','')).strip()
                    if token and not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,256}',token):raise ValueError('Enter a valid Telegram bot token.')
                    configured=token or dotenv_values(ROOT/'.env').get('TELEGRAM_BOT_TOKEN','')
                    if not configured:return self.reply(400,{'error':'Enter a Telegram bot token first.'})
                    try:bot=TELEGRAM.api(configured,'getMe',{})
                    except RuntimeError as error:return self.reply(400,{'error':str(error)+' Get a current token from BotFather and save it locally.'})
                    try:TELEGRAM.api(configured,'setMyCommands',{'commands':TELEGRAM.commands()})
                    except RuntimeError as error:return self.reply(400,{'error':'Telegram bot commands could not be registered: '+str(error)})
                    username=str(bot.get('username','')).strip()
                    message='Telegram connection verified'+(' for @'+username if username else '')+'.'
                    if self.path=='/api/telegram-check':return self.reply(200,{'message':message})
                    if token:
                        set_key(str(ROOT/'.env'),'TELEGRAM_BOT_TOKEN',token,quote_mode='never')
                    bridge=TELEGRAM.state();bridge['enabled']=bool(data.get('enabled'));bridge.pop('lastError',None);TELEGRAM.save(bridge)
                    return self.reply(200,{'message':message+(' Private-chat polling is enabled.' if bridge['enabled'] else ' Saved but polling remains disabled.')})
                elif self.path=='/api/workspace-authorize':
                    # Google Desktop OAuth permits a loopback callback only. Keep all other
                    # dashboard settings available on the LAN, but require this one-time sign-in
                    # to begin in a browser on the Windows host itself.
                    if not ACCESS.host_pc(self):return self.reply(409,{'error':'Google requires this one-time sign-in to start from the dashboard opened on the Windows host at http://localhost:8765. After it connects, Workspace is available to every staff tool and from your Mac.'})
                    if WORKSPACE_SETUP and WORKSPACE_SETUP.poll() is None:return self.reply(200,{'message':'Google sign-in is already open on this Windows PC.'})
                    if not (ROOT/'google-oauth-client.json').exists():return self.reply(409,{'error':'The Google Desktop client file is missing.'})
                    # A completed/abandoned sign-in may leave its URL behind. Remove both
                    # transient files before creating the new state so the browser receives
                    # only the URL that belongs to this authorization attempt.
                    for transient in ('workspace-auth-url.json','workspace-auth-response.json'):
                        try:(ROOT/'work'/transient).unlink()
                        except FileNotFoundError:pass
                    with (ROOT/'work'/'workspace-setup.log').open('ab') as output:
                        WORKSPACE_SETUP=subprocess.Popen([sys.executable,'-u','-m','staff.workspace_tools','--authorize'],cwd=ROOT,stdout=output,stderr=output)
                    auth_file=ROOT/'work'/'workspace-auth-url.json'
                    for _ in range(30):
                        if auth_file.exists():break
                        time.sleep(.1)
                    try:url=json.loads(auth_file.read_text()).get('url','')
                    except (OSError,ValueError,TypeError):url=''
                    if not url:return self.reply(500,{'error':'Google sign-in could not be prepared. Check the local monitor log.'})
                    return self.reply(200,{'message':'Continue Google sign-in in the browser window that just opened.','url':url})
                elif self.path=='/api/workspace-check':
                    from staff.workspace_tools import Workspace,save_status
                    result=Workspace(ROOT).request('GET','https://www.googleapis.com/drive/v3/about',params={'fields':'user(emailAddress)'})
                    account=result.get('user',{}).get('emailAddress','')
                    save_status(ROOT,'connected',account,'Connection verified with Google Drive.')
                    return self.reply(200,{'message':'Google Workspace connection verified.'})
                elif self.path=='/api/cloudflare':
                    account=str(data.get('account','')).strip();token=str(data.get('token','')).strip()
                    if not re.fullmatch('[a-fA-F0-9]{32}',account) or (token and not re.fullmatch('[A-Za-z0-9_-]{20,256}',token)):raise ValueError()
                    set_key(str(ROOT/'.env'),'CLOUDFLARE_ACCOUNT_ID',account,quote_mode='never')
                    if token:set_key(str(ROOT/'.env'),'CLOUDFLARE_API_TOKEN',token,quote_mode='never')
                    set_key(str(ROOT/'.env'),'CLOUDFLARE_FREE_PLAN_CONFIRMED','true' if data.get('freePlan') is True else 'false',quote_mode='never')
                    (ROOT/'.env').chmod(0o600)
                    return self.reply(200,{'ok':True})
                elif self.path=='/api/cloudflare-check':
                    result=Actions(ROOT,'Jordan','cloudflare-check-'+secrets.token_hex(8)).execute('design',{'prompt':'Minimal black and gold Onyx and Ink connection test icon on a plain white background, no text','model':'schnell'})
                    if result.get('error'):raise RuntimeError(result['error'])
                    status={'state':'connected','checked':datetime.now(timezone.utc).isoformat(),'message':'Live image generation verified.'}
                    path=ROOT/'work'/'cloudflare-status.json';temp=path.with_suffix('.tmp');temp.write_text(json.dumps(status));temp.chmod(0o600);temp.replace(path)
                    return self.reply(200,{'message':'Cloudflare image generation verified. The test image is in Created files.','file':result.get('file_id')})
                elif self.path in ('/api/square','/api/square-check'):
                    token=str(data.get('token','')).strip();environment=str(data.get('environment','sandbox')).strip().lower()
                    if environment not in ('sandbox','production'):raise ValueError('Choose Square sandbox or production.')
                    if token and (len(token)<20 or len(token)>512 or any(c.isspace() for c in token)):raise ValueError('Enter a valid Square access token.')
                    if self.path=='/api/square-check':
                        from staff.square_commerce import SquareCommerce
                        return self.reply(200,SquareCommerce(ROOT).check())
                    if token:set_key(str(ROOT/'.env'),'SQUARE_ACCESS_TOKEN',token,quote_mode='never')
                    set_key(str(ROOT/'.env'),'SQUARE_ENVIRONMENT',environment,quote_mode='never')
                    (ROOT/'.env').chmod(0o600)
                    return self.reply(200,{'message':'Square '+environment+' connection saved locally. Test it before publishing products.'})
                elif self.path=='/api/tavily':
                    token=str(data.get('token','')).strip()
                    if token and not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',token):raise ValueError()
                    if token:set_key(str(ROOT/'.env'),'TAVILY_API_KEY',token,quote_mode='never')
                    (ROOT/'.env').chmod(0o600)
                    return self.reply(200,{'message':'Tavily key saved locally.'})
                elif self.path=='/api/tavily-check':
                    from staff.web_research import WebResearchError,verify
                    key=dotenv_values(ROOT/'.env').get('TAVILY_API_KEY')
                    try:verify(key)
                    except WebResearchError as error:return self.reply(400,{'error':str(error)})
                    status={'state':'connected','checked':datetime.now(timezone.utc).isoformat(),'message':'Live Tavily web search verified.'}
                    path=ROOT/'work'/'tavily-status.json';temp=path.with_suffix('.tmp');temp.write_text(json.dumps(status));temp.chmod(0o600);temp.replace(path)
                    return self.reply(200,{'message':'Tavily web search verified.'})
                elif self.path=='/api/signatures':
                    config=dotenv_values(ROOT/'.env');addresses={n:config.get(k,'') for n,_,k in STAFF};website=config.get('BUSINESS_WEBSITE','https://onyxandink.org')
                    values=reset_signature_settings(ROOT,addresses,website) if data.get('reset') is True else save_signature_settings(ROOT,data.get('signatures'))
                    if monitor_active():
                        stop_monitor();start_monitor();set_desired(True)
                    return self.reply(200,{'message':'Agent signatures restored.' if data.get('reset') is True else 'Agent signatures saved.','signatures':values})
                elif self.path=='/api/report-project':
                    name=str(data.get('name','')).strip()
                    known={item['id']:item['name'] for item in projects(ROOT)}
                    slug,label,_=ensure_project(ROOT,name,known.get(name,name),activate=True)
                    return self.reply(200,{'message':label+' selected. New agent reports will save to this project.','project':slug})
                elif self.path=='/api/project-overview':
                    result=save_project_overview(ROOT,str(data.get('project','')),str(data.get('body','')))
                    return self.reply(200,{'message':'Project overview saved. New dashboard and Telegram conversations receive it.','overview':result})
                elif self.path=='/api/create-report':
                    result=create_report(ROOT,str(data.get('project','')),str(data.get('agent','')),str(data.get('title','')),clean(str(data.get('body',''))))
                    return self.reply(200,{'message':'Report created in '+result['project_name']+'.','report':result})
                elif self.path=='/api/export-report':
                    result=export_google_doc(ROOT,str(data.get('id','')),force=True)
                    return self.reply(200,{'message':'Google Doc created or updated.','url':result['url']})
                elif self.path=='/api/delete-report':
                    if data.get('confirm') is not True:raise ValueError()
                    identifier=str(data.get('id',''))
                    try:current=json.loads((ROOT/'work'/'staff-work-status.json').read_text()).get('assignment','')
                    except (OSError,ValueError,TypeError):current=''
                    if AUTONOMY.status().get('running') and current and current in identifier:return self.reply(409,{'error':'This assignment is still active. Stop staff or wait for it to finish before deleting its report.'})
                    delete_report(ROOT,identifier);return self.reply(200,{'message':'Report deleted.'})
                elif self.path=='/api/delete-project':
                    if data.get('confirm') is not True:raise ValueError()
                    project=str(data.get('project',''))
                    if AUTONOMY.status().get('running') and project==next((item['id'] for item in projects(ROOT) if item['active']),None):return self.reply(409,{'error':'Stop autonomous staff before deleting the active project.'})
                    active=delete_project(ROOT,project);return self.reply(200,{'message':'Project deleted.','project':active})
                elif self.path=='/api/smtp-check':
                    import smtplib
                    config=dotenv_values(ROOT/'.env');mail=mail_from_config(config,mode='send')
                    context=ssl.create_default_context()
                    client=smtplib.SMTP_SSL(mail.host,mail.port,context=context,timeout=30) if mail.security=='ssl' else smtplib.SMTP(mail.host,mail.port,timeout=30)
                    with client:
                        if mail.security=='starttls':client.ehlo();client.starttls(context=context);client.ehlo()
                        if mail.oauth:mail.oauth.smtp_login(client)
                        else:client.login(*mail.credentials['Morgan'])
                    return self.reply(200,{'message':'SMTP authentication successful. No email sent. Approve a new draft to test delivery.'})
                elif self.path=='/api/telegram-agents':
                    tokens=data.get('tokens',{});enabled=data.get('enabled',{})
                    if not isinstance(tokens,dict) or not isinstance(enabled,dict) or set(tokens)-set(TELEGRAM_AGENTS) or set(enabled)-set(TELEGRAM_AGENTS):raise ValueError()
                    config=dotenv_values(ROOT/'.env');verified=[]
                    for name,bridge in TELEGRAM_AGENTS.items():
                        token=str(tokens.get(name,'')).strip();current=token or config.get(bridge.token_key,'')
                        if token and not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,256}',token):raise ValueError('Enter a valid '+name+' bot token.')
                        if enabled.get(name) and not current:return self.reply(400,{'error':'Enter and save the '+name+' bot token before enabling it.'})
                        if current and (token or enabled.get(name)):
                            try:bot=bridge.api(current,'getMe',{});bridge.api(current,'setMyCommands',{'commands':[{'command':'pair','description':'Connect this private chat to the dashboard'},{'command':'help','description':'Show connection instructions'}]})
                            except RuntimeError as error:return self.reply(400,{'error':name+': '+str(error)})
                            verified.append('@'+str(bot.get('username','')).lstrip('@'))
                        if token:set_key(str(ROOT/'.env'),bridge.token_key,token,quote_mode='never')
                        state=bridge.state();state['enabled']=bool(enabled.get(name));state.pop('lastError',None);bridge.save(state)
                    (ROOT/'.env').chmod(0o600)
                    return self.reply(200,{'message':'Dedicated agent bots saved'+(' and verified: '+', '.join(verified)+'.' if verified else '.')})
                elif self.path=='/api/telegram-agent-pair':
                    agent=str(data.get('agent','')).title();bridge=TELEGRAM_AGENTS.get(agent)
                    if not bridge:raise ValueError('Choose an agent bot.')
                    if not bridge.state().get('enabled'):return self.reply(409,{'error':'Save and enable the '+agent+' bot before creating a pairing code.'})
                    return self.reply(200,{'code':bridge.pairing(),'agent':agent})
                elif self.path=='/api/telegram-daily-brief':
                    chat=str(data.get('chat','')).strip();when=str(data.get('time','09:00')).strip()
                    if chat and not re.fullmatch(r'(?:-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31})',chat):raise ValueError('Enter a channel ID such as -1001234567890 or a public @channelusername.')
                    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',when):raise ValueError('Choose a valid daily time.')
                    if chat and not dotenv_values(ROOT/'.env').get('TELEGRAM_MORGAN_BOT_TOKEN'):return self.reply(409,{'error':'Save Morgan’s dedicated bot before configuring a daily brief.'})
                    set_key(str(ROOT/'.env'),'TELEGRAM_DAILY_BRIEF_CHAT',chat,quote_mode='never')
                    set_key(str(ROOT/'.env'),'TELEGRAM_DAILY_BRIEF_TIME',when,quote_mode='never')
                    (ROOT/'.env').chmod(0o600)
                    if data.get('test') is True and chat:
                        try:post_daily_brief(force=True)
                        except RuntimeError as error:return self.reply(400,{'error':str(error)+' Make Morgan an administrator of the channel and confirm the channel ID.'})
                        return self.reply(200,{'message':'Daily brief test sent through Morgan.'})
                    return self.reply(200,{'message':'Daily Morgan brief saved.' if chat else 'Daily Morgan brief disabled.'})
                elif self.path=='/api/discord-provision':
                    guild=str(data.get('guild','')).strip();token=str(data.get('token','')).strip()
                    layout=DISCORD.provision_workspace(guild,token or None)
                    set_key(str(ROOT/'.env'),'DISCORD_GUILD_ID',layout['guild'],quote_mode='never')
                    set_key(str(ROOT/'.env'),'DISCORD_STAFF_CHANNEL_ID',layout['staff'],quote_mode='never')
                    set_key(str(ROOT/'.env'),'DISCORD_AGENT_REQUESTS_CHANNEL_ID',layout['requests'],quote_mode='never')
                    set_key(str(ROOT/'.env'),'DISCORD_DAILY_BRIEF_CHANNEL_ID',layout['briefs'],quote_mode='never')
                    for name,channel in layout['agents'].items():set_key(str(ROOT/'.env'),'DISCORD_'+name.upper()+'_CHANNEL_ID',channel,quote_mode='never')
                    (ROOT/'.env').chmod(0o600)
                    return self.reply(200,{'message':'Discord staff workspace created: staff floor, requests, daily briefs, projects, and four role channels.','layout':layout})
                elif self.path=='/api/discord':
                    token=str(data.get('token','')).strip();guild=str(data.get('guild','')).strip();staff_channel=str(data.get('staffChannel','')).strip();requests_channel=str(data.get('requestsChannel','')).strip();brief_channel=str(data.get('dailyBriefChannel','')).strip();channels=data.get('agentChannels',{});agent_tokens=data.get('agentTokens',{})
                    if not isinstance(channels,dict) or not isinstance(agent_tokens,dict) or set(channels)-{name for name,_,_ in STAFF} or set(agent_tokens)-{name for name,_,_ in STAFF}:raise ValueError('Invalid agent channel settings.')
                    identifiers=[value for value in [guild,staff_channel,requests_channel,brief_channel,*channels.values()] if value]
                    if any(not re.fullmatch(r'\d{16,22}',str(value)) for value in identifiers):raise ValueError('Discord server and channel IDs contain 16–22 digits.')
                    if data.get('test') is True:
                        identity=DISCORD.check(token or None);return self.reply(200,{'message':'Connected to Discord as '+identity['name']+'.'})
                    if token:set_key(str(ROOT/'.env'),'DISCORD_BOT_TOKEN',token,quote_mode='never')
                    set_key(str(ROOT/'.env'),'DISCORD_ENABLED','true' if data.get('enabled') is True else 'false',quote_mode='never')
                    for key,value in [('DISCORD_GUILD_ID',guild),('DISCORD_STAFF_CHANNEL_ID',staff_channel),('DISCORD_AGENT_REQUESTS_CHANNEL_ID',requests_channel),('DISCORD_DAILY_BRIEF_CHANNEL_ID',brief_channel)]:set_key(str(ROOT/'.env'),key,value,quote_mode='never')
                    for name,_,_ in STAFF:
                        if str(agent_tokens.get(name,'')).strip():set_key(str(ROOT/'.env'),'DISCORD_'+name.upper()+'_BOT_TOKEN',str(agent_tokens[name]).strip(),quote_mode='never')
                        set_key(str(ROOT/'.env'),'DISCORD_'+name.upper()+'_CHANNEL_ID',str(channels.get(name,'')).strip(),quote_mode='never')
                    (ROOT/'.env').chmod(0o600)
                    state=DISCORD.state();state.update({'enabled':data.get('enabled') is True,'lastError':''});DISCORD.save(state)
                    return self.reply(200,{'message':'Discord staff workspace saved. Enable Message Content Intent in Discord Developer Portal, then invite the bot to this server.'})
                elif self.path=='/api/telegram-agent-group':
                    chat=str(data.get('chat','')).strip()
                    if chat and not re.fullmatch(r'-?\d{5,20}',chat):raise ValueError('Enter the private Telegram group ID, for example -1001234567890.')
                    set_key(str(ROOT/'.env'),'TELEGRAM_AGENT_GROUP_CHAT',chat,quote_mode='never')
                    (ROOT/'.env').chmod(0o600)
                    return self.reply(200,{'message':'Staff Telegram group saved.' if chat else 'Staff Telegram group disabled.'})
                elif self.path=='/api/telegram-pair':
                    state=TELEGRAM.state()
                    if not state.get('enabled'):return self.reply(409,{'error':'Verify and enable Telegram before creating a pairing code.'})
                    return self.reply(200,{'code':TELEGRAM.pairing()})
                elif self.path=='/api/mode':
                    if data.get('mode') not in ('off','draft','send'):raise ValueError()
                    restart=monitor_active() or desired()
                    if monitor_active():stop_monitor()
                    set_key(str(ROOT/'.env'),'STAFF_EMAIL_MODE',data['mode'],quote_mode='never')
                    if restart and data['mode']!='off':start_monitor();set_desired(True)
                    elif data['mode']=='off':set_desired(False)
                    return self.reply(200,{'message':'Email mode saved. The inbox monitor was restarted with the new setting.' if restart and data['mode']!='off' else 'Email mode saved.'})
                elif self.path=='/api/autonomy':
                    try:state,message=configure_autonomy(data)
                    except ValueError as error:return self.reply(400,{'error':str(error)})
                    return self.reply(200,{'message':message})
                elif self.path=='/api/start':
                    start_monitor();set_desired(True)
                elif self.path=='/api/stop':
                    stop_monitor()
                elif self.path=='/api/inbox':
                    return self.reply(200,MAILBOX.page(refresh=bool(data.get('refresh')),mailbox=data.get('mailbox','all'),page_token=data.get('pageToken'),view=data.get('view','visible')))
                elif self.path=='/api/inbox-settings':
                    InboxState(ROOT).configure(data.get('days'));return self.reply(200,{'ok':True})
                elif self.path=='/api/hide-email':
                    identifier=str(data.get('id',''));MAILBOX.read(identifier)
                    InboxState(ROOT).hide(identifier,hide=data.get('hidden',True) is True)
                    return self.reply(200,{'message':'Dashboard visibility updated. Gmail is unchanged.'})
                elif self.path=='/api/delete-email':
                    if data.get('confirm') is not True:raise ValueError()
                    return self.reply(200,MAILBOX.trash(str(data.get('id',''))))
                elif self.path=='/api/upload':
                    return self.reply(200,InboxState(ROOT).upload(data.get('name',''),data.get('data','')))
                elif self.path=='/api/delivery':
                    return self.reply(200,check_delivery(ROOT,MAILBOX))
                elif self.path=='/api/message':
                    return self.reply(200,MAILBOX.read(str(data.get('id',''))))
                elif self.path in ('/api/reply','/api/compose'):
                    identifier=str(data.get('id',''));body=str(data.get('body','')).strip();sender=data.get('sender','Owner')
                    request_id=str(data.get('requestId',''))
                    attachments=InboxState(ROOT).attachment_paths(data.get('attachments',[]))
                    if not body or len(body)>20000 or not re.fullmatch('[a-f0-9-]{36}',request_id) or sender not in ('Owner','Shared',*(n for n,_,_ in STAFF)):raise ValueError()
                    composing=self.path=='/api/compose'
                    if composing:
                        from staff.staff_email import valid_address
                        to=str(data.get('to','')).strip();subject=str(data.get('subject','')).strip()
                        if not valid_address(to) or not subject or '\r' in subject or '\n' in subject:raise ValueError()
                        original={'replyTo':to,'subject':subject,'messageId':None,'references':None}
                    else:original=MAILBOX.read(identifier)
                    config=dotenv_values(ROOT/'.env')
                    if config.get('STAFF_EMAIL_MODE')=='off':return self.reply(409,{'error':'Email is off. Select Draft or Live.'})
                    # Durable reservation prevents duplicate sends after browser retries or crashes.
                    db=sqlite3.connect(ROOT/'work'/'dashboard-replies.sqlite3')
                    try:
                        db.execute('CREATE TABLE IF NOT EXISTS replies (id TEXT PRIMARY KEY, status TEXT)')
                        previous=db.execute('SELECT status FROM replies WHERE id=?',(request_id,)).fetchone()
                        if previous:return self.reply(200,{'result':previous[0]})
                        db.execute('INSERT INTO replies VALUES (?,?)',(request_id,'Submission reserved; delivery may be unconfirmed. Do not retry automatically.'));db.commit()
                        mail=mail_from_config(config)
                        subject=original['subject'].replace('\r',' ').replace('\n',' ')[:190]
                        if not composing and not subject.lower().startswith('re:'):subject='Re: '+subject
                        internal_addresses={'Owner':config.get('OWNER_EMAIL',''),'Jaunee':config.get('IMPORTANT_CC_EMAIL',''),
                                            **{name:config.get(key,'') for name,_,key in STAFF}}
                        internal=next((name for name,address in internal_addresses.items() if address and address.lower()==original['replyTo'].lower()),None)
                        result=mail.deliver(sender,[internal or 'Owner'],subject,body,kind='compose' if composing else 'manual',
                            reply_address=None if internal else original['replyTo'],important=internal=='Owner',
                            in_reply_to=original['messageId'],references=original['references'] or None,attachments=attachments)
                        db.execute('UPDATE replies SET status=? WHERE id=?',(result,request_id));db.commit()
                        return self.reply(200,{'result':result})
                    finally:db.close()
                elif self.path=='/api/approve':
                    path=outbox_file(str(data.get('id','')))
                    current=path.with_suffix('.status').read_text().strip() if path.with_suffix('.status').exists() else 'draft'
                    if current!='draft':return self.reply(409,{'error':'This message is not waiting for approval. Unconfirmed mail is never resent automatically.'})
                    config=dotenv_values(ROOT/'.env')
                    if config.get('STAFF_EMAIL_MODE')=='off':return self.reply(409,{'error':'Email is off. Select Draft or Live.'})
                    result=mail_from_config(config, mode='send').send_saved(path)
                    return self.reply(200,{'result':result})
                elif self.path=='/api/dismiss':
                    ids=data.get('ids')
                    if not isinstance(ids,list) or not ids or len(ids)>60:raise ValueError()
                    for identifier in ids:
                        path=outbox_file(str(identifier))
                        current=path.with_suffix('.status').read_text().strip() if path.with_suffix('.status').exists() else 'draft'
                        if current in ('draft','delivery-unconfirmed','partially-accepted'):
                            path.with_suffix('.status').write_text('dismissed\n')
                    return self.reply(200,{'ok':True})
                else:return self.reply(404,{'error':'Not found.'})
            self.reply(200,{'ok':True})
        except (ValueError,json.JSONDecodeError) as error:
            self.reply(400,{'error':str(error) if str(error) else 'Invalid request or missing mail configuration.'})
        except Exception as error:
            # Keep credentials and submitted values out of both the browser and logs.
            # The exception class is enough to diagnose server-side configuration faults.
            try:
                path=ROOT/'work'/'dashboard-errors.log'
                with path.open('a',encoding='utf-8') as log:log.write(datetime.now(timezone.utc).isoformat()+' '+self.path+' '+type(error).__name__+'\n')
                path.chmod(0o600)
            except OSError:pass
            self.reply(500,{'error':'Could not save this setting ('+type(error).__name__+'). Check dashboard-errors.log on this Windows PC.'})

if __name__=='__main__':
    if not DESIRED.exists():set_desired(monitor_active())
    threading.Thread(target=supervise,daemon=True).start()
    print(f'Onyx and Ink dashboard: http://{APP_HOST}:{APP_PORT}')
    threading.Thread(target=TELEGRAM.run,daemon=True).start()
    threading.Thread(target=DISCORD.run,daemon=True).start()
    for bridge in TELEGRAM_AGENTS.values():threading.Thread(target=bridge.run,daemon=True).start()
    tls=None if CLOUD_MODE else ACCESS.context();lan=None
    if tls:
        lan=ThreadingHTTPServer(('0.0.0.0',8766),Handler);lan.socket=tls.wrap_socket(lan.socket,server_side=True)
        threading.Thread(target=lan.serve_forever,daemon=True).start()
    server=ThreadingHTTPServer((APP_HOST,APP_PORT),Handler)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        STOP.set()
        if MONITOR and MONITOR.poll() is None:
            MONITOR.send_signal(signal.SIGINT)
            try:MONITOR.wait(timeout=10)
            except subprocess.TimeoutExpired:MONITOR.terminate()
        if AUTONOMY.process and AUTONOMY.process.poll() is None:AUTONOMY.process.send_signal(signal.SIGINT)
        if lan:lan.shutdown();lan.server_close()
        server.server_close()







