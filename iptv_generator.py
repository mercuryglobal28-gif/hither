

import os, sys, json, time, re, socket, logging
import concurrent.futures
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
from collections import defaultdict

import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# Config (env)
# ============================================================
INPUT_PLAYLIST    = os.environ.get('INPUT_PLAYLIST',  'source_playlist.m3u')
OUTPUT_PLAYLIST   = os.environ.get('OUTPUT_PLAYLIST', 'playlist.m3u')
CACHE_FILE        = os.environ.get('CACHE_FILE',      'working_links.json')
REPORT_FILE       = os.environ.get('REPORT_FILE',     'update_report.txt')
MAX_WORKERS       = int(os.environ.get('MAX_WORKERS', '40'))
TIMEOUT_HTTP      = int(os.environ.get('TIMEOUT_HTTP', '6'))
TIMEOUT_MANIFEST  = int(os.environ.get('TIMEOUT_MANIFEST', '8'))
TIMEOUT_SEGMENT   = int(os.environ.get('TIMEOUT_SEGMENT', '8'))
MIN_SEGMENT_BYTES = int(os.environ.get('MIN_SEGMENT_BYTES', '2048'))
REPLACE_DEAD      = os.environ.get('REPLACE_DEAD', 'true').lower() == 'true'

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36'),
    'Accept': '*/*',
}

DONOR_SOURCES = [
    ("IPTVru",      "https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTVru.m3u", 1),
    ("IPTVstable",  "https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTVstable.m3u8", 1),
    ("IPTVxxx",     "https://raw.githubusercontent.com/smolnp/IPTVru/gh-pages/IPTV%D1%85%D1%85%D1%85.m3u", 1),
    ("iptv-org RU", "https://iptv-org.github.io/iptv/countries/ru.m3u", 2),
    ("Zabava",      "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-full.m3u", 2),
    ("Spirt007",    "https://raw.githubusercontent.com/Spirt007/Tvru/refs/heads/Master/Rus.m3u", 3),
    ("LoganetX",    "https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetXAll.m3u", 4),
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("iptv")


# ============================================================
# Name normalization (preserve HD/SD/4K)
# ============================================================
def normalize_name(name: str) -> str:
    if not name:
        return ""
    n = name.lower().strip()
    n = re.sub(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF]+", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


# ============================================================
# StreamValidator — الفحص الذكي متعدد المراحل
# ============================================================
class StreamValidator:

    @staticmethod
    def check(url: str) -> tuple:
        """Return (is_alive, reason, details)."""
        details = {'url': url[:110]}

        if not url:
            return False, "empty_url", details

        p = urlparse(url)
        if not p.hostname:
            return False, "bad_url", details

        try:
            socket.gethostbyname(p.hostname)
        except socket.gaierror:
            return False, "dns_fail", details

        # ---------- Stage 1: HTTP ----------
        try:
            s = requests.Session()
            s.headers.update(HEADERS)
            try:
                r = s.head(url, timeout=TIMEOUT_HTTP, allow_redirects=True, verify=False)
                code = r.status_code
                details['http'] = code
                if code not in (200, 206, 301, 302, 304, 307, 308):
                    r = s.get(url, timeout=TIMEOUT_HTTP, allow_redirects=True,
                              verify=False, stream=True)
                    code = r.status_code
                    details['http'] = code
                    try: r.close()
                    except Exception: pass
                    if code not in (200, 206):
                        return False, f"http_{code}", details
            finally:
                try: s.close()
                except Exception: pass
        except requests.Timeout:
            return False, "http_timeout", details
        except requests.ConnectionError:
            return False, "conn_error", details
        except Exception as e:
            return False, f"http_err_{type(e).__name__}", details

        # ---------- Stage 2 & 3 ----------
        low = url.lower()
        if '.m3u8' in low or 'hls' in low or '/live/' in low or '/stream/' in low:
            return StreamValidator._check_hls(url, details)
        if '.mpd' in low or 'dash' in low:
            return StreamValidator._check_dash(url, details)
        # Unknown format - HTTP worked
        return True, "http_ok", details

    # ---------- HLS ----------
    @staticmethod
    def _check_hls(url, details):
        try:
            r = requests.get(url, timeout=TIMEOUT_MANIFEST, verify=False,
                             headers=HEADERS, stream=True)
            if r.status_code != 200:
                try: r.close()
                except Exception: pass
                return False, f"hls_http_{r.status_code}", details

            data = b""
            for chunk in r.iter_content(4096):
                data += chunk
                if len(data) >= 16384:
                    break
            try: r.close()
            except Exception: pass

            text = data.decode('utf-8', errors='ignore')

            # Error page detection
            if '#EXTM3U' not in text:
                low = text.lower()
                if any(x in low for x in ['<html', '<!doctype', 'subscription',
                                          'expired', 'blocked', 'unauthorized',
                                          'not found', 'access denied']):
                    details['body_snippet'] = text[:120].replace('\n', ' ')
                    return False, "expired_or_error_page", details
                return False, "not_hls", details

            # Master playlist
            if '#EXT-X-STREAM-INF' in text:
                variant_url = StreamValidator._extract_first_url(text, url)
                if not variant_url:
                    return False, "master_no_variant", details
                v_det = {'url': variant_url[:110], 'variant_of': url[:80]}
                return StreamValidator._check_hls_variant(variant_url, v_det)

            return StreamValidator._evaluate_media_playlist(text, url, details)

        except requests.Timeout:
            return False, "hls_timeout", details
        except Exception as e:
            return False, f"hls_err_{type(e).__name__}", details

    @staticmethod
    def _check_hls_variant(url, details):
        try:
            r = requests.get(url, timeout=TIMEOUT_MANIFEST, verify=False, headers=HEADERS)
            if r.status_code != 200:
                return False, f"variant_http_{r.status_code}", details
            text = r.text[:16384]
            return StreamValidator._evaluate_media_playlist(text, url, details)
        except Exception as e:
            return False, f"variant_err_{type(e).__name__}", details

    @staticmethod
    def _evaluate_media_playlist(text, base_url, details):
        seg_count = text.count('#EXTINF:')
        details['segments'] = seg_count

        if seg_count == 0:
            return False, "no_segments", details

        is_vod = '#EXT-X-ENDLIST' in text
        if not is_vod and seg_count < 2:
            return False, "stale_live", details

        first_seg = StreamValidator._extract_first_segment(text, base_url)
        if first_seg:
            ok, size = StreamValidator._check_segment_data(first_seg)
            details['seg_ok'] = ok
            details['seg_bytes'] = size
            if not ok:
                return False, "segment_invalid", details
            if size < MIN_SEGMENT_BYTES:
                return False, f"segment_too_small({size}b)", details

        return True, f"ok_{seg_count}seg", details

    @staticmethod
    def _extract_first_url(text, base_url):
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF'):
                for j in range(i + 1, len(lines)):
                    nxt = lines[j].strip()
                    if nxt and not nxt.startswith('#'):
                        return urljoin(base_url, nxt)
        return None

    @staticmethod
    def _extract_first_segment(text, base_url):
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith('#'):
                return urljoin(base_url, s)
        return None

    @staticmethod
    def _check_segment_data(url):
        try:
            r = requests.get(url, timeout=TIMEOUT_SEGMENT, verify=False,
                             headers=HEADERS, stream=True)
            if r.status_code != 200:
                try: r.close()
                except Exception: pass
                return False, 0
            data = b""
            for chunk in r.iter_content(8192):
                data += chunk
                if len(data) >= 32768:
                    break
            try: r.close()
            except Exception: pass
            if not data:
                return False, 0

            head = data[:64]
            if head[0] == 0x47:
                return True, len(data)
            if b'ftyp' in head or b'styp' in head or b'moof' in head or b'mdat' in head:
                return True, len(data)

            low = data[:1000].lower()
            if b'<html' in low or b'<error' in low:
                return False, 0

            return len(data) >= MIN_SEGMENT_BYTES, len(data)
        except Exception:
            return False, 0

    @staticmethod
    def _check_dash(url, details):
        try:
            r = requests.get(url, timeout=TIMEOUT_MANIFEST, verify=False, headers=HEADERS)
            if r.status_code != 200:
                return False, f"dash_http_{r.status_code}", details
            text = r.text[:32768]
            if '<MPD' not in text and '<mpd' not in text:
                if '<html' in text.lower():
                    return False, "dash_html_error", details
                return False, "not_mpd", details
            if '<Period' not in text:
                return False, "dash_no_period", details
            if '<Representation' not in text:
                return False, "dash_no_representation", details
            return True, "dash_ok", details
        except Exception as e:
            return False, f"dash_err_{type(e).__name__}", details


# ============================================================
# Playlist parsing (preserve everything)
# ============================================================
def parse_full_playlist(content: str):
    lines = content.splitlines()
    entries, i, n = [], 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip().startswith('#EXTINF:'):
            i += 1
            continue
        entry = {
            'extinf': line.rstrip(),
            'extras': [],
            'url': None,
            'name': line.rsplit(',', 1)[-1].strip() if ',' in line else '',
        }
        j = i + 1
        while j < n:
            raw = lines[j]
            s = raw.strip()
            if not s:
                j += 1
                continue
            if (s.startswith('#EXTINF:') or s.startswith('#EXTM3U')
                    or s.startswith('#PLAYLIST')):
                break
            if s.startswith('#'):
                entry['extras'].append(raw.rstrip())
                j += 1
                continue
            entry['url'] = s
            j += 1
            break
        entries.append(entry)
        i = j
    return entries


def parse_m3u_quick(content: str, src: str, priority: int):
    out, lines = [], content.splitlines()
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if s.startswith('#EXTINF:') and ',' in s:
            name = s.rsplit(',', 1)[-1].strip()
            j, url = i + 1, None
            while j < n and j < i + 15:
                nxt = lines[j].strip()
                if nxt and not nxt.startswith('#'):
                    url = nxt
                    break
                j += 1
            if url and name:
                out.append({'name': name, 'url': url, 'priority': priority, 'source': src})
            i = j
        else:
            i += 1
    return out


def fetch_donor(url, name, priority):
    try:
        r = requests.get(url, timeout=40, verify=False, headers=HEADERS)
        if r.status_code != 200:
            log.warning(f"Donor {name}: HTTP {r.status_code}")
            return []
        chans = parse_m3u_quick(r.text, name, priority)
        log.info(f"Donor {name}: {len(chans)} channels")
        return chans
    except Exception as e:
        log.error(f"Donor {name}: {e}")
        return []


# ============================================================
# Main
# ============================================================
def main() -> int:
    log.info("=" * 64)
    log.info("IPTV Updater — Smart Multi-Stage Validation")
    log.info("=" * 64)
    t0 = time.time()

    if not os.path.exists(INPUT_PLAYLIST):
        log.error(f"✘ Missing: {INPUT_PLAYLIST}")
        return 1

    with open(INPUT_PLAYLIST, 'r', encoding='utf-8') as f:
        content = f.read()
    entries = parse_full_playlist(content)
    with_url = sum(1 for e in entries if e['url'])
    log.info(f"Master playlist: {len(entries)} entries "
             f"({with_url} with URL / {len(entries)-with_url} without)")

    # Cache
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            cache = json.load(open(CACHE_FILE, encoding='utf-8'))
            log.info(f"Cache: {len(cache)}")
        except Exception as e:
            log.warning(f"Cache: {e}")

    # Donors
    log.info(f"Fetching {len(DONOR_SOURCES)} donors...")
    donors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(fetch_donor, u, nm, pr) for nm, u, pr in DONOR_SOURCES]
        for f in concurrent.futures.as_completed(futs):
            try: donors.extend(f.result())
            except Exception as e: log.error(f"Donor: {e}")
    log.info(f"Total donor channels: {len(donors)}")

    # Index donors by EXACT name (preserve HD/SD) - STRICT matching only
    donor_index = {}
    for d in donors:
        k = normalize_name(d['name'])
        if not k:
            continue
        cur = donor_index.get(k)
        if cur is None or d['priority'] < cur['priority']:
            donor_index[k] = d
    log.info(f"Donor index (exact names): {len(donor_index)}")

    # ============================================================
    # Stage 1: Validate original URLs
    # ============================================================
    log.info(f"Validating {with_url} original URLs (3-stage)...")
    to_check = [(i, e['url']) for i, e in enumerate(entries) if e['url']]
    validation = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(StreamValidator.check, url): i for i, url in to_check}
        done, total = 0, len(futs)
        for f in concurrent.futures.as_completed(futs):
            i = futs[f]
            try:
                validation[i] = f.result()
            except Exception as e:
                validation[i] = (False, f"exc_{type(e).__name__}", {})
            done += 1
            if done % 50 == 0 or done == total:
                alive = sum(1 for v in validation.values() if v[0])
                log.info(f"  {done}/{total}  alive={alive}")

    alive_orig = sum(1 for v in validation.values() if v[0])
    log.info(f"Original URLs alive: {alive_orig}/{with_url}")

    # ============================================================
    # Stage 2: Resolve final URL for each entry
    # ============================================================
    resolved = []
    seen_names = set()
    seen_urls = set()
    stats = defaultdict(int)
    report = []

    for i, e in enumerate(entries):
        name = e['name'].strip()
        if not name:
            continue

        nkey = normalize_name(name)
        if nkey in seen_names:
            stats['dup_name'] += 1
            report.append(f"[DUP-NAME] {name}")
            continue

        final_url = None
        source = "kept"

        # (a) Original URL is alive?
        if e['url'] and validation.get(i, (False,))[0]:
            final_url = e['url']
            stats['kept'] += 1

        # (b) Cache has a working URL?
        if not final_url:
            cached = cache.get(nkey)
            if cached and StreamValidator.check(cached)[0]:
                final_url = cached
                source = "cache"
                stats['from_cache'] += 1

        # (c) Exact-name donor match (STRICT — no fuzzy!)
        if not final_url and REPLACE_DEAD:
            d = donor_index.get(nkey)
            if d:
                ok, reason, _ = StreamValidator.check(d['url'])
                if ok:
                    final_url = d['url']
                    source = f"donor:{d['source']}"
                    stats['from_donor'] += 1
                else:
                    stats['donor_failed'] += 1
                    report.append(f"[DONOR-DEAD] {name} <- {d['source']} ({reason})")

        if not final_url:
            stats['dropped'] += 1
            if e['url']:
                reason = validation.get(i, (False, "unknown"))[1]
                report.append(f"[DROP] {name} ({reason})")
            else:
                report.append(f"[NO-URL] {name}")
            continue

        if final_url in seen_urls:
            stats['dup_url'] += 1
            report.append(f"[DUP-URL] {name}")
            continue

        seen_names.add(nkey)
        seen_urls.add(final_url)
        cache[nkey] = final_url
        resolved.append((e, final_url, source))

        if source != "kept":
            report.append(f"[{source.upper()}] {name}")

    log.info(f"Stats: {dict(stats)}")
    log.info(f"Final: {len(resolved)} channels")

    # ============================================================
    # Write output
    # ============================================================
    with open(OUTPUT_PLAYLIST, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        f.write(f'# Updated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}\n')
        f.write(f'# Channels: {len(resolved)}\n')
        f.write(f'# Kept: {stats["kept"]} | Cache: {stats["from_cache"]} | '
                f'Donor: {stats["from_donor"]} | Dropped: {stats["dropped"]}\n\n')
        for e, url, _ in resolved:
            f.write(e['extinf'] + '\n')
            for ex in e['extras']:
                f.write(ex + '\n')
            f.write(url + '\n')

    log.info(f"✔ Wrote {OUTPUT_PLAYLIST} ({len(resolved)} channels)")

    # Report
    try:
        with open(REPORT_FILE, 'w', encoding='utf-8') as f:
            f.write(f"Report: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"Total: {len(entries)} | Final: {len(resolved)}\n")
            f.write(f"Stats: {dict(stats)}\n")
            f.write("=" * 60 + "\n")
            f.write("\n".join(report[:2000]))
    except Exception as e:
        log.warning(f"Report: {e}")

    # Cache
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)
        log.info(f"✔ Cache: {len(cache)}")
    except Exception as e:
        log.warning(f"Cache: {e}")

    log.info(f"Done in {time.time() - t0:.1f}s")
    return 0


if __name__ == '__main__':
    sys.exit(main())
