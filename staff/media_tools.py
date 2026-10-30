"""Bounded media capabilities for staff agents.

Honesty rules (do not weaken them):
- Image generation through Cloudflare Workers AI is real AI generation.
- ``render_image`` is a LOCAL Pillow render (flat graphic + real text), NOT
  AI generation. Callers must label it as locally rendered.
- Audio generation through Cloudflare Workers AI text-to-speech is real.
- Video generation has NO configured provider: ``generate_video`` always
  raises a clear not-configured error and never writes a placeholder file.
- ``view_media`` only describes/verifies a file that already exists. It
  never claims to have watched or listened to anything it cannot decode.
"""

import base64
import io
import re
import textwrap
import wave

import requests
from dotenv import dotenv_values

AUDIO_MODEL = "@cf/myshell-ai/melotts"
IMAGE_MODEL_NOTE = "Cloudflare Flux via staff.design_images (already wired)"

VIDEO_NOT_CONFIGURED = (
    "Video generation is not configured: no video-generation endpoint is "
    "wired for any configured provider (OpenRouter free models are chat-only, "
    "the Gemini free tier is unconfirmed and has no video model wired, and "
    "Cloudflare Workers AI has no video-generation model on the configured "
    "account path). No file was created. To unblock, James must choose and "
    "configure a video-generation provider; do not present any placeholder "
    "as a generated video."
)


def _cloudflare_credentials(root):
    from pathlib import Path

    cfg = dotenv_values(Path(root) / ".env")
    account = cfg.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = cfg.get("CLOUDFLARE_API_TOKEN", "")
    if cfg.get("CLOUDFLARE_FREE_PLAN_CONFIRMED") != "true" or not re.fullmatch(
        "[a-fA-F0-9]{32}", account
    ) or not token:
        raise RuntimeError(
            "Add Cloudflare credentials and confirm the Free plan in Settings."
        )
    return account, token


def generate_audio(root, text):
    """Real text-to-speech via Cloudflare Workers AI (melotts).

    Returns WAV bytes. Raises RuntimeError with an honest reason on any
    failure; never returns placeholder audio.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 1000:
        raise ValueError("Provide narration text within 1,000 characters.")
    account, token = _cloudflare_credentials(root)
    response = requests.post(
        "https://api.cloudflare.com/client/v4/accounts/" + account + "/ai/run/" + AUDIO_MODEL,
        headers={"Authorization": "Bearer " + token},
        json={"prompt": text.strip()},
        timeout=90,
    )
    if not response.ok:
        raise RuntimeError(
            "Cloudflare audio generation failed. Check token permissions, "
            "model availability, and daily quota."
        )
    payload = response.json()
    encoded = (payload.get("result") or {}).get("audio", "")
    if not encoded:
        raise RuntimeError("Cloudflare audio generation returned no audio.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise RuntimeError("Cloudflare audio generation returned invalid audio.")
    # Verify it is really a decodable WAV before any caller claims success.
    try:
        with wave.open(io.BytesIO(data)) as clip:
            frames = clip.getnframes()
            rate = clip.getframerate()
            channels = clip.getnchannels()
    except (wave.Error, EOFError):
        raise RuntimeError("Cloudflare audio generation returned undecodable audio.")
    if frames <= 0 or rate <= 0:
        raise RuntimeError("Cloudflare audio generation returned empty audio.")
    if len(data) > 15_000_000:
        raise RuntimeError("Generated audio exceeds the file limit.")
    return {
        "data": data,
        "format": "WAV",
        "channels": channels,
        "sample_rate": rate,
        "duration_seconds": round(frames / rate, 2),
    }



def convert_audio(wav_data, target_format):
    """Convert verified WAV bytes to mp3/aac/flac/ogg via ffmpeg (installed
    in the dashboard container; also added to the Dockerfile for rebuilds).
    Returns bytes in the target format. Never returns a mislabelled file."""
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path
    fmt = str(target_format).strip().lower().lstrip('.')
    if fmt == 'acc':
        fmt = 'aac'
    codecs = {'mp3': 'libmp3lame', 'aac': 'aac', 'flac': 'flac', 'ogg': 'libopus'}
    if fmt not in codecs:
        raise ValueError('Audio format must be wav, mp3, aac, flac, or ogg.')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('Audio conversion needs ffmpeg, which is not installed in the dashboard container.')
    if len(wav_data) > 15_000_000:
        raise ValueError('Generated audio exceeds the file limit.')
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / 'input.wav'
        dst = Path(tmp) / ('output.' + fmt)
        src.write_bytes(wav_data)
        result = subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', str(src), '-vn', '-c:a', codecs[fmt], str(dst)], capture_output=True, text=True, timeout=60)
        if result.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
            raise RuntimeError('Audio conversion to ' + fmt.upper() + ' failed.')
        data = dst.read_bytes()
    if len(data) > 15_000_000:
        raise ValueError('Converted audio exceeds the file limit.')
    return data


def probe_audio_duration(path):
    """Duration via ffprobe for mp3/aac/flac/ogg/m4a; None if unprobable."""
    import json as _json
    import shutil
    import subprocess
    ffprobe = shutil.which('ffprobe')
    if not ffprobe:
        return None
    try:
        result = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration', '-of', 'json', str(path)], capture_output=True, text=True, timeout=30)
        duration = float(_json.loads(result.stdout).get('format', {}).get('duration', 0) or 0)
        return round(duration, 2) if duration > 0 else None
    except (ValueError, OSError, subprocess.SubprocessError):
        return None

def generate_video(root, text):
    """No video provider is configured. Always raises; never fakes a file."""
    raise RuntimeError(VIDEO_NOT_CONFIGURED)


def render_image(title, subtitle="", image_format="png"):
    """Locally rendered graphic, NOT AI-generated.

    Flat ink background, gold accent bar, real rendered text. Returns bytes
    in the requested format: png, jpg, jpeg, webp, gif (Pillow) or svg
    (hand-written vector markup — the only honest SVG path for a local
    render; a raster image is never relabelled as SVG). Callers must
    describe the result as locally rendered.
    """
    from PIL import Image, ImageDraw, ImageFont

    if not isinstance(title, str) or not title.strip() or len(title) > 160:
        raise ValueError("Provide an image title within 160 characters.")
    subtitle = subtitle if isinstance(subtitle, str) else ""
    if len(subtitle) > 300:
        raise ValueError("Provide a subtitle within 300 characters.")
    fmt = str(image_format or 'png').strip().lower().lstrip('.')
    if fmt not in ('png', 'jpg', 'jpeg', 'webp', 'gif', 'svg'):
        raise ValueError('Image format must be png, jpg, jpeg, webp, gif, or svg.')
    if fmt == 'svg':
        import html as _html
        esc = lambda v: _html.escape(str(v), quote=True)
        svg_lines = ['<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800" viewBox="0 0 1200 800">',
                     '<rect width="1200" height="800" fill="#090b11"/>',
                     '<rect width="1200" height="14" fill="#c9a15b"/>',
                     '<rect x="80" y="700" width="200" height="6" fill="#c9a15b"/>',
                     '<text x="80" y="110" font-family="Arial, sans-serif" font-size="24" font-weight="bold" fill="#c9a15b">ONYX &amp; INK</text>']
        y = 230
        for line in textwrap.wrap(title.strip(), width=26):
            svg_lines.append('<text x="80" y="%d" font-family="Arial, sans-serif" font-size="64" font-weight="bold" fill="#f5f0e6">%s</text>' % (y, esc(line)))
            y += 78
        if subtitle.strip():
            y += 30
            for line in textwrap.wrap(subtitle.strip(), width=52):
                svg_lines.append('<text x="80" y="%d" font-family="Arial, sans-serif" font-size="30" fill="#bec0c6">%s</text>' % (y, esc(line)))
                y += 40
        svg_lines.append('</svg>')
        return ('\n'.join(svg_lines) + '\n').encode('utf-8')
    width, height = 1200, 800
    canvas = Image.new("RGB", (width, height), (9, 11, 17))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, width, 14], fill=(201, 161, 91))
    draw.rectangle([80, 700, 280, 706], fill=(201, 161, 91))

    def load_font(size, bold=False):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except (OSError, IOError):
                continue
        return ImageFont.load_default()

    title_font = load_font(64, bold=True)
    sub_font = load_font(30)
    brand_font = load_font(24, bold=True)
    draw.text((80, 90), "ONYX & INK", font=brand_font, fill=(201, 161, 91))
    y = 190
    for line in textwrap.wrap(title.strip(), width=26):
        draw.text((80, y), line, font=title_font, fill=(245, 240, 230))
        y += 78
    if subtitle.strip():
        y += 30
        for line in textwrap.wrap(subtitle.strip(), width=52):
            draw.text((80, y), line, font=sub_font, fill=(190, 192, 198))
            y += 40
    buffer = io.BytesIO()
    if fmt in ('jpg', 'jpeg'):
        canvas.save(buffer, format='JPEG', quality=90)
    elif fmt == 'webp':
        canvas.save(buffer, format='WEBP', quality=90)
    elif fmt == 'gif':
        canvas.save(buffer, format='GIF')
    else:
        canvas.save(buffer, format='PNG')
    return buffer.getvalue()


def view_media(path):
    """Describe/verify an existing media file. Never invents content.

    Returns format, size, and — where a decoder is installed — dimensions,
    page count, or duration (WAV via stdlib, other audio/video via ffprobe
    now that ffmpeg is installed). Without a probe result it reports
    size/format honestly and says duration could not be probed.
    """
    from pathlib import Path

    path = Path(path)
    if not path.is_file():
        raise ValueError("Media file not found.")
    suffix = path.suffix.lower()
    size = path.stat().st_size
    if size > 15_000_000:
        raise ValueError("Media file exceeds the 15 MB staff-file limit.")
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            return {
                "kind": "image",
                "format": image.format,
                "width": image.width,
                "height": image.height,
                "bytes": size,
                "result": "Image verified: %s %dx%d."
                % (image.format, image.width, image.height),
            }
    if suffix == ".pdf":
        import pymupdf

        with pymupdf.open(str(path)) as document:
            pages = len(document)
            sample = ""
            if pages:
                sample = document[0].get_text()[:500]
        return {
            "kind": "pdf",
            "pages": pages,
            "bytes": size,
            "first_page_text_sample": sample,
            "result": "PDF verified: %d page(s)." % pages,
        }
    if suffix == ".wav":
        with wave.open(str(path)) as clip:
            frames = clip.getnframes()
            rate = clip.getframerate()
            duration = round(frames / rate, 2) if rate else 0
        return {
            "kind": "audio",
            "format": "WAV",
            "channels": clip.getnchannels(),
            "sample_rate": rate,
            "duration_seconds": duration,
            "bytes": size,
            "result": "Audio verified: WAV, %.2f seconds." % duration,
        }
    if suffix == ".svg":
        import xml.etree.ElementTree as _et
        try:
            _et.fromstring(path.read_text(encoding='utf-8', errors='strict'))
        except (ValueError, OSError) as error:
            raise ValueError('SVG file could not be parsed.') from error
        return {"kind": "image", "format": "SVG", "bytes": size, "result": "SVG verified: well-formed vector markup."}
    if suffix in (".mp3", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".webm", ".mkv"):
        kind = "audio" if suffix in (".mp3", ".m4a", ".aac", ".flac", ".ogg") else "video"
        duration = probe_audio_duration(path)
        if duration is not None:
            return {"kind": kind, "format": suffix[1:].upper(), "bytes": size, "duration_seconds": duration,
                    "result": "%s verified: %.2f seconds." % (suffix[1:].upper(), duration)}
        return {
            "kind": kind,
            "format": suffix[1:].upper(),
            "bytes": size,
            "duration_seconds": None,
            "result": (
                "%s file present (%d bytes) but duration could not be "
                "probed: ffprobe is unavailable. Do not claim a duration or "
                "transcript for it." % (suffix[1:].upper(), size)
            ),
        }
    raise ValueError(
        "Viewing supports PNG, JPEG, WebP, GIF, SVG, PDF, WAV, MP3, M4A, AAC, "
        "FLAC, OGG, MP4, MOV, WebM, and MKV files."
    )
