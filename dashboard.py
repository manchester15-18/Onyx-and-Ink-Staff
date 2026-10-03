"""Local staff dashboard. Bound to loopback; never exposes credentials."""
import json
from contextlib import closing
import os
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
from dashboard_mail import Mailbox
from staff_email import StaffMail
from delivery_tracking import check_delivery
from inbox_state import InboxState

os.environ['CREWAI_TELEMETRY_DISABLED']='true'
os.environ['CREWAI_TRACING_ENABLED']='false'
os.environ['OTEL_SDK_DISABLED']='true'
ROOT = Path(__file__).resolve().parent
STAFF = [('Morgan','Chief Operating Officer','MORGAN_EMAIL'),('Avery','Marketing Lead','AVERY_EMAIL'),('Jordan','IT & Storefront Development Lead','JORDAN_EMAIL'),('Cameron','Legal & HR Lead','CAMERON_EMAIL')]
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
MONITOR = None
WORKSPACE_SETUP = None
MAILBOX = Mailbox(ROOT)
STOP = threading.Event()
DESIRED = ROOT/'work'/'monitor-enabled.json'
from dashboard_access import Access
from agent_chat import AgentChat, TelegramBridge
from agent_actions import Actions
ACCESS = Access(ROOT)
CHAT = AgentChat(ROOT)
TELEGRAM = TelegramBridge(ROOT,CHAT,STOP)


def provider_status(name):
    try:return json.loads((ROOT/'work'/(name+'-status.json')).read_text())
    except (OSError,ValueError):return {}


def workspace_status():
    if WORKSPACE_SETUP and WORKSPACE_SETUP.poll() is None:
        return {'state':'authorizing','message':'Google sign-in is open on this Mac.'}
    token=ROOT/'work'/'google-workspace-token.json';saved=provider_status('workspace')
    if not token.exists():
        if saved.get('state')=='failed' and 'outside the configured Workspace domain' in saved.get('message',''):
            return {'state':'failed','message':'Reconnect Google Workspace once to apply the updated account verification.'}
        return saved if saved.get('state')=='failed' else {'state':'not_connected','message':'Google Workspace sign-in needed.'}
    try:
        from google.oauth2.credentials import Credentials
        credentials=Credentials.from_authorized_user_file(str(token))
        if not credentials.refresh_token:raise ValueError()
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


def stop_monitor():
    global MONITOR
    set_desired(False)
    pid=MONITOR.pid if MONITOR and MONITOR.poll() is None else monitor_pid()
    if pid:os.kill(pid,signal.SIGINT)
    deadline=time.monotonic()+10
    while monitor_active() and time.monotonic()<deadline:time.sleep(.1)
    if monitor_active():raise RuntimeError('Monitor is still stopping. Wait and refresh.')


def clean(text):
    for key,value in dotenv_values(ROOT/'.env').items():
        if value and any(word in key.upper() for word in ('KEY','PASSWORD','SECRET','TOKEN')):
            text=text.replace(value,'[REDACTED]')
    import re
    return re.sub(r'(?:gsk_|AIza)[A-Za-z0-9_-]+','[REDACTED]',text)


def snapshot():
    config=dotenv_values(ROOT/'.env')
    activity=[]
    files=sorted((ROOT/'work'/'email-outbox').glob('*/*.eml'),key=lambda p:p.stat().st_mtime,reverse=True)[:60]
    for path in files:
        try:
            msg=BytesParser(policy=policy.default).parsebytes(path.read_bytes())
            status=path.with_suffix('.status')
            activity.append({'id':str(path.relative_to(ROOT)), 'time':datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),
                'sender':str(msg.get('From','')), 'to':str(msg.get('To','')), 'subject':clean(str(msg.get('Subject',''))),
                'receipt':json.loads(path.with_suffix('.receipt.json').read_text()) if path.with_suffix('.receipt.json').exists() else None,
                'kind':str(msg.get('X-Onyx-Ink-Kind','message')), 'status':status.read_text().strip() if status.exists() else 'draft',
                'diagnostic':path.with_suffix('.diagnostic').read_text().strip() if path.with_suffix('.diagnostic').exists() else '',
                'body':clean((msg.get_body(preferencelist=('plain',)).get_content() if msg.get_body(preferencelist=('plain',)) else '')[:30000])})
        except (OSError,ValueError): continue
    processed=[]
    db=ROOT/'work'/'inbox-monitor.sqlite3'
    if db.exists():
        try:
            with closing(sqlite3.connect(f'file:{db}?mode=ro',uri=True)) as connection:
                processed=[{'uid':r[0],'status':r[1]} for r in connection.execute('SELECT uid,state FROM processed ORDER BY rowid DESC LIMIT 10')]
        except sqlite3.Error: pass
    reports=[]
    for path in sorted((ROOT/'reports').glob('*.md')):
        owner={'marketing_campaign':'Avery','web_dev_specs':'Jordan','legal_terms':'Cameron','operational_plan':'Morgan'}.get(path.stem,'Unassigned')
        reports.append({'name':path.stem.replace('_',' ').title(),'agent':owner,'body':clean(path.read_text()[:50000])})
    tools=Actions(ROOT,'Morgan','dashboard-status')
    artifacts=tools.files()
    for item in artifacts:
        if item['kind']=='report':
            reports.append({'name':item['name'],'agent':item['agent'],'body':clean(tools.file(item['file_id']).read_text()[:50000])})
    return {'cleanupDays':InboxState(ROOT).days(),'artifacts':artifacts,'actions':tools.recent(),'integrations':{'workspace':workspace_status(),'cloudflare':bool(config.get('CLOUDFLARE_API_TOKEN') and config.get('CLOUDFLARE_ACCOUNT_ID')),'freePlan':config.get('CLOUDFLARE_FREE_PLAN_CONFIRMED')=='true','cloudflareTest':provider_status('cloudflare')},'mode':config.get('STAFF_EMAIL_MODE','off'),'monitor':monitor_active(),'managed':bool(monitor_active()),'enabled':desired(),
        'authorized':(ROOT/'work'/'google-mail-token.json').exists(),'agents':[{'name':n,'role':r,'email':config.get(k,'')} for n,r,k in STAFF],'businessWebsite':config.get('BUSINESS_WEBSITE','https://onyxandink.org'),
        'wifi':{'enabled':bool(ACCESS.settings().get('enabled')),'url':next(('https://'+host+':8766' for host in ACCESS.settings().get('hosts',[]) if host not in ('localhost','127.0.0.1')),'')},'telegram':{'configured':bool(config.get('TELEGRAM_BOT_TOKEN')),'enabled':TELEGRAM.state().get('enabled',False),'paired':len(TELEGRAM.state().get('users',{}))},'activity':activity,'processed':processed,'reports':reports,'token':TOKEN,'mailboxes':[{'name':'all','label':'All inboxes','email':''},{'name':'Shared','label':'Shared inbox','email':config.get('GOOGLE_MAIL_USER','')},{'name':'Owner','label':'CEO','email':config.get('OWNER_EMAIL','')},*[{'name':n,'label':n+' · '+r,'email':config.get(k,'')} for n,r,k in STAFF]]}


def outbox_file(identifier):
    path=(ROOT/str(identifier)).resolve()
    root=(ROOT/'work'/'email-outbox').resolve()
    if not path.is_relative_to(root) or path.suffix!='.eml' or not path.is_file():
        raise ValueError()
    return path


def mail_from_config(config, mode=None):
    return StaffMail.from_env(ROOT, mode=mode, config=config)


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
        if not self.allowed():return self.reply(403,{'error':'Local access only.'})
        if path=='/app.js' or path=='/style.css':pass
        elif path=='/ca.crt':
            return self.reply(200,(ROOT/'work'/'certificates'/'onyx-dashboard-ca.crt').read_bytes(),'application/x-x509-ca-cert')
        elif not ACCESS.authenticated(self):
            if path.startswith('/api/'):return self.reply(401,{'error':'Sign in to the dashboard.'})
            return self.reply(200,(ROOT/'dashboard'/'login.html').read_bytes(),'text/html; charset=utf-8')
        if path=='/api/status':return self.reply(200,snapshot())
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
                file=tools.file(identifier)
                kind={'.png':'image/png','.md':'text/plain; charset=utf-8'}.get(file.suffix,'application/octet-stream')
                return self.reply(200,file.read_bytes(),kind)
            except (ValueError,OSError):return self.reply(404,{'error':'Generated file not found.'})
        names={'/':'index.html','/overview':'index.html','/inbox':'index.html','/activity':'index.html','/reports':'index.html','/settings':'index.html','/chat':'index.html','/files':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if path not in names:return self.reply(404,{'error':'Not found.'})
        name=names[path];kind={'html':'text/html; charset=utf-8','js':'text/javascript','css':'text/css'}[name.split('.')[-1]]
        self.reply(200,(ROOT/'dashboard'/name).read_bytes(),kind)
    def do_POST(self):
        global MONITOR, WORKSPACE_SETUP
        origin=self.headers.get('Origin','')
        allowed_origin=origin in {'http://127.0.0.1:8765','http://localhost:8765',*('https://'+host for host in ACCESS.hosts() if host.endswith(':8766'))}
        if self.path=='/api/login' and self.allowed() and allowed_origin:
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length<0 or length>2048:raise ValueError()
                data=json.loads(self.rfile.read(length));session=ACCESS.login(str(data.get('password','')),self.client_address[0])
                if not session:return self.reply(401,{'error':'Incorrect password or too many attempts. Try again later.'})
                self.send_response(200);self.send_header('Set-Cookie','onyx_session='+session+'; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=43200');self.send_header('Content-Type','application/json');self.send_header('Content-Length','11');self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(b'{"ok":true}');return
            except Exception:return self.reply(400,{'error':'Invalid login.'})
        if not self.allowed() or not allowed_origin or self.headers.get('X-Dashboard-Token')!=TOKEN or not ACCESS.authenticated(self):
            return self.reply(403,{'error':'Local dashboard authorization required.'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            maximum=14_100_000 if self.path=='/api/upload' else 40000
            if length>maximum or length<0:raise ValueError()
            data=json.loads(self.rfile.read(length))
            if self.path=='/api/chat-history':return self.reply(200,{'messages':CHAT.history(data.get('agent','Morgan'))})
            if self.path=='/api/chat':
                return self.reply(200,{'answer':CHAT.ask(data.get('agent','Morgan'),str(data.get('body','')),str(data.get('requestId','')))})
            if self.path=='/api/draft-reply':
                return self.reply(200,{'answer':CHAT.draft_reply(data.get('agent','Morgan'),str(data.get('body','')))})
            with LOCK:
                if self.path=='/api/wifi-password':
                    if self.client_address[0] not in ('127.0.0.1','::1'):return self.reply(403,{'error':'View the Wi-Fi password on the host Mac.'})
                    return self.reply(200,{'password':(ROOT/'work'/'wifi-password.txt').read_text().strip()})
                elif self.path=='/api/telegram':
                    token=str(data.get('token','')).strip()
                    if token:
                        import re
                        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+',token):raise ValueError()
                        set_key(str(ROOT/'.env'),'TELEGRAM_BOT_TOKEN',token,quote_mode='never')
                    bridge=TELEGRAM.state();bridge['enabled']=bool(data.get('enabled'));TELEGRAM.save(bridge)
                    return self.reply(200,{'ok':True})
                elif self.path=='/api/workspace-authorize':
                    if self.client_address[0] not in ('127.0.0.1','::1'):return self.reply(403,{'error':'Connect Google Workspace on the host Mac.'})
                    if WORKSPACE_SETUP and WORKSPACE_SETUP.poll() is None:return self.reply(200,{'message':'Google sign-in is already open on this Mac.'})
                    if not (ROOT/'google-oauth-client.json').exists():return self.reply(409,{'error':'The Google Desktop client file is missing.'})
                    with (ROOT/'work'/'workspace-setup.log').open('ab') as output:
                        WORKSPACE_SETUP=subprocess.Popen([sys.executable,'-u',str(ROOT/'workspace_tools.py'),'--authorize'],cwd=ROOT,stdout=output,stderr=output)
                    return self.reply(200,{'message':'Google sign-in is opening on this Mac. Select the shared Workspace account and review the requested access.'})
                elif self.path=='/api/workspace-check':
                    from workspace_tools import Workspace,save_status
                    result=Workspace(ROOT).request('GET','https://www.googleapis.com/drive/v3/about',params={'fields':'user(emailAddress)'})
                    account=result.get('user',{}).get('emailAddress','')
                    save_status(ROOT,'connected',account,'Connection verified with Google Drive.')
                    return self.reply(200,{'message':'Google Workspace connection verified.'})
                elif self.path=='/api/cloudflare':
                    if self.client_address[0] not in ('127.0.0.1','::1'):return self.reply(403,{'error':'Configure provider credentials on the host Mac.'})
                    import re
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
                elif self.path=='/api/telegram-pair':return self.reply(200,{'code':TELEGRAM.pairing()})
                elif self.path=='/api/mode':
                    if data.get('mode') not in ('off','draft','send'):raise ValueError()
                    if monitor_active():return self.reply(409,{'error':'Stop the monitor before changing email mode. A running monitor retains its old settings.'})
                    set_key(str(ROOT/'.env'),'STAFF_EMAIL_MODE',data['mode'],quote_mode='never')
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
                    import re
                    if not body or len(body)>20000 or not re.fullmatch('[a-f0-9-]{36}',request_id) or sender not in ('Owner','Shared',*(n for n,_,_ in STAFF)):raise ValueError()
                    composing=self.path=='/api/compose'
                    if composing:
                        from staff_email import valid_address
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
                        result=mail.deliver(sender,['Owner'],subject,body,kind='compose' if composing else 'manual',reply_address=original['replyTo'],
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
        except (ValueError,json.JSONDecodeError):self.reply(400,{'error':'Invalid request or missing mail configuration.'})
        except Exception:self.reply(500,{'error':'Action failed. Check the local monitor log.'})

if __name__=='__main__':
    if not DESIRED.exists():set_desired(monitor_active())
    threading.Thread(target=supervise,daemon=True).start()
    print('Onyx and Ink dashboard: http://127.0.0.1:8765')
    threading.Thread(target=TELEGRAM.run,daemon=True).start()
    tls=ACCESS.context();lan=None
    if tls:
        lan=ThreadingHTTPServer(('0.0.0.0',8766),Handler);lan.socket=tls.wrap_socket(lan.socket,server_side=True)
        threading.Thread(target=lan.serve_forever,daemon=True).start()
    server=ThreadingHTTPServer(('127.0.0.1',8765),Handler)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        STOP.set()
        if MONITOR and MONITOR.poll() is None:
            MONITOR.send_signal(signal.SIGINT)
            try:MONITOR.wait(timeout=10)
            except subprocess.TimeoutExpired:MONITOR.terminate()
        if lan:lan.shutdown();lan.server_close()
        server.server_close()
