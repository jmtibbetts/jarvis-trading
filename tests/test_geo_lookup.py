import unittest

from lib.geo_lookup import resolve_coords


class ResolveCoordsTests(unittest.TestCase):
    def test_known_country_resolves(self):
        lat, lon = resolve_coords("Iran", "Middle East")
        self.assertAlmostEqual(lat, 32.4, places=1)
        self.assertAlmostEqual(lon, 53.7, places=1)

    def test_country_lookup_is_case_insensitive(self):
        lat, lon = resolve_coords("UKRAINE", "Europe")
        self.assertIsNotNone(lat)
        self.assertIsNotNone(lon)

    def test_unknown_country_falls_back_to_region(self):
        lat, lon = resolve_coords("Nowhereland", "Europe")
        self.assertAlmostEqual(lat, 54.5, places=1)
        self.assertAlmostEqual(lon, 15.3, places=1)

    def test_no_country_or_region_returns_none(self):
        lat, lon = resolve_coords(None, None)
        self.assertIsNone(lat)
        self.assertIsNone(lon)

    def test_unresolvable_region_returns_none(self):
        lat, lon = resolve_coords(None, "Atlantis")
        self.assertIsNone(lat)
        self.assertIsNone(lon)


if __name__ == "__main__":
    unittest.main()
