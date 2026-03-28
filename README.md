# WanderTransit

Find hikes reachable by public transit. Click on a map to discover which Alpine hikes you can reach within 30–180 minutes using trains and buses.

## Features

- **Interactive map** — Click anywhere to see reachable hikes
- **Transit isochrones** — 30, 60, 90, 120, 150, 180 minute bands
- **Multi-country** — Covers Switzerland, Germany and Austria (Tyrol and Salzburg), as well as border regions in neighboring countries
- **Filter & sort** — By distance, elevation, duration, and travel time
- **Open source** — Built with MapLibre GL, GTFS, and OpenStreetMap data

## Quick Start

### Prerequisites

- Python 3.12+
- `make`

### Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Process hikes, compute isochrones, and deploy
make all

# Serve on
# → http://localhost:8000
```

## Build Pipeline

### Step 1: Clean GPX files (optional)
```bash
make process-source-gpx
```
Anonymizes GPX tracks, removes timestamps, splits multi-track files.
*Note that the `<name></name>`tag is preserved and used in the interface.*

### Step 2: Extract destinations
```bash
make destinations
```
- Reads cleaned GPX files from `docs/gpx/`
- Extracts start/end points
- Snaps to nearest GTFS stop
- Outputs `docs/destinations.csv` + `docs/hikes.json`

**Inputs:**
- `docs/gpx/*.gpx` — hiking tracks

**Intermediate Outputs:**
- `docs/gpx_stats.json` — pre-computed stats (distance, elevation, name)

**Outputs:**
- `docs/destinations.csv` — stop→hike mapping for isochrone computation
- `docs/hikes.json` — hike metadata for frontend

### Step 3: Compute isochrones
```bash
make isochrones
```
- Downloads GTFS feeds (auto-cached)
- Runs reverse RAPTOR from each destination stop
- Generates alpha-shape polygons for 30–180 min bands
- Outputs `docs/isochrones/*.geojson`

**Inputs:**
- `docs/destinations.csv`
- GTFS feeds (auto-downloaded from CH, DE, AT)

**Outputs:**
- `docs/isochrones/` — one GeoJSON per stop

### Step 4: Serve frontend
```bash
make serve
```
Opens http://localhost:8000 with the interactive map.

## Configuration

### GTFS Feeds

Edit `scripts/gtfs_common.py`:

Or set environment variables:
```bash
export GTFS_CH_URL="https://..."
export GTFS_DE_URL="https://..."
export GTFS_AT7_ID="1abc..."
export GTFS_AT5_ID="1def..."
```

### Isochrone Settings

Edit `scripts/fetch_isochrones.py`:

## Project Structure

```
hikept/
├── docs/                          # Frontend & data
│   ├── index.html                 # Interactive map
│   ├── gpx/                       # Input hiking tracks
│   ├── gpx_stats.json             # Pre-computed stats
│   ├── destinations.csv           # Stop→hike mapping
│   ├── hikes.json                 # Hike metadata
│   ├── isochrones/                # Generated isochrone GeoJSON
│   └── *.geojson                  # GPX tracks as GeoJSON
├── scripts/                       # Data pipeline
│   ├── process_source_gpx.py      # Anonymize GPX
│   ├── gpx_to_geojson.py          # Extract stats
│   ├── gpx_to_destinations.py     # Snap to GTFS stops
│   ├── fetch_isochrones.py        # Compute isochrones
│   └── gtfs_common.py             # Shared GTFS config
├── gtfs/                          # Cached GTFS feeds (auto-downloaded)
├── requirements.txt
├── Makefile
└── README.md
```

## Data Sources

| Type | License | Source |
|--------|---------|-------|
| **Hiking tracks** | CC-BY | Custom collection |
| **GTFS DE** | CC-BY | [GTFS.de](gtfs.de) |
| **GTFS CH** | Attribution | [OpenTransportData.swiss](https://data.opentransportdata.swiss) |
| **GTFS AT** | Attribution | [Mobilitätsverbünde Österreich OG](http://data.moblitaetsverbuende.at/) |
| **Base map** | ODbL | [OpenStreetMap](https://www.openstreetmap.org/) via [OpenFreeMap](https://www.openfreemap.org/) |
| **OSM peaks** | ODbL | [OpenStreetMap](https://www.openstreetmap.org/) via [OverPassAPI.de](https://overpass-api.de) |

## Testing

```bash
# Run unit tests
make test

# Test isochrone output (GeoJSON validation)
make test-iso

# All tests
make test-all
```

## 🤖 AI Disclosure

This project was primarily authored by Generative AI.

| Component | AI Involvement | Human Oversight |
|---|---|---|
| **Ideation** | Analyzed different options | Choice of technology stack |
| **Logic/Core** | Generated codebase | Partially reviewed and debugged |
| **Tests** | Generated codebase | Authored real-world connections in `tests/journeys.py`|
| **Docs** | Generated README draft | Final polish, formatting and verification |

Models: Claude Sonnet 4.6, Claude Haiku 4.5, Gemini 3

## License

This project uses multiple data sources under their respective licenses (CC-BY, ODbL).

The source code is licensed under the MIT License.

## Contributing

Pull requests (also with new gpx hikes) welcome! Please:
1. Add tests for new features
2. Run `make test-all` before submitting
3. Update README if changing data pipeline

## Contact

Issues? Questions? Open an issue on GitHub