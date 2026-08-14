"""One-time depth census of Twelve Data's full USD crypto catalog.

Asks earliest_timestamp for the 15m series of every USD-quoted pair and
writes data/twelvedata_crypto_depth.json incrementally — the map that
turns 'which coins can the desk actually learn from?' from a guess into
a lookup. Resumable: already-probed symbols are skipped on re-run.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import httpx  # noqa: E402

from lib.twelvedata import earliest_timestamp  # noqa: E402

OUT = Path("data/twelvedata_crypto_depth.json")


def main() -> int:
    catalog = httpx.get("https://api.twelvedata.com/cryptocurrencies",
                        timeout=60.0).json().get("data") or []
    usd = sorted({p["symbol"] for p in catalog
                  if p.get("symbol", "").endswith("/USD")})
    print(f"{len(usd)} USD pairs in catalog")

    depth: dict = {}
    if OUT.exists():
        depth = json.loads(OUT.read_text(encoding="utf-8")).get("pairs", {})
        print(f"resuming: {len(depth)} already probed")

    probed = 0
    for i, sym in enumerate(usd):
        if sym in depth:
            continue
        try:
            ts = earliest_timestamp(sym, "15m")
            depth[sym] = str(ts)[:10] if ts else None
        except Exception as e:
            depth[sym] = f"error: {str(e)[:50]}"
        probed += 1
        if probed % 50 == 0:
            _write(depth)
            print(f"  {i + 1}/{len(usd)} probed...", flush=True)
    _write(depth)

    ok = {s: d for s, d in depth.items()
          if d and not str(d).startswith("error")}
    now = datetime.now(timezone.utc)

    def years_back(d):
        try:
            return (now - datetime.fromisoformat(d).replace(
                tzinfo=timezone.utc)).days / 365.25
        except Exception:
            return 0.0

    tiers = {">=5y": 0, "3-5y": 0, "1-3y": 0, "<1y": 0}
    for d in ok.values():
        y = years_back(d)
        tiers[">=5y" if y >= 5 else "3-5y" if y >= 3 else
              "1-3y" if y >= 1 else "<1y"] += 1
    print(f"probed {len(depth)} | with 15m history: {len(ok)}")
    print("depth tiers:", tiers)
    return 0


def _write(depth: dict) -> None:
    OUT.write_text(json.dumps(
        {"probed_at": datetime.now(timezone.utc).isoformat(),
         "interval": "15m", "pairs": depth},
        indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
