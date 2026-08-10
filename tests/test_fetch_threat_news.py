import unittest

from jobs.fetch_threat_news import _coerce_scalar_str


class CoerceScalarStrTests(unittest.TestCase):
    def test_passes_through_a_plain_string(self):
        self.assertEqual(_coerce_scalar_str("Iran"), "Iran")

    def test_joins_a_list_into_a_comma_separated_string(self):
        # This is the exact shape that crashed sqlite3 with "Error binding
        # parameter 6: type 'list' is not supported" — a multi-country story
        # made the LLM return a JSON array instead of the single string the
        # classifier schema asks for.
        self.assertEqual(
            _coerce_scalar_str(["Iran", "China", "United States"]),
            "Iran, China, United States",
        )

    def test_joins_a_tuple_or_set_too(self):
        self.assertEqual(_coerce_scalar_str(("Iran", "China")), "Iran, China")
        self.assertIn(_coerce_scalar_str({"Iran"}), {"Iran"})

    def test_none_and_empty_list_return_default(self):
        self.assertEqual(_coerce_scalar_str(None), "")
        self.assertEqual(_coerce_scalar_str(None, default="Global"), "Global")
        self.assertEqual(_coerce_scalar_str([]), "")
        self.assertEqual(_coerce_scalar_str([None, ""]), "")

    def test_non_string_scalar_is_stringified(self):
        self.assertEqual(_coerce_scalar_str(42), "42")


if __name__ == "__main__":
    unittest.main()
