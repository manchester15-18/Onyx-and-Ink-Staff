import base64
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import ANY,Mock,patch
from PIL import Image
from agent_actions import Actions
from agent_chat import AgentChat
from design_images import generate

CFG='''STAFF_EMAIL_MODE=send
MORGAN_EMAIL=coo@example.com
AVERY_EMAIL=marketing@example.com
JORDAN_EMAIL=it@example.com
CAMERON_EMAIL=hr@example.com
OWNER_EMAIL=ceo@example.com
OWNER_BCC_EMAIL=owner@example.com
'''
class ActionTests(unittest.TestCase):
 def test_email_is_approval_only_even_with_live_mode_and_attachment(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'.env').write_text(CFG);a=Actions(root,'Avery','run')
   file=a.execute('report',{'title':'Plans','text':'Christmas plans'})
   with patch('staff_email.StaffMail._submit') as send:
    args={'to':'outside@example.com','subject':'Plans','body':'Please review','attachments':[file['file_id']]}
    result=a.execute('email',args);self.assertIn('approval',result);send.assert_not_called()
    self.assertEqual(a.execute('email',args),result)
   files=list((root/'work/email-outbox').glob('*/*.eml'));self.assertEqual(len(files),1)
   from email import policy
   from email.parser import BytesParser
   message=BytesParser(policy=policy.default).parsebytes(files[0].read_bytes())
   self.assertEqual(list(message.iter_attachments())[0].get_payload(decode=True),b'Christmas plans')
   self.assertIn(b'marketing@example.com',files[0].read_bytes())
   with self.assertRaises(ValueError):a.file('../.env')
 def test_reserved_actions_do_not_repeat_external_write(self):
  with tempfile.TemporaryDirectory() as d:
   a=Actions(Path(d),'Jordan','one');a.workspace.create_doc=Mock(side_effect=RuntimeError('Authorization required.'))
   args={'title':'Test','text':'Body'}
   self.assertIn('error',a.execute('document',args));a.execute('document',args)
   a.workspace.create_doc.assert_called_once()
 def test_chat_executes_and_shares_real_file_receipt(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'.env').write_text('AGENT_TOOLS_ENABLED=true\n')
   chat=AgentChat(root);chat.llm=Mock();chat.llm.call.side_effect=[json.dumps({'action':'report','arguments':{'title':'Test report','text':'Known facts only.'}}),json.dumps({'answer':'Report created.'})]
   rid=str(uuid.uuid4());answer=chat.ask('Morgan','Create a test report',rid)
   self.assertIn('/api/artifact?id=',answer);self.assertEqual(len(Actions(root,'Morgan','check').files()),1)
   self.assertEqual(len(list((root/'reports/custom-gift-push/agent-drafts/morgan').glob('*.md'))),1)
   chat.ask('Morgan','Retry',rid);self.assertEqual(chat.llm.call.call_count,2)
 def test_sheet_values_and_calendar_are_validated_before_write(self):
  with tempfile.TemporaryDirectory() as d:
   a=Actions(Path(d),'Cameron','test');a.workspace.create_sheet=Mock();a.workspace.create_event=Mock()
   self.assertIn('error',a.execute('spreadsheet',{'title':'Bad','values':[{'bad':1}]}))
   self.assertIn('error',a.execute('calendar_event',{'summary':'Test','start':'2026-10-03T10:00:00','end':'2026-10-03T11:00:00'}))
   a.workspace.create_event.assert_not_called();a.workspace.create_sheet.assert_not_called()
 def test_cloudflare_no_retry_and_local_daily_cap(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'work').mkdir();(root/'.env').write_text('CLOUDFLARE_FREE_PLAN_CONFIRMED=true\nCLOUDFLARE_ACCOUNT_ID='+('a'*32)+'\nCLOUDFLARE_API_TOKEN=private-test-token\n')
   output=io.BytesIO();Image.new('RGB',(20,20)).save(output,'JPEG')
   response=Mock(ok=True,headers={'Content-Type':'application/json'});response.json.return_value={'success':True,'result':{'image':base64.b64encode(output.getvalue()).decode()}}
   with patch('design_images.requests.post',return_value=response) as post:
    self.assertTrue(generate(root,'Test').startswith(b'\x89PNG'))
    self.assertEqual(post.call_args.kwargs['json'],{'prompt':'Test','steps':4})
    import sqlite3
    with sqlite3.connect(root/'work/image-usage.sqlite3') as db:db.execute('UPDATE usage SET count=50')
    with self.assertRaises(RuntimeError):generate(root,'Test')
    self.assertEqual(post.call_count,1)
 def test_klein_reference_uses_bounded_multipart_image(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'work').mkdir();(root/'.env').write_text('CLOUDFLARE_FREE_PLAN_CONFIRMED=true\nCLOUDFLARE_ACCOUNT_ID='+('a'*32)+'\nCLOUDFLARE_API_TOKEN=private-test-token\n')
   reference=root/'source.png';Image.new('RGB',(1024,768)).save(reference)
   out=io.BytesIO();Image.new('RGB',(20,20)).save(out,'PNG')
   r=Mock(ok=True,headers={'Content-Type':'image/png'},content=out.getvalue())
   with patch('design_images.requests.post',return_value=r) as post:
    generate(root,'Make a variation','klein',reference=reference)
    files=post.call_args.kwargs['files'];self.assertIn('input_image_0',files)
    with Image.open(io.BytesIO(files['input_image_0'][1])) as image:self.assertLess(max(image.size),512)
    self.assertNotIn('json',post.call_args.kwargs)
 def test_read_email_does_not_expose_other_agent_mail(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'.env').write_text(CFG);a=Actions(root,'Avery','mail')
   with patch('dashboard_mail.Mailbox.read',return_value={'to':'it@example.com','body':'private other agent email'}):
    result=a.execute('read_email',{'id':'abc'})
    self.assertIn('error',result);self.assertNotIn('private other agent email',str(result))
 def test_missing_provider_has_no_network_fallback(self):
  with tempfile.TemporaryDirectory() as d,patch('design_images.requests.post') as post:
   with self.assertRaises(RuntimeError):generate(Path(d),'Test')
   post.assert_not_called()
 def test_agents_can_read_saved_dashboard_reports(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);folder=root/'reports/custom-gift-push/assignments/one';folder.mkdir(parents=True);(folder/'marketing_campaign.md').write_text('# Marketing\nUse the approved holiday collection.')
   a=Actions(root,'Jordan','reports')
   result=a.execute('staff_reports',{'name':'marketing'})
   self.assertEqual(result['dashboard'],'/reports');self.assertIn('approved holiday collection',result['reports']['marketing'])
   self.assertIn('error',a.execute('staff_reports',{'name':'private'}))
 def test_staff_message_routes_a_targeted_bot_handoff_to_the_staff_group(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'.env').write_text('TELEGRAM_AGENT_GROUP_CHAT=-1004450996238\nTELEGRAM_MORGAN_BOT_TOKEN=private-test-token\n')
   response=Mock(ok=True)
   with patch('agent_actions.requests.post',return_value=response) as post:
    result=Actions(root,'Morgan','handoff').execute('staff_message',{'to':'Cameron','text':'Review the return policy and report any gaps.'})
   self.assertIn('sent to Cameron',result['result'])
   self.assertEqual(post.call_args.args[0],'https://api.telegram.org/botprivate-test-token/sendMessage')
   self.assertEqual(post.call_args.kwargs['json']['chat_id'],'-1004450996238')
   self.assertTrue(post.call_args.kwargs['json']['text'].startswith('[[ONYX_STAFF target=Cameron hops=1]]'))
 def test_allowlisted_external_email_sends_without_an_outbox_review(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);(root/'.env').write_text(CFG+'AUTONOMOUS_EXTERNAL_EMAILS=partner@example.com\n')
   mail=Mock();mail.deliver.return_value='Email accepted by mail server.'
   with patch('agent_actions.StaffMail.from_env',return_value=mail) as factory:
    result=Actions(root,'Avery','approved-external').execute('email',{'to':'partner@example.com','subject':'Brief','body':'Attached is the current brief.'})
   factory.assert_called_once_with(root,config=ANY);mail.deliver.assert_called_once();self.assertIn('Sent automatically',result['approval'])
 def test_agents_can_generate_word_and_pdf_files_for_telegram_or_drive_sharing(self):
  with tempfile.TemporaryDirectory() as d:
   a=Actions(Path(d),'Avery','files')
   word=a.execute('word_document',{'title':'Campaign brief','text':'Launch details\n• Confirm assets'})
   pdf=a.execute('pdf',{'title':'Campaign brief','text':'Launch details\n• Confirm assets'})
   self.assertEqual(a.file(word['file_id']).suffix,'.docx');self.assertTrue(a.file(word['file_id']).read_bytes().startswith(b'PK'))
   self.assertEqual(a.file(pdf['file_id']).suffix,'.pdf');self.assertTrue(a.file(pdf['file_id']).read_bytes().startswith(b'%PDF'))
 def test_jordan_can_stage_a_product_with_a_telegram_image(self):
  with tempfile.TemporaryDirectory() as d:
   a=Actions(Path(d),'Jordan','catalog')
   image=a.save('mug.png',b'png-image','.png','telegram-upload')
   product=a.execute('product_update',{'title':'Onyx Mug','description':'Black ceramic mug.','price':'18.5','image_file_id':image['file_id'],'status':'published'})
   self.assertEqual(product['status'],'ready');self.assertIn('Square',product['result'])
   self.assertEqual(a.execute('products',{})['products'][0]['title'],'Onyx Mug')
   self.assertIn('error',Actions(Path(d),'Avery','catalog').execute('products',{}))
 def test_google_drive_aliases_return_the_workspace_result(self):
  with tempfile.TemporaryDirectory() as d:
   a=Actions(Path(d),'Morgan','drive');a.workspace.list_files=Mock(return_value={'files':[{'name':'Brief'}]})
   self.assertEqual(a.execute('google_drive',{})['files'][0]['name'],'Brief')
   error=a.execute('not_a_real_action',{})
   self.assertIn('Unknown action "not_a_real_action"',error['error'])
if __name__=='__main__':unittest.main()
