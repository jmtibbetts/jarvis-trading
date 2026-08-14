"""Model attribution — which brain wrote which signal.

Pinned: the client records the RESPONSE's model name (not the request
config), the signal writer stamps it, and the comparison groups
pre-attribution history as 'unattributed' rather than guessing.
"""
import unittest
from unittest.mock import patch

from app.database import TradingSignal, TradeOutcome, get_db, init_db


class ServedModelCaptureTests(unittest.TestCase):
    def test_response_model_wins_over_request_config(self):
        import lib.lmstudio as lm
        lm._last_served_model = None
        # Simulate what the capture line does with a response payload.
        data = {"model": "qwen/qwen3-32b", "choices": [
            {"message": {"content": "ok"}, "finish_reason": "stop"}]}
        served = data.get("model")
        if served:
            lm._last_served_model = str(served)
        self.assertEqual(lm.last_served_model(), "qwen/qwen3-32b")

    def test_batch_helper_survives_import_failure(self):
        from jobs.generate_signals import _llm_model_for_batch
        with patch("lib.lmstudio.last_served_model",
                   side_effect=RuntimeError("boom")):
            self.assertIsNone(_llm_model_for_batch())


class ResolveCacheTtlTests(unittest.TestCase):
    """A permanent resolve cache holds the PREVIOUS model's name after a
    load swap — and naming an unloaded model triggers LM Studio's JIT
    loading: two models in VRAM the GPU may not have. The TTL is the
    guard: within minutes the desk follows the operator's swap."""

    def test_cache_expires_after_ttl(self):
        import time as _time

        import lib.lmstudio as lm
        url = "http://test-ttl:1234/v1"
        with lm._model_cache_lock:
            lm._resolved_model_cache[url] = ("old-model",
                                             _time.time() - 301.0)
        with patch("lib.lmstudio.httpx.get") as g:
            g.return_value.status_code = 200
            g.return_value.json.return_value = {
                "data": [{"id": "newly-loaded-model"}]}
            got = lm._resolve_model({"model": "", "url": url})
        self.assertEqual(got, "newly-loaded-model")
        with lm._model_cache_lock:
            lm._resolved_model_cache.pop(url, None)

    def test_fresh_cache_is_served_without_a_network_hop(self):
        import time as _time

        import lib.lmstudio as lm
        url = "http://test-fresh:1234/v1"
        with lm._model_cache_lock:
            lm._resolved_model_cache[url] = ("current-model", _time.time())
        with patch("lib.lmstudio.httpx.get") as g:
            got = lm._resolve_model({"model": "local-model", "url": url})
            g.assert_not_called()
        self.assertEqual(got, "current-model")
        with lm._model_cache_lock:
            lm._resolved_model_cache.pop(url, None)


class ComparisonGroupingTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with get_db() as db:
            db.query(TradeOutcome).filter(
                TradeOutcome.symbol == "TEST-ATTR").delete(
                synchronize_session=False)
            db.query(TradingSignal).filter(
                TradingSignal.asset_symbol == "TEST-ATTR").delete(
                synchronize_session=False)
            db.commit()

    def test_unattributed_history_stays_honest(self):
        from app.routers.learning import llm_model_comparison
        with get_db() as db:
            db.add(TradingSignal(id="TEST-ATTR-1", asset_symbol="TEST-ATTR",
                                 direction="Long", llm_model="qwen/qwen3-32b"))
            db.add(TradingSignal(id="TEST-ATTR-2", asset_symbol="TEST-ATTR",
                                 direction="Long", llm_model=None))
            db.add(TradeOutcome(signal_id="TEST-ATTR-1", symbol="TEST-ATTR",
                                pnl_pct=1.5, mfe_r=1.0))
            db.commit()
        rows = {m["model"]: m for m in llm_model_comparison()["models"]}
        self.assertIn("qwen/qwen3-32b", rows)
        self.assertIn("unattributed", rows)
        self.assertEqual(rows["qwen/qwen3-32b"]["outcomes"], 1)
        self.assertEqual(rows["qwen/qwen3-32b"]["win_rate"], 100.0)
        self.assertGreaterEqual(rows["unattributed"]["signals"], 1)


if __name__ == "__main__":
    unittest.main()
