"""Google Workspace access limited to app-created files and owned calendar events."""
import argparse
import json
import os
import uuid
from pathlib import Path
from urllib.parse import quote
import fcntl
import requests
from dotenv import dotenv_values

SCOPES=['https://www.googleapis.com/auth/drive.file','https://www.googleapis.com/auth/calendar.events.owned']

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
        if not self.path.exists():raise RuntimeError('Workspace sign-in needed: run workspace_tools.py --authorize on the Mac.')
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
        self.add_slides(identifier,slides);return {'id':identifier,'url':'https://docs.google.com/presentation/d/'+identifier+'/edit'}
    def add_slides(self,identifier,slides):
        identifier=self.own(identifier,'slides');operations=[]
        for text in slides:
            slide='slide_'+uuid.uuid4().hex;box='box_'+uuid.uuid4().hex
            operations.extend([{'createSlide':{'objectId':slide,'slideLayoutReference':{'predefinedLayout':'BLANK'}}},{'createShape':{'objectId':box,'shapeType':'TEXT_BOX','elementProperties':{'pageObjectId':slide,'size':{'width':{'magnitude':640,'unit':'PT'},'height':{'magnitude':300,'unit':'PT'}},'transform':{'scaleX':1,'scaleY':1,'translateX':40,'translateY':35,'unit':'PT'}}}},{'insertText':{'objectId':box,'text':text}},{'updateTextStyle':{'objectId':box,'textRange':{'type':'ALL'},'style':{'fontSize':{'magnitude':20,'unit':'PT'}},'fields':'fontSize'}}])
        return self.request('POST','https://slides.googleapis.com/v1/presentations/'+identifier+':batchUpdate',json={'requests':operations})
    def read_doc(self,identifier):return self.request('GET','https://docs.googleapis.com/v1/documents/'+self.own(identifier,'doc'))
    def read_slides(self,identifier):return self.request('GET','https://slides.googleapis.com/v1/presentations/'+self.own(identifier,'slides'))
    def calendar(self):return self.request('GET','https://www.googleapis.com/calendar/v3/calendars/primary/events',params={'maxResults':20,'singleEvents':'true','orderBy':'startTime','timeMin':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
    def create_event(self,summary,start,end):
        # Primary calendar only; do not invite guests or email notifications.
        return self.request('POST','https://www.googleapis.com/calendar/v3/calendars/primary/events',params={'sendUpdates':'none'},json={'summary':summary,'start':{'dateTime':start},'end':{'dateTime':end}})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--authorize',action='store_true',required=True);parser.parse_args()
    root=Path(__file__).resolve().parent;cfg=dotenv_values(root/'.env');workspace=Workspace(root)
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import AuthorizedSession
        data=json.loads((root/'google-oauth-client.json').read_text());flow=InstalledAppFlow.from_client_config(data,SCOPES)
        creds=flow.run_local_server(host='localhost',port=0,open_browser=True,authorization_prompt_message='Opening Google Workspace sign-in.',success_message='Workspace authorization received. You may close this tab.',login_hint=cfg.get('GOOGLE_MAIL_USER'),prompt='consent',timeout_seconds=300)
        if not creds.refresh_token:raise ValueError()
        # Confirm the same Workspace identity without printing private profile data.
        with AuthorizedSession(creds) as session:
            response=session.get('https://www.googleapis.com/drive/v3/about',params={'fields':'user(emailAddress)'},timeout=30)
            # The account the human selected is authoritative. It may be a primary
            # Google identity while inbox@ is only a mail alias.
            account=verified_account(response)
        workspace.save(creds);save_status(root,'connected',account,'Authorization verified with Google Drive.')
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
