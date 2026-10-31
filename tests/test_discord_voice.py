"""Discord voice replies, fragment guard, per-agent tokens, and slash commands.

Offline tests: the Discord API is stubbed, the voice engine is stubbed,
and no token values leave the test. Covers the Discord parity of the
Discord voice flow: /voice as a message, /voice as a slash command,
one retry for a truncated answer, and replies/typing under the
answering agent's own bot token when one is configured.
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from staff.agent_actions import Actions
from staff.discord_bridge import DiscordBridge, DISCORD_COMMANDS

STAFF_CHANNEL = '111111111111111111'
AVERY_CHANNEL = '222222222222222222'
SHARED_TOKEN = 'shared-token'
AVERY_TOKEN = 'avery-token'
FRAGMENT = "Hey Jauneé, good to meet you. I'm Avery,"
FULL = 'Hey Jauneé, good to meet you. I am Avery, the marketing lead, and I am glad you are here.'


class FakeChat:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def ask(self, agent, body, request_id, source='discord', attachments=None, no_tools=False):
        self.calls.append({'agent': agent, 'body': body, 'request_id': request_id,
                           'source': source, 'no_tools': no_tools})
        return self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]


class CapturingBridge(DiscordBridge):
    def __init__(self, root, chat):
        super().__init__(root, chat, None)
        self.calls = []

    def api(self, method, path, token, **kwargs):
        self.calls.append({'method': method, 'path': path, 'token': token,
                           'json': kwargs.get('json'), 'files': bool(kwargs.get('files'))})
        if path == '/applications/@me':
            return {'id': '999'}
        return {}


def _root_with_env(tmp):
    root = Path(tmp)
    (root / 'work').mkdir(parents=True)
    (root / '.env').write_text(
        'DISCORD_ENABLED=true\nDISCORD_BOT_TOKEN=%s\nDISCORD_AVERY_BOT_TOKEN=%s\n'
        'DISCORD_STAFF_CHANNEL_ID=%s\nDISCORD_AVERY_CHANNEL_ID=%s\n'
        % (SHARED_TOKEN, AVERY_TOKEN, STAFF_CHANNEL, AVERY_CHANNEL))
    return root


def _message(channel, text, message_id='555'):
    return {'id': message_id, 'author': {'id': '123', 'bot': False},
            'channel_id': channel, 'content': text, 'attachments': []}


def _speak_stub(root, agent, text):
    return {'data': b'OggS-fake', 'format': 'OGG', 'duration_seconds': 3.0,
            'provider': 'Gemini text-to-speech'}


class DiscordVoiceMessageTests(unittest.TestCase):
    def _bridge(self, root, answers):
        chat = FakeChat(answers)
        bridge = CapturingBridge(root, chat)
        return bridge, chat

    def test_truncated_voice_message_is_retried_and_full_answer_spoken(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            bridge, chat = self._bridge(root, [FRAGMENT, FULL])
            with patch('staff.voice.speak', side_effect=_speak_stub), \
                    patch.object(DiscordBridge, 'ogg_to_mp3', return_value=b'ID3fake'):
                bridge.handle_message(_message(AVERY_CHANNEL, '/voice Everyone take sometime to showcase your new voices to Jauneé'), SHARED_TOKEN)
            records = [json.loads(line) for line in (root / 'work' / 'voice-messages.jsonl').read_text().strip().splitlines()]
        self.assertEqual(len(chat.calls), 2)
        self.assertEqual(chat.calls[0]['request_id'], 'discord:555')
        self.assertNotEqual(chat.calls[1]['request_id'], 'discord:555')
        self.assertIn('cut off', chat.calls[1]['body'])
        self.assertTrue(chat.calls[1]['no_tools'])
        typing = [c for c in bridge.calls if c['path'].endswith('/typing')]
        self.assertEqual(typing[0]['token'], AVERY_TOKEN)
        audio = [c for c in bridge.calls if c['files']]
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]['token'], AVERY_TOKEN)
        self.assertEqual(records[-1]['text'], FULL)
        self.assertEqual(records[-1]['provider'], 'Gemini text-to-speech')
        self.assertEqual(records[-1]['kind'], 'discord-voice-reply')

    def test_complete_voice_message_is_spoken_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            bridge, chat = self._bridge(root, [FULL])
            with patch('staff.voice.speak', side_effect=_speak_stub), \
                    patch.object(DiscordBridge, 'ogg_to_mp3', return_value=b'ID3fake'):
                bridge.handle_message(_message(AVERY_CHANNEL, '/voice Hello there'), SHARED_TOKEN)
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual([c for c in bridge.calls if c['files']][0]['token'], AVERY_TOKEN)

    def test_plain_message_uses_agent_token_and_staff_floor_defaults_to_morgan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            bridge, chat = self._bridge(root, ['Hello back.'])
            bridge.handle_message(_message(AVERY_CHANNEL, 'A status update'), SHARED_TOKEN)
            bridge.handle_message(_message(STAFF_CHANNEL, 'Hello everyone'), SHARED_TOKEN)
        self.assertEqual(chat.calls[0]['agent'], 'Avery')
        self.assertEqual(chat.calls[1]['agent'], 'Morgan')
        typing = [c for c in bridge.calls if c['path'].endswith('/typing')]
        self.assertEqual(typing[0]['token'], AVERY_TOKEN)
        self.assertEqual(typing[1]['token'], SHARED_TOKEN)

    def test_bare_voice_command_gets_a_hint_not_a_voice_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            bridge, chat = self._bridge(root, [FULL])
            with patch('staff.voice.speak', side_effect=_speak_stub):
                bridge.handle_message(_message(AVERY_CHANNEL, '/voice'), SHARED_TOKEN)
        self.assertEqual(chat.calls, [])
        self.assertEqual([c for c in bridge.calls if c['files']], [])
        hint = [c for c in bridge.calls if c['json'] and 'Send /voice followed by' in str(c['json'].get('content', ''))]
        self.assertEqual(len(hint), 1)


class DiscordInteractionTests(unittest.TestCase):
    def _interaction(self, name, channel, message='Hello there'):
        return {'type': 2, 'id': '111', 'token': 'itoken', 'channel_id': channel,
                'member': {'user': {'id': '123', 'bot': False}},
                'data': {'name': name, 'options': [{'name': 'message', 'value': message}]}}

    def test_voice_slash_command_acks_then_follows_up_with_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            chat = FakeChat([FULL])
            bridge = CapturingBridge(root, chat)
            with patch('staff.voice.speak', side_effect=_speak_stub), \
                    patch.object(DiscordBridge, 'ogg_to_mp3', return_value=b'ID3fake'):
                bridge.handle_interaction(self._interaction('voice', AVERY_CHANNEL), SHARED_TOKEN)
            records = [json.loads(line) for line in (root / 'work' / 'voice-messages.jsonl').read_text().strip().splitlines()]
        ack = [c for c in bridge.calls if '/interactions/' in c['path'] and c['path'].endswith('/callback')]
        self.assertEqual(len(ack), 1)
        self.assertEqual(ack[0]['json'], {'type': 5})
        self.assertEqual(len(chat.calls), 1)
        self.assertEqual(chat.calls[0]['agent'], 'Avery')
        followups = [c for c in bridge.calls if c['path'].startswith('/webhooks/999/')]
        self.assertEqual(len(followups), 1)
        self.assertTrue(followups[0]['files'])
        self.assertEqual(records[-1]['kind'], 'discord-voice-reply')

    def test_agent_slash_command_routes_to_that_agent_as_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            chat = FakeChat(['Jordan here.'])
            bridge = CapturingBridge(root, chat)
            bridge.handle_interaction(self._interaction('jordan', STAFF_CHANNEL, 'Check the storefront'), SHARED_TOKEN)
        self.assertEqual(chat.calls[0]['agent'], 'Jordan')
        followups = [c for c in bridge.calls if c['path'].startswith('/webhooks/999/')]
        self.assertEqual(len(followups), 1)
        self.assertFalse(followups[0]['files'])
        self.assertIn('Jordan here.', followups[0]['json']['content'])

    def test_register_commands_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            bridge = CapturingBridge(root, None)
            bridge.register_commands(SHARED_TOKEN)
        put = [c for c in bridge.calls if c['method'] == 'PUT'][0]
        self.assertEqual(put['path'], '/applications/999/commands')
        self.assertEqual([c['name'] for c in put['json']], ['voice', 'morgan', 'avery', 'jordan', 'cameron', 'help', 'join-voice', 'leave-voice'])
        self.assertEqual([c['name'] for c in DISCORD_COMMANDS], ['voice', 'morgan', 'avery', 'jordan', 'cameron', 'help', 'join-voice', 'leave-voice'])


class DiscordVoiceActionTests(unittest.TestCase):
    def test_discord_voice_action_posts_audio_under_agent_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            posted = []

            class FakeDiscordBridge:
                def __init__(self, *args, **kwargs):
                    pass

                def channel(self):
                    return STAFF_CHANNEL

                def valid_channel(self, value):
                    return True

                def token(self, agent=''):
                    return AVERY_TOKEN if agent == 'Avery' else SHARED_TOKEN

                def ogg_to_mp3(self, data):
                    return b'ID3fake'

                def post_audio(self, channel, token, mp3, agent, caption):
                    posted.append({'channel': channel, 'token': token, 'mp3': mp3, 'agent': agent})

            with patch('staff.discord_bridge.DiscordBridge', FakeDiscordBridge), \
                    patch('staff.voice.speak', side_effect=_speak_stub):
                result = Actions(root, 'Avery', 'test-run').execute('discord_voice', {'text': 'Hello team'})
            records = [json.loads(line) for line in (root / 'work' / 'voice-messages.jsonl').read_text().strip().splitlines()]
        self.assertEqual(result['channel'], STAFF_CHANNEL)
        self.assertEqual(result['provider'], 'Gemini text-to-speech')
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]['token'], AVERY_TOKEN)
        self.assertEqual(posted[0]['mp3'], b'ID3fake')
        self.assertEqual(records[-1]['kind'], 'discord-group-voice')
        self.assertEqual(records[-1]['provider'], 'Gemini text-to-speech')


if __name__ == '__main__':
    unittest.main()
