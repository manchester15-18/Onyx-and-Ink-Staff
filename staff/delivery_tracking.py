"""Track evidence from Gmail without claiming acceptance proves delivery."""
import base64
import json
import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses,parseaddr

def check_delivery(root, mailbox):
    files=sorted((root/'work'/'email-outbox').glob('*/*.eml'),key=lambda p:p.stat().st_mtime,reverse=True)
    outgoing={}
    for path in files:
        if len(outgoing)>=10:break
        status=path.with_suffix('.status')
        if not status.exists():continue
        msg=BytesParser(policy=policy.default).parsebytes(path.read_bytes());mid=str(msg.get('Message-ID','')).strip()
        if re.fullmatch(r'<[^\s<>]+>',mid):outgoing[mid]=(path,msg)
    failures=set()
    if outgoing:
        notices=mailbox.request('messages',{'q':'newer_than:30d {from:mailer-daemon from:postmaster}','maxResults':20}).get('messages',[])
        for notice in notices:
            raw=mailbox.request('messages/'+notice['id'],{'format':'raw'}).get('raw','')
            data=base64.urlsafe_b64decode(raw+'='*(-len(raw)%4))
            message=BytesParser(policy=policy.default).parsebytes(data)
            ids=set();failed=False
            for part in message.walk():
                candidate=str(part.get('Original-Message-ID',part.get('Message-ID',''))).strip()
                if candidate in outgoing:ids.add(candidate)
                if part.get_content_type()=='message/delivery-status':
                    for block in part.get_payload():
                        if str(block.get('Action','')).lower()=='failed':failed=True
                        candidate=str(block.get('Original-Message-ID','')).strip()
                        if candidate in outgoing:ids.add(candidate)
            if failed:failures.update(ids)
    results=[]
    for mid,(path,msg) in outgoing.items():
        evidence={'checked':datetime.now(timezone.utc).isoformat(),'state':'not-verified','label':'Delivery not verified'}
        if mid in failures:
            evidence.update(state='failure-notice',label='Failure notice received')
        else:
            found=mailbox.request('messages',{'q':'in:sent rfc822msgid:'+mid[1:-1],'maxResults':1}).get('messages',[])
            if found:
                evidence.update(state='sent-confirmed',label='Saved in Gmail Sent')
                thread_id=found[0].get('threadId')
                if thread_id:
                    thread=mailbox.request('threads/'+thread_id,{'format':'metadata','metadataHeaders':['From','In-Reply-To','References','Auto-Submitted']})
                    recipients={address.lower() for _,address in getaddresses([str(msg.get('To',''))])}
                    for reply in thread.get('messages',[]):
                        h=mailbox.headers(reply)
                        links=re.findall(r'<[^\s<>]+>',h.get('in-reply-to','')+' '+h.get('references',''))
                        if 'SENT' not in reply.get('labelIds',[]) and mid in links and parseaddr(h.get('from',''))[1].lower() in recipients and h.get('auto-submitted','no').lower()=='no':
                            evidence.update(state='reply-received',label='Recipient reply received');break
        temporary=path.with_suffix('.receipt.tmp');temporary.write_text(json.dumps(evidence));temporary.replace(path.with_suffix('.receipt.json'))
        results.append(evidence)
    return {'checked':len(results),'message':'Checked the latest '+str(len(results))+' sent attempts. Gmail Sent confirms sending; only a recipient reply supplies response evidence. Failure notices may arrive later.'}
