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
| **LunarCrush** | social/sentiment (v4) | **PAYMENT_REQUIRED** | now implemented; every endpoint returns HTTP 402 *"You must have an active Individual or higher subscription"*. Credential is valid — headers decrement 100→95 |
| **Stocklake** | — | via `lib/mcp_client` only | no direct module |
| **Alpaca** | broker | **MUTATING_FORBIDDEN** | `execute`/`positions`/`guardian` behind an opt-in that is OFF; not registered |
| **Kraken** (private) | account/orders | **MUTATING_FORBIDDEN** | `lib/kraken_account`; no collection caller |
| **Telegram** | operator delivery | configured | bot credential set |

## Two findings worth acting on

**LunarCrush: the consumer is now built; the subscription is not active.**
It previously had zero code references anywhere. It now has a v4 client, an
observations table, an hourly COLLECTION job and health telemetry — and it
collects nothing, because every endpoint answers:

    HTTP 402  {"error": "You must have an active Individual or higher
                         subscription to use this endpoint."}

The credential is **valid**: the service recognises it and returns
rate-limit headers that decrement on each call (100/day, 4/min; 95 left
after probing). This is neither an auth failure nor an outage, and no retry
or backoff can change it — **only the account owner can**. Recorded as
`PAYMENT_REQUIRED` so it never reads as a bug to chase. The moment the plan
is active, collection starts with no further work.

**ACTION REQUIRED (external):** activate an Individual-or-higher LunarCrush
plan, or drop the subscription.

**Massive is rate-limited, but not by a polling loop.** Its aggregates
endpoint returned 429 to a manual probe. Re-measured against the running
service: Massive is called **zero** times on a schedule — its only callers
are `lib/signal_verification` and two on-demand `intel` routes. It already
has a 5-req/min budget and a 300 s cache. So the 429 reflects an exhausted
plan allowance rather than a runaway caller, and the correct fix is quota
awareness and 429 state (now available through `provider_health`) rather
than more retries. Left as measured; not yet re-verified after a quota
reset.

## Health telemetry gap

**Closed.** `intelligence_source_health` still covers only the 43 RSS feeds,
but `provider_health` now exists alongside it — keyed by (provider,
capability), with distinct statuses, quota, freshness and sanitised errors,
exposed at `GET /api/providers/health`. LunarCrush is the first live
citizen, reporting `PAYMENT_REQUIRED` with 95 daily requests remaining.

Remaining work: the other providers still record health only when something
calls them, so the table fills in as each capability is exercised. A
periodic capability heartbeat would make the surface complete.

## Mutation safety

Read-only and mutating capabilities are separated per capability, not per
provider. Alpaca and Kraken both expose account/order endpoints; both are
unreachable from the current runtime — the broker jobs sit behind an opt-in
that is off and are never registered, and no collection job imports the
account clients. Verified in the running service: 31 jobs registered, and
`paper_trading`, `auto_simulator`, `dex_autotrade` withheld as ECONOMIC.
