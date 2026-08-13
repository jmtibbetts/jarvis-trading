"""Application version — single source of truth.

8.0.0 marks the evidence/expectancy refactor. The major version moves for
the same reason 7.0.0 did: these change how trades are SIZED and REJECTED,
not merely how they are displayed.

  - A NO_TRADE gate on measured NET expectancy. Setups whose costs exceed
    their edge are refused outright — a BTC 15m setup with a 0.08% stop
    carries 13R of round-trip cost and previously executed.
  - A strategy lifecycle gate judged OUT OF SAMPLE. SHADOW and DISABLED
    strategies size to zero; unmeasured ones trade at quarter size.
  - Contradiction and regime penalties subtract from the composite, so
    what clears the execution gates changed.
  - Trailing-stop room went from a square root to a fourth root of the
    horizon ratio, tightening the 1D loss cut from a clamped -16% to a
    derived -10.2%.

Also in this version, and behaviour-affecting even though not gates:
every LLM call routes through one layer that states FAST/AUTO/DEEP, the
timeframe ladder runs 1m through 1W, and equity daily bars are no longer
three months stale (Alpaca applies `limit` from the START of the window).
"""
VERSION = "8.0.0"
