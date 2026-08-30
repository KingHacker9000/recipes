import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


class SocialImportError(RuntimeError):
    pass


_TRACKING_KEYS = {
    'fbclid', 'gclid', 'igshid', 'si', 'utm_campaign', 'utm_content',
    'utm_medium', 'utm_source', 'utm_term',
}

_HOSTS = {
    'tiktok': {'tiktok.com', 'www.tiktok.com', 'm.tiktok.com', 'vm.tiktok.com', 'vt.tiktok.com'},
    'instagram': {'instagram.com', 'www.instagram.com'},
    'youtube': {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be'},
}


@dataclass
class AcquiredSocialPost:
    canonical_url: str
    platform: str
    external_id: str = ''
    creator: str = ''
    caption: str = ''
    thumbnail_url: str = ''
    transcript: str = ''
    keyframes: tuple[str, ...] = ()


def identify_platform(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        raise SocialImportError('Invalid social URL.') from exc
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise SocialImportError('Only normal HTTP(S) social URLs are accepted.')
    host = parts.hostname.lower().rstrip('.')
    for platform, hosts in _HOSTS.items():
        if host in hosts:
            return platform
    raise SocialImportError('Only TikTok, Instagram, and YouTube URLs are supported.')


def canonicalize_url(url: str) -> str:
    platform = identify_platform(url)
    parts = urlsplit(url.strip())
    host = parts.hostname.lower().rstrip('.')
    if platform == 'tiktok' and host == 'm.tiktok.com':
        host = 'www.tiktok.com'
    if platform == 'instagram':
        host = 'www.instagram.com'
    if platform == 'youtube' and host == 'm.youtube.com':
        host = 'www.youtube.com'
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_KEYS and not k.lower().startswith('utm_')]
    path = re.sub(r'/+', '/', parts.path or '/')
    return urlunsplit(('https', host, path, urlencode(query), ''))


def extract_external_id(platform: str, canonical_url: str) -> str:
    parts = urlsplit(canonical_url)
    path = parts.path
    if platform == 'tiktok':
        match = re.search(r'/video/([^/?#]+)', path)
        return match.group(1) if match else ''
    if platform == 'instagram':
        match = re.search(r'/(?:reel|reels|p)/([^/?#]+)', path)
        return match.group(1) if match else ''
    if parts.hostname == 'youtu.be':
        return path.strip('/').split('/')[0]
    match = re.search(r'/shorts/([^/?#]+)', path)
    if match:
        return match.group(1)
    return dict(parse_qsl(parts.query)).get('v', '')


def _run(command: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SocialImportError('Social-media acquisition timed out.') from exc


def _vtt_text(path: Path) -> str:
    lines = []
    previous = None
    for raw in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = raw.strip()
        if not line or line == 'WEBVTT' or '-->' in line or line.isdigit() or line.startswith(('Kind:', 'Language:', 'NOTE ')):
            continue
        line = re.sub(r'<[^>]+>', '', line)
        if line and line != previous:
            lines.append(line)
            previous = line
    return '\n'.join(lines)


def acquire_social_post(url: str, workdir: Path, keyframe_count: int = 8) -> AcquiredSocialPost:
    canonical = canonicalize_url(url)
    platform = identify_platform(canonical)
    ytdlp = shutil.which('yt-dlp')
    if not ytdlp:
        raise SocialImportError('yt-dlp is not installed in the social-import worker.')

    workdir.mkdir(parents=True, exist_ok=True)
    metadata = _run([
        ytdlp, '--no-config', '--dump-single-json', '--skip-download', '--no-playlist', '--no-warnings',
        '--socket-timeout', '20', canonical,
    ])
    if metadata.returncode != 0:
        detail = (metadata.stderr or metadata.stdout or 'unknown yt-dlp error').strip()[-1500:]
        raise SocialImportError(f'Could not read the social post: {detail}')
    try:
        info = json.loads(metadata.stdout)
    except json.JSONDecodeError as exc:
        raise SocialImportError('yt-dlp returned invalid metadata.') from exc

    external_id = str(info.get('id') or extract_external_id(platform, canonical))
    creator = str(info.get('uploader') or info.get('channel') or info.get('creator') or '')
    caption = str(info.get('description') or info.get('title') or '')
    thumbnail = str(info.get('thumbnail') or '')

    subtitle = _run([
        ytdlp, '--no-config', '--skip-download', '--write-subs', '--write-auto-subs',
        '--sub-langs', 'all,-live_chat', '--sub-format', 'vtt', '--no-playlist',
        '--no-warnings', '-o', str(workdir / '%(id)s.%(ext)s'), canonical,
    ], timeout=120)
    transcript_parts = []
    if subtitle.returncode == 0:
        for path in sorted(workdir.glob('*.vtt'))[:8]:
            if path.stat().st_size <= 1_000_000:
                text = _vtt_text(path)
                if text and text not in transcript_parts:
                    transcript_parts.append(text)
    transcript = '\n\n'.join(transcript_parts)[:80_000]

    keyframes = []
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg and keyframe_count > 0:
        media = _run([
            ytdlp, '--no-config', '--no-playlist', '--no-warnings', '--max-filesize', '100M',
            '-f', 'bv*[height<=1080]+ba/b[height<=1080]/b', '--merge-output-format', 'mp4',
            '-o', str(workdir / 'source.%(ext)s'), canonical,
        ], timeout=180)
        if media.returncode == 0:
            candidates = [p for p in workdir.glob('source.*') if p.suffix.lower() not in ('.vtt', '.json')]
            if candidates:
                output = workdir / 'frame-%02d.jpg'
                frames = _run([
                    ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', str(candidates[0]),
                    '-vf', "fps=1/5,scale='min(960,iw)':-2", '-frames:v', str(keyframe_count),
                    '-q:v', '4', str(output),
                ], timeout=120)
                if frames.returncode == 0:
                    keyframes = [str(p) for p in sorted(workdir.glob('frame-*.jpg'))[:keyframe_count]]

    return AcquiredSocialPost(
        canonical_url=canonical,
        platform=platform,
        external_id=external_id,
        creator=creator,
        caption=caption[:20_000],
        thumbnail_url=thumbnail[:2048],
        transcript=transcript,
        keyframes=tuple(keyframes),
    )
