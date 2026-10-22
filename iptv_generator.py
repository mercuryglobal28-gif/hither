"""Generate a validated M3U playlist from a source playlist and permitted donors."""
import concurrent.futures
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

INPUT = Path(os.getenv('INPUT_PLAYLIST', 'source_playlist.m3u'))
OUTPUT = Path(os.getenv('OUTPUT_PLAYLIST', 'playlist.m3u'))
CACHE = Path(os.getenv('CACHE_FILE', 'working_links.json'))
REPORT = Path(os.getenv('REPORT_FILE', 'update_report.txt'))
WORKERS = max(1, min(30, int(os.getenv('MAX_WORKERS', '20'))))
TIMEOUT = float(os.getenv('TIMEOUT_HTTP', '8'))
REPLACE_DEAD = os.getenv('REPLACE_DEAD', 'true').lower() == 'true'
MIN_SEGMENT_BYTES = int(os.getenv('MIN_SEGMENT_BYTES', '2048'))
BLOCKED_DOMAINS = ('plist.link', 'ott.watch')
# No explicit/adult catalogue sources. Filter inappropriate entries from all inputs.
DONOR_SOURCES = [
    ('IPTVru', 'https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTVru.m3u', 1),
    ('IPTVstable', 'https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTVstable.m3u8', 1),
    ('iptv-org RU', 'https://iptv-org.github.io/iptv/countries/ru.m3u', 2),
    ('Zabava', 'https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-full.m3u', 2),
    ('Spirt007', 'https://raw.githubusercontent.com/Spirt007/Tvru/refs/heads/Master/Rus.m3u', 3),
    ('LoganetX', 'https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetXAll.m3u', 4),
]
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; PlaylistValidator/1.0)', 'Accept': '*/*'}
LOG = logging.getLogger('iptv')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')


def blocked(url):
    """Check actual hostname only; blocks subdomains, not lookalike suffixes."""
    try:
        p = urlparse((url or '').strip())
        host = (p.hostname or '').rstrip('.').lower()
        return p.scheme.lower() not in ('http', 'https') or not host or any(
            host == domain or host.endswith('.' + domain) for domain in BLOCKED_DOMAINS
        )
    except ValueError:
        return True


def safe_channel(extinf):
    # Skip explicitly adult-labeled channels/groups, regardless of original/donor.
    return not re.search(r'(?i)(?:\bxxx\b|\badult\b|\bporn\b|18\+|для взрослых)', extinf)


def key(name):
    return re.sub(r'\s+', ' ', name.casefold()).strip()


def parse_playlist(text):
    """Preserve original EXTINF metadata and per-channel option lines."""
    rows = []
    current = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('#EXTINF:'):
            if current is not None:
                rows.append(current)
            current = {'extinf': line.rstrip(), 'name': line.rsplit(',', 1)[-1].strip(),
                       'extras': [], 'url': None}
        elif current is not None and s:
            if s.startswith('#'):
                current['extras'].append(line.rstrip())
            elif current['url'] is None:
                current['url'] = s
    if current is not None:
        rows.append(current)
    return rows


def download_text(url, timeout=35):
    if blocked(url):
        raise ValueError('blocked_source')
    with requests.Session() as s:
        s.headers.update(HEADERS)
        with s.get(url, timeout=timeout, allow_redirects=True) as r:
            r.raise_for_status()
            if any(blocked(step.url) for step in r.history) or blocked(r.url):
                raise ValueError('blocked_redirect')
            return r.text


def check_stream(url):
    if blocked(url):
        return False, 'blocked_domain_or_url'
    try:
        with requests.Session() as s:
            s.headers.update(HEADERS)
            # GET handles servers that reject HEAD. Streaming limits downloaded data.
            with s.get(url, timeout=(TIMEOUT, TIMEOUT), stream=True,
                       allow_redirects=True) as r:
                if any(blocked(step.url) for step in r.history) or blocked(r.url):
                    return False, 'blocked_redirect'
                if r.status_code not in (200, 206):
                    return False, f'http_{r.status_code}'
                ct = r.headers.get('Content-Type', '').lower()
                sample = b''
                for chunk in r.iter_content(8192):
                    sample += chunk
                    if len(sample) >= 32768:
                        break
                if not sample:
                    return False, 'empty_response'
                low = sample[:1000].lower()
                if b'<html' in low or b'<!doctype html' in low:
                    return False, 'html_instead_of_stream'
                is_hls = '.m3u8' in urlparse(r.url).path.lower() or 'mpegurl' in ct
                if is_hls:
                    body = sample.decode('utf-8', 'replace')
                    if '#EXTM3U' not in body:
                        return False, 'invalid_hls'
                    if '#EXT-X-STREAM-INF' in body:
                        for line in body.splitlines():
                            line = line.strip()
                            if line and not line.startswith('#'):
                                variant = urljoin(r.url, line)
                                if blocked(variant):
                                    return False, 'blocked_hls_variant'
                                return check_hls_child(variant)
                        return False, 'missing_hls_variant'
                    return inspect_segments(body, r.url)
                if '.mpd' in urlparse(r.url).path.lower() or 'dash+xml' in ct:
                    return ('<MPD' in sample.decode('utf-8', 'replace'), 'dash_manifest')
                return True, 'http_ok'
    except requests.RequestException as e:
        return False, type(e).__name__
    except Exception as e:
        return False, type(e).__name__


def check_hls_child(url):
    if blocked(url):
        return False, 'blocked_hls_variant'
    try:
        with requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True) as r:
            if blocked(r.url) or any(blocked(step.url) for step in r.history):
                return False, 'blocked_redirect'
            if r.status_code != 200:
                return False, f'variant_http_{r.status_code}'
            data = b''
            for chunk in r.iter_content(8192):
                data += chunk
                if len(data) >= 32768:
                    break
            text = data.decode('utf-8', 'replace')
            if '#EXTM3U' not in text:
                return False, 'invalid_variant'
            return inspect_segments(text, r.url)
    except requests.RequestException as e:
        return False, type(e).__name__


def inspect_segments(text, base):
    urls = [x.strip() for x in text.splitlines() if x.strip() and not x.startswith('#')]
    if not urls:
        return False, 'no_segments'
    if '#EXT-X-ENDLIST' not in text and len(urls) < 2:
        return False, 'stale_live'
    seg = urljoin(base, urls[0])
    if blocked(seg):
        return False, 'blocked_segment'
    try:
        with requests.get(seg, headers=HEADERS, stream=True, timeout=TIMEOUT) as r:
            if blocked(r.url) or any(blocked(step.url) for step in r.history):
                return False, 'blocked_redirect'
            if r.status_code not in (200, 206):
                return False, f'segment_http_{r.status_code}'
            data = b''
            for chunk in r.iter_content(8192):
                data += chunk
                if len(data) >= 32768:
                    break
            if len(data) < MIN_SEGMENT_BYTES:
                return False, 'segment_too_short'
            if b'<html' in data[:1000].lower():
                return False, 'html_segment'
            return True, 'hls_segment_ok'
    except requests.RequestException as e:
        return False, type(e).__name__


def get_donor(source):
    name, url, priority = source
    try:
        rows = parse_playlist(download_text(url))
        result = [{'name': e['name'], 'url': e['url'], 'priority': priority, 'source': name}
                  for e in rows if e['url'] and e['name'] and safe_channel(e['extinf'])
                  and not blocked(e['url'])]
        LOG.info('Donor %s: %s eligible channels', name, len(result))
        return result
    except Exception as e:
        LOG.warning('Donor %s failed: %s', name, e)
        return []


def load_cache():
    if not CACHE.exists():
        return {}
    try:
        raw = json.loads(CACHE.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items()
                if isinstance(v, str) and not blocked(v)}
    except (OSError, ValueError) as e:
        LOG.warning('Ignoring unreadable cache: %s', e)
        return {}


def main():
    if not INPUT.is_file():
        LOG.error('Missing input playlist: %s', INPUT)
        return 1
    entries = parse_playlist(INPUT.read_text(encoding='utf-8-sig'))
    cache = load_cache()
    donors = []
    if REPLACE_DEAD:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            for result in pool.map(get_donor, DONOR_SOURCES):
                donors.extend(result)
    donor_index = defaultdict(list)
    for d in donors:
        donor_index[key(d['name'])].append(d)
    for candidates in donor_index.values():
        candidates.sort(key=lambda item: item['priority'])

    # Only permitted candidate links are probed. Save result by URL to avoid duplicates.
    all_urls = set()
    for e in entries:
        if not safe_channel(e['extinf']):
            continue
        candidates = [e['url'], cache.get(key(e['name']))]
        candidates += [d['url'] for d in donor_index.get(key(e['name']), [])] if REPLACE_DEAD else []
        all_urls.update(u for u in candidates if u and not blocked(u))
    LOG.info('Validating %d permitted unique URLs', len(all_urls))
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        tasks = {pool.submit(check_stream, u): u for u in all_urls}
        for task in concurrent.futures.as_completed(tasks):
            url = tasks[task]
            try:
                results[url] = task.result()
            except Exception as e:
                results[url] = (False, type(e).__name__)

    resolved, new_cache, seen_names, seen_urls = [], {}, set(), set()
    stats = defaultdict(int)
    notes = []
    for e in entries:
        name, name_key = e['name'], key(e['name'])
        if not name_key or not safe_channel(e['extinf']):
            stats['inappropriate_or_empty'] += 1
            continue
        if name_key in seen_names:
            stats['duplicate_name'] += 1
            continue
        candidates = [('original', e['url']), ('cache', cache.get(name_key))]
        if REPLACE_DEAD:
            candidates += [('donor:' + d['source'], d['url'])
                           for d in donor_index.get(name_key, [])]
        chosen = None
        for source, url in candidates:
            if not url:
                continue
            if blocked(url):
                stats['blocked_candidate'] += 1
                continue
            if url in seen_urls:
                continue
            if results.get(url, (False,))[0]:
                chosen = (url, source)
                break
        if not chosen:
            stats['dropped'] += 1
            notes.append(f'[DROP] {name}')
            continue
        url, source = chosen
        if blocked(url):
            raise AssertionError('Blocked URL somehow selected')
        seen_names.add(name_key)
        seen_urls.add(url)
        new_cache[name_key] = url
        resolved.append((e, url))
        stats[source] += 1

    # Do not wipe a previously good playlist during a temporary total outage.
    if entries and not resolved:
        LOG.error('Zero working permitted channels; outputs left unchanged')
        return 2
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    lines = ['#EXTM3U', f'# Updated: {now}', f'# Channels: {len(resolved)}', '']
    for entry, url in resolved:
        lines.extend([entry['extinf'], *entry['extras'], url])
    OUTPUT.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    CACHE.write_text(json.dumps(new_cache, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
                     encoding='utf-8')
    REPORT.write_text('Updated: ' + now + '\n' + 'Source entries: ' + str(len(entries)) +
                      '\nOutput channels: ' + str(len(resolved)) + '\nStatistics: ' +
                      json.dumps(dict(stats), ensure_ascii=False, sort_keys=True) + '\n\n' +
                      '\n'.join(notes[:2000]) + '\n', encoding='utf-8')
    LOG.info('Wrote %d channels; stats=%s', len(resolved), dict(stats))
    return 0


if __name__ == '__main__':
    sys.exit(main())
