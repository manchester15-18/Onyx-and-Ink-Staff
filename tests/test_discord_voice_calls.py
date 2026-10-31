"""Discord voice calls, voice-message replies, and multi-bot gateways.

Offline tests: the Discord API is stubbed, the voice engine is
stubbed, and no token values leave the test. Covers the Oct 31, 2026
build: TurnTracker endpointing and barge-in, the 48k stereo to 16k
mono downmix, voice-message attachments transcribed and answered by
voice, scoped per-agent gateways with ownership dedup, dynamic spawn
when a token appears, and /join-voice + /leave-voice interactions.
"""
import asyncio
import json
import re
import struct
import sys
import tempfile
import unittest
import wave
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from staff.discord_bridge import DiscordBridge, DISCORD_COMMANDS
from staff.discord_voice import (
    TurnTracker, VoiceSession, downmix_pcm48_stereo_to_16k_mono, pcm_to_wav)

STAFF_CHANNEL = '111111111111111111'
AVERY_CHANNEL = '222222222222222222'
VOICE_CHANNEL = '333333333333333333'
SHARED_TOKEN = 'shared-token'
AVERY_TOKEN = 'avery-token'
FULL = 'Hey Jauneé, good to meet you. I am Avery, the marketing lead, and I am glad you are here.'


class FakeChat:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def ask(self, agent, body, request_id, source='discord', attachments=None, no_tools=False):
        self.calls.append({'agent': agent, 'body': body, 'request_id': request_id,
                           'source': source, 'attachments': attachments, 'no_tools': no_tools})
        return self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]


class CapturingBridge(DiscordBridge):
    def __init__(self, root, chat, scope=''):
        super().__init__(root, chat, None, scope=scope)
        self.calls = []

    def api(self, method, path, token, **kwargs):
        self.calls.append({'method': method, 'path': path, 'token': token,
                           'json': kwargs.get('json'), 'files': bool(kwargs.get('files'))})
        if path == '/applications/@me':
            return {'id': '999'}
        return {}


def _root_with_env(tmp, avery_token=True, morgan_token=False):
    root = Path(tmp)
    (root / 'work').mkdir(parents=True)
    env = ('DISCORD_ENABLED=true\nDISCORD_BOT_TOKEN=%s\n'
           'DISCORD_STAFF_CHANNEL_ID=%s\nDISCORD_AVERY_CHANNEL_ID=%s\n'
           % (SHARED_TOKEN, STAFF_CHANNEL, AVERY_CHANNEL))
    if avery_token:
        env += 'DISCORD_AVERY_BOT_TOKEN=%s\n' % AVERY_TOKEN
    if morgan_token:
        env += 'DISCORD_MORGAN_BOT_TOKEN=dummy-morgan-token\n'
    (root / '.env').write_text(env)
    return root


def _message(channel, text, attachments=None, guild_id='777'):
    data = {'id': '555', 'author': {'id': '123', 'bot': False},
            'channel_id': channel, 'content': text, 'attachments': attachments or []}
    if guild_id:
        data['guild_id'] = guild_id
    return data


def _speak_stub(root, agent, text):
    return {'data': b'OggS-fake', 'format': 'OGG', 'duration_seconds': 3.0,
            'provider': 'Gemini text-to-speech'}


class FakeResponse:
    def __init__(self, content):
        self.content = content
        self.status_code = 200
        self.ok = True


class ScriptedVAD:
    """VAD that returns a scripted speech/silence pattern per frame."""

    def __init__(self, pattern):
        self.pattern = list(pattern)
        self.index = 0

    def is_speech(self, frame, rate):
        value = self.pattern[min(self.index, len(self.pattern) - 1)]
        self.index += 1
        return value


def _silence_frames(n):
    return b'\x00' * (640 * n)


class TurnTrackerTests(unittest.TestCase):
    def test_turn_ends_after_silence_and_keeps_pre_roll(self):
        tracker = TurnTracker(vad=ScriptedVAD([False] * 5 + [True] * 3 + [False] * 40))
        events = tracker.feed(_silence_frames(5) + b'\x01' * (640 * 3) + _silence_frames(40))
        ended = [p for e, p in events if e == 'turn_ended']
        self.assertEqual(len(ended), 1)
        pcm, truncated = ended[0]
        self.assertFalse(truncated)
        # 5 pre-roll silence frames + 3 speech frames; trailing silence trimmed.
        self.assertEqual(len(pcm), 640 * 8)

    def test_barge_in_fires_after_debounce_not_grace(self):
        tracker = TurnTracker(vad=ScriptedVAD([True] * 16 + [False] * 40))
        tracker.set_playing(True, now=0.0)
        events = tracker.feed(b'\x01' * (640 * 10), now=0.1)
        self.assertEqual([e for e, _ in events], [])
        events = tracker.feed(b'\x01' * (640 * 6), now=10.0)
        self.assertEqual([e for e, _ in events], ['barge_in'])
        events = tracker.feed(_silence_frames(40), now=10.0)
        ended = [p for e, p in events if e == 'turn_ended']
        self.assertEqual(len(ended), 1)
        # Barge turns start at the barge run, never from playback-era pre-roll.
        self.assertEqual(len(ended[0][0]), 640 * 6)

    def test_long_turn_is_truncated_at_sixty_seconds(self):
        tracker = TurnTracker(vad=ScriptedVAD([True] * 100), max_seconds=1)
        events = tracker.feed(b'\x01' * (640 * 60))
        ended = [p for e, p in events if e == 'turn_ended']
        self.assertEqual(len(ended), 1)
        self.assertTrue(ended[0][1])


class AudioHelperTests(unittest.TestCase):
    def test_downmix_averages_channels_and_decimates(self):
        frames = [(3000, 1000)] * 6
        pcm48 = b''.join(struct.pack('<hh', left, right) for left, right in frames)
        out = downmix_pcm48_stereo_to_16k_mono(pcm48)
        self.assertEqual(struct.unpack('<hh', out), (2000, 2000))

    def test_pcm_to_wav_header(self):
        wav_bytes = pcm_to_wav(b'\x00\x00' * 16000)
        with wave.open(BytesIO(wav_bytes), 'rb') as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getframerate(), 16000)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getnframes(), 16000)


class VoiceMessageTests(unittest.TestCase):
    def _voice_message(self):
        return _message(AVERY_CHANNEL, '', attachments=[
            {'filename': 'voice-message.ogg', 'size': 1234,
             'url': 'https://cdn.discordapp.com/voice.ogg'}])

    def test_voice_message_is_transcribed_and_answered_by_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            chat = FakeChat([FULL])
            bridge = CapturingBridge(root, chat)
            with patch('staff.discord_bridge.requests.get', return_value=FakeResponse(b'OggS-fake')), \
                    patch('staff.voice.transcribe', return_value='Hello from voice'), \
                    patch('staff.voice.speak', side_effect=_speak_stub), \
                    patch.object(DiscordBridge, 'ogg_to_mp3', return_value=b'ID3fake'):
                bridge.handle_message(self._voice_message(), SHARED_TOKEN)
        self.assertEqual(len(chat.calls), 1)
        self.assertIn('Voice message transcribed: "Hello from voice"', chat.calls[0]['body'])
        self.assertEqual(chat.calls[0]['attachments'], [])
        self.assertTrue(chat.calls[0]['no_tools'])
        self.assertEqual(len([c for c in bridge.calls if c['files']]), 1)

    def test_transcription_failure_sends_a_note_not_silence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            chat = FakeChat([FULL])
            bridge = CapturingBridge(root, chat)
            with patch('staff.discord_bridge.requests.get', return_value=FakeResponse(b'OggS-fake')), \
                    patch('staff.voice.transcribe', side_effect=RuntimeError('whisper down')):
                bridge.handle_message(self._voice_message(), SHARED_TOKEN)
        self.assertEqual(chat.calls, [])
        notes = [c for c in bridge.calls if c['json'] and 'could not transcribe' in str(c['json'].get('content', ''))]
        self.assertEqual(len(notes), 1)


class MultiBotTests(unittest.TestCase):
    def test_ownership_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp, morgan_token=True)
            shared = CapturingBridge(root, FakeChat([]))
            morgan = CapturingBridge(root, FakeChat([]), scope='Morgan')
            avery = CapturingBridge(root, FakeChat([]), scope='Avery')
            avery_channel = _message(AVERY_CHANNEL, 'Hi')
            staff_floor = _message(STAFF_CHANNEL, 'Hi')
            self.assertFalse(shared.owns_message(avery_channel))
            self.assertTrue(avery.owns_message(avery_channel))
            self.assertFalse(shared.owns_message(staff_floor))
            self.assertTrue(morgan.owns_message(staff_floor))
            self.assertFalse(avery.owns_message(staff_floor))
            dm = _message(AVERY_CHANNEL, 'Hi', guild_id=None)
            self.assertTrue(shared.owns_message(dm))
            self.assertTrue(avery.owns_message(dm))

    def test_morgan_falls_back_to_agent_channel_without_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp, avery_token=False, morgan_token=True)
            shared = CapturingBridge(root, FakeChat([]))
            morgan = CapturingBridge(root, FakeChat([]), scope='Morgan')
            self.assertTrue(morgan.owns_message(_message(AVERY_CHANNEL, 'Hi')))
            self.assertFalse(shared.owns_message(_message(AVERY_CHANNEL, 'Hi')))

    def test_scoped_bridge_defaults_routing_to_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            chat = FakeChat(['Hello back.'])
            bridge = CapturingBridge(root, chat, scope='Avery')
            bridge.handle_message(_message(STAFF_CHANNEL, 'Hello everyone'), SHARED_TOKEN)
            self.assertEqual(chat.calls[0]['agent'], 'Avery')
            shared_chat = FakeChat(['Hello back.'])
            shared = CapturingBridge(root, shared_chat)
            shared.handle_message(_message(STAFF_CHANNEL, 'Hello everyone'), SHARED_TOKEN)
            self.assertEqual(shared_chat.calls[0]['agent'], 'Morgan')

    def test_spawn_starts_one_thread_per_configured_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            shared = CapturingBridge(root, FakeChat([]))
            started = []

            class FakeThread:
                def __init__(self, target=None, daemon=None):
                    self.target = target

                def start(self):
                    started.append(self.target)

            shared._spawn_missing_agent_bridges(thread_factory=FakeThread)
            self.assertEqual(list(shared._agent_bridges), ['Avery'])
            self.assertEqual(len(started), 1)
            shared._spawn_missing_agent_bridges(thread_factory=FakeThread)
            self.assertEqual(len(started), 1)


class VoiceCallCommandTests(unittest.TestCase):
    def _interaction(self, name, options):
        return {'type': 2, 'id': '111', 'token': 'itoken', 'channel_id': STAFF_CHANNEL,
                'guild_id': '777777777777777777', 'member': {'user': {'id': '123', 'bot': False}},
                'data': {'name': name, 'options': options}}

    def test_join_and_leave_voice_route_to_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            bridge = CapturingBridge(root, FakeChat([]))

            class FakeSupervisor:
                def __init__(self):
                    self.joins = []
                    self.leaves = []

                def request_join(self, agent, guild, channel, text_channel=None):
                    self.joins.append((agent, guild, channel, text_channel))

                def request_leave(self, agent):
                    self.leaves.append(agent)

            supervisor = FakeSupervisor()
            bridge.voice_supervisor = lambda: supervisor
            bridge.handle_interaction(self._interaction('join-voice', [
                {'name': 'agent', 'value': 'Avery'},
                {'name': 'channel', 'value': VOICE_CHANNEL}]), SHARED_TOKEN)
            self.assertEqual(supervisor.joins, [('Avery', '777777777777777777', VOICE_CHANNEL, STAFF_CHANNEL)])
            followups = [c for c in bridge.calls if c['path'].startswith('/webhooks/999/')]
            self.assertIn('is joining', followups[-1]['json']['content'])
            bridge.handle_interaction(self._interaction('leave-voice', [
                {'name': 'agent', 'value': 'Avery'}]), SHARED_TOKEN)
            self.assertEqual(supervisor.leaves, ['Avery'])

    def test_join_voice_without_token_is_refused_before_supervisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp, avery_token=False)
            bridge = CapturingBridge(root, FakeChat([]))

            class FakeSupervisor:
                def __init__(self):
                    self.joins = []

                def request_join(self, *args, **kwargs):
                    self.joins.append(args)

            supervisor = FakeSupervisor()
            bridge.voice_supervisor = lambda: supervisor
            bridge.handle_interaction(self._interaction('join-voice', [
                {'name': 'agent', 'value': 'Avery'},
                {'name': 'channel', 'value': VOICE_CHANNEL}]), SHARED_TOKEN)
            self.assertEqual(supervisor.joins, [])
            followups = [c for c in bridge.calls if c['path'].startswith('/webhooks/999/')]
            self.assertIn('does not have their own Discord bot token', followups[-1]['json']['content'])

    def test_call_commands_are_registered_with_agent_choices(self):
        names = [c['name'] for c in DISCORD_COMMANDS]
        self.assertIn('join-voice', names)
        self.assertIn('leave-voice', names)
        join = [c for c in DISCORD_COMMANDS if c['name'] == 'join-voice'][0]
        agent_option = [o for o in join['options'] if o['name'] == 'agent'][0]
        self.assertEqual([c['value'] for c in agent_option['choices']],
                         ['Morgan', 'Avery', 'Jordan', 'Cameron'])
        channel_option = [o for o in join['options'] if o['name'] == 'channel'][0]
        self.assertEqual(channel_option['type'], 7)
        self.assertEqual(channel_option['channel_types'], [2])


class InteractionRequestIdTests(unittest.TestCase):
    """Slash-command turns must pass agent_chat.ask's request_id check:
    a UUID or 'discord:<digits>'. The old 'discord:interaction:<id>'
    format failed validation, so every slash chat command errored."""

    def test_interaction_request_id_is_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            chat = FakeChat(['hello'])
            bridge = CapturingBridge(root, chat)
            interaction = {'type': 2, 'id': '111', 'token': 'itoken',
                           'channel_id': STAFF_CHANNEL,
                           'guild_id': '777777777777777777',
                           'member': {'user': {'id': '123', 'bot': False}},
                           'data': {'name': 'morgan',
                                    'options': [{'name': 'message', 'value': 'hi'}]}}
            bridge.handle_interaction(interaction, SHARED_TOKEN)
            self.assertEqual(chat.calls[0]['request_id'], 'discord:111')
            self.assertIsNotNone(re.fullmatch(
                r'(?:[a-f0-9-]{36}|discord:\d+)', chat.calls[0]['request_id']))


class VoiceSessionStartTests(unittest.TestCase):
    """VoiceSession.start() must reach channel.connect() even though
    discord.py Client.start() blocks until the client closes. The old
    code awaited start() inline, so connect() never ran and agents
    never joined a voice channel (with no error anywhere)."""

    def test_start_connects_even_when_client_start_blocks(self):
        import discord

        class FakeVC:
            def __init__(self):
                self.listened = False

            def listen(self, sink):
                self.listened = True

            def is_playing(self):
                return False

            def stop_playing(self):
                pass

            def stop_listening(self):
                pass

            async def disconnect(self):
                pass

        class FakeChannel:
            def __init__(self):
                self.connected_cls = None
                self.vc = FakeVC()

            async def connect(self, cls=None):
                self.connected_cls = cls
                return self.vc

        class FakeClient:
            def __init__(self, intents=None):
                self.started = False
                self.closed = False
                self.channel = FakeChannel()

            def add_listener(self, *args):
                pass

            async def login(self, token):
                self.started = True

            async def connect(self):
                await asyncio.Event().wait()

            async def wait_until_ready(self):
                await asyncio.sleep(0)

            def get_channel(self, channel_id):
                return self.channel

            async def fetch_channel(self, channel_id):
                return self.channel

            async def close(self):
                self.closed = True

        class FakeSupervisor:
            def __init__(self, tmp):
                self.root = Path(tmp)
                self.chat = FakeChat([])
                self.loop = asyncio.new_event_loop()
                self.sessions = {}

            def token(self, agent):
                return 'tok'

        async def run_case(tmp):
            supervisor = FakeSupervisor(tmp)
            session = VoiceSession(supervisor, 'Avery', '777', VOICE_CHANNEL, STAFF_CHANNEL)
            with patch('discord.Client', FakeClient), \
                    patch('discord.opus.is_loaded', return_value=True):
                await asyncio.wait_for(session.start(), timeout=5)
                await asyncio.sleep(0)
                self.assertTrue(session.client.started)
                self.assertIs(session.vc, session.client.channel.vc)
                self.assertTrue(session.vc.listened)
                self.assertEqual(session.client.channel.connected_cls.__name__,
                                 'VoiceRecvClient')
                await session.leave()
                self.assertTrue(session.client.closed)

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run_case(tmp))


class ConversationFollowerTests(unittest.TestCase):
    """Topic routing + last-agent memory for shared channels."""

    def _bridge(self, tmp):
        root = _root_with_env(tmp)
        return CapturingBridge(root, FakeChat([])), root

    def test_refund_policy_routes_to_cameron(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, _root = self._bridge(tmp)
            agent, body = bridge.route_agent('Refund policy done?', STAFF_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Cameron')
            self.assertEqual(body, 'Refund policy done?')

    def test_site_routes_to_jordan_and_design_to_avery(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, _root = self._bridge(tmp)
            agent, _body = bridge.route_agent('Can you check the site?', STAFF_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Jordan')
            agent, _body = bridge.route_agent('Make a tumbler wrap mockup', STAFF_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Avery')

    def test_tie_falls_back_to_default(self):
        # "site launch" hits Jordan (site) and Morgan (launch) equally.
        with tempfile.TemporaryDirectory() as tmp:
            bridge, _root = self._bridge(tmp)
            agent, _body = bridge.route_agent('How is the site launch going?', STAFF_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Morgan')

    def test_name_prefix_beats_topic_and_channel_beats_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, _root = self._bridge(tmp)
            agent, body = bridge.route_agent('Morgan: refund policy?', STAFF_CHANNEL, user_id='u1')
            self.assertEqual((agent, body), ('Morgan', 'refund policy?'))
            agent, _body = bridge.route_agent('refund policy?', AVERY_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Avery')

    def test_word_boundaries_do_not_false_positive(self):
        # "artwork" must not trip Morgan's "work"; it is Avery's via artwork.
        with tempfile.TemporaryDirectory() as tmp:
            bridge, _root = self._bridge(tmp)
            agent, _body = bridge.route_agent('Check the artwork', STAFF_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Avery')

    def test_follower_keeps_last_agent_then_topic_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, _root = self._bridge(tmp)
            agent, _body = bridge.route_agent('Refund policy done?', STAFF_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Cameron')
            agent, _body = bridge.route_agent('And the shipping?', STAFF_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Cameron')
            agent, _body = bridge.route_agent('How is the site?', STAFF_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Jordan')
            agent, _body = bridge.route_agent('Thanks', STAFF_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Jordan')

    def test_follower_expires(self):
        with tempfile.TemporaryDirectory() as tmp:
            bridge, root = self._bridge(tmp)
            bridge.route_agent('Refund policy done?', STAFF_CHANNEL, user_id='u1')
            follower = Path(root) / 'work' / 'discord-follower.json'
            data = json.loads(follower.read_text())
            for entry in data.values():
                entry['ts'] = time.time() - 4000
            follower.write_text(json.dumps(data))
            agent, _body = bridge.route_agent('Thanks', STAFF_CHANNEL, user_id='u1')
            self.assertEqual(agent, 'Morgan')

    def test_voice_command_routes_by_its_message_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _root_with_env(tmp)
            bridge = CapturingBridge(root, FakeChat(['Refund policy is ready.']))
            data = {
                'type': 2, 'id': '777', 'token': 'tok',
                'data': {'name': 'voice', 'options': [{'name': 'message', 'value': 'Refund policy done?'}]},
                'channel_id': STAFF_CHANNEL,
                'member': {'user': {'id': 'u1'}},
            }
            with patch('staff.voice.speak', side_effect=_speak_stub):
                bridge.handle_interaction(data)
            self.assertEqual(bridge.chat.calls[0]['agent'], 'Cameron')
