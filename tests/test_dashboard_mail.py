import base64
from pathlib import Path
import unittest
from unittest.mock import patch
from dashboard_mail import Mailbox

class MailboxTests(unittest.TestCase):
    def test_plain_message_threading_no_replyto_redirect(self):
        mail=Mailbox(Path('/tmp'))
        msg={'id':'abc123','threadId':'thread','payload':{'headers':[{'name':'From','value':'Customer <customer@example.com>'},{'name':'Reply-To','value':'redirect@example.com'},{'name':'Message-ID','value':'<request@example.com>'}], 'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode(b'Hello').decode()}}}
        with patch.object(mail,'request',return_value=msg):result=mail.read('abc123')
        self.assertEqual(result['body'],'Hello');self.assertEqual(result['replyTo'],'customer@example.com');self.assertEqual(result['references'],'<request@example.com>')
        with self.assertRaises(ValueError):mail.read('../../private')
    def test_html_is_text_no_scripts_and_attachment_names_only(self):
        mail=Mailbox(Path('/tmp'))
        encoded=base64.urlsafe_b64encode(b'<p>Hello</p><script>unsafe()</script>').decode()
        msg={'id':'abc','payload':{'headers':[], 'parts':[{'mimeType':'text/html','body':{'data':encoded}},{'filename':'invoice.pdf','mimeType':'application/pdf','body':{'attachmentId':'private'}}]}}
        with patch.object(mail,'request',return_value=msg):result=mail.read('abc')
        self.assertIn('Hello',result['body']);self.assertNotIn('unsafe',result['body']);self.assertEqual(result['attachments'],['invoice.pdf'])
    def test_inbox_cache(self):
        mail=Mailbox(Path('/tmp'))
        with patch.object(mail,'request',return_value={'messages':[]}) as request:
            self.assertEqual(mail.list(),[]);mail.list();request.assert_called_once();mail.list(refresh=True);self.assertEqual(request.call_count,2)

if __name__=='__main__':unittest.main()

class AliasTests(unittest.TestCase):
    def test_alias_query_and_separate_cache(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'.env').write_text('AVERY_EMAIL=marketing@example.com\nOWNER_EMAIL=ceo@example.com\n')
            mail=Mailbox(root)
            with patch.object(mail,'request',return_value={'messages':[]}) as request:
                mail.list(mailbox='Avery');self.assertIn('to:marketing@example.com',request.call_args.args[1]['q'])
                mail.list(mailbox='Owner');self.assertIn('to:ceo@example.com',request.call_args.args[1]['q'])
                mail.list(mailbox='Avery');self.assertEqual(request.call_count,2)
                with self.assertRaises(ValueError):mail.list(mailbox='unknown')

class ManualReplyTests(unittest.TestCase):
    def test_ceo_reply_sender_bcc_and_human_headers(self):
        import tempfile
        from email.parser import BytesParser
        from email import policy
        from staff_email import StaffMail, STAFF
        with tempfile.TemporaryDirectory() as d:
            addresses={name:f'{name.lower()}@example.com' for name in (*STAFF,'Owner')}
            mail=StaffMail(d,'draft',addresses,bcc='personal@example.com')
            mail.deliver('Owner',['Owner'],'Re: Question','A reply written by the CEO.',kind='manual',reply_address='customer@example.com',in_reply_to='<request@example.com>')
            saved=next(mail.outbox.glob('*.eml'));msg=BytesParser(policy=policy.default).parsebytes(saved.read_bytes())
            self.assertIn('owner@example.com',str(msg['From']))
            self.assertEqual(msg['To'],'customer@example.com');self.assertEqual(msg['Bcc'],'personal@example.com')
            self.assertEqual(msg['Auto-Submitted'],'no');self.assertEqual(msg['In-Reply-To'],'<request@example.com>')
            self.assertNotIn('Automated staff message',msg.get_content())
