#!/usr/bin/env python3
"""Generate mock HTTP fixtures for evals.

Takes existing HTML fixtures from tests/fixtures/ and creates
md5-named copies in evals/mock/e{1,2,3}/http/ directories.
Also creates mock pacman.log and checkupdates files for each eval.

Usage: python3 scripts/prepare_mock_fixtures.py
"""

import hashlib
import os
import shutil

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FIXTURES = os.path.join(SKILL_DIR, 'tests', 'fixtures')
MOCK_DIR = os.path.join(SKILL_DIR, 'evals', 'mock')

# Mapping: URL pattern → fixture file (for common resources shared across evals)
# The mock system maps URL → md5(url).hexdigest() + '.html'
URL_FIXTURE_MAP = {
    "https://archlinux.org/news/?page=1": "news_page_1.html",
    "https://archlinux.org/news/?page=14": "news_page_14.html",
    "https://bbs.archlinux.org/viewforum.php?id=44&p=1": "bbs_page_1.html",
    "https://bbs.archlinux.org/viewforum.php?id=44&p=24": "bbs_page_24.html",
    "https://bbs.archlinux.org/viewforum.php?id=44&p=50": "bbs_page_50.html",
    "https://bbs.archlinux.org/viewtopic.php?id=314363": "bbs_topic_314363.html",
    "https://bbs.archlinux.org/viewtopic.php?id=314096": "bbs_topic_solved.html",
}

# ── E1: regular-upgrade (14 days) ──
#  - pacman.log shows last upgrade 14 days ago
#  - checkupdates lists packages that will match content in fixtures
E1_PACMAN_LOG = """\
[2026-08-03T12:00:00+0800] [PACMAN] Running 'pacman -Syu'
[2026-08-03T12:01:00+0800] [PACMAN] synchronizing package lists
[2026-08-03T12:05:00+0800] [PACMAN] starting full system upgrade
[2026-08-03T12:10:00+0800] [ALPM] upgraded shadow (4.14.3-1 -> 4.15.0-1)
[2026-08-03T12:10:00+0800] [ALPM] upgraded glibc (2.39-1 -> 2.40-1)
"""
E1_CHECKUPDATES = """\
shadow 4.16.0-1
glibc 2.41-1 2.40-1
systemd 256.6-1
linux-firmware-intel 20260801-1
pipewire 1.4.0-1
nvidia-utils 570.0-1
"""

# ── E2: long-time-no-upgrade (~547 days) ──
#  - pacman.log shows last upgrade ~550 days ago → lookback_capped=true
#  - same checkupdates as E1
E2_PACMAN_LOG = """\
[2025-01-15T12:00:00+0800] [PACMAN] Running 'pacman -Syu'
[2025-01-15T12:01:00+0800] [PACMAN] synchronizing package lists
[2025-01-15T12:05:00+0800] [PACMAN] starting full system upgrade
[2025-01-15T12:10:00+0800] [ALPM] upgraded glibc (2.38-1 -> 2.39-1)
"""
E2_CHECKUPDATES = E1_CHECKUPDATES

# ── E3: custom-days (90 days, pipewire focus) ──
#  - pacman.log absent (will trigger "--days 90" from user's request)
#  - This eval tests the LLM's ability to pass --days to the script
#  - checkupdates includes pipewire and related packages
E3_PACMAN_LOG = None  # no pacman.log → script falls back to 90-day default
E3_CHECKUPDATES = """\
pipewire 1.4.0-1
pipewire-jack 1.4.0-1
pipewire-alsa 1.4.0-1
wireplumber 0.5.6-1
glibc 2.41-1 2.40-1
systemd 256.6-1
"""


def copy_fixture_by_url(target_dir, url, fixture_file):
    """Copy fixture to mock dir as md5-hashed name."""
    filename = hashlib.md5(url.encode()).hexdigest() + '.html'
    src = os.path.join(TEST_FIXTURES, fixture_file)
    dst = os.path.join(target_dir, filename)
    if os.path.exists(src):
        shutil.copy2(src, dst)
        print(f"  {fixture_file} → {filename}")
    else:
        print(f"  ⚠ SKIP: {fixture_file} not found")


def setup_eval(eval_dir, pacman_log_content, checkupdates_content):
    """Setup mock files for one eval."""
    http_dir = os.path.join(eval_dir, 'http')
    os.makedirs(http_dir, exist_ok=True)
    os.makedirs(eval_dir, exist_ok=True)

    print(f"\n📁 {os.path.basename(eval_dir)}/")

    # Copy all URL→fixture mappings
    for url, fixture_file in URL_FIXTURE_MAP.items():
        copy_fixture_by_url(http_dir, url, fixture_file)

    # Write checkupdates
    cu_path = os.path.join(eval_dir, 'checkupdates.txt')
    with open(cu_path, 'w') as f:
        f.write(checkupdates_content)
    print(f"  checkupdates.txt ({len(checkupdates_content.strip().split(chr(10)))} packages)")

    # Write pacman.log (if any)
    if pacman_log_content:
        pl_path = os.path.join(eval_dir, 'pacman.log')
        with open(pl_path, 'w') as f:
            f.write(pacman_log_content)
        print(f"  pacman.log ({len(pacman_log_content.strip().split(chr(10)))} lines)")


def main():
    print("Preparing mock fixtures for evals...")
    print(f"  Source fixtures: {TEST_FIXTURES}")
    print(f"  Target directory: {MOCK_DIR}")

    setup_eval(os.path.join(MOCK_DIR, 'e1'), E1_PACMAN_LOG, E1_CHECKUPDATES)
    setup_eval(os.path.join(MOCK_DIR, 'e2'), E2_PACMAN_LOG, E2_CHECKUPDATES)
    setup_eval(os.path.join(MOCK_DIR, 'e3'), E3_PACMAN_LOG, E3_CHECKUPDATES)

    print("\n✅ Done! Verify with:")
    for e in ('e1', 'e2', 'e3'):
        print(f"   python3 scripts/arch_upgrade_check.py --json --mock-pacman-log evals/mock/{e}/pacman.log --mock-checkupdates evals/mock/{e}/checkupdates.txt --mock-http-dir evals/mock/{e}/http/ | python3 -m json.tool | head -20")
    print()


if __name__ == '__main__':
    main()
