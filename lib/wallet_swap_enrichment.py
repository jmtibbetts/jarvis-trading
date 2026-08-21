"""Full-transaction swap evidence for the signatures the transfers feed
could not explain.

WHAT WAS MISSING. `lib/wallet_swaps` already carries a complete
balance-delta classifier — it reads the transaction's own pre/post
balances, nets out routing hops, removes the fee from the SOL leg, refuses
a failed transaction, refuses a one-sided move, and refuses an LP
operation that merely LOOKS like a swap. It had no production caller, so
723 of 1,266 market observations sat at `UNKNOWN_TRANSFER` while the
evidence that would resolve them was one RPC call away.

This module is that caller, and nothing more. It does NOT decode
transactions — `wallet_swaps.normalize_swap` does. It decides WHICH
signatures are worth a call, spends a bounded number of calls, and records
what came back so the same signature is never bought twice.

WHY PER-SIGNATURE AND NOT PER-WALLET. `wallet_swaps.sync_wallet_history`
walks a wallet's whole history from a cursor; that is the right shape for
building a wallet's trade ledger and the wrong shape here. The cycle
already knows the exact signatures whose classification is unresolved, and
fetching those is bounded by the work outstanding rather than by how much
history a wallet happens to have.

BOUNDED, ALWAYS. Every limit below exists because the alternative was
observed or is obvious: a 3,890-signature unbounded backfill, a retry
storm against a signature that will never resolve, re-enriching a
signature that already succeeded, or starving the interactive API behind a
long provider queue. A failure is recorded against ITS OWN signature and
the cycle continues.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

ENRICHMENT_VERSION = "wallet_swap_enrichment_v1"

# States. The swap-specific ones are new because no existing vocabulary
# distinguished "we have not looked" from "we looked and it is not a trade".
PENDING = "PENDING"
ENRICHED = "ENRICHED"
PARTIAL = "PARTIAL"
RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
PERMANENTLY_UNRESOLVED = "PERMANENTLY_UNRESOLVED"
REFUSED_NON_TRADING = "REFUSED_NON_TRADING"

STATES = (PENDING, ENRICHED, PARTIAL, RETRYABLE_FAILURE,
          PERMANENTLY_UNRESOLVED, REFUSED_NON_TRADING)

#: States that will never be fetched again. ENRICHED and REFUSED_NON_TRADING
#: are both ANSWERS — "this was a buy" and "this was a failed transaction"
#: are equally final, and re-reading either spends a call to learn nothing.
TERMINAL_STATES = frozenset({ENRICHED, REFUSED_NON_TRADING,
                             PERMANENTLY_UNRESOLVED, PARTIAL})

#: The strongest evidence rank this system can produce: the chain's own
#: recorded balances, not a transfer row's description of them.
BALANCE_DELTA_EVIDENCE = "BALANCE_DELTA_EVIDENCE"

# Budgets.
MAX_SIGNATURES_PER_CYCLE = 40
MAX_PROVIDER_CALLS_PER_CYCLE = 60
MAX_ATTEMPTS = 3
MIN_SPACING_S = 0.12
CALL_TIMEOUT_S = 20.0
#: A signature older than this is not worth a call: its event-time price
#: was never captured, so enriching it cannot make it eligible. It is left
#: alone rather than marked unresolved — the evidence is fine, the WINDOW
#: has passed, and those are different facts.
MAX_AGE_SECONDS = 7 * 24 * 3600
#: Backoff between attempts on a retryable failure.
RETRY_BACKOFF_S = (300, 1800, 7200)

_ENV_PREFIX = "JARVIS_WALLET_ENRICHMENT_"


def _cfg_int(name: str, default: int) -> int:
    raw = os.getenv(f"{_ENV_PREFIX}{name}")
    if raw is None:
        return default
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def max_signatures_per_cycle() -> int:
    return _cfg_int("MAX_SIGNATURES", MAX_SIGNATURES_PER_CYCLE)


def max_provider_calls_per_cycle() -> int:
    return _cfg_int("MAX_CALLS", MAX_PROVIDER_CALLS_PER_CYCLE)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt) -> str | None:
    return None if dt is None else dt.isoformat()


def _parse(ts):
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# -- Candidate selection --------------------------------------------------
def candidates(*, limit: int | None = None, now=None) -> list[dict]:
    """Signatures whose classification is unresolved and worth one call.

    THE ORDER IS THE POLICY. Newest first, because a recent event is the
    only kind that can still acquire an event-time price and become a
    thesis; enriching a three-week-old signature produces a better
    classification of something that can never qualify.

    Only observations the transfers feed could NOT explain are candidates.
    A signature already established as a paired swap is not re-bought.
    """
    import json as _json

    from sqlalchemy import text

    from app.database import engine
    from lib import wallet_event_classifier as C
    from lib.wallet_shadow_intel import CURRENT_ONLY_E as _CUR

    now = now or _now()
    cutoff = _iso(now - timedelta(seconds=MAX_AGE_SECONDS))
    cap = int(limit if limit is not None else max_signatures_per_cycle())
    if cap <= 0:
        return []

    out: list[dict] = []
    seen: set = set()
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT e.signatures_json, e.event_time, e.classification, "
            "       e.cluster_id "
            "FROM wallet_shadow_events e "
            "WHERE e.classification IN (:c1, :c2) "
            "  AND e.event_time >= :cut "
            f"  AND {_CUR} "
            "ORDER BY e.event_time DESC LIMIT :lim"),
            {"c1": C.UNKNOWN, "c2": C.PARTIAL_EVIDENCE, "cut": cutoff,
             "lim": max(cap * 4, 100)}).fetchall()

    for sig_json, event_time, classification, cluster_id in rows:
        try:
            sigs = _json.loads(sig_json or "[]")
        except (TypeError, ValueError):
            continue
        for sig in sigs:
            if not sig or sig in seen:
                continue
            seen.add(sig)
            out.append({"signature": sig, "event_time": event_time,
                        "classification": classification,
                        "cluster_id": cluster_id})
        if len(out) >= cap * 3:
            break

    # The shadow event stores ABBREVIATED wallet labels, never addresses —
    # a privacy property worth keeping. The owner is recovered from the
    # observation store instead, which is where the address legitimately
    # lives.
    owners = _owners_for([c["signature"] for c in out])
    ready = []
    for c in out:
        owner = owners.get(c["signature"])
        if not owner:
            # No watched wallet on record for this signature.
            # `wallet_swaps` computes deltas FOR AN OWNER, so without one
            # there is nothing to compute — a missing input, not a failed
            # read.
            continue
        c["wallet_address"] = owner
        ready.append(c)

    return _drop_terminal(ready, now=now)[:cap]


def _owners_for(signatures: list) -> dict:
    """signature -> watched wallet address, from the observation store."""
    import json as _json
    import os as _os
    import sqlite3

    from lib.event_store import _db_path

    out: dict = {}
    if not signatures:
        return out
    path = _db_path()
    if not _os.path.exists(path):
        return out
    wanted = set(signatures)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        chunk = 300
        sigs = list(wanted)
        for i in range(0, len(sigs), chunk):
            part = sigs[i:i + chunk]
            clauses = " OR ".join("dedup_key LIKE ?" for _ in part)
            params = [f"helius:{s}:%" for s in part]
            try:
                rows = con.execute(
                    f"SELECT payload FROM events WHERE {clauses}",
                    params).fetchall()
            except sqlite3.Error as e:
                logger.debug("[SwapEnrichment] owner lookup: %s", e)
                rows = []
            for (payload,) in rows:
                try:
                    d = _json.loads(payload)
                except (TypeError, ValueError):
                    continue
                dk = str(d.get("dedup_key") or "").split(":")
                if len(dk) < 5:
                    continue
                owner = d.get("watched_wallet")
                if owner and dk[1] in wanted and dk[1] not in out:
                    out[dk[1]] = owner
    finally:
        con.close()
    return out


def _drop_terminal(rows: list, *, now) -> list:
    """Remove signatures already answered, or not yet due for a retry."""
    from sqlalchemy import text

    from app.database import engine

    if not rows:
        return []
    sigs = [r["signature"] for r in rows]
    known: dict = {}
    with engine.connect() as conn:
        chunk = 400
        for i in range(0, len(sigs), chunk):
            part = sigs[i:i + chunk]
            marks = ",".join(f":s{j}" for j in range(len(part)))
            params = {f"s{j}": v for j, v in enumerate(part)}
            for sig, state, attempts, nxt in conn.execute(text(
                    f"SELECT signature, state, attempts, next_attempt_at "
                    f"FROM wallet_swap_enrichment "
                    f"WHERE signature IN ({marks})"), params).fetchall():
                known[sig] = (state, attempts or 0, nxt)

    ready = []
    for r in rows:
        rec = known.get(r["signature"])
        if rec is None:
            ready.append(r)
            continue
        state, attempts, nxt = rec
        if state in TERMINAL_STATES:
            continue
        if attempts >= MAX_ATTEMPTS:
            continue
        due = _parse(nxt)
        if due is not None and due > now:
            continue
        ready.append(r)
    return ready


# -- The bounded pass -----------------------------------------------------
def enrich_pending(*, limit: int | None = None,
                   max_calls: int | None = None,
                   rpc_fn=None, now=None) -> dict:
    """Buy full-transaction evidence for a bounded set of signatures.

    NEVER RAISES. A provider failure is recorded against the signature that
    caused it and the pass continues — one unreadable transaction must not
    stop the cycle behind it.
    """
    now = now or _now()
    budget_sigs = int(limit if limit is not None
                      else max_signatures_per_cycle())
    budget_calls = int(max_calls if max_calls is not None
                       else max_provider_calls_per_cycle())

    stats = {"considered": 0, "attempted": 0, "provider_calls": 0,
             "enriched": 0, "refused_non_trading": 0, "partial": 0,
             "failures": 0, "permanently_unresolved": 0,
             "budget_signatures": budget_sigs, "budget_calls": budget_calls,
             "exhausted_budget": False, "errors": [],
             "version": ENRICHMENT_VERSION}
    if budget_sigs <= 0 or budget_calls <= 0:
        return stats

    try:
        picks = candidates(limit=budget_sigs, now=now)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[SwapEnrichment] candidate selection failed: %s", e)
        stats["errors"].append(f"candidates: {type(e).__name__}: {e}"[:200])
        return stats

    stats["considered"] = len(picks)
    if not picks:
        return stats

    if rpc_fn is None:
        try:
            from lib.helius_client import rpc as _rpc
            rpc_fn = _rpc
        except Exception as e:                               # noqa: BLE001
            stats["errors"].append(f"provider unavailable: {e}"[:200])
            return stats

    from lib import wallet_swaps

    last_call = 0.0
    for pick in picks:
        if stats["provider_calls"] >= budget_calls:
            stats["exhausted_budget"] = True
            break

        sig = pick["signature"]
        owner = pick["wallet_address"]
        stats["attempted"] += 1

        gap = MIN_SPACING_S - (time.time() - last_call)
        if gap > 0:
            time.sleep(gap)

        tx, err = None, None
        try:
            stats["provider_calls"] += 1
            last_call = time.time()
            tx = rpc_fn("getTransaction",
                        [sig, {"encoding": "jsonParsed",
                               "maxSupportedTransactionVersion": 0}])
        except Exception as e:                               # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:160]}"

        if err is not None:
            _record_failure(sig, owner, err, now=now, stats=stats)
            continue
        if not tx:
            # A SUCCESSFUL read that returned nothing. The signature is
            # outside the provider's retained history — that is an answer,
            # and retrying it forever would never change it.
            _record_terminal(sig, owner, state=PERMANENTLY_UNRESOLVED,
                             reason="NO_TRANSACTION_RETURNED",
                             detail=("the provider returned no transaction "
                                     "for this signature; it is outside "
                                     "retained history"),
                             now=now)
            stats["permanently_unresolved"] += 1
            continue

        try:
            row = wallet_swaps.normalize_swap(tx, owner)
        except Exception as e:                               # noqa: BLE001
            _record_failure(sig, owner,
                            f"decode {type(e).__name__}: {str(e)[:140]}",
                            now=now, stats=stats)
            continue

        _persist(sig, owner, row, tx, now=now, stats=stats)

    return stats


def _record_failure(sig, owner, detail, *, now, stats) -> None:
    """A read that failed. Bounded: attempts are counted and capped."""
    from sqlalchemy import text

    from app.database import engine, new_id

    stats["failures"] += 1
    if len(stats["errors"]) < 5:
        stats["errors"].append(f"{sig[:8]}...: {detail}")

    with engine.begin() as conn:
        found = conn.execute(text(
            "SELECT id, attempts FROM wallet_swap_enrichment "
            "WHERE signature=:s AND wallet_address=:w"),
            {"s": sig, "w": owner}).fetchone()
        attempts = (found[1] if found else 0) + 1
        exhausted = attempts >= MAX_ATTEMPTS
        if exhausted:
            stats["permanently_unresolved"] += 1
        state = PERMANENTLY_UNRESOLVED if exhausted else RETRYABLE_FAILURE
        backoff = RETRY_BACKOFF_S[min(attempts - 1, len(RETRY_BACKOFF_S) - 1)]
        payload = {
            "state": state, "attempts": attempts,
            "last_error": detail[:400],
            "refusal_reason": "RETRY_BUDGET_EXHAUSTED" if exhausted else None,
            "next_attempt_at": None if exhausted else _iso(
                now + timedelta(seconds=backoff)),
            "updated_at": _iso(now),
        }
        if found:
            sets = ", ".join(f"{k}=:{k}" for k in payload)
            conn.execute(text(
                f"UPDATE wallet_swap_enrichment SET {sets} WHERE id=:id"),
                {**payload, "id": found[0]})
        else:
            conn.execute(text(
                "INSERT INTO wallet_swap_enrichment "
                "(id, signature, wallet_address, state, attempts, "
                " last_error, refusal_reason, next_attempt_at, "
                " first_seen_at, created_at, updated_at) VALUES "
                "(:id, :s, :w, :state, :attempts, :last_error, "
                " :refusal_reason, :next_attempt_at, :now, :now, :now)"),
                {**payload, "id": new_id(), "s": sig, "w": owner,
                 "now": _iso(now)})


def _record_terminal(sig, owner, *, state, reason, detail, now) -> None:
    from sqlalchemy import text

    from app.database import engine, new_id

    with engine.begin() as conn:
        found = conn.execute(text(
            "SELECT id, attempts FROM wallet_swap_enrichment "
            "WHERE signature=:s AND wallet_address=:w"),
            {"s": sig, "w": owner}).fetchone()
        payload = {"state": state, "refusal_reason": reason,
                   "reason": detail, "next_attempt_at": None,
                   "attempts": (found[1] if found else 0) + 1,
                   "updated_at": _iso(now)}
        if found:
            sets = ", ".join(f"{k}=:{k}" for k in payload)
            conn.execute(text(
                f"UPDATE wallet_swap_enrichment SET {sets} WHERE id=:id"),
                {**payload, "id": found[0]})
        else:
            conn.execute(text(
                "INSERT INTO wallet_swap_enrichment "
                "(id, signature, wallet_address, state, refusal_reason, "
                " reason, attempts, next_attempt_at, first_seen_at, "
                " created_at, updated_at) VALUES "
                "(:id, :s, :w, :state, :refusal_reason, :reason, "
                " :attempts, :next_attempt_at, :now, :now, :now)"),
                {**payload, "id": new_id(), "s": sig, "w": owner,
                 "now": _iso(now)})


def _persist(sig, owner, row: dict, tx: dict, *, now, stats) -> None:
    """Land one decoded transaction. IDEMPOTENT on (signature, wallet)."""
    from sqlalchemy import text

    from app.database import engine, new_id
    from lib import wallet_swaps as S

    meta = (tx or {}).get("meta") or {}
    tx_err = meta.get("err")
    kind = row.get("kind")

    if tx_err is not None or kind == S.NOT_A_TRADE:
        state = REFUSED_NON_TRADING
        stats["refused_non_trading"] += 1
    elif kind == S.TOKEN_TOKEN or row.get("notional_usd") is None:
        # A REAL swap that cannot be valued. PARTIAL is the honest state:
        # the direction is established, the dollars are not, and inventing
        # a cost basis is how a token-for-token hop becomes a fake trade.
        state = PARTIAL
        stats["partial"] += 1
    else:
        state = ENRICHED
        stats["enriched"] += 1

    payload = {
        "state": state,
        "refusal_reason": ("TRANSACTION_FAILED_ON_CHAIN" if tx_err is not None
                           else "NOT_AN_ECONOMIC_TRADE"
                           if kind == S.NOT_A_TRADE else None),
        "kind": kind,
        "reason": row.get("reason"),
        "base_mint": row.get("base_mint"),
        "base_amount": row.get("base_amount"),
        "quote_mint": row.get("quote_mint"),
        "quote_amount": row.get("quote_amount"),
        "notional_usd": row.get("notional_usd"),
        "quote_price_usd": row.get("quote_price_usd"),
        "entry_price_usd": row.get("entry_price_usd"),
        "entry_price_source": row.get("entry_price_source"),
        "price_quality": row.get("price_quality"),
        "unvalued_reason": row.get("unvalued_reason"),
        "tx_success": tx_err is None,
        "tx_error": str(tx_err)[:200] if tx_err is not None else None,
        "slot": (tx or {}).get("slot"),
        "block_time": row.get("timestamp"),
        "fee_sol": row.get("fee_sol"),
        "evidence_quality": BALANCE_DELTA_EVIDENCE,
        "ledger_version": row.get("ledger_version"),
        "parser_version": ENRICHMENT_VERSION,
        "next_attempt_at": None,
        "last_error": None,
        "enriched_at": _iso(now),
        "updated_at": _iso(now),
    }

    if state == ENRICHED:
        # THE SAME TRANSACTION, LANDED TWICE ON PURPOSE — and fetched once.
        # `wallet_trades` is the wallet's economic ledger: it is what
        # `wallet_swaps.verified_entry_for` reads to prove an acquisition,
        # and therefore what lets a HOLDER_SNAPSHOT become a
        # VERIFIED_BUY_ENTRY and earn a post-entry alpha measurement. The
        # enrichment row beside it explains ONE observed signature,
        # including the ones that turned out not to be trades.
        #
        # `wallet_swaps._persist` is idempotent on (address, signature), so
        # a reprocessed signature updates nothing and inserts nothing.
        try:
            from app.database import get_db
            with get_db() as db:
                S._persist(db, row)
        except Exception as e:                               # noqa: BLE001
            # The ledger is a CONSUMER of this evidence, not its purpose.
            # Failing to land it must not lose the enrichment record.
            logger.warning("[SwapEnrichment] ledger write failed for %s: %s",
                           sig[:8], e)
            stats.setdefault("ledger_errors", 0)
            stats["ledger_errors"] += 1
        else:
            stats.setdefault("ledger_rows", 0)
            stats["ledger_rows"] += 1

    with engine.begin() as conn:
        found = conn.execute(text(
            "SELECT id, attempts FROM wallet_swap_enrichment "
            "WHERE signature=:s AND wallet_address=:w"),
            {"s": sig, "w": owner}).fetchone()
        payload["attempts"] = (found[1] if found else 0) + 1
        if found:
            sets = ", ".join(f"{k}=:{k}" for k in payload)
            conn.execute(text(
                f"UPDATE wallet_swap_enrichment SET {sets} WHERE id=:id"),
                {**payload, "id": found[0]})
        else:
            cols = list(payload)
            conn.execute(text(
                f"INSERT INTO wallet_swap_enrichment "
                f"(id, signature, wallet_address, first_seen_at, created_at, "
                f" {', '.join(cols)}) VALUES "
                f"(:id, :s, :w, :now, :now, "
                f" {', '.join(':' + c for c in cols)})"),
                {**payload, "id": new_id(), "s": sig, "w": owner,
                 "now": _iso(now)})


# -- What the classifier reads --------------------------------------------
def lookup_factory():
    """signature -> the swap verdict, or None. READ-ONLY.

    Injected into the classifier the same way `entity_lookup` is, so the
    classifier stays pure and testable and this module stays the only thing
    that knows how the evidence is stored.
    """
    from sqlalchemy import text

    from app.database import engine

    table: dict = {}
    try:
        with engine.connect() as conn:
            for r in conn.execute(text(
                    "SELECT signature, wallet_address, state, kind, reason, "
                    "       base_mint, base_amount, quote_mint, "
                    "       quote_amount, notional_usd, entry_price_usd, "
                    "       tx_success, refusal_reason, evidence_quality, "
                    "       block_time "
                    "FROM wallet_swap_enrichment "
                    "WHERE state IN (:s1, :s2, :s3)"),
                    {"s1": ENRICHED, "s2": REFUSED_NON_TRADING,
                     "s3": PARTIAL}).fetchall():
                table[r[0]] = {
                    "signature": r[0], "wallet_address": r[1],
                    "state": r[2], "kind": r[3], "reason": r[4],
                    "base_mint": r[5], "base_amount": r[6],
                    "quote_mint": r[7], "quote_amount": r[8],
                    "notional_usd": r[9], "entry_price_usd": r[10],
                    "tx_success": bool(r[11]) if r[11] is not None else None,
                    "refusal_reason": r[12], "evidence_quality": r[13],
                    "block_time": r[14],
                }
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[SwapEnrichment] lookup unavailable: %s", e)
    return lambda sig: table.get(sig)


def coverage() -> dict:
    """Enrichment state counts, for the desk. Missing is never zero."""
    from sqlalchemy import text

    from app.database import engine

    out = {"by_state": {}, "total": 0, "pending_candidates": None,
           "classified_buys": 0, "classified_sells": 0,
           "non_trading": 0, "unknown_activity": 0,
           "budget_signatures": max_signatures_per_cycle(),
           "budget_calls": max_provider_calls_per_cycle(),
           "max_attempts": MAX_ATTEMPTS, "version": ENRICHMENT_VERSION,
           "state": "MEASURED"}
    try:
        with engine.connect() as conn:
            for state, n in conn.execute(text(
                    "SELECT state, COUNT(*) FROM wallet_swap_enrichment "
                    "GROUP BY state")).fetchall():
                out["by_state"][state] = n
                out["total"] += n
            for kind, n in conn.execute(text(
                    "SELECT kind, COUNT(*) FROM wallet_swap_enrichment "
                    "WHERE state=:s GROUP BY kind"),
                    {"s": ENRICHED}).fetchall():
                if kind == "BUY":
                    out["classified_buys"] = n
                elif kind == "SELL":
                    out["classified_sells"] = n
            out["non_trading"] = out["by_state"].get(REFUSED_NON_TRADING, 0)
            out["unknown_activity"] = out["by_state"].get(
                PERMANENTLY_UNRESOLVED, 0)
    except Exception as e:                                   # noqa: BLE001
        logger.warning("[SwapEnrichment] coverage unavailable: %s", e)
        out["state"] = "UNAVAILABLE"
        out["detail"] = str(e)[:200]
        return out
    try:
        out["pending_candidates"] = len(candidates(limit=500))
    except Exception:                                        # noqa: BLE001
        out["pending_candidates"] = None
    return out
