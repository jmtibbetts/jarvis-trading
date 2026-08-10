import unittest

from app import request_metrics


class ErrorRateSummaryTests(unittest.TestCase):
    def setUp(self):
        request_metrics.request_log.clear()

    def test_empty_log_has_zero_rate(self):
        summary = request_metrics.error_rate_summary()
        self.assertEqual(summary["total_requests"], 0)
        self.assertEqual(summary["error_rate_pct"], 0.0)

    def test_computes_rate_from_5xx_only(self):
        now = request_metrics.time.time()
        request_metrics.request_log.append((now, "/api/signals", 200))
        request_metrics.request_log.append((now, "/api/signals", 404))
        request_metrics.request_log.append((now, "/api/paper/open", 500))
        summary = request_metrics.error_rate_summary()
        self.assertEqual(summary["total_requests"], 3)
        self.assertEqual(summary["error_count"], 1)
        self.assertAlmostEqual(summary["error_rate_pct"], 33.33, places=1)

    def test_excludes_requests_outside_window(self):
        now = request_metrics.time.time()
        request_metrics.request_log.append((now - 3600, "/api/signals", 500))  # 1h ago
        request_metrics.request_log.append((now, "/api/signals", 200))
        summary = request_metrics.error_rate_summary(window_minutes=15)
        self.assertEqual(summary["total_requests"], 1)
        self.assertEqual(summary["error_count"], 0)

    def test_top_error_paths_ranked_by_count(self):
        now = request_metrics.time.time()
        for _ in range(3):
            request_metrics.request_log.append((now, "/api/paper/open", 500))
        request_metrics.request_log.append((now, "/api/signals", 502))
        summary = request_metrics.error_rate_summary()
        self.assertEqual(summary["top_error_paths"][0]["path"], "/api/paper/open")
        self.assertEqual(summary["top_error_paths"][0]["count"], 3)


if __name__ == "__main__":
    unittest.main()
