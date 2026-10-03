import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from email import policy
from email.parser import BytesParser
from staff_email import StaffMail, STAFF,default_signatures,save_signature_settings,signature_settings,reset_signature_settings

ADDRESSES={name:f'{name.lower()}@example.com' for name in (*STAFF,'Owner')}


class StaffMailTests(unittest.TestCase):
    def test_signature_settings_save_and_restore_privately(self):
        with tempfile.TemporaryDirectory() as directory:
            custom={name:name+' custom signature' for name in STAFF}
            save_signature_settings(directory,custom);path=Path(directory)/'work/email-signatures.json'
            self.assertEqual(path.stat().st_mode & 0o777,0o600);self.assertEqual(signature_settings(directory,ADDRESSES),custom)
            restored=reset_signature_settings(directory,ADDRESSES);self.assertEqual(restored,default_signatures(ADDRESSES));self.assertFalse(path.exists())
            with self.assertRaises(ValueError):save_signature_settings(directory,{'Morgan':'missing others'})

    def test_environment_builds_official_agent_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            config={'STAFF_EMAIL_MODE':'draft','MORGAN_EMAIL':'coo@example.com','AVERY_EMAIL':'marketing@example.com','JORDAN_EMAIL':'it@example.com','CAMERON_EMAIL':'hr@example.com','OWNER_EMAIL':'ceo@example.com','OWNER_BCC_EMAIL':'ceo@example.com','BUSINESS_WEBSITE':'https://onyxandink.org'}
            mail=StaffMail.from_env(directory,config=config);mail.deliver('Jordan',['Owner'],'Design update','The design is ready for review.')
            msg=BytesParser(policy=policy.default).parsebytes(next(mail.outbox.glob('*.eml')).read_bytes())
            body=msg.get_content()
            self.assertIn('Jordan\nIT & Storefront Development Lead\nOnyx & Ink',body)
            self.assertIn('it@example.com | https://onyxandink.org',body)

    def test_draft_has_identity_and_never_connects(self):
        with tempfile.TemporaryDirectory() as directory, patch('smtplib.SMTP') as smtp:
            mail=StaffMail(directory,'draft',ADDRESSES)
            mail.report_callback('Avery')(SimpleNamespace(raw='Campaign report'))
            mail.finish()
            files=list(mail.outbox.glob('*.eml'))
            self.assertEqual(len(files),2)
            msg=BytesParser(policy=policy.default).parsebytes(files[0].read_bytes())
            self.assertIn('avery@example.com',str(msg['From']))
            self.assertIn('jordan@example.com',str(msg['To']))
            self.assertIn('morgan@example.com',str(msg['To']))
            self.assertIn('Campaign report',msg.get_content())
            smtp.assert_not_called()

    def test_callbacks_do_not_duplicate_and_coo_copies_team(self):
        with tempfile.TemporaryDirectory() as directory:
            mail=StaffMail(directory,'draft',ADDRESSES)
            callback=mail.report_callback('Morgan')
            callback(SimpleNamespace(raw='Plan')); callback(SimpleNamespace(raw='Plan'))
            mail.finish(); mail.finish()
            self.assertEqual(mail.count,1)
            msg=BytesParser(policy=policy.default).parsebytes(next(mail.outbox.glob('*.eml')).read_bytes())
            for name in ['Owner','Avery','Jordan','Cameron']:
                self.assertIn(ADDRESSES[name],str(msg['To']))

    def test_rejects_external_recipient_injection_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            mail=StaffMail(directory,'draft',ADDRESSES,limit=1)
            with self.assertRaises(ValueError): mail.deliver('Avery',['outside@example.com'],'Hello','Text')
            with self.assertRaises(ValueError): mail.deliver('Avery',['Owner'],'Hello\nBcc: other@example.com','Text')
            mail.deliver('Avery',['Owner'],'Hello','Text')
            self.assertIn('limit reached',mail.deliver('Avery',['Owner'],'Again','Text'))
            self.assertEqual(len(list(mail.outbox.glob('*.eml'))),1)

    def test_tls_auth_and_authorized_envelope(self):
        with tempfile.TemporaryDirectory() as directory, patch('smtplib.SMTP') as smtp:
            client=MagicMock(); client.send_message.return_value={}
            smtp.return_value=client
            client.__enter__.return_value=client
            creds={name:('account@example.com','local-test-password') for name in STAFF}
            mail=StaffMail(directory,'send',ADDRESSES,'smtp.example.com',587,'starttls',creds,bcc='private@example.com')
            result=mail.deliver('Jordan',['Morgan'],'Question','Preview dimensions?')
            client.starttls.assert_called_once()
            client.login.assert_called_once_with('account@example.com','local-test-password')
            self.assertEqual(client.send_message.call_args.kwargs['from_addr'],'jordan@example.com')
            self.assertEqual(client.send_message.call_args.kwargs['to_addrs'],['morgan@example.com','private@example.com'])
            self.assertIn('accepted',result)
            self.assertEqual(next(mail.outbox.glob('*.status')).read_text().strip(),'accepted')

    def test_send_failure_is_not_retried_or_exposed(self):
        with tempfile.TemporaryDirectory() as directory, patch('smtplib.SMTP',side_effect=OSError('sensitive diagnostic')) as smtp:
            mail=StaffMail(directory,'send',ADDRESSES,'smtp.example.com',587,'starttls',{name:('u','p') for name in STAFF})
            result=mail.deliver('Morgan',['Owner'],'Failure','Fixed explanation')
            smtp.assert_called_once()
            self.assertNotIn('sensitive',result)
            self.assertIn('unconfirmed',result)
            self.assertEqual(len(list(mail.outbox.glob('*.eml'))),1)

    def test_send_saved_transmits_draft_and_refuses_unconfirmed(self):
        with tempfile.TemporaryDirectory() as directory, patch('smtplib.SMTP') as smtp:
            client=MagicMock(); client.send_message.return_value={}
            smtp.return_value=client; client.__enter__.return_value=client
            creds={name:('account@example.com','local-test-password') for name in STAFF}
            draft=StaffMail(directory,'draft',{**ADDRESSES,'Shared':'shared@example.com'},bcc='private@example.com')
            draft.deliver('Avery',['Owner'],'Hello','Saved draft',kind='compose',reply_address='customer@example.com')
            path=next(draft.outbox.glob('*.eml'))
            live=StaffMail(directory,'send',{**ADDRESSES,'Shared':'shared@example.com'},'smtp.example.com',587,'starttls',creds,bcc='private@example.com')
            result=live.send_saved(path)
            self.assertIn('accepted',result)
            client.send_message.assert_called_once()
            self.assertEqual(path.with_suffix('.status').read_text().strip(),'accepted')
            self.assertIn('not a draft',live.send_saved(path))
            self.assertEqual(client.send_message.call_count,1)

    def test_concurrent_approval_cannot_submit_twice(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            mail=StaffMail(directory,'draft',ADDRESSES)
            mail.deliver('Avery',['Owner'],'Hello','Draft')
            path=next(mail.outbox.glob('*.eml'))
            with path.with_suffix('.send-lock').open('a') as lock,patch.object(mail,'_submit') as submit:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertIn('already being submitted',mail.send_saved(path))
                submit.assert_not_called()

    def test_bad_configuration_and_off_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError): StaffMail(directory,'send',ADDRESSES)
            with self.assertRaises(ValueError): StaffMail(directory,'draft',{})
            mail=StaffMail(directory)
            self.assertIn('disabled',mail.deliver('Morgan',['Owner'],'Hi','Text'))
            self.assertFalse(mail.outbox.exists())


if __name__=='__main__': unittest.main()
