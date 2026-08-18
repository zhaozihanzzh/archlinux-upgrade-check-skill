#!/usr/bin/env python3
"""Unit tests for find_packages_in_text().

Run from the skill directory:
    python3 tests/test_find_packages.py
"""

import sys
import os
import re

# Allow importing from scripts/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.arch_upgrade_check import find_packages_in_text

passed = 0
failed = 0

def test(name, packages, text, expected):
    global passed, failed
    result = find_packages_in_text(text, packages)
    if result == expected:
        passed += 1
        print(f"  ✓ {name}")
    else:
        failed += 1
        print(f"  ✗ {name}")
        print(f"      packages={packages!r}")
        print(f"      text={text!r}")
        print(f"      expected={expected!r}")
        print(f"      got={result!r}")

# ── U1: Full name, simple word ──
test("full-match-simple",
     ["shadow", "systemd"],
     "sg dropped from shadow?",
     {"shadow"})

# ── U2: Full name with hyphen ──
test("full-match-hyphenated",
     ["libxfont2"],
     "error: failed retrieving file 'libxfont2-2.0.9-1-x86_64.pkg.tar.zst'",
     {"libxfont2"})

# ── U3: Base name ≥5 chars ──
test("base-name-ge5-chars",
     ["dovecot-core"],
     "dovecot config",
     {"dovecot-core"})

test("base-name-plasma",
     ["plasma-desktop", "alsa-utils"],
     "I use plasma on my desktop",
     {"plasma-desktop"})

# ── U4: Base name too short (<5 chars) ──
test("base-name-too-short",
     ["libx11"],
     "lib files",
     set())

test("base-name-gtk",
     ["gtk+"],
     "gtk 4.0",
     set())   # gtk is 3 chars

# ── U5/U6: Plus sign handling ──
test("base-plus-full-match",
     ["gtk+"],
     "gtk+ 4.0",
     {"gtk+"})  # full name match

# ── U7: Blacklist - linux ──
test("blacklist-linux",
     ["linux-firmware-intel", "linux-headers", "linux"],
     "Arch Linux",
     {"linux"})   # only "linux" itself (full name), not linux-firmware-* / linux-headers

# ── U8: Blacklist - python ──
test("blacklist-python",
     ["python-pip", "python-certifi", "python"],
     "Python 3.14",
     {"python"})  # only "python" itself (full name), not python-*

# ── U9: Blacklist - archlinux ──
test("blacklist-archlinux",
     ["archlinux-keyring"],
     "https://archlinux.org/packages/",
     set())

# ── U10: Blacklist still allows full name match ──
test("blacklist-still-matches-full",
     ["python-pip", "python-certifi"],
     "python-pip is installed",
     {"python-pip"})

# ── U11: Word boundary protection ──
test("word-boundary-shadowd",
     ["shadow"],
     "shadowd",
     set())

test("word-boundary-wireplumber",
     ["wireplumber"],
     "wire cable",
     set())   # "wire" is 4 chars < 5, no match anyway

# ── U12: Multiple matches ──
test("multiple-matches",
     ["shadow", "systemd", "glibc"],
     "systemd and glibc issues, shadow too",
     {"shadow", "systemd", "glibc"})

# ── U13: No match ──
test("no-match",
     ["alsa-utils", "mesa"],
     "kernel updated",
     set())

# ── U14: Base with special chars ──
test("base-pipewire",
     ["pipewire-jack"],
     "pipewire setup",
     {"pipewire-jack"})

# ── U15: Edge case: empty text ──
test("empty-text",
     ["shadow", "systemd"],
     "",
     set())

# ── U16: Edge case: empty packages ──
test("empty-packages",
     [],
     "sg dropped from shadow?",
     set())

# ── U17: Multi-word base with hyphen followed by base match ──
test("base-name-nvidia",
     ["nvidia-utils"],
     "NVIDIA driver issue",
     {"nvidia-utils"})

# ── Summary ──
print()
print(f"  {'=' * 50}")
print(f"  Results: {passed} passed, {failed} failed")
print(f"  {'=' * 50}")

sys.exit(1 if failed > 0 else 0)
