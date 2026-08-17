"""Which Alpaca market-data feed do these keys actually get?

The distinction matters for the Virtual CEX fill models: `iex` is a single
venue's book, which is thin and often shows a misleading spread, while
`sip` is the consolidated tape. Spread and slippage attribution is
MEASURED on sip and merely assumed on iex.

Reads credentials from .env and never prints them.

    .\\.venv\\Scripts\\python.exe scripts\\check_alpaca_feed.py
"""
import json
import os
import urllib.error
import urllib.request

SYMBOL = os.getenv("CHECK_SYMBOL", "AAPL")


def probe(key: str, secret: str, feed: str) -> tuple[bool, str]:
    url = (f"https://data.alpaca.markets/v2/stocks/{SYMBOL}"
           f"/quotes/latest?feed={feed}")
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            q = (json.load(r) or {}).get("quote") or {}
        bid, ask = q.get("bp"), q.get("ap")
        if not bid or not ask:
            # Outside market hours a working subscription can still return
            # an empty quote. That is not a failure of entitlement.
            return True, "authorised, but no two-sided quote right now"
        return True, f"bid {bid}  ask {ask}  spread {round(ask - bid, 4)}"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:160]
        except Exception:
            pass
        return False, f"HTTP {e.code} {body}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    key = os.getenv("ALPACA_API_KEY")
    secret = os.getenv("ALPACA_API_SECRET")
    if not key or not secret:
        print("No ALPACA_API_KEY / ALPACA_API_SECRET found in .env")
        return

    print(f"Probing {SYMBOL} with the keys in .env "
          f"(key ends ...{key[-4:]})\n")
    results = {}
    for feed in ("iex", "sip"):
        ok, detail = probe(key, secret, feed)
        results[feed] = ok
        print(f"  {feed.upper():4s}  {'OK  ' if ok else 'FAIL'}  {detail}")

    print()
    if results.get("sip"):
        print("SIP is authorised on these keys — the Pro subscription "
              "applies. Nothing to change; keep using the paper keys.")
    elif results.get("iex"):
        print("Only IEX is authorised. The keys work, but the Pro plan is "
              "NOT attached to them — worth raising with Alpaca before "
              "wiring the Virtual CEX quote source.")
    else:
        print("Neither feed authorised — the credentials themselves are "
              "being rejected, which is a different problem from the "
              "subscription.")


if __name__ == "__main__":
    main()
