"""Telegram voice, poll, and photo delivery: unit tests (no network)."""
import io
import json
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from staff.voice import chunk_text, load_profiles, tune_voice
from staff.agent_chat import TelegramBridge, VOICES
from staff.media_tools import convert_audio


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


if __name__ == '__main__':
    unittest.main()
