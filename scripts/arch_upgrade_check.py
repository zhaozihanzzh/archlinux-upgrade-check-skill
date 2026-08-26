#!/usr/bin/env python3
"""
arch_upgrade_check.py - Arch Linux Upgrade Safety Checker

Workflow:
  1. Parse /var/log/pacman.log to get last system upgrade date
  2. Run checkupdates to get list of packages to be updated
  3. Fetch Arch Linux News (HTML) since last upgrade
  4. Fetch Arch Linux BBS (HTML) since last upgrade
  5. Cross-reference: find topics whose title or content mention any
     of the packages to be updated
  6. Output a structured JSON report for LLM-based verification

Usage:
  python3 arch_upgrade_check.py
  python3 arch_upgrade_check.py [--days N]

Testing/Reproducibility:
  python3 arch_upgrade_check.py \\
    --mock-pacman-log tests/fixtures/pacman.log \\
    --mock-checkupdates evals/mock/e1/checkupdates.txt \\
    --mock-http-dir evals/mock/e1/http/ \\
    --json
"""

import sys
import re
import os
import json
import subprocess
import time
import argparse
import hashlib
from datetime import datetime, timezone, timedelta
from html import unescape
from urllib.request import urlopen, Request


# ──────────── Configuration ────────────

NEWS_URL = "https://archlinux.org/news/"
BBS_FORUM_URL = "https://bbs.archlinux.org/viewforum.php?id=44"
PACMAN_LOG = "/var/log/pacman.log"
CHECKUPDATES_CMD = ["checkupdates"]
LOOKBACK_CAP_DAYS = 365  # Max days to look back; beyond this, recommend archive stepwise upgrade

# Keywords that signal a news article is about a manual intervention
INTERVENTION_KEYWORDS = [
    "manual intervention", "requires manual", "breaking change",
    "action required", "manual step", "need to", "must run",
    "you should", "you will need", "overwrite", "conflicting files",
    "failed to commit", "requires attention", "upgrade requires",
    "update requires", "rename", "migration",
]

# News titles to always exclude
EXCLUDE_NEWS_TITLES = [
    "leader election", "election results", "congratulations",
    "security advisory", "CVE-",
]


def _is_archlinux():
    """Return True if the host appears to be Arch Linux.

    Used to short-circuit the skill on non-Arch systems, where /var/log/pacman.log
    and checkupdates don't exist and running an upgrade check is meaningless.
    """
    if os.path.exists("/etc/arch-release"):
        return True
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.strip() == "ID=arch":
                    return True
    except OSError:
        pass
    return False


# ──────────── Step 1: Last Upgrade Date ────────────

def get_last_upgrade_date(pacman_log=None):
    """Parse /var/log/pacman.log (or mock path) to find the last system upgrade date."""
    log_path = pacman_log if pacman_log else PACMAN_LOG
    if not log_path or not os.path.exists(log_path):
        return None

    patterns = [
        r'\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})\].*\[PACMAN\].*Running.*-Syu',
        r'\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})\].*\[PACMAN\].*Running.*-Su\b(?!\S)',
        r'\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})\].*starting full system upgrade',
    ]

    last_date = None
    with open(log_path, "r", errors="replace") as f:
        for line in f:
            for pattern in patterns:
                m = re.search(pattern, line)
                if m:
                    date_str = m.group(1)
                    try:
                        dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S%z")
                        last_date = dt
                    except ValueError:
                        continue

    return last_date


# ──────────── Step 2: Checkupdates ────────────

def get_checkupdates(mock_checkupdates=None):
    """Run checkupdates and return a set of package names (lowercase).

    Returns None when checkupdates can't be run successfully (not installed,
    timed out, non-zero exit for any reason). An empty set is returned only for
    a genuine "no updates available" result so that a failure is never mistaken
    for "system is up to date" — that mislabeling is dangerous for a pre-upgrade
    safety check.
    """
    if mock_checkupdates:
        with open(mock_checkupdates) as f:
            packages = set()
            for line in f:
                line = line.strip()
                if line:
                    pkg_name = line.split()[0].strip()
                    if pkg_name:
                        packages.add(pkg_name.lower())
            return packages

    try:
        result = subprocess.run(
            CHECKUPDATES_CMD,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        print("  ERROR: checkupdates not found. Install pacman-contrib.", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("  ERROR: checkupdates timed out.", file=sys.stderr)
        return None

    # checkupdates exit codes: 0 = updates available (listed on stdout),
    # 2 = no updates available (recent pacman-contrib). Anything else is a
    # real failure (mirror error, interrupted, etc.) and must NOT be reported
    # as "up to date".
    if result.returncode == 2:
        return set()
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        detail = detail[-1] if detail else "unknown error"
        print(f"  ERROR: checkupdates failed (exit {result.returncode}): {detail}", file=sys.stderr)
        return None

    packages = set()
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        pkg_name = line.split()[0].strip()
        if pkg_name:
            packages.add(pkg_name.lower())

    return packages


# ──────────── Matching: find package names in text ────────────

def find_packages_in_text(text, packages):
    """
    Find which packages from the update list appear in the given text.
    Uses whole-word matching with word boundaries.
    For hyphenated packages (e.g., 'virtualbox-ext-vnc'),
    also matches the base component if >= 5 chars.

    Returns a set of matched package names (original case from packages).
    """
    text_lower = text.lower()
    matched = set()

    # Common words that should NOT trigger base-name matches for hyphenated packages.
    # These words appear extremely frequently in generic context ("Arch Linux", "Python code")
    # and would produce too many false positives. Full package name matches still work.
    _COMMON_BASE_BLACKLIST = {
        "linux",    # matches from "Arch Linux", "Linux kernel" → linux-firmware-*, linux-headers
        "python",   # matches from "Python 3.14" → python-*
        "archlinux", # matches from "archlinux.org" → archlinux-keyring
    }

    for pkg in packages:
        pkg_lower = pkg.lower()

        # Whole-word match for the full package name (highest confidence)
        # Use (?<!\w) / (?!\w) instead of \b because \b fails at non-word
        # boundaries (e.g. "gtk+ 4.0" — + is non-word, space is non-word, no \b).
        if re.search(r'(?<!\w)' + re.escape(pkg_lower) + r'(?!\w)', text_lower):
            matched.add(pkg)
            continue

        # For hyphenated packages, try base component
        # e.g., 'dovecot' from 'dovecot-2.3' in text "dovecot >= 2.4 requires..."
        if "-" in pkg_lower or "+" in pkg_lower:
            parts = re.split(r'[-+]', pkg_lower)
            base = parts[0]
            if len(base) >= 5 and base not in _COMMON_BASE_BLACKLIST:
                if re.search(r'(?<!\w)' + re.escape(base) + r'(?!\w)(?![+-])', text_lower):
                    matched.add(pkg)
                    continue

    return matched


# ──────────── Mock HTTP helper ────────────

class _MockHTTPResponse:
    """Mimic urllib.response for mock HTTP data."""
    def __init__(self, data):
        self._data = data
    def read(self):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


def _make_mock_urlopen(mock_http_dir):
    """Create a urlopen replacement that reads from mock HTTP directory."""
    def mock_urlopen(req, **kwargs):
        url = req.full_url if hasattr(req, 'full_url') else str(req)
        filename = hashlib.md5(url.encode()).hexdigest() + '.html'
        filepath = os.path.join(mock_http_dir, filename)
        if os.path.exists(filepath):
            with open(filepath, 'rb') as f:
                return _MockHTTPResponse(f.read())
        # If a .json file exists, serve that too
        json_path = os.path.join(mock_http_dir, hashlib.md5(url.encode()).hexdigest() + '.json')
        if os.path.exists(json_path):
            with open(json_path, 'rb') as f:
                return _MockHTTPResponse(f.read())
        raise Exception(f"Mock HTTP file not found: {url} → {filepath}")
    return mock_urlopen


_global_mock_http_dir = None  # set by main() from args
_global_mock_urlopen_override = None  # set by tests for direct monkey-patching


def _get_urlopen():
    """Return mock or real urlopen based on global state."""
    if _global_mock_urlopen_override is not None:
        return _global_mock_urlopen_override
    if _global_mock_http_dir:
        return _make_mock_urlopen(_global_mock_http_dir)
    return urlopen


# ──────────── Step 3: Fetch News HTML ────────────

def fetch_news_page(page_num):
    """Fetch a single news page and return list of articles + has_more flag."""
    _urlopen = _get_urlopen()
    url = f"{NEWS_URL}?page={page_num}"
    req = Request(url, headers={"User-Agent": "ArchUpgradeCheck/1.0"})
    for attempt in range(3):
        try:
            with _urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
                continue
            return [], False

    articles = []
    pattern = re.compile(
        r"<tr>\s*"
        r"<td>(\d{4}-\d{2}-\d{2})</td>\s*"
        r'<td class="wrap"><a href="(/news/[^"]+)"\s*'
        r'title="View: [^"]*">([^<]+)</a></td>\s*'
        r"<td>([^<]+)</td>"
        r"\s*</tr>",
        re.DOTALL,
    )

    for match in pattern.finditer(html):
        date_str, link, title, author = match.groups()
        title = unescape(title)
        author = unescape(author.strip())
        link = "https://archlinux.org" + link
        articles.append((date_str, title, author, link))

    has_more = bool(re.search(rf'href="\?page={page_num + 1}"', html))
    return articles, has_more


def get_article_content(url):
    """Fetch full content of a news article."""
    _urlopen = _get_urlopen()
    req = Request(url, headers={"User-Agent": "ArchUpgradeCheck/1.0"})
    try:
        with _urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

    for pattern in [
        r'<div[^>]*class="article-content"[^>]*>(.*?)</div>\s*</div>',
        r'<div[^>]*id="news-article"[^>]*>(.*?)</div>\s*</div>',
        r'<div[^>]*id="news-article-content"[^>]*>(.*?)</div>',
    ]:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            text = m.group(1)
            text = re.sub(r"<br\s*/?>", "\n", text)
            text = re.sub(r"</p>", "\n\n", text)
            text = re.sub(r"<li>", "\n- ", text)
            text = re.sub(r"</li>", "", text)
            text = re.sub(r"<[^>]+>", "", text)
            text = unescape(text)
            text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
            return text.strip()

    return ""


def fetch_news(since_date):
    """Fetch all news articles since since_date."""
    articles = []
    page = 1
    seen_links = set()

    while True:
        items, has_more = fetch_news_page(page)
        if not items:
            break

        page_oldest = items[-1][0]
        for date_str, title, author, link in items:
            if link in seen_links:
                continue
            seen_links.add(link)

            article_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if article_date >= since_date:
                articles.append({"date": date_str, "title": title, "author": author, "link": link})
            else:
                pass

        oldest_date = datetime.strptime(page_oldest, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # Once the oldest item on this page is older than since_date, the next
        # page can only be older still — stop early instead of paging to the end.
        if oldest_date < since_date:
            break
        if not has_more:
            break

        page += 1
        time.sleep(0.3)

    return articles


# ──────────── Step 4: Fetch BBS HTML ────────────

def fetch_bbs_page(page_num):
    """Fetch a single BBS forum page and return list of topics."""
    _urlopen = _get_urlopen()
    url = f"{BBS_FORUM_URL}&p={page_num}"
    req = Request(url, headers={"User-Agent": "ArchUpgradeCheck/1.0"})
    try:
        with _urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARNING: Failed to fetch BBS page {page_num}: {e}", file=sys.stderr)
        return [], False

    topics = []
    rows = re.findall(r"<tr[^>]*>.*?</tr>", html, re.DOTALL)

    for row in rows:
        if "<th" in row:
            continue

        link_match = re.search(r'href="(viewtopic\.php\?id=(\d+))[^"]*"', row)
        if not link_match:
            continue

        topic_href, topic_id = link_match.groups()
        topic_url = "https://bbs.archlinux.org/" + topic_href

        title_match = re.search(
            r'href="viewtopic\.php\?id=\d+[^"]*"[^>]*>([^<]+)</a>', row
        )
        if not title_match:
            continue
        title = unescape(title_match.group(1).strip())

        is_sticky = "Sticky:" in row
        is_closed = "Closed:" in row
        is_solved = bool(re.search(r"\[(solved|resolved)", title, re.IGNORECASE))

        date_match = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", row)
        if date_match:
            date_obj = datetime.strptime(
                date_match.group(1), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        elif "Today" in row or "Yesterday" in row:
            continue
        else:
            continue

        date_str = date_obj.strftime("%Y-%m-%d")

        # Reply count lives in the <td class="tc2"> cell (2nd data column on the
        # BBS forum listing). Use a class-scoped match instead of a bare
        # `<td>(\d+)</td>` regex, which never matches because every cell carries
        # a class attribute (tcl/tc2/tc3/tcr) — that left replies stuck at 0.
        replies = 0
        replies_match = re.search(r'<td class="tc2">\s*(\d+)\s*</td>', row)
        if replies_match:
            replies = int(replies_match.group(1))

        if not is_sticky:
            topics.append({
                "date": date_str,
                "date_obj": date_obj,
                "title": title,
                "link": topic_url,
                "id": topic_id,
                "is_solved": is_solved,
                "is_closed": is_closed,
                "replies": replies,
            })

    has_more = bool(
        re.search(r'href="viewforum\.php\?id=44&amp;p=' + str(page_num + 1) + '"', html)
    )
    return topics, has_more


def fetch_bbs(since_date):
    """Fetch all BBS topics since since_date."""
    all_topics = []
    page = 1
    seen_ids = set()

    while True:
        topics, has_more = fetch_bbs_page(page)
        if not topics:
            break

        new_count = 0
        for t in topics:
            if t["id"] in seen_ids:
                continue
            seen_ids.add(t["id"])

            if t["date_obj"] >= since_date:
                all_topics.append(t)
                new_count += 1

        first_nonsticky = None
        for t in topics:
            if not t.get("is_sticky", False):
                first_nonsticky = t["date_obj"]
                break
        if first_nonsticky and first_nonsticky < since_date and new_count == 0:
            break

        if not has_more:
            break

        page += 1
        time.sleep(0.3)

    all_topics.sort(key=lambda t: t["date_obj"], reverse=True)
    return all_topics


# ──────────── Step 5: Fetch BBS Topic Content ────────────

def parse_bbs_topic_page(html, since_date):
    """
    Parse a BBS topic page, extract posts newer than since_date.
    Returns (content_text, first_post_date, total_pages, recent_count).
    """
    posts = []

    total_pages = 1
    page_links = re.findall(r'viewtopic\.php\?id=\d+&amp;p=(\d+)', html)
    if page_links:
        total_pages = max(int(p) for p in page_links)

    post_blocks = re.split(r'<div id="p\d+" class="blockpost', html)

    first_post_date = None
    first_post_content = ""

    for block in post_blocks[1:]:
        num_match = re.search(r'<span class="conr">#(\d+)</span>', block)
        if not num_match:
            continue

        post_num = int(num_match.group(1))

        date_match = re.search(r'">(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})</a>', block)
        if not date_match:
            continue

        post_date = datetime.strptime(date_match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

        if first_post_date is None:
            first_post_date = post_date

        content_match = re.search(r'<div class="postmsg">(.*?)</div>\s*</div>', block, re.DOTALL)
        if not content_match:
            content_match = re.search(r'<div class="postmsg">(.*?)</div>', block, re.DOTALL)

        content = ""
        if content_match:
            text = content_match.group(1)
            text = re.sub(r'<br\s*/?>', ' ', text)
            text = re.sub(r'<[^>]+>', '', text)
            text = unescape(text)
            text = re.sub(r'\s+', ' ', text).strip()
            content = text

        # Always capture first post regardless of date
        if post_num == 1:
            first_post_content = content

        if post_date < since_date:
            continue

        posts.append({
            "num": post_num,
            "date": post_date,
            "content": content,
        })

    recent_posts_content = "\n---\n".join(
        f"[Post #{p['num']} @ {p['date'].strftime('%Y-%m-%d %H:%M:%S')}] {p['content']}"
        for p in posts
    )
    is_necrobump = first_post_date is not None and first_post_date < since_date

    return first_post_content, recent_posts_content, first_post_date, total_pages, len(posts), is_necrobump


def fetch_bbs_topic(topic_id, since_date):
    """Fetch a BBS topic and extract posts after since_date."""
    _urlopen = _get_urlopen()
    url = f"https://bbs.archlinux.org/viewtopic.php?id={topic_id}"
    req = Request(url, headers={"User-Agent": "ArchUpgradeCheck/1.0"})

    try:
        with _urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    ⚠ Failed to fetch topic {topic_id}: {e}", file=sys.stderr)
        return "", None, 1, 0, False, False

    first_post_content, recent_posts_content, first_post_date, total_pages, recent_count, is_necrobump = \
        parse_bbs_topic_page(html, since_date)

    if total_pages > 1 and recent_count == 0:
        last_url = f"https://bbs.archlinux.org/viewtopic.php?id={topic_id}&p={total_pages}"
        req = Request(last_url, headers={"User-Agent": "ArchUpgradeCheck/1.0"})
        try:
            with _urlopen(req, timeout=15) as resp:
                last_html = resp.read().decode("utf-8", errors="replace")
            _, last_recent_content, _, _, last_recent_count, _ = \
                parse_bbs_topic_page(last_html, since_date)
            if last_recent_content:
                recent_posts_content = last_recent_content
                recent_count = last_recent_count
        except Exception as e:
            print(f"    ⚠ Failed to fetch topic {topic_id} last page: {e}", file=sys.stderr)

    return first_post_content, recent_posts_content, first_post_date, total_pages, recent_count, is_necrobump


# ──────────── Step 6: Cross-reference matching ────────────

def find_news_matches(news_articles, packages, since_date):
    """
    Match news articles against packages to update.

    For news: check title (must have intervention keywords),
    then fetch content and find all matching packages.

    Returns list of match dicts with context for LLM review.
    """
    matches = []

    for article in news_articles:
        title = article["title"]
        title_lower = title.lower()

        has_intervention_keyword = any(kw in title_lower for kw in INTERVENTION_KEYWORDS)
        is_excluded = any(kw in title_lower for kw in EXCLUDE_NEWS_TITLES)

        if not has_intervention_keyword or is_excluded:
            continue

        # Find packages in title
        title_matched = find_packages_in_text(title, packages)

        # Fetch content
        content = get_article_content(article["link"])
        content_matched = set()
        if content:
            content_matched = find_packages_in_text(content, packages)

        all_matched = title_matched | content_matched

        if all_matched:
            matches.append({
                "type": "news",
                "date": article["date"],
                "title": title,
                "author": article["author"],
                "link": article["link"],
                "matched_packages": sorted(all_matched),
                "title_matched": sorted(title_matched),
                "content_matched": sorted(content_matched - title_matched),
                "content_snippet": content[:500] if content else "",
            })

    return matches


def find_bbs_matches(bbs_topics, packages, since_date):
    """
    Match BBS topics against packages to update.

    Strategy:
    For every non-solved/non-closed topic in the time window:
    1. Fetch first post + recent posts' content
    2. Check title, first post, and recent posts for package names
    3. Match ANY source (title, first_post, recent_posts) — not just title
    4. Output structured data with per-package evidence for LLM review

    Returns list of match dicts with context for LLM review.
    """
    matches = []

    for topic in bbs_topics:
        if topic["is_solved"] or topic["is_closed"]:
            continue

        # Fetch content
        print(f"    Fetching BBS topic {topic['id']}...", file=sys.stderr)

        first_post_content, recent_posts_content, first_post_date, total_pages, recent_count, is_necrobump = \
            fetch_bbs_topic(topic["id"], since_date)

        # Match in all sources
        title_matched = find_packages_in_text(topic["title"], packages)
        first_post_matched = set()
        if first_post_content:
            first_post_matched = find_packages_in_text(first_post_content, packages)
        recent_matched = set()
        if recent_posts_content:
            recent_matched = find_packages_in_text(recent_posts_content, packages)

        all_matched = title_matched | first_post_matched | recent_matched

        if not all_matched:
            time.sleep(0.2)
            continue

        # Build per-package evidence:
        # For each matched package, find textual context showing *where* it appears
        package_evidence = {}
        for pkg in all_matched:
            evidence = []
            pkg_lower = pkg.lower()

            def find_snippet(text_to_search, label):
                """Try to locate pkg in text, returning evidence snippet or None."""
                idx = text_to_search.lower().find(pkg_lower)
                if idx >= 0:
                    start = max(0, idx - 40)
                    end = min(len(text_to_search), idx + len(pkg) + 40)
                    return {"source": label, "snippet": text_to_search[start:end].strip()}
                # For hyphenated packages, also try base match
                if "-" in pkg_lower or "+" in pkg_lower:
                    parts = re.split(r'[-+]', pkg_lower)
                    base = parts[0]
                    if len(base) >= 5:
                        m = re.search(r'(?<!\w)' + re.escape(base) + r'(?!\w)(?![+-])', text_to_search.lower())
                        if m:
                            start = max(0, m.start() - 40)
                            end = min(len(text_to_search), m.end() + 40)
                            return {"source": label, "snippet": text_to_search[start:end].strip(),
                                    "match_type": "base"}
                return None

            # Title
            if pkg in title_matched:
                evidence.append({"source": "title", "snippet": topic["title"].strip()})

            # First post
            if first_post_content and pkg in first_post_matched:
                snippet = find_snippet(first_post_content, "first_post")
                if snippet:
                    evidence.append(snippet)

            # Recent posts
            if recent_posts_content and pkg in recent_matched:
                snippet = find_snippet(recent_posts_content, "recent_posts")
                if snippet:
                    evidence.append(snippet)

            package_evidence[pkg] = evidence

        matches.append({
            "type": "bbs",
            "date": topic["date"],
            "title": topic["title"],
            "link": topic["link"],
            "topic_id": topic["id"],
            "matched_packages": sorted(all_matched),
            "title_matched": sorted(title_matched),
            "first_post_matched": sorted(first_post_matched - title_matched),
            "recent_matched": sorted(recent_matched - title_matched - first_post_matched),
            "package_evidence": package_evidence,
            "first_post": first_post_content[:1000] if first_post_content else "",
            "recent_posts": recent_posts_content[:3000] if recent_posts_content else "",
            "is_necrobump": is_necrobump,
            "recent_post_count": recent_count,
            "total_pages": total_pages,
            "replies": topic.get("replies", 0),
        })

        time.sleep(0.2)

    return matches


# ──────────── Main ────────────

def main():
    parser = argparse.ArgumentParser(description="Arch Linux Upgrade Safety Checker")
    parser.add_argument("--days", type=int, default=None,
                        help="Override time window in days")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON report to stdout (for programmatic use)")
    parser.add_argument("--report-file", type=str, default=None,
                        help="Write JSON report to file")
    parser.add_argument("--minimal", action="store_true",
                        help="Minimal output: omit the full package list and shorten content fields (reduces context usage; pairs well with --report-file)")
    parser.add_argument("--mock-pacman-log", type=str, default=None,
                        help="Path to mock pacman.log for testing/reproducibility")
    parser.add_argument("--mock-checkupdates", type=str, default=None,
                        help="Path to mock checkupdates output for testing/reproducibility")
    parser.add_argument("--mock-http-dir", type=str, default=None,
                        help="Directory with mock HTTP responses (URL→md5(URL)+.html) for testing/reproducibility")
    args = parser.parse_args()

    # Only enforce the Arch-only guard on real systems; any mock input means
    # we're in a test/eval harness that doesn't have /etc/arch-release.
    in_mock_mode = bool(args.mock_pacman_log or args.mock_checkupdates
                        or args.mock_http_dir or os.environ.get('ARCH_CHECK_MOCK_DIR'))
    if not in_mock_mode and not _is_archlinux():
        print("ERROR: this skill only applies to Arch Linux.", file=sys.stderr)
        print("Refusing to run a pre-upgrade check on a non-Arch system.", file=sys.stderr)
        result = {
            "status": "error",
            "message": "Not an Arch Linux system; this upgrade check does not apply.",
            "matches": [],
        }
        _emit_output(result, args)
        sys.exit(1)

    # Auto-detect mock data from environment variables (for skill_eval.py integration)
    # When ARCH_CHECK_MOCK_DIR is set, look for pacman.log and checkupdates.txt inside it
    if not args.mock_http_dir and os.environ.get('ARCH_CHECK_MOCK_DIR'):
        mock_dir = os.environ['ARCH_CHECK_MOCK_DIR']
        if not args.mock_pacman_log:
            pl_path = os.path.join(mock_dir, 'pacman.log')
            if os.path.exists(pl_path):
                args.mock_pacman_log = pl_path
        if not args.mock_checkupdates:
            cu_path = os.path.join(mock_dir, 'checkupdates.txt')
            if os.path.exists(cu_path):
                args.mock_checkupdates = cu_path
        if not args.mock_http_dir:
            http_dir = os.path.join(mock_dir, 'http')
            if os.path.isdir(http_dir):
                args.mock_http_dir = http_dir

    # Set global mock state
    global _global_mock_http_dir
    _global_mock_http_dir = args.mock_http_dir

    # ── Step 1: Get last upgrade date ──
    print("▸ Step 1/4: Checking last system upgrade date...", file=sys.stderr)
    last_upgrade = get_last_upgrade_date(pacman_log=args.mock_pacman_log)

    days_since_orig = 0  # track original gap before any capping
    if last_upgrade:
        if args.days:
            since_date = datetime.now(timezone.utc) - timedelta(days=args.days)
            days_since_orig = args.days
            print(f"  Using --days={args.days} override", file=sys.stderr)
        else:
            since_date = last_upgrade
            days_since_orig = (datetime.now(timezone.utc) - since_date).days
        days_since = days_since_orig
        if days_since > LOOKBACK_CAP_DAYS:
            print(f"  Since: {since_date.strftime('%Y-%m-%d')} ({days_since} days ago) — capped to {LOOKBACK_CAP_DAYS} days", file=sys.stderr)
            since_date = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_CAP_DAYS)
            days_since = LOOKBACK_CAP_DAYS
            print(f"  ╔══════════════════════════════════════════════════════════════╗", file=sys.stderr)
            print(f"  ║  ⚠  Your last upgrade was over a year ago.                ║", file=sys.stderr)
            print(f"  ║                                                              ║", file=sys.stderr)
            print(f"  ║  For systems this old, the recommended approach is NOT       ║", file=sys.stderr)
            print(f"  ║  direct 'pacman -Syu', but step-wise upgrades using dated    ║", file=sys.stderr)
            print(f"  ║  snapshots from:                                            ║", file=sys.stderr)
            print(f"  ║    https://archive.archlinux.org/                            ║", file=sys.stderr)
            print(f"  ║                                                              ║", file=sys.stderr)
            print(f"  ║  Strategy: upgrade to intermediary snapshots (e.g., 6-month  ║", file=sys.stderr)
            print(f"  ║  increments) before catching up to current.                  ║", file=sys.stderr)
            print(f"  ║                                                              ║", file=sys.stderr)
            print(f"  ║  The scan below covers the last 12 months for issues you     ║", file=sys.stderr)
            print(f"  ║  should be aware of when you eventually catch up.            ║", file=sys.stderr)
            print(f"  ╚══════════════════════════════════════════════════════════════╝", file=sys.stderr)
        else:
            print(f"  Since: {since_date.strftime('%Y-%m-%d')} ({days_since} days ago)", file=sys.stderr)
    else:
        print(f"  WARNING: {PACMAN_LOG} not found. Using default 90 days.", file=sys.stderr)
        since_date = datetime.now(timezone.utc) - timedelta(days=90)
    print(file=sys.stderr)

    # ── Step 2: Get checkupdates ──
    print("▸ Step 2/4: Checking packages to update...", file=sys.stderr)
    packages = get_checkupdates(mock_checkupdates=args.mock_checkupdates)
    if packages is None:
        # checkupdates failed — do NOT pretend the system is up to date.
        print(file=sys.stderr)
        print("Cannot determine pending updates — aborting upgrade check.", file=sys.stderr)
        print("Fix checkupdates (install pacman-contrib, check your mirror) and rerun.", file=sys.stderr)
        result = {
            "status": "error",
            "message": "checkupdates failed to run. Cannot determine pending updates; do not assume the system is up to date.",
            "since_date": since_date.strftime("%Y-%m-%d"),
            "matches": [],
        }
        _emit_output(result, args)
        sys.exit(1)
    if packages:
        pkg_list = sorted(packages)
        print(f"  {len(packages)} packages to update", file=sys.stderr)
    else:
        print("  System is up to date!", file=sys.stderr)
        print(file=sys.stderr)
        result = {
            "status": "ok",
            "message": "System is up to date. No packages to update.",
            "packages_to_update": [],
            "since_date": since_date.strftime("%Y-%m-%d"),
            "matches": [],
        }
        _emit_output(result, args)
        return
    print(file=sys.stderr)

    # ── Step 3: Fetch news and BBS ──
    print("▸ Step 3/4: Fetching news & forum posts...", file=sys.stderr)

    print(f"  Fetching news since {since_date.strftime('%Y-%m-%d')}...", file=sys.stderr)
    news_articles = fetch_news(since_date)
    print(f"    Found {len(news_articles)} news articles", file=sys.stderr)

    print(f"  Fetching BBS topics since {since_date.strftime('%Y-%m-%d')}...", file=sys.stderr)
    bbs_topics = fetch_bbs(since_date)
    print(f"    Found {len(bbs_topics)} topics", file=sys.stderr)
    print(file=sys.stderr)

    # ── Step 4: Cross-reference ──
    print("▸ Step 4/4: Cross-referencing with your packages...", file=sys.stderr)
    news_matches = find_news_matches(news_articles, packages, since_date)
    bbs_matches = find_bbs_matches(bbs_topics, packages, since_date)
    print(file=sys.stderr)

    # ── Build result ──
    all_matches = news_matches + bbs_matches

    result = {
        "status": "has_matches" if all_matches else "safe",
        "since_date": since_date.strftime("%Y-%m-%d"),
        "last_upgrade": last_upgrade.strftime("%Y-%m-%d") if last_upgrade else None,
        "lookback_capped": days_since_orig > LOOKBACK_CAP_DAYS if last_upgrade else False,
        "packages_to_update": sorted(packages),
        "packages_count": len(packages),
        "matches": all_matches,
        "match_count": len(all_matches),
    }

    if args.minimal:
        # Omit full package list (big), truncate content fields
        del result["packages_to_update"]
        for m in result["matches"]:
            if "first_post" in m and len(m["first_post"]) > 300:
                m["first_post"] = m["first_post"][:300]
            if "recent_posts" in m and len(m["recent_posts"]) > 1000:
                m["recent_posts"] = m["recent_posts"][:1000]

    _emit_output(result, args)


def _emit_output(result, args):
    """Output the result in requested format.

    Two output modes, mutually exclusive in practice:
      --report-file PATH : write JSON to a file only (recommended — keeps the
                            often-large report out of the agent's conversation
                            context); a one-line confirmation goes to stderr
                            (which does not enter the context)
      --json              : print JSON to stdout (for pipes / human inspection)
    Giving both is allowed and writes the file first; JSON still goes to stdout
    only if --json was explicitly requested.
    """
    if args.report_file:
        with open(args.report_file, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"  Report written to {args.report_file}", file=sys.stderr)
        print(file=sys.stderr)
        if not args.json:
            return

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # Human-readable output
    if result.get("lookback_capped"):
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  ⚠  Your last upgrade was over a year ago.                ║")
        print("║                                                              ║")
        print("║  For systems this old, the recommended approach is NOT       ║")
        print("║  direct 'pacman -Syu', but step-wise upgrades using dated    ║")
        print("║  snapshots from:                                            ║")
        print("║    https://archive.archlinux.org/                            ║")
        print("║                                                              ║")
        print("║  Strategy: upgrade to intermediary snapshots (e.g., 6-month  ║")
        print("║  increments) before catching up to current.                  ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()

    if result["status"] == "ok":
        print("╔══════════════════════════════════════════════════════════╗")
        print("║   No updates available. Your system is up to date.       ║")
        print("╚══════════════════════════════════════════════════════════╝")
        return

    if result["status"] == "safe":
        print("╔══════════════════════════════════════════════════════════╗")
        print("║   No manual intervention issues found for your packages. ║")
        print("║   Your system upgrade should be safe.                    ║")
        print("╚══════════════════════════════════════════════════════════╝")
        return

    matches = result["matches"]
    news_only = [m for m in matches if m["type"] == "news"]
    bbs_only = [m for m in matches if m["type"] == "bbs"]

    print("╔══════════════════════════════════════════════════════════╗")
    print("║   ⚠  Potential issues found — LLM review in progress    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print(f"  {result['packages_count']} packages to update")
    print(f"  {result['match_count']} candidate matches found (to be verified by LLM)")
    print()

    if news_only:
        print("─" * 72)
        print("  OFFICIAL NEWS ANNOUNCEMENTS (candidates)")
        print("─" * 72)
        print()
        for m in news_only:
            print(f"  Title:    {m['title']}")
            print(f"  Date:     {m['date']}")
            print(f"  Author:   {m['author']}")
            print(f"  Link:     {m['link']}")
            if m.get("title_matched"):
                print(f"  Packages in title: {', '.join(m['title_matched'])}")
            if m.get("content_matched"):
                print(f"  Packages in content: {', '.join(m['content_matched'])}")
            print()

    if bbs_only:
        print("─" * 72)
        print("  BBS FORUM TOPICS (candidates)")
        print("─" * 72)
        print()
        for m in bbs_only:
            meta = ""
            if m.get("is_necrobump"):
                meta += f" [necro: {m.get('recent_post_count', '?')} recent posts]"
            if m.get("total_pages", 1) > 1:
                meta += f" [{m['total_pages']} pages]"
            print(f"  Title:    {m['title']}{meta}")
            print(f"  Date:     {m['date']}")
            print(f"  Link:     {m['link']}")
            _print_package_evidence(m.get("package_evidence", {}))
            print()

    print("─" * 72)
    print("  Please review the above before running 'pacman -Syu'")
    print("─" * 72)
    print()


def _print_package_evidence(package_evidence):
    """Print per-package evidence."""
    for pkg, evidence in sorted(package_evidence.items()):
        sources = [e["source"] for e in evidence if e.get("snippet")]
        source_str = ", ".join(sorted(set(sources))) if sources else "base-name only"
        print(f"  Package:  {pkg} (found in: {source_str})")
        for e in evidence:
            snip = e.get("snippet", "")
            if snip:
                tag = e["source"]
                if e.get("match_type") == "base":
                    tag += "/base"
                print(f"    [{tag}] ...{snip[:100]}...")


if __name__ == "__main__":
    main()
