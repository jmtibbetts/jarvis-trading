"""Prove this process genuinely has no route out. Exit 0 if isolated.

THE CONTROL FOR THE HERMETIC TEST. The offline suite is only evidence if the
sandbox is real. An `unshare -rn` that silently failed to isolate would run
the whole suite WITH network and report a hermetic pass — a check that
reports without protecting, which is worse than no check at all.

Run this INSIDE the same sandbox as the suite:

    unshare -rn .venv/bin/python scripts/assert_no_network.py

A file rather than `python -c`: an inline program inside a YAML block scalar
keeps the block's indentation, which breaks any compound statement.
"""
from __future__ import annotations

import socket
import sys

# Well-known resolvers, tried directly by IP so DNS is not involved. If the
# namespace is isolated these fail immediately with an OS error.
PROBES = (("1.1.1.1", 53), ("8.8.8.8", 53))


def main() -> int:
    reached = []
    for host, port in PROBES:
        try:
            socket.create_connection((host, port), timeout=5).close()
            reached.append(f"{host}:{port}")
        except OSError:
            continue
    if reached:
        print("SANDBOX IS NOT ISOLATED — reached " + ", ".join(reached))
        print("The offline suite would run WITH network and report a "
              "hermetic pass, which proves nothing.")
        return 1
    print("ok  sandbox has no route out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
