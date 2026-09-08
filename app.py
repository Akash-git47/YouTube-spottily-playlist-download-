"""
YouTube Downloader - Flask Web Application
Paste a YouTube video or playlist link and download it as MP3 (audio) or MP4 (video).
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote, urlparse

from flask import Flask, jsonify, render_template, request, send_file
import yt_dlp


def _find_youtube_cookies():
    """Locate a YouTube cookies.txt file for yt-dlp authentication."""
    candidates = [
        os.environ.get('YOUTUBE_COOKIES'),
        os.path.join(BASE_DIR, 'cookies.txt'),
        '/etc/secrets/cookies.txt',
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, 'temp')
os.makedirs(TEMP_DIR, exist_ok=True)

YOUTUBE_COOKIES = _find_youtube_cookies()

# Clear leftovers from previous runs.
for old in os.listdir(TEMP_DIR):
    shutil.rmtree(os.path.join(TEMP_DIR, old), ignore_errors=True)

MAX_ITEMS = 1000  # safety cap for playlist batches

jobs = {}
jobs_lock = threading.Lock()

INFO_CACHE_TTL = 30 * 60
info_cache = {}
info_cache_lock = threading.Lock()


def get_cached_info(url):
    with info_cache_lock:
        entry = info_cache.get(url)
        if entry and time.time() - entry[0] < INFO_CACHE_TTL:
            return entry[1]
    return None


def set_cached_info(url, payload):
    with info_cache_lock:
        info_cache[url] = (time.time(), payload)
        if len(info_cache) > 300:
            cutoff = time.time() - INFO_CACHE_TTL
            for k in [k for k, (t, _) in info_cache.items() if t < cutoff]:
                info_cache.pop(k, None)

QUALITY_HEIGHTS = {
    '144p': 144,
    '240p': 240,
    '360p': 360,
    '480p': 480,
    '720p': 720,
    '1080p': 1080,
}

YDL_BASE_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'ignoreerrors': True,
    'retries': 5,
    'fragment_retries': 5,
    'extractor_retries': 3,
    'socket_timeout': 45,
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    },
}
if YOUTUBE_COOKIES:
    YDL_BASE_OPTS['cookiefile'] = YOUTUBE_COOKIES


class DownloadJob:
    def __init__(self, job_id, items, file_type, quality):
        self.job_id = job_id
        self.items = items          # list of {'url', 'title'}
        self.file_type = file_type  # 'mp3' | 'mp4'
        self.quality = quality      # quality label for mp4
        self.status = 'queued'      # queued | running | done | error
        self.progress = 0
        self.message = 'Waiting to start...'
        self.current = ''
        self.output_path = None
        self.error = None
        self.failed = []
        self.folder = os.path.join(TEMP_DIR, job_id)
        self.chunk_limit = 8
        self.zip_folder = None


def safe_name(name):
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\-. ]+', '', name or '')).strip() or 'download'


def duration_str(seconds):
    if not seconds:
        return ''
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def probe_duration(path):
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def encode_mp3(source, target, bitrate='192k', max_chunks=None):
    """Convert any audio file to MP3. Files over 10 minutes are split into
    chunks and encoded in parallel (mp3 frames concatenate cleanly with
    -c copy); everything else is a single fast VBR pass. Chunks live in a
    scratch dir with safe names, so song titles with special characters
    never break the concat step."""
    duration = probe_duration(source)
    if duration > 600:
        n = max(2, min(os.cpu_count() or 4, int(duration // 30)))
        n = min(n, 8)
        if max_chunks:
            n = max(2, min(n, max_chunks))
    else:
        n = 1

    if n == 1:
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', source,
            '-vn', '-c:a', 'libmp3lame', '-compression_level', '0',
            '-q:a', '5', target,
        ], check=True)
        return

    workdir = tempfile.mkdtemp(dir=os.path.dirname(os.path.abspath(target)))
    try:
        chunk_len = duration / n
        chunks = [os.path.join(workdir, f'p{i}.mp3') for i in range(n)]
        procs = [
            subprocess.Popen([
                'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-ss', str(i * chunk_len), '-i', source,
                '-t', str(chunk_len), '-vn',
                '-c:a', 'libmp3lame', '-compression_level', '0',
                '-q:a', '5', chunk,
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for i, chunk in enumerate(chunks)
        ]
        for p in procs:
            p.wait()

        list_file = os.path.join(workdir, 'list.txt')
        with open(list_file, 'w') as f:
            for c in chunks:
                f.write(f"file '{c}'\n")
        subprocess.run([
            'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'concat', '-safe', '0', '-i', list_file, '-c', 'copy', target,
        ], check=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def is_playlist_url(url):
    try:
        query = urlparse(url).query
    except Exception:
        return False
    return 'list' in parse_qs(query)


def is_instagram_url(url):
    try:
        host = (urlparse(url).netloc or '').lower()
    except Exception:
        return False
    return host in ('www.instagram.com', 'instagram.com', 'm.instagram.com', 'instagr.am')


def is_spotify_url(url):
    try:
        host = (urlparse(url).netloc or '').lower()
    except Exception:
        return False
    return host == 'open.spotify.com'


SPOTIFY_TYPE_RE = re.compile(r'/(track|playlist|album|artist)/([A-Za-z0-9]{22})')


def parse_spotify_url(url):
    """Return (spotify_type, spotify_id) or None for an open.spotify.com URL."""
    m = SPOTIFY_TYPE_RE.search(url or '')
    if not m:
        return None
    return m.group(1), m.group(2)


def spotify_embed_html(spotify_id):
    """Fetch the public embed page for a Spotify track/playlist (no API key).
    ``spotify_id`` should include the type segment, e.g. 'track/XXXX'."""
    url = f'https://open.spotify.com/embed/{spotify_id}'
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'},
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:  # transient errors; retry with backoff
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise last_err


def spotify_next_data(html):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def spotify_duration_text(ms):
    if not ms:
        return ''
    total = int(ms) // 1000
    m, s = divmod(total, 60)
    return f'{m}:{s:02d}'


def spotify_track_thumbnail(tid):
    """Album art for a single Spotify track, fetched from its embed page."""
    if not tid:
        return ''
    data = spotify_next_data(spotify_embed_html(f'track/{tid}'))
    entity = find_in(data, 'entity') or {}
    if not isinstance(entity, dict):
        return ''
    images = (entity.get('visualIdentity') or {}).get('image') or []
    if not images:
        return ''
    # Take the largest artwork so the card looks sharp.
    return max((i for i in images if i.get('url')), key=lambda i: i.get('maxWidth') or 0).get('url') or ''


def find_in(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = find_in(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_in(v, key)
            if r is not None:
                return r
    return None


def fetch_spotify_track(url):
    parsed = parse_spotify_url(url)
    if not parsed:
        raise ValueError('Invalid Spotify link.')
    stype, sid = parsed
    if stype == 'album':
        raise ValueError('Albums are not supported - paste a Spotify song or playlist link instead.')
    if stype != 'track':
        raise ValueError('Use a Spotify song or playlist link.')

    data = spotify_next_data(spotify_embed_html(f'{stype}/{sid}'))
    entity = find_in(data, 'entity') or {}
    if not isinstance(entity, dict):
        entity = {}
    title = (entity.get('title') or entity.get('name') or '').strip()
    artists = [a.get('name') for a in (entity.get('artists') or []) if a.get('name')]
    artist = ', '.join(artists)
    if not title:
        raise ValueError('Could not read that Spotify track.')
    images = (entity.get('visualIdentity') or {}).get('image') or []
    thumb = ''
    if images:
        thumb = max((i for i in images if i.get('url')), key=lambda i: i.get('maxWidth') or 0).get('url') or ''
    return {
        'type': 'spotify_track',
        'id': sid,
        'title': title,
        'artist': artist,
        'duration': spotify_duration_text(entity.get('duration')),
        'thumbnail': thumb,
        'url': f'https://open.spotify.com/track/{sid}',
        'query': f'{title} {artist}'.strip(),
    }


# ---------- Full Spotify playlists without any account or Premium ----------
#
# The Web API now requires the app owner to have a Spotify Premium
# subscription, so instead we read Spotify's own public pages exactly like
# the web player does: the anonymous access token is bootstrapped from the
# public embed page, then the GraphQL pathfinder API is paginated for the
# full track list. No API key, no login, no Premium - works for anyone.

def spotify_scrape_entity(url):
    """Full playlist/artist track lists from Spotify's public pages."""
    from spotify_scraper import SpotifyClient

    stype, sid = parse_spotify_url(url)
    if stype == 'album':
        raise ValueError('Albums are not supported - paste a Spotify song, playlist, or artist link instead.')
    if stype not in ('playlist', 'artist'):
        raise ValueError('Use a Spotify song, playlist, or artist link.')

    def best_image(images):
        best, best_w = '', 0
        for img in (images or []):
            try:
                w = int(getattr(img, 'width', 0) or 0)
            except Exception:
                w = 0
            url = getattr(img, 'url', '') or ''
            if w > best_w and url:
                best, best_w = url, w
        return best

    # The anonymous token + pathfinder API can rate-limit transiently, so
    # retry a few times before the caller falls back to the 100-track embed.
    last_err = None
    for attempt in range(3):
        try:
            with SpotifyClient() as client:
                if stype == 'playlist':
                    pl = client.get_playlist(url, max_tracks=None)
                    title = (pl.name or '').strip() or 'Spotify Playlist'
                    tracks = [pt.track for pt in (pl.tracks or []) if getattr(pt, 'track', None)]
                    cover = best_image(pl.images)
                    if not cover and tracks:
                        cover = best_image(tracks[0].images)
                else:  # artist -> top tracks
                    ar = client.get_artist(url)
                    title = (ar.name or '').strip() or 'Spotify Artist'
                    tracks = list(ar.top_tracks or [])
                    cover = best_image(ar.images)
            break
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    else:
        raise last_err

    entries = []
    for track in tracks[:MAX_ITEMS]:
        tname = (track.name or '').strip()
        if not tname:
            continue
        tid = getattr(track, 'id', '') or ''
        artist = ', '.join(
            a.name for a in (track.artists or []) if getattr(a, 'name', '')
        )
        thumb = best_image(track.images) or cover
        entries.append({
            'id': tid or tname,
            'title': tname,
            'artist': artist,
            'thumbnail': thumb,
            'duration': spotify_duration_text(getattr(track, 'duration_ms', None)),
            'url': f'https://open.spotify.com/track/{tid}' if tid else '',
            'query': f'{tname} {artist}'.strip(),
        })
    if not entries:
        raise ValueError('Could not read that Spotify link - it may be private or empty.')
    return {
        'type': 'spotify_playlist',
        'title': title,
        'cover': cover,
        'entries': entries,
    }


def fetch_spotify_playlist(url):
    parsed = parse_spotify_url(url)
    if not parsed:
        raise ValueError('Invalid Spotify link.')
    stype, sid = parsed
    if stype == 'album':
        raise ValueError('Albums are not supported - paste a Spotify song, playlist, or artist link instead.')
    if stype not in ('playlist', 'artist'):
        raise ValueError('Use a Spotify song, playlist, or artist link.')

    data = spotify_next_data(spotify_embed_html(f'{stype}/{sid}'))

    entity = find_in(data, 'entity') or {}
    if not isinstance(entity, dict):
        entity = {}
    name = (entity.get('name') or '').strip() or 'Spotify Playlist'
    track_list = find_in(data, 'trackList') or []
    cover = ''
    cover_art = find_in(data, 'coverArt')
    if isinstance(cover_art, dict):
        for src in sorted((cover_art.get('sources') or []), key=lambda s: s.get('width') or 0, reverse=True):
            if src.get('url'):
                cover = src['url']
                break
    if not cover:
        # Artists expose their image via visualIdentity instead of coverArt.
        for src in sorted((entity.get('visualIdentity') or {}).get('image') or [], key=lambda s: s.get('maxWidth') or 0, reverse=True):
            if src.get('url'):
                cover = src['url']
                break

    entries = []
    for t in track_list[:MAX_ITEMS]:
        if not isinstance(t, dict):
            continue
        uri = t.get('uri') or ''
        tid = uri.split(':')[-1] if uri.startswith('spotify:track:') else ''
        title = (t.get('title') or '').strip()
        if not title:
            continue
        artist = (t.get('subtitle') or '').strip()
        entries.append({
            'id': tid or title,
            'title': title,
            'artist': artist,
            'thumbnail': cover,
            'duration': spotify_duration_text(t.get('duration')),
            'url': f'https://open.spotify.com/track/{tid}' if tid else '',
            'query': f'{title} {artist}'.strip(),
        })
    if not entries:
        raise ValueError('Could not read that Spotify playlist - it may be private or empty.')

    # Give each song its own album art instead of the playlist cover.
    # The playlist embed page has no per-track images, so fetch each track's
    # embed page in parallel. Failures fall back to the playlist cover.
    if any(e['thumbnail'] and e['id'] for e in entries):
        def thumb_for(e):
            try:
                return spotify_track_thumbnail(e['id']) if e['id'] else ''
            except Exception:
                return ''
        with ThreadPoolExecutor(max_workers=12) as pool:
            thumbs = list(pool.map(thumb_for, entries))
        for entry, thumb in zip(entries, thumbs):
            if thumb:
                entry['thumbnail'] = thumb

    return {
        'type': 'spotify_playlist',
        'title': name,
        'cover': cover,
        'entries': entries,
    }


def available_qualities(info):
    heights = set()
    for f in (info.get('formats') or []):
        h = f.get('height')
        vcodec = f.get('vcodec') or ''
        if h and vcodec != 'none':
            heights.add(h)
    labels = []
    for label, h in sorted(QUALITY_HEIGHTS.items(), key=lambda item: item[1]):
        if h in heights:
            labels.append(label)
    return labels


def fetch_instagram_post(url):
    """Extract Instagram post info using Playwright (headless browser).

    Returns a dict with type='video' or type='playlist' (for carousels),
    containing direct CDN URLs for images/videos so they can be downloaded
    without further Instagram authentication."""
    from playwright.sync_api import sync_playwright

    # Extract shortcode from URL
    m = re.search(r'/(?:p|reel|tv|reels)/([A-Za-z0-9_-]+)', url)
    if not m:
        raise ValueError('Invalid Instagram URL – expected a /p/, /reel/, or /tv/ link.')
    shortcode = m.group(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
        )
        ctx = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 900},
        )
        page = ctx.new_page()

        # Intercept CDN media responses (images and videos)
        cdn_media = []

        def _on_response(response):
            rurl = response.url
            ct = response.headers.get('content-type', '')
            cl = response.headers.get('content-length', '0')
            if ('scontent' in rurl or 'fbcdn' in rurl or 'cdninstagram' in rurl):
                if 'video' in ct or 'image' in ct or (cl.isdigit() and int(cl) > 50000):
                    cdn_media.append({
                        'url': rurl, 'ct': ct,
                        'cl': int(cl) if cl.isdigit() else 0,
                    })

        page.on('response', _on_response)

        page.goto(url, wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(2000)

        # Dismiss cookie / login popups
        for _ in range(3):
            for sel in ['button:has-text("Accept")', 'button:has-text("Allow all cookies")',
                        'button:has-text("Not Now")', '[aria-label="Close"]']:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(300)
                except Exception:
                    pass

        page.wait_for_timeout(5000)

        # Read OG meta tags (always present in the server-rendered HTML)
        og = page.evaluate('''() => {
            const metas = document.querySelectorAll('meta[property^="og:"], meta[name^="og:"]');
            const out = {};
            for (const m of metas) {
                const key = m.getAttribute('property') || m.getAttribute('name');
                if (key) out[key] = m.getAttribute('content') || '';
            }
            return out;
        }''')

        title = og.get('og:title', '').split(' on Instagram:')[0].strip() or shortcode
        description = og.get('og:description', '')
        image_url = og.get('og:image', '')

        # Determine if this is a video post
        is_video = any('video' in (m.get('ct') or '') for m in cdn_media)

        browser.close()

    if is_video:
        # Pick the largest video CDN response as the main video
        videos = [m for m in cdn_media if 'video' in m['ct'] and m['cl'] > 100000]
        if not videos:
            # Fallback: any video response
            videos = [m for m in cdn_media if 'video' in m['ct']]
        best_video = max(videos, key=lambda m: m['cl']) if videos else None
        return {
            'type': 'video',
            'id': shortcode,
            'title': title,
            'thumbnail': image_url,
            'duration': '',
            'url': best_video['url'] if best_video else image_url,
            'qualities': [],
            'source': 'instagram',
            'caption': description,
        }
    else:
        # Image post – use og:image as the direct download URL
        if not image_url:
            raise ValueError('Could not extract Instagram image. The post may be private or require login.')
        return {
            'type': 'video',
            'id': shortcode,
            'title': title,
            'thumbnail': image_url,
            'duration': '',
            'url': image_url,
            'qualities': [],
            'source': 'instagram',
            'caption': description,
        }


def _proxy_youtube_info(url):
    """Fallback: fetch YouTube video info via a public proxy API when yt-dlp is
    blocked by YouTube's datacenter IP restrictions.  Returns a dict matching
    the format of fetch_video_info, or raises on failure."""
    # Extract video ID from URL
    m = re.search(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})', url)
    if not m:
        raise ValueError('Could not extract video ID.')
    vid = m.group(1)

    # Try multiple public Invidious instances
    invidious_instances = [
        'https://inv.nadeko.net',
        'https://invidious.nerdvpn.de',
        'https://vid.puffyan.us',
        'https://invidious.private.coffee',
    ]
    for instance in invidious_instances:
        try:
            api_url = f'{instance}/api/v1/videos/{vid}'
            req = urllib.request.Request(api_url, headers={
                'User-Agent': YDL_BASE_OPTS['http_headers']['User-Agent'],
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if data.get('error'):
                continue
            title = data.get('title') or 'Unknown'
            thumb = data.get('videoThumbnails') or []
            # Pick highest-res thumbnail
            best_thumb = ''
            for t in thumb:
                if t.get('quality') == 'maxres':
                    best_thumb = t.get('url', '')
                    break
                if t.get('quality') == 'sddefault' and not best_thumb:
                    best_thumb = t.get('url', '')
            if not best_thumb and thumb:
                best_thumb = thumb[0].get('url', '')
            if not best_thumb:
                best_thumb = f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'

            return {
                'type': 'video',
                'id': vid,
                'title': title,
                'thumbnail': best_thumb if best_thumb.startswith('http') else f'{instance}{best_thumb}',
                'duration': duration_str(data.get('lengthSeconds')),
                'url': url,
                'qualities': [{'label': '360p', 'value': '360p'}],
                'source': 'youtube_proxy',
            }
        except Exception:
            continue

    raise ValueError(
        'YouTube is blocking this server\'s IP. '
        'You can upload a cookies.txt file to enable downloads, '
        'or try again later.'
    )


def fetch_video_info(url):
    # If it's a YouTube URL, try yt-dlp first, then fall back to proxy.
    is_yt = bool(re.search(r'(youtube\.com|youtu\.be)', url))
    if is_yt:
        try:
            return _fetch_video_info_ytdlp(url)
        except Exception:
            return _proxy_youtube_info(url)
    return _fetch_video_info_ytdlp(url)


def _fetch_video_info_ytdlp(url):
    opts = dict(YDL_BASE_OPTS)
    opts.update({'skip_download': True, 'noplaylist': True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ValueError('Could not fetch the video. It may be private or unavailable.')
    # yt-dlp may return a playlist for multi-media posts (Instagram carousels,
    # Reddit galleries).  Detect and surface every item so the user can pick.
    if info.get('_type') == 'playlist' and info.get('entries'):
        entries = []
        for e in info['entries']:
            if not e:
                continue
            vid = e.get('id')
            if not vid:
                continue
            thumb = (e.get('thumbnails') or [{}])[0].get('url') if e.get('thumbnails') else None
            entries.append({
                'id': vid,
                'title': e.get('title') or info.get('title') or 'Unknown',
                'thumbnail': thumb or info.get('thumbnail'),
                'duration': duration_str(e.get('duration')),
                'url': e.get('url') or f'https://www.youtube.com/watch?v={vid}',
                'ie_key': e.get('ie_key', ''),
            })
        if entries:
            return {
                'type': 'playlist',
                'title': info.get('title'),
                'entries': entries,
            }
    return {
        'type': 'video',
        'id': info.get('id'),
        'title': info.get('title'),
        'thumbnail': info.get('thumbnail'),
        'duration': duration_str(info.get('duration')),
        'url': url,
        'qualities': available_qualities(info),
    }


def fetch_playlist_info(url):
    is_yt = bool(re.search(r'(youtube\.com|youtu\.be)', url))
    if is_yt:
        try:
            return _fetch_playlist_info_ytdlp(url)
        except Exception:
            return _fetch_playlist_info_proxy(url)
    return _fetch_playlist_info_ytdlp(url)


def _fetch_playlist_info_ytdlp(url):
    opts = dict(YDL_BASE_OPTS)
    opts.update({
        'skip_download': True,
        'extract_flat': True,
        'playlistend': MAX_ITEMS,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ValueError('Could not fetch the playlist. It may be private or unavailable.')
    entries = []
    for e in (info.get('entries') or []):
        if not e:
            continue
        vid = e.get('id')
        if not vid:
            continue
        entry_url = e.get('url') or f'https://www.youtube.com/watch?v={vid}'
        thumb = (e.get('thumbnails') or [{}])[0].get('url') if e.get('thumbnails') else None
        if not thumb:
            thumb = f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
        entries.append({
            'id': vid,
            'title': e.get('title') or 'Unknown',
            'thumbnail': thumb,
            'duration': duration_str(e.get('duration')),
            'url': entry_url,
            'ie_key': e.get('ie_key', ''),
        })
    return {
        'type': 'playlist',
        'title': info.get('title'),
        'entries': entries,
    }


def _fetch_playlist_info_proxy(url):
    """Fetch YouTube playlist via Invidious proxy."""
    # Extract playlist ID from URL
    m = re.search(r'[?&]list=([A-Za-z0-9_-]+)', url)
    if not m:
        raise ValueError('Could not extract playlist ID.')
    plid = m.group(1)

    invidious_instances = [
        'https://inv.nadeko.net',
        'https://invidious.nerdvpn.de',
        'https://vid.puffyan.us',
        'https://invidious.private.coffee',
    ]
    for instance in invidious_instances:
        try:
            api_url = f'{instance}/api/v1/playlists/{plid}'
            req = urllib.request.Request(api_url, headers={
                'User-Agent': YDL_BASE_OPTS['http_headers']['User-Agent'],
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            if data.get('error'):
                continue
            entries = []
            for e in (data.get('videos') or [])[:MAX_ITEMS]:
                vid = e.get('videoId')
                if not vid:
                    continue
                thumb = e.get('videoThumbnails') or []
                best_thumb = f'https://i.ytimg.com/vi/{vid}/hqdefault.jpg'
                for t in thumb:
                    if t.get('quality') == 'sddefault':
                        best_thumb = t.get('url', best_thumb)
                        break
                entries.append({
                    'id': vid,
                    'title': e.get('title') or 'Unknown',
                    'thumbnail': best_thumb if best_thumb.startswith('http') else f'{instance}{best_thumb}',
                    'duration': duration_str(e.get('lengthSeconds')),
                    'url': f'https://www.youtube.com/watch?v={vid}',
                    'ie_key': '',
                })
            if entries:
                return {
                    'type': 'playlist',
                    'title': data.get('title') or 'YouTube Playlist',
                    'entries': entries,
                }
        except Exception:
            continue
    raise ValueError(
        'YouTube is blocking this server\'s IP. '
        'Upload a cookies.txt file to enable playlist downloads.'
    )


@app.route('/api/info', methods=['POST'])
def api_info():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'error': 'Please enter a YouTube or Spotify link.'}), 400
    cached = get_cached_info(url)
    if cached is not None:
        return jsonify(cached)
    try:
        if is_spotify_url(url):
            stype, _ = parse_spotify_url(url) or (None, None)
            if stype in ('playlist', 'artist'):
                # Full track list via Spotify's public pages (no API key, no
                # Premium). Falls back to the 100-track embed if that fails.
                try:
                    payload = spotify_scrape_entity(url)
                except Exception:
                    payload = fetch_spotify_playlist(url)
            else:
                payload = fetch_spotify_track(url)
        elif is_instagram_url(url):
            # Instagram requires browser rendering; use Playwright.
            payload = fetch_instagram_post(url)
        elif is_playlist_url(url):
            # YouTube playlist (detected via ?list= query param).
            payload = fetch_playlist_info(url)
        else:
            # For any other URL, try flat extraction first.  This detects
            # playlists from Vimeo, SoundCloud, Dailymotion, and multi-media
            # posts (Instagram carousels, Reddit galleries).  If flat
            # extraction finds entries we return them directly; otherwise we
            # fall back to full single-video extraction.
            try:
                payload = fetch_playlist_info(url)
                if not payload.get('entries'):
                    # Flat extraction returned no entries — treat as single video.
                    payload = fetch_video_info(url)
            except Exception:
                payload = fetch_video_info(url)
        set_cached_info(url, payload)
        return jsonify(payload)
    except Exception as e:
        return jsonify({'error': f'{e}'}), 500


@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    file_type = data.get('type')
    quality = data.get('quality') or '720p'

    if file_type not in ('mp3', 'mp4'):
        return jsonify({'error': 'Invalid download type.'}), 400
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'No videos selected.'}), 400
    if len(items) > MAX_ITEMS:
        return jsonify({'error': f'Too many videos (max {MAX_ITEMS}).'}), 400
    for item in items:
        if not isinstance(item, dict) or not str(item.get('url', '')).strip():
            return jsonify({'error': 'Invalid video item.'}), 400

    job_id = uuid.uuid4().hex
    clean_items = []
    for item in items:
        clean = {'url': item['url'].strip(), 'title': str(item.get('title') or '')}
        if item.get('query'):
            clean['query'] = str(item['query'])
        clean_items.append(clean)
    job = DownloadJob(job_id, clean_items, file_type, quality)
    if any(i.get('query') for i in clean_items) and len(clean_items) > 1:
        job.zip_folder = 'Spotify'
    with jobs_lock:
        jobs[job_id] = job

    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return jsonify({'job_id': job_id})


def build_download_opts(job, index, hook):
    item = job.items[index]
    if item.get('query'):
        # Spotify-originated item: name the file after the song, not the YouTube title.
        base = safe_name(item.get('title') or 'track')
        outtmpl = os.path.join(job.folder, base + '.%(ext)s')
        if len(job.items) > 1:
            outtmpl = os.path.join(job.folder, f'{index + 1:02d} - {base}.%(ext)s')
    else:
        outtmpl = os.path.join(job.folder, '%(title)s.%(ext)s')
        if len(job.items) > 1:
            outtmpl = os.path.join(job.folder, f'{index + 1:02d} - %(title)s.%(ext)s')

    opts = dict(YDL_BASE_OPTS)
    # Downloads must raise on failure (HTTP 403 etc.) so the retry logic in
    # run_job can retry instead of silently producing nothing.
    opts['ignoreerrors'] = False
    # The web player client is throttled (HTTP 403) on this network; the
    # android client is not. Always prefer it for the actual download.
    opts['extractor_args'] = {'youtube': {'player_client': ['android']}}
    opts.update({
        'outtmpl': outtmpl,
        'noplaylist': True,
        'progress_hooks': [hook],
    })

    if job.file_type == 'mp3':
        # Download the raw audio; encoding to MP3 is done in download_item
        # with a fast parallel encoder.
        opts['format'] = 'bestaudio/best'
    else:
        h = QUALITY_HEIGHTS.get(job.quality, 1080)
        # Fast path: native MP4 (H.264, else AV1) -> remux, no re-encode.
        # Slow fallback: VP9/webm -> re-encode to MP4 (veryfast preset).
        opts['format'] = (
            f'bv*[height<={h}][ext=mp4][vcodec^=avc1]+ba[ext=m4a]/'
            f'b[height<={h}][ext=mp4]/'
            f'bv*[height<={h}][ext=mp4]+ba[ext=m4a]/'
            f'bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/'
            f'bv*[height<={h}]+ba[ext=m4a]/'
            f'bv*[height<={h}]+ba/'
            f'b[height<={h}]/b'
        )
        opts['postprocessors'] = [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }]
        opts['postprocessor_args'] = {
            'videoconvertor': [
                '-preset', 'veryfast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '192k',
            ],
        }

    return opts


def clean_query_part(text):
    """Strip noise from a search query: parentheticals, '2003 Remaster'
    style suffixes, extra spaces - so YouTube matches the right song."""
    text = re.sub(r'[\(\[].*?[\)\]]', ' ', text)
    text = re.sub(r'\s*-\s*\d{4}\s+(Remaster(ed)?|Version|Mix|Edit)\b.*$', '', text, flags=re.I)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip()


def build_query_fallbacks(item):
    """Ordered list of YouTube search queries to try, simplest fallbacks last."""
    q = (item.get('query') or '').strip()
    title = (item.get('title') or '').strip()
    artist = (item.get('artist') or '').strip()
    variants = []
    if q:
        variants.append(q)
    if title and artist:
        clean_title = clean_query_part(title)
        variants.append(f'{clean_title} {artist}')
        if clean_title != title:
            variants.append(clean_title)
        variants.append(f'{title} {artist}')
    elif title:
        variants.append(title)
    seen, out = set(), []
    for v in variants:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out or [None]


def search_candidates(query, limit=3):
    """Top `limit` YouTube video URLs for a search query (flat, fast)."""
    # Try yt-dlp first, fall back to Invidious search API.
    try:
        opts = dict(YDL_BASE_OPTS)
        opts.update({'skip_download': True, 'process': False, 'extract_flat': True})
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f'ytsearch{limit}:{query}', download=False)
        urls = []
        for e in (info.get('entries') or []):
            vid = e.get('id')
            if vid:
                urls.append(f'https://www.youtube.com/watch?v={vid}')
        if urls:
            return urls
    except Exception:
        pass

    # Fallback: Invidious search API
    invidious_instances = [
        'https://inv.nadeko.net',
        'https://invidious.nerdvpn.de',
        'https://vid.puffyan.us',
        'https://invidious.private.coffee',
    ]
    for instance in invidious_instances:
        try:
            api_url = f'{instance}/api/v1/search?q={urllib.parse.quote(query)}&type=video&sort_by=relevance'
            req = urllib.request.Request(api_url, headers={
                'User-Agent': YDL_BASE_OPTS['http_headers']['User-Agent'],
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            urls = []
            for entry in (data or [])[:limit]:
                vid = entry.get('videoId')
                if vid:
                    urls.append(f'https://www.youtube.com/watch?v={vid}')
            if urls:
                return urls
        except Exception:
            continue
    return []


def _download_url(job, item, index, url):
    """Download one concrete YouTube URL, then encode to MP3 if needed."""
    # Instagram CDN URLs need direct HTTP download (yt-dlp can't handle them).
    if item.get('source') == 'instagram':
        _download_instagram(job, item, index, url)
        return

    # YouTube: try yt-dlp first, then fall back to proxy download.
    is_yt = bool(re.search(r'(youtube\.com|youtu\.be)', url))
    if is_yt:
        try:
            _download_url_ytdlp(job, item, index, url)
            return
        except Exception:
            _download_url_proxy(job, item, index, url)
            return

    _download_url_ytdlp(job, item, index, url)


def _download_url_proxy(job, item, index, url):
    """Download a YouTube video via a public Invidious proxy instance."""
    m = re.search(r'(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})', url)
    if not m:
        raise ValueError('Could not extract video ID for proxy download.')
    vid = m.group(1)

    invidious_instances = [
        'https://inv.nadeko.net',
        'https://invidious.nerdvpn.de',
        'https://vid.puffyan.us',
        'https://invidious.private.coffee',
    ]
    for instance in invidious_instances:
        try:
            api_url = f'{instance}/api/v1/videos/{vid}'
            req = urllib.request.Request(api_url, headers={
                'User-Agent': YDL_BASE_OPTS['http_headers']['User-Agent'],
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if data.get('error'):
                continue

            title = safe_name(data.get('title') or item.get('title') or f'video_{index + 1}')

            # Pick best available stream
            streams = data.get('formatStreams') or []
            # Prefer mp4, then highest quality
            best = None
            for s in streams:
                if 'mp4' in (s.get('type') or ''):
                    best = s
                    break
            if not best and streams:
                best = streams[-1]  # highest quality last

            if not best:
                continue

            stream_url = best.get('url')
            if not stream_url:
                continue

            ext = '.mp4'
            if job.file_type == 'mp3':
                ext = '.webm'  # download audio-only format if available
                for s in (data.get('adaptiveFormats') or []):
                    if 'audio' in (s.get('type') or '') and s.get('url'):
                        stream_url = s['url']
                        ext = '.webm'
                        break

            if len(job.items) > 1:
                fname = f'{index + 1:02d} - {title}{ext}'
            else:
                fname = f'{title}{ext}'
            dest = os.path.join(job.folder, fname)

            job.message = 'Downloading via proxy...'
            req = urllib.request.Request(stream_url, headers={
                'User-Agent': YDL_BASE_OPTS['http_headers']['User-Agent'],
                'Referer': f'{instance}/',
            })
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(dest, 'wb') as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)

            if job.file_type == 'mp3':
                target = os.path.splitext(dest)[0] + '.mp3'
                job.message = 'Encoding...'
                encode_mp3(dest, target, max_chunks=job.chunk_limit)
                os.remove(dest)

            return
        except Exception:
            continue

    raise RuntimeError('All proxy instances failed. YouTube may be temporarily unavailable.')


def _download_url_ytdlp(job, item, index, url):

    downloaded = {'file': None}
    before = set(os.listdir(job.folder))

    def hook(d):
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            done = d.get('downloaded_bytes') or 0
            if total:
                job.message = f'Downloading {int(done / total * 100)}%'
            else:
                job.message = 'Downloading...'
        elif d.get('status') == 'finished':
            job.message = 'Converting...'
            downloaded['file'] = d.get('filename')

    opts = build_download_opts(job, index, hook)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])

    if job.file_type != 'mp3':
        # Postprocessors may leave temporary files; the final MP4 is what
        # matters. If nothing usable was produced, raise so the retry/candidate
        # logic kicks in instead of silently skipping the video.
        new = set(os.listdir(job.folder)) - before
        final = [f for f in new if not f.endswith(('.part', '.ytdl', '.temp.mp4', '.webm.temp'))]
        if not final:
            raise RuntimeError('Download failed - no video file was produced')
        return

    src = downloaded['file']
    if not src or not os.path.isfile(src):
        raise RuntimeError('Downloaded audio file not found')
    target = os.path.splitext(src)[0] + '.mp3'
    job.message = 'Encoding...'
    try:
        encode_mp3(src, target, max_chunks=job.chunk_limit)
        os.remove(src)
    except Exception as e:
        if os.path.isfile(src):
            os.remove(src)
        raise RuntimeError(f'MP3 encoding failed: {e}') from e


def _download_instagram(job, item, index, url):
    """Download an Instagram post (image or video) using Playwright."""
    import base64
    from playwright.sync_api import sync_playwright

    title = safe_name(item.get('title') or f'instagram_{index + 1}')
    ext_lower = url.lower()

    # Images: direct HTTP download works fine.
    if any(ext_lower.endswith(ext) for ext in ('.jpg', '.jpeg', '.png', '.webp')):
        ext = '.jpg'
        if '.png' in ext_lower: ext = '.png'
        elif '.webp' in ext_lower: ext = '.webp'
        if len(job.items) > 1:
            fname = f'{index + 1:02d} - {title}{ext}'
        else:
            fname = f'{title}{ext}'
        dest = os.path.join(job.folder, fname)
        job.message = 'Downloading...'
        req = urllib.request.Request(url, headers={
            'User-Agent': YDL_BASE_OPTS['http_headers']['User-Agent'],
            'Referer': 'https://www.instagram.com/',
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            with open(dest, 'wb') as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        return

    # Videos: the CDN URL is a DASH segment.  Use Playwright to fetch the
    # blob URL (complete merged video) from the page context.
    shortcode = item.get('id', '')
    page_url = f'https://www.instagram.com/reel/{shortcode}/'

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
        )
        ctx = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            viewport={'width': 1280, 'height': 900},
        )
        page = ctx.new_page()
        page.goto(page_url, wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(3000)
        for _ in range(3):
            for sel in ['button:has-text("Accept")', 'button:has-text("Not Now")',
                        '[aria-label="Close"]']:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(300)
                except Exception:
                    pass
        page.wait_for_timeout(6000)

        job.message = 'Downloading...'
        # Fetch the blob: URL from inside the page and read its bytes.
        video_data_url = page.evaluate('''async () => {
            const v = document.querySelector('video');
            if (!v || !v.src || !v.src.startsWith('blob:')) return null;
            const resp = await fetch(v.src);
            const blob = await resp.blob();
            return new Promise(res => {
                const r = new FileReader();
                r.onloadend = () => res(r.result);
                r.readAsDataURL(blob);
            });
        }''')
        browser.close()

    if not video_data_url:
        raise RuntimeError('Could not extract Instagram video – the post may be private.')

    _, encoded = video_data_url.split(',', 1)
    file_data = base64.b64decode(encoded)
    if len(job.items) > 1:
        fname = f'{index + 1:02d} - {title}.mp4'
    else:
        fname = f'{title}.mp4'
    dest = os.path.join(job.folder, fname)
    with open(dest, 'wb') as f:
        f.write(file_data)


def download_item(job, item, index):
    """Download one playlist item, trying several YouTube search matches."""
    if not item.get('query'):
        before = set(os.listdir(job.folder))
        try:
            _download_url(job, item, index, item['url'])
        except Exception:
            # Remove any partial files this attempt left behind.
            for f in set(os.listdir(job.folder)) - before:
                try:
                    os.remove(os.path.join(job.folder, f))
                except OSError:
                    pass
            raise
        return
    # Resolve the search to concrete videos so a bad first match doesn't
    # sink the song - fall through to the next candidate on failure.
    candidates = search_candidates(item['query'], limit=3)
    if not candidates:
        raise RuntimeError(f'No YouTube results for "{item["query"]}"')
    last_err = None
    for url in candidates:
        before = set(os.listdir(job.folder))
        try:
            _download_url(job, item, index, url)
            return
        except Exception as e:
            last_err = e
            # Remove any partial files this candidate attempt left behind.
            for f in set(os.listdir(job.folder)) - before:
                try:
                    os.remove(os.path.join(job.folder, f))
                except OSError:
                    pass
    raise last_err


def run_job(job):
    os.makedirs(job.folder, exist_ok=True)
    job.status = 'running'
    total = len(job.items)
    workers = min(10, max(1, total))
    job.chunk_limit = max(1, os.cpu_count() // workers) if os.cpu_count() else 4
    done_count = [0]

    def process(entry):
        i, item = entry
        job.current = item['title'] or item['url']
        # Never skip a song: try progressively simpler search queries, and
        # back off between attempts (YouTube intermittently throttles 403).
        queries = build_query_fallbacks(item)
        last_err = None
        succeeded = False
        for q in queries:
            if item.get('query') is not None:
                item['query'] = q
            for attempt in range(3):
                try:
                    download_item(job, item, i)
                    succeeded = True
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(min(4 * (attempt + 1), 15))
            if succeeded:
                break
        if not succeeded and last_err:
            with jobs_lock:
                job.failed.append(f'{item["title"] or item["url"]} ({last_err})')
        with jobs_lock:
            done_count[0] += 1
            job.progress = int(done_count[0] / total * 100)

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(process, enumerate(job.items)))

        if job.file_type == 'mp3':
            for f in list(os.listdir(job.folder)):
                if not f.lower().endswith('.mp3'):
                    os.remove(os.path.join(job.folder, f))

        files = [f for f in os.listdir(job.folder) if os.path.isfile(os.path.join(job.folder, f))]
        if not files:
            raise RuntimeError('No files were produced. The videos may be unavailable.')

        if total == 1:
            job.output_path = os.path.join(job.folder, files[0])
        else:
            zip_path = os.path.join(job.folder, 'download.zip')
            prefix = f"{job.zip_folder}/" if job.zip_folder else ''
            # Media files are already compressed - use ZIP_STORED for speed.
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:
                used = set()
                for f in sorted(files):
                    base, ext = os.path.splitext(f)
                    name = prefix + f
                    counter = 1
                    while name in used:
                        name = f'{prefix}{base} ({counter}){ext}'
                        counter += 1
                    used.add(name)
                    zf.write(os.path.join(job.folder, f), arcname=name)
            job.output_path = zip_path

        job.status = 'done'
        job.progress = 100
        job.current = ''
        if job.failed:
            job.message = f'Ready - {len(job.failed)} video(s) failed'
        else:
            job.message = 'Ready'
    except Exception as e:
        job.status = 'error'
        job.error = str(e)


@app.route('/api/status/<job_id>')
def api_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found.'}), 404
    payload = {
        'status': job.status,
        'progress': job.progress,
        'message': job.message,
        'current': job.current,
        'error': job.error,
        'failed_count': len(job.failed),
        'failed': list(job.failed),
    }
    if job.status == 'done':
        payload['download_url'] = f'/api/result/{job_id}'
    return jsonify(payload)


@app.route('/api/result/<job_id>')
def api_result(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or not job.output_path or not os.path.exists(job.output_path):
        return jsonify({'error': 'File not found.'}), 404

    if len(job.items) > 1:
        download_name = 'download.zip'
    else:
        ext = os.path.splitext(job.output_path)[1] or '.zip'
        download_name = f'{safe_name(job.items[0]["title"])}{ext}'

    def delayed_cleanup():
        time.sleep(60)
        shutil.rmtree(job.folder, ignore_errors=True)
        with jobs_lock:
            jobs.pop(job_id, None)

    threading.Thread(target=delayed_cleanup, daemon=True).start()
    return send_file(job.output_path, as_attachment=True, download_name=download_name)


@app.route('/')
def index():
    return render_template('index.html')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
