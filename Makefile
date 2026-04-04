# ── Isochrone project ─────────────────────────────────────────────────────────
#
# Preparation:
#  make process-source-gpx # clean and anonymise GPX files (offline, run locally)
# 
# Full CI pipeline:
#   make all
#
# Step by step:
#   make destinations      # GPX → destinations.csv
#   make isochrones        # compute isochrones from GTFS  (downloads full GTFS)
#   make serve             # open http://localhost:8000
# ─────────────────────────────────────────────────────────────────────────────

PYTHON ?= python3

# ── GPX cleaning (anonymisation, removing timestamps, etc.) ─────────────────────
.PHONY: process-source-gpx
process-source-gpx:
	@echo "▶ Cleaning source GPX files into public files (offline, run locally)…"
	$(PYTHON) scripts/process_source_gpx.py

# ── Full CI pipeline ─────────────────────────────────────────────────────────────
.PHONY: all
all: destinations isochrones serve

# ── Step 1: GPX → destinations.csv ───────────────────────────────────────────
.PHONY: destinations
destinations:
	@echo "▶ Converting GPX to GeoJSON + extracting stats…"
	$(PYTHON) scripts/gpx_to_geojson.py
	@echo "▶ Snapping GPX files to nearest stops…"
	$(PYTHON) scripts/gpx_to_destinations.py

# ── Step 2: Compute isochrones from GTFS (no server needed) ──────────────────
.PHONY: isochrones
isochrones:
	@echo "▶ Computing isochrones from GTFS…"
	$(PYTHON) scripts/fetch_isochrones.py

# ── Serve frontend ────────────────────────────────────────────────────────────
.PHONY: serve
serve:
	@echo "▶ http://localhost:8000"
	cd docs && $(PYTHON) -m http.server 8000

.PHONY: docker-serve
docker-serve:
	docker run --rm \
		-v $(PWD)/docs:/usr/share/nginx/html:ro \
		-p 8000:80 \
		nginx:alpine

# ── Tests ────────────────────────────────────────────────────────────────────
.PHONY: test
test:
	@echo "▶ Running RAPTOR unit tests…"
	$(PYTHON) -m pytest tests/test_raptor.py -v

# Isochrone output tests — fast point-in-polygon checks against built GeoJSON
.PHONY: test-iso
test-iso:
	@echo "▶ Running isochrone output tests…"
	$(PYTHON) -m pytest tests/test_isochrones.py -v

.PHONY: test-all
test-all: test test-iso

# ── Clean ─────────────────────────────────────────────────────────────────────
.PHONY: clean
clean:
	rm -f destinations.csv .dest_region.csv
	rm -f docs/manifest.json docs/isochrones/*.geojson docs/gpx_tracks.geojson
	@echo "✓ Outputs cleaned. GTFS cache preserved in gtfs/"

.PHONY: clean-all
clean-all: clean
	rm -rf gtfs/
	@echo "✓ Full clean."

# ── Status ────────────────────────────────────────────────────────────────────
.PHONY: status
status:
	@echo "GPX files:    $$(find gpx -name '*.gpx' 2>/dev/null | wc -l | tr -d ' ')"
	@echo "Destinations: $$([ -f destinations.csv ] && tail -n +2 destinations.csv | wc -l | tr -d ' ' || echo 0)"
	@echo "Isochrones:   $$(find docs/isochrones -name '*.geojson' 2>/dev/null | wc -l | tr -d ' ')"
	@echo "GTFS CH:      $$([ -f gtfs/ch/stop_times.txt ] && echo cached || echo not cached)"
	@echo "GTFS DE:      $$([ -f gtfs/DE/stop_times.txt ] && echo cached || echo not cached)"
	@echo "GTFS AT-7:       $$([ -f gtfs/at-7/stop_times.txt ] && echo cached || echo not cached)"
	@echo "GTFS AT-5:       $$([ -f gtfs/at-5/stop_times.txt ] && echo cached || echo not cached)"

