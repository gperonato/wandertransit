#!/usr/bin/env python3
"""
scripts/clean_gpx.py
Offline preprocessing step: converts raw GPX files in gpx/ to clean,
anonymised GPX files in docs/gpx/.

Operations performed:
  - Keep only coordinates, elevation, and timestamps
  - Shift all timestamps so the track starts at 1970-01-01 10:00:00 UTC
    (preserves relative timing / pace, removes personal date/time info)
  - Strip all other data: heart rate, cadence, power, extensions, metadata
  - Rename files to <slugified-name>-<track-hash>.gpx

Run this ONCE locally whenever you add or change GPX files:
    python scripts/clean_gpx.py

The resulting docs/gpx/ files are committed to the repo.
gpx/ stays local and is gitignored.

CI/CD only uses docs/gpx/
"""

import hashlib
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

GPX_DIR     = Path(__file__).parent.parent / "gpx"
OUT_DIR     = Path(__file__).parent.parent / "docs" / "gpx"
EPOCH_START = datetime(1970, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s_-]+", "-", text).strip("-")[:50]


def track_hash(points: list) -> str:
    """Stable 6-char hash from 8 evenly-spaced track points."""
    n = len(points)
    idxs = [int(i * (n - 1) / 7) for i in range(8)] if n >= 8 else list(range(n))
    sample = [points[i] for i in idxs]
    key = "|".join(f"{lon:.5f},{lat:.5f}" for lat, lon, *_ in sample)
    return hashlib.sha256(key.encode()).hexdigest()[:6]


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


# ── Core processing ───────────────────────────────────────────────────────────

def clean_gpx(src: Path) -> tuple[str, str] | None:
    """
    Parse src GPX, return (slug_filename, xml_string) or None on failure.

    Output GPX contains:
      - One <trk><name>…</name><trkseg>
      - One <trkpt lat="…" lon="…"> per point with:
          <ele> if present
          <time> shifted to start at 1970-01-01 10:00:00 UTC
    """
    try:
        tree = ET.parse(src)
        root = tree.getroot()
    except ET.ParseError as e:
        print(f"  ✗ {src.name}: XML parse error: {e}")
        return None

    tag = root.tag
    ns  = tag[1:tag.index("}")] if "{" in tag else ""
    pfx = f"{{{ns}}}" if ns else ""

    # ── Extract track name ────────────────────────────────────────────────────
    name = None
    for tag_path in (
        f".//{pfx}trk/{pfx}name",
        f".//{pfx}metadata/{pfx}name",
        f"{pfx}name",
    ):
        el = root.find(tag_path)
        if el is not None and el.text and el.text.strip():
            name = el.text.strip()
            break
    if not name:
        name = src.stem.replace("_", " ").replace("-", " ")

    # ── Extract trackpoints ───────────────────────────────────────────────────
    raw_pts = []   # (lat, lon, ele_or_None, time_or_None)
    for trkpt in root.iter(f"{pfx}trkpt"):
        try:
            lat = float(trkpt.attrib["lat"])
            lon = float(trkpt.attrib["lon"])
        except (KeyError, ValueError):
            continue
        ele_el = trkpt.find(f"{pfx}ele")
        ele = float(ele_el.text) if ele_el is not None and ele_el.text else None
        time_el = trkpt.find(f"{pfx}time")
        t = parse_time(time_el.text) if time_el is not None else None
        raw_pts.append((lat, lon, ele, t))

    if not raw_pts:
        print(f"  ✗ {src.name}: no trackpoints found")
        return None

    # ── Shift timestamps to start at EPOCH_START ─────────────────────────────
    times = [t for _, _, _, t in raw_pts if t is not None]
    if times:
        t0 = min(times)
        offset = EPOCH_START - t0  # shift so first point = 10:00:00
    else:
        offset = None

    # ── Build output filename ─────────────────────────────────────────────────
    slug = f"{slugify(name)}-{track_hash(raw_pts)}"
    filename = f"{slug}.gpx"

    # ── Build output XML ──────────────────────────────────────────────────────
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="wandertransit"',
        '  xmlns="http://www.topografix.com/GPX/1/1">',
        f'  <trk><name>{name}</name><trkseg>',
    ]
    for lat, lon, ele, t in raw_pts:
        parts = [f'    <trkpt lat="{lat:.6f}" lon="{lon:.6f}">']
        if ele is not None:
            parts.append(f'      <ele>{ele:.1f}</ele>')
        if t is not None and offset is not None:
            shifted = t + offset
            parts.append(f'      <time>{shifted.strftime("%Y-%m-%dT%H:%M:%SZ")}</time>')
        parts.append('    </trkpt>')
        lines.extend(parts)
    lines += ['  </trkseg></trk>', '</gpx>']

    return filename, "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    gpx_files = sorted(GPX_DIR.glob("**/*.gpx"))
    if not gpx_files:
        print(f"No GPX files found in {GPX_DIR}/")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Track which output files we produce — remove stale ones afterwards
    produced = set()
    ok = failed = 0

    for src in gpx_files:
        print(f"  ⚙  {src.name}…", end=" ", flush=True)
        result = clean_gpx(src)
        if result is None:
            failed += 1
            continue

        filename, xml = result
        out_path = OUT_DIR / filename
        out_path.write_text(xml, encoding="utf-8")
        produced.add(filename)
        pts = xml.count("<trkpt")
        has_time = "<time>" in xml
        print(f"✓  {filename}  ({pts} pts{'  ⏱' if has_time else '  no timestamps'})")
        ok += 1

    # Remove stale output files (renamed or deleted sources)
    stale = [f for f in OUT_DIR.glob("*.gpx") if f.name not in produced]
    for f in stale:
        f.unlink()
        print(f"  🗑  removed stale {f.name}")

    print(f"\n✅ {ok} cleaned → {OUT_DIR}")
    if failed:
        print(f"❌ {failed} failed")
    if stale:
        print(f"🗑  {len(stale)} stale files removed")


if __name__ == "__main__":
    main()
