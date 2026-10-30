"""Telegram voice, poll, and photo delivery: unit tests (no network)."""
import io
import json
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from staff.voice import chunk_text, load_profiles, tune_voice, log_voice_send
from staff.agent_chat import TelegramBridge, VOICES, AgentChat, parse_voice_command
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


class TelegramDeliveryTests(unittest.TestCase):
    def test_send_file_uses_photo_for_images(self):
        import tempfile
        bridge = TelegramBridge(Path('/tmp'), None, None, agent='Jordan')
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'pic.png'
            image.write_bytes(b'\x89PNG')
            doc = Path(tmp) / 'doc.pdf'
            doc.write_bytes(b'%PDF')
            seen = {}

            class Resp:
                ok = True

            def fake_post(url, data=None, files=None, timeout=None):
                seen.setdefault('urls', []).append(url)
                seen.setdefault('fields', []).append(list((files or {}).keys()))
                return Resp()

            with patch('staff.agent_chat.requests.post', side_effect=fake_post):
                bridge.send_file('token', {'chat': {'id': 1}}, image, 'pic.png')
                bridge.send_file('token', {'chat': {'id': 1}}, doc, 'doc.pdf')
        self.assertIn('sendPhoto', seen['urls'][0])
        self.assertEqual(seen['fields'][0], ['photo'])
        self.assertIn('sendDocument', seen['urls'][1])
        self.assertEqual(seen['fields'][1], ['document'])

    def test_send_poll_validation_without_network(self):
        bridge = TelegramBridge(Path('/tmp'), None, None, agent='Morgan')
        with self.assertRaises(ValueError):
            bridge.send_poll('token', '1', 'Question?', ['only-one'])
        with self.assertRaises(ValueError):
            bridge.send_poll('token', '1', 'Question?', ['a', 'a'])
        with self.assertRaises(ValueError):
            bridge.send_poll('token', '1', '', ['a', 'b'])

    def test_poll_answer_is_recorded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'work').mkdir()
            (root / 'work' / 'telegram-polls.json').write_text(json.dumps([
                {'poll_id': 'p1', 'question': 'Ship it?', 'options': ['Yes', 'No'], 'agent': 'Morgan'}]))
            bridge = TelegramBridge(root, None, None, agent='Morgan')
            bridge.record_poll_answer({'poll_id': 'p1', 'user': {'id': 7}, 'option_ids': [0]})
            line = (root / 'work' / 'telegram-poll-answers.jsonl').read_text().strip()
            record = json.loads(line)
            self.assertEqual(record['options'], ['Yes'])
            self.assertEqual(record['question'], 'Ship it?')


class VoiceLogTests(unittest.TestCase):
    def test_log_appends_send_records(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log_voice_send(root, 'Jordan', 'group-voice', '-1001', 'Hello group', 3.5)
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
            log_voice_send(root, 'Avery', 'group-voice', '-1001', 'newest', 1.0)
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
                    return '{"action": "telegram_voice", "arguments": {"text": "hello group"}}'
                return '{"answer": "Hello content"}'

        class FakeActions:
            def __init__(self, root, agent, request_id, delete_ids=None):
                pass

            def execute(self, name, args):
                self.last = (name, args)
                if name == 'telegram_voice':
                    return {'result': 'Voice message sent as Jordan to the staff Telegram group.'}
                return {'error': 'unknown action'}

        with tempfile.TemporaryDirectory() as tmp:
            chat = AgentChat(Path(tmp))
            fake = FakeLLM()
            messages = [{'role': 'system', 'content': 'sys'},
                        {'role': 'user', 'content': 'send a voice message to the group saying hello'}]
            with patch('staff.agent_actions.Actions', FakeActions):
                answer = chat.act('Jordan', messages, 'test-1', llm=fake, source='dashboard')
        self.assertEqual(answer, 'Hello content')
        self.assertNotIn('telegram_voice', answer)
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
                {'action': 'telegram_voice', 'status': 'complete',
                 'summary': 'Voice message sent as Jordan to the staff Telegram group.'},
                {'action': 'product_create', 'status': 'complete', 'summary': 'Widget staged'},
            ]}))
            context = tool_results_context(root)
        self.assertIn('product_create', context)
        self.assertIn('Widget staged', context)
        self.assertNotIn('telegram_voice', context)
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
                                  'telegram:123', source='telegram', no_tools=True)
        self.assertEqual(act_calls, [])
        self.assertEqual(answer, 'Hello by voice')


class RegisterCommandsTests(unittest.TestCase):
    def test_voice_command_is_published(self):
        bridge = TelegramBridge(Path('/tmp'), None, None, agent='Cameron')
        seen = {}
        bridge.api = lambda token, method, data: seen.setdefault('call', (token, method, data))
        bridge.register_commands('tok')
        token, method, data = seen['call']
        self.assertEqual(token, 'tok')
        self.assertEqual(method, 'setMyCommands')
        names = [c['command'] for c in data['commands']]
        self.assertIn('voice', names)
        self.assertIn('pair', names)

    def test_general_bot_also_lists_agents(self):
        bridge = TelegramBridge(Path('/tmp'), None, None, agent='')
        seen = {}
        bridge.api = lambda token, method, data: seen.setdefault('call', (token, method, data))
        bridge.register_commands('tok')
        names = [c['command'] for c in seen['call'][2]['commands']]
        self.assertIn('voice', names)
        self.assertIn('morgan', names)


if __name__ == '__main__':
    unittest.main()
