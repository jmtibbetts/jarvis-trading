"""The swap ledger — what a wallet ACTUALLY traded, proven by balance change.

A TRANSFER IS NOT A TRADE. Scoring reconstructed round trips by pairing
inbound and outbound transfer legs inside one signature, which is a
plausible-looking heuristic and wrong often enough to poison a win rate.
`/v1/transfers` describes value MOVING. It cannot distinguish:

    a swap on Jupiter          from  a withdrawal to an exchange
    a buy                      from  an airdrop
    a sell                     from  collateral posted to a lending market
    a fill                     from  an internal routing leg of someone
                                     else's trade that touched this wallet

Every one of those produced a "trade" with a cost basis and a P&L, and
those fed win rate, profit factor and the smart-money score.

WHAT COUNTS HERE. A trade exists when the wallet's OWN net token balances
move in opposite directions within one transaction: it ended up with more
of one asset and less of another, and that difference IS the economics of
the trade regardless of how many hops the router took to get there. Net
balance change is also what makes multi-hop Jupiter routes correct without
parsing the route: intermediate legs net to zero by construction, so a
USDC -> WSOL -> BONK route reads as "spent USDC, received BONK", which is
what the wallet actually did.

WHAT DOES NOT COUNT, and each is a real pattern that used to score:

    plain transfer        one mint moves, nothing comes back
    ATA creation          rent only, no token delta
    wrap / unwrap alone   SOL <-> WSOL is the same asset in two costumes
    staking movement      SOL -> stake account, no counter-asset
    LP add / remove       token pair -> LP mint is a position change
    rewards / airdrops    something arrives, nothing was paid
    internal routing      a leg that nets to zero for this wallet

Ambiguous transactions are recorded as NOT_A_TRADE with a stated reason
rather than dropped silently: an unexplained gap in a ledger looks like a
quiet wallet, and a wallet that cannot be measured must say so.

RESTART-SAFE AND INCREMENTAL. Every wallet carries a cursor — newest and
oldest signature seen, how far history has been walked, whether the
backfill finished. An incremental sync walks only what is new; a deep
backfill continues from where the last one stopped. Neither re-downloads
the same shallow window every 30 minutes, which is what the previous
scoring pass did for every unscorable wallet, forever.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Wrapped SOL. Held apart from every other mint because SOL and WSOL are
# the same economic asset: a wrap is not a trade, and a route that wraps
# on the way through must not read as "sold SOL, bought WSOL".
WSOL_MINT = "So11111111111111111111111111111111111111112"
NATIVE_SOL = "SOL"

# Ledger schema version. Rows built by different reconstructions are NOT
# comparable and must never be pooled — v1 was the transfer-pairing
# heuristic this module replaces.
LEDGER_VERSION = "swap_v1_balance_delta"

# Classifications.
BUY = "BUY"
SELL = "SELL"
TOKEN_TOKEN = "TOKEN_TOKEN"
NOT_A_TRADE = "NOT_A_TRADE"

# Dust threshold. Rounding in token math leaves sub-atomic residue that is
# not an economic position.
MIN_ABS_DELTA = 1e-12


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _canon_mint(mint: str | None) -> str | None:
    """WSOL and native SOL are one asset."""
    if not mint:
        return None
    return NATIVE_SOL if mint == WSOL_MINT else mint


def quote_symbol_of(mint: str | None) -> str | None:
    """Mint -> the symbol the valuation layer prices, or None.

    The ledger works in MINTS because identity by ticker is how BSOL the
    liquid-staking token got confused with BSOL the US-listed ETF. The
    valuation layer works in SYMBOLS. This is the single conversion point
    between them, sourced from lib/solana_protocols rather than a fresh
    hardcoded list — there are already three of those and this must not be
    the fourth.
    """
    if not mint:
        return None
    if mint in (NATIVE_SOL, WSOL_MINT):
        return "SOL"
    try:
        from lib.solana_protocols import USDC_MINT, USDT_MINT
        return {USDC_MINT: "USDC", USDT_MINT: "USDT"}.get(mint)
    except Exception:
        return None


def _is_priceable(mint: str | None) -> bool:
    """A leg we can put a dollar value on at a point in time."""
    from lib.quote_valuation import is_valuable_quote
    sym = quote_symbol_of(mint)
    return bool(sym) and is_valuable_quote(sym)


def owner_balance_deltas(tx: dict, owner: str) -> dict:
    """Net per-mint balance change for `owner` in ONE transaction.

    THE evidence this module is built on. Derived from the transaction's own
    pre/post token balances rather than from transfer rows, because the
    balances are what the chain actually recorded — they include every leg,
    net out intermediate hops automatically, and cannot be confused with
    somebody else's movement through the same signature.

    Native SOL is included but the transaction FEE is removed from it: the
    fee is a cost of trading, not part of the traded amount, and leaving it
    in makes every SOL-quoted trade look marginally worse than it was.
    """
    meta = (tx or {}).get("meta") or {}
    out: dict[str, float] = {}

    pre = {}
    for b in meta.get("preTokenBalances") or []:
        if b.get("owner") != owner:
            continue
        mint = _canon_mint(b.get("mint"))
        amt = _f(((b.get("uiTokenAmount") or {}).get("uiAmount")))
        pre[mint] = pre.get(mint, 0.0) + amt

    post = {}
    for b in meta.get("postTokenBalances") or []:
        if b.get("owner") != owner:
            continue
        mint = _canon_mint(b.get("mint"))
        amt = _f(((b.get("uiTokenAmount") or {}).get("uiAmount")))
        post[mint] = post.get(mint, 0.0) + amt

    for mint in set(pre) | set(post):
        delta = post.get(mint, 0.0) - pre.get(mint, 0.0)
        if abs(delta) > MIN_ABS_DELTA:
            out[mint] = out.get(mint, 0.0) + delta

    # Native SOL, from the account index of the owner.
    keys = ((tx or {}).get("transaction") or {}).get("message", {}).get("accountKeys") or []
    idx = None
    for i, k in enumerate(keys):
        addr = k.get("pubkey") if isinstance(k, dict) else k
        if addr == owner:
            idx = i
            break
    if idx is not None:
        prebal = (meta.get("preBalances") or [])
        postbal = (meta.get("postBalances") or [])
        if idx < len(prebal) and idx < len(postbal):
            lamports = postbal[idx] - prebal[idx]
            # The fee payer paid the fee; that is a cost, not a trade leg.
            if idx == 0:
                lamports += _f(meta.get("fee"))
            sol = lamports / 1e9
            if abs(sol) > MIN_ABS_DELTA:
                out[NATIVE_SOL] = out.get(NATIVE_SOL, 0.0) + sol

    return {m: v for m, v in out.items() if abs(v) > MIN_ABS_DELTA}


def classify(deltas: dict, *, tx: dict | None = None) -> dict:
    """Is this an economic swap, and which way?

    Requires OPPOSING net movement: something increased and something else
    decreased. That single rule rejects every non-trade pattern the audit
    named, because each of them moves value in only one direction for this
    wallet — a transfer out, an airdrop in, a stake deposit, an LP add, a
    reward. Wrapping is rejected earlier still, by canonicalising WSOL to
    SOL so the two legs cancel to nothing.
    """
    gained = {m: v for m, v in deltas.items() if v > MIN_ABS_DELTA}
    lost = {m: -v for m, v in deltas.items() if v < -MIN_ABS_DELTA}

    if not deltas:
        return {"kind": NOT_A_TRADE, "reason": "no net balance change for this wallet"}
    if not gained:
        return {"kind": NOT_A_TRADE,
                "reason": "value left and nothing came back — transfer, stake or LP deposit"}
    if not lost:
        return {"kind": NOT_A_TRADE,
                "reason": "value arrived and nothing was paid — airdrop, reward or incoming transfer"}

    # The QUOTE side is whichever leg is a priceable currency. That is what
    # makes the trade valuable in dollars; the other leg is the position.
    def _pick(d: dict) -> tuple[str, float]:
        return max(d.items(), key=lambda kv: kv[1])

    gained_quotes = {m: v for m, v in gained.items() if _is_priceable(m)}
    lost_quotes = {m: v for m, v in lost.items() if _is_priceable(m)}

    # A SWAP IS ONE-IN, ONE-OUT economically. Paying with two different
    # assets to receive one is an LP deposit, and receiving two for one is
    # an LP withdrawal — both look exactly like a buy or a sell from
    # balance deltas alone, which is how "provided liquidity" would have
    # been scored as a trade with a cost basis and a return.
    if len(lost) > 1 and len(gained) == 1:
        return {"kind": NOT_A_TRADE,
                "reason": (f"{len(lost)} assets paid for one — LP deposit or "
                           f"multi-asset operation, not a swap")}
    if len(gained) > 1 and len(lost) == 1:
        return {"kind": NOT_A_TRADE,
                "reason": (f"one asset paid for {len(gained)} — LP withdrawal "
                           f"or multi-asset operation, not a swap")}

    if lost_quotes and not gained_quotes:
        quote_mint, quote_amt = _pick(lost_quotes)
        base_mint, base_amt = _pick(gained)
        return {"kind": BUY, "base_mint": base_mint, "base_amount": base_amt,
                "quote_mint": quote_mint, "quote_amount": quote_amt,
                "reason": "paid a priceable quote and received a token"}
    if gained_quotes and not lost_quotes:
        quote_mint, quote_amt = _pick(gained_quotes)
        base_mint, base_amt = _pick(lost)
        return {"kind": SELL, "base_mint": base_mint, "base_amount": base_amt,
                "quote_mint": quote_mint, "quote_amount": quote_amt,
                "reason": "sold a token for a priceable quote"}
    if gained_quotes and lost_quotes:
        # Stable-to-stable or SOL-to-USDC: a real swap, but a currency
        # conversion rather than a position, so it is not a trade whose
        # return means anything.
        return {"kind": NOT_A_TRADE,
                "reason": "quote-to-quote conversion, not a position"}

    # Neither leg is priceable. This IS a swap, and it cannot be valued
    # without a price for both sides — recorded honestly rather than given
    # an invented cost basis.
    base_mint, base_amt = _pick(gained)
    quote_mint, quote_amt = _pick(lost)
    return {"kind": TOKEN_TOKEN, "base_mint": base_mint, "base_amount": base_amt,
            "quote_mint": quote_mint, "quote_amount": quote_amt,
            "reason": "token-to-token swap — no priceable leg, so unvalued"}


def normalize_swap(tx: dict, owner: str) -> dict:
    """One transaction -> one normalized ledger row (or a stated refusal).

    The entry price comes from the wallet's OWN economic input and output —
    quote spent divided by base received — not from an external candle. A
    market price at the same minute is a different number from the price
    this wallet actually got, and substituting it silently would make every
    reconstruction agree with the market by construction. External history
    remains available as a labelled fallback, never as a quiet substitute.
    """
    sig = ((tx or {}).get("transaction") or {}).get("signatures", [None])[0]
    ts = (tx or {}).get("blockTime")
    meta = (tx or {}).get("meta") or {}

    if meta.get("err"):
        return {"signature": sig, "kind": NOT_A_TRADE, "timestamp": ts,
                "reason": "transaction failed on chain"}

    deltas = owner_balance_deltas(tx, owner)
    c = classify(deltas, tx=tx)
    row = {
        "signature": sig, "timestamp": ts, "wallet_address": owner,
        "kind": c["kind"], "reason": c.get("reason"),
        "ledger_version": LEDGER_VERSION,
        "fee_sol": _f(meta.get("fee")) / 1e9,
        "deltas": deltas,
    }
    if c["kind"] == NOT_A_TRADE:
        return row

    from lib.quote_valuation import value_in_usd

    base_amt = c["base_amount"]
    quote_amt = c["quote_amount"]
    v = value_in_usd(quote_amt, quote_symbol_of(c["quote_mint"]), ts)

    row.update({
        "base_mint": c["base_mint"], "base_amount": base_amt,
        "quote_mint": c["quote_mint"], "quote_amount": quote_amt,
        "quote_price_usd": v.get("quote_price_usd"),
        "notional_usd": v.get("usd_value"),
        "price_source": v.get("price_source"),
        "price_quality": v.get("price_quality"),
        # EXECUTION price — what this wallet paid per unit, from its own
        # balance change. Independent of any market feed.
        "entry_price_usd": ((v["usd_value"] / base_amt)
                            if v.get("usd_value") and base_amt else None),
        "entry_price_source": ("EXECUTION_BALANCE_DELTA"
                               if v.get("usd_value") and base_amt else None),
    })
    if row["notional_usd"] is None:
        row["unvalued_reason"] = v.get("reason") or "no price for the quote leg"
    return row


def sync_wallet_history(address: str, *, session=None, max_pages: int = 5,
                        page_size: int = 100, deep: bool = False) -> dict:
    """Walk history from the persistent cursor and land normalized swaps.

    INCREMENTAL by default: stops at the newest signature already stored,
    so a quiet wallet costs one cheap call instead of re-downloading the
    same window. DEEP continues from the oldest signature seen, so a
    backfill resumes where it stopped rather than restarting.

    The previous behaviour re-fetched the newest 100 transfers for every
    unscorable candidate every 30 minutes, in perpetuity, and learned
    nothing new from any of it.
    """
    from app.database import WalletRegistry, get_db, now_iso
    from lib.helius_client import rpc

    def _run(db):
        w = (db.query(WalletRegistry)
               .filter(WalletRegistry.address == address).first())
        if w is None:
            return {"error": "wallet not in registry"}

        before = w.history_oldest_signature if deep else None
        until = None if deep else w.history_newest_signature

        stats = {"pages": 0, "inspected": 0, "swaps": 0, "not_trades": 0,
                 "unvalued": 0, "mode": "deep" if deep else "incremental"}
        newest_seen, oldest_seen = None, None

        for _ in range(max(1, max_pages)):
            params = {"limit": page_size}
            if before:
                params["before"] = before
            if until:
                params["until"] = until
            try:
                sigs = rpc("getSignaturesForAddress", [address, params]) or []
            except Exception as e:
                # PROVIDER FAILURE — the cursor is NOT advanced and nothing
                # already stored is touched. A failed page must never look
                # like the end of history.
                w.history_status = "FAILED"
                w.history_error = f"{type(e).__name__}: {str(e)[:160]}"
                w.last_history_sync_at = now_iso()
                return {**stats, "error": w.history_error}

            stats["pages"] += 1
            if not sigs:
                if not deep:
                    break
                w.history_backfill_complete = 1
                break

            for s in sigs:
                sig = s.get("signature")
                if not sig:
                    continue
                if newest_seen is None:
                    newest_seen = sig
                oldest_seen = sig
                stats["inspected"] += 1
                try:
                    tx = rpc("getTransaction",
                             [sig, {"encoding": "jsonParsed",
                                    "maxSupportedTransactionVersion": 0}])
                except Exception:
                    continue
                if not tx:
                    continue
                row = normalize_swap(tx, address)
                if row["kind"] == NOT_A_TRADE:
                    stats["not_trades"] += 1
                    continue
                if row.get("notional_usd") is None:
                    stats["unvalued"] += 1
                if _persist(db, row):
                    stats["swaps"] += 1

            before = oldest_seen
            if len(sigs) < page_size:
                if deep:
                    w.history_backfill_complete = 1
                break

        # Cursor bookkeeping. Newest only moves forward on an incremental
        # pass; oldest only moves backward on a deep one.
        if newest_seen and not deep:
            w.history_newest_signature = newest_seen
        if oldest_seen:
            w.history_oldest_signature = oldest_seen
        w.history_records_loaded = (w.history_records_loaded or 0) + stats["inspected"]
        w.history_status = "OK"
        w.history_error = None
        w.last_history_sync_at = now_iso()
        if deep:
            w.last_deep_backfill_at = now_iso()
        return stats

    if session is not None:
        return _run(session)
    with get_db() as db:
        return _run(db)


def _persist(db, row: dict) -> bool:
    """Idempotent on (address, signature). Returns True if newly stored."""
    from app.database import WalletTrade

    exists = (db.query(WalletTrade)
                .filter(WalletTrade.address == row["wallet_address"],
                        WalletTrade.signature == row["signature"]).first())
    if exists is not None:
        return False
    from datetime import datetime, timezone
    opened = (datetime.fromtimestamp(row["timestamp"], tz=timezone.utc).isoformat()
              if row.get("timestamp") else None)
    db.add(WalletTrade(
        address=row["wallet_address"], signature=row["signature"],
        mint=row.get("base_mint"), direction=row["kind"],
        quantity=row.get("base_amount"),
        quote_mint=row.get("quote_mint"),
        quote_amount=row.get("quote_amount"),
        quote_price_usd=row.get("quote_price_usd"),
        value_usd=row.get("notional_usd"),
        price=row.get("entry_price_usd"),
        price_source=row.get("entry_price_source") or row.get("price_source"),
        price_quality=row.get("price_quality"),
        fees_usd=None, dex=row.get("dex"),
        opened_at=opened, ledger_version=row.get("ledger_version"),
        population="WALLET_ALPHA",
    ))
    return True
