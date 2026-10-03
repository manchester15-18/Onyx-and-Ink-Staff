import tempfile
import io
from email.message import Message
from unittest.mock import Mock
import unittest
from pathlib import Path
from unittest.mock import patch
from email.message import EmailMessage
import dashboard

class DashboardTests(unittest.TestCase):
    def test_inbox_query_string_serves_page(self):
        handler=dashboard.Handler.__new__(dashboard.Handler);handler.client_address=('127.0.0.1',1);handler.connection=None
        handler.headers=Message();handler.headers['Host']='127.0.0.1:8765'
        handler.path='/inbox?mailbox=Jordan';handler.reply=Mock()
        handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[0],200)
        self.assertIn(b'mailboxSelect',handler.reply.call_args.args[1])

    def test_snapshot_redacts_secrets_and_reads_delivery(self):
        with tempfile.TemporaryDirectory() as d,patch.object(dashboard,'ROOT',Path(d)):
            root=Path(d);(root/'.env').write_text('STAFF_EMAIL_MODE=send\nGROQ_API_KEY=secret-test-value\n')
            folder=root/'work'/'email-outbox'/'test';folder.mkdir(parents=True)
            msg=EmailMessage();msg['From']='Avery <marketing@example.com>';msg['To']='ceo@example.com';msg['Subject']='secret-test-value';msg.set_content('Sample body secret-test-value')
            p=folder/'01-avery.eml';p.write_bytes(msg.as_bytes());p.with_suffix('.status').write_text('delivery-unconfirmed');p.with_suffix('.diagnostic').write_text('message submission: SMTPSenderRefused (SMTP 530)')
            state=dashboard.snapshot()
            self.assertEqual(state['mode'],'send');self.assertFalse(state['monitor'])
            self.assertEqual(state['activity'][0]['status'],'delivery-unconfirmed')
            self.assertNotIn('secret-test-value',str(state))
            self.assertIn('SMTP 530',state['activity'][0]['diagnostic'])

    def test_no_secret_config_fields_exposed(self):
        with tempfile.TemporaryDirectory() as d,patch.object(dashboard,'ROOT',Path(d)):
            (Path(d)/'.env').write_text('SMTP_PASSWORD=private-password\nGOOGLE_MAIL_AUTH=oauth\n')
            state=dashboard.snapshot()
            self.assertNotIn('private-password',str(state))
            self.assertNotIn('SMTP_PASSWORD',str(state))
            self.assertEqual(len(state['agents']),4)

    def test_control_rejects_requests_without_local_token(self):
        handler=dashboard.Handler.__new__(dashboard.Handler);handler.client_address=('127.0.0.1',12345);handler.connection=None
        handler.headers=Message();handler.headers['Host']='127.0.0.1:8765'
        handler.headers['Origin']='https://outside.example'
        handler.path='/api/start';handler.reply=Mock()
        handler.do_POST()
        self.assertEqual(handler.reply.call_args.args[0],403)

    def test_mode_change_blocked_while_existing_monitor_runs(self):
        handler=dashboard.Handler.__new__(dashboard.Handler);handler.client_address=('127.0.0.1',12345);handler.connection=None
        handler.headers=Message()
        for key,value in {'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','X-Dashboard-Token':dashboard.TOKEN,'Content-Length':'16'}.items():handler.headers[key]=value
        body=b'{"mode": "send"}'
        handler.headers.replace_header('Content-Length',str(len(body)))
        handler.rfile=io.BytesIO(body);handler.path='/api/mode';handler.reply=Mock()
        with patch.object(dashboard,'monitor_active',return_value=True),patch.object(dashboard,'set_key') as update:
            handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0],409)
            update.assert_not_called()

    def test_manual_reply_retry_is_not_sent_twice(self):
        import json
        with tempfile.TemporaryDirectory() as d,patch.object(dashboard,'ROOT',Path(d)),patch.object(dashboard,'dotenv_values',return_value={'STAFF_EMAIL_MODE':'send'}),patch.object(dashboard.MAILBOX,'read',return_value={'subject':'Question','replyTo':'customer@example.com','messageId':'<request@example.com>','references':'<request@example.com>'}),patch.object(dashboard.StaffMail,'from_env') as factory:
            (Path(d)/'work').mkdir()
            mail=factory.return_value;mail.deliver.return_value='Email accepted by mail server; inbox delivery is not verified.'
            for attempt in range(2):
                body=json.dumps({'id':'abc','body':'My reply','sender':'Owner','requestId':'11111111-1111-1111-1111-111111111111'}).encode()
                handler=dashboard.Handler.__new__(dashboard.Handler);handler.client_address=('127.0.0.1',12345);handler.connection=None;handler.path='/api/reply';handler.rfile=io.BytesIO(body);handler.reply=Mock();handler.headers=Message()
                for key,value in {'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','X-Dashboard-Token':dashboard.TOKEN,'Content-Length':str(len(body))}.items():handler.headers[key]=value
                handler.do_POST();self.assertEqual(handler.reply.call_args.args[0],200)
            mail.deliver.assert_called_once()
            self.assertEqual(mail.deliver.call_args.kwargs['kind'],'manual')

    def test_compose_uses_selected_sender_and_original_subject(self):
        import json
        with tempfile.TemporaryDirectory() as d,patch.object(dashboard,'ROOT',Path(d)),patch.object(dashboard,'dotenv_values',return_value={'STAFF_EMAIL_MODE':'draft'}),patch.object(dashboard.StaffMail,'from_env') as factory:
            (Path(d)/'work').mkdir()
            body=json.dumps({'to':'customer@example.com','subject':'Christmas designs','body':'Sample draft','sender':'Avery','requestId':'22222222-2222-2222-2222-222222222222'}).encode()
            handler=dashboard.Handler.__new__(dashboard.Handler);handler.client_address=('127.0.0.1',1);handler.connection=None
            handler.path='/api/compose';handler.rfile=io.BytesIO(body);handler.reply=Mock();handler.headers=Message()
            for key,value in {'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','X-Dashboard-Token':dashboard.TOKEN,'Content-Length':str(len(body))}.items():handler.headers[key]=value
            factory.return_value.deliver.return_value='Draft saved'
            handler.do_POST();self.assertEqual(handler.reply.call_args.args[0],200)
            call=factory.return_value.deliver.call_args
            self.assertEqual(call.args[0],'Avery');self.assertEqual(call.args[2],'Christmas designs')
            self.assertEqual(call.kwargs['kind'],'compose');self.assertEqual(call.kwargs['reply_address'],'customer@example.com')

    def test_approve_sends_saved_draft_once(self):
        import json
        with tempfile.TemporaryDirectory() as d,patch.object(dashboard,'ROOT',Path(d)),patch.object(dashboard,'dotenv_values',return_value={'STAFF_EMAIL_MODE':'draft'}),patch.object(dashboard.StaffMail,'from_env') as factory:
            folder=Path(d)/'work'/'email-outbox'/'run';folder.mkdir(parents=True)
            msg=EmailMessage();msg['From']='Avery | Onyx and Ink <avery@example.com>';msg['To']='customer@example.com';msg['Subject']='Hello';msg.set_content('Draft body')
            path=folder/'01-avery.eml';path.write_bytes(msg.as_bytes())
            identifier=str(path.relative_to(d))
            mail=factory.return_value;mail.send_saved.return_value='Email accepted by mail server; inbox delivery is not verified.'
            body=json.dumps({'id':identifier}).encode()
            handler=dashboard.Handler.__new__(dashboard.Handler);handler.client_address=('127.0.0.1',1);handler.connection=None
            handler.path='/api/approve';handler.rfile=io.BytesIO(body);handler.reply=Mock();handler.headers=Message()
            for key,value in {'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','X-Dashboard-Token':dashboard.TOKEN,'Content-Length':str(len(body))}.items():handler.headers[key]=value
            handler.do_POST();self.assertEqual(handler.reply.call_args.args[0],200)
            mail.send_saved.assert_called_once()
            self.assertEqual(factory.call_args.kwargs.get('mode'),'send')
            path.with_suffix('.status').write_text('accepted\n')
            handler.rfile=io.BytesIO(body);handler.reply=Mock();handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0],409)
            self.assertEqual(mail.send_saved.call_count,1)

    def test_dismiss_leaves_file_and_rejects_path_escape(self):
        import json
        with tempfile.TemporaryDirectory() as d,patch.object(dashboard,'ROOT',Path(d)):
            folder=Path(d)/'work'/'email-outbox'/'run';folder.mkdir(parents=True)
            path=folder/'01-avery.eml';path.write_bytes(b'From: a\nTo: b\nSubject: s\n\nbody\n')
            path.with_suffix('.status').write_text('delivery-unconfirmed\n')
            identifier=str(path.relative_to(d))
            body=json.dumps({'ids':[identifier]}).encode()
            handler=dashboard.Handler.__new__(dashboard.Handler);handler.client_address=('127.0.0.1',1);handler.connection=None
            handler.path='/api/dismiss';handler.rfile=io.BytesIO(body);handler.reply=Mock();handler.headers=Message()
            for key,value in {'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','X-Dashboard-Token':dashboard.TOKEN,'Content-Length':str(len(body))}.items():handler.headers[key]=value
            handler.do_POST();self.assertEqual(handler.reply.call_args.args[0],200)
            self.assertEqual(path.with_suffix('.status').read_text().strip(),'dismissed')
            self.assertTrue(path.exists())
            escaped=json.dumps({'ids':['../.env']}).encode()
            handler.rfile=io.BytesIO(escaped);handler.headers.replace_header('Content-Length',str(len(escaped)));handler.reply=Mock();handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0],400)

if __name__=='__main__':unittest.main()
