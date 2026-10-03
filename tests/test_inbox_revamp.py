import base64
import io
import json
import tempfile
import time
import unittest
from email.message import Message
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock,patch
from dashboard_mail import Mailbox
from inbox_state import InboxState
from agent_actions import Actions
from staff_email import StaffMail,STAFF
import dashboard
import github_sync

class InboxRevampTests(unittest.TestCase):
 def test_cleanup_hide_restore_never_mutates_gmail(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);state=InboxState(root);mail=Mailbox(root)
   items=[{'id':'abc1','received':int((time.time()-40*86400)*1000)},{'id':'abc2','received':int(time.time()*1000)}]
   with patch.object(mail,'list',return_value=items),patch.object(mail,'request') as request:
    self.assertEqual([m['id'] for m in mail.page()['messages']],['abc2'])
    self.assertEqual([m['id'] for m in mail.page(view='hidden')['messages']],['abc1'])
    state.hide('abc1',False);self.assertEqual(len(mail.page()['messages']),2)
    state.hide('abc2');self.assertEqual([m['id'] for m in mail.page()['messages']],['abc1'])
    state.configure(0);request.assert_not_called()
 def test_explicit_delete_calls_trash_not_permanent_delete(self):
  with tempfile.TemporaryDirectory() as d:
   mail=Mailbox(Path(d));mail.cache['old']=['cached']
   with patch.object(mail,'request',return_value={'labelIds':['TRASH']}) as request:
    mail.trash('abcd1234');request.assert_called_once_with('messages/abcd1234/trash',method='POST')
    self.assertFalse(mail.cache)
   with self.assertRaises(ValueError):mail.trash('../private')
 def test_attachment_download_resolves_part_and_decodes_bytes(self):
  with tempfile.TemporaryDirectory() as d:
   mail=Mailbox(Path(d));msg={'payload':{'parts':[{'partId':'1','filename':'invoice.pdf','body':{'size':4,'attachmentId':'attach_123'}}]}}
   with patch.object(mail,'request',side_effect=[msg,{'data':base64.urlsafe_b64encode(b'test').decode()}]) as request:
    self.assertEqual(mail.attachment('abc','1'),('invoice.pdf',b'test'))
    self.assertEqual(request.call_args.args[0],'messages/abc/attachments/attach_123')
 def test_uploaded_attachment_is_confined_and_preserves_filename(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);state=InboxState(root);upload=state.upload('../../holiday.txt',base64.b64encode(b'Christmas').decode())
   paths=state.attachment_paths([upload['id']]);self.assertTrue(paths[0].is_relative_to((root/'work/uploads').resolve()))
   addresses={n:n.lower()+'@example.com' for n in (*STAFF,'Owner')};mail=StaffMail(root,mode='draft',addresses=addresses)
   mail.deliver('Avery',['Owner'],'Design','Attached',attachments=paths)
   message=BytesParser(policy=policy.default).parsebytes(next(mail.outbox.glob('*.eml')).read_bytes())
   attached=list(message.iter_attachments())[0];self.assertEqual(attached.get_filename(),'holiday.txt');self.assertEqual(attached.get_payload(decode=True),b'Christmas')
   with self.assertRaises(ValueError):state.attachment_paths(['../.env'])
 def test_agent_delete_requires_exact_human_authorization_and_matching_inbox(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'.env').write_text('AVERY_EMAIL=marketing@example.com\n')
   with patch('dashboard_mail.Mailbox.read',return_value={'to':'marketing@example.com'}),patch('dashboard_mail.Mailbox.trash',return_value={'message':'Moved to Trash'}) as trash:
    a=Actions(root,'Avery','no-command');self.assertIn('error',a.execute('delete_email',{'id':'abc123'}));trash.assert_not_called()
    a=Actions(root,'Avery','command',delete_ids=['abc123']);self.assertIn('message',a.execute('delete_email',{'id':'abc123'}));trash.assert_called_once()
 def test_delete_endpoint_requires_confirmation(self):
  body=json.dumps({'id':'abc123','confirm':False}).encode();handler=dashboard.Handler.__new__(dashboard.Handler)
  handler.client_address=('127.0.0.1',123);handler.connection=None;handler.path='/api/delete-email';handler.rfile=io.BytesIO(body);handler.reply=Mock();handler.headers=Message()
  for k,v in {'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','X-Dashboard-Token':dashboard.TOKEN,'Content-Length':str(len(body))}.items():handler.headers[k]=v
  with patch.object(dashboard.MAILBOX,'trash') as trash:
   handler.do_POST();self.assertEqual(handler.reply.call_args.args[0],400);trash.assert_not_called()
 def test_changelog_records_updates_once_without_source_contents(self):
  with tempfile.TemporaryDirectory() as d,patch.object(github_sync,'ROOT',Path(d)):
   root=Path(d);(root/'main.py').write_text('private implementation contents')
   github_sync.update_changelog(['main.py']);first=(root/'CHANGELOG.md').read_text();github_sync.update_changelog(['main.py'])
   self.assertEqual(first,(root/'CHANGELOG.md').read_text());self.assertIn('main.py',first);self.assertNotIn('private implementation contents',first)
   (root/'main.py').write_text('updated implementation');github_sync.update_changelog(['main.py'])
   self.assertEqual((root/'CHANGELOG.md').read_text().count('— Application update'),2)
if __name__=='__main__':unittest.main()
