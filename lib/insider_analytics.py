"""Insider-transaction signal detection — pure functions over already-fetched
transaction dicts (no I/O), so cluster logic is fully unit-testable in
isolation from the SEC EDGAR client and the DB.

Only P (open-market buy) and S (open-market sale) codes represent an actual
buy/sell decision by the insider — everything else (grants, option exercises,
tax withholding, gifts) is compensation mechanics, not a market view, and is
deliberately excluded from cluster detection. Flags are plain descriptive
labels (MULTIPLE_INSIDERS_BUYING, not "SUSPICIOUS") — this module never
asserts wrongdoing or intent, only reports what the filings say.
"""
from __future__ import annotations

ACTIONABLE_CODES = ("P", "S")


def group_by_ticker(transactions: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for t in transactions:
        ticker = t.get("ticker")
        if not ticker:
            continue
        groups.setdefault(ticker, []).append(t)
    return groups


def cluster_summary(transactions: list[dict]) -> dict:
    """Aggregate P/S transactions for ONE ticker within a caller-chosen window.
    Each transaction dict needs: owner_name, owner_cik, is_officer,
    transaction_code, total_value."""
    buys = [t for t in transactions if t.get("transaction_code") == "P"]
    sells = [t for t in transactions if t.get("transaction_code") == "S"]

    def _owner_key(t: dict):
        return t.get("owner_cik") or t.get("owner_name")

    buyer_keys = {_owner_key(t) for t in buys if _owner_key(t)}
    seller_keys = {_owner_key(t) for t in sells if _owner_key(t)}
    officer_buyers = sorted({t.get("owner_name") for t in buys if t.get("is_officer") and t.get("owner_name")})

    buy_value = sum(t.get("total_value") or 0 for t in buys)
    sell_value = sum(t.get("total_value") or 0 for t in sells)

    flags = []
    if len(buyer_keys) >= 2:
        flags.append("MULTIPLE_INSIDERS_BUYING")
    if len(seller_keys) >= 2:
        flags.append("MULTIPLE_INSIDERS_SELLING")
    if officer_buyers:
        flags.append("OFFICER_BUYING")
    # "_ONLY_CLUSTER" means one-directional AND more than a single lone buy/sell
    # — a solitary insider transaction isn't a cluster, it's just a filing.
    if buy_value > 0 and sell_value == 0 and len(buys) >= 2:
        flags.append("BUY_ONLY_CLUSTER")
    if sell_value > 0 and buy_value == 0 and len(sells) >= 2:
        flags.append("SELL_ONLY_CLUSTER")

    return {
        "buy_count": len(buys),
        "sell_count": len(sells),
        "distinct_buyers": len(buyer_keys),
        "distinct_sellers": len(seller_keys),
        "officer_buyers": officer_buyers,
        "buy_value": round(buy_value, 2),
        "sell_value": round(sell_value, 2),
        "net_value": round(buy_value - sell_value, 2),
        "flags": flags,
    }


def rank_clusters(transactions: list[dict]) -> list[dict]:
    """Group by ticker, summarize, keep only tickers with at least one flag,
    sorted by |net_value| descending (largest net insider conviction first —
    either direction)."""
    results = []
    for ticker, txs in group_by_ticker(transactions).items():
        summary = cluster_summary(txs)
        if summary["flags"]:
            results.append({"ticker": ticker, **summary})
    results.sort(key=lambda r: abs(r["net_value"]), reverse=True)
    return results
