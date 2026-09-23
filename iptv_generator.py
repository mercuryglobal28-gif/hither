#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV Playlist Updater — يحافظ على قائمتك الأصلية ويحدّث فقط الروابط
- يقرأ source_playlist.m3u (قائمتك)
- يفحص كل رابط
- يستبدل الروابط المعطوبة من مصادر مانحة
- يحذف التكرار (اسم + رابط)
- يخرج playlist.m3u بنفس البنية تمامًا
"""
import os, sys, json, time, re, socket, logging
import concurrent.futures
from datetime import datetime, timezone
from urllib.parse import urlparse
from difflib import SequenceMatcher

import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("iptv")

# ============ Config ============
INPUT_PLAYLIST  = os.environ.get('INPUT_PLAYLIST',  'source_playlist.m3u')
OUTPUT_PLAYLIST = os.environ.get('OUTPUT_PLAYLIST', 'playlist.m3u')
CACHE_FILE      = os.environ.get('CACHE_FILE',      'working_links.json')
REPORT_FILE     = os.environ.get('REPORT_FILE',     'update_report.txt')
MAX_WORKERS     = int(os.environ.get('MAX_WORKERS', '50'))
CHECK_TIMEOUT   = int(os.environ.get('CHECK_TIMEOUT', '6'))
FUZZY_THRESHOLD = float(os.environ.get('FUZZY_THRESHOLD', '0.87'))

HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36'),
    'Accept': '*/*',
}

# ============ Donor sources ============
DONOR_SOURCES = [
    ("IPTVru",       "https://smolnp.github.io/IPTVru/IPTVru.m3u", 1),
    ("IPTVstable",   "https://smolnp.github.io/IPTVru/IPTVstable.m3u8", 1),
    ("iptv-org RU",  "https://iptv-org.github.io/iptv/countries/ru.m3u", 1),
    ("Zabava",       "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-full.m3u", 2),
    ("Spirt007",     "https://raw.githubusercontent.com/Spirt007/Tvru/refs/heads/Master/Rus.m3u", 3),
    ("SlyNet",       "https://slynet-iptv2025.do.am/FreeBestTV.m3u8", 3),
    ("iptv-org",     "https://iptv-org.github.io/iptv/index.m3u", 4),
    ("artem-art998", "https://raw.githubusercontent.com/artem-art998/IPTVru/refs/heads/main/iptv126.m3u", 4),
    ("empty180",     "https://raw.githubusercontent.com/empty180/IPTVrus/refs/heads/main/pishma.m3u", 4),
    ("naggdd",       "https://raw.githubusercontent.com/naggdd/iptv/refs/heads/main/ru.m3u", 4),
    ("LoganetX",     "https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetXAll.m3u", 4),
    ("LoganetX-SB",  "https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetXStrawberry.m3u", 4),
    ("Zabava-EF",    "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-ef.m3u", 4),
    ("Zabava-Reg",   "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-reg.m3u", 4),
]

# ============ Name normalization ============
def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r'\(\s*(?:720|1080|480|360|2160|4K|8K|UHD|FHD|HEVC|H264|H265)\s*[pPi]?\s*\)',
                  '', name, flags=re.I)
    name = re.sub(r"[\U0001F300-\U0001FAFF\U00002600-\U000027BF]+", '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return re.sub(r'^[-–—\s]+|[-–—\s]+$', '', name)

def make_key(name: str) -> str:
    """Key for matching donors: strip HD/FHD/SD suffix, lowercase."""
    n = normalize_name(name).lower()
    n = re.sub(r'\s+(hd|fhd|uhd|sd|4k|8k)$', '', n)
    return n.strip()

def exact_key(name: str) -> str:
    """Key for internal dedup: exact normalized name."""
    return normalize_name(name).lower()

# ============ Playlist parsing (preserve everything) ============
def parse_full_playlist(content: str):
    """Returns (entries). Each entry preserves the exact EXTINF line + extras."""
    lines = content.splitlines()
    entries, i = [], 0
    while i < len(lines):
        line = lines[i]
        if not line.strip().startswith('#EXTINF:'):
            i += 1
            continue
        entry = {
            'extinf': line,
            'extras': [],
            'url': None,
            'name': line.rsplit(',', 1)[-1].strip() if ',' in line else '',
        }
        j = i + 1
        while j < len(lines):
            nxt = lines[j].strip()
            if not nxt:
                j += 1
                continue
            if nxt.startswith('#EXTINF:') or nxt.startswith('#EXTM3U') or nxt.startswith('#PLAYLIST'):
                break
            if nxt.startswith('#'):
                entry['extras'].append(lines[j])
                j += 1
                continue
            entry['url'] = nxt
            j += 1
            break
        entries.append(entry)
        i = j
    return entries

# ============ Donor parsing ============
def parse_m3u_quick(content: str, src: str, priority: int):
    out, lines = [], content.splitlines()
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if s.startswith('#EXTINF:') and ',' in s:
            name = s.rsplit(',', 1)[-1].strip()
            q = 0
            up = s.upper()
            if   '4K'   in up or '2160' in up: q = 4
            elif '1080' in up or 'FHD'  in up: q = 3
            elif '720'  in up or ' HD'  in up: q = 2
            elif '480'  in up or ' SD'  in up: q = 1
            j, url = i + 1, None
            while j < n and j < i + 12:
                nxt = lines[j].strip()
                if nxt and not nxt.startswith('#'):
                    url = nxt
                    break
                j += 1
            if url and name:
                out.append({'name': name, 'url': url, 'priority': priority,
                            'quality': q, 'source': src})
            i = j
        else:
            i += 1
    return out

def fetch_donor(url, name, priority):
    try:
        r = requests.get(url, timeout=30, verify=False, headers=HEADERS)
        if r.status_code != 200:
            log.warning(f"Donor {name}: HTTP {r.status_code}")
            return []
        chans = parse_m3u_quick(r.text, name, priority)
        log.info(f"Donor {name}: {len(chans)} channels")
        return chans
    except Exception as e:
        log.error(f"Donor {name}: {e}")
        return []

# ============ Stream check ============
def check_stream(url: str, timeout: int):
    if not url:
        return False, 0.0
    try:
        p = urlparse(url)
        if not p.hostname:
            return False, 0.0
        try:
            socket.gethostbyname(p.hostname)
        except socket.gaierror:
            return False, 0.0

        t0 = time.time()
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            r = s.head(url, timeout=timeout, allow_redirects=True, verify=False)
            dt = time.time() - t0
            if r.status_code in (200, 206, 301, 302, 304, 307, 308):
                return True, dt
            r = s.get(url, timeout=timeout, allow_redirects=True, verify=False,
                      stream=True, headers={**HEADERS, 'Range': 'bytes=0-2048'})
            dt = time.time() - t0
            ok = r.status_code in (200, 206)
            try: r.close()
            except Exception: pass
            return ok, dt
        except (requests.Timeout, requests.ConnectionError):
            return False, 0.0
        except Exception:
            return False, 0.0
        finally:
            try: s.close()
            except Exception: pass
    except Exception:
        return False, 0.0

# ============ Donor index ============
def build_donor_index(donors):
    idx = {}
    for d in donors:
        k = make_key(d['name'])
        if not k or len(k) < 2:
            continue
        cur = idx.get(k)
        if cur is None or (d['priority'], -d['quality']) < (cur['priority'], -cur['quality']):
            idx[k] = d
    log.info(f"Donor index: {len(idx)} unique base names")
    return idx

def find_donor(name, idx, alt_idx):
    k = make_key(name)
    if not k:
        return None
    if k in idx:
        return idx[k]
    nk = normalize_name(name).lower()
    if nk in alt_idx:
        return alt_idx[nk]
    if len(k) < 4:
        return None
    best, best_ratio = None, 0.0
    for cand_key, cand in idx.items():
        if abs(len(cand_key) - len(k)) > 3:
            continue
        r = SequenceMatcher(None, k, cand_key).ratio()
        if r > best_ratio:
            best_ratio, best = r, cand
    return best if best_ratio >= FUZZY_THRESHOLD else None

# ============ Main ============
def main() -> int:
    log.info("=" * 60)
    log.info("IPTV Playlist Updater — تحديث قائمتك الأصلية")
    log.info("=" * 60)
    t_start = time.time()

    if not os.path.exists(INPUT_PLAYLIST):
        log.error(f"✘ القائمة الأصلية غير موجودة: {INPUT_PLAYLIST}")
        log.error("  احفظ ملف IPTVххх باسم source_playlist.m3u في جذر المستودع.")
        return 1

    with open(INPUT_PLAYLIST, 'r', encoding='utf-8') as f:
        content = f.read()

    entries = parse_full_playlist(content)
    with_url = sum(1 for e in entries if e['url'])
    log.info(f"قائمتك: {len(entries)} مدخل — مع رابط: {with_url} / بدون: {len(entries)-with_url}")

    # Cache
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            cache = json.load(open(CACHE_FILE, encoding='utf-8'))
            log.info(f"Cache: {len(cache)}")
        except Exception as e:
            log.warning(f"Cache load: {e}")

    # Fetch donors
    log.info(f"جلب {len(DONOR_SOURCES)} مصدر مانح...")
    donors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_donor, u, nm, pr) for nm, u, pr in DONOR_SOURCES]
        for f in concurrent.futures.as_completed(futs):
            try: donors.extend(f.result())
            except Exception as e: log.error(f"Donor: {e}")
    log.info(f"إجمالي قنوات المانحين: {len(donors)}")

    donor_index = build_donor_index(donors)
    alt_index = {}
    for d in donors:
        k = normalize_name(d['name']).lower()
        if k and k not in alt_index:
            alt_index[k] = d

    # ============ Step 1: فحص روابط قائمتك ============
    log.info(f"فحص {with_url} رابط أصلي...")
    check_results = {}
    to_check = [(i, e['url']) for i, e in enumerate(entries) if e['url']]
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(check_stream, url, CHECK_TIMEOUT): i for i, url in to_check}
        done, total = 0, len(futs)
        for f in concurrent.futures.as_completed(futs):
            i = futs[f]
            try: check_results[i] = f.result()
            except Exception: check_results[i] = (False, 0.0)
            done += 1
            if done % 100 == 0 or done == total:
                log.info(f"  {done}/{total}")

    working_orig = sum(1 for v in check_results.values() if v[0])
    log.info(f"روابط أصلية تعمل: {working_orig}/{len(check_results)}")

    # ============ Step 2: حلّ رابط نهائي لكل قناة ============
    resolved = []
    seen_names = set()   # exact normalized name
    seen_urls  = set()
    stats = {'kept': 0, 'replaced': 0, 'cached': 0, 'dropped': 0, 'dup_name': 0, 'dup_url': 0}
    report_lines = []

    for i, e in enumerate(entries):
        name = e['name'].strip()
        if not name:
            stats['dropped'] += 1
            continue

        nkey = exact_key(name)
        if nkey in seen_names:
            stats['dup_name'] += 1
            report_lines.append(f"[DUP-NAME] {name}")
            continue

        final_url, replaced = None, False

        # (a) الرابط الأصلي يعمل → احتفظ به
        if e['url'] and check_results.get(i, (False, 0))[0]:
            final_url = e['url']
            stats['kept'] += 1

        # (b) من الـ cache
        if not final_url:
            cached = cache.get(make_key(name))
            if cached and check_stream(cached, CHECK_TIMEOUT)[0]:
                final_url = cached
                replaced = True
                stats['cached'] += 1

        # (c) من المانحين
        if not final_url:
            donor = find_donor(name, donor_index, alt_index)
            if donor and check_stream(donor['url'], CHECK_TIMEOUT)[0]:
                final_url = donor['url']
                replaced = True
                stats['replaced'] += 1

        if not final_url:
            stats['dropped'] += 1
            report_lines.append(f"[DROP] {name}  (لا يوجد رابط شغّال)")
            continue

        # URL dedup
        if final_url in seen_urls:
            stats['dup_url'] += 1
            report_lines.append(f"[DUP-URL] {name}")
            continue

        seen_names.add(nkey)
        seen_urls.add(final_url)
        cache[make_key(name)] = final_url
        resolved.append((e, final_url, replaced))

        if replaced:
            report_lines.append(f"[REPLACED] {name}")

    log.info(f"إحصاءات: {stats}")
    log.info(f"النتيجة النهائية: {len(resolved)} قناة")

    # ============ Step 3: كتابة الخرج بنفس البنية ============
    with open(OUTPUT_PLAYLIST, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        f.write(f'# Updated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}\n')
        f.write(f'# Source:  {os.path.basename(INPUT_PLAYLIST)}\n')
        f.write(f'# Channels: {len(resolved)}\n')
        f.write(f'# Kept URLs: {stats["kept"]} | Replaced: {stats["replaced"]} | '
                f'From cache: {stats["cached"]} | Dropped: {stats["dropped"]}\n')
        f.write('\n')
        for e, url, replaced in resolved:
            f.write(e['extinf'] + '\n')
            if not replaced:  # احتفظ بـ #EXTVLCOPT فقط إن لم نغيّر الرابط
                for ex in e['extras']:
                    f.write(ex + '\n')
            f.write(url + '\n')

    log.info(f"✔ كُتب {OUTPUT_PLAYLIST} ({len(resolved)} قناة)")

    # Report
    try:
        with open(REPORT_FILE, 'w', encoding='utf-8') as f:
            f.write(f"تقرير تحديث: {datetime.now(timezone.utc).isoformat()}\n")
            f.write(f"المدخلات: {len(entries)} | المحفوظة: {stats['kept']} | "
                    f"المستبدلة: {stats['replaced']} | من cache: {stats['cached']}\n")
            f.write(f"محذوفة (تكرار اسم): {stats['dup_name']} | "
                    f"(تكرار رابط): {stats['dup_url']} | (بلا رابط): {stats['dropped']}\n")
            f.write("=" * 60 + "\n")
            f.write("\n".join(report_lines[:500]))
    except Exception as e:
        log.warning(f"Report: {e}")

    # Cache
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)
        log.info(f"✔ Cache: {len(cache)}")
    except Exception as e:
        log.warning(f"Cache save: {e}")

    log.info(f"تم في {time.time() - t_start:.1f}s")
    return 0

if __name__ == '__main__':
    sys.exit(main())
