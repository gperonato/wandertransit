"""
scripts/gtfs_common.py
Shared GTFS feed configuration and download logic for all scripts.
"""

import csv
import os
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
GTFS_DIR = ROOT / "gtfs"

GTFS_FEEDS = {
    "CH": {
        "url": os.environ.get("GTFS_CH_URL", "https://data.opentransportdata.swiss/en/dataset/timetable-2026-gtfs2020/permalink"),
        "gdown_id": None,
        "dir": GTFS_DIR / "ch",
        "zip": GTFS_DIR / "ch.zip",
        "search_date": "20260606",
    },
    "DE": {
        "url": os.environ.get("GTFS_DE_URL", "https://download.gtfs.de/germany/free/latest.zip"),
        "gdown_id": None,
        "dir": GTFS_DIR / "de",
        "zip": GTFS_DIR / "de.zip",
        "search_date": "20260328",
    },
    "AT-7": {
        "url": None,
        "gdown_id": os.environ.get("GTFS_AT7_ID", None),
        "dir": GTFS_DIR / "at-7",
        "zip": GTFS_DIR / "at-7.zip",
        "search_date": "20251206",
    },
    "AT-5": {
        "url": None,
        "gdown_id": os.environ.get("GTFS_AT5_ID", None),
        "dir": GTFS_DIR / "at-5",
        "zip": GTFS_DIR / "at-5.zip",
        "search_date": "20251206",
    },
}

GTFS_FILES_NEEDED = ["stops.txt", "stop_times.txt", "trips.txt",
                     "calendar.txt", "calendar_dates.txt", "routes.txt"]


def resolve_search_date(region: str) -> str:
    """
    Validate the configured search_date for a region against the actual GTFS
    calendar data. If the date has no active services, find the nearest date
    with the same weekday that does. Returns the resolved date as 'YYYYMMDD'.
    """
    feed = GTFS_FEEDS[region]
    preferred = feed["search_date"]
    gtfs_dir  = feed["dir"]
    cal_path  = gtfs_dir / "calendar.txt"
    cal_dates_path = gtfs_dir / "calendar_dates.txt"

    preferred_dt = datetime.strptime(preferred, "%Y%m%d")
    target_weekday = preferred_dt.weekday()  # 0=Mon … 6=Sun

    def active_services_on(dt: datetime) -> set:
        date_str = dt.strftime("%Y%m%d")
        day_col = ["monday","tuesday","wednesday","thursday",
                   "friday","saturday","sunday"][dt.weekday()]
        active: set = set()
        if cal_path.exists():
            with open(cal_path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    try:
                        start = datetime.strptime(row["start_date"], "%Y%m%d")
                        end   = datetime.strptime(row["end_date"],   "%Y%m%d")
                        if start <= dt <= end and row.get(day_col, "0") == "1":
                            active.add(row["service_id"])
                    except (ValueError, KeyError):
                        continue
        if cal_dates_path.exists():
            with open(cal_dates_path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if row.get("date") == date_str:
                        active.add(row.get("service_id", ""))
        return active

    # Fast path: preferred date is fine
    if active_services_on(preferred_dt):
        return preferred

    # Collect all dates that appear in calendar.txt or calendar_dates.txt
    # with the same weekday, then pick the earliest one with active services.
    candidates: set[datetime] = set()

    if cal_path.exists():
        with open(cal_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    start = datetime.strptime(row["start_date"], "%Y%m%d")
                    end   = datetime.strptime(row["end_date"],   "%Y%m%d")
                    # Walk every matching weekday in [start, end]
                    # Clamp range to avoid huge loops on wide-range feeds
                    delta = (target_weekday - start.weekday()) % 7
                    day = start + timedelta(days=delta)
                    while day <= end:
                        candidates.add(day)
                        day += timedelta(weeks=1)
                except (ValueError, KeyError):
                    continue

    if cal_dates_path.exists():
        with open(cal_dates_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    dt = datetime.strptime(row["date"], "%Y%m%d")
                    if dt.weekday() == target_weekday:
                        candidates.add(dt)
                except (ValueError, KeyError):
                    continue

    # Among candidates, find the one closest to preferred_dt (same weekday)
    # that actually has active services
    for dt in sorted(candidates, key=lambda d: abs((d - preferred_dt).days)):
        if active_services_on(dt):
            resolved = dt.strftime("%Y%m%d")
            print(f"  ⚠  {region}: search_date {preferred} has no active services "
                  f"→ using {resolved} instead")
            return resolved

    # Nothing found — return original and let the loader surface the error
    print(f"  ⚠  {region}: could not find any active date with weekday "
          f"{preferred_dt.strftime('%A')} in GTFS calendar, keeping {preferred}")
    return preferred


def ensure_gtfs(region: str) -> bool:
    """
    Download and unzip GTFS feed if not already present.
    Returns True if feed is available, False if missing and cannot download.
    """
    feed = GTFS_FEEDS[region]
    feed["dir"].mkdir(parents=True, exist_ok=True)
    missing = [f for f in GTFS_FILES_NEEDED
               if not (feed["dir"] / f).exists()]
    if not missing:
        print(f"  ✓ GTFS {region} cached")
        return True

    # Try gdown if gdown_id is set (Google Drive)
    if feed["gdown_id"]:
        print(f"  ⬇  Downloading GTFS {region} from Google Drive…")
        try:
            import gdown
            gdown.download(id=feed["gdown_id"], output=str(feed["zip"]), quiet=True)
        except ImportError:
            print(f"  ✗ gdown not installed. Run: pip install gdown")
            return False
        except Exception as e:
            print(f"  ✗ Download failed: {e}")
            return False

    # Try standard URL if url is set (CH, DE)
    elif feed["url"]:
        print(f"  ⬇  Downloading GTFS {region}…")
        req = urllib.request.Request(
            feed["url"],
            headers={
                'User-Agent': 'Mozilla/5.0 (compatible; hikept-isochrones/1.0)',
                'Referer': 'https://www.opentransportdata.swiss',
                'Accept': 'application/zip, application/octet-stream, */*',
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                with open(feed["zip"], 'wb') as f:
                    f.write(response.read())
        except urllib.error.HTTPError as e:
            print(f"  ✗ HTTP {e.code}: {feed['url']}")
            print(f"    Check if URL is still valid or timetable year has changed")
            return False
        except Exception as e:
            print(f"  ✗ Download failed: {e}")
            return False

    else:
        print(f"  ⚠  GTFS {region}: no URL or gdown_id configured")
        return False

    # Extract ZIP
    print(f"  📦 Extracting GTFS {region}…")
    try:
        with zipfile.ZipFile(feed["zip"]) as z:
            z.extractall(feed["dir"])
        feed["zip"].unlink(missing_ok=True)
    except zipfile.BadZipFile:
        print(f"  ✗ Corrupted ZIP file, removing…")
        feed["zip"].unlink(missing_ok=True)
        return False

    print(f"  ✓ GTFS {region} ready")
    return True