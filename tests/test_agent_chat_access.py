import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch
from email.message import Message
from staff.agent_chat import AgentChat, concise_reply
from staff.dashboard_access import Access, setup
import threading
import time

class ChatAccessTests(unittest.TestCase):
    def test_plain_chat_skips_tools_and_action_request_uses_them(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('GROQ_API_KEY=test\nAGENT_TOOLS_ENABLED=true\n')
            chat=AgentChat(root);chat.llm=Mock();chat.llm.call.return_value='A focused answer.';chat.act=Mock(return_value='Report saved.')
            plain=chat.ask('Morgan','Help me prioritize the Christmas launch.','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
            self.assertEqual(plain,'A focused answer.');chat.act.assert_not_called()
            action=chat.ask('Morgan','Create a launch report with the next steps.','bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb')
            self.assertEqual(action,'Report saved.');chat.act.assert_called_once()

    def test_presentation_about_a_tumbler_uses_the_action_path_not_image_generation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('GROQ_API_KEY=test\nAGENT_TOOLS_ENABLED=true\n')
            chat=AgentChat(root);chat.llm=Mock();chat.act=Mock(return_value='Google Slides: https://docs.google.com/presentation/d/example/edit')
            answer=chat.ask('Avery','Create a Google Slides presentation about the Christmas tumbler bundles.','abababab-abab-abab-abab-abababababab')
            self.assertIn('docs.google.com/presentation',answer)
            chat.act.assert_called_once()

    def test_design_deck_term_uses_google_slides_action_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('GROQ_API_KEY=test\nAGENT_TOOLS_ENABLED=true\n')
            chat=AgentChat(root);chat.llm=Mock();chat.act=Mock(return_value='Google Slides: https://docs.google.com/presentation/d/example/edit')
            answer=chat.ask('Avery','Create a design deck for the Christmas tumbler launch.','acacacac-acac-acac-acac-acacacacacac')
            self.assertIn('docs.google.com/presentation',answer)
            self.assertIn('first action must be presentation',chat.act.call_args.args[1][0]['content'])

    def test_coworker_email_uses_tools_with_limited_authority(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('GROQ_API_KEY=test\nAGENT_TOOLS_ENABLED=true\n')
            chat=AgentChat(root);chat.llm=Mock();chat.act=Mock(return_value='Work completed.')
            answer=chat.ask('Jordan','Authenticated internal message from Avery. Update the storefront notes.','eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',source='staff_email')
            self.assertEqual(answer,'Work completed.')
            self.assertEqual(chat.act.call_args.kwargs['source'],'staff_email')
            self.assertIn('named Onyx & Ink coworker',chat.act.call_args.args[1][0]['content'])

    def test_slow_agent_does_not_block_another_agent_chat(self):
        class ConcurrentLLM:
            def __init__(self):self.active=0;self.maximum=0;self.lock=threading.Lock()
            def call(self,messages):
                with self.lock:self.active+=1;self.maximum=max(self.maximum,self.active)
                time.sleep(.08)
                with self.lock:self.active-=1
                return 'Done'
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('GROQ_API_KEY=test\n')
            chat=AgentChat(root);chat.llm=ConcurrentLLM();results=[]
            threads=[threading.Thread(target=lambda a=a,r=r:results.append(chat.ask(a,'Give me one idea.',r))) for a,r in [('Morgan','cccccccc-cccc-cccc-cccc-cccccccccccc'),('Avery','dddddddd-dddd-dddd-dddd-dddddddddddd')]]
            for thread in threads:thread.start()
            for thread in threads:thread.join()
            self.assertEqual(results,['Done','Done']);self.assertEqual(chat.llm.maximum,2)
    def test_shared_history_idempotency_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('GROQ_API_KEY=test-private-secret\n')
            chat=AgentChat(root);chat.llm=Mock();chat.llm.call.return_value='Draft test-private-secret'
            request='11111111-1111-1111-1111-111111111111'
            answer=chat.ask('Morgan','Plan test-private-secret',request)
            self.assertEqual(answer,'Draft [REDACTED]')
            self.assertEqual(chat.ask('Morgan','Plan',request),answer)
            chat.llm.call.assert_called_once()
            self.assertNotIn('test-private-secret',str(chat.history('Morgan')))
            chat.ask('Morgan','Next step','discord:42',source='discord')
            self.assertEqual(AgentChat(root).history('Morgan')[-1]['source'],'discord')
            with self.assertRaises(ValueError):chat.history('CEO')
    def test_transient_cloud_error_is_not_sent_back_to_the_model_as_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('GROQ_API_KEY=test\n')
            chat=AgentChat(root);chat.llm=Mock();chat.llm.call.return_value='Recovered answer.'
            with chat.db() as db:
                db.execute('INSERT INTO messages(agent,role,body,source) VALUES (?,?,?,?)',('Morgan','assistant','I could not reach a compatible cloud AI service. Please try again.','discord'))
            answer=chat.ask('Morgan','What is next?','12121212-1212-1212-1212-121212121212')
            self.assertEqual(answer,'Recovered answer.')
            sent=chat.llm.call.call_args.args[0]
            self.assertFalse(any('I could not reach a compatible cloud' in str(item['content']) for item in sent))
    def test_cameron_retries_a_safety_only_model_response(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('GROQ_API_KEY=test\n')
            chat=AgentChat(root);chat.llm=Mock();chat.llm.call.side_effect=['User Safety: safe Response Safety: safe','I can review the policy language and flag the open point.']
            answer=chat.ask('Cameron','Please introduce yourself.','13131313-1313-1313-1313-131313131313')
            self.assertIn('review the policy',answer)
            self.assertEqual(chat.llm.call.call_count,2)
    def test_email_draft_uses_one_plain_model_call_without_tools(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('GROQ_API_KEY=test-private-secret\nOWNER_EMAIL=ceo@onyxandink.org\n')
            chat=AgentChat(root);chat.draft_llm=Mock();chat.draft_llm.call.return_value='Hello,\n\nThanks for writing.\n\nMorgan'
            answer=chat.draft_reply('Morgan','Reply to this customer question.')
            self.assertEqual(answer,'Hello,\n\nThanks for writing.\n\nMorgan')
            chat.draft_llm.call.assert_called_once()
            messages=chat.draft_llm.call.call_args.args[0]
            self.assertIn('ceo@onyxandink.org',messages[0]['content'])
            self.assertNotIn('JSON object',messages[0]['content'])
            self.assertEqual(chat.history('Morgan'),[])
    def test_wifi_requires_password_session_and_preserves_setup(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);setup(root,['10.0.0.20']);access=Access(root)
            handler=SimpleNamespace(client_address=('10.0.0.21',1),connection=None,headers=Message())
            self.assertFalse(access.authenticated(handler))
            self.assertIsNone(access.login('wrong','10.0.0.21'))
            password=(root/'work/wifi-password.txt').read_text().strip()
            token=access.login(password,'10.0.0.21');self.assertTrue(token)
            handler.headers['Cookie']='onyx_session='+token
            self.assertTrue(access.authenticated(handler));access.sessions[token]=0
            self.assertFalse(access.authenticated(handler))
            setup(root,['10.0.0.22']);self.assertEqual((root/'work/wifi-password.txt').read_text().strip(),password)
            self.assertIsNotNone(access.context())
    def test_cloud_proxy_never_receives_localhost_auth_bypass(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'ONYX_CLOUD_MODE':'true','ONYX_PUBLIC_ORIGIN':'https://staff.example.com','DASHBOARD_PASSWORD':'cloud-test-password'},clear=False):
            root=Path(folder);access=Access(root)
            handler=SimpleNamespace(client_address=('127.0.0.1',1),connection=None,headers=Message())
            self.assertFalse(access.authenticated(handler))
            self.assertIn('staff.example.com',access.hosts())
            token=access.login('cloud-test-password','127.0.0.1');self.assertTrue(token)
            handler.headers['Cookie']='onyx_session='+token
            self.assertTrue(access.authenticated(handler))
            saved=(root/'work/wifi-access.json').read_text()
            self.assertNotIn('cloud-test-password',saved)
    def test_password_display_rejects_stale_migration_password(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'ONYX_REQUIRE_LOGIN':'true','DASHBOARD_PASSWORD':'current-test-password'},clear=False):
            root=Path(folder);access=Access(root)
            (root/'work/wifi-password.txt').write_text('old-Windows PC-password\n')
            self.assertEqual(access.local_password(),'current-test-password')
            with patch.dict('os.environ',{'DASHBOARD_PASSWORD':''}):
                with self.assertRaisesRegex(RuntimeError,'outdated'):access.local_password()
            (root/'work/wifi-password.txt').write_text('current-test-password\n')
            self.assertEqual(access.local_password(),'current-test-password')
            self.assertEqual(access.sessions,{})
    def test_windows_docker_host_requires_local_origin_and_session(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'ONYX_CLOUD_MODE':'false','ONYX_REQUIRE_LOGIN':'true','ONYX_WINDOWS_DOCKER':'true','DASHBOARD_PASSWORD':'host-test-password'}):
            access=Access(folder)
            handler=SimpleNamespace(client_address=('192.168.65.1',1),connection=None,headers=Message())
            handler.headers['Host']='localhost:8765';handler.headers['Origin']='http://localhost:8765'
            self.assertFalse(access.host_pc(handler))
            token=access.login('host-test-password','192.168.65.1');handler.headers['Cookie']='onyx_session='+token
            self.assertTrue(access.host_pc(handler))
            handler.headers.replace_header('Origin','http://10.0.0.156:8765');self.assertFalse(access.host_pc(handler))
            handler.headers.replace_header('Origin','http://localhost:8765');access.cloud_mode=True
            self.assertFalse(access.host_pc(handler))
    def test_windows_login_mode_requires_a_session_even_behind_local_docker(self):
        with tempfile.TemporaryDirectory() as folder,patch.dict('os.environ',{'ONYX_CLOUD_MODE':'false','ONYX_REQUIRE_LOGIN':'true','ONYX_ALLOWED_HOSTS':'192.168.1.40:8765','DASHBOARD_PASSWORD':'windows-test-password'},clear=False):
            root=Path(folder);access=Access(root)
            handler=SimpleNamespace(client_address=('127.0.0.1',1),connection=None,headers=Message())
            self.assertFalse(access.authenticated(handler))
            self.assertIn('192.168.1.40:8765',access.hosts())
            self.assertTrue(access.login('windows-test-password','127.0.0.1'))
    def test_message_instruction_enters_tool_flow(self):
        self.assertTrue(AgentChat.needs_tools('Tell Cameron to get on the return policy ASAP.'))

    def test_drive_reference_request_enters_tool_flow(self):
        self.assertTrue(AgentChat.needs_tools('Look at the Drive files and use them as references.'))
        self.assertTrue(AgentChat.needs_tools('Create two tumbler designs from the Google Drive references.'))
        self.assertTrue(AgentChat.needs_tools('Can Morgan access Drive?'))
    def test_replies_remove_heading_syntax_and_keep_basic_formatting(self):
        self.assertEqual(concise_reply('## Next steps\n\n- **Launch**\n\n\nDone'),'Next steps\n\n- **Launch**\n\nDone')
    def test_staff_message_command_dispatches_before_reporting_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.env').write_text('GROQ_API_KEY=test\nAGENT_TOOLS_ENABLED=true\n')
            chat=AgentChat(root);chat.llm=Mock()
            with patch('staff.agent_actions.Actions.execute',return_value={'result':'Email accepted by mail server; inbox delivery is not verified.'}) as execute:
                answer=chat.ask('Morgan','Tell Cameron to get the return policy information ASAP.','14141414-1414-1414-1414-141414141414')
            self.assertIn('sent your message to Cameron',answer)
            self.assertEqual(execute.call_args.args[0],'staff_message')
            self.assertEqual(execute.call_args.args[1]['to'],'Cameron')
            self.assertIn('return policy',execute.call_args.args[1]['text'])

if __name__=='__main__':unittest.main()
