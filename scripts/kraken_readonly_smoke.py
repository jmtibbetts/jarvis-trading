"""Read-only Kraken smoke check, for the two tests CI cannot run.

WHY THIS EXISTS. `test_venues.py` has two credentialed tests, and on any
public runner they skip — correctly, since no CI should hold operator keys.
But the thing they check is not cosmetic: the published Kraken schedule
reads 0.40%/0.25% for BTC while the measured account tier was 0.8%/0.4%, so
pricing from the table understated true cost by HALF, in the direction that
lets unprofitable trades through. That deserves an occasional real check on
a machine that does have the keys.

IT CANNOT PLACE AN ORDER. Not by convention — structurally. It calls only
`lib.venues.account_fee()` and `fee_for()`, which read a fee tier, and it
asserts before exiting that no order-submitting symbol was ever imported
into this process. If a future refactor routes account_fee through an
order-capable client, this fails rather than trusting itself.

    python scripts/kraken_readonly_smoke.py

Exit 0 = credentials work and the measured tier is being preferred.
Exit 2 = no credentials (not a failure; that is the normal CI state).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Anything that could submit. Checked after the read, so a refactor that
# quietly pulls an order path into the fee lookup is caught here.
ORDER_SURFACE = ("add_order", "create_order", "submit_order", "place_order")


def main() -> int:
    from lib import venues

    print("Kraken read-only smoke check")
    print("  this process will read a fee tier and nothing else\n")

    fee = venues.account_fee("kraken")
    if fee is None:
        print("  account fee : UNREADABLE")
        print("\nNo usable Kraken credentials. This is the normal state on CI")
        print("and is not a failure - the deterministic fee-preference tests")
        print("in tests/test_venues.py cover the logic without an account.")
        return 2

    print(f"  account fee : {fee}")
    rate, why = venues.fee_for("kraken")
    print(f"  fee_for()   : {rate} ({why})")

    problems = []
    if "MEASURED" not in why:
        problems.append("the measured tier was readable but NOT preferred")

    table_rate, table_why = venues.fee_for("kraken", use_account=False)
    print(f"  published   : {table_rate} ({table_why})")
    if rate < table_rate:
        # The direction that matters. Cheaper-than-published is exactly the
        # error mode that lets unprofitable trades through.
        problems.append(f"measured {rate} is CHEAPER than published "
                        f"{table_rate} - verify before trusting it")

    # Prove nothing order-capable was pulled in on the way here.
    leaked = [name for mod in list(sys.modules.values()) if mod is not None
              for name in ORDER_SURFACE
              if getattr(mod, "__name__", "").startswith("lib.")
              and hasattr(mod, name)]
    if leaked:
        problems.append(f"an order-capable symbol is reachable: {sorted(set(leaked))}")

    print()
    if problems:
        for p in problems:
            print(f"  PROBLEM: {p}")
        return 1
    print("  ok - measured tier read and preferred; no order surface touched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
