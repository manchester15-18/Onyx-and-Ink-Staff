"""Voice profiles, rendering, logging, and Discord voice flows: unit tests (no network)."""
import io
import json
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from staff.voice import chunk_text, load_profiles, tune_voice, log_voice_send
from staff.agent_chat import VOICES, AgentChat, parse_voice_command, is_truncated_reply
from staff.media_tools import convert_audio
from staff.tool_tasks import tool_results_context


def _tiny_wav():
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as clip:
        clip.setnchannels(1)
        clip.setsampwidth(2)
        clip.setframerate(16000)
        clip.writeframes(b'\x00\x00' * 1600)
    return buf.getvalue()


class VoiceProfileTests(unittest.TestCase):
    def test_genders_and_aave_direction(self):
        profiles = load_profiles(Path('/tmp'))
        self.assertEqual(profiles['Morgan']['gender'], 'female')
        for agent in ('Avery', 'Jordan', 'Cameron'):
            self.assertEqual(profiles[agent]['gender'], 'male')
        for agent in ('Morgan', 'Avery', 'Jordan', 'Cameron'):
            self.assertIn('AAVE', profiles[agent]['direction'])

    def test_voice_rule_in_persona(self):
        for agent in ('Morgan', 'Avery', 'Jordan', 'Cameron'):
            self.assertIn('never cuss at the CEO', VOICES[agent])
            self.assertIn('Vice President', VOICES[agent])

    def test_tune_appends_and_caps(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'work').mkdir()
            for i in range(22):
                tune_voice(root, 'Morgan', 'note %d' % i)
            notes = load_profiles(root)['Morgan']['notes']
            self.assertEqual(len(notes), 20)
            self.assertEqual(notes[-1], 'note 21')
            with self.assertRaises(ValueError):
                tune_voice(root, 'Nobody', 'x')

    def test_chunking_respects_sentence_boundaries(self):
        text = 'Hello world. ' * 100
        chunks = chunk_text(text)
        self.assertTrue(all(len(c) <= 900 for c in chunks))
        self.assertTrue(all(c.endswith('.') for c in chunks))
        self.assertEqual(' '.join(chunks), ' '.join(text.split()))


class AudioConvertTests(unittest.TestCase):
    def test_wav_to_ogg_opus(self):
        data = convert_audio(_tiny_wav(), 'ogg')
        self.assertTrue(data.startswith(b'OggS'))
        self.assertGreater(len(data), 0)


class VoiceLogTests(unittest.TestCase):
    def test_log_appends_send_records(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_voice_send(root, 'Jordan', 'group-voice', '123456789012345678', 'Hello group', 3.5)
            log_voice_send(root, 'Morgan', 'voice-reply', '42', 'Hello back', 2.0)
            lines = (root / 'work' / 'voice-messages.jsonl').read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertEqual(first['agent'], 'Jordan')
            self.assertEqual(first['kind'], 'group-voice')
            self.assertEqual(first['text'], 'Hello group')
            self.assertEqual(first['duration_seconds'], 3.5)

    def test_log_trims_to_last_thousand(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'work').mkdir()
            (root / 'work' / 'voice-messages.jsonl').write_text(
                '\n'.join('{"n": %d}' % i for i in range(1000)) + '\n')
            log_voice_send(root, 'Avery', 'group-voice', '123456789012345678', 'newest', 1.0)
            lines = (root / 'work' / 'voice-messages.jsonl').read_text().strip().splitlines()
            self.assertEqual(len(lines), 1000)
            self.assertEqual(json.loads(lines[-1])['text'], 'newest')


class ActVoiceSuppressionTests(unittest.TestCase):
    def test_voice_send_is_not_narrated_back(self):
        import tempfile

        class FakeLLM:
            def __init__(self):
                self.calls = []

            def call(self, messages):
                self.calls.append([dict(m) for m in messages])
                if len(self.calls) == 1:
                    return '{"action": "discord_voice", "arguments": {"text": "hello group"}}'
                return '{"answer": "Hello content"}'

        class FakeActions:
            def __init__(self, root, agent, request_id, delete_ids=None):
                pass

            def execute(self, name, args):
                self.last = (name, args)
                if name == 'discord_voice':
                    return {'result': 'Voice message sent as Jordan to the Discord staff channel.'}
                return {'error': 'unknown action'}

        with tempfile.TemporaryDirectory() as tmp:
            chat = AgentChat(Path(tmp))
            fake = FakeLLM()
            messages = [{'role': 'system', 'content': 'sys'},
                        {'role': 'user', 'content': 'send a voice message to the group saying hello'}]
            with patch('staff.agent_actions.Actions', FakeActions):
                answer = chat.act('Jordan', messages, 'test-1', llm=fake, source='dashboard')
        self.assertEqual(answer, 'Hello content')
        self.assertNotIn('discord_voice', answer)
        self.assertNotIn('Voice message sent', answer)
        self.assertNotIn('Action results', answer)
        second_call_text = ' '.join(str(m.get('content', '')) for m in fake.calls[1])
        self.assertIn('Do not announce', second_call_text)


class ContextExclusionTests(unittest.TestCase):
    def test_voice_results_stay_out_of_report_context(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'work').mkdir()
            (root / 'work' / 'tool-task-results.json').write_text(json.dumps({'results': [
                {'action': 'discord_voice', 'status': 'complete',
                 'summary': 'Voice message sent as Jordan to the Discord staff channel.'},
                {'action': 'product_create', 'status': 'complete', 'summary': 'Widget staged'},
            ]}))
            context = tool_results_context(root)
        self.assertIn('product_create', context)
        self.assertIn('Widget staged', context)
        self.assertNotIn('discord_voice', context)
        self.assertNotIn('Voice message sent', context)


class VoiceCommandParseTests(unittest.TestCase):
    def test_private_voice_prefix_is_stripped(self):
        self.assertEqual(parse_voice_command('/voice hello', False), (True, 'hello'))
        self.assertEqual(parse_voice_command('/voice hello', True), (True, 'hello'))

    def test_group_prestripped_text_stays_a_voice_turn(self):
        self.assertEqual(parse_voice_command('hello', True), (True, 'hello'))

    def test_plain_text_is_not_a_voice_turn(self):
        self.assertEqual(parse_voice_command('hello', False), (False, 'hello'))

    def test_bare_voice_command_and_case(self):
        self.assertEqual(parse_voice_command('/voice', False), (True, ''))
        self.assertEqual(parse_voice_command('/VOICE Hey', False), (True, 'Hey'))


class VoiceNoToolsTests(unittest.TestCase):
    def test_voice_turn_bypasses_the_tool_loop(self):
        import tempfile

        class FakeLLM:
            def call(self, messages):
                return 'Hello by voice'

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.env').write_text('AGENT_TOOLS_ENABLED=true\n')
            chat = AgentChat(root)
            chat.get_llm = lambda cfg, agent='Cameron': FakeLLM()
            act_calls = []
            with patch.object(AgentChat, 'act', side_effect=lambda *a, **k: act_calls.append(1)):
                answer = chat.ask('Cameron', 'message Morgan about the launch',
                                  'discord:123', source='discord', no_tools=True)
        self.assertEqual(act_calls, [])
        self.assertEqual(answer, 'Hello by voice')


class GeminiSpeakTests(unittest.TestCase):
    def test_profiles_carry_designed_voice_ids(self):
        profiles = load_profiles(Path('/tmp'))
        self.assertEqual(profiles['Morgan']['gemini_voice_id'], 'voice_tmaxr1l2l0kt')
        self.assertEqual(profiles['Avery']['gemini_voice_id'], 'voice_aho1ngj4ul5p')
        self.assertEqual(profiles['Jordan']['gemini_voice_id'], 'voice_qqnd9kl9ef9s')
        self.assertEqual(profiles['Cameron']['gemini_voice_id'], 'voice_90zh7jkztuox')

    def _gemini_response(self):
        import base64
        payload = {'candidates': [{'content': {'parts': [{'inlineData': {
            'mimeType': 'audio/wav',
            'data': base64.b64encode(_tiny_wav()).decode()}}]}}]}
        return json.dumps(payload).encode()

    def _fake_urlopen(self, seen):
        import urllib.request

        class FakeResp:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return self.payload

        def fake_urlopen(req, timeout=None):
            seen.append(req.data)
            return FakeResp(self._gemini_response())

        return fake_urlopen

    def test_gemini_path_renders_designed_voice_ogg(self):
        import tempfile
        import urllib.request
        from staff.voice import speak
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch('staff.voice.urllib.request.urlopen',
                       side_effect=self._fake_urlopen(seen)):
                result = speak(Path(tmp), 'Morgan', 'Hello there.')
        self.assertTrue(result['data'].startswith(b'OggS'))
        self.assertEqual(result['format'], 'OGG')
        self.assertEqual(result['provider'], 'Gemini text-to-speech')
        self.assertIn('voice_tmaxr1l2l0kt', seen[0].decode())

    def test_profile_file_override_of_voice_id_is_honored(self):
        import tempfile
        from staff.voice import speak
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'work').mkdir()
            (root / 'work' / 'agent-voices.json').write_text(json.dumps(
                {'Morgan': {'gemini_voice_id': 'voice_override123'}}))
            with patch('staff.voice.urllib.request.urlopen',
                       side_effect=self._fake_urlopen(seen)):
                result = speak(root, 'Morgan', 'Hello there.')
        self.assertEqual(result['provider'], 'Gemini text-to-speech')
        self.assertIn('voice_override123', seen[0].decode())

    def test_gemini_failure_falls_back_to_melotts(self):
        import tempfile
        import urllib.error
        from staff.voice import speak
        with tempfile.TemporaryDirectory() as tmp:
            with patch('staff.voice.urllib.request.urlopen',
                       side_effect=urllib.error.URLError('down')), \
                    patch('staff.media_tools.generate_audio',
                          return_value={'data': _tiny_wav(), 'duration_seconds': 0.1}):
                result = speak(Path(tmp), 'Jordan', 'Hello there.')
        self.assertTrue(result['data'].startswith(b'OggS'))
        self.assertEqual(result['provider'], 'Cloudflare Workers AI text-to-speech')

    def test_missing_key_falls_back_to_melotts(self):
        import tempfile
        from staff.voice import speak
        with tempfile.TemporaryDirectory() as tmp:
            with patch('staff.voice._gemini_api_key', return_value=''), \
                    patch('staff.media_tools.generate_audio',
                          return_value={'data': _tiny_wav(), 'duration_seconds': 0.1}):
                result = speak(Path(tmp), 'Avery', 'Hello there.')
        self.assertEqual(result['provider'], 'Cloudflare Workers AI text-to-speech')


if __name__ == '__main__':
    unittest.main()


class TruncatedReplyTests(unittest.TestCase):
    def test_exact_intro_fragments_are_truncated(self):
        self.assertTrue(is_truncated_reply("Hey Jauneé, good to meet you. I'm Avery,"))
        self.assertTrue(is_truncated_reply('e? Welcome aboard, truly glad to'))

    def test_complete_replies_are_not_truncated(self):
        self.assertFalse(is_truncated_reply('Hey Jauneé, good to meet you. I am Avery, the marketing lead.'))
        self.assertFalse(is_truncated_reply('Copy that.'))
        self.assertFalse(is_truncated_reply('Welcome aboard, truly glad to have you here.'))
        self.assertFalse(is_truncated_reply('Thanks!'))
        self.assertFalse(is_truncated_reply('Hello there'))

    def test_punctuation_rules(self):
        self.assertTrue(is_truncated_reply(''))
        self.assertTrue(is_truncated_reply('Wait,'))
        self.assertTrue(is_truncated_reply('I was going to'))
        self.assertTrue(is_truncated_reply("I'm not"))
        self.assertFalse(is_truncated_reply('Done…'))


class VoiceLogProviderTests(unittest.TestCase):
    def test_provider_recorded_when_given(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_voice_send(root, 'Avery', 'voice-reply', '123456789012345678', 'Hello', 2.0,
                           provider='Gemini text-to-speech')
            record = json.loads((root / 'work' / 'voice-messages.jsonl').read_text().strip())
            self.assertEqual(record['provider'], 'Gemini text-to-speech')

    def test_provider_absent_when_not_given(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_voice_send(root, 'Avery', 'voice-reply', '123456789012345678', 'Hello', 2.0)
            record = json.loads((root / 'work' / 'voice-messages.jsonl').read_text().strip())
            self.assertNotIn('provider', record)
