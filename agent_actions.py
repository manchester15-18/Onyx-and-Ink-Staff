"""Bounded staff actions: files, policy-routed email, Workspace, and research."""
import csv
import base64
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
import requests
from dotenv import dotenv_values
from workspace_tools import Workspace
from staff_email import StaffMail, STAFF, valid_address
from report_projects import active_project, ensure_project, project_slug, project_context, project_overview, save_project_overview, consolidated_project_report
from report_format import standardize_staff_report
from storefront import Storefront
from staff_tasks import enqueue as enqueue_staff_task

CATALOG = '''Actions (use exact names and argument keys):
report {project:project name optional,title,text}; document {title,text}; word_document {title,text}; pdf {title,text}; spreadsheet {title,values:[[cells]]}; presentation {title,slides:[text]}; print_sheet {title,file_ids:[generated PNG/JPEG IDs]}; drive_search {query}; drive_download {id,name};
email {to:one address or staff name or all_agents,subject,body,attachments:[file IDs optional]};
design {prompt,model:schnell or klein,reference_file_id:optional generated PNG ID for klein edits}; upload {file_id}; share_drive {file_id}; files {}; project_context {}; staff_reports {name:morgan,avery,jordan,cameron,or all}; project_create {name,overview,reason,activate:false}; project_brief_update {project:optional current project,overview,reason}; staff_message {to:Morgan,Avery,Jordan,or Cameron,text,attachments:[generated file IDs optional]}; staff_group_post {text,attachments:[generated file IDs optional]}; workspace_files {};
products {query:optional}; product_update {id:optional existing/new product ID,title,description,price,image_file_id:optional uploaded/generated image,status:draft,ready,or published}; square_import_website {url:optional public store URL};
github_site_files {query:optional}; github_site_read {path}; github_preview_patch {path,find,replace,summary}; github_preview_write {path,content,summary};
github_main_files {query:optional}; github_main_read {path}; github_main_patch_request {path,find,replace,summary};
inbox {}; read_email {id}; reply_email {id,body}; delete_email {id} (only when the human says "delete email ID" explicitly; otherwise ask for that command); calendar {}; calendar_event {summary,start,end} (ISO datetimes with offsets, no attendees);
search {query}; webpage {url}. Workspace creation/upload is automatic. Use staff_message for ordinary agent-to-agent coordination in the configured Discord workspace or Telegram staff group. Email to James (CEO), Jaunee (Vice President), named Onyx & Ink agents, or an address explicitly listed in AUTONOMOUS_EXTERNAL_EMAILS sends automatically; any other outside address creates a draft awaiting human approval in Outbox. Address James as CEO and Jaunee as Vice President in executive correspondence. Email James only for a blocker, urgent risk, decision, or specific human input needed to finish an assignment. Keep CEO email under 120 words with one clear request; routine progress and completed reports stay in the dashboard. Do not put a sign-off or signature in email bodies; the mail system appends the official agent signature. Reports save into the named project folder; omit project to use the project selected on the Reports page. Document/sheet/presentation require Google Workspace sign-in. Design requires configured Cloudflare. No shell, arbitrary local files, purchases, deletion, or unapproved outside sending.'''

class Actions:
    def __init__(self, root, agent, run_id, delete_ids=None):
        self.delete_ids=set(delete_ids or [])
        self.root=Path(root);self.agent=agent;self.run_id=run_id
        self.folder=self.root/'work'/'artifacts';self.folder.mkdir(parents=True,exist_ok=True)
        self.workspace=Workspace(root)
        self.storefront=Storefront(root)
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
    def save_report(self,title,text,project=None):
        if project:
            slug,label,folder=ensure_project(self.root,project,str(project),activate=False)
        else:
            slug,label,folder=active_project(self.root)
        assignment=folder/'agent-drafts'/self.agent.lower();assignment.mkdir(parents=True,exist_ok=True)
        cleaned=self.clean(text)
        report_text=standardize_staff_report(title,cleaned,self.agent)
        path=assignment/(project_slug(title)+'.md');existed=path.exists()
        temporary=path.with_suffix('.tmp');temporary.write_text(report_text+('' if report_text.endswith('\n') else '\n'));temporary.chmod(0o600);temporary.replace(path)
        result=self.save(title,cleaned.encode(),'.md','project-report')
        result.update({'project':slug,'project_name':label,'dashboard':'/reports','updated':existed})
        return result

    def save_word_document(self,title,text):
        safe=self.clean(text)
        escaped=lambda value:__import__('html').escape(value,quote=False)
        paragraphs=''.join('<w:p><w:r><w:t xml:space="preserve">'+escaped(line or ' ')+'</w:t></w:r></w:p>' for line in safe.splitlines())
        document='<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+paragraphs+'<w:sectPr/></w:body></w:document>'
        content=io.BytesIO()
        with zipfile.ZipFile(content,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('[Content_Types].xml','<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            archive.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
            archive.writestr('word/document.xml',document)
        return self.save(title,content.getvalue(),'.docx','word-document')

    def save_pdf(self,title,text):
        import fitz
        doc=fitz.open();remaining=self.clean(text)
        while remaining:
            cut=min(3200,len(remaining));split=remaining.rfind('\n',0,cut)
            if split<1:split=cut
            page=doc.new_page();page.insert_textbox(fitz.Rect(54,54,541,788),remaining[:split],fontsize=11,fontname='helv',lineheight=1.35)
            remaining=remaining[split:].lstrip()
        data=doc.tobytes(garbage=4,deflate=True);doc.close()
        return self.save(title,data,'.pdf','pdf')

    def print_sheet(self,title,identifiers):
        if not isinstance(identifiers,list) or not 1<=len(identifiers)<=12:raise ValueError('Choose 1–12 generated PNG or JPEG files.')
        files=[self.file(identifier) for identifier in identifiers]
        if any(path.suffix.lower() not in ('.png','.jpg','.jpeg') for path in files):raise ValueError('A print sheet needs PNG or JPEG design files.')
        import fitz
        from PIL import Image,ImageOps,ImageDraw
        page_width,page_height=2550,3300; margin=120; columns=2; rows=(len(files)+1)//2
        canvas=Image.new('RGB',(page_width,page_height),'white');draw=ImageDraw.Draw(canvas)
        cell_width=(page_width-margin*(columns+1))//columns;cell_height=(page_height-margin*(rows+1))//rows
        for index,path in enumerate(files):
            with Image.open(path) as image:
                image=ImageOps.exif_transpose(image).convert('RGB');image.thumbnail((cell_width,cell_height))
                column=index%columns;row=index//columns;x=margin+column*(cell_width+margin)+(cell_width-image.width)//2;y=margin+row*(cell_height+margin)+(cell_height-image.height)//2
                canvas.paste(image,(x,y))
        preview=io.BytesIO();canvas.save(preview,format='PNG',dpi=(300,300))
        document=fitz.open();page=document.new_page(width=612,height=792);page.insert_image(page.rect,stream=preview.getvalue());pdf=document.tobytes(garbage=4,deflate=True);document.close()
        pdf_result=self.save(title,pdf,'.pdf','print-sheet-pdf');png_result=self.save(title,preview.getvalue(),'.png','print-sheet-preview')
        return {'pdf':pdf_result,'png':png_result,'result':'Print-sheet PDF and PNG preview created from verified staff files.'}

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
    def site_root(self):
        root=(self.root/'cloudflare-storefront').resolve()
        if not (root/'.git').exists():raise RuntimeError('The approved storefront repository is not available on this Windows PC.')
        return root
    @staticmethod
    def git(site,*args):
        environment=os.environ.copy();token=environment.get('GITHUB_TOKEN','').strip()
        if token:
            basic=base64.b64encode(('x-access-token:'+token).encode()).decode()
            environment.update({'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'http.https://github.com/.extraheader','GIT_CONFIG_VALUE_0':'AUTHORIZATION: basic '+basic})
        result=subprocess.run(['git','-C',str(site),*args],capture_output=True,text=True,timeout=45,env=environment)
        if result.returncode:raise RuntimeError((result.stderr or result.stdout or 'GitHub operation failed.').strip()[:300])
        return result.stdout.strip()
    def site_path(self,raw,allow_missing=False):
        if not isinstance(raw,str) or not raw.strip() or len(raw)>220:raise ValueError('Provide a storefront file path.')
        relative=Path(raw.strip().replace('\\','/'))
        if relative.is_absolute() or '..' in relative.parts or any(part.startswith('.') for part in relative.parts):raise ValueError('Use a normal storefront file path, not a hidden or parent path.')
        if relative.suffix.lower() not in ('.html','.css','.js','.json','.md','.txt','.svg'):raise ValueError('Jordan may edit only storefront text, style, script, JSON, SVG, or Markdown files.')
        site=self.site_root();path=(site/relative).resolve()
        if not path.is_relative_to(site) or (not allow_missing and not path.is_file()):raise ValueError('That storefront file was not found.')
        return site,path,relative.as_posix()
    @staticmethod
    def site_text_safe(text):
        banned=(r'(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*["\']?[^\s"\']{12,}',r'(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',r'(?i)\b(?:gh[pousr]_|github_pat_|sk_|csk-|AIza)[A-Za-z0-9_-]{16,}')
        if any(re.search(pattern,text) for pattern in banned):raise ValueError('Site changes cannot contain credentials or private keys.')
    def github_site_files(self,query=''):
        site=self.site_root();needle=str(query or '').strip().lower()
        files=[name for name in self.git(site,'ls-files').splitlines() if name and not name.startswith(('.github/','.env'))]
        if needle:files=[name for name in files if needle in name.lower()]
        return {'branch':self.git(site,'branch','--show-current'),'files':files[:160],'count':len(files),'policy':'Preview is editable; main requires owner approval.'}
    def github_site_read(self,path):
        site,target,relative=self.site_path(path)
        if target.stat().st_size>120_000:raise ValueError('That file is too large for chat. Ask for a more specific file.')
        text=target.read_text(encoding='utf-8')
        self.site_text_safe(text)
        return {'branch':self.git(site,'branch','--show-current'),'path':relative,'content':text[:60000]}
    def github_main_files(self,query=''):
        site=self.site_root()
        try:self.git(site,'fetch','origin','main')
        except RuntimeError:pass
        needle=str(query or '').strip().lower()
        files=[name for name in self.git(site,'ls-tree','-r','--name-only','origin/main').splitlines() if name and not name.startswith(('.github/','.env'))]
        if needle:files=[name for name in files if needle in name.lower()]
        return {'branch':'main','files':files[:160],'count':len(files),'policy':'Read-only production view. Main changes must be proposed through preview and approved by the owner.'}
    def github_main_read(self,path):
        site,_,relative=self.site_path(path,allow_missing=True)
        try:self.git(site,'fetch','origin','main')
        except RuntimeError:pass
        text=self.git(site,'show','origin/main:'+relative)
        if len(text)>60000:raise ValueError('That file is too large for chat. Ask for a more specific file.')
        self.site_text_safe(text)
        return {'branch':'main','path':relative,'content':text,'policy':'Read-only production view.'}
    def github_preview_change(self,path,content,summary,expect=None):
        if self.agent!='Jordan':raise ValueError('Only Jordan may manage the approved storefront repository.')
        if not isinstance(content,str) or not content.strip() or len(content)>300_000:raise ValueError('Provide storefront content under 300,000 characters.')
        if not isinstance(summary,str) or not summary.strip() or len(summary)>120:raise ValueError('Provide a concise change summary.')
        self.site_text_safe(content)
        site,target,relative=self.site_path(path,allow_missing=expect is None)
        branch=self.git(site,'branch','--show-current')
        if branch!='preview':raise RuntimeError('Jordan can edit only the preview branch. Main is protected for owner approval.')
        self.git(site,'fetch','origin','preview')
        ahead=self.git(site,'merge-base','--is-ancestor','origin/preview','HEAD')
        if ahead is None:raise RuntimeError('GitHub preview has changes that need review before editing.')
        if target.exists():
            current=target.read_text(encoding='utf-8')
            if expect is not None:
                if not isinstance(expect,str) or not expect or current.count(expect)!=1:raise ValueError('The requested preview replacement must match exactly one existing passage.')
                content=current.replace(expect,content,1)
            self.site_text_safe(current)
        elif expect is not None:raise ValueError('Use github_preview_write only for a new storefront file.')
        target.parent.mkdir(parents=True,exist_ok=True);temporary=target.with_suffix(target.suffix+'.tmp');temporary.write_text(content,encoding='utf-8');temporary.replace(target)
        try:
            self.git(site,'diff','--check')
            if target.suffix=='.js':
                check=subprocess.run(['node','--check',str(target)],capture_output=True,text=True,timeout=30)
                if check.returncode:raise RuntimeError((check.stderr or 'JavaScript syntax check failed.').strip()[:300])
            self.git(site,'add','--',relative)
            if not self.git(site,'diff','--cached','--name-only').strip():raise ValueError('That change does not alter the storefront file.')
            self.git(site,'commit','-m','Jordan preview: '+summary.strip())
            self.git(site,'push','origin','preview')
        except Exception:
            try:self.git(site,'restore','--staged','--worktree','--',relative)
            except Exception:pass
            raise
        return {'result':'Preview change committed and pushed for Cloudflare Preview review.','branch':'preview','path':relative,'summary':summary.strip(),'review_url':'https://preview.onyx-and-ink-site.pages.dev/','production':'Main remains protected and requires owner-approved pull request.'}
    def github_main_patch_request(self,path,find,replace,summary):
        if self.agent!='Jordan':raise ValueError('Only Jordan may prepare approved storefront changes.')
        site,target,relative=self.site_path(path,allow_missing=True)
        try:self.git(site,'fetch','origin','main')
        except RuntimeError:pass
        main=self.git(site,'show','origin/main:'+relative)
        preview=target.read_text(encoding='utf-8') if target.exists() else ''
        if preview!=main:raise RuntimeError('Preview differs from main for this file. Review or merge the existing Preview work before proposing another production change.')
        if not isinstance(find,str) or not find or main.count(find)!=1:raise ValueError('The requested main replacement must match exactly one production passage.')
        proposed=main.replace(find,replace,1)
        result=self.github_preview_change(relative,proposed,'Main approval request: '+summary)
        result.update({'approval_required':True,'production':'A pull request from preview to main must be reviewed and explicitly approved by the owner before merge.'})
        return result
    def execute(self,action,args):
        if not isinstance(args,dict):raise ValueError('Action arguments must be an object.')
        identifier=hashlib.sha256((self.run_id+self.agent+action+json.dumps(args,sort_keys=True)).encode()).hexdigest()
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
        # Cloud models occasionally use the integration's product name instead
        # of the concise action name.  Keep documented aliases deterministic.
        action=str(action).strip().lower().replace('-','_').replace(' ','_')
        aliases={
            'google_drive':'workspace_files','drive_files':'workspace_files','list_drive_files':'workspace_files',
            'google_drive_upload':'upload','drive_upload':'upload','upload_to_drive':'upload',
            'google_doc':'document','google_docs':'document','google_sheet':'spreadsheet','google_sheets':'spreadsheet',
            'google_slides':'presentation','create_pdf':'pdf','create_docx':'word_document','docx':'word_document',
        }
        action=aliases.get(action,action)
        if action in ('report','document','word_document','pdf','spreadsheet','presentation'):
            title=self.text(a,'title',160)
            if action=='report':return self.save_report(title,self.text(a,'text'),a.get('project'))
            if action=='document':return self.workspace.create_doc(title,self.clean(self.text(a,'text')))
            if action=='word_document':return self.save_word_document(title,self.text(a,'text'))
            if action=='pdf':return self.save_pdf(title,self.text(a,'text'))
            if action=='spreadsheet':
                values=a.get('values')
                if not isinstance(values,list) or not values or len(values)>200 or any(not isinstance(r,list) or len(r)>30 or any(not isinstance(c,(str,int,float,bool)) or len(str(c))>2000 for c in r) for r in values):raise ValueError('Use up to 200 rows and 30 columns of plain values.')
                values=json.loads(self.clean(json.dumps(values)))
                return self.workspace.create_sheet(title,values)
            slides=a.get('slides')
            # Title and section-divider slides are intentionally brief.  Require
            # a real deck with at least two substantive slides without rejecting
            # those legitimate short slides.
            if not isinstance(slides,list) or not 3<=len(slides)<=15 or any(not isinstance(s,str) or not s.strip() or len(s)>3000 for s in slides) or sum(len(s.strip())>=40 for s in slides)<2:raise ValueError('Use 3–15 slides with at least two substantive slides (40+ characters); title slides may be shorter.')
            return self.workspace.create_slides(title,[self.clean(s) for s in slides])
        if action=='print_sheet':return self.print_sheet(self.text(a,'title',160),a.get('file_ids'))
        if action=='drive_search':return {'files':self.workspace.find_files(self.text(a,'query',160)).get('files',[])}
        if action=='drive_download':
            identifier=self.text(a,'id',200);name=self.text(a,'name',180)
            if Path(name).suffix.lower() not in ('.png','.jpg','.jpeg'):raise ValueError('Choose a PNG or JPEG Drive design file.')
            data,_=self.workspace.download(identifier)
            from PIL import Image
            with Image.open(io.BytesIO(data)) as image:image.verify()
            return self.save(name,data,Path(name).suffix.lower(),'drive-design')
        if action=='email':
            cfg=dotenv_values(self.root/'.env')
            if cfg.get('STAFF_EMAIL_MODE')=='off':raise ValueError('Email is disabled in Settings.')
            to=self.text(a,'to',254);subject=self.clean(self.text(a,'subject',190));body=self.clean(self.text(a,'body'))
            ids=a.get('attachments',[])
            if not isinstance(ids,list) or len(ids)>3:raise ValueError('Attach at most three generated files.')
            attachments=[self.file(i) for i in ids]
            internal=to=='all_agents' or to in (*STAFF,'Owner','James','Jaunee')
            approved_external={item.strip().lower() for item in str(cfg.get('AUTONOMOUS_EXTERNAL_EMAILS','')).split(',') if valid_address(item.strip())}
            autonomous_external=valid_address(to) and to.lower() in approved_external
            mail=StaffMail.from_env(self.root,config=cfg) if internal or autonomous_external else StaffMail.from_env(self.root,mode='draft',config=cfg)
            if to=='all_agents':result=mail.deliver(self.agent,list(STAFF),subject,body,attachments=attachments)
            elif to in (*STAFF,'Owner','James','Jaunee'):result=mail.deliver(self.agent,[to],subject,body,attachments=attachments,important=to in ('Owner','James'))
            elif valid_address(to):result=mail.deliver(self.agent,['Owner'],subject,body,kind='compose',reply_address=to,attachments=attachments)
            else:raise ValueError('Use a staff name or one valid recipient address.')
            if str(result).startswith('Email delivery unconfirmed'):
                raise RuntimeError('Email delivery was unconfirmed by the SMTP server. The message is preserved in Outbox and was not reported as sent.')
            return {'result':result,'approval':'Sent automatically to an approved staff or external recipient.' if internal or autonomous_external else 'Review in Outbox and click Approve & send.','url':'/activity'}
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
        if action in ('staff_group_post','staff_message'):
            message=self.clean(self.text(a,'text',4000));recipient=''
            if action=='staff_message':
                recipient=self.text(a,'to',30).title()
                if recipient not in STAFF:raise ValueError('Choose Morgan, Avery, Jordan, or Cameron.')
            ids=a.get('attachments',[])
            if not isinstance(ids,list) or len(ids)>3:raise ValueError('Attach at most three generated files.')
            files=[self.file(identifier) for identifier in ids]
            cfg=dotenv_values(self.root/'.env')
            # Discord is the primary staff workspace when enabled. It provides
            # restricted role channels, threads, and normal file delivery.
            if str(cfg.get('DISCORD_ENABLED','')).lower()=='true' and cfg.get('DISCORD_BOT_TOKEN'):
                from discord_bridge import DiscordBridge
                result=DiscordBridge(self.root,None,None).send(self.agent,message,files,recipient)
                if recipient:
                    task=enqueue_staff_task(self.root,self.agent,recipient,message,active_project(self.root)[0])
                    result['task_id']=task['id']
                return result
            chat=str(cfg.get('TELEGRAM_AGENT_GROUP_CHAT','')).strip()
            token=cfg.get('TELEGRAM_'+self.agent.upper()+'_BOT_TOKEN','')
            if not re.fullmatch(r'-?\d{5,20}',chat) or not token:raise ValueError('Configure Discord, or configure the staff Telegram group and this agent’s dedicated bot in Settings.')
            prefix=('[[ONYX_STAFF target='+recipient+' hops=1]] ') if recipient else ''
            response=requests.post('https://api.telegram.org/bot'+token+'/sendMessage',json={'chat_id':chat,'text':prefix+message},timeout=30)
            if not response.ok:raise RuntimeError('Telegram staff group delivery failed.')
            for path in files:
                with path.open('rb') as stream:
                    sent=requests.post('https://api.telegram.org/bot'+token+'/sendDocument',data={'chat_id':chat,'caption':prefix+path.name[:150]},files={'document':(path.name,stream)},timeout=45)
                if not sent.ok:raise RuntimeError('Telegram delivered the message but could not deliver one attachment.')
            result={'result':('Telegram message sent to '+recipient if recipient else 'Telegram staff-group message posted')+(' with '+str(len(files))+' file(s).' if files else '.'),'channel':chat}
            if recipient:
                task=enqueue_staff_task(self.root,self.agent,recipient,message,active_project(self.root)[0])
                result['task_id']=task['id']
            return result
        if action=='square_import_website':
            if self.agent!='Jordan':raise ValueError('Only Jordan manages Square catalog imports.')
            from square_commerce import SquareCommerce
            from website_catalog import collect,download_image
            square=SquareCommerce(self.root)
            if not square.configured:raise ValueError('Connect Square in Settings before importing products.')
            url=str(a.get('url') or dotenv_values(self.root/'.env').get('BUSINESS_WEBSITE','')).strip()
            parsed=urlparse(url)
            if parsed.scheme!='https' or not parsed.hostname:raise ValueError('Use the public HTTPS storefront URL.')
            source=collect(url)
            if not source:raise RuntimeError('No public product cards were found on the current storefront.')
            imported=[];failed=[]
            for item in source:
                image_path=None
                try:
                    image_path=download_image(item.pop('image_url',''))
                    product=self.storefront.update({**item,'status':'published'})
                    result=square.publish_product(product,image_path)
                    self.storefront.set_square_item(product['id'],result['square_item_id'])
                    imported.append(product['title'])
                except Exception as error:failed.append(item.get('title','product'))
                finally:
                    if image_path:
                        try:image_path.unlink()
                        except OSError:pass
            return {'result':'Imported '+str(len(imported))+' of '+str(len(source))+' public storefront products into Square '+square.mode+'.','imported':imported,'failed':failed,'environment':square.mode}
        if action=='github_site_files':
            if self.agent!='Jordan':raise ValueError('Only Jordan may access the approved storefront repository.')
            return self.github_site_files(str(a.get('query','')))
        if action=='github_site_read':
            if self.agent!='Jordan':raise ValueError('Only Jordan may access the approved storefront repository.')
            return self.github_site_read(self.text(a,'path',220))
        if action=='github_preview_patch':
            return self.github_preview_change(self.text(a,'path',220),self.text(a,'replace',300000),self.text(a,'summary',120),self.text(a,'find',120000))
        if action=='github_preview_write':
            return self.github_preview_change(self.text(a,'path',220),self.text(a,'content',300000),self.text(a,'summary',120))
        if action=='github_main_files':
            if self.agent!='Jordan':raise ValueError('Only Jordan may access the approved storefront repository.')
            return self.github_main_files(str(a.get('query','')))
        if action=='github_main_read':
            if self.agent!='Jordan':raise ValueError('Only Jordan may access the approved storefront repository.')
            return self.github_main_read(self.text(a,'path',220))
        if action=='github_main_patch_request':
            return self.github_main_patch_request(self.text(a,'path',220),self.text(a,'find',120000),self.text(a,'replace',300000),self.text(a,'summary',100))
        if action=='products':
            if self.agent!='Jordan':raise ValueError('Only Jordan manages the storefront catalog.')
            return {'products':self.storefront.list(str(a.get('query','')))}
        if action=='product_update':
            if self.agent!='Jordan':raise ValueError('Only Jordan manages the storefront catalog.')
            image=None;image_id=a.get('image_file_id')
            if image_id:
                path=self.file(image_id)
                if path.suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):raise ValueError('Choose a PNG, JPEG, or WebP product image.')
                image=(str(image_id),path.name)
            product=self.storefront.update(a,image=image)
            if product['status']=='published':
                from square_commerce import SquareCommerce
                square=SquareCommerce(self.root)
                if not square.configured:
                    product['status']='ready'
                    self.storefront.update({**product,'status':'ready'},image=image)
                    product['result']='Saved as ready for Square. Connect Square in Settings before publishing.'
                else:
                    image_path=self.file(product['image_file_id']) if product.get('image_file_id') else None
                    result=square.publish_product(product,image_path)
                    self.storefront.set_square_item(product['id'],result['square_item_id'])
                    product.update(result);product['status']='published';product['result']='Published to Square '+result['environment']+' catalog.'
            else:product['result']='Product saved in the local catalog.'
            return product
        if action=='project_context':return {'dashboard':'/reports','project_context':project_context(self.root)}
        if action in ('project_create','project_brief_update'):
            if self.agent!='Morgan':raise ValueError('Only Morgan manages project creation and project briefings.')
            reason=self.text(a,'reason',800)
            if len(reason)<20:raise ValueError('Update a project only when needed; provide a specific reason of at least 20 characters.')
            body=self.text(a,'overview',20000)
            required=('## Background','## Goals','## Desired outcomes')
            if not all(section.lower() in body.lower() for section in required):
                raise ValueError('A project briefing needs Background, Goals, and Desired outcomes sections.')
            if action=='project_create':
                name=self.text(a,'name',80);slug=project_slug(name)
                if any(item['id']==slug for item in __import__('report_projects').projects(self.root)):
                    raise ValueError('That project already exists. Use project_brief_update only when the briefing has materially changed.')
                slug,label,_=ensure_project(self.root,name,name,activate=a.get('activate') is True)
            else:
                slug=str(a.get('project') or active_project(self.root)[0]).strip()
                current=project_overview(self.root,slug)['body']
                compact=lambda value:re.sub(r'\s+',' ',value).strip().casefold()
                if compact(current)==compact(body):raise ValueError('The briefing is unchanged; no update was saved.')
                # Preserve the prior briefing for recovery and review before replacing it.
                _,_,folder=ensure_project(self.root,slug)
                history=folder/'overview-history';history.mkdir(exist_ok=True)
                stamp=__import__('datetime').datetime.now().strftime('%Y%m%d-%H%M%S')
                previous=history/(stamp+'-overview.md');previous.write_text(current.rstrip()+'\n',encoding='utf-8');previous.chmod(0o600)
            result=save_project_overview(self.root,slug,body)
            report=consolidated_project_report(self.root,slug)
            result.update({'message':'Project created.' if action=='project_create' else 'Project briefing updated because the recorded project context changed.','reason':self.clean(reason),'consolidated_report':report['id'],'dashboard':'/reports'})
            return result
        if action=='staff_reports':
            name=str(a.get('name','all')).strip().lower()
            choices={'morgan':'morgan-operations.md','avery':'avery-marketing.md','jordan':'jordan-web-it.md','cameron':'cameron-hr-legal.md',
                     'operations':'morgan-operations.md','marketing':'avery-marketing.md','storefront':'jordan-web-it.md','policy':'cameron-hr-legal.md'}
            if name!='all' and name not in choices:raise ValueError('Choose morgan, avery, jordan, cameron, or all.')
            selected=choices.items() if name=='all' else [(name,choices[name])]
            reports={}
            for label,filename in selected:
                _,_,folder=active_project(self.root)
                matches=sorted((folder/'agent-reports').glob(filename),key=lambda path:path.stat().st_mtime,reverse=True)
                if not matches:
                    legacy={'avery-marketing.md':'marketing_campaign.md','jordan-web-it.md':'web_dev_specs.md','cameron-hr-legal.md':'legal_terms.md','morgan-operations.md':'operational_plan.md'}[filename]
                    matches=sorted(folder.glob('**/'+legacy),key=lambda path:path.stat().st_mtime,reverse=True)
                try:reports[label]=matches[0].read_text()[:5000]
                except IndexError:reports[label]='No saved report yet.'
            return {'dashboard':'/reports','reports':reports}
        if action in ('upload','share_drive'):
            file=self.file(a.get('file_id',''));return self.workspace.upload(file,file.name)
        if action=='workspace_files':return self.workspace.list_files()
        if action=='calendar':return self.workspace.calendar()
        if action=='calendar_event':
            from datetime import datetime
            start=self.text(a,'start',80);end=self.text(a,'end',80)
            x,y=datetime.fromisoformat(start),datetime.fromisoformat(end)
            if not x.tzinfo or not y.tzinfo or y<=x:raise ValueError('Use start/end times with timezone offsets and end after start.')
            return self.workspace.create_event(self.text(a,'summary',200),start,end)
        if action in ('search','webpage'):
            cfg=dotenv_values(self.root/'.env');key=cfg.get('TAVILY_API_KEY')
            if not key:raise ValueError('Web research needs a Tavily API key in Settings.')
            if action=='search':
                from web_research import search
                return {'untrusted_web_content':search(self.root,self.text(a,'query',500),max_results=5)}
            else:
                target=self.text(a,'url',1500);p=urlparse(target)
                import ipaddress
                if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443) or '.' not in p.hostname or p.hostname.endswith(('.local','.localhost','.internal')):raise ValueError('Use a public HTTPS website.')
                try:ipaddress.ip_address(p.hostname);raise ValueError('Use a public website hostname, not an IP address.')
                except ValueError as e:
                    if str(e).startswith('Use a public'):raise
                # Tavily Extract uses the same account and returns page content.
                r=requests.post('https://api.tavily.com/extract',headers={'Authorization':'Bearer '+key},json={'urls':[target],'extract_depth':'basic'},timeout=30)
                if not r.ok:raise RuntimeError('Webpage research failed; check Tavily authorization and credits.')
                return {'untrusted_web_content':json.dumps(r.json())[:6500]}
        if action=='design':
            from design_images import generate
            model=a.get('model','schnell')
            reference=self.file(a['reference_file_id']) if a.get('reference_file_id') else None
            if reference and reference.suffix!='.png':raise ValueError('Choose a generated design as the reference.')
            data=generate(self.root,self.clean(self.text(a,'prompt',2048)),model,reference=reference)
            return self.save('Design by '+self.agent,data,'.png','design')
        raise ValueError('Unknown action "'+action+'". Choose an action from the available staff tools.')

