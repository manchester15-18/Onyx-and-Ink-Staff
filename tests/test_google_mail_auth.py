import json
import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from google_mail_auth import GoogleMailAuth
from inbox_monitor import InboxMonitor
from staff_email import StaffMail, STAFF

class OAuthTests(unittest.TestCase):
    def test_protocol_payload_and_empty_challenge(self):
        with tempfile.TemporaryDirectory() as d:
            auth=GoogleMailAuth(d,'inbox@example.com')
            with patch.object(auth,'token',return_value='test-token'):
                smtp=Mock(); auth.smtp_login(smtp)
                name, callback=smtp.auth.call_args.args
                self.assertEqual(name,'XOAUTH2')
                smtp.ehlo_or_helo_if_needed.assert_called_once()
                self.assertEqual(smtp.mock_calls[0][0], 'ehlo_or_helo_if_needed')
                self.assertEqual(callback(),'user=inbox@example.com\x01auth=Bearer test-token\x01\x01')
                self.assertEqual(callback(b'error'),'')
                imap=Mock(); auth.imap_login(imap)
                name, callback=imap.authenticate.call_args.args
                self.assertEqual(name,'XOAUTH2')
                self.assertIn(b'auth=Bearer test-token',callback(b''))
                self.assertEqual(callback(b'error'),b'')

    def test_private_storage_and_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            auth=GoogleMailAuth(d,'inbox@example.com')
            with self.assertRaises(ValueError): auth.check()
            creds=Mock(); creds.to_json.return_value=json.dumps({'token':'test'})
            auth.save(creds)
            self.assertEqual(auth.token_path.stat().st_mode & 0o777,0o600)
            creds.valid=False; creds.refresh_token='refresh'; creds.token='new-token'
            with patch('google.oauth2.credentials.Credentials.from_authorized_user_file',return_value=creds):
                self.assertEqual(auth.token(),'new-token')
                creds.refresh.assert_called_once()
                creds.refresh.side_effect=RuntimeError('sensitive detail')
                with self.assertRaisesRegex(RuntimeError,'could not be refreshed') as error: auth.token()
                self.assertNotIn('sensitive detail',str(error.exception))

    def test_setup_reports_api_disabled_without_response_secrets(self):
        from google_mail_auth import main
        with tempfile.TemporaryDirectory() as d:
            client=Path(d)/'client.json'
            client.write_text(json.dumps({'installed':{'client_id':'test'}}))
            with patch('sys.argv',['setup','--client',str(client)]), patch.dict('os.environ',{'GOOGLE_MAIL_USER':'inbox@example.com'}), patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_config') as factory, patch('google.auth.transport.requests.AuthorizedSession') as session_factory:
                response=session_factory.return_value.__enter__.return_value.get.return_value
                response.status_code=403
                response.json.return_value={'error':{'errors':[{'reason':'accessNotConfigured','message':'secret-do-not-print'}]}}
                output=io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(main(),1)
                self.assertIn('Enable Gmail API',output.getvalue())
                self.assertNotIn('secret-do-not-print',output.getvalue())

    def test_send_uses_oauth_without_password(self):
        with tempfile.TemporaryDirectory() as d:
            auth=Mock(); auth.user='inbox@example.com'
            addresses={name:f'{name.lower()}@example.com' for name in (*STAFF,'Owner')}
            mail=StaffMail(d,'send',addresses,host='smtp.gmail.com',port=465,security='ssl',oauth=auth)
            with patch('staff_email.smtplib.SMTP_SSL') as factory:
                client=factory.return_value
                client.send_message.return_value={}
                mail.deliver('Morgan',['Owner'],'Subject','Body')
                auth.smtp_login.assert_called_once_with(client)
                client.login.assert_not_called()
            monitor=InboxMonitor(d,mail,Mock(),auth.user,'',oauth=auth)
            monitor.close()

if __name__=='__main__': unittest.main()
