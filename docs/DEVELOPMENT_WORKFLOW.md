# Development workflow

**Linux is authoritative.** `~/jarvis-trading` on WSL2 Ubuntu 24.04 is the
repository, the runtime and the place `gh` is authenticated. Windows hosts
LM Studio and the GPU; it may run the test suite, but it is not a runtime
and you do not need `gh` there.

## The loop

```bash
cd ~/jarvis-trading
git fetch --all --prune

# ... work ...

.venv/bin/python -m pytest -q                    # full suite
unshare -rn .venv/bin/python -m pytest -q        # hermetic: no network

git add -A && git commit
git push

scripts/dev_preflight.sh                         # environment + exact-SHA CI
```

## Answering "did THIS commit pass CI?"

```bash
.venv/bin/python scripts/github_ci_status.py          # human
.venv/bin/python scripts/github_ci_status.py --json   # machine
```

It queries the Actions API with `head_sha=<your HEAD>` and discards any run
whose `head_sha` is not exactly that commit. Exit codes:

| code | meaning |
|---|---|
| 0 | `SUCCESS` — every run for this SHA concluded successfully |
| 1 | `FAILURE` — at least one failed, cancelled or timed out |
| 2 | `IN_PROGRESS` — queued or running; no verdict yet |
| 3 | `NO_RUN` — GitHub has no run for this exact SHA |
| 4 | `UNAVAILABLE` — `gh` missing, unauthenticated, or the API failed |

`NO_RUN` is **not** a pass. Neither is a green run on the previous commit.

`gh run list --commit` alone is not sufficient — it does not reliably show
both `push` and `pull_request` events, so an empty result there is not proof
that no CI ran.

## Preflight

```bash
scripts/dev_preflight.sh
```

Read-only: no database is opened for writing, no economic job runs, no
secret is printed. Checks the filesystem is not Windows-backed, the venv
exists, git identity, branch/upstream, tree cleanliness, local-vs-remote
sync, `gh` auth and the expected repository, then the exact-SHA CI state.

Exit `0` clean, `1` hard problem, `2` warnings (dirty tree, unpushed HEAD,
CI still running).

## What CI actually runs

| job | what it proves |
|---|---|
| `pytest` | full suite on ubuntu-24.04, `set -o pipefail` before `tee` |
| `offline` | the same suite under `unshare -rn` — **no network at all** |
| `frontend` | typecheck + build |
| `bootstrap` | schema initialises twice from empty; migrations idempotent |
| `dependency_audit` | CVEs — **advisory**, `continue-on-error` by design |
| `secret_scan` | gitleaks — **fail-closed**, deliberately no soft flag |

The `offline` job carries a control that proves the sandbox has no route
out; without it, an `unshare` that silently failed to isolate would run the
suite *with* network and report a hermetic pass.

## Auth

`gh` is authenticated as `jmtibbetts` under the normal user `nullcode` —
never root, never `sudo gh auth login`. `gh auth setup-git` makes git use
that login over HTTPS. The token lives in `~/.config/gh/hosts.yml` (0600)
and belongs nowhere else: not `.env`, not source, not docs, not a fixture.

## GitHub is not a trading dependency

This tooling is for developers and CI. Nothing in `TradeDecision`,
`RiskDecision`, `OrderPlan`, `ExecutionVenue`, settlement, collection or the
scheduler imports it. **If GitHub is down, JARVIS keeps collecting,
deciding and settling** — the only thing that stops is your ability to ask
about CI.

## What must never be committed

`data/` is ignored wholesale — the active book, the 1 GB evidence store and
the 5.3 GB OHLCV cache are runtime state, not source. Verified: zero files
tracked under `data/`, and the largest tracked file in the repository is a
392 KB screenshot.
