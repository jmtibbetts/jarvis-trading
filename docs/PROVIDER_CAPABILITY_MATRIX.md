# Provider capability matrix

Every provider discovered from `.env`, `platform_configs`, provider modules
and scheduler jobs — then **probed read-only against the live service**, not
inferred from configuration. No credential value appears here or in the
probe output.

Probed 2026-08-19 ~04:37 UTC from the canonical WSL tree.

## Status

| provider | capability | status | proof |
|---|---|---|---|
| **Bitnomial** | perp order books (public WS) | **WORKING** | `PBTCUCZ50` `state=OK`, age 0.05s; PETH/PSOL/PLTC/PBCH/PLNK books observed |
| **Kraken** (public) | spot ticker/REST | **WORKING** | `XXBTZUSD` ask 64300.00 / bid 64299.90 |
| **TwelveData** | equity/FX bars, quotes | **WORKING** | 3 bars for AAPL; **1,000,000,000 credits remaining** |
| **FRED** | macro series | **WORKING** | `DGS10` → 2026-08-17 = 4.72 |
| **EIA** | energy series | **WORKING** | `sync_eia` fetched 8 |
| **AllRates** | FX rates | **WORKING** | USD base, dated 2026-08-18; quota configured |
| **CoinGecko** (direct) | crypto prices | **WORKING** | BTC/USD 64350 |
| **CoinGecko** (MCP) | crypto snapshot | **WORKING** | bitcoin 64316 |
| **Helius** | Solana RPC + wallet API | **WORKING** | RPC ok 107 ms, wallet_api ok |
| **OpenFIGI** | instrument identity | **WORKING** | HTTP 200, AAPL → `BBG000B9XRY4` |
| **Tavily / MCP** | web research | **WORKING** | tool list returned |
| **LM Studio** | local inference | **WORKING** | discovery fell through `127.0.0.1` → WSL gateway `172.31.48.1`; generating with `google/gemma-4-26b-a4b-qat` |
| **Massive** | equity aggregates | **RATE_LIMITED** | HTTP 429, "too many 429 error responses" |
| **LunarCrush** | — | **CONFIGURED_NO_IMPLEMENTATION** | credential present, **zero code references anywhere** |
| **Stocklake** | — | via `lib/mcp_client` only | no direct module |
| **Alpaca** | broker | **MUTATING_FORBIDDEN** | `execute`/`positions`/`guardian` behind an opt-in that is OFF; not registered |
| **Kraken** (private) | account/orders | **MUTATING_FORBIDDEN** | `lib/kraken_account`; no collection caller |
| **Telegram** | operator delivery | configured | bot credential set |

## Two findings worth acting on

**LunarCrush is paid and has never been implemented.** The credential exists
in `.env`; a repository-wide search finds no reference to it in any module,
job, test or document. It is not "broken" and not "idle" — nothing was ever
written to call it. Either build a consumer or drop the subscription.

**Massive is rate-limited.** Its aggregates endpoint returns 429 under the
current call pattern. That is a real utilisation problem, not an outage:
capacity is being spent faster than the plan allows, so the data is missing
precisely when a job wants it.

## Health telemetry gap

`intelligence_source_health` tracks **only the 43 RSS news feeds** (42
healthy; "Micron Newsroom" returns 403). None of the paid API providers has
a health record, so nothing in the running system can answer "is TwelveData
authenticating?" or "is Massive rate-limited?" without a manual probe like
this one. The statuses above were obtained by hand.

## Mutation safety

Read-only and mutating capabilities are separated per capability, not per
provider. Alpaca and Kraken both expose account/order endpoints; both are
unreachable from the current runtime — the broker jobs sit behind an opt-in
that is off and are never registered, and no collection job imports the
account clients. Verified in the running service: 31 jobs registered, and
`paper_trading`, `auto_simulator`, `dex_autotrade` withheld as ECONOMIC.
