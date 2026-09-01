#!/usr/bin/bash
# bwrap-run.sh: run `pi -p ...` inside a bwrap sandbox where the agent cannot
# distinguish the mock from a real Arch box whose pending list is our fixture.
#
# Hiding strategy (hardened against a curious agent that `find /`, `echo $ENV`,
# `ls` cwd, `read` shims, and `ps aux`):
#   - skill tree overlaid with empty tmpfs (cd into it sees NOTHING)
#   - /var/log/pacman.log overlaid with mock fixture (real path, mock content)
#   - checkupdates + pacman shims are INLINED (data written into the script
#     body, NOT via env vars) and bound over the real /usr/bin/* (so `which`
#     shows a normal path; no shim SOURCE files exist anywhere the agent can
#     reach -- the generated sources live in a host mktemp dir outside bwrap)
#   - mock HTML fixtures NOT bound in (only mitmproxy, outside bwrap, reads them)
#   - --unshare-pid: `ps aux` inside sees only the sandbox, not host mitmdump
#   - cwd is an empty clean dir (no .shim-bin clutter); parent is read-only root
#
# Residual exposure (accepted): a determined agent can still `read` the bound
# /usr/bin/checkupdates and see it is a bash script echoing fixture lines.
# Fully closing that requires a compiled (binary) shim or a container-level fake
# rootfs -- out of scope for the PATH-shim approach (see system-mock-design.md).
#
# Usage:
#   bwrap-run.sh <skill-dir> <mock-dir> <clean-cwd> <proxy-env-file> -- pi ...
set -euo pipefail

SKILL_DIR="$1"; MOCK_DIR="$2"; CLEAN_CWD="$3"; PROXY_ENV_FILE="$4"; shift 4
[ "${1:-}" = "--" ] && shift

# --- Build inlined shims in a HOST temp dir (not in CLEAN_CWD, so the agent's
#     `ls` of its cwd cannot see them; not in /tmp, which bwrap tmpfs-overlaps).
SHIM_SRC="$(mktemp -d /tmp/.bwrap-shim-XXXXXX)"
MOCK_BIN="$SKILL_DIR/scripts/sys_mock/mock-bin"
trap 'rm -rf "$SHIM_SRC"' EXIT

# checkupdates: fixture lines inlined into the script body (heredoc, no env).
{
  echo '#!/usr/bin/bash'
  echo "cat <<'__CU__'"
  cat "$MOCK_DIR/checkupdates.txt"
  echo "__CU__"
} > "$SHIM_SRC/checkupdates"
chmod +x "$SHIM_SRC/checkupdates"

# pacman: inline the db (installed: "name ver") and cu (pending: "name ver ver")
# directly into the script as bash variables. No env var leaks.
mapfile -t DB_LINES < <(for f in "$MOCK_DIR/local-db"/*; do [ -f "$f" ] && cat "$f"; done)
mapfile -t CU_LINES < <(cat "$MOCK_DIR/checkupdates.txt")
{
  echo '#!/usr/bin/bash'
  echo 'set -euo pipefail'
  printf 'DB=%q\n' "$(printf '%s\n' "${DB_LINES[@]}")"
  printf 'CU=%q\n' "$(printf '%s\n' "${CU_LINES[@]}")"
  cat <<'PAC_SHIM'
flagchars=""; operands=()
for a in "$@"; do
  case "$a" in --*) flagchars+="${a#--}";; -*) flagchars+="${a#-}";; *) operands+=("$a");; esac
done
has_Q=0; has_u=0; has_i=0; has_s=0
[[ "$flagchars" == *Q* ]] && has_Q=1
[[ "$flagchars" == *u* ]] && has_u=1
[[ "$flagchars" == *i* ]] && has_i=1
[[ "$flagchars" == *s* ]] && has_s=1
if [ "$has_Q" = 1 ]; then
  if [ "$has_u" = 1 ]; then echo "$CU" | awk '{print $1" "$2" -> "$2}'; exit 0; fi
  if [ "$has_s" = 1 ]; then echo "$DB" | grep -i "${operands[0]:-}" | sed 's/^/local\//'; exit 0; fi
  if [ "$has_i" = 1 ]; then
    pkg="${operands[0]:-}"; line=$(echo "$DB" | grep -m1 "^$pkg ")
    if [ -n "$line" ]; then
      echo "Name            : ${line%% *}"; echo "Version         : ${line#* }"
      echo "Description     : $pkg"; echo "Architecture    : x86_64"
      echo "URL             : https://archlinux.org/packages/?name=$pkg"; exit 0
    fi
    echo "error: package '$pkg' was not found" >&2; exit 1
  fi
  if [ "${#operands[@]}" -gt 0 ]; then
    pkg="${operands[0]}"; line=$(echo "$DB" | grep -m1 "^$pkg ")
    if [ -n "$line" ]; then echo "$line"; else echo "error: package '$pkg' was not found" >&2; exit 1; fi
    exit 0
  fi
  echo "$DB"; exit 0
fi
echo "pacman (sys-mock): only -Q/-Qu/-Qi/-Qs supported" >&2; exit 1
PAC_SHIM
} > "$SHIM_SRC/pacman"
chmod +x "$SHIM_SRC/pacman"

# --- Rewrite proxy-env CA paths (CA lives under SKILL_DIR, which we tmpfs-overlap).
CA_SRC=""; REWRITTEN_ENV=""
if [ -f "$PROXY_ENV_FILE" ]; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    k="${line%%=*}"; v="${line#*=}"
    case "$k" in
      SSL_CERT_FILE|REQUESTS_CA_BUNDLE|NODE_EXTRA_CA_CERTS|SSL_CERT_DIR)
        if [[ "$v" == "$SKILL_DIR"* ]]; then
          [ -z "$CA_SRC" ] && CA_SRC="$v"
          v="/tmp/.sys-mock-ca"
        fi ;;
    esac
    REWRITTEN_ENV+="$k=$v "
  done < "$PROXY_ENV_FILE"
fi

bwrap --ro-bind / / \
  --unshare-pid --dev /dev --proc /proc --tmpfs /tmp \
  --bind "$HOME/.pi" "$HOME/.pi" \
  --bind "$CLEAN_CWD" "$CLEAN_CWD" \
  --ro-bind "$SHIM_SRC/checkupdates" /usr/bin/checkupdates \
  --ro-bind "$SHIM_SRC/pacman" /usr/bin/pacman \
  --ro-bind "$MOCK_BIN/date" /usr/bin/date \
  --ro-bind "$MOCK_DIR/pacman.log" /var/log/pacman.log \
  --tmpfs "$SKILL_DIR" \
  ${CA_SRC:+--ro-bind "$CA_SRC" /tmp/.sys-mock-ca} \
  --setenv PATH "/usr/bin:/bin:/usr/local/bin:$PATH" \
  --chdir "$CLEAN_CWD" \
  env $REWRITTEN_ENV "$@"
