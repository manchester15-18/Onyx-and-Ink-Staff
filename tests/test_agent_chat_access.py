import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from email.message import Message
from agent_chat import AgentChat, TelegramBridge
from dashboard_access import Access, setup
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
            chat.ask('Morgan','Next step','telegram:42',source='telegram')
            self.assertEqual(AgentChat(root).history('Morgan')[-1]['source'],'telegram')
            with self.assertRaises(ValueError):chat.history('CEO')
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
    def test_telegram_pair_code_is_temporary_and_state_defaults_disabled(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);bridge=TelegramBridge(root,AgentChat(root),threading.Event())
            self.assertFalse(bridge.state()['enabled'])
            self.assertNotEqual(bridge.pairing(),bridge.pairing())

if __name__=='__main__':unittest.main()
