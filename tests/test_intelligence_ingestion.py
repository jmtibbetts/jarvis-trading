import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from jobs import fetch_threat_news as news
from lib.intelligence_quality import canonicalize_url, enrich_articles, extract_entities, source_reliability


class IntelligenceParsingTests(unittest.TestCase):
    def test_rss_parser_preserves_provenance_and_normalizes_time(self):
        xml = b"""<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
          <title>Chip factory expands capacity</title>
          <description><![CDATA[<b>More HBM production</b> is coming.]]></description>
          <link>https://example.com/chips?utm_source=rss</link>
          <pubDate>Sun, 09 Aug 2026 12:00:00 GMT</pubDate>
        </item></channel></rss>"""
        feed = {"source": "Test Fab", "category": "semiconductors", "kind": "manufacturer"}
        articles = news.parse_feed_content(xml, feed)

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["source"], "Test Fab")
        self.assertEqual(articles[0]["summary"], "More HBM production is coming.")
        self.assertEqual(articles[0]["published"], "2026-08-09T12:00:00+00:00")

    def test_x_parser_builds_clickable_post(self):
        payload = {"data": [{"id": "123", "text": "New tariff policy", "created_at": "2026-08-09T12:00:00Z"}]}
        article = news.parse_x_payload("POTUS", payload)[0]

        self.assertEqual(article["source"], "X @POTUS")
        self.assertEqual(article["url"], "https://x.com/POTUS/status/123")
        self.assertEqual(article["category"], "social_politics")

    def test_truth_parser_accepts_nested_licensed_api_shape(self):
        payload = {"data": {"posts": [{
            "id": "456", "content": "<p>Policy announcement</p>",
            "account": {"username": "realDonaldTrump"},
            "url": "https://truthsocial.com/@realDonaldTrump/posts/456",
            "created_at": "2026-08-09T12:00:00Z",
        }]}}
        article = news.parse_truth_payload(payload)[0]

        self.assertEqual(article["source"], "Truth Social @realDonaldTrump")
        self.assertEqual(article["summary"], "Policy announcement")
        self.assertEqual(article["provider"], "truth_api")

    def test_gdelt_parser_tags_ai_infrastructure_topic(self):
        payload = {"articles": [{
            "title": "New data center power agreement", "url": "https://example.com/dc",
            "domain": "example.com", "seendate": "20260809T120000Z",
        }]}
        article = news.parse_gdelt_payload(payload)[0]

        self.assertEqual(article["category"], "data_centers")
        self.assertEqual(article["published"], "2026-08-09T12:00:00+00:00")


class IntelligenceClassificationTests(unittest.TestCase):
    def test_direct_reporting_rates_above_social_and_aggregators(self):
        self.assertGreater(source_reliability("wire", "Reuters"), source_reliability("social", "X"))
        self.assertGreater(source_reliability("government", "White House"), source_reliability("aggregator", "GDELT"))

    def test_baseline_detects_supply_threat_and_assets(self):
        result = news._baseline_analysis({
            "title": "NVIDIA GPU supply shortage after factory fire",
            "summary": "", "category": "semiconductors",
        }, 1)

        self.assertTrue(result["is_threat"])
        self.assertEqual(result["event_type"], "supply_disruption")
        self.assertIn("NVDA", result["affected_assets"])
        self.assertEqual(result["sentiment"], "negative")

    def test_word_matching_does_not_mark_award_as_war(self):
        result = news._baseline_analysis({
            "title": "Company wins an industry award",
            "summary": "", "category": "tech",
        }, 1)
        self.assertFalse(result["is_threat"])

    def test_llm_failure_still_returns_every_article(self):
        articles = [
            {"title": "Routine earnings preview", "summary": "", "source": "Test", "category": "finance"},
            {"title": "Port shutdown disrupts supply chain", "summary": "", "source": "Test", "category": "supply_chain"},
        ]
        with patch.object(news, "call_lm_studio", side_effect=RuntimeError("offline")):
            results = news.analyze_batch(articles)

        self.assertEqual(len(results), 2)
        self.assertFalse(results[0]["is_threat"])
        self.assertTrue(results[1]["is_threat"])

    def test_balancing_keeps_specialist_category_near_front(self):
        articles = [
            {"title": f"World {index}", "url": f"https://example.com/world/{index}", "category": "geopolitics", "published": f"2026-08-09T12:0{index}:00Z"}
            for index in range(5)
        ]
        articles.append({
            "title": "Only chip story", "url": "https://example.com/chip",
            "category": "semiconductors", "published": "2026-08-09T12:00:00Z",
        })

        selected = news.select_balanced(articles, limit=3)
        self.assertIn("semiconductors", [item["category"] for item in selected])

    def test_tracking_parameters_are_removed_from_canonical_url(self):
        self.assertEqual(
            canonicalize_url("https://example.com/story?utm_source=rss&id=7#top"),
            "https://example.com/story?id=7",
        )

    def test_ai_supply_chain_entities_map_to_tickers(self):
        entities = extract_entities({
            "title": "NVIDIA and Vertiv expand data center power equipment supply",
            "summary": "Micron HBM and Applied Materials capacity are included.",
        })
        self.assertTrue({"NVDA", "VRT", "MU", "AMAT"}.issubset(set(entities["assets"])))
        self.assertIn("Data Centers", entities["industries"])

    def test_social_post_needs_independent_corroboration(self):
        articles = [{
            "title": "President Trump announces new chip tariffs",
            "summary": "Tariffs begin next month", "source": "Truth Social @realDonaldTrump",
            "source_kind": "social", "provider": "truth_api",
            "url": "https://truthsocial.com/post/1", "published": "2026-08-09T12:00:00Z",
        }]
        enrich_articles(articles, now=datetime(2026, 8, 9, 13, tzinfo=timezone.utc))
        self.assertEqual(articles[0]["confirmation_status"], "unconfirmed_social")
        self.assertLessEqual(articles[0]["claim_confidence"], 45)

        articles.append({
            "title": "Trump announces chip tariffs beginning next month",
            "summary": "The policy covers semiconductor imports", "source": "Reuters Example",
            "source_kind": "newspaper", "provider": "rss",
            "url": "https://example.com/tariffs", "published": "2026-08-09T12:10:00Z",
        })
        enrich_articles(articles, now=datetime(2026, 8, 9, 13, tzinfo=timezone.utc))
        self.assertEqual(articles[0]["confirmation_status"], "corroborated")
        self.assertEqual(articles[0]["corroboration_count"], 1)

    def test_old_feed_item_is_marked_stale(self):
        article = {
            "title": "Old report", "summary": "", "source": "Test",
            "source_kind": "newspaper", "provider": "rss", "url": "https://example.com/old",
            "published": "2026-08-01T00:00:00Z",
        }
        enrich_articles([article], now=datetime(2026, 8, 9, tzinfo=timezone.utc))
        self.assertTrue(article["is_stale"])


if __name__ == "__main__":
    unittest.main()
