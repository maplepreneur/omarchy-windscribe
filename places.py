#!/usr/bin/env python3
"""Where Windscribe's cities are, for the panel mini-map.

The CLI's `locations` prints names but no coordinates, and the plugin
promises to make no network requests, so the coordinates are bundled
here. The list is the city set Windscribe's own location list uses
(as of CLI 2.25.x); a city the engine later adds that this file
doesn't know is simply not lit on the map, never an error.

Fake locations ("Fake Mumbai", "Fake Antarctica") are drawn at the
coordinates of the place they impersonate, which is the honest dot:
it is where your traffic appears to come from. Troll is the real
Troll research station, which is where Windscribe's Antarctica
location actually exits.

Usage: places.py --cities        every known city, with lat/lon
       places.py --locate CITY  one city's coordinates
Prints compact JSON, or [] / {} when nothing matches.
"""
import json
import sys

# "City": (lat, lon)
PLACES = {
    # North America
    "Halifax": (44.65, -63.58),
    "Montreal": (45.50, -73.57),
    "Toronto": (43.65, -79.38),
    "Calgary": (51.05, -114.07),
    "Vancouver": (49.28, -123.12),
    "Winnipeg": (49.90, -97.14),
    "Atlanta": (33.75, -84.39),
    "Dallas": (32.78, -96.80),
    "Denver": (39.74, -104.99),
    "Houston": (29.76, -95.37),
    "Kansas City": (39.10, -94.58),
    "Ashburn": (39.04, -77.49),
    "Boston": (42.36, -71.06),
    "Buffalo": (42.89, -78.88),
    "Charlotte": (35.23, -80.84),
    "Chicago": (41.88, -87.63),
    "Cleveland": (41.50, -81.69),
    "Detroit": (42.33, -83.05),
    "Miami": (25.76, -80.19),
    "New Jersey": (40.74, -74.17),
    "New York": (40.71, -74.01),
    "Philadelphia": (39.95, -75.17),
    "Raleigh": (35.78, -78.64),
    "South Bend": (41.68, -86.25),
    "Tampa": (27.95, -82.46),
    "Washington DC": (38.91, -77.04),
    "Bend": (44.06, -121.32),
    "Honolulu": (21.31, -157.86),
    "Las Vegas": (36.17, -115.14),
    "Los Angeles": (34.05, -118.24),
    "Phoenix": (33.45, -112.07),
    "Salt Lake City": (40.76, -111.89),
    "San Francisco": (37.77, -122.42),
    "San Jose": (37.34, -121.89),
    "Santa Clara": (37.35, -121.95),
    "Seattle": (47.61, -122.33),
    "Queretaro": (20.59, -100.39),
    "Guatemala City": (14.63, -90.51),
    "San Salvador": (13.69, -89.22),
    "Panama City": (8.98, -79.52),
    # South America
    "Bogota": (4.71, -74.07),
    "Quito": (-0.18, -78.47),
    "Lima": (-12.05, -77.04),
    "Santiago": (-33.45, -70.67),
    "Buenos Aires": (-34.60, -58.38),
    "Montevideo": (-34.90, -56.16),
    "Asuncion": (-25.26, -57.58),
    "Sao Paulo": (-23.55, -46.63),
    # Europe
    "Tirana": (41.33, 19.82),
    "Vienna": (48.21, 16.37),
    "Brussels": (50.85, 4.35),
    "Sofia": (42.70, 23.32),
    "Zagreb": (45.81, 15.98),
    "Prague": (50.08, 14.44),
    "Copenhagen": (55.68, 12.57),
    "Tallinn": (59.44, 24.75),
    "Helsinki": (60.17, 24.94),
    "Bordeaux": (44.84, -0.58),
    "Marseille": (43.30, 5.37),
    "Paris": (48.86, 2.35),
    "Berlin": (52.52, 13.41),
    "Frankfurt": (50.11, 8.68),
    "Athens": (37.98, 23.73),
    "Budapest": (47.50, 19.04),
    "Reykjavik": (64.15, -21.94),
    "Dublin": (53.35, -6.26),
    "Milan": (45.46, 9.19),
    "Palermo": (38.12, 13.36),
    "Riga": (56.95, 24.11),
    "Vilnius": (54.69, 25.28),
    "Luxembourg": (49.61, 6.13),
    "Chisinau": (47.01, 28.86),
    "Amsterdam": (52.37, 4.90),
    "Skopje": (41.99, 21.43),
    "Oslo": (59.91, 10.75),
    "Gdansk": (54.35, 18.65),
    "Warsaw": (52.23, 21.01),
    "Lisbon": (38.72, -9.14),
    "Bucharest": (44.43, 26.10),
    "Belgrade": (44.79, 20.45),
    "Bratislava": (48.15, 17.11),
    "Ljubljana": (46.06, 14.51),
    "Barcelona": (41.39, 2.17),
    "Madrid": (40.42, -3.70),
    "Stockholm": (59.33, 18.07),
    "Zurich": (47.38, 8.54),
    "Kyiv": (50.45, 30.52),
    "Edinburgh": (55.95, -3.19),
    "London": (51.51, -0.13),
    "Manchester": (53.48, -2.24),
    "Sarajevo": (43.86, 18.41),
    "Novi Travnik": (44.17, 17.66),
    # Middle East
    "Limassol": (34.71, 33.02),
    "Nicosia": (35.19, 33.38),
    "Tbilisi": (41.72, 44.78),
    "Tel Aviv": (32.09, 34.78),
    "Ashdod": (31.80, 34.65),
    "Istanbul": (41.01, 28.98),
    "Dubai": (25.20, 55.27),
    # Africa
    "Lagos": (6.52, 3.38),
    "Nairobi": (-1.29, 36.82),
    "Johannesburg": (-26.20, 28.05),
    # Asia
    "Mumbai": (19.08, 72.88),
    "New Delhi": (28.61, 77.21),
    "Bali": (-8.65, 115.22),
    "Jakarta": (-6.21, 106.85),
    "Surabaya": (-7.26, 112.75),
    "Kuala Lumpur": (3.14, 101.69),
    "Bangkok": (13.76, 100.50),
    "Manila": (14.60, 120.98),
    "Singapore": (1.35, 103.82),
    "Hanoi": (21.03, 105.85),
    "Hong Kong": (22.32, 114.17),
    "Tokyo": (35.68, 139.69),
    "Seoul": (37.57, 126.98),
    "Taipei": (25.03, 121.57),
    # Oceania
    "Auckland": (-36.85, 174.76),
    "Adelaide": (-34.93, 138.60),
    "Brisbane": (-27.47, 153.03),
    "Melbourne": (-37.81, 144.96),
    "Perth": (-31.95, 115.86),
    "Sydney": (-33.87, 151.21),
    # Virtual / fake locations, drawn where they appear from
    "Comfort Zone": (43.65, -79.38),      # Best Location: Toronto
    "Fake Mumbai": (19.08, 72.88),
    "Fake St Petersburg": (59.93, 30.34),
    "Troll": (-72.01, 2.53),              # Fake Antarctica: Troll station
}


def all_cities():
    return [
        {"city": city, "lat": lat, "lon": lon}
        for city, (lat, lon) in sorted(PLACES.items())
    ]


def locate(name):
    key = " ".join(str(name or "").split())
    if key in PLACES:
        lat, lon = PLACES[key]
        return {"city": key, "lat": lat, "lon": lon}
    return {}


def main():
    if len(sys.argv) < 2:
        print("[]")
        return
    if sys.argv[1] == "--cities":
        print(json.dumps(all_cities(), separators=(",", ":")))
    elif sys.argv[1] == "--locate":
        name = sys.argv[2] if len(sys.argv) > 2 else ""
        print(json.dumps(locate(name), separators=(",", ":")))
    else:
        print("[]")


if __name__ == "__main__":
    main()
