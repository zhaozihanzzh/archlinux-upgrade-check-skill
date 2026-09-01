#!/usr/bin/env python3
"""Discover which real URLs the checker fetches in mock mode, and emit a
URL -> mock-file mapping. Output: JSON map {url: file_basename} plus a list.

Used to configure MockServer expectations (Phase 1 of mock-env-design.md):
each real URL the agent's curl would hit must be mapped to the same mock
HTML the script reads, so with-skill and baseline face identical data.

Run:  python3 scripts/discover_mock_urls.py
"""
import os
import sys
import json
import hashlib

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(SKILL_DIR, 'scripts'))

import arch_upgrade_check as A

MOCK_DIR = os.path.join(SKILL_DIR, 'evals', 'mock', 'e1')
HTTP_DIR = os.path.join(MOCK_DIR, 'http')

url_log = []


def recording_urlopen(req, **kwargs):
    url = req.full_url if hasattr(req, 'full_url') else str(req)
    url_log.append(url)
    # delegate to the real mock reader
    return A._make_mock_urlopen(HTTP_DIR)(req, **kwargs)


def main():
    # Wire up mock globals as main() would
    A._global_mock_http_dir = HTTP_DIR
    A._global_mock_urlopen_override = recording_urlopen

    # Also set ARCH_CHECK_MOCK_DIR so pacman.log / checkupdates.txt resolve
    os.environ['ARCH_CHECK_MOCK_DIR'] = MOCK_DIR

    # Drive a full check run (no report-dir needed; we just want the fetches).
    # Reuse the script's main flow by calling the high-level scan directly.
    # We monkeypatch _emit_output so nothing is printed.
    # Drive the scan directly via the two fetch entry points main() uses.
    # since_date must be a tz-aware datetime (as get_last_upgrade_date returns),
    # not a date -- fetch_news compares it against parsed datetimes.
    from datetime import datetime, timedelta, timezone
    since_date = datetime.now(timezone.utc) - timedelta(days=365)
    try:
        A.fetch_news(since_date)
    except Exception as e:
        print(f"fetch_news raised: {e}", file=sys.stderr)
    try:
        A.fetch_bbs(since_date)
    except Exception as e:
        print(f"fetch_bbs raised: {e}", file=sys.stderr)

    # Dedupe, preserve order
    seen = set()
    urls = []
    for u in url_log:
        if u not in seen:
            seen.add(u)
            urls.append(u)

    mapping = {}
    for u in urls:
        h = hashlib.md5(u.encode()).hexdigest() + '.html'
        jp = hashlib.md5(u.encode()).hexdigest() + '.json'
        fp = os.path.join(HTTP_DIR, h)
        fjp = os.path.join(HTTP_DIR, jp)
        if os.path.exists(fp):
            mapping[u] = h
        elif os.path.exists(fjp):
            mapping[u] = jp
        else:
            mapping[u] = None  # URL fetched but no mock file (would have raised)

    out = {'urls_in_fetch_order': urls, 'url_to_file': mapping}
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
