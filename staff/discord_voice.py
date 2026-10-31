"""Discord voice calls: one agent per call, listening while talking.

A VoiceSession is one agent in one voice channel. The agent hears
through discord-ext-voice-recv, answers through the same ask()/speak()
path as text chats, and plays the reply back into the channel. The
microphone never closes while the agent talks: speech detected during
playback stops the playback at once, and the reply to the interruption
comes once the user finishes speaking.

Threading shape, because discord.py is asyncio and everything else
here is synchronous: the supervisor owns one asyncio loop in a daemon
thread. The receive sink only queues raw audio and returns. A consumer
thread turns queued audio into turn events and hands them to the loop
with call_soon_threadsafe, so every discord.py call happens on the
loop thread. ask()/speak() run in executor threads so the loop never
blocks on a model call.
"""

import asyncio
import json
import queue
import re
import struct
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path

from dotenv import dotenv_values

STAFF = ("Morgan", "Avery", "Jordan", "Cameron")
CALL_SUFFIX = (
    "You are speaking in a live voice call. Keep your reply short and "
    "conversational, under about 120 words, plain spoken text, no "
    "formatting or lists."
)
INTERRUPTION_NOTE = (
    "Your previous reply was cut off because the user spoke over you. "
    "Do not repeat it; answer what they just said."
)
STOP_WORDS = {"stop", "please stop", "stop please", "be quiet", "quiet"}
FRAME_BYTES = 640  # 20 ms of 16 kHz mono s16le audio


def downmix_pcm48_stereo_to_16k_mono(pcm48):
    """48 kHz stereo s16le -> 16 kHz mono s16le, for webrtcvad.

    Naive decimation (every third frame, channels averaged). It aliases
    high frequencies, but the VAD only needs speech presence, not
    fidelity, and this keeps the pipeline dependency-free.
    """
    frames = len(pcm48) // 4
    out = bytearray()
    for i in range(0, frames, 3):
        left, right = struct.unpack_from("<hh", pcm48, i * 4)
        out += struct.pack("<h", (left + right) // 2)
    return bytes(out)


def split_sentences(text):
    """Split spoken text on sentence ends, keeping the punctuation."""
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]


class TurnTracker:
    """Turn-taking state machine over 16 kHz mono audio.

    Events returned by feed(): 'barge_in' (the user started speaking
    while the agent was playing, past the debounce) and 'turn_ended'
    (a completed turn with its PCM). A turn starts after a few
    consecutive speech frames and ends after ~800 ms of silence or
    after 60 seconds, whichever comes first. While the agent plays,
    the first half second is grace (speaker bleed, join noises), then
    a longer speech run than the idle threshold counts as a barge-in,
    so a cough does not cut the agent off.
    """

    def __init__(self, vad=None, vad_mode=2, start_frames=3, barge_frames=6,
                 silence_frames=40, max_seconds=60, grace_seconds=0.5):
        if vad is None:
            import webrtcvad
            vad = webrtcvad.Vad(vad_mode)
        self.vad = vad
        self.start_frames = start_frames
        self.barge_frames = barge_frames
        self.silence_frames = silence_frames
        self.max_bytes = max_seconds * 16000 * 2
        self.grace_seconds = grace_seconds
        self.pre_roll = deque(maxlen=25)  # ~500 ms of audio before a turn
        self.playing = False
        self.play_started = 0.0
        self.reset_turn()

    def reset_turn(self):
        self.state = "idle"
        self.speech = 0
        self.silence = 0
        self.turn_pcm = bytearray()
        self.barge_speech = 0
        self.barge_pcm = bytearray()
        self.barged = False

    def set_playing(self, playing, now=None):
        self.playing = playing
        self.play_started = now if now is not None else time.monotonic()
        self.barge_speech = 0
        self.barge_pcm = bytearray()
        self.barged = False
        if not playing:
            # Agent audio tail must not seed the next turn's pre-roll.
            self.pre_roll.clear()

    def feed(self, pcm16k, now=None):
        """Feed 16 kHz mono bytes; returns a list of (event, payload)."""
        now = now if now is not None else time.monotonic()
        events = []
        for offset in range(0, len(pcm16k) - FRAME_BYTES + 1, FRAME_BYTES):
            frame = pcm16k[offset:offset + FRAME_BYTES]
            speech = bool(self.vad.is_speech(frame, 16000))
            self.pre_roll.append(frame)
            in_grace = self.playing and (now - self.play_started) < self.grace_seconds
            just_barged = False
            if self.playing and not in_grace and not self.barged:
                if speech:
                    self.barge_speech += 1
                    self.barge_pcm += frame
                    if self.barge_speech >= self.barge_frames:
                        self.barged = True
                        just_barged = True
                        events.append(("barge_in", None))
                        if self.state == "idle":
                            self.state = "speaking"
                            # No pre-roll here: during playback it may
                            # hold agent audio picked up by the mic.
                            self.turn_pcm = bytearray(self.barge_pcm)
                            self.speech = self.barge_speech
                            self.silence = 0
                else:
                    self.barge_speech = 0
                    self.barge_pcm = bytearray()
            if just_barged:
                # The barge frame is already in turn_pcm; do not count it twice.
                continue
            if self.state == "idle":
                if speech and not self.playing:
                    self.speech += 1
                    if self.speech == 1:
                        self.turn_pcm = bytearray(b"".join(self.pre_roll))
                    else:
                        self.turn_pcm += frame
                    if self.speech >= self.start_frames:
                        self.state = "speaking"
                        self.silence = 0
                elif not speech:
                    self.speech = 0
                    self.turn_pcm = bytearray()
            elif self.state == "speaking":
                self.turn_pcm += frame
                if speech:
                    self.silence = 0
                else:
                    self.silence += 1
                if self.silence >= self.silence_frames or len(self.turn_pcm) >= self.max_bytes:
                    truncated = len(self.turn_pcm) >= self.max_bytes
                    pcm = bytes(self.turn_pcm)
                    if self.silence:
                        pcm = pcm[:max(0, len(pcm) - self.silence * FRAME_BYTES)]
                    events.append(("turn_ended", (pcm, truncated)))
                    self.reset_turn()
        return events


def pcm_to_wav(pcm16k):
    """Wrap raw 16 kHz mono s16le PCM in a WAV header for transcribe()."""
    import io
    import wave
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm16k)
    return buffer.getvalue()


class VoiceSession:
    """One agent in one voice channel, owned by the supervisor loop."""

    def __init__(self, supervisor, agent, guild_id, channel_id, text_channel):
        self.supervisor = supervisor
        self.root = supervisor.root
        self.chat = supervisor.chat
        self.agent = agent
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.text_channel = text_channel
        self.loop = supervisor.loop
        self.tracker = TurnTracker()
        self.audio_queue = queue.Queue()
        self.closed = threading.Event()
        self.client = None
        self._client_task = None
        self.vc = None
        self.channel = None
        self.human_ids = set()
        self.generation_task = None
        self.interrupted = False
        self.busy = False

    def log(self, kind, text, provider=None):
        try:
            from staff.voice import log_voice_send
            log_voice_send(self.root, self.agent, kind, str(self.channel_id), text, provider=provider)
        except Exception:
            pass

    def _post_text(self, text):
        token = self.supervisor.token(self.agent)
        if not token or not self.text_channel:
            return
        try:
            request = urllib.request.Request(
                "https://discord.com/api/v10/channels/" + str(self.text_channel) + "/messages",
                data=json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode(),
                headers={"Authorization": "Bot " + token, "Content-Type": "application/json"},
                method="POST")
            urllib.request.urlopen(request, timeout=15).read()
        except Exception:
            pass

    async def start(self):
        import discord
        from discord import opus
        from discord.ext import voice_recv
        if not opus.is_loaded():
            opus.load_opus("libopus.so.0")
        intents = discord.Intents(guilds=True, voice_states=True)
        session_self = self

        class _SessionClient(discord.Client):
            async def on_voice_state_update(self, member, before, after):
                await session_self._on_voice_state_update(member, before, after)

        self.client = _SessionClient(intents=intents)
        token = self.supervisor.token(self.agent)
        # login() first: wait_until_ready() raises until the client has
        # been initialised, so it cannot be called while start() still
        # runs in the background. Split start() into an awaited login
        # and a backgrounded gateway loop instead.
        await self.client.login(token)
        self._client_task = asyncio.create_task(self.client.connect())
        await self.client.wait_until_ready()
        channel = self.client.get_channel(int(self.channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(self.channel_id))
        self.channel = channel
        self.vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        self.vc.listen(voice_recv.BasicSink(self._on_voice_packet))
        for member_id, voice_state in list(getattr(channel, "voice_states", {}).items()):
            member = getattr(voice_state, "member", None)
            if member is not None and not getattr(member, "bot", False):
                self.human_ids.add(member_id)
        self.consumer = threading.Thread(target=self._consume_audio, daemon=True)
        self.consumer.start()

    def _on_voice_packet(self, user, data):
        """Receive sink callback: queue and return, never block."""
        if user is None or getattr(user, "bot", False):
            return
        pcm = getattr(data, "pcm", None)
        if pcm:
            self.audio_queue.put((user.id, pcm))

    def _consume_audio(self):
        while not self.closed.is_set():
            try:
                _user_id, pcm48 = self.audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            pcm16k = downmix_pcm48_stereo_to_16k_mono(pcm48)
            for event, payload in self.tracker.feed(pcm16k):
                if event == "barge_in":
                    self.loop.call_soon_threadsafe(self._on_barge)
                elif event == "turn_ended":
                    pcm, truncated = payload
                    self.loop.call_soon_threadsafe(self._on_turn, pcm, truncated)

    def _on_barge(self):
        """User spoke over the agent: go silent now, answer later."""
        if self.closed.is_set():
            return
        if self.busy:
            self.interrupted = True
            if self.generation_task is not None and not self.generation_task.done():
                self.generation_task.cancel()
            if self.vc is not None and self.vc.is_playing():
                self.vc.stop_playing()
            self.tracker.set_playing(False)

    def _on_turn(self, pcm, truncated):
        if self.closed.is_set():
            return
        if self.busy:
            # A finished turn while the agent was talking is itself
            # an interruption: drop the old reply, answer this one.
            self._on_barge()
            interrupted = True
        else:
            interrupted = self.interrupted
        self.interrupted = False
        self.generation_task = asyncio.ensure_future(self._answer(pcm, truncated, interrupted))

    async def _answer(self, pcm, truncated, interrupted):
        self.busy = True
        try:
            from staff.voice import speak, transcribe
            try:
                wav_bytes = pcm_to_wav(pcm)
                transcript = await self.loop.run_in_executor(
                    None, lambda: transcribe(self.root, wav_bytes, ".wav"))
            except Exception:
                self._post_text("**" + self.agent + "**\nI could not make out that last bit. Say it again, a little slower.")
                return
            transcript = transcript.strip()
            self.log("discord-call", transcript)
            if truncated:
                self._post_text("**" + self.agent + "**\nThat ran long, so I only caught the first minute. Say it in shorter bits and I will keep up.")
            if transcript.lower().strip(" .!,?") in STOP_WORDS:
                return
            body = transcript + "\n\n" + CALL_SUFFIX
            if interrupted:
                body = INTERRUPTION_NOTE + "\n\n" + body
            answer = await self.loop.run_in_executor(
                None, lambda: self.chat.ask(self.agent, body, str(uuid.uuid4()), source="discord"))
            if self.closed.is_set():
                return
            spoken = await self.loop.run_in_executor(
                None, lambda: speak(self.root, self.agent, answer))
            if self.closed.is_set():
                return
            self.log("discord-call-reply", answer, provider=spoken.get("provider"))
            await self._play(spoken["data"])
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._post_text("**" + self.agent + "**\nI hit an error answering that. Try again in a moment.")
            try:
                state_path = self.root / "work" / "discord-voice-state.json"
                state = {}
                try:
                    state = json.loads(state_path.read_text())
                except (OSError, ValueError):
                    pass
                state[self.agent] = {"lastError": str(error)[:180]}
                state_path.write_text(json.dumps(state))
            except Exception:
                pass
        finally:
            # Only the current task clears busy: a cancelled older
            # task must not clear the flag for its replacement.
            if self.generation_task is asyncio.current_task():
                self.busy = False

    async def _play(self, ogg_bytes):
        import discord
        folder = self.root / "work" / "discord-call-audio"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (self.agent.lower() + "-" + uuid.uuid4().hex[:8] + ".ogg")
        path.write_bytes(ogg_bytes)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        finished = self.loop.create_future()

        def after(error):
            try:
                path.unlink()
            except OSError:
                pass
            if not finished.done():
                self.loop.call_soon_threadsafe(finished.set_result, error)

        self.tracker.set_playing(True)
        self.vc.play(discord.FFmpegPCMAudio(str(path)), after=after)
        try:
            await finished
        except asyncio.CancelledError:
            if self.vc is not None and self.vc.is_playing():
                self.vc.stop_playing()
            raise
        finally:
            self.tracker.set_playing(False)

    async def _on_voice_state_update(self, member, before, after):
        if self.closed.is_set():
            return
        if getattr(member, "bot", False):
            return
        if after.channel is not None and str(after.channel.id) == str(self.channel_id):
            self.human_ids.add(member.id)
        if before.channel is not None and str(before.channel.id) == str(self.channel_id):
            if after.channel is None or str(after.channel.id) != str(self.channel_id):
                self.human_ids.discard(member.id)
        if not self.human_ids:
            self._post_text("**" + self.agent + "**\nEveryone left the call, so I stepped out too.")
            await self.leave()

    async def leave(self):
        if self.closed.is_set():
            return
        self.closed.set()
        if self.generation_task is not None and not self.generation_task.done():
            self.generation_task.cancel()
        try:
            if self.vc is not None:
                if self.vc.is_playing():
                    self.vc.stop_playing()
                try:
                    self.vc.stop_listening()
                except Exception:
                    pass
                await self.vc.disconnect()
        except Exception:
            pass
        try:
            if self.client is not None:
                await self.client.close()
        except Exception:
            pass
        try:
            if self._client_task is not None and not self._client_task.done():
                self._client_task.cancel()
        except Exception:
            pass
        self.supervisor.sessions.pop(self.agent, None)


class DiscordVoiceSupervisor:
    """Owns voice sessions; one asyncio loop, one thread, N agents.

    The shared text bridge starts this lazily on the first
    /join-voice, so a dashboard with no calls running pays nothing.
    Requests arrive from bridge threads and are drained on the loop
    thread, so sessions are only ever created or removed there.
    """

    def __init__(self, root, chat, stop):
        self.root = Path(root)
        self.chat = chat
        self.stop = stop
        self.loop = None
        self.thread = None
        self.sessions = {}
        self.requests = queue.Queue()

    def token(self, agent):
        return dotenv_values(self.root / ".env").get("DISCORD_" + agent.upper() + "_BOT_TOKEN")

    def start(self):
        if self.thread is not None and self.thread.is_alive():
            return
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def request_join(self, agent, guild_id, channel_id, text_channel=None):
        self.start()
        self.requests.put(("join", agent, guild_id, channel_id, text_channel))
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._drain)

    def request_leave(self, agent):
        self.start()
        self.requests.put(("leave", agent, None, None, None))
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._drain)

    def _drain(self):
        while True:
            try:
                kind, agent, guild_id, channel_id, text_channel = self.requests.get_nowait()
            except queue.Empty:
                return
            if kind == "join":
                asyncio.ensure_future(self._do_join(agent, guild_id, channel_id, text_channel))
            else:
                asyncio.ensure_future(self._do_leave(agent))

    async def _do_join(self, agent, guild_id, channel_id, text_channel):
        if agent in self.sessions:
            self._post_text(agent, text_channel, "**" + agent + "** is already in a call. Use /leave-voice first if you want them somewhere else.")
            return
        for other_agent, session in list(self.sessions.items()):
            if str(session.channel_id) == str(channel_id):
                self._post_text(agent, text_channel, "**" + other_agent + "** is already in that call. One voice agent per call, so they do not talk over each other.")
                return
        session = VoiceSession(self, agent, guild_id, channel_id, text_channel)
        try:
            await session.start()
        except Exception as error:
            self._post_text(agent, text_channel, "**" + agent + "** could not join that voice channel: " + str(error)[:180])
            return
        self.sessions[agent] = session

    async def _do_leave(self, agent):
        session = self.sessions.get(agent)
        if session is not None:
            await session.leave()

    def _post_text(self, agent, channel_id, text):
        token = self.token(agent)
        if not token or not channel_id:
            return
        try:
            request = urllib.request.Request(
                "https://discord.com/api/v10/channels/" + str(channel_id) + "/messages",
                data=json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode(),
                headers={"Authorization": "Bot " + token, "Content-Type": "application/json"},
                method="POST")
            urllib.request.urlopen(request, timeout=15).read()
        except Exception:
            pass
