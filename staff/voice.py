"""Agent voices for Discord: persistent per-agent voice profiles, speech
synthesis to Discord voice messages, and voice-note transcription.

Speech provider is Gemini text-to-speech first: each agent has a designed
voice (a persistent voice_... ID created from that agent's profile
direction, persona tone, and role) rendered through gemini-3.8-flash-tts.
If Gemini is unavailable for any reason (no key, HTTP error, bad audio),
speak() falls back to Cloudflare Workers AI MeloTTS rather than failing
the voice turn; it raises only when both providers fail. MeloTTS renders
one voice per language, so it is a fallback, not the identity: the
designed Gemini voice IDs are what make the agents sound distinct.
Nothing here invents a voice: speak() raises on a dual-provider failure
instead of returning placeholder audio.
"""
import base64
import io
import json
import os
import re
import urllib.error
import urllib.request
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
        'gemini_voice_id': 'voice_tmaxr1l2l0kt',
        'notes': [],
    },
    'Avery': {
        'gender': 'male',
        'provider': 'cloudflare-melotts',
        'model': '@cf/myshell-ai/melotts',
        'lang': 'en',
        'direction': 'Creative, energetic African American man. AAVE, natural human cadence, crisp pronunciation and enunciation.',
        'gemini_voice_id': 'voice_aho1ngj4ul5p',
        'notes': [],
    },
    'Jordan': {
        'gender': 'male',
        'provider': 'cloudflare-melotts',
        'model': '@cf/myshell-ai/melotts',
        'lang': 'en',
        'direction': 'Grounded, concise African American man. AAVE, natural human cadence, crisp pronunciation and enunciation.',
        'gemini_voice_id': 'voice_qqnd9kl9ef9s',
        'notes': [],
    },
    'Cameron': {
        'gender': 'male',
        'provider': 'cloudflare-melotts',
        'model': '@cf/myshell-ai/melotts',
        'lang': 'en',
        'direction': 'Measured, considerate African American man. AAVE, natural human cadence, crisp pronunciation and enunciation.',
        'gemini_voice_id': 'voice_90zh7jkztuox',
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


def log_voice_send(root, agent, kind, chat, text, duration_seconds=None, provider=None):
    """Append one voice-send record to the voice log.

    This is where voice confirmations belong: the log records what was
    sent, by whom, and where. It is never posted to chat or spoken, so a
    send never produces a second announcement message. Best-effort callers
    swallow errors from this function; a logging failure must not fail
    a send that already succeeded. The provider is recorded when known,
    so a later debug can tell which text-to-speech engine rendered a send.
    """
    path = Path(root) / 'work' / 'voice-messages.jsonl'
    path.parent.mkdir(exist_ok=True)
    record = {'time': __import__('time').time(), 'agent': agent, 'kind': kind,
              'chat': str(chat or ''), 'text': ' '.join(str(text or '').split())[:3000],
              'duration_seconds': duration_seconds}
    if provider:
        record['provider'] = provider
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


def _gemini_api_key(root):
    """Gemini API key from the environment, else the project's .env file."""
    key = os.environ.get('GEMINI_API_KEY', '')
    if key:
        return key
    try:
        from dotenv import dotenv_values
        return dotenv_values(Path(root) / '.env').get('GEMINI_API_KEY', '')
    except Exception:  # noqa: BLE001 - missing key is a fallback trigger, not an error
        return ''


def _gemini_synthesize_chunk(root, voice_id, text):
    """One Gemini TTS chunk. Returns (wav_bytes, duration_seconds)."""
    key = _gemini_api_key(root)
    if not key:
        raise RuntimeError('Gemini API key is not configured.')
    body = json.dumps({
        'contents': [{'parts': [{'text': text}]}],
        'generationConfig': {
            'responseModalities': ['AUDIO'],
            'speechConfig': {'voiceConfig': {'voice': voice_id}},
        },
    }).encode()
    req = urllib.request.Request(
        'https://generativelanguage.googleapis.com/v1beta/models/'
        'gemini-3.8-flash-tts:generateContent',
        data=body,
        headers={'Content-Type': 'application/json', 'x-goog-api-key': key},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError('Gemini voice synthesis failed (HTTP %s).' % e.code)
    except Exception as e:  # noqa: BLE001 - surfaced as a fallback trigger
        raise RuntimeError('Gemini voice synthesis failed: %s' % type(e).__name__)
    parts = ((payload.get('candidates') or [{}])[0].get('content') or {}).get('parts') or []
    blob = next((p.get('inlineData') for p in parts if p.get('inlineData')), None)
    if not blob or not blob.get('data'):
        raise RuntimeError('Gemini voice synthesis returned no audio.')
    wav = base64.b64decode(blob['data'])
    if wav[:4] != b'RIFF':
        raise RuntimeError('Gemini voice synthesis returned non-WAV audio.')
    with wave.open(io.BytesIO(wav)) as clip:
        duration = clip.getnframes() / clip.getframerate()
    return wav, duration


def _speak_gemini(root, agent, chunks, voice_id, profile):
    """Render chunks with the agent's designed Gemini voice. Returns speak()'s dict."""
    try:
        from staff.media_tools import convert_audio
    except ImportError:
        from media_tools import convert_audio
    wavs = []
    duration = 0.0
    for piece in chunks:
        wav, piece_duration = _gemini_synthesize_chunk(root, voice_id, piece)
        wavs.append(wav)
        duration += piece_duration
    combined, _params = _concat_wav(wavs)
    ogg = convert_audio(combined, 'ogg')
    return {'data': ogg, 'format': 'OGG', 'duration_seconds': round(duration, 2),
            'provider': 'Gemini text-to-speech',
            'voice': profile}


def speak(root, agent, text):
    """Render text in the agent's voice. Returns Ogg Opus bytes for Discord.

    Tries the agent's designed Gemini voice first; on any Gemini failure
    falls back to MeloTTS. Raises only when both providers fail (callers
    then fall back to text). Validation errors always raise.
    """
    if agent not in _DEFAULTS:
        raise ValueError('Choose Morgan, Avery, Jordan, or Cameron.')
    text = ' '.join(str(text or '').split())
    if not text or len(text) > MAX_SPEAK_CHARS:
        raise ValueError('Voice messages must be 1-%d characters.' % MAX_SPEAK_CHARS)
    chunks = chunk_text(text)
    if not chunks or len(chunks) > 4:
        raise ValueError('Voice message is too long to render.')
    profile = load_profiles(root)[agent]
    voice_id = profile.get('gemini_voice_id') or ''
    if voice_id:
        try:
            return _speak_gemini(root, agent, chunks, voice_id, profile)
        except Exception:  # noqa: BLE001 - Gemini failure falls back to MeloTTS by design
            pass
    try:
        from staff.media_tools import generate_audio, convert_audio
    except ImportError:
        from media_tools import generate_audio, convert_audio
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
            'voice': profile}


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
    """Transcribe a Discord voice/audio message via Cloudflare Whisper."""
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
