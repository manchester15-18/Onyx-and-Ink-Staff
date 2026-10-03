import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from email import policy
from email.parser import BytesParser
from staff_email import StaffMail, STAFF

ADDRESSES={name:f'{name.lower()}@example.com' for name in (*STAFF,'Owner')}


class StaffMailTests(unittest.TestCase):
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
