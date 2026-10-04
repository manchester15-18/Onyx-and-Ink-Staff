from email.message import EmailMessage
from email import policy
from email.parser import Parser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from staff_email import StaffMail, STAFF
from inbox_monitor import InboxMonitor, message_route, plain_body, owner_reply_context, continue_owner_reply

ADDRESSES={n:f'{n.lower()}@example.com' for n in (*STAFF,'Owner')}


def email(sender='owner@example.com',to='avery@example.com',kind=None):
    msg=EmailMessage(); msg['From']=sender; msg['To']=to; msg['Subject']='Christmas designs'
    msg['Message-ID']='<request-1@example.com>'
    msg['Authentication-Results']='mx.google.com; dmarc=pass header.from=example.com'
    if kind:
        msg['Auto-Submitted']='auto-replied' if kind=='reply' else 'auto-generated'
        msg['X-Onyx-Ink-Kind']=kind
    msg.set_content('Please suggest three Christmas tumbler designs.')
    return msg


class FakeIMAP:
    messages={}
    def __init__(self,*a,**k): pass
    def login(self,*a): return 'OK',[]
    def select(self,*a,**k): return 'OK',[]
    def response(self,*a): return 'UIDVALIDITY',[b'123']
    def uid(self,operation,*args):
        if operation=='search': return 'OK',[b' '.join(str(uid).encode() for uid in sorted(self.messages))]
        assert args[1]=='(BODY.PEEK[])'
        return 'OK',[(b'metadata',self.messages[int(args[0])])]
    def logout(self): pass


class InboxTests(unittest.TestCase):
    def test_routing_authentication_and_loop_exclusions(self):
        with tempfile.TemporaryDirectory() as d:
            mail=StaffMail(d,'draft',ADDRESSES,bcc='owner@gmail.com')
            self.assertEqual(message_route(email(),mail),('Avery','Owner'))
            self.assertEqual(message_route(email('avery@example.com','jordan@example.com, morgan@example.com','handoff'),mail),('Jordan','Avery'))
            self.assertIsNone(message_route(email(kind='reply'),mail))
            self.assertIsNone(message_route(email(kind='report'),mail))
            self.assertEqual(message_route(email('stranger@example.com'),mail),('Avery','stranger@example.com'))
            self.assertIsNone(message_route(email(to='outside@example.com'),mail))
            forged=email(); del forged['Authentication-Results']
            self.assertIsNone(message_route(forged,mail))
            self.assertIn('Christmas',plain_body(email()))

    def test_baseline_new_message_restart_duplicate_and_threading(self):
        FakeIMAP.messages={1:email().as_bytes()}
        with tempfile.TemporaryDirectory() as d,patch('imaplib.IMAP4_SSL',FakeIMAP):
            mail=StaffMail(d,'draft',ADDRESSES)
            generated=[]
            def reply(*args): generated.append(args); return 'Here are three Christmas concepts.'
            monitor=InboxMonitor(d,mail,reply,'login@example.com','local-test-password')
            self.assertEqual(monitor.poll(),0)
            new=email(); new.replace_header('Message-ID','<new@example.com>')
            FakeIMAP.messages[2]=new.as_bytes()
            self.assertEqual(monitor.poll(),1)
            self.assertEqual(monitor.poll(),0)
            monitor.close()
            monitor=InboxMonitor(d,mail,reply,'login@example.com','local-test-password')
            FakeIMAP.messages[3]=new.as_bytes()
            self.assertEqual(monitor.poll(),0)
            monitor.close()
            self.assertEqual(len(generated),1)
            contents=next(mail.outbox.glob('*.eml')).read_text()
            self.assertIn('In-Reply-To: <new@example.com>',contents)
            self.assertIn('References: <new@example.com>',contents)
            self.assertIn('Auto-Submitted: auto-replied',contents)
            self.assertIn('avery@example.com',contents)

    def test_external_reply_copies_owner_and_preserves_thread(self):
        FakeIMAP.messages={}
        with tempfile.TemporaryDirectory() as d,patch('imaplib.IMAP4_SSL',FakeIMAP):
            mail=StaffMail(d,'draft',ADDRESSES,bcc='owner@gmail.com')
            monitor=InboxMonitor(d,mail,lambda *args: self.fail('Outside email must not call the model'),'login@example.com','local-password')
            try:
                monitor.poll()
                incoming=email('customer@example.com')
                incoming['Reply-To']='redirect@outside.com'
                FakeIMAP.messages[1]=incoming.as_bytes()
                self.assertEqual(monitor.poll(),1)
                contents=(mail.outbox/'02-avery.eml').read_text()
                forward=(mail.outbox/'01-avery.eml').read_text()
                self.assertIn('message/rfc822',forward)
                self.assertIn('To: Owner <owner@example.com>',forward)
                parsed=Parser(policy=policy.default).parsestr(contents)
                self.assertIn('forwarded to our CEO',parsed.get_body(preferencelist=('plain',)).get_content())
                self.assertIn('To: customer@example.com',contents)
                self.assertIn('Bcc: owner@gmail.com',contents)
                self.assertIn('In-Reply-To: <request-1@example.com>',contents)
                self.assertNotIn('redirect@outside.com',contents)
            finally:
                monitor.close()

    def test_failed_generation_reserved_and_single_monitor(self):
        FakeIMAP.messages={}
        with tempfile.TemporaryDirectory() as d,patch('imaplib.IMAP4_SSL',FakeIMAP):
            mail=StaffMail(d,'draft',ADDRESSES)
            def fail(*args): raise RuntimeError('do not print secret')
            monitor=InboxMonitor(d,mail,fail,'login@example.com','local-password')
            with self.assertRaises(ValueError): InboxMonitor(d,mail,fail,'login@example.com','local-password')
            monitor.poll(); FakeIMAP.messages[1]=email().as_bytes()
            self.assertEqual(monitor.poll(),0)
            self.assertEqual(monitor.poll(),0)
            self.assertEqual(monitor.db.execute('SELECT state FROM processed').fetchone()[0],'needs-review')
            monitor.close()
            self.assertFalse(mail.outbox.exists())

    def test_owner_reply_becomes_agent_continuation_with_thread_context(self):
        message=email();message.set_content('Yes, proceed with the design.\n\nOn Sunday, Avery wrote:\n> Should I create the Christmas tumbler design?')
        prompt=owner_reply_context(message,message.get_content())
        self.assertIn('CEO NEW REPLY:\nYes, proceed',prompt)
        self.assertIn('PRIOR THREAD CONTEXT',prompt)
        chat=Mock();chat.ask.return_value='Design created and uploaded.'
        result=continue_owner_reply(chat,'Avery',message,message.get_content())
        self.assertEqual(result,'Design created and uploaded.')
        call=chat.ask.call_args.args
        self.assertEqual(call[0],'Avery');self.assertEqual(len(call[2]),36)
        self.assertEqual(chat.ask.call_args.kwargs['source'],'email')


if __name__=='__main__': unittest.main()
