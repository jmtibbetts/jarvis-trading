# Runtime architecture

One machine hosts Windows and Linux. That does not give JARVIS two runtimes.

```
WINDOWS 11
└── LM Studio  ──►  NVIDIA GPU
        ▲
        │  HTTP only
        │
WSL2 Ubuntu 24.04  ◄── everything else
    ~/jarvis-trading   repo, venv, API, scheduler, collectors,
                       execution, risk, accounting, learning,
                       databases, WAL/SHM, caches, logs, backups
```

## Canonical

| thing | location | verified |
|---|---|---|
| repository | `/home/nullcode/jarvis-trading` | `ext4` on `/dev/sdd` |
| Python | `.venv` → `/usr/bin/python3.12` | Linux-native |
| economic DB | `data/jarvis.db` | ext4, WAL, 158 MB |
| evidence DB | `data/forward_evidence.db` | ext4, WAL, 1008 MB |
| events DB | `data/events.db` | ext4, WAL, 417 MB |
| OHLCV cache | `data/ohlcv_cache.db` | ext4, WAL, 5.3 GB |
| scheduler lease | `data/jarvis.db.scheduler.lock` | ext4 |
| scheduler | Linux process only | lease held by the Linux PID |
| collectors | Linux process only | verified from `/proc/<pid>/fd` |

Verified from the **running** process's open file descriptors, not from
configuration. The live process carries no `JARVIS_DB_PATH`, so every store
resolves to the repo's `data/` directory on ext4.

## Windows' role, and its limits

Windows hosts the GPU and LM Studio and answers model requests over HTTP.
That exception gives it **no** authority over the database, the scheduler,
the economy, the evidence, the source tree or the Python runtime.

LM Studio is reached by discovery — configured endpoint, then local
candidates, then the WSL host gateway — and the gateway address is **not**
hard-coded, because it changes.

## Enforcement

`lib/runtime_paths` refuses Windows-backed persistence at three points: the
database engine, the scheduler lease, and a startup self-check that will not
serve if any active store is unsafe.

The check is on the **filesystem**, never a string prefix. The case that
matters is the one a prefix check misses — a symlink at
`~/jarvis-trading/data` pointing into `/mnt/c`. Paths are resolved through
symlinks, matched against the longest mount in `/proc/self/mountinfo`, and
judged by that mount's type: `9p`, `drvfs`, `cifs`, `ntfs`, `ntfs3`,
`fuseblk`, `smbfs`, `v9fs`, `prjfs` are refused.

Named refusals: `WINDOWS_BACKED_PERSISTENCE_FORBIDDEN`,
`NON_CANONICAL_RUNTIME_ENVIRONMENT`. Reported at `GET /api/system/runtime`.

**Unknowable is not unsafe.** On a platform with no mount table the guard
declines to refuse, because refusing would assert something it cannot see.
JARVIS stays *testable* on Windows; it is not *operable* there.

## Why this is enforced rather than documented

SQLite's correctness rests on filesystem semantics — advisory locking,
fsync, atomic rename. Through WSL's translation layer to NTFS those are
slower and weaker, and the failure mode is not a clean error but a corrupted
book, or a lock that silently does not lock.

## Persistence engine

SQLite stays. The failures actually measured were application-level:
buffer-drain-before-write, scheduler coupling, unit-blind arithmetic and
path confusion. None implicate the engine. PostgreSQL/Timescale is recorded
as a future option **if** evidence scale later demands it — not now, and not
without measurement.

## The stale Windows tree — read this before touching it

`C:\jarvis-trading-ai-python` is used for editing and running the test
suite. It is **not** a runtime, and it is **not** to be deleted casually,
because it contains a third economy that is neither of the two you know
about:

| copy | positions | trades | cash |
|---|---|---|---|
| active canonical book (ext4) | 0 | 0 | 100,000.00 |
| legacy archive (ext4, `0444`) | 667 | 654* | 63,550.8371643338 |
| **Windows tree `data/jarvis.db`** | **652** | **654** | **74,426.6869165943** |

\* legacy archive trades: 664.

That Windows copy is a **pre-cutover snapshot mutated by Windows test runs**
— last written 2026-08-18 08:12, before the canonical epoch cutover. It is
not the archive, not the dormant original, and not the active book.

**Proven non-authoritative**, 2026-08-19:

- `0` processes hold any of its `.db` files
- the live runtime's open descriptors are entirely ext4 under
  `~/jarvis-trading/data`
- no service, env var or config resolves into `/mnt/c`
- the live process carries no `JARVIS_DB_PATH` at all

It is left exactly as found — not migrated, not deleted, not mutated. If it
is ever removed, remove it *deliberately*, knowing it is a distinct
historical snapshot and not a duplicate of anything preserved elsewhere.
