# ── Known journeys ─────────────────────────────────────────────────────────────
# Each entry:
#   description    — human label
#   stop_id        — destination stop dest_id from manifest.json
#   origin_lat/lon — coordinates of the origin stop (from GTFS stops.txt)
#   expected_band  — which 30-min band should contain the origin (30/60/90/…)
#   source         — verified on SBB.ch / DB.de + date checked
#
# dest_id format: slugify(stop_name) + "_" + slugify(gtfs_stop_id)
# Find it in docs/manifest.json after running make isochrones.

JOURNEYS = [
    {
        "description": "Zuerich HB to Airolo",
        "stop_id":     "airolo_parent8505201",  # check manifest.json
        "origin_lat":  47.3782,   # Zürich HB
        "origin_lon":  8.5403,
        "expected_band": 120,     # 119 min → 120-min band
        "source": "SBB.ch 20260307 — Zürich HB→Arth-Goldau→Airolo, 1h59m, 1 transfer",
        "debug": True,
    },
    {
        "description": "Muenchen Hbf to Scharnitz",
        "stop_id":     "scharnitz-bahnhof_at47223102",  # check manifest.json
        "origin_lat":  48.140833,   # München Hbf
        "origin_lon":  11.555,
        "expected_band": 150,     # 134 min → 150-min band
        "source": "bahn.de",
        "debug": True,
    },
    {
        "description": "Innsbruck Hbf to Telfes",
        "stop_id":     "telfes-i-st-ort_at476534203",  # check manifest.json
        "origin_lat":  47.2633,   # Innsbruck Hbf
        "origin_lon":  11.4009,
        "expected_band": 60,     # 57 min → 60-min band
        "source": "bahn.de",
        "debug": True,
    },
    {
        "description": "Lausanne to Miex, Le Flon",
        "stop_id":     "miex-le-flon_parent8501737",  # check manifest.json
        "origin_lat":  46.5197,    # Lausanne
        "origin_lon":  6.6323,
        "expected_band": 120,     # 97 min → 120-min band
        "source": "SBB.ch",
        "debug": True,
    },
    {
        "description": "Freilassing (Bayern) to Salzburg Ludwig-Schmederer-Platz",
        "stop_id":     "salzburg-ludwig-schmederer-platz_at455086601",  # check manifest.json
        "origin_lat":  47.836975,   # Freilassing
        "origin_lon":  12.976570,
        "expected_band": 60,     # 31 min → 60-min band
        "source": "oebb.at",
        "debug": True,
    }
]