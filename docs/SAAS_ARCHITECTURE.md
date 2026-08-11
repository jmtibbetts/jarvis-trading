# Jarvis SaaS Architecture

## Objective

Turn the current single-user desktop service into a hosted subscription product where each customer can:

- Sign in to an isolated account.
- Connect one or more broker or exchange accounts.
- View their own balances, positions, orders, and performance.
- Receive plan-appropriate signals and alerts.
- Approve or reject eligible trades from the web, Telegram, Discord, or WhatsApp.
- Manage subscription, payment method, invoices, and upgrades.

The global market-data and signal-generation pipeline should remain shared. Credentials, portfolios, approvals, orders, risk limits, notifications, and billing state must be user-owned.

## Current Production Blockers

Do not expose the current application directly to the internet.

1. `PlatformConfig` stores credentials as plaintext strings.
2. `/api/settings` returns full API keys and secrets to the browser.
3. SQLite, in-process APScheduler jobs, positions, and Telegram configuration are global.
4. Routes have no authenticated user or tenant boundary.
5. A second web process could run the same scheduled jobs and duplicate alerts or trades.
6. Signal approval endpoints have no user ownership, idempotency key, or audit record.

These are normal constraints for a local application, but they must be replaced before accepting customer credentials or money.

## Target Shape

```text
Browser / Telegram / Discord / WhatsApp
                    |
              HTTPS webhooks
                    |
       FastAPI API + authentication
          |         |          |
     PostgreSQL   Redis      Stripe
          |       queue      webhooks
          |         |
          |    worker processes
          |     |     |      |
          |  market portfolio execution notifications
          |   data     sync      |
          +-----------------------+
                       |
             broker/exchange APIs
```

Run the API and background workers as separate deployable processes. Use one scheduler leader or a managed scheduler to enqueue jobs; workers consume idempotent jobs from Redis. Do not execute scheduled trading work inside every web process.

## Data Ownership

Move from SQLite to PostgreSQL and use Alembic migrations. Every customer-owned row must include `user_id` or `organization_id`, with indexed foreign keys and authorization checks in the query layer.

Recommended tables:

| Table | Purpose |
| --- | --- |
| `users` | Login identity, status, locale, timezone, MFA state |
| `organizations` | Optional workspace boundary for future teams |
| `memberships` | User roles within an organization |
| `subscriptions` | Stripe customer/subscription IDs and normalized status |
| `plan_entitlements` | Feature limits resolved from the active plan |
| `exchange_credentials` | Encrypted credential blob, provider, scopes, key version, verification state |
| `broker_accounts` | User-visible account metadata; never raw secrets |
| `portfolio_snapshots` | Account value and buying power history per broker account |
| `positions` | Current user positions per broker account |
| `orders` | Local order state and remote broker order IDs |
| `signals` | Shared global market signals and analysis |
| `user_signal_actions` | Per-user pending, approved, rejected, expired, or executed state |
| `risk_policies` | Per-account loss cap, sizing, leverage, asset and trading-hour limits |
| `notification_channels` | Linked Telegram, Discord, or WhatsApp identity and verification state |
| `notification_deliveries` | Delivery status and remote message IDs |
| `webhook_events` | Deduplication and processing state for external events |
| `audit_events` | Append-only security, approval, credential, billing, and execution history |

Keep `signals` global so analysis is calculated once. Never put a shared mutable status such as `Approved` on the global signal. Each subscriber gets a `user_signal_actions` row.

## Credential Security

- Encrypt each credential with envelope encryption backed by a managed KMS. Store ciphertext, key version, and non-secret metadata only.
- Decrypt only inside the portfolio or execution worker that needs the credential.
- Never return an existing secret to the browser. Return a mask such as `****8K2D`, scopes, and last verification time.
- Require API keys with withdrawals disabled. Encourage exchange IP allowlists where available.
- Separate read-only portfolio credentials from trade-enabled credentials when a provider permits it.
- Scrub secrets from logs, exceptions, tracing, support exports, and analytics.
- Record credential creation, verification, rotation, and deletion in `audit_events`.
- Add rate limits, MFA for enabling live execution, session revocation, and encrypted backups.

## Authentication And Authorization

Use a mature OIDC provider rather than building password storage first. FastAPI should validate signed access tokens and resolve an `AuthContext` containing `user_id`, `organization_id`, role, and entitlements.

Every owned route must filter by that context. A resource ID alone must never grant access. Administrative support access should be time-limited, explicit, and audited.

## Billing And Entitlements

Use Stripe Checkout for purchase and Stripe Customer Portal for payment methods, invoices, cancellation, and plan changes. Treat Stripe webhooks as the source of subscription state because billing changes happen asynchronously.

Suggested product boundaries:

| Plan | Capabilities |
| --- | --- |
| Starter | Dashboard, delayed signals, watchlists, one alert channel |
| Pro | Real-time and scalp signals, expanded TA, portfolio sync, multiple channels |
| Approval | Approve/reject workflow, configurable risk policy, paper execution |
| Live Execution add-on | Trade-enabled credentials, live orders, stronger onboarding and controls |

Resolve features on the server through `plan_entitlements`; never trust a plan name or feature flag sent by the frontend. Deduplicate Stripe events by event ID and make upgrades/downgrades idempotent.

References: [Stripe subscription webhooks](https://docs.stripe.com/billing/subscriptions/webhooks), [Stripe Customer Portal](https://docs.stripe.com/customer-management).

## Multi-Channel Approval Flow

1. The signal engine creates one immutable global signal with an expiration time.
2. A fan-out job creates `user_signal_actions` only for eligible subscribers and accounts.
3. The notification service sends an alert with Approve, Reject, and Details actions.
4. Buttons carry an opaque, short-lived action token, not a user ID, account ID, or API key.
5. The channel webhook verifies the provider signature and maps the external sender to a verified `notification_channels` row.
6. The API atomically changes one action from `Pending` to `Approved` or `Rejected` using an idempotency key.
7. Approval rechecks subscription entitlement, signal freshness, market price drift, account state, buying power, and risk policy.
8. An eligible approval enqueues an execution command. The worker recalculates quantity, stop, and target against current prices before placing an order.
9. The result is written to orders and audit history, then sent back to every linked channel.

An approval must apply to one named account. If a user has multiple accounts, the alert should either show the selected default or ask them to choose before approval.

### Channel Adapters

Expose one internal adapter contract:

```python
class AlertChannel:
    def send_signal(self, action, signal): ...
    def update_signal(self, delivery, state): ...
    def verify_webhook(self, request): ...
    def parse_action(self, request): ...
```

- Telegram: inline keyboard buttons and callback queries. Replace polling with a signed webhook in production.
- Discord: message components and interactions. Verify Discord request signatures and acknowledge interactions promptly.
- WhatsApp: Cloud API interactive reply buttons, verified webhooks, approved templates where required, and phone-number opt-in tracking.

References: [Telegram Bot API](https://core.telegram.org/bots/api), [Discord interactions](https://docs.discord.com/developers/interactions/receiving-and-responding), [WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api/overview).

## Market Data Strategy

Use exchange OHLCV first because the pair and venue are explicit. The current provider order is:

1. Binance
2. OKX
3. Bybit
4. Coinbase
5. Kraken
6. KuCoin
7. MEXC
8. CryptoCompare, then CoinGecko when explicitly enabled

Preserve the actual quote and venue internally. `BANK/USDT` on MEXC is not interchangeable with a different `BANK` token or an illiquid pair on another exchange. Save `provider`, `provider_symbol`, quote, timestamp, and data-quality flags with cached bars. Reject stale, crossed, zero-volume, or implausibly divergent data instead of silently averaging it.

Provider references: [Bybit klines](https://bybit-exchange.github.io/docs/v5/market/kline), [KuCoin klines](https://www.kucoin.com/docs-new/3473244e0), [MEXC market data](https://mexcdevelop.github.io/apidocs/spot_v3_en/).

## Delivery Phases

### Phase 0: Security Baseline

- Stop returning saved secrets from settings APIs.
- Introduce secret encryption and masked credential responses.
- Add structured secret redaction and audit events.
- Add migration tooling and automated tests.

### Phase 1: Accounts And Read-Only Portfolios

- PostgreSQL, authentication, user ownership, and account pages.
- Read-only exchange connections and portfolio sync.
- Keep live execution disabled; support paper trading per user.

### Phase 2: Billing

- Stripe Checkout, Customer Portal, webhook processing, plans, and entitlements.
- Subscription lifecycle tests for trial, payment failure, cancellation, and upgrade.

### Phase 3: Alerts

- Notification service and Telegram adapter first.
- Add Discord, then WhatsApp after webhook and identity-linking tests pass.
- Details and reject actions can ship before trade approval.

### Phase 4: Approval And Paper Execution

- Per-user action records, expiring signed tokens, idempotency, and audit trail.
- Paper execution only until replay, concurrency, price-drift, and failure tests pass.

### Phase 5: Live Execution

- Explicit live-trading activation, MFA, account-specific limits, emergency kill switch, and operational monitoring.
- Add provider-specific order reconciliation and recovery from ambiguous API timeouts.

## Regulatory Gate

Selling signals and especially offering automated or subscriber-approved execution can create investment-adviser, commodity trading adviser, broker, marketing, privacy, and money-transmission questions depending on instruments, product behavior, and customer location. Approval-per-trade is a useful control, but it is not itself a legal exemption.

Before charging for signals or enabling customer execution, have qualified securities and commodities counsel review the exact product, target jurisdictions, disclosures, performance claims, recordkeeping, and registrations. Relevant starting points include the [SEC auto-trading notice](https://www.sec.gov/about/reports-publications/investorpubsautotradinghtm) and the [CFTC CTA overview](https://www.cftc.gov/IndustryOversight/Intermediaries/CTAs/index.htm).

## Recommended First Build Slice

Build authentication, PostgreSQL tenant ownership, encrypted read-only credentials, and per-user portfolio pages first. Follow with Stripe entitlements and outbound alerts. Add approve/reject channel actions against paper accounts only after identity linking and audit records exist.

That sequence produces a sellable read-only product while keeping live customer funds outside the first migration's risk surface.
