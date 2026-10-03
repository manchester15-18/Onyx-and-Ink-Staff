import base64
from email.message import EmailMessage, Message
from email import policy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import json
from delivery_tracking import check_delivery
from dashboard_mail import Mailbox

class DeliveryTests(unittest.TestCase):
    def outgoing(self,root):
        folder=root/'work'/'email-outbox'/'run';folder.mkdir(parents=True)
        msg=EmailMessage();msg['Message-ID']='<outgoing@example.com>';msg['To']='customer@example.com';msg['From']='coo@example.com';msg.set_content('Hello')
        path=folder/'01-morgan.eml';path.write_bytes(msg.as_bytes());path.with_suffix('.status').write_text('accepted');return path
    def test_sent_and_matching_human_reply_are_separate_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=self.outgoing(root);mail=Mock();mail.headers=Mailbox.headers
            def request(route,params):
                if route=='threads/thread':return {'messages':[{'labelIds':['INBOX'],'payload':{'headers':[{'name':'From','value':'Customer <customer@example.com>'},{'name':'In-Reply-To','value':'<outgoing@example.com>'}]}}]}
                if params.get('q','').startswith('in:sent'):return {'messages':[{'id':'sent','threadId':'thread'}]}
                return {'messages':[]}
            mail.request.side_effect=request;check_delivery(root,mail)
            result=json.loads(path.with_suffix('.receipt.json').read_text());self.assertEqual(result['state'],'reply-received')
    def test_no_sent_record_does_not_claim_delivery(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=self.outgoing(root);mail=Mock();mail.request.return_value={'messages':[]}
            check_delivery(root,mail);result=json.loads(path.with_suffix('.receipt.json').read_text());self.assertEqual(result['state'],'not-verified')
    def test_failure_notice_matches_original_id(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);path=self.outgoing(root)
            raw=b'From: Mail Delivery <mailer-daemon@example.com>\nMIME-Version: 1.0\nContent-Type: multipart/report; boundary=x; report-type=delivery-status\n\n--x\nContent-Type: message/delivery-status\n\nOriginal-Message-ID: <outgoing@example.com>\n\nFinal-Recipient: rfc822; customer@example.com\nAction: failed\nStatus: 5.1.1\n\n--x--\n'
            mail=Mock();mail.request.side_effect=[{'messages':[{'id':'bounce'}]},{'raw':base64.urlsafe_b64encode(raw).decode()}]
            check_delivery(root,mail);result=json.loads(path.with_suffix('.receipt.json').read_text());self.assertEqual(result['state'],'failure-notice')
