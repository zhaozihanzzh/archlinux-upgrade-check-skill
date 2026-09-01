#!/usr/bin/env python3
"""Generate mock HTTP fixtures for evals.

Takes existing HTML fixtures from tests/fixtures/ and creates
md5-named copies in evals/mock/e{1,2,3}/http/ directories.
Also creates mock pacman.log and checkupdates files for each eval.

Usage: python3 scripts/prepare_mock_fixtures.py
"""

import hashlib
import os
import random
import shutil
from datetime import datetime, timedelta

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_FIXTURES = os.path.join(SKILL_DIR, 'tests', 'fixtures')
MOCK_DIR = os.path.join(SKILL_DIR, 'evals', 'mock')

# Mapping: URL pattern → fixture file (for common resources shared across evals)
# The mock system maps URL → md5(url).hexdigest() + '.html'
URL_FIXTURE_MAP = {
    "https://archlinux.org/news/?page=1": "news_page_1.html",
    "https://archlinux.org/news/?page=14": "news_page_14.html",
    "https://archlinux.org/news/": "news_index.html",
    "https://archlinux.org/feeds/news/": "news_rss.html",
    "https://bbs.archlinux.org/": "bbs_home.html",
    "https://bbs.archlinux.org/viewforum.php?id=2": "bbs_viewforum_id2.html",
    "https://bbs.archlinux.org/viewforum.php?id=27": "bbs_viewforum_id27.html",
    "https://bbs.archlinux.org/viewforum.php?id=44": "bbs_page_1.html",
    "https://bbs.archlinux.org/viewforum.php?id=44&p=1": "bbs_page_1.html",
    "https://bbs.archlinux.org/viewforum.php?id=44&p=24": "bbs_page_24.html",
    "https://bbs.archlinux.org/viewforum.php?id=44&p=50": "bbs_page_50.html",
    "https://bbs.archlinux.org/viewtopic.php?id=314544": "bbs_topic_314544.html",
    "https://bbs.archlinux.org/viewtopic.php?id=314363": "bbs_topic_314363.html",
    "https://bbs.archlinux.org/viewtopic.php?id=314096": "bbs_topic_solved.html",
}

# ── E1: regular-upgrade (14 days) ──
#  - pacman.log shows last upgrade 14 days ago (2026-08-17; today is 2026-08-31)
#  - checkupdates lists 6 pending packages (newver oldver) -- the oldver is
#    what the 08-17 upgrade installed, so local-db (installed) < checkupdates
#    (pending) is self-consistent.
E1_CHECKUPDATES = """\
shadow 4.16.0-1 4.15.0-1
glibc 2.41-1 2.40-1
systemd 256.6-1 256.5-1
linux-firmware-intel 20260801-1 20260601-1
pipewire 1.4.0-1 1.3.0-1
nvidia-utils 570.0-1 565.0-1
"""

# Installed versions after the 08-17 upgrade (== checkupdates oldver). Used to
# build a realistic multi-month pacman.log history ending at that upgrade.
_E1_INSTALLED = {
    'shadow': ('4.14.3-1', '4.15.0-1'),
    'glibc': ('2.39-1', '2.40-1'),
    'systemd': ('256.4-1', '256.5-1'),
    'pipewire': ('1.2.0-1', '1.3.0-1'),
    'nvidia-utils': ('560.0-1', '565.0-1'),
    'linux-firmware-intel': ('20260501-1', '20260601-1'),
}


def _ts(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S+0800')


def _upgrade_block(dt, pkgs):
    """Build a realistic pacman -Syu block (sync + N upgraded lines)."""
    lines = [
        f"[{_ts(dt)}] [PACMAN] Running 'pacman -Syu'",
        f"[{_ts(dt + timedelta(seconds=15))}] [PACMAN] synchronizing package lists",
        f"[{_ts(dt + timedelta(minutes=1))}] [PACMAN] starting full system upgrade",
    ]
    t = dt + timedelta(minutes=2)
    for name, (old, new) in pkgs:
        lines.append(f"[{_ts(t)}] [ALPM] upgraded {name} ({old} -> {new})")
        t += timedelta(seconds=random.randint(20, 60))
    lines.append(f"[{_ts(t)}] [ALPM] transaction completed")
    return lines


def _sync_block(dt, note='synchronizing package lists'):
    return [
        f"[{_ts(dt)}] [PACMAN] Running 'pacman -Sy'",
        f"[{_ts(dt + timedelta(seconds=10))}] [PACMAN] {note}",
        f"[{_ts(dt + timedelta(seconds=30))}] [PACMAN] transaction completed",
    ]


def _install_block(dt, name, old, new):
    return [
        f"[{_ts(dt)}] [PACMAN] Running 'pacman -S {name}'",
        f"[{_ts(dt + timedelta(seconds=20))}] [ALPM] upgraded {name} ({old} -> {new})",
        f"[{_ts(dt + timedelta(seconds=35))}] [ALPM] transaction completed",
    ]


def generate_e1_pacman_log():
    """~200-line realistic pacman.log ending at the most recent upgrade (14 days ago).
    Dates are RELATIVE to datetime.now() so the fixture stays consistent with the
    prompt ('about two weeks ago') no matter when the eval is run."""
    random.seed(20260817)
    # Fixed date: mock-bin/date pins "today" to 2026-08-31, and the last
    # upgrade is 2026-08-17 (14 days ago). Keeping this fixed (not now-14d)
    # keeps the fixture consistent with the mock BBS shadow topic (2026-08-25,
    # after the upgrade) and mock news (latest 2026-07-21, before the upgrade),
    # regardless of the real current date.
    recent = datetime(2026, 8, 17, 13, 14)
    def D(offset_days, h, m):
        return (recent + timedelta(days=offset_days)).replace(hour=h, minute=m)
    lines = []
    # ~10 weeks of history before the recent upgrade, then the upgrade itself,
    # then a few -Sy syncs after (no upgrade) -- explains why checkupdates now
    # lists pending packages the user hasn't pulled.
    lines += _upgrade_block(D(-75, 10, 5), [('linux', ('6.9.3-1', '6.9.4-1'))])
    lines += _sync_block(D(-67, 21, 30))
    lines += _upgrade_block(D(-60, 9, 12), [('mesa', ('24.1.0-1', '24.1.1-1'))])
    lines += _install_block(D(-54, 14, 0), 'firefox', '127.0-1', '127.0.1-1')
    lines += _sync_block(D(-50, 18, 45))
    lines += _upgrade_block(D(-46, 11, 0), [('linux', ('6.9.4-1', '6.9.5-1')), ('linux-firmware-intel', ('20260601-1', '20260615-1'))])
    lines += _sync_block(D(-39, 20, 10))
    lines += _upgrade_block(D(-32, 8, 30), [('xorg-server', ('21.1.12-1', '21.1.13-1'))])
    lines += _sync_block(D(-26, 16, 22))
    lines += _upgrade_block(D(-19, 10, 0), [('linux', ('6.9.5-1', '6.10.2-1'))])
    lines += _sync_block(D(-14, 12, 0))
    lines += _install_block(D(-8, 19, 40), 'git', '2.45.2-1', '2.46.0-1')
    lines += _upgrade_block(D(0, 13, 14), list(_E1_INSTALLED.items()))
    for off, h in [(3, 9), (6, 22), (10, 11), (13, 18)]:
        lines += _sync_block(D(off, h, 5))
    return '\n'.join(lines) + '\n'


E1_PACMAN_LOG = generate_e1_pacman_log()

# ── E2: long-time-no-upgrade (~547 days) ──
#  - pacman.log shows last upgrade ~550 days ago → lookback_capped=true
#  - same checkupdates as E1
def generate_e2_pacman_log():
    """E2: last upgrade ~547 days before the mock 'today' (2026-08-31), i.e.
    2025-03-03. Fixed (mock-bin/date pins today to 2026-08-31)."""
    d = datetime(2025, 3, 3, 12, 0, 0)
    return '\n'.join([
        f"[{_ts(d)}] [PACMAN] Running 'pacman -Syu'",
        f"[{_ts(d + timedelta(seconds=15))}] [PACMAN] synchronizing package lists",
        f"[{_ts(d + timedelta(minutes=5))}] [PACMAN] starting full system upgrade",
        f"[{_ts(d + timedelta(minutes=10))}] [ALPM] upgraded glibc (2.38-1 -> 2.39-1)",
    ]) + '\n'


E2_PACMAN_LOG = generate_e2_pacman_log()
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
