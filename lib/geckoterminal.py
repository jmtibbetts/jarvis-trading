"""One GeckoTerminal client, because the same 429 bug was written three times.

`dex_discovery`, the `/onchain/surge` route and `wallet_discovery` each
fetched pools directly, and each treated HTTP 429 as a permanent empty
result. The visible consequence was identical every time: a panel reporting
"nothing found" when the truth was "could not look" — the §4 failure this
codebase spends most of its effort avoiding, reintroduced by copy-paste.

429 is the one status that explicitly means "ask again shortly", so it is
the one worth retrying. Every other status fails on the first attempt: a
500 is not an invitation to hammer.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

BASE = "https://api.geckoterminal.com/api/v2"
RETRIES = 3
BACKOFF_S = (4.0, 9.0)
# Keyless GeckoTerminal allows roughly 30 calls a minute. Self-inflicted
# 429s are the easy failure, so calls are paced process-wide.
SPACING_S = 2.5
_last_call = 0.0


def get(path: str, *, timeout: float = 30.0,
        errors: list | None = None) -> dict | None:
    """GET a GeckoTerminal path, retrying only on 429.

    Returns the decoded body, or None with the reason appended to `errors`.
    None means UNKNOWN — never "empty" — and callers must render it that
    way.
    """
    global _last_call
    import httpx

    url = f"{BASE}/{path.lstrip('/')}"
    for attempt in range(RETRIES):
        wait = SPACING_S - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            r = httpx.get(url, timeout=timeout)
        except Exception as e:
            if errors is not None:
                errors.append(f"{path}: {type(e).__name__}")
            return None
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                if errors is not None:
                    errors.append(f"{path}: unparseable JSON")
                return None
        if r.status_code == 429 and attempt < RETRIES - 1:
            # Honour Retry-After when sent; the server knows its own window
            # better than a constant does.
            try:
                hinted = float(r.headers.get("Retry-After", "") or 0)
            except ValueError:
                hinted = 0.0
            time.sleep(max(hinted, BACKOFF_S[attempt]))
            continue
        if errors is not None:
            suffix = f" after {RETRIES} attempts" if r.status_code == 429 else ""
            errors.append(f"{path}: HTTP {r.status_code}{suffix}")
        return None
    return None


def solana_pools(path: str, errors: list | None = None) -> list[dict]:
    """`trending_pools` or `new_pools` for Solana, as a list."""
    body = get(f"networks/solana/{path}", errors=errors)
    return (body or {}).get("data") or []
