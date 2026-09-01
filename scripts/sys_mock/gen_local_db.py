#!/usr/bin/env python3
"""Generate the mock pacman local-db from a checkupdates.txt fixture.

`pacman -Q` lists "name version" per installed package. Our mock needs the 6
pending packages to appear installed (so `pacman -Q shadow` works, consistent
with `checkupdates` listing shadow as pending). We synthesize a minimal
local-db dir: one file per package containing "name version".

This is the lightweight form of mock local-db (enough for `-Q`/`-Qi`/`-Qs`).
A real /var/lib/pacman/local tree is NOT needed because the pacman shim
intercepts those queries and reads these files directly.

Usage: gen_local_db.py <checkupdates.txt> <out-dir>
"""
import os
import sys


def main():
    if len(sys.argv) != 3:
        print("usage: gen_local_db.py <checkupdates.txt> <out-dir>", file=sys.stderr)
        sys.exit(2)
    cu, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    n = 0
    with open(cu) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[0]
            # checkupdates format: "name newver" or "name newver oldver".
            # The INSTALLED version is oldver (3rd col) when present; otherwise
            # fall back to the single version listed.
            ver = parts[2] if len(parts) >= 3 else parts[1]
            # pacman -Q format: "name version"
            with open(os.path.join(out, name), "w") as o:
                o.write(f"{name} {ver}\n")
            n += 1
    print(f"wrote {n} packages to {out}")


if __name__ == "__main__":
    main()
