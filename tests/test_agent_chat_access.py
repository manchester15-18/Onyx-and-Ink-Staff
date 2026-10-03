import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from email.message import Message
from agent_chat import AgentChat, TelegramBridge
from dashboard_access import Access, setup
import threading

class ChatAccessTests(unittest.TestCase):
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
