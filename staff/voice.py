"""Agent voices for Telegram: persistent per-agent voice profiles, speech
synthesis to Telegram voice messages, and voice-note transcription.

Provider today is Cloudflare Workers AI: MeloTTS for speech, Whisper for
transcription. MeloTTS renders one voice per language, so the profiles
below capture each agent's gender, direction, and identity now and map
to distinct provider voice IDs when a multi-voice provider (for example
ElevenLabs) is connected. Nothing here invents a voice: speak() raises
on any provider failure instead of returning placeholder audio.
"""
import io
import json
import re
import wave
from pathlib import Path

AGENTS = ('Morgan', 'Avery', 'Jordan', 'Cameron')

_DEFAULTS = {
    'Morgan': {
        'gender': 'female',
        'provider': 'cloudflare-melotts',
        'model': '@cf/myshell-ai/melotts',
        'lang': 'en',
        'direction': 'Warm, calm, decisive African American woman. AAVE, natural human cadence, crisp pronunciation and enunciation.',
        'notes': [],
    },
    'Avery': {
        'gender': 'male',
        'provider': 'cloudflare-melotts',
        'model': '@cf/myshell-ai/melotts',
        'lang': 'en',
        'direction': 'Creative, energetic African American man. AAVE, natural human cadence, crisp pronunciation and enunciation.',
        'notes': [],
    },
    'Jordan': {
        'gender': 'male',
        'provider': 'cloudflare-melotts',
        'model': '@cf/myshell-ai/melotts',
        'lang': 'en',
        'direction': 'Grounded, concise African American man. AAVE, natural human cadence, crisp pronunciation and enunciation.',
        'notes': [],
    },
    'Cameron': {
        'gender': 'male',
        'provider': 'cloudflare-melotts',
        'model': '@cf/myshell-ai/melotts',
        'lang': 'en',
        'direction': 'Measured, considerate African American man. AAVE, natural human cadence, crisp pronunciation and enunciation.',
        'notes': [],
    },
}

MAX_SPEAK_CHARS = 3600
_CHUNK = 900


def profiles_path(root):
    return Path(root) / 'work' / 'agent-voices.json'


def load_profiles(root):
    """Voice profiles: file values over defaults, per agent."""
    profiles = {agent: dict(spec, notes=list(spec['notes'])) for agent, spec in _DEFAULTS.items()}
    try:
        data = json.loads(profiles_path(root).read_text())
    except (OSError, ValueError, TypeError):
        return profiles
    if not isinstance(data, dict):
        return profiles
    for agent, spec in data.items():
        if agent in profiles and isinstance(spec, dict):
            merged = dict(profiles[agent])
            for key, value in spec.items():
                if key == 'notes' and isinstance(value, list):
                    merged['notes'] = [str(n)[:200] for n in value[-20:]]
                elif key in merged and isinstance(value, str):
                    merged[key] = value[:500]
            profiles[agent] = merged
    return profiles


def log_voice_send(root, agent, kind, chat, text, duration_seconds=None):
    """Append one voice-send record to the voice log.

    This is where voice confirmations belong: the log records what was
    sent, by whom, and where. It is never posted to chat or spoken, so a
    send never produces a second announcement message. Best-effort callers
    swallow errors from this function; a logging failure must not fail
    a send that already succeeded.
    """
    path = Path(root) / 'work' / 'voice-messages.jsonl'
    path.parent.mkdir(exist_ok=True)
    record = {'time': __import__('time').time(), 'agent': agent, 'kind': kind,
              'chat': str(chat or ''), 'text': ' '.join(str(text or '').split())[:3000],
              'duration_seconds': duration_seconds}
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except (OSError, ValueError):
        lines = []
    lines.append(json.dumps(record))
    path.write_text('\n'.join(lines[-1000:]) + '\n', encoding='utf-8')
    path.chmod(0o600)
    return record


def tune_voice(root, agent, note):
    """Append one voice-direction note to the agent's own profile."""
    if agent not in _DEFAULTS:
        raise ValueError('Choose Morgan, Avery, Jordan, or Cameron.')
    note = ' '.join(str(note or '').split())
    if not note or len(note) > 200:
        raise ValueError('Voice direction notes must be 1-200 characters.')
    path = profiles_path(root)
    path.parent.mkdir(exist_ok=True)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    spec = dict(data.get(agent) or {})
    notes = [str(n) for n in spec.get('notes', []) if isinstance(n, str)][-19:]
    notes.append(note)
    spec['notes'] = notes
    data[agent] = spec
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, indent=2))
    temp.chmod(0o600)
    temp.replace(path)
    return {'agent': agent, 'notes': notes}


def chunk_text(text):
    """Split narration into sentence-boundary chunks of at most _CHUNK chars."""
    pieces = [p.strip() for p in re.split(r'(?<=[.!?])\s+', text.strip()) if p.strip()]
    chunks = []
    current = ''
    for piece in pieces:
        if len(piece) > _CHUNK:
            if current:
                chunks.append(current)
                current = ''
            for i in range(0, len(piece), _CHUNK):
                chunks.append(piece[i:i + _CHUNK])
            continue
        if current and len(current) + 1 + len(piece) > _CHUNK:
            chunks.append(current)
            current = piece
        else:
            current = (current + ' ' + piece).strip() if current else piece
    if current:
        chunks.append(current)
    return chunks


def _concat_wav(chunks):
    """Concatenate same-format WAV clips; raise on any parameter mismatch."""
    params = None
    frames = b''
    for data in chunks:
        with wave.open(io.BytesIO(data)) as clip:
            current = (clip.getnchannels(), clip.getsampwidth(), clip.getframerate())
            if params is None:
                params = current
            elif current != params:
                raise RuntimeError('Voice synthesis returned inconsistent audio formats.')
            frames += clip.readframes(clip.getnframes())
    out = io.BytesIO()
    with wave.open(out, 'wb') as clip:
        clip.setnchannels(params[0])
        clip.setsampwidth(params[1])
        clip.setframerate(params[2])
        clip.writeframes(frames)
    return out.getvalue(), params


def speak(root, agent, text):
    """Render text in the agent's voice. Returns Ogg Opus bytes for Telegram.

    Raises on any failure (no placeholder audio); callers fall back to text.
    """
    if agent not in _DEFAULTS:
        raise ValueError('Choose Morgan, Avery, Jordan, or Cameron.')
    text = ' '.join(str(text or '').split())
    if not text or len(text) > MAX_SPEAK_CHARS:
        raise ValueError('Voice messages must be 1-%d characters.' % MAX_SPEAK_CHARS)
    try:
        from staff.media_tools import generate_audio, convert_audio
    except ImportError:
        from media_tools import generate_audio, convert_audio
    chunks = chunk_text(text)
    if not chunks or len(chunks) > 4:
        raise ValueError('Voice message is too long to render.')
    wavs = []
    duration = 0.0
    for piece in chunks:
        rendered = generate_audio(root, piece)
        wavs.append(rendered['data'])
        duration += float(rendered.get('duration_seconds') or 0)
    combined, _params = _concat_wav(wavs)
    ogg = convert_audio(combined, 'ogg')
    return {'data': ogg, 'format': 'OGG', 'duration_seconds': round(duration, 2),
            'provider': 'Cloudflare Workers AI text-to-speech',
            'voice': load_profiles(root)[agent]}


def _to_wav(audio_bytes, suffix):
    """Normalize any voice/audio upload to 16 kHz mono WAV for Whisper."""
    import shutil
    import subprocess
    import tempfile
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        return audio_bytes
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / ('input' + suffix)
        dst = Path(tmp) / 'output.wav'
        src.write_bytes(audio_bytes)
        result = subprocess.run(
            [ffmpeg, '-y', '-v', 'error', '-i', str(src), '-vn', '-c:a', 'pcm_s16le',
             '-ar', '16000', '-ac', '1', str(dst)],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            return audio_bytes
        return dst.read_bytes()


def transcribe(root, audio_bytes, suffix='.ogg'):
    """Transcribe a Telegram voice/audio message via Cloudflare Whisper."""
    import requests
    from dotenv import dotenv_values
    if not audio_bytes or len(audio_bytes) > 10_000_000:
        raise ValueError('Voice messages must be 10 MB or smaller.')
    wav = _to_wav(audio_bytes, suffix if suffix.startswith('.') else '.ogg')
    if len(wav) > 2_000_000:
        raise ValueError('Voice message is too long to transcribe; keep it under about a minute.')
    cfg = dotenv_values(Path(root) / '.env')
    account = cfg.get('CLOUDFLARE_ACCOUNT_ID', '')
    token = cfg.get('CLOUDFLARE_API_TOKEN', '')
    if cfg.get('CLOUDFLARE_FREE_PLAN_CONFIRMED') != 'true' or not account or not token:
        raise RuntimeError('Cloudflare credentials are not configured for transcription.')
    response = requests.post(
        'https://api.cloudflare.com/client/v4/accounts/' + account + '/ai/run/@cf/openai/whisper',
        headers={'Authorization': 'Bearer ' + token},
        json={'audio': list(wav)},
        timeout=120)
    if not response.ok:
        raise RuntimeError('Voice transcription failed.')
    result = (response.json().get('result') or {})
    text = result.get('text', '').strip() if isinstance(result, dict) else str(result).strip()
    if not text:
        raise ValueError('No speech detected in that voice message.')
    return text
