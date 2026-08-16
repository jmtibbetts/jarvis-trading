"""W3 — one token-surge definition, and it reaches production.

There were two:

    wallet_discovery.surge_metrics()   h1/h6/h24 buckets, NO baseline —
                                       used by the scheduled discovery pass
    token_surge.score_snapshot()       measured self-baselines — reachable
                                       ONLY from /onchain/surge, and called
                                       there as `score_snapshot(snap, [])`

That empty-list literal meant the rigorous implementation ran permanently
in new-token mode. TokenActivitySnapshot and TokenSurgeState were declared
in the schema with ZERO writers and ZERO readers.

The audit asked for an end-to-end test that exercises the REAL production
path, not `score_snapshot()` directly:

    six quiet snapshots -> one high-activity snapshot
    -> production discovery returns baseline_quality="measured"
    -> the waking token outranks a permanently-large flat token
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.database import (TokenActivitySnapshot, TokenSurgeState, get_db)
from lib import token_surge

QUIET_MINT = "QuietMint1111111111111111111111111111111111"
BIG_MINT = "BigFlatMint111111111111111111111111111111111"


def _pool(mint, *, name, pool, vol_m5, buys, sells, buyers, sellers,
          liq=500_000.0, vol_h24=1_000_000.0, chg_h1=2.0):
    """A GeckoTerminal pool payload in the real response shape."""
    return {
        "attributes": {
            "name": name, "address": pool,
            "base_token_price_usd": "1.0",
            "reserve_in_usd": str(liq),
            "volume_usd": {"m5": str(vol_m5), "m15": "0", "m30": "0",
                           "h1": str(vol_m5 * 12), "h6": str(vol_m5 * 72),
                           "h24": str(vol_h24)},
            "transactions": {
                "m5": {"buys": buys, "sells": sells,
                       "buyers": buyers, "sellers": sellers},
                "h1": {"buys": buys * 12, "sells": sells * 12,
                       "buyers": buyers * 12, "sellers": sellers * 12},
            },
            "price_change_percentage": {"m5": "1.0", "h1": str(chg_h1),
                                        "h6": "3.0", "h24": "5.0"},
        },
        "relationships": {"base_token": {"data": {"id": f"solana_{mint}"}}},
    }


def _quiet(mint=QUIET_MINT):
    return _pool(mint, name="QUIET/SOL", pool="poolQuiet",
                 vol_m5=900.0, buys=6, sells=5, buyers=5, sellers=4)


def _waking(mint=QUIET_MINT):
    """Same token, suddenly 70x its own 5m volume and far more wallets."""
    return _pool(mint, name="QUIET/SOL", pool="poolQuiet",
                 vol_m5=63_000.0, buys=420, sells=180,
                 buyers=260, sellers=140)


def _big_and_flat(mint=BIG_MINT):
    """Permanently large, going nowhere. Must NOT outrank the waking token."""
    return _pool(mint, name="BIGFLAT/SOL", pool="poolBig",
                 vol_m5=40_000.0, buys=200, sells=195,
                 buyers=150, sellers=148,
                 liq=20_000_000.0, vol_h24=60_000_000.0, chg_h1=0.1)


def _clear():
    with get_db() as db:
        db.query(TokenActivitySnapshot).delete()
        db.query(TokenSurgeState).delete()


class SurgePipelineTests(unittest.TestCase):

    def setUp(self):
        _clear()

    def tearDown(self):
        _clear()

    def _scan(self, pools):
        def _fake(path, errors=None):
            return pools if path == "trending_pools" else []
        with patch("lib.geckoterminal.solana_pools", _fake):
            return token_surge.scan_and_score()

    # ── the audit's end-to-end scenario ──────────────────────────────────

    def test_six_quiet_then_a_spike_produces_a_measured_baseline(self):
        for _ in range(6):
            r = self._scan([_quiet()])
            self.assertEqual(r["tokens"][0]["baseline_quality"], "new_token",
                             "before six snapshots there is no baseline")

        r = self._scan([_waking()])
        tok = r["tokens"][0]
        self.assertEqual(tok["baseline_quality"], "measured",
                         "the seventh scan must score against stored history")
        self.assertGreater(tok["volume_accel"], 10.0)
        self.assertEqual(r["measured_baselines"], 1)

    def test_the_waking_token_outranks_the_permanently_large_one(self):
        for _ in range(6):
            self._scan([_quiet(), _big_and_flat()])
        r = self._scan([_waking(), _big_and_flat()])
        ranked = [t["mint"] for t in r["tokens"]]
        self.assertEqual(ranked[0], QUIET_MINT,
                         "acceleration must beat size — that is the whole point")

    def test_discovery_consumes_the_same_pipeline(self):
        """The REAL production path, not score_snapshot() directly."""
        from lib.wallet_discovery import interesting_solana_mints

        for _ in range(6):
            self._scan([_quiet()])

        def _fake(path, errors=None):
            return [_waking()] if path == "trending_pools" else []
        with patch("lib.geckoterminal.solana_pools", _fake):
            toks = interesting_solana_mints(limit=5)

        self.assertTrue(toks)
        self.assertEqual(toks[0]["mint"], QUIET_MINT)
        self.assertEqual(toks[0]["baseline_quality"], "measured",
                         "discovery must see the measured baseline, not a "
                         "bucket-derived guess")
        self.assertEqual(toks[0]["source_list"], "token_surge")

    def test_the_route_consumes_the_same_pipeline(self):
        from app.routers.onchain import token_surge as surge_route

        for _ in range(6):
            self._scan([_quiet()])

        def _fake(path, errors=None):
            return [_waking()] if path == "trending_pools" else []
        with patch("lib.geckoterminal.solana_pools", _fake):
            out = surge_route(limit=10)
        self.assertEqual(out["tokens"][0]["baseline_quality"], "measured")
        self.assertIn("measured_baselines", out)

    # ── persistence ──────────────────────────────────────────────────────

    def test_snapshots_are_actually_written(self):
        """The tables had zero writers before this phase."""
        self._scan([_quiet()])
        with get_db() as db:
            self.assertEqual(
                db.query(TokenActivitySnapshot).filter(
                    TokenActivitySnapshot.mint == QUIET_MINT).count(), 1)

    def test_the_current_reading_is_not_part_of_its_own_baseline(self):
        """Including it would drag the median toward the spike being hunted."""
        for _ in range(6):
            self._scan([_quiet()])
        with get_db() as db:
            hist = token_surge.load_history(db, QUIET_MINT)
        self.assertEqual(len(hist), 6)
        r = self._scan([_waking()])
        # 63,000 against a ~900 median is ~70x. If the current reading were
        # in its own baseline the median would be pulled far higher.
        self.assertGreater(r["tokens"][0]["volume_accel"], 20.0)

    # ── surge state / T0 ─────────────────────────────────────────────────

    def test_surge_started_at_is_stamped_and_held(self):
        """The T0 pre-surge wallet discovery searches backwards from."""
        for _ in range(6):
            self._scan([_quiet()])
        self._scan([_waking()])
        with get_db() as db:
            row = db.query(TokenSurgeState).filter(
                TokenSurgeState.mint == QUIET_MINT).first()
            state, first_t0 = row.state, row.surge_started_at
        self.assertEqual(state, token_surge.STATE_SURGING)
        self.assertIsNotNone(first_t0)

        self._scan([_waking()])
        with get_db() as db:
            row = db.query(TokenSurgeState).filter(
                TokenSurgeState.mint == QUIET_MINT).first()
            later_t0 = row.surge_started_at
        self.assertEqual(later_t0, first_t0,
                         "a continuing surge keeps the moment it BEGAN")

    def test_surging_tokens_reads_the_persisted_state(self):
        for _ in range(6):
            self._scan([_quiet()])
        self._scan([_waking()])
        surging = token_surge.surging_tokens()
        self.assertTrue(surging)
        self.assertEqual(surging[0]["mint"], QUIET_MINT)
        self.assertIsNotNone(surging[0]["surge_started_at"])

    def test_a_quiet_token_never_enters_the_surging_state(self):
        for _ in range(7):
            self._scan([_quiet()])
        self.assertEqual(token_surge.surging_tokens(), [])


class NoParallelDefinitionTests(unittest.TestCase):
    """Guard against the two-definitions problem returning."""

    def test_no_production_path_scores_with_an_empty_history(self):
        """Matched on the AST, not the text — the modules explain the old
        `score_snapshot(snap, [])` defect in prose, and a text scan would
        flag the comment describing the fix as the defect itself.
        """
        import ast
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        bad = []
        for d in ("lib", "app", "jobs"):
            for f in (root / d).rglob("*.py"):
                if "__pycache__" in f.parts:
                    continue
                try:
                    tree = ast.parse(f.read_text(encoding="utf-8", errors="ignore"))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    fn = node.func
                    name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                    if name != "score_snapshot" or len(node.args) < 2:
                        continue
                    second = node.args[1]
                    if isinstance(second, ast.List) and not second.elts:
                        bad.append(f"{f.relative_to(root).as_posix()}:{node.lineno}")
        self.assertEqual(bad, [], (
            "score_snapshot(snap, []) permanently disables the baseline it "
            f"exists to compute: {bad}"))

    def test_discovery_does_not_use_the_bucket_metric_for_ranking(self):
        """Checks the CODE, with the docstring stripped — the docstring
        names surge_metrics precisely to record that it is no longer used.
        """
        import ast
        import inspect

        from lib import wallet_discovery
        src = inspect.getsource(wallet_discovery.interesting_solana_mints)
        fn = ast.parse(src.lstrip()).body[0]
        if (fn.body and isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)):
            fn.body = fn.body[1:]                       # drop the docstring
        called = {getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                  for n in ast.walk(ast.Module(body=fn.body, type_ignores=[]))
                  if isinstance(n, ast.Call)}
        self.assertNotIn("surge_metrics", called,
                         "discovery must rank on the canonical engine")
        self.assertIn("scan_and_score", called)


if __name__ == "__main__":
    unittest.main()
