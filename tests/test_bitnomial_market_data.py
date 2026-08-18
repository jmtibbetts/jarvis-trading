"""A10.1. US perpetuals are priced from the exchange they actually list on.

A10 proved that `lib/kraken_stream` is the SPOT WebSocket and correctly
stopped it pricing a CRYPTO_PERP. That left perpetuals fail-closed, which
was right, and was not the end state: US perpetuals list on BITNOMIAL, and
Bitnomial publishes a real book over a public, unauthenticated WebSocket.

Every protocol fact asserted here was captured from the live service on
2026-08-17 into tests/fixtures/bitnomial_ws_capture.json — the message
shapes, the ack batching, the `quantity: 0` removal, the level ordering.
None of it is inferred from documentation prose.
"""
import json
import pathlib
import unittest

from lib import bitnomial_market_data as MD
from lib import bitnomial_products as BP

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures"
     / "bitnomial_ws_capture.json").read_text(encoding="utf-8"))

SYM = "PBTCUCZ50"


def _messages(kind):
    return [m for m in FIXTURE["messages"] if m.get("type") == kind]


def _snapshot():
    return json.loads(json.dumps(_messages("book")[0]))


class TheCapturedProtocolIsWhatWeCodedAgainstTests(unittest.TestCase):
    """If the fixture ever stops looking like this, the adapter is decoding
    a protocol the exchange no longer speaks."""

    def test_the_book_snapshot_has_the_documented_shape(self):
        snap = _snapshot()
        for field in ("ack_id", "bids", "asks", "symbol", "timestamp"):
            self.assertIn(field, snap)

    def test_asks_ascend_and_bids_descend_so_index_zero_is_best(self):
        snap = _snapshot()
        asks = [p for p, _ in snap["asks"]]
        bids = [p for p, _ in snap["bids"]]
        self.assertEqual(asks, sorted(asks))
        self.assertEqual(bids, sorted(bids, reverse=True))

    def test_a_level_update_carries_price_quantity_and_side(self):
        lvl = _messages("level")[0]
        for field in ("ack_id", "price", "quantity", "side", "symbol"):
            self.assertIn(field, lvl)
        self.assertIn(lvl["side"], ("Bid", "Ask"))

    def test_the_capture_contains_a_real_zero_quantity_removal(self):
        """Not a hypothetical: the live feed removes levels this way."""
        zeros = [m for m in _messages("level") if m["quantity"] == 0]
        self.assertTrue(zeros, "fixture no longer proves the removal semantics")

    def test_one_ack_id_can_carry_several_level_messages(self):
        """So equal ids must be APPLIED, not skipped as duplicates — an
        ack_id identifies an atomic batch, not a single message."""
        acks = [m["ack_id"] for m in _messages("level")]
        self.assertLess(len(set(acks)), len(acks))

    def test_the_subscribe_frame_we_send_is_the_one_that_worked(self):
        sent = FIXTURE["subscribe"]
        ours = MD.subscribe_message([SYM])
        self.assertEqual(ours["type"], sent["type"])
        self.assertEqual(ours["product_codes"], [SYM])
        self.assertIn("book", [c["name"] for c in ours["channels"]])


class TheBookIsABookNotAListOfMessagesTests(unittest.TestCase):

    def setUp(self):
        MD.reset_books()
        self.book = MD.PerpBook(SYM)
        self.book.apply(_snapshot())

    def test_a_snapshot_builds_the_correct_best_bid_and_ask(self):
        snap = _snapshot()
        top = self.book.top()
        self.assertEqual(top["bid_raw"], max(p for p, _ in snap["bids"]))
        self.assertEqual(top["ask_raw"], min(p for p, _ in snap["asks"]))
        self.assertEqual(top["state"], MD.BOOK_OK)

    def test_a_level_update_changes_exactly_that_level(self):
        before = self.book.top()
        ack = int(self.book.ack_id) + 10
        self.book.apply({"type": "level", "ack_id": str(ack), "symbol": SYM,
                         "price": before["bid_raw"] + 1, "quantity": 7,
                         "side": "Bid", "timestamp": None})
        after = self.book.top()
        self.assertEqual(after["bid_raw"], before["bid_raw"] + 1)
        self.assertEqual(after["bid_size"], 7)
        self.assertEqual(after["ask_raw"], before["ask_raw"])

    def test_quantity_zero_removes_the_level_rather_than_resting_at_zero(self):
        """A zero-size level left in the book is a phantom best price that
        nothing can trade at."""
        before = self.book.top()
        ack = int(self.book.ack_id) + 10
        self.book.apply({"type": "level", "ack_id": str(ack), "symbol": SYM,
                         "price": before["bid_raw"], "quantity": 0,
                         "side": "Bid", "timestamp": None})
        after = self.book.top()
        self.assertNotEqual(after["bid_raw"], before["bid_raw"])
        self.assertLess(after["bid_raw"], before["bid_raw"])
        self.assertEqual(after["bid_levels"], before["bid_levels"] - 1)

    def test_an_older_ack_cannot_move_the_book_backwards(self):
        before = self.book.top()
        stale = int(self.book.ack_id) - 500
        self.book.apply({"type": "level", "ack_id": str(stale), "symbol": SYM,
                         "price": before["bid_raw"] + 999, "quantity": 99,
                         "side": "Bid", "timestamp": None})
        self.assertEqual(self.book.top()["bid_raw"], before["bid_raw"])

    def test_an_equal_ack_is_applied_because_it_is_the_same_batch(self):
        before = self.book.top()
        same = str(self.book.ack_id)
        self.book.apply({"type": "level", "ack_id": same, "symbol": SYM,
                         "price": before["bid_raw"] + 1, "quantity": 3,
                         "side": "Bid", "timestamp": None})
        self.assertEqual(self.book.top()["bid_raw"], before["bid_raw"] + 1)

    def test_the_real_captured_updates_replay_without_desyncing(self):
        for m in _messages("level"):
            self.book.apply(m)
        self.assertEqual(self.book.top()["state"], MD.BOOK_OK)


class SequenceIntegrityIsAnExecutionSafetyPropertyTests(unittest.TestCase):

    def setUp(self):
        MD.reset_books()
        self.book = MD.PerpBook(SYM)
        self.book.apply(_snapshot())

    def test_a_malformed_update_invalidates_rather_than_being_skipped(self):
        """A message we could not parse is a GAP. Continuing would fill
        against a book missing an unknown number of changes."""
        self.book.apply({"type": "level", "ack_id": str(int(self.book.ack_id) + 1),
                         "price": "not-a-price", "quantity": 5, "side": "Bid",
                         "symbol": SYM})
        top = self.book.top()
        self.assertEqual(top["state"], MD.BOOK_DESYNCED)
        self.assertIsNone(top["bid_raw"])

    def test_an_unknown_side_invalidates(self):
        self.book.apply({"type": "level", "ack_id": str(int(self.book.ack_id) + 1),
                         "price": 12345, "quantity": 5, "side": "Middle",
                         "symbol": SYM})
        self.assertEqual(self.book.top()["state"], MD.BOOK_DESYNCED)

    def test_a_desynced_book_prices_nothing_until_a_fresh_snapshot(self):
        self.book.invalidate("test")
        self.assertEqual(self.book.top()["state"], MD.BOOK_DESYNCED)
        self.book.apply(_snapshot())
        self.assertEqual(self.book.top()["state"], MD.BOOK_OK)

    def test_a_reconnect_invalidates_every_book(self):
        MD.apply_message(_snapshot())
        self.assertEqual(MD.latest_top(SYM)["state"], MD.BOOK_OK)
        MD.reset_books()
        self.assertIsNone(MD.latest_top(SYM))


class MarketStateIsNotADataOutageTests(unittest.TestCase):

    def setUp(self):
        MD.reset_books()
        self.book = MD.PerpBook(SYM)
        self.book.apply(_snapshot())

    def test_the_captured_status_message_reads_open(self):
        status = _messages("status")[0]
        self.assertEqual(status["state"], MD.STATE_OPEN)
        self.book.apply(status)
        self.assertEqual(self.book.top()["state"], MD.BOOK_OK)

    def test_a_closed_session_stops_the_book_pricing_fills(self):
        self.book.apply({"type": "status", "state": MD.STATE_CLOSE,
                         "symbol": SYM, "ack_id": str(self.book.ack_id)})
        self.assertEqual(self.book.top()["state"], MD.BOOK_CLOSED)

    def test_a_halt_is_distinct_from_a_close(self):
        self.book.apply({"type": "status", "state": MD.STATE_HALT,
                         "symbol": SYM, "ack_id": str(self.book.ack_id)})
        self.assertEqual(self.book.top()["state"], MD.BOOK_HALTED)


class ThePriceScaleIsVerifiedNotAssumedTests(unittest.TestCase):
    """THE DANGEROUS PART. One tick-conversion error on a cheap coin
    corrupts notional, fees, leverage, stop distance and liquidation at
    once, and each of those looks plausible on its own."""

    def test_btc_converts_raw_ticks_to_dollars(self):
        p = BP.resolve("BTC/USD")
        self.assertTrue(p.ok, p.detail)
        self.assertEqual(p.price_increment, 5.0)
        self.assertEqual(p.contract_size, 0.01)
        # The captured book: raw 12881 x $5 = $64,405/BTC, against a spot
        # reference of $64,439 the same minute.
        self.assertAlmostEqual(p.price_usd(12881), 64_405.0, places=6)

    def test_a_contract_is_valued_through_its_own_size(self):
        p = BP.resolve("BTC/USD")
        self.assertAlmostEqual(p.contract_value_usd(64_405.0), 644.05, places=6)

    def test_shib_is_refused_because_its_scale_could_not_be_verified(self):
        """MEASURED: the published increment implies ~$0.00446 against an
        observed market of ~$0.00000447. The exchange publishes nothing that
        explains the ~1000x gap, so it is refused rather than divided by a
        constant somebody invented."""
        p = BP.resolve("SHIB/USD")
        self.assertFalse(p.ok)
        self.assertEqual(p.reason, BP.UNVERIFIED_PRICE_SCALE)
        self.assertIn("1000x", p.detail)

    def test_an_asset_with_no_us_perpetual_is_refused(self):
        """Capability before economics: a spot listing does not prove a
        perpetual exists."""
        for sym in ("BANK/USD", "BEAT/USD"):
            with self.subTest(symbol=sym):
                p = BP.resolve(sym)
                self.assertFalse(p.ok)
                self.assertEqual(p.reason, BP.NO_BITNOMIAL_PRODUCT)

    def test_every_verified_product_resolves_completely(self):
        for sym in BP.active_symbols():
            spec = BP._load()["by_symbol"][sym]
            with self.subTest(symbol=sym):
                p = BP.resolve(f"{spec['contract_size_unit']}/USD")
                self.assertTrue(p.ok, p.detail)
                self.assertGreater(p.contract_size, 0)
                self.assertGreater(p.price_increment, 0)
                self.assertTrue(p.product_id)


class TheExchangeAgreesWithTheHandWrittenRegistryTests(unittest.TestCase):
    """`venues.US_PERP_CONTRACTS` carries hand-typed contract sizes and is
    load-bearing for every US perp fee. This cross-checks it against the
    exchange's own published specs instead of trusting it."""

    def test_every_registry_contract_size_matches_the_exchange(self):
        audit = BP.audit_against_venue_registry()
        self.assertEqual(audit["disagree"], [],
                         f"registry diverged from published specs: {audit['disagree']}")

    def test_the_registry_covers_the_active_perpetuals(self):
        audit = BP.audit_against_venue_registry()
        self.assertGreaterEqual(len(audit["agree"]), 16)


class ReadOnlyByConstructionTests(unittest.TestCase):
    """Not a promise in a docstring — an assertion over the source."""

    def _source(self, rel):
        return (pathlib.Path(__file__).parent.parent / rel).read_text(encoding="utf-8")

    MODULES = ("lib/bitnomial_market_data.py", "lib/bitnomial_products.py")

    def _executable_tokens(self, rel):
        """Identifiers and string LITERALS that the module actually executes.

        Deliberately AST-based and docstring-free. A substring scan over the
        raw source fails on this module's own prose — it explains that there
        is no cancel or transfer surface, and a naive grep reads that
        explanation as the offence. Prose must never be able to satisfy or
        break a guard; that mistake has cost this codebase six cycles.
        """
        import ast
        tree = ast.parse(self._source(rel))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        names, literals = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr.lower())
            elif isinstance(node, ast.Name):
                names.add(node.id.lower())
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value not in docstrings:
                    literals.add(node.value.lower())
        return names, literals

    def test_the_adapter_calls_no_mutating_http_verb(self):
        for rel in self.MODULES:
            names, _ = self._executable_tokens(rel)
            for verb in ("post", "put", "delete", "patch"):
                with self.subTest(module=rel, verb=verb):
                    self.assertNotIn(verb, names)

    def test_no_order_or_account_surface_is_referenced_in_code(self):
        for rel in self.MODULES:
            names, literals = self._executable_tokens(rel)
            blob = " ".join(sorted(literals))
            for token in ("cancel", "withdraw", "transfer", "api_key",
                          "secret", "hmac", "signature"):
                with self.subTest(module=rel, token=token):
                    self.assertNotIn(token, names)
                    self.assertNotIn(token, blob)

    def test_no_url_literal_points_at_a_private_endpoint(self):
        for rel in self.MODULES:
            _, literals = self._executable_tokens(rel)
            urls = [s for s in literals if s.startswith(("http", "ws"))]
            for u in urls:
                with self.subTest(module=rel, url=u):
                    self.assertNotIn("/order", u)
                    self.assertNotIn("/account", u)
                    self.assertNotIn("/fill", u)

    def test_only_the_public_unauthenticated_surfaces_are_named(self):
        self.assertEqual(BP.WS_URL, "wss://bitnomial.com/exchange/ws")
        self.assertEqual(BP.REST_BASE,
                         "https://bitnomial.com/exchange/api/v1/prod")

    def test_a_price_lookup_never_opens_a_connection(self):
        """Market data is a side effect. An execution path that silently
        connects turns a missing quote into a timeout instead of a refusal."""
        import ast
        fn = next(n for n in ast.walk(ast.parse(
            self._source("lib/bitnomial_market_data.py")))
            if isinstance(n, ast.FunctionDef) and n.name == "latest_top")
        calls = {c.func.attr for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
        self.assertNotIn("connect", calls)
        self.assertNotIn("start_stream", calls)


if __name__ == "__main__":
    unittest.main()
