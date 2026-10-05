"""Gmail inbox access for the local dashboard; no model calls."""
import base64
import re
import threading
import time
from email.utils import parseaddr
from dotenv import dotenv_values
import requests
from google_mail_auth import GoogleMailAuth
from inbox_state import InboxState

class Mailbox:
    def __init__(self, root):
        self.root=root; self.lock=threading.Lock();self.cache={};self.cached_at={};self.next_pages={}
    def request(self, path, params=None, method="GET"):
        if not re.fullmatch(r"(?:messages(?:/[a-fA-F0-9]+(?:/(?:trash|untrash|modify)|/attachments/[A-Za-z0-9_-]+)?)?|threads/[A-Za-z0-9_-]+)",path):raise ValueError("Invalid Gmail request.")
        config=dotenv_values(self.root/'.env')
        auth=GoogleMailAuth(self.root,config.get('GOOGLE_MAIL_USER',''))
        request=requests.get if method=='GET' else requests.post
        response=request('https://gmail.googleapis.com/gmail/v1/users/me/'+path,
            params=params,headers={'Authorization':'Bearer '+auth.token()},timeout=30)
        if response.status_code not in (200,204):
            raise RuntimeError('Google inbox unavailable. Check authorization and Gmail API access.')
        return response.json() if response.content else {}
    @staticmethod
    def headers(message):
        return {h['name'].lower():h['value'] for h in message.get('payload',{}).get('headers',[])}
    def list(self, refresh=False, mailbox="all", page_token=None):
        with self.lock:
            config=dotenv_values(self.root/'.env')
            names={'Owner':'OWNER_EMAIL','Jaunee':'IMPORTANT_CC_EMAIL','Morgan':'MORGAN_EMAIL','Avery':'AVERY_EMAIL','Jordan':'JORDAN_EMAIL','Cameron':'CAMERON_EMAIL','Shared':'GOOGLE_MAIL_USER'}
            if mailbox != 'all' and mailbox not in names:raise ValueError('Unknown inbox.')
            # Named mailboxes are virtual views over aliases that share one Google
            # account. Gmail often stores alias-to-alias mail only in Sent, so an
            # INBOX-only query silently loses internal messages.
            params={'maxResults':40}
            if mailbox in ('all','Shared'):
                params['labelIds']='INBOX'
            if page_token:
                if not isinstance(page_token,str) or len(page_token)>500 or not re.fullmatch('[A-Za-z0-9_-]+',page_token):raise ValueError('Invalid inbox page.')
                params['pageToken']=page_token
            if mailbox not in ('all','Shared'):
                from staff_email import valid_address
                address=config.get(names[mailbox],'')
                if not valid_address(address):raise ValueError('Inbox address is not configured.')
                params['q']='{in:inbox in:sent} {to:'+address+' cc:'+address+'}'
            cache_key=(mailbox,page_token)
            if not refresh and cache_key in self.cache and time.monotonic()-self.cached_at.get(cache_key,0)<60:self.next_page=self.next_pages.get(cache_key);return self.cache[cache_key]
            page=self.request('messages',params)
            entries=page.get('messages',[])
            self.next_page=page.get('nextPageToken');self.next_pages[cache_key]=self.next_page
            def metadata(entry):
                msg=self.request('messages/'+entry['id'],{'format':'metadata','metadataHeaders':['From','To','Subject','Date']})
                h=self.headers(msg)
                return {'id':msg['id'],'from':h.get('from',''),'to':h.get('to',''),'subject':h.get('subject','(No subject)'),
                    'date':h.get('date',''),'received':int(msg.get('internalDate','0')),'snippet':__import__('html').unescape(msg.get('snippet',''))[:180],
                    'unread':'UNREAD' in msg.get('labelIds',[]),'sent':'SENT' in msg.get('labelIds',[])}
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=6) as pool:result=list(pool.map(metadata,entries))
            self.cache[cache_key]=result;self.cached_at[cache_key]=time.monotonic();return result
    def read(self, identifier):
        if not re.fullmatch('[a-fA-F0-9]+',identifier):raise ValueError('Invalid message.')
        msg=self.request('messages/'+identifier,{'format':'full'});h=self.headers(msg)
        texts=[];html=[];attachments=[];attachment_details=[]
        def visit(part):
            if part.get('filename'):
                attachments.append(part['filename']);attachment_details.append({'part':part.get('partId',''),'name':part['filename'],'size':part.get('body',{}).get('size',0)});return
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
        reply_to=parseaddr(h.get('from',''))[1]
        labels=set(msg.get('labelIds',[]));config=dotenv_values(self.root/'.env')
        internal_addresses={str(config.get(key,'')).lower() for key in ('OWNER_EMAIL','IMPORTANT_CC_EMAIL','MORGAN_EMAIL','AVERY_EMAIL','JORDAN_EMAIL','CAMERON_EMAIL')}
        requested_reply=parseaddr(h.get('reply-to',''))[1]
        if 'SENT' in labels and h.get('x-onyx-ink-kind') in ('message','handoff','failure') and requested_reply.lower() in internal_addresses:
            reply_to=requested_reply
        return {'id':msg['id'],'thread':msg.get('threadId'),'from':h.get('from',''),'to':h.get('to',''),'subject':h.get('subject',''),
            'cc':h.get('cc',''),'date':h.get('date',''),'body':'\n'.join(texts)[:100000] or '(No readable text body.)','attachments':attachments,'attachmentDetails':attachment_details,
            'replyTo':reply_to, 'messageId':mid,'references':refs,'sent':'SENT' in labels}

    def page(self,mailbox='all',refresh=False,page_token=None,view='visible'):
        if view not in ('visible','hidden','all'):raise ValueError('Invalid dashboard inbox view.')
        items=self.list(refresh=refresh,mailbox=mailbox,page_token=page_token)
        state=InboxState(self.root);hidden=state.hidden();visible=state.visible();days=state.days()
        cutoff=(time.time()-days*86400)*1000 if days else 0
        result=[]
        for item in items:
            local_hidden=item['id'] in hidden or bool(item['id'] not in visible and cutoff and item.get('received',0) and item['received']<cutoff)
            item=dict(item,dashboardHidden=local_hidden)
            if view=='all' or view=='hidden' and local_hidden or view=='visible' and not local_hidden:result.append(item)
        return {'messages':result,'nextPage':self.next_pages.get((mailbox,page_token)),'cleanupDays':days,'view':view}
    def trash(self,identifier):
        if not re.fullmatch('[a-fA-F0-9]+',identifier):raise ValueError('Invalid message.')
        result=self.request('messages/'+identifier+'/trash',method='POST')
        if 'TRASH' not in result.get('labelIds',[]):raise RuntimeError('Gmail did not confirm deletion. Refresh before retrying.')
        self.cache.clear();return {'message':'Email moved to Gmail Trash and removed from the actual inbox.'}
    def attachment(self,identifier,part_id):
        if not re.fullmatch('[a-fA-F0-9]+',identifier) or not re.fullmatch('[0-9.]{1,30}',part_id):raise ValueError('Invalid attachment.')
        message=self.request('messages/'+identifier,{'format':'full'})
        def find(part):
            if part.get('partId')==part_id and part.get('filename'):return part
            for child in part.get('parts',[]):
                result=find(child)
                if result:return result
        part=find(message.get('payload',{}))
        if not part or part.get('body',{}).get('size',0)>15_000_000:raise ValueError('Attachment unavailable or larger than 15 MB.')
        body=part.get('body',{})
        if body.get('attachmentId'):body=self.request('messages/'+identifier+'/attachments/'+body['attachmentId'])
        encoded=body.get('data','')
        if len(encoded)>21_000_000:raise ValueError('Attachment exceeds download limit.')
        data=base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4))
        if len(data)>15_000_000:raise ValueError('Attachment exceeds download limit.')
        return part['filename'],data
