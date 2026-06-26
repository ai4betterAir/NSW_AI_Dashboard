# NSW Air Quality Nowcasting Dashboard

This folder is the active Python dashboard/API application. It is now organized
as a self-contained project so the dashboard does not depend on the older
React/Panel folders for reference files.

## Layout

```text
AI_Dashboard_2026/
  app.py                    # FastAPI entrypoint
  dashboard/                # Dash UI entrypoint and assets
  src/nowcasting/           # Reusable application code
    config/paths.py         # Central project paths
    data/                   # Forecast parsing, site metadata, monitoring data
  data/
    dashboard_files/        # Small fallback/sample forecast CSVs
    reference/              # Site details, recommendations, PurpleAir sensors
  api/v1/                   # FastAPI routes
  scripts/                  # Run helpers
  requirements.txt
```

## Run the Dash Dashboard

```bash
cd /mnt/scratch_lustre/ar_ai4ba_scratch/Ai4BetterAir/AI_Nowcasting/AI_Dashboard_2026
PORT=8050 scripts/run_dashboard.sh
```

### Basemap tiles (optional)

Leaflet basemap tiles default to OpenStreetMap. If your host/browser cannot reach the tile server, you can
either disable tiles (markers only) or point to an internal tile service.

```bash
# Markers only (no basemap tiles)
DISABLE_LEAFLET_TILES=1 PORT=8050 scripts/run_dashboard.sh

# Custom tile server (example)
LEAFLET_TILE_URL='https://tile.openstreetmap.org/{z}/{x}/{y}.png' \
LEAFLET_TILE_ATTRIBUTION='&copy; OpenStreetMap contributors' \
PORT=8050 scripts/run_dashboard.sh

# Multiple fallback tile servers (comma-separated)
LEAFLET_TILE_URLS='https://tile.openstreetmap.org/{z}/{x}/{y}.png,https://a.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png' \
PORT=8050 scripts/run_dashboard.sh
```

## Run the API

```bash
cd /mnt/scratch_lustre/ar_ai4ba_scratch/Ai4BetterAir/AI_Nowcasting/AI_Dashboard_2026
PORT=8000 scripts/run_api.sh
```

## Download Monitoring Feeds (AQMS + PurpleAir)

```bash
cd /mnt/scratch_lustre/ar_ai4ba_scratch/Ai4BetterAir/AI_Nowcasting/AI_Dashboard_2026
scripts/download_monitoring_feeds.py --out-dir data/downloads/monitoring --timestamped
```

Optional: override PurpleAir key with `PURPLEAIR_API_KEY` in your environment.

## Data Paths

By default, the dashboard reads forecast CSVs from:

```text
/mnt/scratch_lustre/ar_aichem_scratch/Nawcasting_Dashboard_Files/
```

You can override that deliberately with:

```bash
export CSV_DATA_FILE_PATH=/path/to/forecast/csvs
```

Reference files are stored in:

```text
data/reference/
```

These include NSW site metadata, PurpleAir sensor locations, and the Air
Quality standard recommendations used by the dashboard.
