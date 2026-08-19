#!/usr/bin/env bash
# Is this environment ready to develop JARVIS, and is HEAD actually green?
#
# One command instead of remembering nine. It only READS: no database is
# opened for writing, no economic job runs, no secret is printed.
#
# Exit codes let a script branch on the answer:
#   0  everything checked passed
#   1  a hard problem (wrong repo, no auth, Windows-backed storage)
#   2  a soft problem (dirty tree, CI still running, HEAD not pushed)
set -uo pipefail

REPO_EXPECTED="jmtibbetts/jarvis-trading"
hard=0
soft=0

say()  { printf '%-22s %s\n' "$1" "$2"; }
bad()  { printf '%-22s %s\n' "$1" "$2"; hard=1; }
warn() { printf '%-22s %s\n' "$1" "$2"; soft=1; }

root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "not inside a git repository"; exit 1; }
cd "$root" || exit 1

echo "── environment ─────────────────────────────────────────────"
say "REPO ROOT" "$root"

# THE RUNTIME INVARIANT. JARVIS keeps all active state on Linux-native
# storage; SQLite's locking and fsync semantics are weaker through the
# Windows translation layer and the failure mode is a corrupted book.
fstype="$(df -T . 2>/dev/null | awk 'NR==2{print $2}')"
case "$fstype" in
  9p|drvfs|cifs|ntfs|ntfs3|fuseblk) bad "FILESYSTEM" "$fstype — Windows-backed, NOT a valid runtime" ;;
  "")                               warn "FILESYSTEM" "unknown (df unavailable)" ;;
  *)                                say  "FILESYSTEM" "$fstype" ;;
esac

if [ -x ./.venv/bin/python ]; then
  say "PYTHON" "$(./.venv/bin/python -V 2>&1) at .venv"
else
  warn "PYTHON" "no .venv/bin/python — run scripts/bootstrap_ubuntu.sh"
fi

echo
echo "── git ─────────────────────────────────────────────────────"
command -v git >/dev/null || { bad "GIT" "not installed"; exit 1; }
say "GIT" "$(git --version)"
say "IDENTITY" "$(git config --get user.name) <$(git config --get user.email)>"

branch="$(git branch --show-current)"
say "BRANCH" "${branch:-(detached)}"
if [ -n "$(git status --porcelain)" ]; then
  warn "TREE" "dirty — $(git status --porcelain | wc -l) file(s) uncommitted"
else
  say "TREE" "clean"
fi

sha="$(git rev-parse HEAD)"
say "LOCAL SHA" "${sha:0:12}"
if upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"; then
  say "UPSTREAM" "$upstream"
  remote_sha="$(git rev-parse "$upstream" 2>/dev/null)"
  if [ "$remote_sha" = "$sha" ]; then
    say "SYNC" "IN_SYNC"
  else
    ahead="$(git rev-list --count "$upstream..HEAD" 2>/dev/null || echo ?)"
    behind="$(git rev-list --count "HEAD..$upstream" 2>/dev/null || echo ?)"
    warn "SYNC" "ahead $ahead / behind $behind — HEAD is not what CI sees"
  fi
else
  warn "UPSTREAM" "none — pushes and CI checks have no branch to target"
fi

echo
echo "── github ──────────────────────────────────────────────────"
if ! command -v gh >/dev/null; then
  bad "GH" "not installed — exact-SHA CI cannot be verified"
else
  say "GH" "$(gh --version | head -1)"
  if gh auth status >/dev/null 2>&1; then
    # The account name only. The token is never read, printed or logged.
    acct="$(gh auth status 2>&1 | sed -n 's/.*account \([A-Za-z0-9_-]*\).*/\1/p' | head -1)"
    say "GH AUTH" "OK (${acct:-unknown})"
    nwo="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null)"
    if [ "$nwo" = "$REPO_EXPECTED" ]; then
      say "REPOSITORY" "$nwo"
    else
      bad "REPOSITORY" "${nwo:-unresolved} (expected $REPO_EXPECTED)"
    fi
  else
    bad "GH AUTH" "not authenticated — run: gh auth login"
  fi
fi

echo
echo "── ci for THIS exact commit ────────────────────────────────"
if [ -x ./.venv/bin/python ] && [ -f scripts/github_ci_status.py ]; then
  ./.venv/bin/python scripts/github_ci_status.py
  case $? in
    0) ;;
    1) hard=1 ;;
    *) soft=1 ;;
  esac
else
  warn "CI" "scripts/github_ci_status.py unavailable"
fi

echo
if [ "$hard" -ne 0 ]; then
  echo "PREFLIGHT: BLOCKED"
  exit 1
elif [ "$soft" -ne 0 ]; then
  echo "PREFLIGHT: OK with warnings"
  exit 2
fi
echo "PREFLIGHT: OK"
