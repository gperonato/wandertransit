#!/usr/bin/env python3
"""
scripts/gpx_to_geojson.py
Converts cleaned GPX files from docs/gpx/ to:
  docs/gpx_tracks.geojson  — LineString features for the map
  docs/gpx_stats.json      — per-file stats (name, elevation, duration, etc.)

Prerequisites:
  - Run scripts/clean_gpx.py first to populate docs/gpx/ with cleaned, anonymized GPX files

Stats extracted:
  name          — from <name> tag in GPX (falls back to filename)
  distance_km   — total track length
  elevation_gain_m — cumulative ascent
  elevation_loss_m — cumulative descent
  max_altitude_m   — highest point
  min_altitude_m   — lowest point
  duration_min     — from <time> tags if present, else None
  osm_peak_name — nearest OSM peak name (cached in docs/gpx/.peak_cache.json)
"""

import hashlib
import json
import re
import math
import time
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# Read cleaned GPX files from docs/gpx/ (output of clean_gpx.py)
GPX_DIR     = Path(__file__).parent.parent / "docs" / "gpx"
OUTPUT      = Path(__file__).parent.parent / "docs" / "gpx_tracks.geojson"
STATS_OUT   = Path(__file__).parent.parent / "docs" / "gpx_stats.json"

# ── Overpass API: find nearest OSM peak name ──────────────────────────────────
OVERPASS_URL       = "https://overpass-api.de/api/interpreter"
PEAK_SEARCH_RADIUS = 500    # metres
OVERPASS_SLEEP     = 2.0    # base delay between requests
OVERPASS_MAX_TRIES = 4      # retries on 429/503

# File-based cache so re-runs never re-query the same coordinate
PEAK_CACHE_FILE = Path(__file__).parent.parent / "docs" / "gpx" / ".peak_cache.json"

# ── Haversine distance (metres) ───────────────────────────────────────────────
def haversine_m(lon1, lat1, lon2, lat2):
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

# ── Overpass API: find nearest OSM peak name ──────────────────────────────────
def _load_cache() -> dict:
    if PEAK_CACHE_FILE.exists():
        try:
            return json.loads(PEAK_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}

def _save_cache(cache: dict):
    try:
        PEAK_CACHE_FILE.write_text(json.dumps(cache, indent=2))
    except Exception:
        pass

def nearest_peak_name(lat: float, lon: float) -> tuple[str | None, bool]:
    """
    Query Overpass for natural=peak nodes near (lat, lon).
    Results are cached in gpx/.peak_cache.json — re-runs are instant.
    Retries with exponential backoff on 429 / 503.
    Returns (name_or_None, from_cache).
    """
    cache_key = f"{lat:.5f},{lon:.5f}"
    cache = _load_cache()
    if cache_key in cache:
        return cache[cache_key], True   # cache hit — no sleep needed

    query = (
        f"[out:json][timeout:10];"
        f'node["natural"="peak"]["name"]'
        f"(around:{PEAK_SEARCH_RADIUS},{lat},{lon});"
        f"out body;"
    )
    data = urllib.parse.urlencode({"data": query}).encode()

    result = None
    for attempt in range(OVERPASS_MAX_TRIES):
        wait = OVERPASS_SLEEP * (2 ** attempt)   # 2, 4, 8, 16 s
        time.sleep(wait if attempt > 0 else OVERPASS_SLEEP)
        req = urllib.request.Request(
            OVERPASS_URL, data=data,
            headers={"User-Agent": "hike-transit-planner/1.0 (gpx_to_geojson.py)"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
                break   # success
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                print(f"    ⏳ Overpass {e.code} — waiting {wait:.0f}s (attempt {attempt+1}/{OVERPASS_MAX_TRIES})")
                continue
            print(f"    ⚠  Overpass HTTP {e.code}")
            break
        except Exception as e:
            print(f"    ⚠  Overpass error: {e}")
            break

    name = None
    if result:
        elements = result.get("elements", [])
        if elements:
            best = min(elements, key=lambda el: haversine_m(lon, lat, el["lon"], el["lat"]))
            tags = best.get("tags", {})
            name = tags.get("name:de") or tags.get("name")

    # Cache result (including None = no peak found, avoids re-querying)
    cache[cache_key] = name
    _save_cache(cache)
    return name, False   # fetched from API — caller should sleep


# ── Parse ISO 8601 time ───────────────────────────────────────────────────────
def parse_time(s: str) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ── Parse a single GPX file ───────────────────────────────────────────────────
def parse_gpx(path: Path) -> tuple[dict, dict] | None:
    """
    Returns (geojson_feature, stats_dict) or None on failure.
    """
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        tag  = root.tag
        ns   = tag[1:tag.index("}")] if "{" in tag else ""
        pfx  = f"{{{ns}}}" if ns else ""

        # ── Name ──────────────────────────────────────────────────────────────
        # Prefer track name > metadata name > filename
        trk_name_el = root.find(f".//{pfx}trk/{pfx}name")
        meta_name_el = root.find(f".//{pfx}metadata/{pfx}name")
        top_name_el  = root.find(f"{pfx}name")
        for el in (trk_name_el, meta_name_el, top_name_el):
            if el is not None and el.text and el.text.strip():
                name = el.text.strip()
                break
        else:
            name = path.stem.replace("_", " ").replace("-", " ")

        # ── Track points ──────────────────────────────────────────────────────
        raw_pts = []   # (lon, lat, ele_or_None, time_or_None)

        for trkpt in root.iter(f"{pfx}trkpt"):
            lon = float(trkpt.attrib["lon"])
            lat = float(trkpt.attrib["lat"])
            ele_el  = trkpt.find(f"{pfx}ele")
            time_el = trkpt.find(f"{pfx}time")
            ele  = float(ele_el.text)  if ele_el  is not None and ele_el.text  else None
            t    = parse_time(time_el.text) if time_el is not None else None
            raw_pts.append((lon, lat, ele, t))

        # Route points fallback (no elevation/time usually)
        if not raw_pts:
            for rtept in root.iter(f"{pfx}rtept"):
                lon = float(rtept.attrib["lon"])
                lat = float(rtept.attrib["lat"])
                raw_pts.append((lon, lat, None, None))

        if len(raw_pts) < 2:
            return None

        # ── GeoJSON coordinates (include elevation if available) ──────────────
        coords = []
        for lon, lat, ele, _ in raw_pts:
            coords.append([lon, lat, ele] if ele is not None else [lon, lat])

        # ── Stats ─────────────────────────────────────────────────────────────
        elevations = [p[2] for p in raw_pts if p[2] is not None]
        times      = [p[3] for p in raw_pts if p[3] is not None]

        # Distance
        dist_m = sum(
            haversine_m(raw_pts[i][0], raw_pts[i][1],
                        raw_pts[i+1][0], raw_pts[i+1][1])
            for i in range(len(raw_pts) - 1)
        )

        # Elevation gain/loss (smooth with 3-pt window to reduce GPS noise)
        gain = loss = 0.0
        if len(elevations) >= 3:
            smoothed = [
                (elevations[max(0,i-1)] + elevations[i] + elevations[min(len(elevations)-1,i+1)]) / 3
                for i in range(len(elevations))
            ]
            for i in range(1, len(smoothed)):
                d = smoothed[i] - smoothed[i-1]
                if d > 0:
                    gain += d
                else:
                    loss += abs(d)

        # Summit — trackpoint with highest elevation
        summit_lat = summit_lon = None
        if elevations:
            max_ele = max(elevations)
            for lon, lat, ele, _ in raw_pts:
                if ele == max_ele:
                    summit_lat, summit_lon = lat, lon
                    break

        # Duration from GPS timestamps
        duration_min = None
        if len(times) >= 2:
            duration_min = round((times[-1] - times[0]).total_seconds() / 60)

        stats = {
            "file":              path.name,
            "name":              name,
            "distance_km":       round(dist_m / 1000, 2),
            "elevation_gain_m":  round(gain),
            "elevation_loss_m":  round(loss),
            "max_altitude_m":    round(max(elevations)) if elevations else None,
            "min_altitude_m":    round(min(elevations)) if elevations else None,
            "duration_min":      duration_min,
            "points":            len(raw_pts),
            "summit_lat":        summit_lat,
            "summit_lon":        summit_lon,
        }

        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]

        feature = {
            "type": "Feature",
            "properties": {
                "name":  name,
                "file":  path.name,
                **{k: stats[k] for k in (
                    "distance_km","elevation_gain_m","elevation_loss_m",
                    "max_altitude_m","min_altitude_m","duration_min",
                    "summit_lat","summit_lon"
                )},
            },
            "geometry": {
                "type":        "LineString",
                "coordinates": coords,
            },
        }

        return feature, stats

    except Exception as e:
        print(f"  ✗ {path.name}: {e}")
        return None


# ── Slugify + content hash ───────────────────────────────────────────────────
def slugify(text: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "-", text).strip("-")[:50]

def track_hash(raw_pts: list) -> str:
    """Stable 6-char hash from evenly-spaced track points.
    Uses 8 samples to distinguish loops with same start/end."""
    n = len(raw_pts)
    idxs = [int(i * (n-1) / 7) for i in range(8)] if n >= 8 else list(range(n))
    sample = [raw_pts[i] for i in idxs]
    key = "|".join(f"{lon:.5f},{lat:.5f}" for lon, lat, *_ in sample)
    return hashlib.sha256(key.encode()).hexdigest()[:6]

def make_file_slug(name: str, raw_pts: list) -> str:
    """Readable slug + hash suffix: e.g. 'zugspitze-a3f8c1'"""
    base = slugify(name) or "hike"
    return f"{base}-{track_hash(raw_pts)}"


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    gpx_files = sorted(GPX_DIR.glob("**/*.gpx"))
    empty_fc  = {"type": "FeatureCollection", "features": []}

    if not gpx_files:
        print(f"No GPX files found in {GPX_DIR}/")
        OUTPUT.write_text(json.dumps(empty_fc))
        STATS_OUT.write_text(json.dumps({}))
        return

    features = []
    stats_map = {}   # filename → stats dict

    for i, path in enumerate(gpx_files):
        print(f"  ⚙  {path.name}…", end=" ", flush=True)
        result = parse_gpx(path)
        if result:
            feat, stats = result

            # Look up nearest OSM peak name for the summit point
            if stats["summit_lat"] and stats["summit_lon"]:
                peak_name, from_cache = nearest_peak_name(stats["summit_lat"], stats["summit_lon"])
                if not from_cache:
                    time.sleep(OVERPASS_SLEEP)   # rate limit — only for real API calls
                if peak_name:
                    print(f"\n    🏔  OSM peak: {peak_name!r}", end=" ")
                    stats["osm_peak_name"] = peak_name
                    # Use OSM peak name as hike name if GPX name looks like a filename
                    gpx_name = stats["name"]
                    if not gpx_name or gpx_name == path.stem.replace("_"," ").replace("-"," "):
                        stats["name"] = peak_name
                        feat["properties"]["name"] = peak_name
                else:
                    stats["osm_peak_name"] = None

            features.append(feat)
            stats_map[path.name] = stats
            dur = f"  {stats['duration_min']}min" if stats['duration_min'] else ""
            print(f"✓  {stats['name']!r}  "
                  f"{stats['distance_km']}km  "
                  f"+{stats['elevation_gain_m']}m  "
                  f"max {stats['max_altitude_m']}m{dur}")
        else:
            print("✗")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(
        {"type": "FeatureCollection", "features": features},
        separators=(",", ":")
    ))
    STATS_OUT.write_text(json.dumps(stats_map, indent=2))
    print(f"\n✅ {len(features)} tracks → {OUTPUT}")
    print(f"✅ Stats → {STATS_OUT}")


if __name__ == "__main__":
    main()
