"""Discord gateway lifecycle: a closed socket must end the read loop.

Regression test for the Oct 31, 2026 shared-gateway spin: Discord
closes the socket (bad token, deleted app), recv() then raises on
every call, and the old bare `except Exception: continue` spun at
full CPU forever while the state file stayed frozen at
connected=True. The fixed loop records connected=False and breaks
out so the outer loop retries cleanly. Also pins .gif as an
accepted inbound attachment suffix.

Offline: the websocket module is stubbed, no token values leave
the test, no network is touched.
"""
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from staff.discord_bridge import DiscordBridge, INBOUND_SUFFIXES


class FakeWebSocket:
    """First recv returns the gateway hello, then the socket is closed."""

    def __init__(self):
        self.sent = []
        self.closed = 0
        self._calls = 0

    def send(self, data):
        self.sent.append(data)

    def settimeout(self, timeout):
        pass

    def recv(self):
        self._calls += 1
        if self._calls == 1:
            return json.dumps({'d': {'heartbeat_interval': 1000}})
        from websocket import WebSocketConnectionClosedException
        raise WebSocketConnectionClosedException()

    def close(self):
        self.closed += 1


class GatewayCloseTests(unittest.TestCase):
    def test_closed_socket_records_disconnected_and_breaks_loop(self):
        from websocket import WebSocketConnectionClosedException
        fake_ws = FakeWebSocket()
        stub = types.ModuleType('websocket')
        stub.create_connection = lambda *args, **kwargs: fake_ws
        stub.WebSocketConnectionClosedException = WebSocketConnectionClosedException
        real_ws = sys.modules.get('websocket')
        sys.modules['websocket'] = stub
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / 'work').mkdir()
                (root / '.env').write_text(
                    'DISCORD_ENABLED=true\nDISCORD_BOT_TOKEN=dummy-shared\n'
                    'DISCORD_STAFF_CHANNEL_ID=111111111111111111\n')
                stop = threading.Event()
                bridge = DiscordBridge(root, None, stop)
                bridge._registered_token = 'dummy-shared'
                thread = threading.Thread(target=bridge.run, daemon=True)
                thread.start()
                deadline = time.time() + 12
                state = {}
                while time.time() < deadline:
                    try:
                        state = json.loads((root / 'work' / 'discord-state.json').read_text())
                    except (OSError, ValueError):
                        state = {}
                    if state.get('connected') is False and state.get('lastError'):
                        break
                    time.sleep(0.2)
                stop.set()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive(), 'gateway thread did not exit')
                self.assertFalse(state.get('connected'), 'closed socket must clear connected')
                self.assertIn('closed', state.get('lastError', '').lower())
                self.assertGreaterEqual(fake_ws.closed, 1, 'socket should be closed')
        finally:
            if real_ws is None:
                sys.modules.pop('websocket', None)
            else:
                sys.modules['websocket'] = real_ws

    def test_gif_is_an_accepted_inbound_suffix(self):
        self.assertIn('.gif', INBOUND_SUFFIXES)


STAFF_CH = '111111111111111111'
AVERY_CH = '222222222222222222'
MORGAN_CH = '333333333333333333'
THREAD_AVERY = '444444444444444444'
THREAD_FLOOR = '555555555555555555'


def _rebuild_root(tmp, morgan=True, avery=True, shared=True, jordan=False):
    root = Path(tmp)
    (root / 'work').mkdir(parents=True)
    env = ('DISCORD_ENABLED=true\nDISCORD_STAFF_CHANNEL_ID=%s\n'
           'DISCORD_AVERY_CHANNEL_ID=%s\nDISCORD_MORGAN_CHANNEL_ID=%s\n'
           % (STAFF_CH, AVERY_CH, MORGAN_CH))
    if shared:
        env += 'DISCORD_BOT_TOKEN=dummy-shared\n'
    if morgan:
        env += 'DISCORD_MORGAN_BOT_TOKEN=dummy-morgan\n'
    if avery:
        env += 'DISCORD_AVERY_BOT_TOKEN=dummy-avery\n'
    if jordan:
        env += 'DISCORD_JORDAN_BOT_TOKEN=dummy-jordan\n'
    (root / '.env').write_text(env)
    return root


def _rebuild_message(channel, text, stickers=None):
    data = {'id': '555', 'author': {'id': '123', 'bot': False},
            'channel_id': channel, 'content': text, 'attachments': [],
            'guild_id': '777'}
    if stickers:
        data['sticker_items'] = stickers
    return data


class RebuildFakeChat:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def ask(self, agent, body, request_id, source='discord', attachments=None, no_tools=False):
        self.calls.append({'agent': agent, 'body': body, 'request_id': request_id,
                           'source': source, 'attachments': attachments, 'no_tools': no_tools})
        return self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]


class RebuildCapturingBridge(DiscordBridge):
    def __init__(self, root, chat, stop=None, scope='', channel_map=None):
        super().__init__(root, chat, stop, scope=scope)
        self.calls = []
        self.channel_map = channel_map or {}

    def api(self, method, path, token, **kwargs):
        self.calls.append({'method': method, 'path': path, 'token': token,
                           'json': kwargs.get('json'), 'files': bool(kwargs.get('files'))})
        if method == 'GET' and path.startswith('/channels/') and path.count('/') == 2:
            return {'parent_id': self.channel_map.get(path.split('/')[-1])}
        if path == '/users/@me':
            return {'id': '123', 'username': 'ScopedBot'}
        if path == '/applications/@me':
            return {'id': '999'}
        if method == 'GET' and path.startswith('/guilds/') and path.endswith('/stickers'):
            return [{'id': '99', 'name': 'Wave'}, {'id': '100', 'name': 'Party'}]
        if method == 'GET' and '/messages/' in path:
            return {'poll': {'question': {'text': 'Lunch?'},
                             'answers': [{'answer_id': 1, 'poll_media': {'text': 'Tacos'}},
                                         {'answer_id': 2, 'poll_media': {'text': 'Salad'}}],
                             'results': {'is_finalized': False,
                                         'answer_counts': [{'id': 1, 'count': 3},
                                                           {'id': 2, 'count': 1}]}}}
        return {}


class RebuildOwnershipTests(unittest.TestCase):
    def test_thread_parent_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp)
            channel_map = {THREAD_AVERY: AVERY_CH, THREAD_FLOOR: STAFF_CH}
            avery = RebuildCapturingBridge(root, None, scope='Avery', channel_map=channel_map)
            morgan = RebuildCapturingBridge(root, None, scope='Morgan', channel_map=channel_map)
            shared = RebuildCapturingBridge(root, None, channel_map=channel_map)
            self.assertTrue(avery.owns_message(_rebuild_message(THREAD_AVERY, 'Hi')))
            self.assertFalse(morgan.owns_message(_rebuild_message(THREAD_AVERY, 'Hi')))
            self.assertTrue(morgan.owns_message(_rebuild_message(THREAD_FLOOR, 'Hi')))
            self.assertFalse(avery.owns_message(_rebuild_message(THREAD_FLOOR, 'Hi')))
            self.assertFalse(shared.owns_message(_rebuild_message(THREAD_AVERY, 'Hi')))
            gets = [c for c in avery.calls if c['method'] == 'GET' and '/channels/' in c['path']]
            self.assertEqual(len(gets), 2, 'thread parents must be cached per bridge')

    def test_floor_and_fallback_belong_to_morgan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp, avery=False)
            morgan = RebuildCapturingBridge(root, None, scope='Morgan')
            shared = RebuildCapturingBridge(root, None)
            self.assertTrue(morgan.owns_message(_rebuild_message(AVERY_CH, 'Hi')))
            self.assertFalse(shared.owns_message(_rebuild_message(AVERY_CH, 'Hi')))
            self.assertTrue(morgan.owns_message(_rebuild_message(STAFF_CH, 'Hi')))
            self.assertFalse(shared.owns_message(_rebuild_message(STAFF_CH, 'Hi')))
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp, avery=True)
            morgan = RebuildCapturingBridge(root, None, scope='Morgan')
            self.assertFalse(morgan.owns_message(_rebuild_message(AVERY_CH, 'Hi')))

    def test_thread_parent_cache_attribute_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp)
            bridge = RebuildCapturingBridge(root, None)
            self.assertEqual(bridge._thread_parents, {})


class RebuildIntakeTests(unittest.TestCase):
    def test_sticker_note_and_emoji_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp)
            chat = RebuildFakeChat(['Hello back.'])
            bridge = RebuildCapturingBridge(root, chat, scope='Avery')
            bridge.handle_message(_rebuild_message(AVERY_CH, '<:party:123> hello',
                                                  stickers=[{'name': 'Wave'}]), 'dummy-avery')
            self.assertEqual(len(chat.calls), 1)
            body = chat.calls[0]['body']
            self.assertIn(':party:', body)
            self.assertNotIn('<:party:', body)
            self.assertIn('Wave', body)
            chat2 = RebuildFakeChat(['Nice sticker.'])
            bridge2 = RebuildCapturingBridge(root, chat2, scope='Avery')
            bridge2.handle_message(_rebuild_message(AVERY_CH, '', stickers=[{'name': 'Wave'}]),
                                   'dummy-avery')
            self.assertEqual(len(chat2.calls), 1)
            self.assertIn('Wave', chat2.calls[0]['body'])


class RebuildFeatureTests(unittest.TestCase):
    def test_poll_create_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp)
            bridge = RebuildCapturingBridge(root, None)
            bridge.create_poll(STAFF_CH, 'tok', 'Lunch?', ['Tacos', 'Salad'], 24)
            post = [c for c in bridge.calls if c['method'] == 'POST'][-1]
            poll = post['json']['poll']
            self.assertEqual(poll['question']['text'], 'Lunch?')
            self.assertEqual([a['answer_id'] for a in poll['answers']], [1, 2])
            self.assertEqual(poll['duration'], 24)
            with self.assertRaises(ValueError):
                bridge.create_poll(STAFF_CH, 'tok', 'Lunch?', ['Only one'], 24)
            result = bridge.read_poll(STAFF_CH, '555', 'tok')
            self.assertEqual(result['question'], 'Lunch?')
            self.assertEqual([(a['id'], a['text'], a['votes']) for a in result['answers']],
                             [(1, 'Tacos', 3), (2, 'Salad', 1)])
            self.assertFalse(result['finalized'])

    def test_react_and_sticker_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp)
            bridge = RebuildCapturingBridge(root, None)
            bridge.react(STAFF_CH, '555', '👍', 'tok')
            put = [c for c in bridge.calls if c['method'] == 'PUT'][-1]
            self.assertIn('/reactions/', put['path'])
            self.assertTrue(put['path'].endswith('/@me'))
            bridge.send_sticker(STAFF_CH, 'tok', '777', 'Wave')
            post = [c for c in bridge.calls if c['method'] == 'POST'][-1]
            self.assertEqual(post['json']['sticker_ids'], ['99'])
            with self.assertRaises(ValueError):
                bridge.send_sticker(STAFF_CH, 'tok', '777', 'Nope')

    def test_scoped_check_uses_scoped_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp, jordan=True)
            jordan = RebuildCapturingBridge(root, None, scope='Jordan')
            identity = jordan.check(None)
            self.assertEqual(identity['name'], 'ScopedBot')
            get = [c for c in jordan.calls if c['path'] == '/users/@me'][-1]
            self.assertEqual(get['token'], 'dummy-jordan')
            shared = RebuildCapturingBridge(root, None)
            shared.check(None)
            get = [c for c in shared.calls if c['path'] == '/users/@me'][-1]
            self.assertEqual(get['token'], 'dummy-shared')
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp, jordan=False)
            jordan = RebuildCapturingBridge(root, None, scope='Jordan')
            with self.assertRaises(ValueError):
                jordan.check(None)

    def test_send_uses_sender_token_without_shared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp, shared=False, avery=True)
            bridge = RebuildCapturingBridge(root, None)
            result = bridge.send('Avery', 'hi')
            self.assertIn('staff-floor', result['result'])
            post = [c for c in bridge.calls if c['method'] == 'POST'][-1]
            self.assertEqual(post['token'], 'dummy-avery')
        with tempfile.TemporaryDirectory() as tmp:
            root = _rebuild_root(tmp, shared=False, morgan=False, avery=False)
            bridge = RebuildCapturingBridge(root, None)
            with self.assertRaises(ValueError):
                bridge.send('Avery', 'hi')

    def test_shared_thread_spawns_agents_without_a_shared_token(self):
        from unittest.mock import patch
        from websocket import WebSocketConnectionClosedException
        fake_ws = FakeWebSocket()
        stub = types.ModuleType('websocket')
        stub.create_connection = lambda *args, **kwargs: fake_ws
        stub.WebSocketConnectionClosedException = WebSocketConnectionClosedException
        real_ws = sys.modules.get('websocket')
        sys.modules['websocket'] = stub
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = _rebuild_root(tmp, shared=False, morgan=True, avery=False)
                stop = threading.Event()
                shared = RebuildCapturingBridge(root, None, stop)
                with patch('staff.discord_bridge.DiscordBridge', RebuildCapturingBridge):
                    thread = threading.Thread(target=shared.run, daemon=True)
                    thread.start()
                    deadline = time.time() + 12
                    while time.time() < deadline and 'Morgan' not in shared._agent_bridges:
                        time.sleep(0.2)
                    stop.set()
                    thread.join(timeout=10)
                self.assertIn('Morgan', shared._agent_bridges)
                self.assertFalse(thread.is_alive(), 'shared gateway thread did not exit')
        finally:
            if real_ws is None:
                sys.modules.pop('websocket', None)
            else:
                sys.modules['websocket'] = real_ws


if __name__ == '__main__':
    unittest.main()
