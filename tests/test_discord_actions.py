"""Discord agent actions: polls, poll results, reactions, stickers.

Offline: the bridge is stubbed, no token values leave the test.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from staff.agent_actions import Actions

STAFF_CHANNEL = '111111111111111111'
GUILD_ID = '777777777777777777'


class FakeBridge:
    """Stub for staff.discord_bridge.DiscordBridge."""
    last = None

    def __init__(self, root, chat, stop):
        self.root = root
        self.calls = []
        FakeBridge.last = self

    def channel(self, agent='', private=False):
        return STAFF_CHANNEL

    def valid_channel(self, value):
        return True

    def token(self, agent=''):
        return 'dummy-' + agent.lower() + '-token' if agent else 'dummy-shared-token'

    def create_poll(self, channel, token, question, answers, duration_hours=24):
        self.calls.append(('create_poll', channel, token, question, answers, duration_hours))
        return {'id': '999'}

    def read_poll(self, channel, message_id, token):
        self.calls.append(('read_poll', channel, message_id, token))
        return {'question': 'Lunch?', 'answers': [{'id': 1, 'text': 'Tacos', 'votes': 3}],
                'finalized': False}

    def react(self, channel, message_id, emoji, token):
        self.calls.append(('react', channel, message_id, emoji, token))
        return {}

    def send_sticker(self, channel, token, guild_id, name):
        self.calls.append(('send_sticker', channel, token, guild_id, name))
        return {'id': '888'}


def _root_with_env(tmp):
    root = Path(tmp)
    (root / 'work').mkdir(parents=True)
    (root / '.env').write_text(
        'DISCORD_ENABLED=true\nDISCORD_AVERY_BOT_TOKEN=dummy-avery-token\n'
        'DISCORD_STAFF_CHANNEL_ID=%s\nDISCORD_GUILD_ID=%s\n' % (STAFF_CHANNEL, GUILD_ID))
    return root


class DiscordActionTests(unittest.TestCase):
    def _actions(self, root, agent='Avery'):
        return Actions(root, agent, 'test-run')

    def test_poll_posts_with_agent_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            with patch('staff.discord_bridge.DiscordBridge', FakeBridge):
                result = self._actions(root).execute(
                    'discord_poll', {'question': 'Lunch?', 'answers': ['Tacos', 'Salad']})
            call = FakeBridge.last.calls[-1]
            self.assertEqual(call[0], 'create_poll')
            self.assertEqual(call[1], STAFF_CHANNEL)
            self.assertEqual(call[2], 'dummy-avery-token')
            self.assertEqual(call[3], 'Lunch?')
            self.assertEqual(call[4], ['Tacos', 'Salad'])
            self.assertEqual(result['message_id'], '999')

    def test_poll_results_reads_votes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            with patch('staff.discord_bridge.DiscordBridge', FakeBridge):
                result = self._actions(root).execute('discord_poll_results', {'message_id': '555'})
            call = FakeBridge.last.calls[-1]
            self.assertEqual(call[0], 'read_poll')
            self.assertEqual(call[2], '555')
            self.assertEqual(result['answers'][0]['votes'], 3)

    def test_react_and_sticker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            with patch('staff.discord_bridge.DiscordBridge', FakeBridge):
                result = self._actions(root).execute(
                    'discord_react', {'message_id': '555', 'emoji': '👍'})
                self.assertEqual(FakeBridge.last.calls[-1][0], 'react')
                result = self._actions(root).execute('discord_sticker', {'name': 'Wave'})
            call = FakeBridge.last.calls[-1]
            self.assertEqual(call[0], 'send_sticker')
            self.assertEqual(call[3], GUILD_ID)
            self.assertEqual(call[4], 'Wave')
            self.assertEqual(result['message_id'], '888')

    def test_actions_require_enabled_and_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'work').mkdir(parents=True)
            (root / '.env').write_text('DISCORD_ENABLED=true\n')
            with patch('staff.discord_bridge.DiscordBridge', FakeBridge):
                result = self._actions(root).execute(
                    'discord_poll', {'question': 'Lunch?', 'answers': ['Tacos', 'Salad']})
            self.assertIn('bot token', result.get('error', ''))


if __name__ == '__main__':
    unittest.main()
