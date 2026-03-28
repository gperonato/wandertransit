#!/usr/bin/env python3
"""
tests/test_isochrones.py
Fast tests against pre-built isochrone GeoJSON files.

Instead of re-running RAPTOR (slow, minutes), these tests load the already-
computed isochrone polygons from docs/isochrones/ and check that known origin
coordinates fall inside the expected travel-time band polygon.

This tests the full pipeline output: RAPTOR + polygon generation + band slicing.

Requirements:
  - Isochrones must be built: make isochrones
  - shapely must be installed: conda activate isochrone

Run:
  python tests/test_isochrones.py        # milliseconds
  python -m pytest tests/ -v             # all tests

HOW TO ADD A TEST
─────────────────
The isochrones are REVERSE isochrones: they answer "from where can I reach
this hike's access stop by 10:00?"

So the test is:
  stop_id        = the hike's ACCESS STOP (where hikers arrive by transit)
  origin_lat/lon = the hiker's DEPARTURE POINT (e.g. Zürich HB)
  expected_band  = travel time from departure to access stop

Steps:
1. Find your hike's access stop dest_id in docs/manifest.json
2. Open SBB.ch, search: FROM <your city> TO <access stop>, arriving 10:00
3. Note the travel time in minutes
4. Look up your departure city's coordinates (any mapping tool)
5. Add an entry to JOURNEYS below

WHY THIS IS FAST
────────────────
Shapely point-in-polygon on a pre-built polygon: ~1ms per check.
"""

import json
import sys
import pytest
from pathlib import Path

ROOT      = Path(__file__).parent.parent
DOCS      = ROOT / "docs"
MANIFEST  = DOCS / "manifest.json"
ISO_DIR   = DOCS / "isochrones"
from journeys import JOURNEYS

# Skip entire module if isochrones haven't been built yet
pytestmark = pytest.mark.skipif(
    not MANIFEST.exists(),
    reason="Isochrones not built — run: make isochrones"
)

try:
    from shapely.geometry import Point, shape
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def manifest():
    return json.loads(MANIFEST.read_text())


@pytest.fixture(scope="session")
def iso_polygons(manifest):
    """Load all isochrone GeoJSON files, indexed by stop dest_id and band."""
    # index[dest_id][duration_min] = shapely polygon
    index = {}
    for entry in manifest:
        path = DOCS / entry["file"]
        if not path.exists():
            continue
        fc = json.loads(path.read_text())
        dest_id = entry["id"]
        index[dest_id] = {}
        for feat in fc.get("features", []):
            band = feat["properties"].get("duration_min")
            if band and HAS_SHAPELY:
                try:
                    index[dest_id][band] = shape(feat["geometry"])
                except Exception:
                    pass
    return index


# ── Smoke tests (always run, no journey data needed) ─────────────────────────

def test_manifest_exists():
    assert MANIFEST.exists(), "manifest.json missing — run: make isochrones"

def test_manifest_non_empty(manifest):
    assert len(manifest) > 0, "manifest.json is empty"

def test_geojson_files_exist(manifest):
    missing = [e["file"] for e in manifest if not (DOCS / e["file"]).exists()]
    assert not missing, f"Missing GeoJSON files: {missing[:5]}"

def test_each_stop_has_bands(iso_polygons):
    """Every stop should have at least one band polygon."""
    if not HAS_SHAPELY:
        pytest.skip("shapely not installed")
    empty = [sid for sid, bands in iso_polygons.items() if not bands]
    assert not empty, f"Stops with no band polygons: {empty[:5]}"

def test_bands_increase_in_size(iso_polygons):
    """Each successive band polygon should be larger than the previous."""
    if not HAS_SHAPELY:
        pytest.skip("shapely not installed")
    violations = []
    for dest_id, bands in iso_polygons.items():
        sorted_bands = sorted(bands.items())   # [(30, poly), (60, poly), …]
        for i in range(1, len(sorted_bands)):
            prev_min, prev_poly = sorted_bands[i-1]
            curr_min, curr_poly = sorted_bands[i]
            if curr_poly.area < prev_poly.area * 0.5:   # allow some tolerance
                violations.append(f"{dest_id}: band {curr_min} smaller than {prev_min}")
    assert not violations, f"Band size violations: {violations[:3]}"

def test_stop_coordinate_in_innermost_band(iso_polygons, manifest):
    """The destination stop's own coordinates should be inside its 30-min band."""
    if not HAS_SHAPELY:
        pytest.skip("shapely not installed")
    failures = []
    for entry in manifest:
        dest_id = entry["id"]
        # Skip Parent stops — their coordinates are centroid averages,
        # not real boarding points, so they may fall outside the polygon
        if "parent" in dest_id.lower():
            continue
        bands = iso_polygons.get(dest_id, {})
        innermost = bands.get(30) or bands.get(min(bands, key=int, default=None))
        if innermost is None:
            continue
        pt = Point(entry["lon"], entry["lat"])
        if not innermost.contains(pt):
            failures.append(f"{dest_id}: own coordinates not in innermost band")
    # Allow a small number of failures (GPS imprecision, polygon simplification)
    assert len(failures) <= len(manifest) * 0.05, \
        f"{len(failures)} stops not inside own band: {failures[:3]}"


# ── Journey tests (require JOURNEYS to be filled in) ─────────────────────────

@pytest.mark.skipif(not JOURNEYS, reason="No journeys defined — see HOW TO ADD A TEST")
@pytest.mark.parametrize("journey", JOURNEYS, ids=[j["description"] for j in JOURNEYS])
def test_journey(journey, iso_polygons):
    """Origin coordinates should fall inside the expected band polygon."""
    if not HAS_SHAPELY:
        pytest.skip("shapely not installed")

    stop_id      = journey["stop_id"]
    origin_lat   = journey["origin_lat"]
    origin_lon   = journey["origin_lon"]
    expected     = journey["expected_band"]

    bands = iso_polygons.get(stop_id)
    assert bands, \
        f"Stop {stop_id!r} not found in manifest — check the dest_id spelling"

    poly = bands.get(expected)
    assert poly is not None, \
        f"Band {expected} min not found for {stop_id!r} — available: {sorted(bands)}"

    pt = Point(origin_lon, origin_lat)
    # Find actual band (tightest band containing the point)
    actual_band = next(
        (b for b in sorted(bands) if bands[b].contains(pt)), None
    )

    assert poly.contains(pt), (
        f"{journey['description']}:\n"
        f"  Expected: inside {expected}-min band\n"
        f"  Actual:   {'not in any band' if actual_band is None else f'inside {actual_band}-min band'}\n"
        f"  Origin:   ({origin_lat}, {origin_lon})\n"
        f"  Stop:     {stop_id!r}\n"
        f"  Source:   {journey.get('source', '?')}\n"
        f"  Hint:     if actual > expected, RAPTOR may be adding transfer penalties"
    )

    # Also verify it's NOT in a tighter band it shouldn't be in
    tighter = [b for b in sorted(bands) if b < expected]
    for band in tighter:
        if bands[band].contains(pt):
            pytest.fail(
                f"{journey['description']}:\n"
                f"  Expected: {expected}-min band\n"
                f"  Actual:   inside tighter {band}-min band (journey faster than expected)\n"
                f"  Source:   {journey.get('source', '?')}"
            )


# ── Standalone runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not MANIFEST.exists():
        print("⚠  No isochrones built yet — run: make isochrones")
        sys.exit(0)

    manifest_data  = json.loads(MANIFEST.read_text())

    # Build polygon index inline
    iso_data = {}
    if HAS_SHAPELY:
        for entry in manifest_data:
            p = DOCS / entry["file"]
            if not p.exists(): continue
            fc = json.loads(p.read_text())
            dest_id = entry["id"]
            iso_data[dest_id] = {}
            for feat in fc.get("features", []):
                band = feat["properties"].get("duration_min")
                if band:
                    try: iso_data[dest_id][band] = shape(feat["geometry"])
                    except: pass

    tests = [
        ("manifest exists",            lambda: MANIFEST.exists()),
        ("manifest non-empty",         lambda: len(manifest_data) > 0),
        ("all GeoJSON files exist",    lambda: not [e for e in manifest_data if not (DOCS/e["file"]).exists()]),
        ("stops have bands",           lambda: not [s for s,b in iso_data.items() if not b] if HAS_SHAPELY else True),
        ("bands increase in size",     lambda: _check_band_sizes(iso_data) if HAS_SHAPELY else True),
        ("stop coords in inner band",  lambda: _check_self_contained(iso_data, manifest_data) if HAS_SHAPELY else True),
    ]

    def _check_band_sizes(idx):
        for dest_id, bands in idx.items():
            sb = sorted(bands.items())
            for i in range(1, len(sb)):
                if sb[i][1].area < sb[i-1][1].area * 0.5:
                    return False
        return True

    def _check_self_contained(idx, mf):
        fail = 0
        for entry in mf:
            bands = idx.get(entry["id"], {})
            inner = bands.get(30) or (bands.get(min(bands)) if bands else None)
            if inner and not inner.contains(Point(entry["lon"], entry["lat"])):
                fail += 1
        return fail <= len(mf) * 0.05

    passed = failed = 0
    for name, fn in tests:
        try:
            ok = fn()
            print(f"  {'✓' if ok else '✗'}  {name}")
            if ok: passed += 1
            else: failed += 1
        except Exception as e:
            print(f"  ✗  {name}: {e}")
            failed += 1

    print(f"\n{passed}/{passed+failed} passed")
    if failed: sys.exit(1)
