"""Approximate country/region centroid coordinates for plotting ThreatEvent
markers on a map. Not precise geocoding — good enough to place a pulsing dot
on the right part of the world, which is all the Command Center's threat map
needs. Falls back to a region centroid when the country isn't in the table,
and to None (not plotted) only when neither resolves."""

COUNTRY_COORDS: dict[str, tuple[float, float]] = {
    "united states": (39.8, -98.6), "usa": (39.8, -98.6), "us": (39.8, -98.6),
    "china": (35.9, 104.2), "russia": (61.5, 105.3), "ukraine": (48.4, 31.2),
    "iran": (32.4, 53.7), "israel": (31.0, 34.8), "gaza": (31.5, 34.5),
    "palestine": (31.9, 35.2), "saudi arabia": (23.9, 45.1), "yemen": (15.6, 48.5),
    "syria": (34.8, 38.9), "iraq": (33.2, 43.7), "lebanon": (33.9, 35.9),
    "north korea": (40.3, 127.5), "south korea": (35.9, 127.8), "japan": (36.2, 138.3),
    "taiwan": (23.7, 121.0), "india": (20.6, 79.0), "pakistan": (30.4, 69.3),
    "afghanistan": (33.9, 67.7), "united kingdom": (55.4, -3.4), "uk": (55.4, -3.4),
    "france": (46.6, 2.2), "germany": (51.2, 10.5), "italy": (41.9, 12.6),
    "spain": (40.5, -3.7), "poland": (51.9, 19.1), "turkey": (38.9, 35.2),
    "egypt": (26.8, 30.8), "libya": (26.3, 17.2), "sudan": (12.9, 30.2),
    "ethiopia": (9.1, 40.5), "somalia": (5.2, 46.2), "nigeria": (9.1, 8.7),
    "south africa": (-30.6, 22.9), "mexico": (23.6, -102.6), "brazil": (-14.2, -51.9),
    "venezuela": (6.4, -66.6), "argentina": (-38.4, -63.6), "canada": (56.1, -106.3),
    "australia": (-25.3, 133.8), "indonesia": (-0.8, 113.9), "philippines": (12.9, 121.8),
    "vietnam": (14.1, 108.3), "thailand": (15.9, 101.0), "myanmar": (21.9, 95.9),
    "singapore": (1.35, 103.8), "hong kong": (22.3, 114.2),
}

REGION_COORDS: dict[str, tuple[float, float]] = {
    "middle east": (29.0, 41.0),
    "europe": (54.5, 15.3),
    "asia pacific": (15.0, 110.0),
    "north america": (45.0, -100.0),
    "south america": (-15.0, -60.0),
    "africa": (2.0, 20.0),
    "global": (20.0, 0.0),
}


def resolve_coords(country: str | None, region: str | None) -> tuple[float | None, float | None]:
    if country:
        hit = COUNTRY_COORDS.get(country.strip().lower())
        if hit:
            return hit
    if region:
        hit = REGION_COORDS.get(region.strip().lower())
        if hit:
            return hit
    return None, None
