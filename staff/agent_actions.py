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
from staff.workspace_tools import Workspace
from staff.staff_email import StaffMail, STAFF, valid_address
from staff.report_projects import active_project, ensure_project, project_slug, project_context, project_overview, save_project_overview, consolidated_project_report
from staff.report_format import standardize_staff_report
from staff.storefront import Storefront
from staff.staff_tasks import enqueue as enqueue_staff_task


ARTIFACT_MIME = {
    '.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.md': 'text/markdown; charset=utf-8', '.txt': 'text/plain; charset=utf-8',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
    '.gif': 'image/gif', '.svg': 'image/svg+xml',
    '.wav': 'audio/wav', '.mp3': 'audio/mpeg', '.aac': 'audio/aac', '.flac': 'audio/flac',
    '.ogg': 'audio/ogg', '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime',
    '.mkv': 'video/x-matroska',
}
def artifact_filename(title, suffix):
    """Every artifact gets a real filename: sanitized title + extension."""
    stem = re.sub(r'[^A-Za-z0-9._-]+', '-', str(title or '').strip()).strip('-.')[:100] or 'artifact'
    if not suffix.startswith('.'):
        suffix = '.' + suffix
    return stem + suffix.lower()
def artifact_mime(suffix):
    return ARTIFACT_MIME.get(suffix.lower(), 'application/octet-stream')

CATALOG = '''Actions (use exact names and argument keys):
report {project:project name optional,title,text}; document {title,text}; word_document {title,text}; pdf {title,text}; spreadsheet {title,values:[[cells]]}; presentation {title,slides:[text]}; print_sheet {title,file_ids:[generated PNG/JPEG IDs]}; drive_search {query}; drive_download {id,name};
email {to:one address or staff name or all_agents,subject,body,attachments:[file IDs optional]};
design {prompt,model:schnell or klein,reference_file_id:optional generated PNG ID for klein edits}; upload {file_id}; share_drive {file_id}; files {}; project_context {}; staff_reports {name:morgan,avery,jordan,cameron,or all}; project_create {name,overview,reason,activate:false}; project_brief_update {project:optional current project,overview,reason}; staff_message {to:Morgan,Avery,Jordan,or Cameron,text,attachments:[generated file IDs optional]}; staff_group_post {text,attachments:[generated file IDs optional]}; telegram_voice {text} (send a voice message in your own voice to the staff Telegram group); telegram_poll {question,options:[2-10 strings],anonymous:true,allows_multiple_answers:false} (post a Telegram poll to the staff group; votes are recorded for any agent to read); voice_tune {note} (refine your own voice direction; never tune another agent); workspace_files {};
products {query:optional}; product_update {id:optional existing/new product ID,title,description,price,image_file_id:optional uploaded/generated image,status:draft,ready,or published}; square_import_website {url:optional public store URL};
github_site_files {query:optional}; github_site_read {path}; github_preview_patch {path,find,replace,summary}; github_preview_write {path,content,summary};
github_main_files {query:optional}; github_main_read {path}; github_main_patch_request {path,find,replace,summary};
inbox {}; read_email {id}; reply_email {id,body}; delete_email {id} (only when the human says "delete email ID" explicitly; otherwise ask for that command); calendar {}; calendar_event {summary,start,end} (ISO datetimes with offsets, no attendees);
search {query}; webpage {url}. Always use presentation for any request containing PowerPoint, PPT, PPTX, Google Slides, slides, slide deck, deck, or presentation when the requested result is a multi-slide deliverable; PowerPoint/Slides/Deck are interchangeable and all route to a native Google Slides deck, and the result always includes its filename (ending .pptx, the export format), format, and live Slides link. Every generated artifact result — document, sheet, deck, drawing, PDF, Word, image, audio, or video — must be handed back with its filename including its extension, its format, and its link; never hand back a bare title or id. Use design for a standalone creative image, product asset, mock-up, or visual element. Canva and Figma are connected as secured Codex creative workspaces: describe the requested creative direction in the deck or design prompt, but never claim a Canva or Figma file was created unless a successful receipt provides its link. Workspace creation/upload is automatic. Use staff_message for ordinary agent-to-agent coordination in the configured Discord workspace or Telegram staff group. Email to James (CEO), Jaunee (Vice President), named Onyx & Ink agents, or an address explicitly listed in AUTONOMOUS_EXTERNAL_EMAILS sends automatically; any other outside address creates a draft awaiting human approval in Outbox. Address James as CEO and Jaunee as Vice President in executive correspondence. Email James only for a blocker, urgent risk, decision, or specific human input needed to finish an assignment. Keep CEO email under 120 words with one clear request; routine progress and completed reports stay in the dashboard. Do not put a sign-off or signature in email bodies; the mail system appends the official agent signature. Reports save into the named project folder; omit project to use the project selected on the Reports page. Document/sheet/presentation require Google Workspace sign-in. Design requires configured Cloudflare. No shell, arbitrary local files, purchases, deletion, or unapproved outside sending.
Media and storefront additions: doc_read {id}; sheet_read {id,range}; slides_read {id}; drawing_create {title} (creates a real but BLANK Google Drawing — shapes must be added in the Drawings editor, the API cannot draw content); drawing_read {id}; image_create {title,subtitle,format:png|jpg|jpeg|webp|gif|svg} (locally rendered, NOT AI-generated; AI design images are PNG); media_view {file_id} (verifies an existing image/PDF/audio/video file and reports only what it can actually decode); audio_generate {text,format:wav|mp3|aac|flac} (Avery only, real Cloudflare text-to-speech; default wav); video_generate {text} (Avery only — NOT CONFIGURED: no video provider is wired, so it always reports the blocker and creates nothing, never a placeholder); storefront_files {query}; storefront_read {path}; storefront_check {check: foundation, node_syntax, or package_scripts, path: JS file for node_syntax} (Jordan only, read-only allowlisted checks on the live storefront folder); github_preview_plan {path,find,replace,summary} and github_main_plan {path,find,replace,summary} (Jordan only — save a gated change plan in work/storefront-plans/ and stop; nothing is pushed or merged by the agent. Preview auto-publishes on Cloudflare only after James approves and the plan is applied and pushed to preview from the PC with the site deploy key; main merges only through a James-approved preview-to-main pull request, never automatically). product_create {slug,name,category,price_cents,stock_quantity,summary,description:[paragraphs],details:[items],keywords:[tags],image_prompts:[exactly 5 prompts] or image_files:[exactly 5 paths],mode:stage or publish,ceo_approved:true for publish} (Jordan only — stages the site listing patch and, in publish mode with CEO approval and a production Square token, creates the Square production catalog item and sets its stock; price and stock must come from the approved spec and are never invented; without approval or a production token it stages only and touches nothing in Square). square_link_catalog {items:[{slug,stock_quantity,one_of_one:false}],mode:stage or publish,ceo_approved:true for publish} (Jordan only — stages a Square-link backfill for existing catalogue slugs: Square production item + stock + squareId + server-map entry; one_of_one items must carry stock 1 and are labeled 'One of One' and 'sold as is'; without approval or a production token it stages only). No general shell: Jordan's storefront checks run only the allowlisted local checks above.'''

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
        cols = [row[1] for row in db.execute('PRAGMA table_info(files)')]
        if 'url' not in cols:
            db.execute('ALTER TABLE files ADD COLUMN url TEXT')
        return db
    def clean(self,text):
        for key,value in dotenv_values(self.root/'.env').items():
            if value and any(w in key.upper() for w in ('KEY','TOKEN','PASSWORD','SECRET')):text=text.replace(value,'[REDACTED]')
        return text
    def save(self,title,data,suffix,kind):
        if len(data)>15_000_000:raise ValueError('Generated file exceeds 15 MB.')
        identifier=uuid.uuid4().hex;disk=identifier+suffix;path=self.folder/disk
        path.write_bytes(data);path.chmod(0o600)
        display=artifact_filename(self.clean(str(title))[:160], suffix)
        with closing(self.db()) as db:
            db.execute('INSERT INTO files(id,name,agent,kind,url) VALUES (?,?,?,?,?)',(identifier,display,self.agent,kind,'/api/artifact?id='+identifier));db.commit()
        return {'file_id':identifier,'name':display,'filename':display,'format':suffix.lstrip('.').upper(),'mime_type':artifact_mime(suffix),'url':'/api/artifact?id='+identifier,'link':'/api/artifact?id='+identifier}
    def save_external(self,identifier,display,kind,url):
        """Record a Google Workspace artifact in the dashboard Files list so
        the agent can hand back its real link, not just a Drive id."""
        display=str(display)[:200]
        with closing(self.db()) as db:
            db.execute('INSERT OR REPLACE INTO files(id,name,agent,kind,url) VALUES (?,?,?,?,?)',(str(identifier),display,self.agent,kind,url));db.commit()
        return {'file_id':str(identifier),'name':display,'filename':display,'url':url,'link':url}
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
        out=[]
        with closing(self.db()) as db:
            rows=db.execute('SELECT id,name,agent,kind,url,created FROM files ORDER BY rowid DESC LIMIT 40').fetchall()
        for identifier,name,agent,kind,url,created in rows:
            display=str(name or '')
            if not Path(display).suffix:
                paths=list(self.folder.glob(identifier+'.*'))
                if len(paths)==1:
                    display=display.rstrip('.')+paths[0].suffix.lower()
            out.append({'file_id':identifier,'name':display,'filename':display,'format':Path(display).suffix.lstrip('.').upper(),'agent':agent,'kind':kind,'url':url or ('/api/artifact?id='+identifier),'link':url or ('/api/artifact?id='+identifier),'created':created})
        return out
    def recent(self):
        with closing(self.db()) as db:
            return [dict(zip(('agent','action','status','result','created'),row)) for row in db.execute('SELECT agent,action,status,result,created FROM actions ORDER BY rowid DESC LIMIT 30')]
    def site_root(self):
        for candidate in (self.root/'cloudflare-storefront', self.root/'storefront'):
            root=candidate.resolve()
            if (root/'.git').exists():return root
        raise RuntimeError('The approved storefront repository is not available on this Windows PC.')
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
        # Container gate: the dashboard container mounts the live storefront
        # read-only and holds no site deploy key, so a direct commit+push
        # cannot succeed there. Fail here, before any edit, and direct
        # Jordan to github_preview_plan. On a local PC run (writable repo
        # + SSH key) the original path below still applies.
        site_probe=self.site_root()
        ssh_dir=Path.home()/'.ssh'
        ssh_present=ssh_dir.exists() and any(ssh_dir.glob('id_*'))
        if site_probe.name=='storefront' or not ssh_present:
            raise RuntimeError('Preview push is gated from this container: the storefront mount is read-only and/or no site deploy key is present. Use github_preview_plan to save a gated plan for James to approve and apply from the PC; nothing was changed or pushed.')
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
        # Gated only: a main change is a plan, never an auto-merge. The
        # container cannot verify origin/main parity (no deploy key), so
        # the plan records that caveat for James's PR review.
        return self.storefront_plan('main',path,find,replace,summary)
    def live_storefront_root(self):
        for candidate in (self.root/'storefront', self.root/'cloudflare-storefront'):
            root=candidate.resolve()
            if root.is_dir():return root
        raise RuntimeError('The live storefront folder is not available in this container.')
    def live_storefront_path(self,raw,allow_missing=False):
        if not isinstance(raw,str) or not raw.strip() or len(raw)>220:raise ValueError('Provide a storefront file path.')
        relative=Path(raw.strip().replace('\\','/'))
        if relative.is_absolute() or '..' in relative.parts or any(part.startswith('.') for part in relative.parts):raise ValueError('Use a normal storefront file path, not a hidden or parent path.')
        if relative.suffix.lower() not in ('.html','.css','.js','.json','.md','.txt','.svg'):raise ValueError('Jordan may edit only storefront text, style, script, JSON, SVG, or Markdown files.')
        site=self.live_storefront_root();path=(site/relative).resolve()
        if not path.is_relative_to(site) or (not allow_missing and not path.is_file()):raise ValueError('That storefront file was not found.')
        return site,path,relative.as_posix()
    @staticmethod
    def doc_text(document):
        parts=[]
        for element in document.get('body',{}).get('content',[]):
            paragraph=element.get('paragraph')
            if not paragraph:continue
            text=''.join(item.get('textRun',{}).get('content','') for item in paragraph.get('elements',[]))
            if text.strip():parts.append(text.rstrip())
        return '\n'.join(parts)
    @staticmethod
    def slides_text(deck):
        slides=[]
        for slide in deck.get('slides',[]):
            texts=[]
            for element in slide.get('pageElements',[]):
                shape=element.get('shape')
                if shape and shape.get('text'):
                    texts.append(''.join(item.get('textRun',{}).get('content','') for item in shape['text'].get('textElements',[]) if 'textRun' in item))
            slides.append(' '.join(item.strip() for item in texts if item.strip()))
        return slides
    def storefront_check(self,check,path=''):
        if self.agent!='Jordan':raise ValueError('Only Jordan may run storefront checks.')
        site=self.live_storefront_root()
        if check=='foundation':
            script=site/'scripts'/'check-foundation.js'
            if not script.is_file():raise ValueError('The storefront foundation check was not found.')
            result=subprocess.run(['node',str(script)],capture_output=True,text=True,timeout=60,cwd=str(site))
            return {'check':'foundation','returncode':result.returncode,'output':(result.stdout+result.stderr)[-3000:]}
        if check=='node_syntax':
            _,target,relative=self.live_storefront_path(path)
            if target.suffix.lower()!='.js':raise ValueError('Syntax checks apply to JavaScript files only.')
            result=subprocess.run(['node','--check',str(target)],capture_output=True,text=True,timeout=30)
            return {'check':'node_syntax','path':relative,'ok':result.returncode==0,'output':(result.stderr or result.stdout or 'Syntax OK.')[:1000]}
        if check=='package_scripts':
            data=json.loads((site/'package.json').read_text(encoding='utf-8'))
            return {'check':'package_scripts','scripts':data.get('scripts',{})}
        raise ValueError('Use check: foundation, node_syntax, or package_scripts.')
    def storefront_plan(self,kind,path,find,replace,summary):
        if self.agent!='Jordan':raise ValueError('Only Jordan may prepare storefront changes.')
        site,target,relative=self.live_storefront_path(path)
        current=target.read_text(encoding='utf-8')
        if not isinstance(summary,str) or not summary.strip() or len(summary)>120:raise ValueError('Provide a concise change summary.')
        if not isinstance(find,str) or not find or current.count(find)!=1:raise ValueError('The requested replacement must match exactly one existing passage.')
        self.site_text_safe(find);self.site_text_safe(replace)
        proposed=current.replace(find,replace,1);self.site_text_safe(proposed)
        folder=self.root/'work'/'storefront-plans';folder.mkdir(parents=True,exist_ok=True)
        identifier=uuid.uuid4().hex[:12]
        plan={'kind':kind,'path':relative,'summary':summary.strip(),'find':find,'replace':replace,'live_file_sha256':hashlib.sha256(current.encode()).hexdigest(),'agent':self.agent,'status':'plan_only_not_pushed_not_merged'}
        plan_path=folder/(kind+'-'+identifier+'.json');plan_path.write_text(json.dumps(plan,indent=2),encoding='utf-8');plan_path.chmod(0o600)
        if kind=='preview':
            gate='Cloudflare auto-publishes preview only after James approves and the plan is applied and pushed to preview from the PC (host git with the site deploy key). Nothing was pushed by this action.'
        else:
            gate='A preview-to-main pull request must be created on GitHub and explicitly approved by James before any merge. This action never merges, and the container cannot verify origin/main parity because it holds no site deploy key.'
        return {'result':('Preview change plan saved, NOT pushed.' if kind=='preview' else 'Main change plan saved, NOT merged.'),'plan_file':str(plan_path),'path':relative,'summary':summary.strip(),'gate':gate}
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
            'google_slides':'presentation','google_slide':'presentation','google_presentation':'presentation',
            'slides':'presentation','slide_deck':'presentation','deck':'presentation','canva_presentation':'presentation','figma_slides':'presentation',
            'powerpoint':'presentation','power_point':'presentation','ppt':'presentation','pptx':'presentation','powerpoint_presentation':'presentation','google_powerpoint':'presentation','presentation_deck':'presentation','slideshow':'presentation','slide_show':'presentation',
            'create_pdf':'pdf','create_docx':'word_document','docx':'word_document',
        }
        action=aliases.get(action,action)
        if action in ('report','document','word_document','pdf','spreadsheet','presentation'):
            title=self.text(a,'title',160)
            if action=='report':return self.save_report(title,self.text(a,'text'),a.get('project'))
            if action=='document':
                result=self.workspace.create_doc(title,self.clean(self.text(a,'text')));self.save_external(result['id'],result['filename'],'google-doc',result['url']);return result
            if action=='word_document':return self.save_word_document(title,self.text(a,'text'))
            if action=='pdf':return self.save_pdf(title,self.text(a,'text'))
            if action=='spreadsheet':
                values=a.get('values')
                if not isinstance(values,list) or not values or len(values)>200 or any(not isinstance(r,list) or len(r)>30 or any(not isinstance(c,(str,int,float,bool)) or len(str(c))>2000 for c in r) for r in values):raise ValueError('Use up to 200 rows and 30 columns of plain values.')
                values=json.loads(self.clean(json.dumps(values)))
                result=self.workspace.create_sheet(title,values);self.save_external(result['id'],result['filename'],'google-sheet',result['url']);return result
            slides=a.get('slides')
            # Title and section-divider slides are intentionally brief.  Require
            # a real deck with at least two substantive slides without rejecting
            # those legitimate short slides.
            if not isinstance(slides,list) or not 3<=len(slides)<=15 or any(not isinstance(s,str) or not s.strip() or len(s)>3000 for s in slides) or sum(len(s.strip())>=40 for s in slides)<2:raise ValueError('Use 3–15 slides with at least two substantive slides (40+ characters); title slides may be shorter.')
            result=self.workspace.create_slides(title,[self.clean(s) for s in slides]);self.save_external(result['id'],result['filename'],'google-slides',result['url']);return result
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
            from staff.dashboard_mail import Mailbox
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
                from staff.discord_bridge import DiscordBridge
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
            from staff.square_commerce import SquareCommerce
            from staff.website_catalog import collect,download_image
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
                from staff.square_commerce import SquareCommerce
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
                from staff.web_research import search
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
            from staff.design_images import generate, generate_text_free
            from staff.design_learning import infer_product
            model=a.get('model','schnell')
            reference=self.file(a['reference_file_id']) if a.get('reference_file_id') else None
            if reference and reference.suffix!='.png':raise ValueError('Choose a generated design as the reference.')
            prompt=self.clean(self.text(a,'prompt',2048))
            product=infer_product(prompt)
            if product:
                data=generate_text_free(self.root,prompt,model,reference=reference,product=product,agent=self.agent)
            else:
                data=generate(self.root,prompt,model,reference=reference)
            return self.save('Design by '+self.agent,data,'.png','design')
        if action=='doc_read':
            document=self.workspace.read_doc(self.text(a,'id',200))
            return {'title':document.get('title'),'text':self.doc_text(document)[:4000],'url':'https://docs.google.com/document/d/'+document.get('documentId','')+'/edit'}
        if action=='sheet_read':
            identifier=self.text(a,'id',200);cell_range=self.text(a,'range',80) if a.get('range') else 'Sheet1!A1:Z200'
            values=self.workspace.read_sheet(identifier,cell_range)
            return {'range':values.get('range'),'values':values.get('values',[])}
        if action=='slides_read':
            deck=self.workspace.read_slides(self.text(a,'id',200))
            return {'title':deck.get('title'),'slides':self.slides_text(deck),'slide_count':len(deck.get('slides',[])),'url':'https://docs.google.com/presentation/d/'+deck.get('presentationId','')+'/edit'}
        if action=='drawing_create':
            result=self.workspace.create_drawing(self.text(a,'title',160));self.save_external(result['id'],result['filename'],'google-drawing',result['url']);return result
        if action=='drawing_read':
            identifier=self.text(a,'id',200);info=self.workspace.drawing_info(identifier)
            try:data,ctype=self.workspace.export_drawing(identifier);export={'bytes':len(data),'content_type':ctype}
            except Exception as error:export={'error':str(error)[:200]}
            return {'info':info,'export':export}
        if action=='image_create':
            from staff.media_tools import render_image
            title=self.text(a,'title',160);subtitle=a.get('subtitle','') if isinstance(a.get('subtitle'),str) else ''
            fmt=str(a.get('format','png') or 'png').strip().lower().lstrip('.')
            if fmt not in ('png','jpg','jpeg','webp','gif','svg'):raise ValueError('Image format must be png, jpg, jpeg, webp, gif, or svg.')
            result=self.save(title,render_image(title,subtitle,fmt),'.'+fmt,'local-image')
            result.update({'rendered':'local','note':'Locally rendered, not AI-generated. AI design images are PNG; use image_create for jpg/jpeg/webp/gif/svg.'})
            return result
        if action=='media_view':
            from staff.media_tools import view_media
            return view_media(self.file(a.get('file_id','')))
        if action=='audio_generate':
            if self.agent!='Avery':raise ValueError('Only Avery generates audio.')
            from staff.media_tools import generate_audio, convert_audio
            fmt=str(a.get('format','wav') or 'wav').strip().lower().lstrip('.')
            if fmt=='acc':fmt='aac'
            if fmt not in ('wav','mp3','aac','flac'):raise ValueError('Audio format must be wav, mp3, aac, or flac.')
            generated=generate_audio(self.root,self.text(a,'text',1000))
            data=generated['data'] if fmt=='wav' else convert_audio(generated['data'],fmt)
            result=self.save('Audio by Avery',data,'.'+fmt,'generated-audio')
            result.update({'format':fmt.upper(),'duration_seconds':generated['duration_seconds'],'sample_rate':generated['sample_rate'],'provider':'Cloudflare Workers AI text-to-speech'})
            return result
        if action=='video_generate':
            if self.agent!='Avery':raise ValueError('Only Avery generates video.')
            from staff.media_tools import generate_video
            generate_video(self.root,self.text(a,'text',1000))
        if action=='storefront_files':
            if self.agent!='Jordan':raise ValueError('Only Jordan may access the storefront folder.')
            site=self.live_storefront_root();needle=str(a.get('query','')).strip().lower();files=[]
            for path in sorted(site.rglob('*')):
                if not path.is_file() or '.git' in path.parts or 'node_modules' in path.parts:continue
                relative=path.relative_to(site).as_posix()
                if needle and needle not in relative.lower():continue
                files.append(relative)
                if len(files)>=160:break
            return {'storefront_root':str(site),'files':files,'count':len(files),'policy':'Live read-only mount. Writes go through github_preview_plan / github_main_plan only.'}
        if action=='storefront_read':
            if self.agent!='Jordan':raise ValueError('Only Jordan may access the storefront folder.')
            site,target,relative=self.live_storefront_path(self.text(a,'path',220))
            if target.stat().st_size>120_000:raise ValueError('That file is too large for chat. Ask for a more specific file.')
            text=target.read_text(encoding='utf-8');self.site_text_safe(text)
            return {'path':relative,'content':text[:60000]}
        if action=='storefront_check':
            return self.storefront_check(str(a.get('check','')),str(a.get('path','')))
        if action=='github_preview_plan':
            return self.storefront_plan('preview',self.text(a,'path',220),self.text(a,'find',120000),self.text(a,'replace',300000),self.text(a,'summary',120))
        if action=='github_main_plan':
            return self.storefront_plan('main',self.text(a,'path',220),self.text(a,'find',120000),self.text(a,'replace',300000),self.text(a,'summary',120))
        if action in ('cloudflare_d1_list','cloudflare_d1_create','cloudflare_d1_query','cloudflare_pages_project','cloudflare_pages_bind_d1'):
            if self.agent!='Jordan':raise ValueError('Only Jordan manages Cloudflare infrastructure.')
            from staff.cloudflare_ops import CloudflareOps
            ops=CloudflareOps(self.root)
            if not ops.configured:raise ValueError('Connect Cloudflare in Settings before managing D1 or Pages.')
            if action=='cloudflare_d1_list':return ops.list_databases()
            if action=='cloudflare_d1_create':return ops.create_database(self.text(a,'name',64))
            if action=='cloudflare_d1_query':return ops.query(self.text(a,'database',120),self.text(a,'sql',10000))
            if action=='cloudflare_pages_project':return ops.get_project(str(a.get('project','') or ''))
            environment=str(a.get('environment','production') or 'production').strip().lower()
            variable=str(a.get('variable','DB') or 'DB').strip()
            return ops.bind_d1(self.text(a,'database',120),environment,variable,str(a.get('project','') or ''))
        if action=='square_inventory_set':
            if self.agent!='Jordan':raise ValueError('Only Jordan manages Square inventory.')
            from staff.square_commerce import SquareCommerce
            square=SquareCommerce(self.root)
            if not square.configured:raise ValueError('Connect Square in Settings before setting stock.')
            quantity=a.get('quantity')
            if isinstance(quantity,bool) or not isinstance(quantity,int):raise ValueError('Use a whole stock quantity from 0 to 100000.')
            return square.set_inventory(self.text(a,'item',200),quantity,str(a.get('location_id','') or '').strip())
        if action=='product_create':
            if self.agent!='Jordan':raise ValueError('Only Jordan manages product creation.')
            from staff.product_pipeline import create_product
            return create_product(self.root,a)
        if action=='square_link_catalog':
            if self.agent!='Jordan':raise ValueError('Only Jordan manages Square catalog linking.')
            from staff.square_link import link_existing_products
            return link_existing_products(self.root,a)
        if action=='telegram_voice':
            from staff.agent_chat import TelegramBridge
            from staff.voice import speak
            cfg=dotenv_values(self.root/'.env')
            chat=str(cfg.get('TELEGRAM_AGENT_GROUP_CHAT','')).strip()
            token=cfg.get('TELEGRAM_'+self.agent.upper()+'_BOT_TOKEN','')
            if not re.fullmatch(r'-?\d{5,20}',chat) or not token:raise ValueError('Configure the staff Telegram group and this agent\u2019s dedicated bot in Settings.')
            spoken=speak(self.root,self.agent,self.text(a,'text',3000))
            TelegramBridge(self.root,None,None,agent=self.agent).send_voice_to_chat(token,chat,spoken['data'])
            return {'result':'Voice message sent as '+self.agent+' to the staff Telegram group.','channel':chat,'duration_seconds':spoken['duration_seconds'],'provider':spoken['provider']}
        if action=='telegram_poll':
            from staff.agent_chat import TelegramBridge
            cfg=dotenv_values(self.root/'.env')
            chat=str(cfg.get('TELEGRAM_AGENT_GROUP_CHAT','')).strip()
            token=cfg.get('TELEGRAM_'+self.agent.upper()+'_BOT_TOKEN','')
            if not re.fullmatch(r'-?\d{5,20}',chat) or not token:raise ValueError('Configure the staff Telegram group and this agent\u2019s dedicated bot in Settings.')
            options=a.get('options',[])
            if not isinstance(options,list):raise ValueError('Poll options must be a list of 2-10 strings.')
            sent=TelegramBridge(self.root,None,None,agent=self.agent).send_poll(token,chat,self.text(a,'question',300),options,a.get('anonymous',True) is not False,a.get('allows_multiple_answers',False) is True)
            poll=(sent.get('poll',{}) if isinstance(sent,dict) else {})
            return {'result':'Poll sent as '+self.agent+' to the staff Telegram group.','poll_id':str(poll.get('id','')),'question':self.text(a,'question',300)}
        if action=='voice_tune':
            from staff.voice import tune_voice
            tuned=tune_voice(self.root,self.agent,self.text(a,'note',200))
            return {'result':'Voice direction updated for '+self.agent+'.','notes':tuned['notes']}
        raise ValueError('Unknown action "'+action+'". Choose an action from the available staff tools.')

