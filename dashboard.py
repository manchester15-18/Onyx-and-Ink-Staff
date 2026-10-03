"""Local staff dashboard. Bound to loopback; never exposes credentials."""
import json
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

os.environ['CREWAI_TELEMETRY_DISABLED']='true'
os.environ['CREWAI_TRACING_ENABLED']='false'
os.environ['OTEL_SDK_DISABLED']='true'
ROOT = Path(__file__).resolve().parent
STAFF = [('Morgan','COO','MORGAN_EMAIL'),('Avery','Marketing','AVERY_EMAIL'),('Jordan','IT','JORDAN_EMAIL'),('Cameron','Legal & HR','CAMERON_EMAIL')]
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
MONITOR = None
MAILBOX = Mailbox(ROOT)
STOP = threading.Event()
DESIRED = ROOT/'work'/'monitor-enabled.json'
from dashboard_access import Access
from agent_chat import AgentChat, TelegramBridge
ACCESS = Access(ROOT)
CHAT = AgentChat(ROOT)
TELEGRAM = TelegramBridge(ROOT,CHAT,STOP)


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
            with sqlite3.connect(f'file:{db}?mode=ro',uri=True) as connection:
                processed=[{'uid':r[0],'status':r[1]} for r in connection.execute('SELECT uid,state FROM processed ORDER BY rowid DESC LIMIT 10')]
        except sqlite3.Error: pass
    reports=[]
    for path in sorted((ROOT/'reports').glob('*.md')):
        owner={'marketing_campaign':'Avery','web_dev_specs':'Jordan','legal_terms':'Cameron','operational_plan':'Morgan'}.get(path.stem,'Unassigned')
        reports.append({'name':path.stem.replace('_',' ').title(),'agent':owner,'body':clean(path.read_text()[:50000])})
    return {'mode':config.get('STAFF_EMAIL_MODE','off'),'monitor':monitor_active(),'managed':bool(monitor_active()),'enabled':desired(),
        'authorized':(ROOT/'work'/'google-mail-token.json').exists(),'agents':[{'name':n,'role':r,'email':config.get(k,'')} for n,r,k in STAFF],
        'wifi':{'enabled':bool(ACCESS.settings().get('enabled')),'url':next(('https://'+host+':8766' for host in ACCESS.settings().get('hosts',[]) if host not in ('localhost','127.0.0.1')),'')},'telegram':{'configured':bool(config.get('TELEGRAM_BOT_TOKEN')),'enabled':TELEGRAM.state().get('enabled',False),'paired':len(TELEGRAM.state().get('users',{}))},'activity':activity,'processed':processed,'reports':reports,'token':TOKEN,'mailboxes':[{'name':'all','label':'All inboxes','email':''},{'name':'Shared','label':'Shared inbox','email':config.get('GOOGLE_MAIL_USER','')},{'name':'Owner','label':'CEO','email':config.get('OWNER_EMAIL','')},*[{'name':n,'label':n+' · '+r,'email':config.get(k,'')} for n,r,k in STAFF]]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def allowed(self):
        return self.headers.get('Host') in ACCESS.hosts()
    def reply(self,code,data,kind='application/json'):
        body=json.dumps(data).encode() if kind=='application/json' else data
        self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(body)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(body)
    def do_GET(self):
        if not self.allowed():return self.reply(403,{'error':'Local access only.'})
        if self.path=='/app.js' or self.path=='/style.css':pass
        elif self.path=='/ca.crt':
            return self.reply(200,(ROOT/'work'/'certificates'/'onyx-dashboard-ca.crt').read_bytes(),'application/x-x509-ca-cert')
        elif not ACCESS.authenticated(self):
            if self.path.startswith('/api/'):return self.reply(401,{'error':'Sign in to the dashboard.'})
            return self.reply(200,(ROOT/'dashboard'/'login.html').read_bytes(),'text/html; charset=utf-8')
        if self.path=='/api/status':return self.reply(200,snapshot())
        names={'/':'index.html','/overview':'index.html','/inbox':'index.html','/activity':'index.html','/reports':'index.html','/settings':'index.html','/chat':'index.html','/app.js':'app.js','/style.css':'style.css'}
        if self.path not in names:return self.reply(404,{'error':'Not found.'})
        name=names[self.path];kind={'html':'text/html; charset=utf-8','js':'text/javascript','css':'text/css'}[name.split('.')[-1]]
        self.reply(200,(ROOT/'dashboard'/name).read_bytes(),kind)
    def do_POST(self):
        global MONITOR
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
            if length>40000 or length<0:raise ValueError()
            data=json.loads(self.rfile.read(length))
            if self.path=='/api/chat-history':return self.reply(200,{'messages':CHAT.history(data.get('agent','Morgan'))})
            if self.path=='/api/chat':
                return self.reply(200,{'answer':CHAT.ask(data.get('agent','Morgan'),str(data.get('body','')),str(data.get('requestId','')))})
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
                    return self.reply(200,{'messages':MAILBOX.list(refresh=bool(data.get('refresh')),mailbox=data.get('mailbox','all'))})
                elif self.path=='/api/delivery':
                    return self.reply(200,check_delivery(ROOT,MAILBOX))
                elif self.path=='/api/message':
                    return self.reply(200,MAILBOX.read(str(data.get('id',''))))
                elif self.path in ('/api/reply','/api/compose'):
                    identifier=str(data.get('id',''));body=str(data.get('body','')).strip();sender=data.get('sender','Owner')
                    request_id=str(data.get('requestId',''))
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
                        env=dict(os.environ)
                        try:
                            for key,value in config.items():
                                if value is not None:os.environ[key]=value
                            mail=StaffMail.from_env(ROOT)
                        finally:
                            for key in config:
                                if key in env:os.environ[key]=env[key]
                                else:os.environ.pop(key,None)
                        subject=original['subject'].replace('\r',' ').replace('\n',' ')[:190]
                        if not composing and not subject.lower().startswith('re:'):subject='Re: '+subject
                        result=mail.deliver(sender,['Owner'],subject,body,kind='compose' if composing else 'manual',reply_address=original['replyTo'],
                            in_reply_to=original['messageId'],references=original['references'] or None)
                        db.execute('UPDATE replies SET status=? WHERE id=?',(result,request_id));db.commit()
                        return self.reply(200,{'result':result})
                    finally:db.close()
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
