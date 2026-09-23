#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IPTV Playlist Generator — Headless (GitHub Actions)
- Fetches Russian channels from many public sources
- Validates each stream
- Updates playlist.m3u + working_links.json cache
- Adds ANY Russian-language channel (Cyrillic detection + reference matching)
"""

import sys
import os
import json
import time
import re
import socket
import logging
import concurrent.futures
from datetime import datetime
from typing import List, Optional, Dict, Set, Tuple
from urllib.parse import urlparse, quote
from difflib import SequenceMatcher
from collections import defaultdict

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("iptv")

# ============================================================
# Config (env vars, GitHub-friendly)
# ============================================================
class Cfg:
    MAX_WORKERS     = int(os.environ.get('MAX_WORKERS', '50'))
    CHECK_TIMEOUT   = int(os.environ.get('CHECK_TIMEOUT', '5'))
    MAX_CHANNELS    = int(os.environ.get('MAX_CHANNELS', '4000'))
    OUTPUT          = os.environ.get('OUTPUT_PLAYLIST', 'playlist.m3u')
    CACHE_FILE      = os.environ.get('CACHE_FILE', 'working_links.json')
    KEEP_DUPLICATES = os.environ.get('KEEP_DUPLICATES', 'true').lower() == 'true'
    ADD_ALL_RUSSIAN = os.environ.get('ADD_ALL_RUSSIAN', 'true').lower() == 'true'

# ============================================================
# Sources — (name, url, priority, is_russian_source)
# ============================================================
SOURCES = [
    ("IPTVru",        "https://smolnp.github.io/IPTVru/IPTVru.m3u", 1, True),
    ("IPTVstable",    "https://smolnp.github.io/IPTVru/IPTVstable.m3u8", 1, True),
    ("iptv-org RU",   "https://iptv-org.github.io/iptv/countries/ru.m3u", 1, True),
    ("Zabava",        "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-full.m3u", 2, True),
    ("Spirt007",      "https://raw.githubusercontent.com/Spirt007/Tvru/refs/heads/Master/Rus.m3u", 3, True),
    ("SlyNet",        "https://slynet-iptv2025.do.am/FreeBestTV.m3u8", 3, True),
    ("Free-TV",       "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8", 3, False),
    ("iptv-org all",  "https://iptv-org.github.io/iptv/index.m3u", 4, False),
    ("artem-art998",  "https://raw.githubusercontent.com/artem-art998/IPTVru/refs/heads/main/iptv126.m3u", 4, True),
    ("empty180",      "https://raw.githubusercontent.com/empty180/IPTVrus/refs/heads/main/pishma.m3u", 4, True),
    ("naggdd",        "https://raw.githubusercontent.com/naggdd/iptv/refs/heads/main/ru.m3u", 4, True),
    ("LoganetX",      "https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetXAll.m3u", 4, True),
    ("LoganetX Strawberry", "https://raw.githubusercontent.com/blackbirdstudiorus/LoganetXIPTV/main/LoganetXStrawberry.m3u", 4, True),
    ("Zabava EF",     "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-ef.m3u", 4, True),
    ("Zabava Reg",    "https://raw.githubusercontent.com/CrocoUser/zabava-project/refs/heads/main/zabava-reg.m3u", 4, True),
]

REFERENCE_SOURCES = [
    "https://raw.githubusercontent.com/smolnp/IPTVru/refs/heads/gh-pages/IPTVmir.m3u8",
    "https://raw.githubusercontent.com/smolnp/IPTVru/refs/heads/gh-pages/" + quote("IPTVххх.m3u"),
]

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': '*/*',
}

# ============================================================
# Name normalization
# ============================================================
_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF\U00002702-\U000027B0"
    "\U000024C2-\U0001F251]+", flags=re.UNICODE
)
_QUALITY_RE = re.compile(
    r'\(\s*(?:720|1080|480|360|2160|4K|8K|UHD|FHD|HEVC|H264|H265)\s*[pPi]?\s*\)',
    re.IGNORECASE
)

def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = _QUALITY_RE.sub('', name)
    name = _EMOJI_RE.sub('', name)
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'^[-–—\s]+|[-–—\s]+$', '', name)
    return name

def has_cyrillic(text: str) -> bool:
    """Detect Russian-language text by Cyrillic characters."""
    return any('\u0400' <= ch <= '\u04FF' for ch in text)

# ============================================================
# Cache
# ============================================================
class Cache:
    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, str] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    self.data = json.load(f)
            except Exception as e:
                logger.warning(f"Cache load failed: {e}")
                self.data = {}

    def save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2, sort_keys=True)
        except Exception as e:
            logger.warning(f"Cache save failed: {e}")

    def get(self, name: str) -> Optional[str]:
        return self.data.get(name.lower()) if name else None

    def set(self, name: str, url: str):
        if name and url:
            self.data[name.lower()] = url

# ============================================================
# Reference channels (defines "known Russian TV")
# ============================================================
def load_reference_channels() -> Set[str]:
    allowed: Set[str] = set()
    for url in REFERENCE_SOURCES:
        try:
            logger.info(f"Loading reference: {url}")
            r = requests.get(url, timeout=25, verify=False, headers=HEADERS)
            if r.status_code != 200:
                logger.warning(f"Reference {url} -> HTTP {r.status_code}")
                continue
            for line in r.text.splitlines():
                if line.startswith('#EXTINF:') and ',' in line:
                    clean = re.sub(r'[a-z0-9-]+="[^"]*"\s*', '', line, flags=re.I)
                    parts = clean.split(',', 1)
                    if len(parts) > 1:
                        nm = normalize_name(parts[1].strip())
                        if nm and len(nm) >= 2:
                            allowed.add(nm.lower())
        except Exception as e:
            logger.error(f"Reference load error {url}: {e}")
    logger.info(f"Loaded {len(allowed)} reference channels")
    return allowed

def matches_reference(name: str, reference: Set[str]) -> bool:
    """Fast matching: exact + substring + (limited) fuzzy."""
    if not name or not reference:
        return False
    n = name.lower().strip()
    if len(n) < 3:
        return False
    if n in reference:
        return True
    # Substring (both ways)
    for ref in reference:
        if len(ref) >= 5 and (ref in n or n in ref):
            return True
    # Fuzzy on similar length only (fast enough)
    n_len = len(n)
    for ref in reference:
        if abs(len(ref) - n_len) > 2:
            continue
        if SequenceMatcher(None, n, ref).ratio() > 0.90:
            return True
    return False

# ============================================================
# M3U parsing
# ============================================================
def parse_m3u(content: str, source_name: str, priority: int) -> List[Dict]:
    channels: List[Dict] = []
    lines = content.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if line.startswith('#EXTINF:'):
            ch = {
                'name': '', 'url': '', 'tvg_id': '', 'tvg_logo': '',
                'group': '', 'quality': 0,
                'source': source_name, 'priority': priority,
            }
            m = re.search(r'tvg-id="([^"]*)"', line);    ch['tvg_id']   = m.group(1) if m else ''
            m = re.search(r'tvg-logo="([^"]*)"', line);  ch['tvg_logo'] = m.group(1) if m else ''
            m = re.search(r'group-title="([^"]*)"', line); ch['group']  = m.group(1) if m else ''

            up = line.upper()
            if   '4K'   in up or '2160' in up: ch['quality'] = 4
            elif '1080' in up or 'FHD'  in up: ch['quality'] = 3
            elif '720'  in up or ' HD'  in up: ch['quality'] = 2
            elif '480'  in up or ' SD'  in up: ch['quality'] = 1

            if ',' in line:
                ch['name'] = normalize_name(line.rsplit(',', 1)[-1].strip())

            j = i + 1
            while j < n and j < i + 15:
                nxt = lines[j].strip()
                if nxt and not nxt.startswith('#'):
                    ch['url'] = nxt
                    break
                j += 1

            if ch['url'] and ch['name'] and len(ch['name']) >= 2:
                channels.append(ch)
            i = j
        else:
            i += 1
    return channels

def fetch_source(url: str, name: str, priority: int, timeout: int = 30) -> List[Dict]:
    try:
        logger.info(f"Fetching {name}")
        r = requests.get(url, timeout=timeout, verify=False, headers=HEADERS)
        if r.status_code != 200:
            logger.warning(f"{name}: HTTP {r.status_code}")
            return []
        chans = parse_m3u(r.text, name, priority)
        logger.info(f"{name}: {len(chans)} channels")
        return chans
    except Exception as e:
        logger.error(f"{name}: {e}")
        return []

# ============================================================
# Stream validation
# ============================================================
def check_stream(url: str, timeout: int) -> Tuple[bool, float]:
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
            resp = s.head(url, timeout=timeout, allow_redirects=True, verify=False)
            dt = time.time() - t0
            if resp.status_code in (200, 206, 301, 302, 304, 307, 308, 405):
                return True, dt
            # Fallback to GET range (some servers block HEAD)
            resp = s.get(url, timeout=timeout, allow_redirects=True,
                         verify=False, stream=True,
                         headers={**HEADERS, 'Range': 'bytes=0-1024'})
            dt = time.time() - t0
            ok = resp.status_code in (200, 206)
            resp.close()
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

# ============================================================
# Main
# ============================================================
def main() -> int:
    logger.info("=" * 64)
    logger.info("IPTV Generator — GitHub Actions (Russian channels)")
    logger.info("=" * 64)
    start = datetime.now()

    # 1. Reference
    reference = load_reference_channels()

    # 2. Cache
    cache = Cache(Cfg.CACHE_FILE)
    logger.info(f"Cache: {len(cache.data)} known working links")

    # 3. Fetch sources (parallel)
    logger.info(f"Fetching {len(SOURCES)} sources...")
    all_channels: List[Dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fetch_source, u, nm, pr): (nm, ru)
                for nm, u, pr, ru in SOURCES}
        for fut in concurrent.futures.as_completed(futs):
            nm, is_ru = futs[fut]
            try:
                chans = fut.result()
                for c in chans:
                    c['is_russian_source'] = is_ru
                all_channels.extend(chans)
            except Exception as e:
                logger.error(f"Source {nm} failed: {e}")

    logger.info(f"Total raw channels: {len(all_channels)}")

    # 4. Filter — keep Russian channels
    filtered: List[Dict] = []
    seen_urls: Set[str] = set()
    stats = {'ru_src': 0, 'cyrillic': 0, 'reference': 0}

    for c in all_channels:
        url = c.get('url')
        if not url or url in seen_urls:
            continue

        keep = False
        if c.get('is_russian_source', False):
            keep = True
            stats['ru_src'] += 1
        elif has_cyrillic(c['name']):
            keep = True
            stats['cyrillic'] += 1
        elif reference and matches_reference(c['name'], reference):
            keep = True
            stats['reference'] += 1

        if keep:
            seen_urls.add(url)
            filtered.append(c)

    logger.info(f"After Russian filter: {len(filtered)} channels  {stats}")

    # 5. Sort by priority / quality
    filtered.sort(key=lambda c: (c['priority'], -c['quality'], c['name'].lower()))

    # 6. Limit
    if len(filtered) > Cfg.MAX_CHANNELS:
        logger.info(f"Trimming to {Cfg.MAX_CHANNELS} (from {len(filtered)})")
        filtered = filtered[:Cfg.MAX_CHANNELS]

    # 7. Validate streams (parallel)
    logger.info(f"Checking {len(filtered)} streams with {Cfg.MAX_WORKERS} workers...")
    working: List[Dict] = []
    total = len(filtered)
    checked = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=Cfg.MAX_WORKERS) as ex:
        futs = {ex.submit(check_stream, c['url'], Cfg.CHECK_TIMEOUT): c
                for c in filtered}
        for fut in concurrent.futures.as_completed(futs):
            c = futs[fut]
            checked += 1
            try:
                ok, dt = fut.result()
                if ok:
                    c['response_time'] = dt
                    working.append(c)
                    cache.set(c['name'], c['url'])
            except Exception:
                pass
            if checked % 100 == 0 or checked == total:
                logger.info(f"  {checked}/{total} checked | working: {len(working)}")

    logger.info(f"Working streams: {len(working)}")

    # 8. Deduplicate by name (keep fastest / highest priority)
    if not Cfg.KEEP_DUPLICATES:
        best: Dict[str, Dict] = {}
        for c in working:
            k = c['name'].lower()
            if k not in best:
                best[k] = c
            else:
                a = best[k]
                if (c['priority'], c.get('response_time', 999)) < \
                   (a['priority'], a.get('response_time', 999)):
                    best[k] = c
        working = list(best.values())
        logger.info(f"After dedup: {len(working)}")
    else:
        # Even with duplicates allowed, drop same URL
        uniq: Dict[str, Dict] = {}
        for c in working:
            key = f"{c['name'].lower()}|{c['url']}"
            uniq[key] = c
        working = list(uniq.values())

    # 9. Sort output
    working.sort(key=lambda c: c['name'].lower())

    # 10. Save M3U
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')
    with open(Cfg.OUTPUT, 'w', encoding='utf-8') as f:
        f.write('#EXTM3U\n')
        f.write(f'# Updated: {now}\n')
        f.write(f'# Channels: {len(working)}\n')
        f.write(f'# Generated by iptv_generator.py (GitHub Actions)\n\n')
        for c in working:
            f.write(f"#EXTINF:-1 ,{c['name']}\n{c['url']}\n")

    logger.info(f"✔ Saved {Cfg.OUTPUT} ({len(working)} channels)")

    # 11. Save cache
    cache.save()
    logger.info(f"✔ Saved {Cfg.CACHE_FILE} ({len(cache.data)} entries)")

    logger.info(f"Done in {(datetime.now() - start).total_seconds():.1f}s")
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        logger.exception(f"Fatal: {e}")
        sys.exit(1)
