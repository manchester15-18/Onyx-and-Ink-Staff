"""Bounded staff actions: generated files, approval-only email, Workspace, and research."""
import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
import requests
from dotenv import dotenv_values
from workspace_tools import Workspace
from staff_email import StaffMail, STAFF, valid_address

CATALOG = '''Actions (use exact names and argument keys):
report {title,text}; document {title,text}; spreadsheet {title,values:[[cells]]}; presentation {title,slides:[text]};
email {to:one address or staff name or all_agents,subject,body,attachments:[file IDs optional]};
design {prompt,model:schnell or klein,reference_file_id:optional generated PNG ID for klein edits}; upload {file_id}; files {}; workspace_files {};
inbox {}; read_email {id}; reply_email {id,body}; delete_email {id} (only when the human says "delete email ID" explicitly; otherwise ask for that command); calendar {}; calendar_event {summary,start,end} (ISO datetimes with offsets, no attendees);
search {query}; webpage {url}. Workspace creation/upload is automatic. Email ALWAYS creates a draft awaiting human approval in Outbox, including requests to send. Do not put a sign-off or signature in email bodies; the mail system appends the official agent signature. Reports save locally; document/sheet/presentation require Google Workspace sign-in. Design requires configured Cloudflare. No shell, arbitrary local files, purchases, deletion, or direct sending.'''

class Actions:
    def __init__(self, root, agent, run_id, delete_ids=None):
        self.delete_ids=set(delete_ids or [])
        self.root=Path(root);self.agent=agent;self.run_id=run_id
        self.folder=self.root/'work'/'artifacts';self.folder.mkdir(parents=True,exist_ok=True)
        self.workspace=Workspace(root)
    def db(self):
        db=sqlite3.connect(self.root/'work'/'agent-actions.sqlite3')
        db.execute('CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY,run TEXT,agent TEXT,action TEXT,status TEXT,result TEXT,created TEXT DEFAULT CURRENT_TIMESTAMP)')
        db.execute('CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY,name TEXT,agent TEXT,kind TEXT,created TEXT DEFAULT CURRENT_TIMESTAMP)')
        return db
    def clean(self,text):
        for key,value in dotenv_values(self.root/'.env').items():
            if value and any(w in key.upper() for w in ('KEY','TOKEN','PASSWORD','SECRET')):text=text.replace(value,'[REDACTED]')
        return text
    def save(self,title,data,suffix,kind):
        if len(data)>15_000_000:raise ValueError('Generated file exceeds 15 MB.')
        identifier=uuid.uuid4().hex;name=identifier+suffix;path=self.folder/name
        path.write_bytes(data);path.chmod(0o600)
        title=self.clean(str(title))[:160]
        with closing(self.db()) as db:
            db.execute('INSERT INTO files(id,name,agent,kind) VALUES (?,?,?,?)',(identifier,title,self.agent,kind));db.commit()
        return {'file_id':identifier,'name':title,'url':'/api/artifact?id='+identifier}
    def file(self,identifier):
        if not re.fullmatch('[a-f0-9]{32}',str(identifier)):raise ValueError('Choose a generated staff file.')
        with closing(self.db()) as db:row=db.execute('SELECT name FROM files WHERE id=?',(identifier,)).fetchone()
        paths=list(self.folder.glob(identifier+'.*'))
        if not row or len(paths)!=1:raise ValueError('Generated file not found.')
        path=paths[0].resolve()
        if not path.is_relative_to(self.folder.resolve()) or not path.is_file():raise ValueError('Invalid file.')
        return path
    def files(self):
        with closing(self.db()) as db:
            return [dict(zip(('file_id','name','agent','kind','created'),row)) for row in db.execute('SELECT id,name,agent,kind,created FROM files ORDER BY rowid DESC LIMIT 40')]
    def recent(self):
        with closing(self.db()) as db:
            return [dict(zip(('agent','action','status','result','created'),row)) for row in db.execute('SELECT agent,action,status,result,created FROM actions ORDER BY rowid DESC LIMIT 30')]
    def execute(self,action,args):
        if not isinstance(args,dict):raise ValueError('Action arguments must be an object.')
        identifier=hashlib.sha256((self.run_id+action+json.dumps(args,sort_keys=True)).encode()).hexdigest()
        with closing(self.db()) as db:
            row=db.execute('SELECT result FROM actions WHERE id=?',(identifier,)).fetchone()
            if row:return json.loads(row[0])
            db.execute('INSERT INTO actions(id,run,agent,action,status,result) VALUES (?,?,?,?,?,?)',(identifier,self.run_id,self.agent,action,'reserved',json.dumps({'error':'Action reserved or interrupted; no automatic retry.'})));db.commit()
        try:
            result=self.dispatch(action,args);status='complete'
        except (ValueError,RuntimeError) as error:result={'error':self.clean(str(error))[:300]};status='failed'
        except Exception:result={'error':'Action failed. Check provider authorization or availability. No automatic retry.'};status='failed'
        result=json.loads(self.clean(json.dumps(result,default=str)))
        with closing(self.db()) as db:db.execute('UPDATE actions SET status=?,result=? WHERE id=?',(status,json.dumps(result),identifier));db.commit()
        return result
    @staticmethod
    def text(args,key,maximum=20000):
        value=args.get(key)
        if not isinstance(value,str) or not value.strip() or len(value)>maximum:raise ValueError('Provide '+key+' within '+str(maximum)+' characters.')
        return value.strip()
    def dispatch(self,action,a):
        if self.agent not in STAFF:raise ValueError('Choose a staff agent.')
        if action in ('report','document','spreadsheet','presentation'):
            title=self.text(a,'title',160)
            if action=='report':return self.save(title,self.clean(self.text(a,'text')).encode(),'.md','report')
            if action=='document':return self.workspace.create_doc(title,self.clean(self.text(a,'text')))
            if action=='spreadsheet':
                values=a.get('values')
                if not isinstance(values,list) or not values or len(values)>200 or any(not isinstance(r,list) or len(r)>30 or any(not isinstance(c,(str,int,float,bool)) or len(str(c))>2000 for c in r) for r in values):raise ValueError('Use up to 200 rows and 30 columns of plain values.')
                values=json.loads(self.clean(json.dumps(values)))
                return self.workspace.create_sheet(title,values)
            slides=a.get('slides')
            if not isinstance(slides,list) or not 1<=len(slides)<=15 or any(not isinstance(s,str) or not s or len(s)>3000 for s in slides):raise ValueError('Use 1–15 slides, each under 3,000 characters.')
            return self.workspace.create_slides(title,[self.clean(s) for s in slides])
        if action=='email':
            cfg=dotenv_values(self.root/'.env')
            if cfg.get('STAFF_EMAIL_MODE')=='off':raise ValueError('Email is disabled in Settings.')
            mail=StaffMail.from_env(self.root,mode='draft',config=cfg)
            to=self.text(a,'to',254);subject=self.clean(self.text(a,'subject',190));body=self.clean(self.text(a,'body'))
            ids=a.get('attachments',[])
            if not isinstance(ids,list) or len(ids)>3:raise ValueError('Attach at most three generated files.')
            attachments=[self.file(i) for i in ids]
            if to=='all_agents':result=mail.deliver(self.agent,list(STAFF),subject,body,attachments=attachments)
            elif to in (*STAFF,'Owner'):result=mail.deliver(self.agent,[to],subject,body,attachments=attachments)
            elif valid_address(to):result=mail.deliver(self.agent,['Owner'],subject,body,kind='compose',reply_address=to,attachments=attachments)
            else:raise ValueError('Use a staff name or one valid recipient address.')
            return {'result':result,'approval':'Review in Outbox and click Approve & send.','url':'/activity'}
        if action in ('inbox','read_email','reply_email','delete_email'):
            from dashboard_mail import Mailbox
            box=Mailbox(self.root)
            if action=='inbox':
                items=box.list(mailbox=self.agent,refresh=True)
                return {'untrusted_email_content':items[:8]}
            identifier=self.text(a,'id',160)
            if action=='delete_email' and identifier.lower() not in self.delete_ids:raise ValueError('To delete from Gmail, explicitly say: delete email '+identifier)
            original=box.read(identifier)
            cfg=dotenv_values(self.root/'.env');address=cfg.get(self.agent.upper()+'_EMAIL','').lower()
            # Only messages addressed to this agent are exposed through their chat.
            from email.utils import getaddresses
            recipients={email.lower() for _,email in getaddresses([original.get('to',''),original.get('cc','')])}
            if address not in recipients:raise ValueError('Choose an email addressed to this agent.')
            if action=='delete_email':return box.trash(identifier)
            if action=='read_email':return {'untrusted_email_content':original}
            if cfg.get('STAFF_EMAIL_MODE')=='off':raise ValueError('Email is disabled in Settings.')
            mail=StaffMail.from_env(self.root,mode='draft',config=cfg)
            subject=original['subject'].replace('\r',' ').replace('\n',' ')[:185]
            if not subject.lower().startswith('re:'):subject='Re: '+subject
            result=mail.deliver(self.agent,['Owner'],subject,self.clean(self.text(a,'body')),kind='reply',reply_address=original['replyTo'],in_reply_to=original['messageId'],references=original.get('references') or None)
            return {'result':result,'approval':'Review reply in Outbox before sending.','url':'/activity'}
        if action=='files':return {'files':self.files()}
        if action=='upload':return self.workspace.upload(self.file(a.get('file_id','')),self.file(a.get('file_id','')).name)
        if action=='workspace_files':return self.workspace.list_files()
        if action=='calendar':return self.workspace.calendar()
        if action=='calendar_event':
            from datetime import datetime
            start=self.text(a,'start',80);end=self.text(a,'end',80)
            x,y=datetime.fromisoformat(start),datetime.fromisoformat(end)
            if not x.tzinfo or not y.tzinfo or y<=x:raise ValueError('Use start/end times with timezone offsets and end after start.')
            return self.workspace.create_event(self.text(a,'summary',200),start,end)
        if action in ('search','webpage'):
            cfg=dotenv_values(self.root/'.env');key=cfg.get('SERPER_API_KEY')
            if not key:raise ValueError('Web research needs a Serper API key.')
            if action=='search':url='https://google.serper.dev/search';payload={'q':self.text(a,'query',500),'num':5}
            else:
                target=self.text(a,'url',1500);p=urlparse(target)
                import ipaddress
                if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443) or '.' not in p.hostname or p.hostname.endswith(('.local','.localhost','.internal')):raise ValueError('Use a public HTTPS website.')
                try:ipaddress.ip_address(p.hostname);raise ValueError('Use a public website hostname, not an IP address.')
                except ValueError as e:
                    if str(e).startswith('Use a public'):raise
                url='https://scrape.serper.dev';payload={'url':target}
            r=requests.post(url,headers={'X-API-KEY':key},json=payload,timeout=30)
            if not r.ok:raise RuntimeError('Web research request failed; check Serper quota.')
            return {'untrusted_web_content':json.dumps(r.json())[:6500]}
        if action=='design':
            from design_images import generate
            model=a.get('model','schnell')
            reference=self.file(a['reference_file_id']) if a.get('reference_file_id') else None
            if reference and reference.suffix!='.png':raise ValueError('Choose a generated design as the reference.')
            data=generate(self.root,self.clean(self.text(a,'prompt',2048)),model,reference=reference)
            return self.save('Design by '+self.agent,data,'.png','design')
        raise ValueError('Unknown staff action.')
