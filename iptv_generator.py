"""Build source-ordered IPTV playlist from publicly available streams.

Source entries are preserved, donors only supply matching channel alternatives unless
APPEND_NEW_CHANNELS=true. Network verification should run in GitHub Actions.
"""
import concurrent.futures as cf
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('iptv')
SOURCE = Path(os.getenv('INPUT_PLAYLIST', 'source_playlist.m3u'))
OUT = Path(os.getenv('OUTPUT_PLAYLIST', 'playlist.m3u'))
CACHE = Path(os.getenv('CACHE_FILE', 'working_links.json'))
REPORT = Path(os.getenv('REPORT_FILE', 'update_report.txt'))
WORKERS = max(1, min(int(os.getenv('MAX_WORKERS', '16')), 32))
TIMEOUT = max(3, int(os.getenv('STREAM_TIMEOUT', '9')))
MAX_CANDIDATES = max(1, int(os.getenv('MAX_CANDIDATES', '5')))
DEEP = os.getenv('DEEP_VIDEO_CHECK', 'true').lower() == 'true'
BLOCKED = ('plist.link', 'ott.watch')
UA = {'User-Agent': 'Mozilla/5.0 (compatible; PublicStreamChecker/2.0)', 'Accept': '*/*'}
# Official iptv-org slices; overlapping regional/country/language lists are deduplicated.
# Public availability does not, by itself, prove redistribution rights for each stream.
DONORS = [
    ('iptv-org-ru', 'https://iptv-org.github.io/iptv/countries/ru.m3u'),
    ('iptv-org-ua', 'https://iptv-org.github.io/iptv/countries/ua.m3u'),
    ('iptv-org-by', 'https://iptv-org.github.io/iptv/countries/by.m3u'),
    ('iptv-org-kz', 'https://iptv-org.github.io/iptv/countries/kz.m3u'),
    ('iptv-org-arab', 'https://iptv-org.github.io/iptv/regions/arab.m3u'),
    ('iptv-org-ara', 'https://iptv-org.github.io/iptv/languages/ara.m3u'),
    ('iptv-org-iraq', 'https://iptv-org.github.io/iptv/countries/iq.m3u'),
    ('iptv-org-egypt', 'https://iptv-org.github.io/iptv/countries/eg.m3u'),
    ('iptv-org-news', 'https://iptv-org.github.io/iptv/categories/news.m3u'),
    ('iptv-org-general', 'https://iptv-org.github.io/iptv/categories/general.m3u'),
    ('iptv-org-education', 'https://iptv-org.github.io/iptv/categories/education.m3u'),
    ('iptv-org-entertainment', 'https://iptv-org.github.io/iptv/categories/entertainment.m3u'),
    ('iptv-org-sports', 'https://iptv-org.github.io/iptv/categories/sports.m3u'),
    ('iptv-org-documentary', 'https://iptv-org.github.io/iptv/categories/documentary.m3u'),
    ('iptv-org-music', 'https://iptv-org.github.io/iptv/categories/music.m3u'),
    ('iptv-org-kids', 'https://iptv-org.github.io/iptv/categories/animation.m3u'),
    ('iptv-org-english', 'https://iptv-org.github.io/iptv/languages/eng.m3u'),
]


def forbidden(url):
    try:
        p = urlparse((url or '').strip())
        h = (p.hostname or '').rstrip('.').lower()
        return p.scheme not in ('http', 'https') or not h or any(h == d or h.endswith('.'+d) for d in BLOCKED)
    except ValueError:
        return True


def suitable(meta):
    return not re.search(r'(?i)(?:\bxxx\b|\badult\b|\bporn\b|18\+|для взрослых)', meta)


def name_key(name):
    name = re.sub(r'(?i)\b(?:hd|fhd|uhd|4k|1080p|720p|sd|live|tv)\b', ' ', name)
    name = re.sub(r'[^\w]+', ' ', name.casefold(), flags=re.UNICODE)
    return ' '.join(name.split())


def parse_m3u(text):
    rows, current = [], None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('#EXTINF:'):
            if current and current['url']:
                rows.append(current)
            current = {'meta': line.rstrip(), 'name': line.rsplit(',', 1)[-1].strip(), 'extras': [], 'url': ''}
        elif current and s:
            if s.startswith('#'):
                current['extras'].append(line.rstrip())
            elif not current['url']:
                current['url'] = s
    if current and current['url']:
        rows.append(current)
    return rows


def checked_response(session, url, *, read_limit=131072):
    if forbidden(url):
        raise ValueError('blocked_url')
    with session.get(url, headers=UA, timeout=(TIMEOUT, TIMEOUT), stream=True,
                     allow_redirects=True) as response:
        if response.status_code not in (200, 206):
            raise ValueError('http_' + str(response.status_code))
        if any(forbidden(x.url) for x in response.history) or forbidden(response.url):
            raise ValueError('blocked_redirect')
        body = bytearray()
        for piece in response.iter_content(16384):
            body.extend(piece)
            if len(body) >= read_limit:
                break
        return response.url, response.headers.get('Content-Type', '').lower(), bytes(body)


def fetch_donor(source):
    label, url = source
    try:
        with requests.Session() as s:
            final, _, body = checked_response(s, url, read_limit=6_000_000)
        rows = [dict(row, donor=label) for row in parse_m3u(body.decode('utf-8-sig', 'replace'))
                if suitable(row['meta']) and not forbidden(row['url'])]
        LOG.info('%s: %d candidates', label, len(rows))
        return rows
    except Exception as exc:
        LOG.warning('%s unavailable: %s', label, exc)
        return []


def playlist_urls(text, base):
    return [urljoin(base, line.strip()) for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def looks_like_media(blob):
    if not blob or b'<html' in blob[:1024].lower() or b'<!doctype html' in blob[:1024].lower():
        return False
    return (blob[0] == 0x47 or (len(blob) > 192 and blob[192] == 0x47)
            or b'ftyp' in blob[:48] or blob.startswith(b'\x1a\x45\xdf\xa3')
            or blob.startswith(b'\xff\xf1') or blob.startswith(b'\xff\xf9')
            or blob.startswith(b'\x00\x00\x01\xba'))


def ffprobe_video(url):
    if not DEEP or not shutil.which('ffprobe'):
        return None
    try:
        proc = subprocess.run(['ffprobe', '-v', 'error', '-rw_timeout', '7000000',
                               '-analyzeduration', '3000000', '-probesize', '1000000',
                               '-show_entries', 'stream=codec_type', '-of', 'json', url],
                              timeout=13, capture_output=True, text=True, check=False)
        streams = json.loads(proc.stdout or '{}').get('streams', [])
        return any(s.get('codec_type') == 'video' for s in streams)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None


def validate(url):
    if forbidden(url):
        return False, 'blocked_domain'
    try:
        with requests.Session() as s:
            final, ct, body = checked_response(s, url)
            if not body or b'<html' in body[:1024].lower():
                return False, 'empty_or_webpage'
            # Handle HLS master and media playlists, not only URLs ending in .m3u8.
            is_hls = b'#EXTM3U' in body[:256] and (b'#EXT-X-' in body)
            if is_hls or 'mpegurl' in ct:
                if not is_hls:
                    return False, 'invalid_hls'
                text = body.decode('utf-8-sig', 'replace')
                for depth in range(3):
                    urls = playlist_urls(text, final)
                    if not urls:
                        return False, 'no_hls_segments'
                    if '#EXT-X-STREAM-INF' in text or '#EXT-X-MEDIA-SEQUENCE' not in text and '#EXT-X-MAP' not in text and '#EXTINF:' not in text:
                        final, _, data = checked_response(s, urls[0])
                        text = data.decode('utf-8-sig', 'replace')
                        if '#EXTM3U' not in text:
                            return False, 'invalid_hls_variant'
                        continue
                    # Only media playlists with media segments qualify.
                    if '#EXTINF:' not in text and '#EXT-X-MAP' not in text:
                        return False, 'no_media_segments'
                    seg = urls[0]
                    _, seg_ct, chunk = checked_response(s, seg, read_limit=65536)
                    if 'text/html' in seg_ct or len(chunk) < 1024 or not looks_like_media(chunk):
                        return False, 'not_video_segment'
                    # Segment content is evidence of a playable stream; not proof of a real station.
                    return True, 'hls_media_confirmed'
                return False, 'hls_nesting_limit'
            if b'<MPD' in body[:4000] or 'dash+xml' in ct:
                return False, 'dash_not_verified'
            if looks_like_media(body):
                probe = ffprobe_video(url)
                if probe is False:
                    return False, 'no_video_track'
                return True, 'media_bytes_confirmed' if probe is None else 'video_track_confirmed'
            return False, 'unrecognized_payload'
    except (requests.RequestException, ValueError, UnicodeError) as exc:
        return False, str(exc)[:100]


def read_cache():
    try:
        data = json.loads(CACHE.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, str) and not forbidden(v)}
    except (OSError, ValueError):
        pass
    return {}


def main():
    if not SOURCE.is_file():
        LOG.error('Missing %s', SOURCE)
        return 1
    original = [x for x in parse_m3u(SOURCE.read_text(encoding='utf-8-sig')) if suitable(x['meta'])]
    cached = read_cache()
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        donor_rows = [item for chunk in pool.map(fetch_donor, DONORS) for item in chunk]
    index = defaultdict(list)
    for row in donor_rows:
        k = name_key(row['name'])
        if k:
            index[k].append(row)
    # Sort candidates: original, last successful, then public-donor alternatives.
    planned = []
    all_urls = set()
    for row in original:
        k = name_key(row['name'])
        candidates = [('original', row['url']), ('cache', cached.get(k, ''))]
        candidates += [(d['donor'], d['url']) for d in index.get(k, [])]
        seen = set()
        unique = []
        for label, url in candidates:
            if url and url not in seen and not forbidden(url):
                unique.append((label, url))
                seen.add(url)
            if len(unique) >= MAX_CANDIDATES:
                break
        planned.append((row, k, unique))
        all_urls.update(url for _, url in unique)
    LOG.info('Source channels: %d; donors: %d; candidate URLs: %d',len(original), len(donor_rows), len(all_urls))
    checks = {}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(validate, url): url for url in all_urls}
        for future in cf.as_completed(futures):
            url = futures[future]
            try:
                checks[url] = future.result()
            except Exception as exc:
                checks[url] = (False, type(exc).__name__)
    output, new_cache = [], {}
    stats = Counter()
    used_urls = set()
    failures = []
    for row, k, candidates in planned:
        if not k:
            stats['empty_name'] += 1
            continue
        chosen = next(((label, url) for label, url in candidates
                       if checks.get(url, (False,))[0] and url not in used_urls), None)
        if not chosen:
            stats['unverified'] += 1
            failures.append(row['name'])
            continue
        label, url = chosen
        used_urls.add(url)
        new_cache[k] = url
        output.append((row, url))
        stats['from_' + label] += 1
    if not output:
        LOG.error('No channels verified; retaining existing output')
        return 2
    timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    lines = ['#EXTM3U', '# Updated: ' + timestamp, '# Verified channels: ' + str(len(output))]
    for row, url in output:
        lines += [row['meta'], *row['extras'], url]
    # Atomic replacement prevents truncated playlist if a job is interrupted.
    for path, value in ((OUT, '\n'.join(lines)+'\n'),
                        (CACHE, json.dumps(new_cache, ensure_ascii=False, indent=2)+'\n'),
                        (REPORT, json.dumps({'updated': timestamp, 'source_count': len(original),
                                             'verified_count': len(output), 'stats': dict(stats),
                                             'unverified_names': failures,
                                             'checks': Counter(x[1] for x in checks.values())},
                                            ensure_ascii=False, indent=2)+'\n')):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         delete=False) as handle:
            handle.write(value)
            temp = Path(handle.name)
        temp.replace(path)
    LOG.info('Playlist saved with %s verified channels', len(output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
