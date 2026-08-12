"""Application version — single source of truth.

7.0.0 marks the profitability/risk refactor: direction-aware sizing,
the risk-at-stop invariant, NO_TRADE, transaction costs in R, contract
specifications for futures, and historical evidence counted exactly once.
These changed how trades are SIZED and REJECTED, not merely how they are
displayed, so the major version moves with them.
"""
VERSION = "7.0.0"
