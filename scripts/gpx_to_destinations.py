#!/usr/bin/env python3
"""
scripts/gpx_to_destinations.py
Reads all GPX files in a folder, extracts start + end points,
snaps each to the nearest GTFS stop (from CH + BY feeds),
and writes destinations.csv for fetch_isochrones.py.

Usage:
    python scripts/gpx_to_destinations.py

Expects:
    docs/gpx/       ← folder of .gpx files
    gtfs/*/         ← unzipped GTFS per location

Downloads GTFS automatically if not present.
"""

import csv
import hashlib
import math
import xml.etree.ElementTree as ET
from pathlib import Path

from gtfs_common import GTFS_FEEDS, GTFS_DIR, ensure_gtfs

# ── Config ────────────────────────────────────────────────────────────────────
ROOT             = Path(__file__).parent.parent
# input
GPX_DIR          = ROOT / "docs" / "gpx"
GPX_STATS_JSON   = ROOT / "docs" / "gpx_stats.json"
# output
OUTPUT_CSV       = ROOT / "docs" / "destinations.csv"
HIKES_JSON       = ROOT / "docs" / "hikes.json"

MAX_SNAP_DIST_KM = 4.0    # ignore stops further than this (likely wrong region)
RAIL_PREFER_KM   = 0.25   # prefer a rail stop within this distance over nearest bus stop


# ── Haversine distance (km) ───────────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ── Simple k-d tree (2D, lat/lon) ────────────────────────────────────────────
class KDNode:
    __slots__ = ("stop", "left", "right")
    def __init__(self, stop, left=None, right=None):
        self.stop  = stop   # dict with lat, lon, ...
        self.left  = left
        self.right = right

def _build(points, depth=0):
    if not points:
        return None
    axis = depth % 2  # 0 = lat, 1 = lon
    key  = "lat" if axis == 0 else "lon"
    points.sort(key=lambda p: p[key])
    mid = len(points) // 2
    return KDNode(
        points[mid],
        _build(points[:mid], depth + 1),
        _build(points[mid + 1:], depth + 1),
    )

def _nearest(node, target_lat, target_lon, depth=0, best=None):
    if node is None:
        return best
    stop = node.stop
    dist = haversine(target_lat, target_lon, stop["lat"], stop["lon"])
    if best is None or dist < best[0]:
        best = (dist, stop)
    axis     = depth % 2
    diff     = (target_lat - stop["lat"]) if axis == 0 else (target_lon - stop["lon"])
    near, far = (node.left, node.right) if diff <= 0 else (node.right, node.left)
    best = _nearest(near, target_lat, target_lon, depth + 1, best)
    # Check if far branch could contain a closer point
    if abs(diff) * 111 < best[0]:  # rough km conversion
        best = _nearest(far, target_lat, target_lon, depth + 1, best)
    return best

class KDTree:
    def __init__(self, stops):
        self._root  = _build(list(stops))
        # Separate tree for rail-only stops (for preference lookup)
        rail_stops  = [s for s in stops if s.get("has_rail")]
        self._rail  = _build(list(rail_stops)) if rail_stops else None

    def nearest(self, lat, lon):
        result = _nearest(self._root, lat, lon)
        return result  # (distance_km, stop_dict) or None

    def nearest_rail(self, lat, lon):
        """Return nearest stop served by a rail route, or (inf, None)."""
        if self._rail is None:
            return float("inf"), None
        result = _nearest(self._rail, lat, lon)
        return result if result else (float("inf"), None)


# GTFS route_type codes for rail modes
RAIL_ROUTE_TYPES = {
    1,   # Subway/Metro
    2,   # Rail (intercity, regional)
    4,   # Ferry
    5,   # Cable tram
    6,   # Aerial lift
    7,   # Funicular
    11,  # Trolleybus
    100, # Railway Service (extended)
    101, # High-Speed Rail
    102, # Long Distance Rail
    103, # Inter Regional Rail
    104, # Car Transport Rail
    105, # Sleeper Rail
    106, # Regional Rail
    107, # Tourist Railway
    108, # Rail Shuttle
    109, # Suburban Railway
}


def load_stop_route_types(region) -> dict:
    """
    Build a mapping stop_id → set of route_types served at that stop.
    Uses routes.txt + trips.txt + stop_times.txt.
    Falls back to empty dict if files are missing (stops still usable, just untyped).
    """
    d = GTFS_FEEDS[region]["dir"]
    route_type = {}   # route_id → int
    try:
        with open(d / "routes.txt", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    route_type[row["route_id"]] = int(row["route_type"])
                except (KeyError, ValueError):
                    pass
    except FileNotFoundError:
        return {}

    trip_route = {}   # trip_id → route_id
    try:
        with open(d / "trips.txt", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                trip_route[row["trip_id"]] = row.get("route_id", "")
    except FileNotFoundError:
        return {}

    stop_types: dict[str, set] = {}
    try:
        with open(d / "stop_times.txt", newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                tid = row.get("trip_id", "")
                sid = row.get("stop_id", "")
                rid = trip_route.get(tid, "")
                rt  = route_type.get(rid)
                if rt is not None:
                    stop_types.setdefault(sid, set()).add(rt)
    except FileNotFoundError:
        return {}

    return stop_types


def load_stops(region):
    """Load stops.txt for a region, return list of dicts with route_types."""
    stops_file = GTFS_FEEDS[region]["dir"] / "stops.txt"
    if not stops_file.exists():
        print(f"  ✗ stops.txt not found for {region} at {stops_file}")
        return []

    print(f"  Loading {region} stop route types…", end=" ", flush=True)
    stop_types = load_stop_route_types(region)
    print(f"{len(stop_types):,} typed")

    stops = []
    with open(stops_file, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                types = stop_types.get(row["stop_id"], set())
                sid = row["stop_id"]
                # Skip parent stations — they have averaged coordinates and
                # no trips directly; child platform stops are used instead
                if sid.startswith("Parent") or sid.startswith("parent"):
                    continue
                stops.append({
                    "id":          sid,
                    "name":        row["stop_name"],
                    "lat":         float(row["stop_lat"]),
                    "lon":         float(row["stop_lon"]),
                    "region":      region,
                    "has_rail":    bool(types & RAIL_ROUTE_TYPES),
                })
            except (KeyError, ValueError):
                continue
    print(f"  ✓ Loaded {len(stops):,} stops for {region}")
    return stops


# ── GPX parsing ───────────────────────────────────────────────────────────────
NS = {
    "gpx":  "http://www.topografix.com/GPX/1/1",
    "gpx10":"http://www.topografix.com/GPX/1/0",
}

def parse_gpx(path: Path):
    """Return (start_lat, start_lon, end_lat, end_lon) or None."""
    try:
        tree = ET.parse(path)
        root = tree.getroot()

        # Detect namespace
        tag = root.tag  # e.g. {http://www.topografix.com/GPX/1/1}gpx
        ns  = tag[1:tag.index("}")] if "{" in tag else ""
        pfx = f"{{{ns}}}" if ns else ""

        points = []

        # Try trkpt (track points) first
        for trkpt in root.iter(f"{pfx}trkpt"):
            lat = float(trkpt.attrib["lat"])
            lon = float(trkpt.attrib["lon"])
            points.append((lat, lon))

        # Fall back to wpt (waypoints)
        if not points:
            for wpt in root.iter(f"{pfx}wpt"):
                lat = float(wpt.attrib["lat"])
                lon = float(wpt.attrib["lon"])
                points.append((lat, lon))

        # Fall back to rtept (route points)
        if not points:
            for rtept in root.iter(f"{pfx}rtept"):
                lat = float(rtept.attrib["lat"])
                lon = float(rtept.attrib["lon"])
                points.append((lat, lon))

        if not points:
            print(f"  ✗ No points found in {path.name}")
            return None

        # Extract GPX name (same priority order as gpx_to_geojson.py)
        gpx_name = None
        for tag_path in (f".//{pfx}trk/{pfx}name", f".//{pfx}metadata/{pfx}name", f"{pfx}name"):
            el = root.find(tag_path)
            if el is not None and el.text and el.text.strip():
                gpx_name = el.text.strip(); break
        return points[0], points[-1], gpx_name, points

    except Exception as e:
        print(f"  ✗ Failed to parse {path.name}: {e}")
        return None


# ── Slug for destination ID ───────────────────────────────────────────────────
def slugify(text):
    import unicodedata, re
    text = unicodedata.normalize("NFKD", str(text))
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "-", text).strip("-")[:50]


def track_hash_from_points(points: list) -> str:
    """Stable 6-char hash from evenly-spaced track points.
    Uses 8 samples to distinguish loops with same start/end."""
    n = len(points)
    idxs = [int(i * (n-1) / 7) for i in range(8)] if n >= 8 else list(range(n))
    sample = [points[i] for i in idxs]
    key = "|".join(f"{lon:.5f},{lat:.5f}" for lat, lon in sample)
    return hashlib.sha256(key.encode()).hexdigest()[:6]


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # 1. Ensure GTFS data
    print("Checking GTFS feeds…")
    GTFS_DIR.mkdir(parents=True, exist_ok=True)
    for region in GTFS_FEEDS:
        ensure_gtfs(region)

    # 2. Load + index all stops
    print("\nBuilding spatial index…")
    all_stops = []
    for region in GTFS_FEEDS:  # load all available feeds
        all_stops.extend(load_stops(region))

    if not all_stops:
        print("ERROR: No stops loaded. Check GTFS download.")
        return

    tree = KDTree(all_stops)
    print(f"  ✓ Index built ({len(all_stops):,} stops total)")

    # 3. Parse GPX files
    gpx_files = sorted(GPX_DIR.glob("**/*.gpx")) if GPX_DIR.exists() else []
    if not gpx_files:
        print(f"\nNo GPX files found in {GPX_DIR}/")
        print("Create the gpx/ folder and add your .gpx files.")
        return

    print(f"\nProcessing {len(gpx_files)} GPX file(s)…")

    # Load GPX stats produced by gpx_to_geojson.py (name, elevation, etc.)
    gpx_stats = {}
    if GPX_STATS_JSON.exists():
        import json as _json
        gpx_stats = _json.loads(GPX_STATS_JSON.read_text())
    else:
        print("  ⚠  gpx_stats.json not found — run gpx_to_geojson.py first for rich names")

    destinations = {}   # stop_id → dest dict (deduplicates across routes)
    hikes        = []   # list of hike metadata with linked stop_dest_ids
    _seen_ids: set = set()

    def _unique_id(base: str, seen: set) -> str:
        """Return base if unseen, else base_2, base_3, …"""
        if base not in seen:
            seen.add(base)
            return base
        n = 2
        while f"{base}_{n}" in seen:
            n += 1
        uid = f"{base}_{n}"
        seen.add(uid)
        return uid

    for gpx_path in gpx_files:
        print(f"\n  📍 {gpx_path.name}")
        result = parse_gpx(gpx_path)
        if not result:
            continue

        start_pt, end_pt, gpx_name_raw, all_pts = result
        candidates = []  # (label, stop)

        for label, (lat, lon) in [("start", start_pt), ("end", end_pt)]:
            dist, stop = tree.nearest(lat, lon)
            if dist > MAX_SNAP_DIST_KM:
                print(f"    ⚠  {label}: nearest stop is {dist:.1f} km away ({stop['name']}) — skipping")
                continue

            # If nearest stop is a bus stop, check whether there is a rail stop
            # within RAIL_PREFER_KM — if so, prefer it (avoids snapping to bus
            # stops at stations that also have trains, e.g. Airolo, Stazione vs Airolo)
            if not stop.get("has_rail"):
                rail_dist, rail_stop = tree.nearest_rail(lat, lon)
                if rail_stop and rail_dist <= RAIL_PREFER_KM and rail_dist <= MAX_SNAP_DIST_KM:
                    print(f"    {label} → {rail_stop['name']} ({rail_stop['region']}, "
                          f"{rail_dist:.2f} km) [rail preferred over {stop['name']} bus stop]")
                    stop = rail_stop
                    dist = rail_dist

            print(f"    {label} → {stop['name']} ({stop['region']}, {dist:.2f} km)"
                  f"{" [rail]" if stop.get('has_rail') else ""}")
            candidates.append((label, stop))

        # Register stops as destinations (deduplicated globally)
        # Track which role (start/end/both) each stop plays for this hike
        stop_dest_ids = []
        stop_roles = {}   # dest_id → "start" | "end" | "both"
        seen_ids = set()
        for label, stop in candidates:
            sid = stop["id"]
            dest_id = slugify(stop["name"]) + "_" + slugify(sid)
            if sid not in seen_ids:
                seen_ids.add(sid)
                if sid not in destinations:
                    destinations[sid] = {
                        "id":     dest_id,
                        "name":   stop["name"],
                        "lat":    stop["lat"],
                        "lon":    stop["lon"],
                        "region": stop["region"],
                    }
                stop_dest_ids.append(dest_id)
                stop_roles[dest_id] = label   # "start" or "end"
            else:
                # Same stop for both start and end
                stop_roles[dest_id] = "both"

        # Compute centroid of track for map dot fallback
        (start_lat, start_lon) = start_pt
        (end_lat, end_lon)     = end_pt
        mid_lat = (start_lat + end_lat) / 2
        mid_lon = (start_lon + end_lon) / 2

        # Look up stats using filename (exact key from gpx_to_geojson.py output)
        # gpx_to_geojson.py stores stats with filename as key: "zugspitze.gpx" → {...}
        stats = gpx_stats.get(gpx_path.name, {})
        
        if stats:
            print(f"    ✓ Found stats for {gpx_path.name}")
        else:
            print(f"    ⚠  No stats found in gpx_stats.json for {gpx_path.name}")
        
        # Priority: OSM peak name > GPX <name> tag > filename
        hike_name = (stats.get("osm_peak_name")
                     or stats.get("name")
                     or gpx_path.stem.replace("_"," ").replace("-"," "))
        # Use summit as the map dot; fall back to centroid if no elevation data
        dot_lat = stats.get("summit_lat") or mid_lat
        dot_lon = stats.get("summit_lon") or mid_lon

        # Build stop list with roles for the frontend
        stops_info = [
            {
                "dest_id": did,
                "role":    stop_roles[did],
            }
            for did in stop_dest_ids
        ]

        # For hike ID: use name+hash slug for uniqueness (even if stats missing)
        _name_for_slug = gpx_name_raw or gpx_path.stem
        _hash = track_hash_from_points(all_pts)
        _slug_key = f"{slugify(_name_for_slug)}-{_hash}"

        hikes.append({
            # ID: slug+hash for stability (doesn't depend on stats)
            "id":               _unique_id(_slug_key, _seen_ids),
            "name":             hike_name,
            "distance_km":      stats.get("distance_km"),
            "elevation_gain_m": stats.get("elevation_gain_m"),
            "elevation_loss_m": stats.get("elevation_loss_m"),
            "max_altitude_m":   stats.get("max_altitude_m"),
            "min_altitude_m":   stats.get("min_altitude_m"),
            "duration_min":      stats.get("duration_min"),
            "osm_peak_name":    stats.get("osm_peak_name"),
            "file":             gpx_path.name,
            "lat":              dot_lat,
            "lon":              dot_lon,
            "start_lat":        start_lat,
            "start_lon":        start_lon,
            "end_lat":          end_lat,
            "end_lon":          end_lon,
            "stop_dest_ids":    stop_dest_ids,
            "stops":            stops_info,   # [{dest_id, role}, …]
        })

    if not destinations:
        print("\nNo destinations found.")
        return

    # 4. Write destinations.csv
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name", "lat", "lon", "region"])
        writer.writeheader()
        writer.writerows(destinations.values())

    # 5. Write hikes.json
    import json
    HIKES_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(HIKES_JSON, "w", encoding="utf-8") as f:
        json.dump(hikes, f, indent=2)

    print(f"\n✅ Written {len(destinations)} destinations to {OUTPUT_CSV}")
    print(f"✅ Written {len(hikes)} hikes to {HIKES_JSON}")
    print("   Run fetch_isochrones.py next.")


if __name__ == "__main__":
    main()
