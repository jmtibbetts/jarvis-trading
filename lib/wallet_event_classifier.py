"""What a watched wallet's transfers actually MEAN — or honestly, that we cannot tell.

A TRANSFER IS NOT A TRADE. That is the whole discipline here. The Helius
Wallet Transfers feed reports movements: a mint, a direction, an amount, a
counterparty and a signature. It reports no program, no instruction and no
swap route. So most single transfers cannot be shown to be trades, and this
module says so instead of guessing — a fabricated buy is worse than a
missing one, because the missing one does not enter a performance table.

WHAT CAN BE ESTABLISHED FROM THIS EVIDENCE, and how:

  PAIRED_SWAP_LEGS   one signature moving a QUOTE asset out and a token in
                     (or the reverse) for the same wallet IS a swap. Both
                     legs are already in the feed; pairing them needs no
                     extra provider call and no new client. This is the
                     same balance-delta reasoning `lib/wallet_swaps` uses,
                     applied to the legs already collected.
  COUNTERPARTY_ENTITY the registry knows which addresses are exchanges and
                     protocols, so a transfer to a known exchange is a
                     custody movement rather than a market action.
  ASSET_IDENTITY     a stablecoin leg with no paired token leg is treasury
                     movement, not a directional bet.
  SINGLE_LEG_ONLY    everything else. UNKNOWN_TRANSFER, and it stays there.

WHAT DELIBERATELY CANNOT BE ESTABLISHED. Staking, liquidity provision,
bridges and mint/burn all require program-level evidence this feed does not
carry. Where a counterparty is a KNOWN protocol the event is classified as
an interaction with that protocol and marked non-trading; where it is not,
the answer is UNKNOWN_TRANSFER with PARTIAL_EVIDENCE. `lib/wallet_swaps`
already implements a full balance-delta classifier against `getTransaction`,
and it has no production caller — turning it on is a provider-load decision,
not a classification one, and is out of scope here.

TOKEN-FOR-TOKEN SWAPS ARE NOT BUYS. Two non-quote legs is a real swap with
no USD anchor in this evidence, so it is PARTIAL_EVIDENCE rather than a
directional signal pointed at whichever leg happened to arrive.

A MINT IS THE IDENTITY. Tickers are not used for identity anywhere in this
module; `symbol` is display only, and the feed frequently reports it as null
for exactly the SPL tokens that matter most.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = "wallet_event_classifier_v1"

# ── Economic event taxonomy ──────────────────────────────────────────────
TOKEN_BUY = "TOKEN_BUY"
TOKEN_SELL = "TOKEN_SELL"
POSITION_INCREASE = "POSITION_INCREASE"
POSITION_REDUCTION = "POSITION_REDUCTION"
FULL_EXIT = "FULL_EXIT"
STABLECOIN_INFLOW = "STABLECOIN_INFLOW"
STABLECOIN_OUTFLOW = "STABLECOIN_OUTFLOW"
EXCHANGE_DEPOSIT = "EXCHANGE_DEPOSIT"
EXCHANGE_WITHDRAWAL = "EXCHANGE_WITHDRAWAL"
BRIDGE_TRANSFER = "BRIDGE_TRANSFER"
STAKING_DEPOSIT = "STAKING_DEPOSIT"
STAKING_WITHDRAWAL = "STAKING_WITHDRAWAL"
LIQUIDITY_ADD = "LIQUIDITY_ADD"
LIQUIDITY_REMOVE = "LIQUIDITY_REMOVE"
INTERNAL_TRANSFER = "INTERNAL_TRANSFER"
SELF_TRANSFER = "SELF_TRANSFER"
AIRDROP = "AIRDROP"
DUST_OR_SPAM = "DUST_OR_SPAM"
PROTOCOL_INTERACTION = "PROTOCOL_INTERACTION"
UNKNOWN_TRANSFER = "UNKNOWN_TRANSFER"

#: Only FULL-TRANSACTION evidence can establish these two. The transfers
#: feed reports no `err` and no net balance, so a failed swap and a
#: one-sided move are invisible to it — which is exactly how a failed
#: transaction used to be indistinguishable from a completed one.
FAILED_TRANSACTION = "FAILED_TRANSACTION"
NON_ECONOMIC_TRANSACTION = "NON_ECONOMIC_TRANSACTION"

EVENT_TYPES = (
    TOKEN_BUY, TOKEN_SELL, POSITION_INCREASE, POSITION_REDUCTION, FULL_EXIT,
    STABLECOIN_INFLOW, STABLECOIN_OUTFLOW, EXCHANGE_DEPOSIT,
    EXCHANGE_WITHDRAWAL, BRIDGE_TRANSFER, STAKING_DEPOSIT,
    STAKING_WITHDRAWAL, LIQUIDITY_ADD, LIQUIDITY_REMOVE, INTERNAL_TRANSFER,
    SELF_TRANSFER, AIRDROP, DUST_OR_SPAM, PROTOCOL_INTERACTION,
    UNKNOWN_TRANSFER,
)

#: The ONLY types that may become a directional market observation.
TRADING_EVENT_TYPES = frozenset({TOKEN_BUY, TOKEN_SELL})

#: Positively identified as something other than a market action.
NON_TRADING_EVENT_TYPES = frozenset({
    FAILED_TRANSACTION, NON_ECONOMIC_TRANSACTION,
    STABLECOIN_INFLOW, STABLECOIN_OUTFLOW, EXCHANGE_DEPOSIT,
    EXCHANGE_WITHDRAWAL, BRIDGE_TRANSFER, STAKING_DEPOSIT,
    STAKING_WITHDRAWAL, LIQUIDITY_ADD, LIQUIDITY_REMOVE, INTERNAL_TRANSFER,
    SELF_TRANSFER, AIRDROP, DUST_OR_SPAM, PROTOCOL_INTERACTION,
})

# ── Classification states ────────────────────────────────────────────────
CLASSIFIED_TRADING_EVENT = "CLASSIFIED_TRADING_EVENT"
CLASSIFIED_NON_TRADING_EVENT = "CLASSIFIED_NON_TRADING_EVENT"
PARTIAL_EVIDENCE = "PARTIAL_EVIDENCE"
UNKNOWN = "UNKNOWN"

CLASSIFICATION_STATES = (CLASSIFIED_TRADING_EVENT,
                         CLASSIFIED_NON_TRADING_EVENT, PARTIAL_EVIDENCE,
                         UNKNOWN)

# ── Evidence quality, best first ─────────────────────────────────────────
#: The transaction's own pre/post balances. Strictly stronger than the
#: transfer rows: it nets routing hops, sees the fee, and sees `err`.
BALANCE_DELTA_EVIDENCE = "BALANCE_DELTA_EVIDENCE"
PAIRED_SWAP_LEGS = "PAIRED_SWAP_LEGS"
COUNTERPARTY_ENTITY = "COUNTERPARTY_ENTITY"
ASSET_IDENTITY = "ASSET_IDENTITY"
SINGLE_LEG_ONLY = "SINGLE_LEG_ONLY"
NO_EVIDENCE = "NO_EVIDENCE"

EVIDENCE_RANK = (BALANCE_DELTA_EVIDENCE, PAIRED_SWAP_LEGS, COUNTERPARTY_ENTITY, ASSET_IDENTITY,
                 SINGLE_LEG_ONLY, NO_EVIDENCE)

# ── Historical compatibility (§19) ───────────────────────────────────────
ELIGIBLE_CURRENT_SCHEMA = "ELIGIBLE_CURRENT_SCHEMA"
LEGACY_PARTIAL = "LEGACY_PARTIAL"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

#: Below this a token leg is bookkeeping residue, not a position. Matches
#: the ledger's own dust floor rather than inventing a second one.
DUST_AMOUNT = 1e-9


#: HELIUS'S NATIVE-SOL MARKER, and it is NOT WSOL.
#
# The transfers feed reports native SOL under a 43-character pseudo-mint
# ending `...111`, one character from WSOL's `...112`. Measured across every
# stored row: 14,610 rows carry this mint and ALL 14,610 report
# `symbol == "SOL"`, while the 131 genuine WSOL rows report no symbol at all.
#
# This matters more than a naming quirk. Native SOL is 70% of every leg in
# this feed, so a quote list that omits it reads SOL as a TRADED TOKEN — and
# the classifier duly produced "TOKEN_BUY of SOL" from ordinary SOL-for-token
# swaps, inverting the direction of the most common event in the data.
# Verified by counting, not by recognising the prefix.
NATIVE_SOL_PSEUDO_MINT = "So11111111111111111111111111111111111111111"


def _quote_mints() -> dict:
    """Mint -> quote symbol. THE conversion point, borrowed not rebuilt."""
    from lib.token_pricing import STABLECOIN_MINTS
    from lib.wallet_swaps import NATIVE_SOL, WSOL_MINT

    out = {WSOL_MINT: "SOL", NATIVE_SOL: "SOL",
           NATIVE_SOL_PSEUDO_MINT: "SOL"}
    out.update(STABLECOIN_MINTS)
    return out


def is_quote_asset(mint: str | None) -> bool:
    """Whether this mint is something a position is priced IN."""
    return bool(mint) and mint in _quote_mints()


def is_stablecoin_mint(mint: str | None) -> bool:
    from lib.token_pricing import STABLECOIN_MINTS

    return bool(mint) and mint in STABLECOIN_MINTS


@dataclass
class TransferLeg:
    """One stored Helius transfer row, as evidence."""

    signature: str
    mint: str | None
    direction: str                 # "in" | "out", RELATIVE to the wallet
    amount: float
    counterparty: str | None = None
    watched_wallet: str | None = None
    symbol: str | None = None      # DISPLAY ONLY. Never identity.
    block_time: float | None = None
    observed_ts: float | None = None
    parser_version: str | None = None
    source: str = "helius"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClassifiedEvent:
    """One economic event, with the reason it was called that."""

    signature: str
    event_type: str
    classification: str
    evidence_quality: str
    reason: str

    #: The asset the event is ABOUT — always a mint, never a ticker.
    subject_mint: str | None = None
    subject_symbol: str | None = None        # display only
    direction: str | None = None             # BUY | SELL | None
    subject_amount: float | None = None

    quote_mint: str | None = None
    quote_symbol: str | None = None
    quote_amount: float | None = None

    watched_wallet: str | None = None
    counterparty: str | None = None
    counterparty_entity: str | None = None
    block_time: float | None = None
    observed_ts: float | None = None
    chain: str = "solana"

    schema_compatibility: str = ELIGIBLE_CURRENT_SCHEMA
    leg_count: int = 1
    legs: list = field(default_factory=list)
    classifier_version: str = CLASSIFIER_VERSION
    parser_version: str | None = None

    @property
    def is_trading_event(self) -> bool:
        return (self.classification == CLASSIFIED_TRADING_EVENT
                and self.event_type in TRADING_EVENT_TYPES)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["is_trading_event"] = self.is_trading_event
        return d


def group_by_signature(legs: list) -> dict:
    """ONE SIGNATURE IS ONE ECONOMIC EVENT.

    A swap arrives as several transfer rows. Counting them separately would
    turn one market action into several observations, which is the sample
    inflation every counting rule in this system exists to prevent.
    """
    groups: dict = {}
    for leg in legs:
        groups.setdefault(leg.signature, []).append(leg)
    return groups


def _entity_of(address: str | None, entity_lookup) -> dict:
    if not address or entity_lookup is None:
        return {}
    try:
        return entity_lookup(address) or {}
    except Exception:                                        # noqa: BLE001
        return {}


def _swap_evidence_of(signature, enrichment_lookup) -> dict:
    if not signature or enrichment_lookup is None:
        return {}
    try:
        return enrichment_lookup(signature) or {}
    except Exception:                                        # noqa: BLE001
        return {}


def canonical_subject_mint(mint: str | None) -> str | None:
    """The swap ledger's SOL sentinel, back to a real mint identity.

    `lib/wallet_swaps` canonicalises WSOL onto the literal string "SOL" so
    that wrapping and unwrapping cancel — correct for computing a wallet's
    net economics, and NOT a mint. Letting that string reach `subject_mint`
    would put a ticker in a mint column, which is the exact confusion the
    mint-only identity rule exists to prevent.

    The merge is not reversible: once WSOL has been folded onto native SOL
    the two are one number. So this resolves to the NATIVE SOL pseudo-mint
    and the caller says so in its reason. Native SOL and WSOL remain
    separate identities everywhere the transfer feed is the evidence.
    """
    from lib.wallet_swaps import NATIVE_SOL

    if mint == NATIVE_SOL:
        return NATIVE_SOL_PSEUDO_MINT
    return mint


def _from_swap_evidence(ev: dict, _out, legs: list):
    """Full-transaction evidence, which OVERRIDES the transfer reading.

    Returns None when the evidence says nothing usable, so the caller falls
    back to the transfer legs rather than losing a classification it could
    already make.
    """
    from lib import wallet_swaps as S

    state = ev.get("state")
    kind = ev.get("kind")
    reason = ev.get("reason") or ""
    n = len(legs)

    if state == "REFUSED_NON_TRADING":
        if ev.get("tx_success") is False:
            # A FAILED TRANSACTION IS NOT A TRADE. The transfers feed
            # reports the attempted movements and no error, so without this
            # the wallet appears to have bought something it never received.
            return _out(FAILED_TRANSACTION, CLASSIFIED_NON_TRADING_EVENT,
                        BALANCE_DELTA_EVIDENCE,
                        "the transaction FAILED on chain, so no value "
                        "changed hands — the transfer rows describe an "
                        "attempt, not an execution")
        lowered = reason.lower()
        if "lp deposit" in lowered:
            etype = LIQUIDITY_ADD
        elif "lp withdrawal" in lowered:
            etype = LIQUIDITY_REMOVE
        elif "quote-to-quote" in lowered:
            etype = INTERNAL_TRANSFER
        else:
            etype = NON_ECONOMIC_TRANSACTION
        return _out(etype, CLASSIFIED_NON_TRADING_EVENT,
                    BALANCE_DELTA_EVIDENCE,
                    f"net balance deltas over the whole transaction show "
                    f"this is not a swap: {reason}")

    base = canonical_subject_mint(ev.get("base_mint"))
    quote = canonical_subject_mint(ev.get("quote_mint"))
    merged = (ev.get("base_mint") == S.NATIVE_SOL
              or ev.get("quote_mint") == S.NATIVE_SOL)
    note = (" (the ledger folds WSOL onto native SOL so wrapping cancels; "
            "the SOL leg is reported under the native pseudo-mint)"
            if merged else "")

    if state == "PARTIAL":
        return _out(UNKNOWN_TRANSFER, PARTIAL_EVIDENCE,
                    BALANCE_DELTA_EVIDENCE,
                    f"balance deltas establish a real swap that cannot be "
                    f"valued: {reason}. Direction without a priceable leg "
                    f"is not a position{note}",
                    subject_mint=base, subject_amount=ev.get("base_amount"),
                    quote_mint=quote, quote_amount=ev.get("quote_amount"))

    if state == "ENRICHED" and kind in (S.BUY, S.SELL):
        if not base:
            return None
        side = TOKEN_BUY if kind == S.BUY else TOKEN_SELL
        return _out(side, CLASSIFIED_TRADING_EVENT, BALANCE_DELTA_EVIDENCE,
                    f"the transaction's own pre/post balances net to one "
                    f"asset in and one out across {n} transfer leg(s): "
                    f"{reason}{note}",
                    subject_mint=base,
                    subject_symbol=_quote_mints().get(base),
                    direction="BUY" if kind == S.BUY else "SELL",
                    subject_amount=ev.get("base_amount"),
                    quote_mint=quote,
                    quote_symbol=_quote_mints().get(quote),
                    quote_amount=ev.get("quote_amount"))

    return None


def classify_group(legs: list, *, entity_lookup=None,
                   enrichment_lookup=None) -> ClassifiedEvent:
    """Classify one signature's legs. DETERMINISTIC — no model, no market.

    `entity_lookup(address) -> {"entity_type", "entity_name", "is_protocol",
    "is_trader"}` is the registry, injected so this stays pure and testable.

    `enrichment_lookup(signature) -> the swap verdict` is FULL-TRANSACTION
    evidence from `lib/wallet_swap_enrichment`, injected the same way. When
    it has an answer it WINS, because it is strictly better evidence: the
    chain's own pre/post balances see the fee, net out routing hops, and
    see whether the transaction actually succeeded — none of which appears
    in a transfer row. When it has nothing, the transfer reading below
    stands unchanged.
    """
    legs = sorted(legs, key=lambda x: (x.direction, str(x.mint or "")))
    sig = legs[0].signature
    wallet = next((l.watched_wallet for l in legs if l.watched_wallet), None)
    compat = (ELIGIBLE_CURRENT_SCHEMA if wallet else LEGACY_PARTIAL)
    block_time = next((l.block_time for l in legs if l.block_time), None)
    observed = next((l.observed_ts for l in legs if l.observed_ts), None)
    parser = next((l.parser_version for l in legs if l.parser_version), None)

    def _out(event_type, classification, quality, reason, **kw):
        return ClassifiedEvent(
            signature=sig, event_type=event_type,
            classification=classification, evidence_quality=quality,
            reason=reason, watched_wallet=wallet, block_time=block_time,
            observed_ts=observed, schema_compatibility=compat,
            leg_count=len(legs), legs=[l.as_dict() for l in legs],
            parser_version=parser, **kw)

    ev = _swap_evidence_of(sig, enrichment_lookup)
    if ev:
        upgraded = _from_swap_evidence(ev, _out, legs)
        if upgraded is not None:
            return upgraded

    ins = [l for l in legs if l.direction == "in"]
    outs = [l for l in legs if l.direction == "out"]

    # ── SELF-TRANSFER. Nothing left the wallet's control. ────────────────
    #
    # THE EMPTY-SEQUENCE TRAP: `all(... for l in legs if l.counterparty)` is
    # vacuously TRUE when no leg has a counterparty, so every group with an
    # unknown counterparty was being called a self-transfer — the most
    # confident possible reading of the least evidence. At least one
    # counterparty must actually be present before this claim is available.
    counterparties = [l.counterparty for l in legs if l.counterparty]
    if wallet and counterparties and all(c == wallet for c in counterparties):
        return _out(SELF_TRANSFER, CLASSIFIED_NON_TRADING_EVENT,
                    COUNTERPARTY_ENTITY,
                    "every observed counterparty is the watched wallet itself",
                    counterparty=wallet)

    # ── PAIRED SWAP LEGS: the one thing this feed can prove. ─────────────
    #
    # THE LARGEST LEG, NOT THE FIRST. A routed swap arrives as seven or
    # eight legs — hops, fee splits and wrapping bookkeeping — so picking
    # `ins[0]` picks whichever mint sorts first, which is arbitrary. It also
    # skewed the result: 1,879 buys against 10 sells on the same data, which
    # is not a market, it is an artefact of alphabetical order. The economic
    # subject is the biggest non-quote leg; the consideration is the biggest
    # quote leg on the other side.
    if ins and outs:
        def _largest(rows, quote: bool):
            pool = [l for l in rows
                    if is_quote_asset(l.mint) is quote
                    and l.amount > DUST_AMOUNT]
            return max(pool, key=lambda l: l.amount) if pool else None

        token_in, quote_out = _largest(ins, False), _largest(outs, True)
        token_out, quote_in = _largest(outs, False), _largest(ins, True)
        buy_ok = token_in is not None and quote_out is not None
        sell_ok = token_out is not None and quote_in is not None

        if buy_ok and sell_ok:
            # Token out AND token in against quote on both sides: a route
            # through this wallet, not a directional position it took.
            return _out(UNKNOWN_TRANSFER, PARTIAL_EVIDENCE, PAIRED_SWAP_LEGS,
                        "quote assets moved in BOTH directions against "
                        "different tokens — a routing hop, and naming either "
                        "token the subject would invent a position",
                        subject_mint=token_in.mint,
                        subject_symbol=token_in.symbol,
                        subject_amount=token_in.amount)
        if buy_ok or sell_ok:
            token_leg = token_in if buy_ok else token_out
            quote_leg = quote_out if buy_ok else quote_in
            side = TOKEN_BUY if buy_ok else TOKEN_SELL
            q = _quote_mints().get(quote_leg.mint)
            return _out(
                side, CLASSIFIED_TRADING_EVENT, PAIRED_SWAP_LEGS,
                (f"one signature moved {q} "
                 f"{'out' if buy_ok else 'in'} against the token — a swap, "
                 f"established from the legs already collected "
                 f"({len(legs)} legs, largest of each side taken)"),
                subject_mint=token_leg.mint,
                subject_symbol=token_leg.symbol,
                direction="BUY" if buy_ok else "SELL",
                subject_amount=token_leg.amount,
                quote_mint=quote_leg.mint, quote_symbol=q,
                quote_amount=quote_leg.amount,
                counterparty=token_leg.counterparty)

        if all(is_quote_asset(l.mint) for l in legs):
            return _out(INTERNAL_TRANSFER, CLASSIFIED_NON_TRADING_EVENT,
                        ASSET_IDENTITY,
                        "every leg is a quote asset — treasury movement, "
                        "not a directional position")
        # TOKEN-FOR-TOKEN. A real swap with no USD anchor in this evidence.
        return _out(UNKNOWN_TRANSFER, PARTIAL_EVIDENCE, PAIRED_SWAP_LEGS,
                    "two non-quote legs: a token-for-token swap with no "
                    "priceable side, so no direction can be claimed without "
                    "inventing one",
                    subject_mint=ins[0].mint, subject_symbol=ins[0].symbol,
                    subject_amount=ins[0].amount)

    # ── SINGLE-SIDED. Identity and counterparty are all there is. ────────
    #
    # "Single-sided" does not mean "one leg". 904 signatures in the stored
    # data move a quote asset OUT and a token OUT with nothing coming back:
    # the wallet paid gas and sent a token away, and no consideration was
    # observed. That is not a sale, and taking `legs[0]` would have picked
    # whichever mint sorted first and then described the transaction from
    # the wrong asset's point of view.
    #
    # The economic subject is the largest NON-QUOTE leg where one exists —
    # the token is what the event is about; the SOL beside it is usually the
    # fee. Falling back to the largest leg overall keeps pure quote-asset
    # movements answerable.
    _tokens = [l for l in legs if not is_quote_asset(l.mint)]
    leg = max(_tokens or legs, key=lambda l: l.amount)
    if len(legs) > 1 and _tokens and not (ins and outs):
        direction_word = "left" if leg.direction == "out" else "reached"
        return _out(UNKNOWN_TRANSFER, PARTIAL_EVIDENCE, SINGLE_LEG_ONLY,
                    f"{len(legs)} legs all moving the same way: the token "
                    f"{direction_word} the wallet and NO consideration was "
                    f"observed in the same transaction, so nothing "
                    f"establishes a purchase or a sale",
                    subject_mint=leg.mint, subject_symbol=leg.symbol,
                    subject_amount=leg.amount,
                    counterparty=leg.counterparty)
    ent = _entity_of(leg.counterparty, entity_lookup)
    etype = str(ent.get("entity_type") or "").upper()
    ename = ent.get("entity_name")

    if leg.amount <= DUST_AMOUNT:
        return _out(DUST_OR_SPAM, CLASSIFIED_NON_TRADING_EVENT,
                    ASSET_IDENTITY,
                    f"amount {leg.amount!r} is bookkeeping residue, not a "
                    f"position", subject_mint=leg.mint,
                    subject_symbol=leg.symbol, subject_amount=leg.amount,
                    counterparty=leg.counterparty, counterparty_entity=etype)

    if etype:
        mapping = {
            "EXCHANGE": (EXCHANGE_WITHDRAWAL if leg.direction == "in"
                         else EXCHANGE_DEPOSIT),
            "CEX": (EXCHANGE_WITHDRAWAL if leg.direction == "in"
                    else EXCHANGE_DEPOSIT),
            "BRIDGE": BRIDGE_TRANSFER,
            "STAKING": (STAKING_WITHDRAWAL if leg.direction == "in"
                        else STAKING_DEPOSIT),
            "AMM": (LIQUIDITY_REMOVE if leg.direction == "in"
                    else LIQUIDITY_ADD),
            "DEX": (LIQUIDITY_REMOVE if leg.direction == "in"
                    else LIQUIDITY_ADD),
        }
        found = mapping.get(etype)
        if found:
            return _out(found, CLASSIFIED_NON_TRADING_EVENT,
                        COUNTERPARTY_ENTITY,
                        f"counterparty is a known {etype.lower()}"
                        + (f" ({ename})" if ename else ""),
                        subject_mint=leg.mint, subject_symbol=leg.symbol,
                        subject_amount=leg.amount,
                        counterparty=leg.counterparty,
                        counterparty_entity=etype)
        if ent.get("is_protocol"):
            return _out(PROTOCOL_INTERACTION, CLASSIFIED_NON_TRADING_EVENT,
                        COUNTERPARTY_ENTITY,
                        f"counterparty is a known protocol"
                        + (f" ({ename})" if ename else "")
                        + " — the specific action needs program evidence "
                          "this feed does not carry",
                        subject_mint=leg.mint, subject_symbol=leg.symbol,
                        subject_amount=leg.amount,
                        counterparty=leg.counterparty,
                        counterparty_entity=etype)

    if is_stablecoin_mint(leg.mint):
        return _out(STABLECOIN_INFLOW if leg.direction == "in"
                    else STABLECOIN_OUTFLOW,
                    CLASSIFIED_NON_TRADING_EVENT, ASSET_IDENTITY,
                    "a stablecoin leg with no paired token leg is treasury "
                    "movement, not a directional bet",
                    subject_mint=leg.mint, subject_symbol=leg.symbol,
                    subject_amount=leg.amount,
                    counterparty=leg.counterparty, counterparty_entity=etype)

    # ── The honest default. ──────────────────────────────────────────────
    return _out(UNKNOWN_TRANSFER, UNKNOWN, SINGLE_LEG_ONLY,
                "a single token leg with an unknown counterparty. This feed "
                "reports no program or instruction, so nothing distinguishes "
                "a purchase from a deposit, an airdrop or a routing hop — "
                "and guessing would put a fabricated trade in the record",
                subject_mint=leg.mint, subject_symbol=leg.symbol,
                subject_amount=leg.amount, counterparty=leg.counterparty,
                counterparty_entity=etype)


def classify_all(legs: list, *, entity_lookup=None,
                 enrichment_lookup=None) -> list:
    """Every signature in `legs`, classified once."""
    return [classify_group(g, entity_lookup=entity_lookup,
                           enrichment_lookup=enrichment_lookup)
            for g in group_by_signature(legs).values()]


def summarise(events: list) -> dict:
    """Counts by state and type — the shape the desk renders."""
    by_state: dict = {}
    by_type: dict = {}
    for e in events:
        by_state[e.classification] = by_state.get(e.classification, 0) + 1
        by_type[e.event_type] = by_type.get(e.event_type, 0) + 1
    return {
        "events": len(events),
        "trading_events": sum(1 for e in events if e.is_trading_event),
        "by_classification": by_state,
        "by_event_type": by_type,
        "classifier_version": CLASSIFIER_VERSION,
    }
