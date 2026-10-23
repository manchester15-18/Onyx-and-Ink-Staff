"""Google Workspace access limited to app-created files and owned calendar events."""
import argparse
import json
import os
import uuid
from pathlib import Path
from urllib.parse import quote
import fcntl
import requests
import time
from dotenv import dotenv_values

SCOPES=['https://www.googleapis.com/auth/drive','https://www.googleapis.com/auth/calendar.events.owned']

class WorkspaceSetupError(RuntimeError):pass

def verified_account(response):
    if not response.ok:
        reasons=set()
        try:reasons={item.get('reason','') for item in response.json().get('error',{}).get('errors',[])}
        except (ValueError,AttributeError,TypeError):pass
        if response.status_code==403 or reasons & {'accessNotConfigured','SERVICE_DISABLED'}:
            raise WorkspaceSetupError('Google Drive could not verify the account. Enable the Google Drive API in the OAuth client project, then reconnect.')
        raise WorkspaceSetupError('Google Drive account verification failed (HTTP '+str(response.status_code)+').')
    account=response.json().get('user',{}).get('emailAddress','').lower()
    if not account:raise WorkspaceSetupError('Google Drive did not return the selected account identity.')
    return account

def save_status(root,state,account='',message=''):
    path=Path(root)/'work'/'workspace-status.json';path.parent.mkdir(exist_ok=True)
    data={'state':state,'account':account,'message':message,'checked':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
    temp=path.with_suffix('.'+uuid.uuid4().hex+'.tmp');temp.write_text(json.dumps(data));temp.chmod(0o600);temp.replace(path)

class Workspace:
    def __init__(self,root):self.root=Path(root);self.path=self.root/'work'/'google-workspace-token.json'
    def save(self,credentials):
        self.path.parent.mkdir(exist_ok=True);tmp=self.path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(credentials.to_json())
        tmp.replace(self.path)
    def token(self):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        if not self.path.exists():raise RuntimeError('Workspace sign-in needed: run workspace_tools.py --authorize on the Windows PC.')
        try:
            c=Credentials.from_authorized_user_file(str(self.path),SCOPES)
            if not c.valid:c.refresh(Request());self.save(c)
            return c.token
        except Exception:raise RuntimeError('Workspace authorization needs renewal.') from None
    def request(self,method,url,**kwargs):
        response=requests.request(method,url,headers={'Authorization':'Bearer '+self.token()},timeout=45,**kwargs)
        if not response.ok:raise RuntimeError('Workspace request failed. Check enabled APIs, authorization, and file access.')
        return response.json() if response.content else {}
    def registry(self):
        p=self.root/'work'/'workspace-files.json'
        try:return json.loads(p.read_text())
        except (OSError,ValueError):return {}
    def register(self,identifier,kind):
        p=self.root/'work'/'workspace-files.json';p.parent.mkdir(parents=True,exist_ok=True)
        with p.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            data=self.registry();data[identifier]=kind
            tmp=p.with_suffix('.'+uuid.uuid4().hex+'.tmp');tmp.write_text(json.dumps(data));tmp.chmod(0o600);tmp.replace(p)
    def own(self,identifier,kind=None):
        if identifier not in self.registry() or kind and self.registry()[identifier]!=kind:raise ValueError('Choose a file created by this staff app.')
        return quote(identifier,safe='')
    def list_files(self):
        return self.request('GET','https://www.googleapis.com/drive/v3/files',params={'q':'trashed=false','pageSize':30,'fields':'files(id,name,mimeType,webViewLink)'})
    def find_files(self, query):
        query=str(query or '').strip()
        if not query or len(query)>160:raise ValueError('Provide a Drive filename or short search phrase.')
        escaped=query.replace("'","\\'")
        return self.request('GET','https://www.googleapis.com/drive/v3/files',params={'q':f"trashed=false and name contains '{escaped}'",'pageSize':20,'fields':'files(id,name,mimeType,webViewLink,size)'})
    def download(self, identifier):
        if not identifier or len(str(identifier))>200:raise ValueError('Choose a Drive file ID.')
        response=requests.get('https://www.googleapis.com/drive/v3/files/'+quote(str(identifier),safe='')+'?alt=media',headers={'Authorization':'Bearer '+self.token()},timeout=60)
        if not response.ok:raise RuntimeError('Google Drive could not download that selected file.')
        if len(response.content)>15_000_000:raise RuntimeError('Selected Drive file exceeds the 15 MB staff-file limit.')
        return response.content,response.headers.get('Content-Type','application/octet-stream')
    def trash(self,identifier):
        """Move an app-created Drive file to Trash; never delete permanent data."""
        return self.request('PATCH','https://www.googleapis.com/drive/v3/files/'+self.own(identifier),json={'trashed':True})
    def upload(self,path,name):
        # Resumable upload; content is only a bounded generated artifact.
        mime=__import__('mimetypes').guess_type(str(path))[0] or 'application/octet-stream'
        response=requests.post('https://www.googleapis.com/upload/drive/v3/files',params={'uploadType':'resumable','fields':'id,webViewLink'},headers={'Authorization':'Bearer '+self.token(),'X-Upload-Content-Type':mime},json={'name':name},timeout=45)
        if not response.ok:raise RuntimeError('Drive upload could not start.')
        location=response.headers.get('Location','')
        from urllib.parse import urlparse
        parsed=urlparse(location)
        if parsed.scheme!='https' or parsed.hostname!='www.googleapis.com' or not parsed.path.startswith('/upload/drive/'):raise RuntimeError('Drive returned an invalid upload location.')
        result=self.request('PUT',location,data=path.read_bytes())
        self.register(result['id'],'file');return result
    def create_doc(self,title,text):
        doc=self.request('POST','https://docs.googleapis.com/v1/documents',json={'title':title});identifier=doc['documentId'];self.register(identifier,'doc')
        self.append_doc(identifier,text);return {'id':identifier,'url':'https://docs.google.com/document/d/'+identifier+'/edit'}
    def append_doc(self,identifier,text):
        identifier=self.own(identifier,'doc')
        return self.request('POST','https://docs.googleapis.com/v1/documents/'+identifier+':batchUpdate',json={'requests':[{'insertText':{'endOfSegmentLocation':{},'text':text+'\n'}}]})
    def replace_doc(self,identifier,text):
        """Replace the body of an app-created Doc while retaining its stable link."""
        document=self.read_doc(identifier);end=max((part.get('endIndex',1) for part in document.get('body',{}).get('content',[])),default=1)
        requests=[]
        if end>2:requests.append({'deleteContentRange':{'range':{'startIndex':1,'endIndex':end-1}}})
        requests.append({'insertText':{'location':{'index':1},'text':text.rstrip()+'\n'}})
        return self.request('POST','https://docs.googleapis.com/v1/documents/'+self.own(identifier,'doc')+':batchUpdate',json={'requests':requests})
    def create_sheet(self,title,values):
        sheet=self.request('POST','https://sheets.googleapis.com/v4/spreadsheets',json={'properties':{'title':title}});identifier=sheet['spreadsheetId'];self.register(identifier,'sheet')
        self.write_sheet(identifier,'Sheet1!A1',values);return {'id':identifier,'url':sheet['spreadsheetUrl']}
    def write_sheet(self,identifier,cell_range,values):
        identifier=self.own(identifier,'sheet')
        return self.request('PUT','https://sheets.googleapis.com/v4/spreadsheets/'+identifier+'/values/'+quote(cell_range,safe=''),params={'valueInputOption':'RAW'},json={'values':values})
    def read_sheet(self,identifier,cell_range):
        identifier=self.own(identifier,'sheet')
        return self.request('GET','https://sheets.googleapis.com/v4/spreadsheets/'+identifier+'/values/'+quote(cell_range,safe=''))
    def create_slides(self,title,slides):
        deck=self.request('POST','https://slides.googleapis.com/v1/presentations',json={'title':title});identifier=deck['presentationId'];self.register(identifier,'slides')
        self.replace_slides(identifier,slides);return {'id':identifier,'url':'https://docs.google.com/presentation/d/'+identifier+'/edit'}
    def _slide_operations(self,slides):
        """Build a restrained, editorial 16:9 slide composition for staff decks."""
        operations=[]
        # Google Slides presents a 720×405 point 16:9 canvas.  Keep the
        # existing 960×540 composition grid, but scale every geometry value
        # at creation time so no generated element can fall outside the slide.
        scale=.75
        palette={'ink':{'red':0.035,'green':0.043,'blue':0.067},'paper':{'red':0.957,'green':0.941,'blue':0.900},'gold':{'red':0.89,'green':0.61,'blue':0.18},'muted':{'red':0.72,'green':0.73,'blue':0.76}}
        def textbox(identifier,slide,text,x,y,w,h,size,color,bold=False):
            operations.extend([{'createShape':{'objectId':identifier,'shapeType':'TEXT_BOX','elementProperties':{'pageObjectId':slide,'size':{'width':{'magnitude':w*scale,'unit':'PT'},'height':{'magnitude':h*scale,'unit':'PT'}},'transform':{'scaleX':1,'scaleY':1,'translateX':x*scale,'translateY':y*scale,'unit':'PT'}}}}, {'insertText':{'objectId':identifier,'text':text}}, {'updateTextStyle':{'objectId':identifier,'textRange':{'type':'ALL'},'style':{'fontFamily':'Aptos','fontSize':{'magnitude':size,'unit':'PT'},'bold':bold,'foregroundColor':{'opaqueColor':{'rgbColor':color}}},'fields':'fontFamily,fontSize,bold,foregroundColor'}}])
        for index,raw in enumerate(slides,1):
            text=str(raw).strip();title,separator,body=text.partition('\n')
            # A generated deck has one compact headline per slide.  This keeps
            # automatic text inside the title region even when a model returns
            # prose in place of a slide title.
            title=title.strip()[:72]
            body=body.lstrip('\n').strip() if separator else ''
            slide='slide_'+uuid.uuid4().hex;accent='accent_'+uuid.uuid4().hex;title_box='title_'+uuid.uuid4().hex;body_box='body_'+uuid.uuid4().hex;label_box='label_'+uuid.uuid4().hex;footer_box='footer_'+uuid.uuid4().hex
            operations.extend([{'createSlide':{'objectId':slide,'slideLayoutReference':{'predefinedLayout':'BLANK'}}},{'updatePageProperties':{'objectId':slide,'pageProperties':{'pageBackgroundFill':{'solidFill':{'color':{'rgbColor':palette['ink']}}}},'fields':'pageBackgroundFill.solidFill.color'}},{'createShape':{'objectId':accent,'shapeType':'RECTANGLE','elementProperties':{'pageObjectId':slide,'size':{'width':{'magnitude':132*scale,'unit':'PT'},'height':{'magnitude':7*scale,'unit':'PT'}},'transform':{'scaleX':1,'scaleY':1,'translateX':64*scale,'translateY':55*scale,'unit':'PT'}}}},{'updateShapeProperties':{'objectId':accent,'shapeProperties':{'shapeBackgroundFill':{'solidFill':{'color':{'rgbColor':palette['gold']}}},'outline':{'propertyState':'NOT_RENDERED'}},'fields':'shapeBackgroundFill.solidFill.color,outline.propertyState'}}])
            if index==1:
                textbox(title_box,slide,title,64,112,720,72,42,palette['paper'],True)
                textbox(body_box,slide,body,68,214,550,150,20,palette['muted'])
                textbox(label_box,slide,'ONYX & INK',68,80,240,24,12,palette['gold'],True)
            else:
                textbox(label_box,slide,'ONYX & INK  /  '+str(index).zfill(2),68,80,300,22,11,palette['gold'],True)
                textbox(title_box,slide,title,64,116,690,60,34,palette['paper'],True)
                rule='rule_'+uuid.uuid4().hex
                operations.extend([{'createShape':{'objectId':rule,'shapeType':'RECTANGLE','elementProperties':{'pageObjectId':slide,'size':{'width':{'magnitude':5*scale,'unit':'PT'},'height':{'magnitude':248*scale,'unit':'PT'}},'transform':{'scaleX':1,'scaleY':1,'translateX':68*scale,'translateY':205*scale,'unit':'PT'}}}},{'updateShapeProperties':{'objectId':rule,'shapeProperties':{'shapeBackgroundFill':{'solidFill':{'color':{'rgbColor':palette['gold']}}},'outline':{'propertyState':'NOT_RENDERED'}},'fields':'shapeBackgroundFill.solidFill.color,outline.propertyState'}}])
                textbox(body_box,slide,body,98,205,650,250,21,palette['paper'])
            textbox(footer_box,slide,'ONYX & INK',750,493,130,18,10,palette['muted'],True)
        return operations
    def add_slides(self,identifier,slides):
        identifier=self.own(identifier,'slides')
        return self.request('POST','https://slides.googleapis.com/v1/presentations/'+identifier+':batchUpdate',json={'requests':self._slide_operations(slides)})
    def replace_slides(self,identifier,slides):
        """Replace an app-owned deck in place so its shared link stays valid."""
        deck=self.read_slides(identifier);encoded=self.own(identifier,'slides')
        operations=self._slide_operations(slides)
        operations.extend({'deleteObject':{'objectId':slide['objectId']}} for slide in deck.get('slides',[]))
        return self.request('POST','https://slides.googleapis.com/v1/presentations/'+encoded+':batchUpdate',json={'requests':operations})
    def read_doc(self,identifier):return self.request('GET','https://docs.googleapis.com/v1/documents/'+self.own(identifier,'doc'))
    def read_slides(self,identifier):return self.request('GET','https://slides.googleapis.com/v1/presentations/'+self.own(identifier,'slides'))
    def calendar(self):return self.request('GET','https://www.googleapis.com/calendar/v3/calendars/primary/events',params={'maxResults':20,'singleEvents':'true','orderBy':'startTime','timeMin':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
    def create_event(self,summary,start,end):
        # Primary calendar only; do not invite guests or email notifications.
        return self.request('POST','https://www.googleapis.com/calendar/v3/calendars/primary/events',params={'sendUpdates':'none'},json={'summary':summary,'start':{'dateTime':start},'end':{'dateTime':end}})

def authorize_workspace(root, cfg, workspace):
    """Run the OAuth callback in Docker while the requesting browser opens Google."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import AuthorizedSession
    from urllib.parse import urlencode
    # Google permits HTTP only for a local loopback callback. OAuthlib requires this
    # explicit opt-in before it will exchange the authorization code on localhost.
    os.environ['OAUTHLIB_INSECURE_TRANSPORT']='1'
    data=json.loads((root/'google-oauth-client.json').read_text())
    flow=InstalledAppFlow.from_client_config(data,SCOPES,autogenerate_code_verifier=True)
    # Desktop OAuth clients may only use a loopback callback. The Windows host owns this
    # authorization, so it must be started from the dashboard opened on that host.
    redirect_uri='http://localhost:8765/workspace-oauth-callback'
    flow.redirect_uri=redirect_uri
    authorization_url,state=flow.authorization_url(access_type='offline',prompt='consent',login_hint=cfg.get('GOOGLE_MAIL_USER'))
    auth_path=root/'work'/'workspace-auth-url.json'
    response_path=root/'work'/'workspace-auth-response.json'
    try:response_path.unlink()
    except FileNotFoundError:pass
    # Retain the short-lived PKCE verifier with this protected, five-minute request.
    # It prevents a container restart from stranding an already-approved login.
    auth_path.write_text(json.dumps({'url':authorization_url,'state':state,'code_verifier':flow.code_verifier}));auth_path.chmod(0o600)
    deadline=time.monotonic()+300
    while not response_path.exists() and time.monotonic()<deadline:time.sleep(.2)
    if not response_path.exists():raise TimeoutError()
    response=json.loads(response_path.read_text())
    if response.get('state') != state:raise ValueError()
    saved=json.loads(auth_path.read_text())
    if saved.get('code_verifier'):
        flow.code_verifier=saved['code_verifier']
    flow.fetch_token(authorization_response=redirect_uri+'?'+response['query'])
    credentials=flow.credentials
    if not credentials.refresh_token:raise ValueError()
    with AuthorizedSession(credentials) as session:
        response=session.get('https://www.googleapis.com/drive/v3/about',params={'fields':'user(emailAddress)'},timeout=30)
        account=verified_account(response)
    workspace.save(credentials);save_status(root,'connected',account,'Authorization verified with Google Drive.')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--authorize',action='store_true',required=True);parser.parse_args()
    root=Path(__file__).resolve().parent;cfg=dotenv_values(root/'.env');workspace=Workspace(root)
    try:
        authorize_workspace(root,cfg,workspace)
        print('Workspace authorized. Mail authorization is unchanged.');return 0
    except Exception as error:
        if isinstance(error,TimeoutError):reason='Google sign-in timed out before approval completed.'
        elif isinstance(error,WorkspaceSetupError):reason=str(error)
        elif isinstance(error,ValueError):reason='Google returned an invalid authorization response. Download a fresh Desktop OAuth client if this repeats.'
        elif isinstance(error,OSError):reason='The local Google sign-in callback could not start or complete.'
        elif isinstance(error,requests.RequestException):reason='Google Drive could not verify the authorized account. Check that the Drive API is enabled.'
        else:reason='Google authorization was not saved. Check the OAuth consent policy and enabled Workspace APIs.'
        save_status(root,'failed',message=reason)
        print('Workspace setup failed. Check the Desktop client, selected Workspace account, enabled APIs, and consent policy. No new authorization saved.');return 1

if __name__=='__main__':raise SystemExit(main())
