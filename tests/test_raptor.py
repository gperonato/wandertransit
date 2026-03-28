#!/usr/bin/env python3
"""
tests/test_raptor.py
Integration tests for the reverse RAPTOR isochrone pipeline.

Tests use a minimal synthetic GTFS feed built in-memory — no real data needed.
Run with:  python -m pytest tests/ -v
       or:  python tests/test_raptor.py
"""

import sys
import math
from collections import defaultdict
from pathlib import Path

# ── Inline the constants and functions under test ─────────────────────────────
# This avoids the shapely import in fetch_isochrones.py at module level.

MIN_TRANSFER_SEC = 3 * 60
WALK_SPEED_MS    = 1.2
SEARCH_TIME_SEC  = 10 * 3600
MAX_RAPTOR_ROUNDS = 6

def haversine_m(lat1, lon1, lat2, lon2):
    R = 6_371_000
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

def reverse_raptor(target_stop_id, gtfs, arrive_by_sec, max_duration_sec):
    """
    Returns dict: stop_id → (dep_at_origin, arr_at_target)
    Travel time = arr_at_target - dep_at_origin.
    Kept in sync with fetch_isochrones.py logic.
    """
    INF = -1
    latest_dep = defaultdict(lambda: INF)
    arr_at_target = {}

    # Target stops set (for tests: just the target itself, no platform stops)
    target_stops = {target_stop_id}

    latest_dep[target_stop_id] = arrive_by_sec
    arr_at_target[target_stop_id] = arrive_by_sec
    walk_only = set()  # stops labelled only by walk from target, not by a trip

    for nb_stop, walk_sec in gtfs.transfers.get(target_stop_id, []):
        depart = arrive_by_sec - walk_sec
        if depart > INF and depart >= arrive_by_sec - max_duration_sec:
            latest_dep[nb_stop] = max(latest_dep[nb_stop], depart)
            arr_at_target[nb_stop] = arrive_by_sec
            walk_only.add(nb_stop)

    for _round in range(MAX_RAPTOR_ROUNDS):
        improved = {}
        improved_arr = {}

        for stop_id, dep_time in list(latest_dep.items()):
            if dep_time == INF:
                continue
            effective_dep = dep_time if stop_id == target_stop_id else dep_time - MIN_TRANSFER_SEC
            stop_arr = arr_at_target.get(stop_id, arrive_by_sec)

            for board_dep, trip_id, seq in gtfs.trips_at_stop.get(stop_id, []):
                if board_dep > effective_dep:
                    continue
                trip_stops = gtfs.stop_times.get(trip_id, [])

                # Find when this trip reaches the target
                trip_arr = None
                if stop_id in target_stops:
                    for fwd_seq, fwd_sid, fwd_arr, fwd_dep in trip_stops:
                        if fwd_seq == seq and fwd_sid == stop_id:
                            if fwd_arr <= arrive_by_sec:
                                trip_arr = fwd_arr
                            break
                else:
                    for fwd_seq, fwd_sid, fwd_arr, fwd_dep in trip_stops:
                        if fwd_seq > seq and fwd_sid in target_stops:
                            if fwd_arr <= arrive_by_sec:
                                trip_arr = fwd_arr
                            break
                    if trip_arr is None and stop_id not in walk_only:
                        trip_arr = stop_arr  # labelled by a trip — inherit its arr

                if trip_arr is None:
                    continue

                for ps, psid, pa, pd in reversed(trip_stops):
                    if ps >= seq:
                        continue
                    elapsed = trip_arr - pd
                    if 0 < elapsed <= max_duration_sec:
                        cur_dep = improved.get(psid, latest_dep.get(psid, INF))
                        cur_arr = improved_arr.get(psid, arr_at_target.get(psid, arrive_by_sec))
                        cur_travel = cur_arr - cur_dep if cur_dep != INF else float("inf")
                        new_travel = elapsed
                        if new_travel < cur_travel or (new_travel == cur_travel and pd > cur_dep) or cur_dep == INF:
                            improved[psid] = pd
                            improved_arr[psid] = trip_arr

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
            if cur_dep == INF or new_travel < cur_travel or (new_travel == cur_travel and dep > cur_dep):
                latest_dep[stop_id] = dep
                arr_at_target[stop_id] = new_arr
                changed = True
            cur_dep = latest_dep[stop_id]
            stop_arr = arr_at_target[stop_id]
            for nb_stop, walk_sec in gtfs.transfers.get(stop_id, []):
                nb_dep = cur_dep - walk_sec
                nb_arr = stop_arr
                if nb_dep > latest_dep.get(nb_stop, INF) and nb_arr - nb_dep <= max_duration_sec:
                    latest_dep[nb_stop] = nb_dep
                    arr_at_target[nb_stop] = nb_arr
                    changed = True
                elif nb_dep == latest_dep.get(nb_stop, INF) and \
                     nb_arr < arr_at_target.get(nb_stop, arrive_by_sec):
                    arr_at_target[nb_stop] = nb_arr
                    changed = True
        if not changed:
            break

    reachable = {}
    for sid, dep in latest_dep.items():
        if dep == INF:
            continue
        arr = arr_at_target.get(sid, arrive_by_sec)
        travel = arr - dep
        if travel == 0 and sid == target_stop_id:
            reachable[sid] = (dep, arr)   # target itself: 0 travel time
        elif 0 < travel <= max_duration_sec:
            reachable[sid] = (dep, arr)
    return reachable


# ── Synthetic GTFS builder ────────────────────────────────────────────────────

def make_gtfs(stops, trips, transfers=None):
    """
    Build a minimal GTFSData-like object from dicts.

    stops:    {stop_id: (lat, lon, name)}
    trips:    [[(stop_id, arrival_sec), ...]]  — each inner list is one trip
    transfers: {stop_id: [(neighbour_id, walk_sec), ...]}  — optional
    """
    gtfs = type('GTFSData', (), {})()  # minimal stand-in

    gtfs.stops = {
        sid: {"lat": lat, "lon": lon, "name": name}
        for sid, (lat, lon, name) in stops.items()
    }

    # Build stop_times and trips_at_stop
    gtfs.stop_times   = {}
    gtfs.trips_at_stop = defaultdict(list)

    for trip_idx, stop_sequence in enumerate(trips):
        trip_id = f"trip_{trip_idx}"
        entries = []
        for seq, (sid, arr) in enumerate(stop_sequence):
            dep = arr  # no dwell time in tests
            entries.append((seq, sid, arr, dep))
            gtfs.trips_at_stop[sid].append((dep, trip_id, seq))  # indexed by dep
        gtfs.stop_times[trip_id] = entries

    for sid in gtfs.trips_at_stop:
        gtfs.trips_at_stop[sid].sort()

    gtfs.transfers = defaultdict(list)
    if transfers:
        for sid, neighbours in transfers.items():
            gtfs.transfers[sid].extend(neighbours)

    return gtfs


# ── Helpers ───────────────────────────────────────────────────────────────────

def travel_time(gtfs, target, result):
    """Return actual travel time in seconds (arr_at_target - dep), or None."""
    entry = result.get(target)
    if entry is None:
        return None
    dep, arr = entry
    return arr - dep


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_direct_trip():
    """A single direct trip: A→B. B is the target. A should be reachable."""
    # Arrive at B at 10:00. Trip departs A at 09:30, arrives B at 10:00.
    gtfs = make_gtfs(
        stops={
            "A": (47.0, 8.0, "Stop A"),
            "B": (47.1, 8.1, "Stop B"),
        },
        trips=[
            [("A", 9*3600 + 30*60), ("B", 10*3600)],
        ],
    )
    result = reverse_raptor("B", gtfs, arrive_by_sec=10*3600, max_duration_sec=3600)

    assert "B" in result, "Target stop must always be reachable"
    assert "A" in result, "A should be reachable via direct trip"
    dep_a, arr_a = result["A"]
    assert arr_a <= 10*3600, "Should arrive at target by arrive_by_sec"
    assert dep_a == 9*3600 + 30*60, "Departure time should be 09:30"
    assert arr_a - dep_a == 30*60, "Travel time should be 30 min"


def test_unreachable_stop():
    """A stop with no trip to target should not appear."""
    gtfs = make_gtfs(
        stops={
            "A": (47.0, 8.0, "Stop A"),
            "B": (47.1, 8.1, "Stop B"),
            "C": (47.2, 8.2, "Stop C"),  # no trip to B
        },
        trips=[
            [("A", 9*3600 + 30*60), ("B", 10*3600)],
        ],
    )
    result = reverse_raptor("B", gtfs, arrive_by_sec=10*3600, max_duration_sec=3600)

    assert "C" not in result, "C has no route to B and must not appear"


def test_max_duration_respected():
    """Stops outside max_duration window must be excluded."""
    # Trip departs A at 07:00, arrives B at 10:00 → 3h travel time
    gtfs = make_gtfs(
        stops={
            "A": (47.0, 8.0, "Stop A"),
            "B": (47.1, 8.1, "Stop B"),
        },
        trips=[
            [("A", 7*3600), ("B", 10*3600)],
        ],
    )
    result_2h = reverse_raptor("B", gtfs, arrive_by_sec=10*3600, max_duration_sec=2*3600)
    result_4h = reverse_raptor("B", gtfs, arrive_by_sec=10*3600, max_duration_sec=4*3600)

    assert "A" not in result_2h, "A is 3h away — should not appear in 2h isochrone"
    assert "A" in result_4h,     "A is 3h away — should appear in 4h isochrone"


def test_two_leg_journey_with_transfer():
    """
    Two-leg journey: A→B (trip 1), then B→C (trip 2). Target is C.
    B is an interchange — transfer time must be respected.
    """
    # Trip 1: A departs 08:00, arrives B 09:00
    # Trip 2: B departs 09:10, arrives C 10:00
    # Transfer at B: 10 min gap — should be valid (≥ MIN_TRANSFER_SEC = 3 min)
    gtfs = make_gtfs(
        stops={
            "A": (47.0, 8.0, "Stop A"),
            "B": (47.1, 8.1, "Stop B"),
            "C": (47.2, 8.2, "Stop C"),
        },
        trips=[
            [("A", 8*3600),        ("B", 9*3600)],          # trip 1
            [("B", 9*3600 + 600),  ("C", 10*3600)],         # trip 2
        ],
    )
    result = reverse_raptor("C", gtfs, arrive_by_sec=10*3600, max_duration_sec=3*3600)

    assert "C" in result
    assert "B" in result, "B is on trip 2 — reachable in 1h"
    assert "A" in result, "A is reachable via A→B→C with 10-min transfer"


def test_transfer_time_blocks_impossible_connection():
    """
    Impossible connection: transfer gap < MIN_TRANSFER_SEC.
    Trip 1 arrives B at 09:00, trip 2 departs B at 09:01 — only 1 min gap.
    A should NOT be reachable.
    """
    gtfs = make_gtfs(
        stops={
            "A": (47.0, 8.0, "Stop A"),
            "B": (47.1, 8.1, "Stop B"),
            "C": (47.2, 8.2, "Stop C"),
        },
        trips=[
            [("A", 8*3600),       ("B", 9*3600)],           # arrives B 09:00
            [("B", 9*3600 + 60),  ("C", 10*3600)],          # departs B 09:01
        ],
    )
    result = reverse_raptor("C", gtfs, arrive_by_sec=10*3600, max_duration_sec=3*3600)

    assert "B" in result,  "B is directly on trip 2"
    assert "A" not in result, \
        f"A requires a 1-min transfer at B (< MIN_TRANSFER_SEC={MIN_TRANSFER_SEC}s) — must be blocked"


def test_walking_transfer():
    """
    Two stops within walking distance. No direct trip between them.
    Passenger walks from A to B (nearby), then takes a trip B→C.
    """
    walk_sec = 120  # 2 min walk A→B
    gtfs = make_gtfs(
        stops={
            "A": (47.0, 8.0, "Stop A"),
            "B": (47.0, 8.001, "Stop B"),  # ~80m away
            "C": (47.2, 8.2,  "Stop C"),
        },
        trips=[
            [("B", 9*3600), ("C", 10*3600)],
        ],
        transfers={
            "A": [("B", walk_sec)],
            "B": [("A", walk_sec)],
        },
    )
    result = reverse_raptor("C", gtfs, arrive_by_sec=10*3600, max_duration_sec=3*3600)

    assert "B" in result, "B is on the trip to C"
    assert "A" in result, "A can walk to B then take trip to C"
    # Departure from A = departure from B minus walk time
    dep_a, _ = result["A"]
    dep_b, _ = result["B"]
    assert dep_a == dep_b - walk_sec, f"dep_a={dep_a} should be dep_b={dep_b} - walk={walk_sec}"


def test_multiple_cutoff_bands():
    """
    Stops at different distances should fall into correct time bands.
    """
    # All trips arrive at target T at 10:00
    gtfs = make_gtfs(
        stops={
            "T":  (47.0, 8.0, "Target"),
            "S1": (47.1, 8.1, "Stop 20min"),
            "S2": (47.2, 8.2, "Stop 50min"),
            "S3": (47.3, 8.3, "Stop 80min"),
        },
        trips=[
            [("S1", 9*3600 + 40*60), ("T", 10*3600)],   # 20 min journey
            [("S2", 9*3600 + 10*60), ("T", 10*3600)],   # 50 min journey
            [("S3", 8*3600 + 40*60), ("T", 10*3600)],   # 80 min journey
        ],
    )
    result = reverse_raptor("T", gtfs, arrive_by_sec=10*3600, max_duration_sec=3*3600)

    def band(stop_id):
        entry = result.get(stop_id)
        if entry is None: return None
        dep, arr = entry
        t = (arr - dep) // 60   # actual travel time
        for cutoff in [30, 60, 90, 120, 150, 180]:
            if t <= cutoff:
                return cutoff
        return None

    assert band("S1") == 30,  f"S1 (20min) should be in 30-min band, got {band('S1')}"
    assert band("S2") == 60,  f"S2 (50min) should be in 60-min band, got {band('S2')}"
    assert band("S3") == 90,  f"S3 (80min) should be in 90-min band, got {band('S3')}"


def test_faster_of_two_trips():
    """
    Two trips serve the same origin A, one faster than the other.
    RAPTOR should pick the one that departs latest (most slack for passenger).
    """
    gtfs = make_gtfs(
        stops={
            "A": (47.0, 8.0, "Stop A"),
            "B": (47.1, 8.1, "Stop B"),
        },
        trips=[
            [("A", 8*3600), ("B", 10*3600)],   # slow: departs 08:00
            [("A", 9*3600), ("B", 10*3600)],   # fast: departs 09:00
        ],
    )
    result = reverse_raptor("B", gtfs, arrive_by_sec=10*3600, max_duration_sec=3*3600)

    # Should use the later departure (09:00), not the earlier one (08:00)
    dep_a, arr_a = result["A"]
    assert dep_a == 9*3600, \
        f"Should pick latest valid departure (09:00), got {dep_a//3600:02d}:{(dep_a%3600)//60:02d}"
    assert arr_a - dep_a == 3600, "Travel time should be 1h"


def test_haversine():
    """Sanity check on haversine — Zürich to Bern is ~95 km."""
    zurich = (47.3782, 8.5403)
    bern   = (46.9491, 7.4396)
    dist   = haversine_m(zurich[0], zurich[1], bern[0], bern[1])
    assert 90_000 < dist < 100_000, f"Zürich–Bern distance unexpected: {dist:.0f}m"


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        test_direct_trip,
        test_unreachable_stop,
        test_max_duration_respected,
        test_two_leg_journey_with_transfer,
        test_transfer_time_blocks_impossible_connection,
        test_walking_transfer,
        test_multiple_cutoff_bands,
        test_faster_of_two_trips,
        test_haversine,
    ]

    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ✗  {t.__name__}: {e}")
            failed += 1

    print(f"\n{passed}/{passed+failed} passed")
    if failed:
        sys.exit(1)
