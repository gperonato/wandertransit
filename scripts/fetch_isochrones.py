#!/usr/bin/env python3
"""
scripts/fetch_isochrones.py
Computes reverse transit isochrones (30–180 min in 30-min steps) from GTFS files.

Algorithm: reverse RAPTOR (Round-based Public Transit Routing)
  — finds all stops that can reach a target stop within MAX_DURATION
  — outputs a GeoJSON polygon (alpha shape of reachable stop coordinates)

Usage:
    pip install shapely
    python scripts/fetch_isochrones.py

GTFS feeds are downloaded automatically into gtfs/ if not present.
"""

import csv
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from shapely.geometry import MultiPoint, mapping
    from shapely.ops import unary_union
except ImportError:
    print("ERROR: shapely not installed. Run: pip install shapely")
    sys.exit(1)

from gtfs_common import GTFS_FEEDS, GTFS_DIR, GTFS_FILES_NEEDED, ensure_gtfs

# ── Config ────────────────────────────────────────────────────────────────────
ROOT             = Path(__file__).parent.parent
DESTINATIONS_CSV = ROOT / "docs" / "destinations.csv"
OUTPUT_DIR       = ROOT / "docs" / "isochrones"
# Isochrone bands: 30, 60, 90, 120, 150, 180 minutes
CUTOFFS_SEC      = [t * 60 for t in range(30, 181, 30)]
MAX_DURATION     = max(CUTOFFS_SEC)  # 180 minutes — RAPTOR runs once to full depth
MAX_WALK_SEC     = 10 * 60          # 10 min walk to/from a stop
WALK_SPEED_MS    = 1.2              # metres per second
MAX_RAPTOR_ROUNDS= 6                # max transit legs (more for longer journeys)
MIN_TRANSFER_SEC = 3 * 60           # minimum interchange time between two vehicles
SEARCH_TIME_SEC  = 10.5 * 3600      # 10:30:00 — arrive at destination by this
ALPHA            = 0.03             # alpha-shape tightness (lower = tighter)

# Walk distance for cross-region transfers at border stations (metres)
CROSS_REGION_WALK_M = 400   # wider radius to merge border stations

GTFS_FILES_NEEDED = ["stops.txt", "stop_times.txt", "trips.txt",
                     "calendar.txt", "calendar_dates.txt", "routes.txt"]


# ── Haversine ─────────────────────────────────────────────────────────────────
def haversine_m(lat1, lon1, lat2, lon2):
    import math
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ── GTFS loader ───────────────────────────────────────────────────────────────
class GTFSData:
    """Loads and indexes the GTFS tables needed for RAPTOR."""

    def __init__(self, region: str):
        d = GTFS_FEEDS[region]["dir"]
        self.search_date = GTFS_FEEDS[region]["search_date"]

        print(f"  Loading {region} stops…", end=" ", flush=True)
        self.stops = self._load_stops(d / "stops.txt")
        print(f"{len(self.stops):,}")

        print(f"  Loading {region} calendar ({self.search_date})…", end=" ", flush=True)
        self.active_services = self._load_calendar(
            d / "calendar.txt",
            d / "calendar_dates.txt",
            self.search_date)
        print(f"{len(self.active_services):,} active services")

        print(f"  Loading {region} trips…", end=" ", flush=True)
        self.trips = self._load_trips(d / "trips.txt", self.active_services)
        print(f"{len(self.trips):,} active trips")

        print(f"  Loading {region} stop_times…", end=" ", flush=True)
        self.stop_times, self.trips_at_stop = self._load_stop_times(
            d / "stop_times.txt", self.trips)
        print(f"{sum(len(v) for v in self.trips_at_stop.values()):,} arrivals indexed")

        print(f"  Building {region} transfer graph…", end=" ", flush=True)
        self.transfers = self._build_transfers(self.stops)
        print(f"{sum(len(v) for v in self.transfers.values()):,} transfer pairs")

    # ── stops.txt ─────────────────────────────────────────────────────────────
    @staticmethod
    def _load_stops(path):
        stops = {}
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    stops[row["stop_id"]] = {
                        "lat": float(row["stop_lat"]),
                        "lon": float(row["stop_lon"]),
                        "name": row.get("stop_name", ""),
                    }
                except (ValueError, KeyError):
                    continue
        return stops

    # ── calendar.txt + calendar_dates.txt ────────────────────────────────────
    @staticmethod
    def _load_calendar(cal_path, cal_dates_path, date_str):
        """
        Return set of service_ids active on date_str (YYYYMMDD).

        NOTE: The SBB CH feed uses calendar_dates.txt in a non-standard way:
        - type=2 entries are the ACTUAL operating days (inclusion list)
        - type=1 entries are rare overrides
        - calendar.txt contains only a broad skeleton

        To handle both standard and SBB-style feeds correctly, we include ALL
        service_ids that appear in calendar_dates.txt for the search date,
        regardless of exception_type. This is safe because:
        - Standard feeds: type=1 adds are correctly included; type=2 removals
          are also included, but those services wouldn't have been active via
          calendar.txt anyway if they're being "removed" (net effect: neutral)
        - SBB feeds: type=2 "operating day" entries are correctly included
        """
        dt = datetime.strptime(date_str, "%Y%m%d")
        day_col = ["monday","tuesday","wednesday","thursday",
                   "friday","saturday","sunday"][dt.weekday()]

        # Step 1: regular schedule from calendar.txt
        active = set()
        with open(cal_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    start = datetime.strptime(row["start_date"], "%Y%m%d")
                    end   = datetime.strptime(row["end_date"],   "%Y%m%d")
                    if start <= dt <= end and row.get(day_col, "0") == "1":
                        active.add(row["service_id"])
                except (ValueError, KeyError):
                    continue

        # Step 2: include ALL calendar_dates entries for this date
        # regardless of exception_type (handles both standard and SBB-style feeds)
        if cal_dates_path.exists():
            with open(cal_dates_path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if row.get("date") == date_str:
                        active.add(row.get("service_id", ""))

        return active

    # ── trips.txt ─────────────────────────────────────────────────────────────
    @staticmethod
    def _load_trips(path, active_services):
        trips = {}
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("service_id") in active_services:
                    trips[row["trip_id"]] = row.get("route_id", "")
        return trips

    # ── stop_times.txt ────────────────────────────────────────────────────────
    @staticmethod
    def _load_stop_times(path, active_trips):
        """
        stop_times[trip_id] = sorted list of (stop_sequence, stop_id, arrival_sec, departure_sec)
        trips_at_stop[stop_id] = list of (departure_sec, trip_id, stop_sequence)

        We use departure_time for backward scanning labels: a passenger can board
        a vehicle up until it departs, not just when it arrives. This matters at
        interchange stops where arrival and departure times differ (e.g. Arth-Goldau:
        train arrives 07:45, departs 07:54 — the 9-minute dwell is boarding time).
        """
        def hms(s):
            h, m, sec = s.strip().split(":")
            return int(h) * 3600 + int(m) * 60 + int(sec)

        raw = defaultdict(list)
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                tid = row.get("trip_id", "")
                if tid not in active_trips:
                    continue
                try:
                    arr = hms(row["arrival_time"])
                    # departure_time may be missing or equal to arrival_time
                    dep_str = row.get("departure_time", "").strip()
                    dep = hms(dep_str) if dep_str else arr
                    raw[tid].append((
                        int(row["stop_sequence"]),
                        row["stop_id"],
                        arr,
                        dep,
                    ))
                except (ValueError, KeyError):
                    continue

        stop_times = {}
        trips_at_stop = defaultdict(list)
        for tid, entries in raw.items():
            entries.sort()
            stop_times[tid] = entries
            for seq, sid, arr, dep in entries:
                # Index by departure_time: a train is boardable until it departs
                trips_at_stop[sid].append((dep, tid, seq))

        # Sort each stop's trip list by departure time for fast lookup
        for sid in trips_at_stop:
            trips_at_stop[sid].sort()

        return stop_times, trips_at_stop

    # ── Transfer graph (walking between nearby stops) ─────────────────────────
    @staticmethod
    def _build_transfers(stops, max_walk_m=500):
        """
        Build walking transfer graph using a grid spatial index.
        O(n log n) instead of O(n²) — fast even for 50k+ stops.
        """
        stop_list = list(stops.items())

        # Bucket size in degrees (~max_walk_m metres)
        lat_deg = max_walk_m / 111_000
        lon_deg = max_walk_m / 111_000  # conservative (worst case at equator)

        # Assign each stop to a grid cell
        grid = defaultdict(list)
        for idx, (sid, s) in enumerate(stop_list):
            cell = (int(s["lat"] / lat_deg), int(s["lon"] / lon_deg))
            grid[cell].append(idx)

        transfers = defaultdict(list)

        for idx_a, (sid_a, s_a) in enumerate(stop_list):
            cell_lat = int(s_a["lat"] / lat_deg)
            cell_lon = int(s_a["lon"] / lon_deg)

            # Only check stops in the 3×3 neighbouring cells
            for dlat in (-1, 0, 1):
                for dlon in (-1, 0, 1):
                    for idx_b in grid[(cell_lat + dlat, cell_lon + dlon)]:
                        if idx_b <= idx_a:
                            continue
                        sid_b, s_b = stop_list[idx_b]
                        dist = haversine_m(
                            s_a["lat"], s_a["lon"],
                            s_b["lat"], s_b["lon"],
                        )
                        if dist <= max_walk_m:
                            walk_sec = int(dist / WALK_SPEED_MS)
                            transfers[sid_a].append((sid_b, walk_sec))
                            transfers[sid_b].append((sid_a, walk_sec))

        return transfers

    @classmethod
    def merge(cls, region_objects: dict) -> "GTFSData":
        """
        Merge multiple per-region GTFSData objects into one unified graph.
        Cross-region walking transfers are added between stops within
        CROSS_REGION_WALK_M of each other across different regions.
        """
        merged = object.__new__(cls)
        merged.stops         = {}
        merged.trips         = {}
        merged.stop_times    = {}
        merged.trips_at_stop = defaultdict(list)
        merged.transfers     = defaultdict(list)
        merged.trip_date     = {}   # trip_id → search_date string for that feed

        # Merge all tables — stop/trip IDs from different feeds can collide,
        # so prefix each with the region code.
        for region, gtfs in region_objects.items():
            for sid, s in gtfs.stops.items():
                merged.stops[f"{region}:{sid}"] = s

            for tid, route in gtfs.trips.items():
                merged.trips[f"{region}:{tid}"] = route
                merged.trip_date[f"{region}:{tid}"] = gtfs.search_date

            for tid, entries in gtfs.stop_times.items():
                merged.stop_times[f"{region}:{tid}"] = [
                    (seq, f"{region}:{sid}", arr, dep) for seq, sid, arr, dep in entries
                ]

            for sid, arrivals in gtfs.trips_at_stop.items():
                merged.trips_at_stop[f"{region}:{sid}"].extend([
                    (arr, f"{region}:{tid}", seq) for arr, tid, seq in arrivals
                ])

            for sid, neighbours in gtfs.transfers.items():
                merged.transfers[f"{region}:{sid}"].extend([
                    (f"{region}:{nb}", walk) for nb, walk in neighbours
                ])

        # Sort trips_at_stop after merge
        for sid in merged.trips_at_stop:
            merged.trips_at_stop[sid].sort()

        # Cross-region walking transfers
        print(f"  Building cross-region transfers…", end=" ", flush=True)
        regions = list(region_objects.keys())
        cross = 0
        for i, r_a in enumerate(regions):
            stops_a = [(f"{r_a}:{sid}", s) for sid, s in region_objects[r_a].stops.items()]
            for r_b in regions[i+1:]:
                stops_b = [(f"{r_b}:{sid}", s) for sid, s in region_objects[r_b].stops.items()]
                # Grid index for stops_b
                lat_deg = CROSS_REGION_WALK_M / 111_000
                lon_deg = CROSS_REGION_WALK_M / 111_000
                grid_b = defaultdict(list)
                for idx, (sid, s) in enumerate(stops_b):
                    cell = (int(s["lat"] / lat_deg), int(s["lon"] / lon_deg))
                    grid_b[cell].append(idx)
                # Find pairs
                for sid_a, s_a in stops_a:
                    cell_lat = int(s_a["lat"] / lat_deg)
                    cell_lon = int(s_a["lon"] / lon_deg)
                    for dlat in (-1, 0, 1):
                        for dlon in (-1, 0, 1):
                            for idx_b in grid_b.get((cell_lat+dlat, cell_lon+dlon), []):
                                sid_b, s_b = stops_b[idx_b]
                                dist = haversine_m(s_a["lat"], s_a["lon"],
                                                   s_b["lat"], s_b["lon"])
                                if dist <= CROSS_REGION_WALK_M:
                                    walk_sec = max(int(dist / WALK_SPEED_MS),
                                                   MIN_TRANSFER_SEC)
                                    merged.transfers[sid_a].append((sid_b, walk_sec))
                                    merged.transfers[sid_b].append((sid_a, walk_sec))
                                    cross += 1

        print(f"{cross} cross-region pairs")
        return merged


# ── Reverse RAPTOR ────────────────────────────────────────────────────────────
def reverse_raptor(target_stop_id: str, gtfs: GTFSData,
                   arrive_by_sec: int, max_duration_sec: int) -> dict[str, tuple]:
    """
    Find all stops that can reach `target_stop_id` by `arrive_by_sec`.

    Returns dict: stop_id → (dep_at_origin, arr_at_target)
      dep_at_origin  = latest departure time from this stop
      arr_at_target  = actual arrival time at the target stop

    Travel time = arr_at_target - dep_at_origin
    This is the real journey duration, not the slack before the deadline.

    Example: ZHB dep=07:05 → Airolo arr=09:01 → travel = 116min (not 175min)
    """
    INF = -1
    latest_dep = defaultdict(lambda: INF)
    arr_at_target = {}
    # Stops that can walk to the target — includes:
    # - Platform stops at the same station (intra-region, short walk)
    # - Cross-region equivalents (e.g. DE:4084 ↔ AT-7:at:47:2231:0:2 at Scharnitz)
    # Threshold: 5 min walk (300s) — catches same-station platforms and border stops
    target_stops = {target_stop_id} | {
        nb for nb, walk in gtfs.transfers.get(target_stop_id, [])
        if walk <= 300
    }

    latest_dep[target_stop_id] = arrive_by_sec
    arr_at_target[target_stop_id] = arrive_by_sec  # target arrives "at itself"
    walk_only = set()  # stops labelled only by walk from target, not by any trip

    # Walking from target
    for nb_stop, walk_sec in gtfs.transfers.get(target_stop_id, []):
        depart = arrive_by_sec - walk_sec
        if depart > INF and depart >= arrive_by_sec - max_duration_sec:
            latest_dep[nb_stop] = max(latest_dep[nb_stop], depart)
            # Walking to target: arrives at target at arrive_by_sec
            if nb_stop not in arr_at_target or arrive_by_sec < arr_at_target[nb_stop]:
                arr_at_target[nb_stop] = arrive_by_sec
            walk_only.add(nb_stop)

    # RAPTOR rounds
    for _round in range(MAX_RAPTOR_ROUNDS):
        improved = {}         # stop → (dep, arr)
        improved_arr = {}     # stop → arr_at_target for this improvement

        for stop_id, dep_time in list(latest_dep.items()):
            if dep_time == INF:
                continue

            is_target = (stop_id == target_stop_id)
            effective_dep = dep_time if is_target else dep_time - MIN_TRANSFER_SEC

            # arr_at_target for stop_id: when does this connection reach the target?
            stop_arr = arr_at_target.get(stop_id, arrive_by_sec)

            for board_dep, trip_id, seq in gtfs.trips_at_stop.get(stop_id, []):
                if board_dep > effective_dep:
                    continue

                trip_stops = gtfs.stop_times.get(trip_id, [])

                # Find when this trip reaches the target.
                # Case 1: boarding stop IS the target → use its arrival at seq.
                # Case 2: target is downstream → scan strictly forward (> seq).
                # Case 3: inherit stop_arr from a previous trip leg.
                # IMPORTANT: only inherit arrive_by_sec (deadline) from walk-labelled
                # stops if the trip itself reaches the target directly — otherwise we
                # create phantom connections where the deadline propagates as a fake
                # arrival time through trips that never reach the target.
                trip_arr = None
                if stop_id in target_stops:
                    # Boarding at target station (main stop or platform) —
                    # find this trip's arrival at the boarding stop
                    for fwd_seq, fwd_sid, fwd_arr, fwd_dep in trip_stops:
                        if fwd_seq == seq and fwd_sid == stop_id:
                            if fwd_arr <= arrive_by_sec:
                                trip_arr = fwd_arr
                            break
                    # If not found at exact seq, check if target_stop_id is nearby
                    if trip_arr is None:
                        for fwd_seq, fwd_sid, fwd_arr, fwd_dep in trip_stops:
                            if fwd_sid == target_stop_id and fwd_arr <= arrive_by_sec:
                                trip_arr = fwd_arr
                                break
                else:
                    # Look for target (or a same-station platform) downstream on this trip
                    for fwd_seq, fwd_sid, fwd_arr, fwd_dep in trip_stops:
                        if fwd_seq > seq and fwd_sid in target_stops:
                            if fwd_arr <= arrive_by_sec:
                                trip_arr = fwd_arr
                            break
                    if trip_arr is None:
                        # Target not on this trip — inherit stop_arr only if it's
                        # a real timetabled arrival (< arrive_by_sec), not a walk deadline
                        if stop_arr < arrive_by_sec:
                            trip_arr = stop_arr
                        # If stop_arr == arrive_by_sec (walk-labelled), skip —
                        # this trip has no verified path to the target

                if trip_arr is None:
                    continue   # no verified path to target via this trip

                for prev_seq, prev_stop, prev_arr, prev_dep in reversed(trip_stops):
                    if prev_seq >= seq:
                        continue
                    elapsed = trip_arr - prev_dep
                    if 0 < elapsed <= max_duration_sec:
                        cur_best_dep = improved.get(prev_stop, latest_dep.get(prev_stop, INF))
                        if prev_dep > cur_best_dep:
                            # Better departure found — update both dep and its paired arr
                            improved[prev_stop] = prev_dep
                            improved_arr[prev_stop] = trip_arr
                        elif prev_dep == cur_best_dep:
                            # Same departure — keep earlier arrival (shorter journey)
                            if trip_arr < improved_arr.get(prev_stop, arrive_by_sec):
                                improved_arr[prev_stop] = trip_arr

        if not improved:
            break

        changed = False
        for stop_id, dep in improved.items():
            new_arr = improved_arr.get(stop_id, arrive_by_sec)
            walk_only.discard(stop_id)  # now labelled by a trip
            cur_dep = latest_dep[stop_id]
            cur_arr = arr_at_target.get(stop_id, arrive_by_sec)
            cur_travel = cur_arr - cur_dep if cur_dep != INF else float("inf")
            new_travel = new_arr - dep
            # Accept if shorter travel time, or same travel with later dep, or first time
            if cur_dep == INF or new_travel < cur_travel or (new_travel == cur_travel and dep > cur_dep):
                latest_dep[stop_id] = dep
                arr_at_target[stop_id] = new_arr
                changed = True

            # Propagate walking transfers — carry the paired arr_at_target
            stop_arr = arr_at_target[stop_id]
            cur_dep  = latest_dep[stop_id]
            for nb_stop, walk_sec in gtfs.transfers.get(stop_id, []):
                nb_dep = cur_dep - walk_sec
                nb_arr = stop_arr
                if nb_dep > latest_dep.get(nb_stop, INF) and \
                   nb_arr - nb_dep <= max_duration_sec:
                    latest_dep[nb_stop] = nb_dep
                    arr_at_target[nb_stop] = nb_arr   # always replace when dep improves
                    changed = True
                elif nb_dep == latest_dep.get(nb_stop, INF) and \
                     nb_arr < arr_at_target.get(nb_stop, arrive_by_sec):
                    arr_at_target[nb_stop] = nb_arr   # same dep, shorter journey
                    changed = True
                elif nb_stop in latest_dep and nb_dep == latest_dep[nb_stop] and \
                     stop_arr < arr_at_target.get(nb_stop, arrive_by_sec):
                    arr_at_target[nb_stop] = stop_arr
                    changed = True

        if not changed:
            break

    # Return (dep, arr_at_target) for each reachable stop
    reachable = {}
    for sid, dep in latest_dep.items():
        if dep == INF:
            continue
        arr = arr_at_target.get(sid, arrive_by_sec)
        travel = arr - dep
        if travel == 0 and sid == target_stop_id:
            reachable[sid] = (dep, arr)   # target itself
        elif 0 < travel <= max_duration_sec:
            reachable[sid] = (dep, arr)
    return reachable


# ── Alpha shape polygon ───────────────────────────────────────────────────────
def stops_to_polygon(stop_ids: list[str], stops: dict) -> dict | None:
    """Convert a set of stop IDs to a GeoJSON polygon using convex/alpha hull."""
    coords = []
    for sid in stop_ids:
        s = stops.get(sid)
        if s:
            coords.append((s["lon"], s["lat"]))

    if len(coords) < 3:
        return None

    mp = MultiPoint(coords)

    # Try alpha shape (concave hull) — needs shapely >= 2.0
    try:
        poly = mp.concave_hull(ratio=ALPHA)
    except AttributeError:
        # Shapely < 2.0 fallback: convex hull
        poly = mp.convex_hull

    if poly.is_empty:
        poly = mp.convex_hull

    # Buffer slightly to merge isolated points
    poly = poly.buffer(0.01).simplify(0.005)

    return mapping(poly)


# ── Per-destination isochrone (multiple cutoffs) ──────────────────────────────
def compute_isochrone(dest: dict, gtfs: GTFSData) -> dict | None:
    """
    Run RAPTOR once to full depth (180 min), then slice into bands:
    30, 60, 90, 120, 150, 180 min — each as a separate GeoJSON Feature.
    Each band is a ring (current cutoff minus previous), so they don't overlap.
    """
    target_id = dest["_gtfs_stop_id"]
    if target_id not in gtfs.stops:
        print(f"  ✗ Stop {target_id} not found in GTFS")
        return None

    # Single RAPTOR run to maximum depth
    reachable = reverse_raptor(
        target_stop_id=target_id,
        gtfs=gtfs,
        arrive_by_sec=SEARCH_TIME_SEC,
        max_duration_sec=MAX_DURATION,
    )

    if len(reachable) < 3:
        print(f"  ✗ Too few reachable stops ({len(reachable)}) for {dest['name']}")
        return None


    # Slice reachable stops into bands by actual travel time
    # reachable[stop_id] = (dep_at_origin, arr_at_target)
    # travel time = arr_at_target - dep_at_origin  (real journey duration)
    features = []
    prev_poly = None

    for cutoff in CUTOFFS_SEC:
        stops_in_band = [
            sid for sid, (dep, arr) in reachable.items()
            if arr - dep <= cutoff
        ]
        if len(stops_in_band) < 3:
            continue

        geometry = stops_to_polygon(stops_in_band, gtfs.stops)
        if not geometry:
            continue

        minutes = cutoff // 60
        features.append({
            "type": "Feature",
            "properties": {
                "id":              dest["id"],
                "name":            dest["name"],
                "lat":             float(dest["lat"]),
                "lon":             float(dest["lon"]),
                "region":          dest["region"],
                "duration_min":    minutes,
                "reachable_stops": len(stops_in_band),
            },
            "geometry": geometry,
        })

    if not features:
        return None

    return {"type": "FeatureCollection", "features": features}


# ── Main ──────────────────────────────────────────────────────────────────────
def download_all_feeds():
    """Download all GTFS feeds that have URLs available."""
    print("▶ Downloading all GTFS feeds…")
    for region in sorted(GTFS_FEEDS.keys()):
        feed = GTFS_FEEDS[region]
        ensure_gtfs(region)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Phase 1: Download all feeds first ─────────────────────────────────────
    download_all_feeds()

    # ── Phase 2: Load destinations ────────────────────────────────────────────
    with open(DESTINATIONS_CSV, newline="", encoding="utf-8") as f:
        destinations = list(csv.DictReader(f))
    print(f"\n▶ Destinations: {len(destinations)}")

    # Collect which regions are actually needed
    needed_regions = {d["region"].upper() for d in destinations}
    # Also load AT if present, even if no destinations yet — it adds connections
    for _at in ("AT-7", "AT-5"):
        if (GTFS_DIR / _at.lower() / "stops.txt").exists():
            needed_regions.add(_at)

    # ── Phase 3: Load all available feeds into one merged graph ────────────────
    print(f"\n▶ Loading GTFS feeds:")
    for r in sorted(needed_regions):
        if r in GTFS_FEEDS:
            print(f"  {r}: search_date={GTFS_FEEDS[r]['search_date']}")
    region_objects = {}
    for region in sorted(needed_regions):
        if region not in GTFS_FEEDS:
            print(f"  ⚠  Unknown region {region!r} — skipping")
            continue
        available = ensure_gtfs(region)
        if not available:
            continue
        print(f"  Indexing {region}…")
        region_objects[region] = GTFSData(region)

    if not region_objects:
        print("ERROR: No GTFS data available.")
        return

    # ── Phase 4: Merge into single graph with cross-region transfers ─────────
    print(f"\n▶ Merging {len(region_objects)} region(s) into unified graph…")
    if len(region_objects) == 1:
        region = next(iter(region_objects))
        gtfs = region_objects[region]
        # Prefix stop IDs to match merged format
        gtfs_merged = GTFSData.merge(region_objects)
    else:
        gtfs_merged = GTFSData.merge(region_objects)

    print(f"  Unified graph: {len(gtfs_merged.stops):,} stops, "
          f"{len(gtfs_merged.trips):,} trips")

    # ── Phase 5: Match destinations to merged stop IDs ──────────────────────
    # Stops are now prefixed: "CH:stop_id", "BY:stop_id", "AT:stop_id"
    print(f"\n▶ Matching destinations to stops…")
    for dest in destinations:
        region = dest["region"].upper()
        best_id, best_dist = None, float("inf")
        dlat, dlon = float(dest["lat"]), float(dest["lon"])
        prefix = f"{region}:"
        for sid, s in gtfs_merged.stops.items():
            if not sid.startswith(prefix):
                continue
            d = haversine_m(dlat, dlon, s["lat"], s["lon"])
            if d < best_dist:
                best_dist = d
                best_id = sid
        dest["_gtfs_stop_id"] = best_id
        if best_dist > 100:
            print(f"  ⚠  {dest['name']}: nearest stop is {best_dist:.0f}m away")

    # ── Phase 6: Compute isochrones ───────────────────────────────────────────
    success, failed = 0, []
    print(f"\n▶ Computing {len(destinations)} isochrone(s)…")

    for i, dest in enumerate(destinations, 1):
        out_path = OUTPUT_DIR / f"{dest['id']}.geojson"

        if out_path.exists():
            print(f"  [{i}/{len(destinations)}] ⏭  {dest['name']} (cached)")
            success += 1
            continue

        print(f"  [{i}/{len(destinations)}] ⚙  {dest['name']}…", end=" ", flush=True)
        t0 = time.time()
        geojson = compute_isochrone(dest, gtfs_merged)
        elapsed = time.time() - t0

        if geojson:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(geojson, f, separators=(",", ":"))
            bands = len(geojson["features"])
            stops_count = geojson["features"][-1]["properties"]["reachable_stops"]
            print(f"✓  {bands} bands  {stops_count} stops @180min  ({elapsed:.1f}s)")
            success += 1
        else:
            failed.append(dest["id"])
            print(f"✗  ({elapsed:.1f}s)")

    print(f"\n✅ {success}/{len(destinations)} succeeded")
    if failed:
        print(f"❌ Failed: {', '.join(failed)}")

    # Rebuild manifest
    manifest = []
    for dest in destinations:
        out_path = OUTPUT_DIR / f"{dest['id']}.geojson"
        if out_path.exists():
            manifest.append({
                "id":     dest["id"],
                "name":   dest["name"],
                "lat":    float(dest["lat"]),
                "lon":    float(dest["lon"]),
                "region": dest["region"],
                "file":   f"isochrones/{dest['id']}.geojson",
            })

    with open(OUTPUT_DIR.parent / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"✅ manifest.json written with {len(manifest)} entries")


if __name__ == "__main__":
    main()
