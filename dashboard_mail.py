"""Gmail inbox access for the local dashboard; no model calls."""
import base64
import re
import threading
import time
from email.utils import parseaddr
from dotenv import dotenv_values
import requests
from google_mail_auth import GoogleMailAuth

class Mailbox:
    def __init__(self, root):
        self.root=root; self.lock=threading.Lock();self.cache={};self.cached_at={}
    def request(self, path, params=None):
        config=dotenv_values(self.root/'.env')
        auth=GoogleMailAuth(self.root,config.get('GOOGLE_MAIL_USER',''))
        response=requests.get('https://gmail.googleapis.com/gmail/v1/users/me/'+path,
            params=params,headers={'Authorization':'Bearer '+auth.token()},timeout=30)
        if response.status_code!=200:
            raise RuntimeError('Google inbox unavailable. Check authorization and Gmail API access.')
        return response.json()
    @staticmethod
    def headers(message):
        return {h['name'].lower():h['value'] for h in message.get('payload',{}).get('headers',[])}
    def list(self, refresh=False, mailbox="all"):
        with self.lock:
            config=dotenv_values(self.root/'.env')
            names={'Owner':'OWNER_EMAIL','Morgan':'MORGAN_EMAIL','Avery':'AVERY_EMAIL','Jordan':'JORDAN_EMAIL','Cameron':'CAMERON_EMAIL','Shared':'GOOGLE_MAIL_USER'}
            if mailbox != 'all' and mailbox not in names:raise ValueError('Unknown inbox.')
            params={'labelIds':'INBOX','maxResults':25}
            if mailbox != 'all':
                from staff_email import valid_address
                address=config.get(names[mailbox],'')
                if not valid_address(address):raise ValueError('Inbox address is not configured.')
                params['q']='{to:'+address+' deliveredto:'+address+' cc:'+address+'}'
            if not refresh and mailbox in self.cache and time.monotonic()-self.cached_at.get(mailbox,0)<60:return self.cache[mailbox]
            entries=self.request('messages',params).get('messages',[])
            result=[]
            for entry in entries:
                msg=self.request('messages/'+entry['id'],{'format':'metadata','metadataHeaders':['From','To','Subject','Date']})
                h=self.headers(msg)
                result.append({'id':msg['id'],'from':h.get('from',''),'to':h.get('to',''),'subject':h.get('subject','(No subject)'),
                    'date':h.get('date',''),'unread':'UNREAD' in msg.get('labelIds',[])})
            self.cache[mailbox]=result;self.cached_at[mailbox]=time.monotonic();return result
    def read(self, identifier):
        if not re.fullmatch('[a-fA-F0-9]+',identifier):raise ValueError('Invalid message.')
        msg=self.request('messages/'+identifier,{'format':'full'});h=self.headers(msg)
        texts=[];html=[];attachments=[]
        def visit(part):
            if part.get('filename'):
                attachments.append(part['filename']);return
            data=part.get('body',{}).get('data')
            if data and part.get('mimeType') in ('text/plain','text/html'):
                decoded=base64.urlsafe_b64decode(data+'='*(-len(data)%4)).decode('utf-8',errors='replace')
                (texts if part['mimeType']=='text/plain' else html).append(decoded)
            for child in part.get('parts',[]):visit(child)
        visit(msg.get('payload',{}))
        if not texts and html:
            from html.parser import HTMLParser
            class Plain(HTMLParser):
                def __init__(self):super().__init__();self.text=[];self.skip=0
                def handle_starttag(self,tag,attrs):
                    if tag in ('script','style'):self.skip+=1
                    if tag in ('p','br','div','tr'):self.text.append('\n')
                def handle_endtag(self,tag):
                    if tag in ('script','style'):self.skip=max(0,self.skip-1)
                def handle_data(self,data):
                    if not self.skip:self.text.append(data)
            parser=Plain();parser.feed('\n'.join(html));texts=[''.join(parser.text)]
        mid=h.get('message-id','');mid=mid if re.fullmatch(r'<[^\s<>]+>',mid) else None
        refs=' '.join(re.findall(r'<[^\s<>]+>',h.get('references',''))[-10:]+([mid] if mid else []))
        return {'id':msg['id'],'thread':msg.get('threadId'),'from':h.get('from',''),'to':h.get('to',''),'subject':h.get('subject',''),
            'date':h.get('date',''),'body':'\n'.join(texts)[:100000] or '(No readable text body.)','attachments':attachments,
            'replyTo':parseaddr(h.get('from',''))[1], 'messageId':mid,'references':refs}
