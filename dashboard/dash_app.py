"""Dash dashboard that mirrors the core React forecast view."""

import json
import math
import os
import re
import sys
from html import escape
from pathlib import Path
from datetime import datetime, timedelta
from functools import lru_cache
from urllib.parse import unquote

import dash
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import Input, Output, State, callback_context, dcc, html, no_update, dash_table, MATCH
from flask import send_from_directory
from dateutil import tz, parser as date_parser

CURRENT_DIR = Path(__file__).resolve().parent
SERVER_PYTHON_ROOT = CURRENT_DIR.parent
SRC_ROOT = SERVER_PYTHON_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from nowcasting.config.paths import RECOMMENDATIONS_JSON
from nowcasting.data.dashboard_data import (
    POLLUTANTS,
    category_for_value,
    load_region_lookup,
    load_sites,
    load_site_lookup,
    normalize_station_name,
    pollutant_display,
    title_case_station_name,
)
from nowcasting.data.file_discovery import CSV_DATA_FILE_PATH, forecast_file_path, get_file_key_params, get_options
from nowcasting.data.obs_data import (
    MONITORING_CATEGORY_COLORS,
    PURPLEAIR_COLOR,
    fetch_observation_history,
    fetch_observations,
    fetch_observations_result,
    fetch_monitoring_and_purpleair_rows,
    fetch_purpleair_sensor_history,
    fetch_purpleair_snapshot,
    load_purpleair_sensors,
    nearest_purpleair_sensor,
    purpleair_clusters,
)
from nowcasting.data.parse_forecast import parse_csv


RECOMMENDATIONS_PATHS = [RECOMMENDATIONS_JSON]


OPTIONS = get_options()
FILE_KEY_PARAMS = get_file_key_params()


GENERAL_HEALTH_GUIDE = []
MONITOR_REFRESH_MS = 2 * 60 * 1000
SYDNEY_TZ = tz.gettz("Australia/Sydney")

# Path to persist the last AQMS per-site snapshot so arrows survive restarts.
# Can be overridden with the DASHBOARD_AQMS_SNAPSHOT_PATH env var.
SNAPSHOT_FILE = os.environ.get("DASHBOARD_AQMS_SNAPSHOT_PATH") or "/tmp/dashboard_aqms_snapshot.json"
# Base map tiles can be disabled for air-gapped / slow networks.
# Default to enabled so users see the basemap without extra env vars.
DISABLE_LEAFLET_TILES = str(os.environ.get("DISABLE_LEAFLET_TILES", "0")).strip().lower() in {"1", "true", "yes", "y"}
LEAFLET_TILE_URL = str(os.environ.get("LEAFLET_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")).strip()
LEAFLET_TILE_URLS = str(os.environ.get("LEAFLET_TILE_URLS", "")).strip()
LEAFLET_TILE_ATTRIBUTION = str(
    os.environ.get(
        "LEAFLET_TILE_ATTRIBUTION",
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    )
).strip()


def _leaflet_tile_url_js():
    """Return a single tile URL as a JS string literal."""
    url = LEAFLET_TILE_URLS or LEAFLET_TILE_URL
    # If multiple URLs provided in LEAFLET_TILE_URLS, use the first.
    if LEAFLET_TILE_URLS:
        parts = [p.strip() for p in LEAFLET_TILE_URLS.split(",") if p.strip()]
        if parts:
            url = parts[0]
    return json.dumps(url)


def _leaflet_tile_urls_js():
    """Return a JS array literal of tile URLs."""
    urls = []
    if LEAFLET_TILE_URLS:
        parts = [p.strip() for p in LEAFLET_TILE_URLS.split(",") if p.strip()]
        urls.extend(parts)
    if not urls and LEAFLET_TILE_URL:
        urls.append(LEAFLET_TILE_URL)
    return json.dumps(urls)


@lru_cache(maxsize=1)
def _site_lookup():
    return load_site_lookup()


@lru_cache(maxsize=1)
def _region_lookup():
    return load_region_lookup()


@lru_cache(maxsize=1)
def _sites_by_id():
    """Return a mapping of site_id (int) -> site dict.

    `load_sites()` may return a dict or a list; normalize to int keys.
    """
    data = load_sites()
    if not data:
        return {}
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            try:
                key = int(k)
            except Exception:
                try:
                    key = int(str(v.get("SiteId") or v.get("Site_Id") or v.get("id") or ""))
                except Exception:
                    continue
            out[key] = v
        return out
    out = {}
    for item in data:
        sid = item.get("SiteId") or item.get("Site_Id") or item.get("id") or item.get("SiteID")
        try:
            key = int(sid)
        except Exception:
            continue
        out[key] = item
    return out


def _overview_normalize_forecast_region(value):
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    if s.lower() in {"all", "all nsw", "all_nsw", "nsw (all forecast regions)"}:
        return "ALL"
    return s


def _overview_merge_series(series_list):
    merged = {}
    for series in series_list:
        for ts, value in (series or {}).items():
            existing = merged.get(ts)
            if existing is None or (value is not None and value > existing):
                merged[ts] = value
    return merged


def _overview_available_forecast_regions(selection):
    selection = selection or {}
    horizon = selection.get("timeScopes")
    model_name = selection.get("models")
    run_date = selection.get("date")
    if not horizon or not model_name or not run_date:
        return []

    pollutants = {"PM2.5", "PM10", "O3"}
    regions = set()
    for kp in FILE_KEY_PARAMS:
        if kp.get("pollutants") not in pollutants:
            continue
        if kp.get("timeScopes") != str(horizon):
            continue
        if kp.get("models") != str(model_name):
            continue
        if kp.get("date") != str(run_date):
            continue
        region = kp.get("regions")
        if region:
            regions.add(region)

    # If sub-regions exist, don't double-count the synthetic "Sydney" region.
    if "Sydney" in regions and any(str(r).startswith("Sydney_") for r in regions):
        regions.discard("Sydney")

    return sorted(regions)


def _overview_region_pm25_summary_rows(pm25_rows):
    """Return per-region summary for PM2.5 using worst (max) site value in each region."""
    return _overview_region_pollutant_summary_rows(pm25_rows, "PM2.5")


def _overview_region_pollutant_summary_rows(aqms_rows, pollutant_label):
    """Return per-region summary for a pollutant using mean across sites in each region."""
    by_region_values = {}
    for row in aqms_rows or []:
        region = str(row.get("region") or "").strip()
        if not region:
            continue
        value = row.get("value")
        try:
            value_num = float(value) if value is not None else None
        except (TypeError, ValueError):
            value_num = None
        if value_num is None:
            continue
        by_region_values.setdefault(region, []).append(value_num)

    rows = []
    for region, values in by_region_values.items():
        if not values:
            continue
        mean_value = sum(values) / float(len(values))
        category_key, colour = category_for_value(pollutant_label, mean_value)
        category_label = category_key.replace("-", " ").title()
        rows.append(
            {
                "region": region,
                "value": mean_value,
                "valueLabel": f"{mean_value:.1f}",
                "category": category_label,
                "categoryColor": colour,
                "station": "--",
            }
        )

    def sort_key(item):
        sev = _overview_category_severity(str(item.get("category") or "").upper())
        return (-sev, -(item.get("value") or -1), str(item.get("region") or ""))

    rows.sort(key=sort_key)
    return rows


def _overview_multi_pollutant_region_rows(monitor_store, pollutants=None):
    """Return per-region snapshot for multiple pollutants (worst site per pollutant)."""
    monitor_store = monitor_store or {}
    pollutants = pollutants or ["PM2.5", "PM10", "O3"]

    pollutant_to_rows = {
        "PM2.5": monitor_store.get("aqmsPm25Rows") or [],
        "PM10": monitor_store.get("aqmsPm10Rows") or [],
        "O3": monitor_store.get("aqmsO3Rows") or [],
    }

    per_pollutant = {}
    for pollutant in pollutants:
        per_pollutant[pollutant] = _overview_region_pollutant_summary_rows(pollutant_to_rows.get(pollutant) or [], pollutant)

    # Build merged region keys.
    regions = set()
    for rows in per_pollutant.values():
        for item in rows or []:
            if item.get("region"):
                regions.add(item.get("region"))

    def _pollutant_payload(region, pollutant):
        match = next((row for row in per_pollutant.get(pollutant, []) if row.get("region") == region), None)
        if match:
            return match
        return {
            "region": region,
            "value": None,
            "valueLabel": "--",
            "category": "No data",
            "categoryColor": _OVERVIEW_SEVERITY_TO_COLOR[-1],
            "station": "--",
        }

    rows = []
    for region in sorted(regions):
        payloads = {p: _pollutant_payload(region, p) for p in pollutants}
        severities = {p: _overview_category_severity(payloads[p].get("category")) for p in pollutants}
        worst_sev = max(severities.values()) if severities else -1
        rows.append(
            {
                "region": region,
                "severity": worst_sev,
                "category": _OVERVIEW_SEVERITY_TO_LABEL.get(worst_sev, "No data"),
                "categoryColor": _OVERVIEW_SEVERITY_TO_COLOR.get(worst_sev, _OVERVIEW_SEVERITY_TO_COLOR[-1]),
                "pollutants": payloads,
            }
        )

    rows.sort(key=lambda item: (-(item.get("severity") if item else -1), str((item or {}).get("region") or "")))
    return rows


def _format_header_timestamp(epoch_seconds):
    if epoch_seconds is None:
        return "--"
    try:
        dt = datetime.fromtimestamp(float(epoch_seconds), tz=tz.gettz("Australia/Sydney"))
    except (TypeError, ValueError, OSError):
        return "--"
    time_label = dt.strftime("%I:%M %p").lstrip("0") or "0"
    tz_label = dt.strftime("%Z")
    return f"{dt.day} {dt.strftime('%b %Y')}, {time_label} {tz_label}".strip()


def _load_recommendations():
    payload = {}
    for path in RECOMMENDATIONS_PATHS:
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            break
        except OSError:
            continue

    if not payload:
        return []

    categories = []
    for category in payload.get("categories", []):
        categories.append(
            {
                "name": category.get("name", ""),
                "class": category.get("class", ""),
                "commonClass": category.get("commonClass", ""),
                "sensitiveGroups": [item.strip() for item in category.get("sensitiveGroups", []) if item.strip()],
                "everyoneElse": [item.strip() for item in category.get("everyoneElse", []) if item.strip()],
            }
        )
    return categories


GENERAL_HEALTH_GUIDE = _load_recommendations()
NSW_MAP_CENTER = {"lat": -33.0, "lon": 147.032179}
NSW_MAP_BOUNDS = {
    "west": 130.9992792,
    "east": 163.638889,
    "south": -37.50528021,
    "north": -28.15701999,
}
NSW_STATES_GEOJSON = CURRENT_DIR / "assets" / "australian-states.json"
BASE_FONT_FAMILY = '"Source Sans 3", "Aptos", "Segoe UI", Helvetica, Arial, sans-serif'
INITIAL_MONITOR_ROWS = []
INITIAL_MONITOR_STATUS = "Monitoring feed is loading."
INITIAL_FORECAST_PARSED = None
INITIAL_FORECAST_MESSAGE = ""

# Keep a tiny in-process memory of the last NSW PM2.5 network average so the Overview
# card can show an approximate 1-hour trend without extra API calls.
_LAST_NSW_PM25_AVG = {"ts": None, "avg": None}
# Keep previous AQMS averages for PM10 and O3 to show small delta arrows
_LAST_NSW_AQMS = {"ts": None, "pm10": None, "o3": None}
# Keep previous per-site AQMS values so we can render small delta arrows beside station values.
# Keep previous per-site AQMS values so we can render small delta arrows beside station values.
_LAST_AQMS_SNAPSHOT = {"ts": None, "sites": {}}
# Hold the prior snapshot for comparison so UI rendering can compare against the
# snapshot immediately before the latest refresh (prevents overwriting arrows).
_PRIOR_AQMS_SNAPSHOT = None
# Attempt to seed from disk on import so arrows survive restarts
try:
    disk_snap = _load_last_aqms_snapshot_from_disk()
    if disk_snap and (disk_snap.get("sites") or {}):
        _LAST_AQMS_SNAPSHOT = disk_snap
except Exception:
    pass


@lru_cache(maxsize=1)
def _nsw_geojson_geometry():
    try:
        payload = json.loads(NSW_STATES_GEOJSON.read_text(encoding="utf-8"))
    except Exception:
        return None
    for feature in payload.get("features") or []:
        props = feature.get("properties") or {}
        if str(props.get("STATE_NAME") or "").strip().lower() == "new south wales":
            return feature.get("geometry") or None
    return None


def _point_in_ring(lon, lat, ring):
    inside = False
    if not ring:
        return False
    j = len(ring) - 1
    for i, point in enumerate(ring):
        xi, yi = point[:2]
        xj, yj = ring[j][:2]
        if (yi > lat) != (yj > lat):
            cross_lon = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < cross_lon:
                inside = not inside
        j = i
    return inside


def _point_in_polygon(lon, lat, polygon):
    if not polygon:
        return False
    if not _point_in_ring(lon, lat, polygon[0]):
        return False
    return not any(_point_in_ring(lon, lat, hole) for hole in polygon[1:])


def _point_in_nsw(lat, lon):
    try:
        lat_num = float(lat)
        lon_num = float(lon)
    except (TypeError, ValueError):
        return False

    if not (
        NSW_MAP_BOUNDS["west"] <= lon_num <= NSW_MAP_BOUNDS["east"]
        and NSW_MAP_BOUNDS["south"] <= lat_num <= NSW_MAP_BOUNDS["north"]
    ):
        return False

    geometry = _nsw_geojson_geometry()
    if not geometry:
        return True

    coordinates = geometry.get("coordinates") or []
    geom_type = geometry.get("type")
    if geom_type == "Polygon":
        return _point_in_polygon(lon_num, lat_num, coordinates)
    if geom_type == "MultiPolygon":
        return any(_point_in_polygon(lon_num, lat_num, polygon) for polygon in coordinates)
    return True


STATION_SERIES_PALETTE = [
    "#2563eb",
    "#16a34a",
    "#dc2626",
    "#9333ea",
    "#ea580c",
    "#0891b2",
    "#be123c",
    "#4f46e5",
    "#65a30d",
    "#ca8a04",
    "#0f766e",
    "#7c3aed",
    "#db2777",
    "#0284c7",
    "#84cc16",
    "#f97316",
    "#64748b",
    "#991b1b",
]


def _default_value(key):
    values = OPTIONS.get(key, [])
    return values[0] if values else ""


def _parse_run_date(value):
    if not value:
        return None
    text = str(value).strip()
    match = re.search(r"(\d{8})", text)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d")
        except ValueError:
            return None
    return None


def _latest_file_params():
    if not FILE_KEY_PARAMS:
        return None

    def sort_key(candidate):
        parsed_date = _parse_run_date(candidate.get("date")) or datetime.min
        return (
            parsed_date,
            str(candidate.get("models") or ""),
            str(candidate.get("timeScopes") or ""),
            str(candidate.get("regions") or ""),
            str(candidate.get("pollutants") or ""),
        )

    return max(FILE_KEY_PARAMS, key=sort_key)


LATEST_SELECTION = _latest_file_params() or {}

DEFAULT_SELECTION = {
    "regions": LATEST_SELECTION.get("regions", _default_value("regions")),
    "pollutants": LATEST_SELECTION.get("pollutants", _default_value("pollutants")),
    "timeScopes": LATEST_SELECTION.get("timeScopes", _default_value("timeScopes")),
    "models": LATEST_SELECTION.get("models", _default_value("models")),
    "date": LATEST_SELECTION.get("date", _default_value("date")),
}


# Defaults for monitoring dropdowns (avoid import-time NameErrors)
MONITOR_REGION_OPTIONS = [
    {"label": "All NSW", "value": "ALL"},
]
for r in sorted(set(OPTIONS.get("regions") or [])):
    if str(r).upper() != "ALL":
        MONITOR_REGION_OPTIONS.append({"label": str(r), "value": str(r)})

MONITOR_POLLUTANT_OPTIONS = [
    {"label": "PM2.5", "value": "PM2.5"},
    {"label": "PM10", "value": "PM10"},
    {"label": "O3", "value": "O3"},
]

MONITOR_CATEGORY_OPTIONS = [{"label": k.title().replace("_", " "), "value": k} for k in (MONITORING_CATEGORY_COLORS.keys() if 'MONITORING_CATEGORY_COLORS' in globals() else [])]


def _parse_forecast_timestamp(value):
    if not value:
        return None
    text = str(value).strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return date_parser.parse(text)
    except (TypeError, ValueError, OverflowError):
        return None


def _format_forecast_label(value):
    parsed = _parse_forecast_timestamp(value)
    if not parsed:
        return str(value or "--")
    hour = parsed.strftime("%I").lstrip("0") or "0"
    meridiem = parsed.strftime("%p")
    return f"{hour}{meridiem} / {parsed.day} {parsed.strftime('%b %Y')}"


def _forecast_live_base_index(times):
    index, _has_upcoming = _forecast_live_base_state(times)
    return index


def _forecast_live_base_state(times):
    if not times:
        return 0, False
    now = datetime.now(SYDNEY_TZ)
    target = now.replace(minute=0, second=0, microsecond=0)
    if now.minute or now.second or now.microsecond:
        target += timedelta(hours=1)
    fallback = max(len(times) - 1, 0)
    for index, value in enumerate(times):
        parsed = _parse_forecast_timestamp(value)
        if not parsed:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=SYDNEY_TZ)
        else:
            parsed = parsed.astimezone(SYDNEY_TZ)
        if parsed >= target:
            return index, True
    return fallback, False


def _station_plot_axes():
    return dict(
        xaxis=dict(
            title="Longitude",
            range=[NSW_MAP_BOUNDS["west"], NSW_MAP_BOUNDS["east"]],
            gridcolor="#e2e8f0",
            zeroline=False,
        ),
        yaxis=dict(
            title="Latitude",
            range=[NSW_MAP_BOUNDS["south"], NSW_MAP_BOUNDS["north"]],
            gridcolor="#e2e8f0",
            zeroline=False,
        ),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )


def _geo_map_layout(height):
    return dict(
        template="plotly_white",
        height=height,
        margin=dict(l=20, r=20, t=30, b=20),
        geo=dict(
            scope="world",
            projection=dict(type="mercator"),
            lonaxis=dict(range=[NSW_MAP_BOUNDS["west"], NSW_MAP_BOUNDS["east"]]),
            lataxis=dict(range=[NSW_MAP_BOUNDS["south"], NSW_MAP_BOUNDS["north"]]),
            showland=True,
            landcolor="#eef4e8",
            showocean=True,
            oceancolor="#dbeafe",
            showlakes=True,
            lakecolor="#dbeafe",
            showcountries=True,
            countrycolor="#94a3b8",
            showsubunits=True,
            subunitcolor="#94a3b8",
            coastlinecolor="#64748b",
            showcoastlines=True,
            bgcolor="white",
        ),
        paper_bgcolor="white",
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        showlegend=False,
    )


def _parse_monitor_time(row):
    if not row:
        return (datetime.min, 0)
    date_value = str(row.get("date") or "").strip()
    hour_value = row.get("hour")
    parsed_date = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            parsed_date = datetime.strptime(date_value, fmt)
            break
        except ValueError:
            continue
    try:
        parsed_hour = int(hour_value)
    except (TypeError, ValueError):
        parsed_hour = -1
    if not parsed_date:
        return (datetime.min, parsed_hour)
    return (parsed_date, parsed_hour)


def _parse_raw_observation_time(item):
    if not item:
        return (datetime.min, -1)
    date_value = str(item.get("Date") or item.get("date") or "").strip()
    parsed_date = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            parsed_date = datetime.strptime(date_value, fmt)
            break
        except ValueError:
            continue
    try:
        parsed_hour = int(item.get("Hour") or item.get("hour") or -1)
    except (TypeError, ValueError):
        parsed_hour = -1
    if not parsed_date:
        return (datetime.min, parsed_hour)
    return (parsed_date, parsed_hour)


def _format_monitor_label(row):
    if not row:
        return "--"
    hour_description = row.get("hour_description") or "--"
    date_value = row.get("date") or "--"
    return f"{hour_description} / {date_value}"


def _monitor_category_badge_text(row):
    category_key = str(row.get("category_key") or "").strip().upper()
    if category_key:
        return category_key
    category = str(row.get("category") or "No data").strip()
    return category.upper()


def _monitor_category_text_color(row):
    category_key = str(row.get("category_key") or "").strip().upper()
    if category_key in {"VERY POOR", "EXTREMELY POOR"}:
        return "#ffffff"
    return "#082f1b"


def _monitor_station_list(rows):
    station_rows = [row for row in rows or [] if row.get("source") != "PurpleAir"]

    if not station_rows:
        return html.Div(
            [
                html.Div(
                    [
                        html.H3("NSW Air Quality Station"),
                        html.P("Latest AQI observations across NSW"),
                    ],
                    className="monitor-station-list__heading",
                ),
                html.Div(
                    "AQMS station observations will appear after the monitoring feed refreshes.",
                    className="monitor-station-list__empty",
                ),
            ],
            className="monitor-station-list",
        )

    items = []
    for row in station_rows:
        category_color = row.get("category_color") or MONITORING_CATEGORY_COLORS["NO DATA"]
        items.append(
            html.Div(
                [
                    html.Div(className="monitor-station-row__stripe", style={"backgroundColor": category_color}),
                    html.Div(row.get("station") or "Unknown station", className="monitor-station-row__station"),
                    html.Div(
                        [
                            html.Div(row.get("hour_description") or "--"),
                            html.Div(row.get("date") or "--", className="monitor-station-row__date"),
                        ],
                        className="monitor-station-row__time",
                    ),
                    html.Div(
                        _monitor_category_badge_text(row),
                        className="monitor-station-row__badge",
                        style={
                            "backgroundColor": category_color,
                            "color": _monitor_category_text_color(row),
                        },
                    ),
                ],
                className="monitor-station-row",
            )
        )

    return html.Div(
        [
            html.Div(
                [
                    html.H3("NSW Air Quality Station"),
                    html.P("Latest AQI observations across NSW"),
                ],
                className="monitor-station-list__heading",
            ),
            html.Div(
                [
                    html.Div("Stations"),
                    html.Div("Hour"),
                    html.Div("(Category)"),
                ],
                className="monitor-station-list__header",
            ),
            html.Div(items, className="monitor-station-list__body"),
        ],
        className="monitor-station-list",
    )


def _monitoring_time_row(rows):
    if not rows:
        return {}
    return max(rows, key=_parse_monitor_time)


def _monitoring_summary_cards(rows):
    rows = rows or []
    aqms_rows = [row for row in rows if row.get("source") != "PurpleAir"]
    purpleair_rows = [row for row in rows if row.get("source") == "PurpleAir"]
    good_count = sum(1 for row in aqms_rows if row.get("category_key") == "GOOD")
    severe_count = sum(1 for row in aqms_rows if row.get("category_key") in {"POOR", "VERY POOR", "EXTREMELY POOR"})
    worst_row = aqms_rows[0] if aqms_rows else {}
    latest_row = _monitoring_time_row(aqms_rows)
    return [
        html.Div([html.Div("AQMS stations"), html.Strong(str(len(aqms_rows)))], className="summary-card"),
        html.Div([html.Div("PurpleAir sensors"), html.Strong(str(len(purpleair_rows)))], className="summary-card"),
        html.Div([html.Div("Latest snapshot"), html.Strong(_format_monitor_label(latest_row))], className="summary-card"),
        html.Div([html.Div("Good stations"), html.Strong(str(good_count))], className="summary-card"),
        html.Div([html.Div("Poor or worse"), html.Strong(str(severe_count))], className="summary-card"),
        html.Div([html.Div("Most severe"), html.Strong(worst_row.get("category", "No data"))], className="summary-card"),
    ]


def _monitoring_map_figure(rows):
    rows = rows or []
    figure = go.Figure()
    map_center, map_zoom = _mapbox_view(rows)
    if not rows:
        figure.update_layout(
            **_geo_map_layout(460),
        )
        figure.add_annotation(
            text="No monitoring observations are available right now.",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font=dict(family=BASE_FONT_FAMILY, size=16, color="#475569"),
        )
        return figure

    plot_rows = [row for row in rows if row.get("lat") is not None and row.get("lon") is not None]
    if not plot_rows:
        figure.update_layout(
            **_geo_map_layout(460),
        )
        figure.add_annotation(
            text="Monitoring observations are available, but the site coordinates could not be resolved.",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font=dict(family=BASE_FONT_FAMILY, size=16, color="#475569"),
        )
        return figure

    lats = [row.get("lat") for row in plot_rows]
    lons = [row.get("lon") for row in plot_rows]
    colors = [row.get("category_color", "#9ca3af") for row in plot_rows]
    names = [row.get("station", "") for row in plot_rows]
    labels = [row.get("category", "") for row in plot_rows]
    hover_data = list(
        zip(
            names,
            labels,
            [row.get("hour_description", "") for row in plot_rows],
            [row.get("date", "") for row in plot_rows],
            [row.get("determining_pollutant", "") for row in plot_rows],
            [row.get("region", "") for row in plot_rows],
        )
    )

    figure.add_trace(
        go.Scattergeo(
            lon=lons,
            lat=lats,
            mode="markers",
            marker=dict(size=17, color=colors, opacity=0.96),
            customdata=hover_data,
            hovertemplate="<b>%{customdata[0]}</b><br>Category: %{customdata[1]}<br>Time: %{customdata[2]}<br>Date: %{customdata[3]}<br>Determining pollutant: %{customdata[4]}<br>Region: %{customdata[5]}<extra></extra>",
        )
    )
    figure.update_layout(
        **_geo_map_layout(460),
    )
    return figure


def _pollutant_category_label(pollutant_label, colour):
    pollutant = POLLUTANTS.get(pollutant_label) or {}
    for category in pollutant.get("categories") or []:
        if category.get("color") == colour:
            return category.get("label") or "No data"
    return "No data"


def _category_payload(pollutant_label, value, category_hint=None):
    hint = str(category_hint or "").strip().upper()
    if hint and hint not in {"N/A", "NONE"}:
        label = hint.replace("_", " ").title()
        colour = MONITORING_CATEGORY_COLORS.get(hint, MONITORING_CATEGORY_COLORS["NO DATA"])
        return label, colour, hint

    key, colour = category_for_value(pollutant_label, value)
    label = _pollutant_category_label(pollutant_label, colour)
    return label, colour, key.replace("-", " ").upper()



def _aqms_rows_for_pollutant(raw_obs, parameter_code, pollutant_label, source_label="AQMS"):
    raw_obs = raw_obs or []
    latest_by_site = {}
    for item in raw_obs:
        param = item.get("Parameter") or {}
        if param.get("ParameterCode") != parameter_code:
            continue
        if (param.get("Frequency") or "").strip() != "Hourly average":
            continue

        site_id = item.get("Site_Id")
        try:
            site_id_int = int(site_id)
        except (TypeError, ValueError):
            continue
        try:
            hour_value = int(item.get("Hour") or -1)
        except (TypeError, ValueError):
            hour_value = -1
        observation_time = _parse_raw_observation_time(item)
        existing = latest_by_site.get(site_id_int)
        if existing is None or observation_time >= existing["_time"]:
            latest_by_site[site_id_int] = dict(item, _hour=hour_value, _time=observation_time)

    rows = []
    for site_id, item in latest_by_site.items():
        site = _sites_by_id().get(int(site_id), {})
        value = item.get("Value")
        try:
            value_num = float(value) if value is not None else None
        except (TypeError, ValueError):
            value_num = None
        category_label, colour, category_key = _category_payload(pollutant_label, value_num, item.get("AirQualityCategory"))
        rows.append(
            {
                "site_id": int(site_id),
                "station": title_case_station_name(site.get("SiteName") or f"Site {site_id}"),
                "lat": site.get("Latitude"),
                "lon": site.get("Longitude"),
                "region": site.get("Region"),
                "date": item.get("Date"),
                "hour": item.get("Hour"),
                "hour_description": item.get("HourDescription"),
                "value": value_num,
                "value_label": "--" if value_num is None else f"{value_num:.1f}",
                "category": category_label,
                "category_key": category_key,
                "category_color": colour,
                "determining_pollutant": item.get("DeterminingPollutant") or pollutant_label,
                "source": source_label,
            }
        )

    def severity(row):
        key = str(row.get("category") or "").strip().lower()
        order = {"no data": -1, "good": 0, "fair": 1, "poor": 2, "very poor": 3, "extremely poor": 4}
        return order.get(key, -1)

    rows.sort(key=lambda row: (-severity(row), -(row.get("value") or -1), row.get("station") or ""))
    return rows


def _aqms_window_by_region(raw_obs, parameter_code, hours=6, aggregator="mean"):
    """Aggregate an AQMS parameter over the last `hours` for each region.

    Returns {"times": [datetime...], "regions": {region: [value_or_None...]}, "units": str}
    Includes synthetic "All NSW" region aggregated across all sites.
    """
    raw_obs = raw_obs or []
    buckets = {}
    units = ""
    for item in raw_obs:
        param = item.get("Parameter") or {}
        if param.get("ParameterCode") != parameter_code:
            continue
        if (param.get("Frequency") or "").strip() != "Hourly average":
            continue
        if not units:
            units = str(param.get("Units") or "").strip()

        site_id = item.get("Site_Id")
        if site_id is None:
            continue
        site = _sites_by_id().get(int(site_id), {})
        region = str(site.get("Region") or "").strip() or "Unknown"

        ts = _parse_aqms_time_point(item)
        if ts is None:
            continue
        value = item.get("Value")
        try:
            value_num = float(value) if value is not None else None
        except (TypeError, ValueError):
            value_num = None
        if value_num is None:
            continue

        buckets.setdefault(region, {}).setdefault(ts, []).append(value_num)
        buckets.setdefault("All NSW", {}).setdefault(ts, []).append(value_num)

    # Choose a consistent time axis: last N timestamps observed in All NSW bucket.
    all_ts = sorted((buckets.get("All NSW") or {}).keys())
    if not all_ts:
        return {"times": [], "regions": {}, "units": units}
    times = all_ts[-hours:]

    def _agg(values):
        if not values:
            return None
        if aggregator == "circular":
            # Circular mean (degrees). Handles wind direction style data.
            try:
                radians = [math.radians(float(v) % 360.0) for v in values]
            except Exception:
                return None
            sin_sum = sum(math.sin(r) for r in radians)
            cos_sum = sum(math.cos(r) for r in radians)
            if abs(sin_sum) < 1e-12 and abs(cos_sum) < 1e-12:
                return None
            angle = math.degrees(math.atan2(sin_sum / len(radians), cos_sum / len(radians)))
            return (angle + 360.0) % 360.0
        if aggregator == "sum":
            return float(sum(values))
        return float(sum(values)) / float(len(values))

    regions_payload = {}
    for region, per_ts in buckets.items():
        regions_payload[region] = [_agg(per_ts.get(ts)) for ts in times]
    return {"times": times, "regions": regions_payload, "units": units}


@lru_cache(maxsize=64)
def _fetch_met_history(region_name, start_iso, end_iso):
    """Fetch meteorological history rows for a region (cached)."""
    # Deprecated: kept for backward compatibility; callers now pass explicit site ids.
    site_ids = _sites_by_id().keys()
    try:
        return fetch_observation_history(site_ids, ["TEMP", "HUMID", "WSP", "WDR", "RAIN"], start_iso, end_iso, timeout=6)
    except Exception:
        return []


@lru_cache(maxsize=128)
def _fetch_met_history_for_sites(site_ids_tuple, start_iso, end_iso):
    site_ids = [int(x) for x in (site_ids_tuple or ()) if x is not None]
    if not site_ids:
        return []
    try:
        return fetch_observation_history(site_ids, ["TEMP", "HUMID", "WSP", "RAIN"], start_iso, end_iso, timeout=6)
    except Exception:
        return []


def _met_series_for_region(region_name, site_ids, hours=6):
    start_dt, end_dt, start_iso, end_iso = _overview_recent_hours_window(hours=hours)
    ticks = []
    cursor = start_dt
    while cursor <= end_dt:
        ticks.append(cursor)
        cursor = cursor + timedelta(hours=1)

    rows = _fetch_met_history_for_sites(tuple(site_ids or ()), start_iso, end_iso) or []
    # param_code -> ts -> values
    buckets = {}
    units_by_param = {}
    for item in rows:
        param = item.get("Parameter")
        code = ""
        units = ""
        # Parameter may be a dict, a JSON string, or missing; fall back to
        # top-level `ParameterCode`/`Units` created by the cache layer.
        if isinstance(param, str):
            try:
                param = json.loads(param)
            except Exception:
                param = None
        if isinstance(param, dict):
            code = str(param.get("ParameterCode") or "").strip()
            units = str(param.get("Units") or "").strip()
        if not code:
            code = str(item.get("ParameterCode") or "").strip()
            units = units or str(item.get("Units") or "").strip()
        if not code:
            continue
        units_by_param.setdefault(code, units)
        ts = _parse_aqms_time_point(item)
        if ts is None:
            continue
        ts = ts.replace(minute=0, second=0, microsecond=0)
        if ts < start_dt or ts > end_dt:
            continue
        value = item.get("Value")
        try:
            value_num = float(value) if value is not None else None
        except (TypeError, ValueError):
            value_num = None
        if value_num is None:
            continue
        buckets.setdefault(code, {}).setdefault(ts, []).append(value_num)

    def _agg(values, mode):
        if not values:
            return None
        if mode == "circular":
            try:
                radians = [math.radians(float(v) % 360.0) for v in values]
            except Exception:
                return None
            sin_sum = sum(math.sin(r) for r in radians)
            cos_sum = sum(math.cos(r) for r in radians)
            if abs(sin_sum) < 1e-12 and abs(cos_sum) < 1e-12:
                return None
            angle = math.degrees(math.atan2(sin_sum / len(radians), cos_sum / len(radians)))
            return (angle + 360.0) % 360.0
        if mode == "sum":
            return float(sum(values))
        return float(sum(values)) / float(len(values))

    modes = {"TEMP": "mean", "HUMID": "mean", "WSP": "mean", "RAIN": "sum"}
    series = {}
    for code, mode in modes.items():
        per_ts = buckets.get(code) or {}
        series[code] = [_agg(per_ts.get(ts), mode) for ts in ticks]

    return {
        "times": [t.isoformat() for t in ticks],
        "series": series,
        "units": {
            "TEMP": units_by_param.get("TEMP") or "°C",
            "HUMID": units_by_param.get("HUMID") or "%",
            "WSP": units_by_param.get("WSP") or "m/s",
            "RAIN": units_by_param.get("RAIN") or "mm",
        },
    }


def _purpleair_rows_for_pollutant(sensors, pollutant_label):
    if pollutant_label not in {"PM2.5", "PM10"}:
        return []
    rows = []
    for sensor in sensors or []:
        value = sensor.get("pm25") if pollutant_label == "PM2.5" else sensor.get("pm10")
        try:
            value_num = float(value) if value is not None else None
        except (TypeError, ValueError):
            value_num = None
        category_label, colour, category_key = _category_payload(pollutant_label, value_num, None)
        rows.append(
            {
                "site_id": int(sensor.get("sensor_index")) if sensor.get("sensor_index") is not None else None,
                "station": str(sensor.get("name") or "PurpleAir sensor"),
                "lat": sensor.get("lat"),
                "lon": sensor.get("lon"),
                "value": value_num,
                "value_label": "--" if value_num is None else f"{value_num:.1f}",
                "category": category_label,
                "category_key": category_key,
                "category_color": colour,
                "source": "PurpleAir",
                "last_seen": sensor.get("last_seen"),
            }
        )
    rows.sort(key=lambda row: (-(row.get("value") or -1), row.get("station") or ""))
    return rows


def _data_completeness(aqms_rows, purpleair_rows):
    total = 0
    valid = 0
    for row in aqms_rows or []:
        total += 1
        if row.get("value") is not None:
            valid += 1
    for row in purpleair_rows or []:
        total += 1
        if row.get("value") is not None:
            valid += 1
    if total <= 0:
        return 0.0
    return (valid / float(total)) * 100.0


def _monitor_kpi_payload(aqms_rows, purpleair_rows, clusters, latest_label, completeness):
    aqms_total = len(aqms_rows or [])
    purpleair_total = len(purpleair_rows or [])
    good_count = sum(1 for row in aqms_rows or [] if str(row.get("category") or "").strip().lower() == "good")
    poor_or_worse = sum(
        1 for row in aqms_rows or [] if str(row.get("category") or "").strip().lower() in {"poor", "very poor", "extremely poor"}
    )
    worst = (aqms_rows or [{}])[0].get("category") if aqms_rows else "No data"
    return {
        "aqms_total": aqms_total,
        "purpleair_total": purpleair_total,
        "latest_label": latest_label or "--",
        "good": {"count": good_count, "pct": (good_count / aqms_total * 100.0) if aqms_total else 0.0},
        "poor": {"count": poor_or_worse, "pct": (poor_or_worse / aqms_total * 100.0) if aqms_total else 0.0},
        "worst": worst or "No data",
        "completeness": completeness or 0.0,
        "clusters": len(clusters or []),
    }


def _monitor_table_rows(aqms_rows, purpleair_rows, snapshot_by_site=None, max_rows=40):
    rows = []
    snapshot_by_site = snapshot_by_site or {}
    for row in (aqms_rows or [])[: max_rows // 2]:
        site_id = row.get("site_id")
        bucket = snapshot_by_site.get(int(site_id)) if site_id is not None else {}
        pm25_r = bucket.get("PM2.5") if isinstance(bucket, dict) else None
        pm10_r = bucket.get("PM10") if isinstance(bucket, dict) else None
        o3_r = bucket.get("O3") if isinstance(bucket, dict) else None
        rows.append(
            {
                "station": row.get("station"),
                "region": row.get("region") or "--",
                "pollutant": row.get("determining_pollutant") or "--",
                "hour": row.get("hour_description") or "--",
                "source": "AQMS",
                "pm25": _format_value_with_arrow(site_id, "PM2.5", (pm25_r or {}).get("value_label") or "--", (pm25_r or {}).get("value")),
                "pm10": _format_value_with_arrow(site_id, "PM10", (pm10_r or {}).get("value_label") or "--", (pm10_r or {}).get("value")),
                "o3": _format_value_with_arrow(site_id, "O3", (o3_r or {}).get("value_label") or "--", (o3_r or {}).get("value")),
                "pm25_category": (pm25_r or {}).get("category") or "No data",
                "pm10_category": (pm10_r or {}).get("category") or "No data",
                "o3_category": (o3_r or {}).get("category") or "No data",
                "category": row.get("category") or "No data",
                "monitorKey": f"aqms:{row.get('site_id')}",
                "categoryColor": row.get("category_color") or "#9ca3af",
            }
        )
    for row in (purpleair_rows or [])[: max_rows - len(rows)]:
        # PurpleAir sensors mainly provide PM2.5
        rows.append(
            {
                "station": row.get("station"),
                "region": "PurpleAir",
                "pollutant": "PM2.5 / PM10",
                "hour": "--",
                "source": "PurpleAir",
                "pm25": row.get("value_label") or "--",
                "pm10": "--",
                "o3": "--",
                "pm25_category": row.get("category") or "No data",
                "pm10_category": "No data",
                "o3_category": "No data",
                "category": row.get("category") or "No data",
                "monitorKey": f"purpleair:{row.get('site_id')}",
                "categoryColor": row.get("category_color") or "#9ca3af",
            }
        )
    return rows


def _monitor_table_rows_all(aqms_rows, purpleair_rows, snapshot_by_site=None):
    rows = []
    snapshot_by_site = snapshot_by_site or {}
    for row in aqms_rows or []:
        site_id = row.get("site_id")
        bucket = snapshot_by_site.get(int(site_id)) if site_id is not None else {}
        pm25_r = bucket.get("PM2.5") if isinstance(bucket, dict) else None
        pm10_r = bucket.get("PM10") if isinstance(bucket, dict) else None
        o3_r = bucket.get("O3") if isinstance(bucket, dict) else None
        rows.append(
            {
                "station": row.get("station"),
                "region": row.get("region") or "--",
            "pollutant": row.get("determining_pollutant") or "--",
                "hour": row.get("hour_description") or "--",
                "source": "AQMS",
                "pm25": _format_value_with_arrow(site_id, "PM2.5", (pm25_r or {}).get("value_label") or "--", (pm25_r or {}).get("value")),
                "pm10": _format_value_with_arrow(site_id, "PM10", (pm10_r or {}).get("value_label") or "--", (pm10_r or {}).get("value")),
                "o3": _format_value_with_arrow(site_id, "O3", (o3_r or {}).get("value_label") or "--", (o3_r or {}).get("value")),
                "pm25_category": (pm25_r or {}).get("category") or "No data",
                "pm10_category": (pm10_r or {}).get("category") or "No data",
                "o3_category": (o3_r or {}).get("category") or "No data",
                "category": row.get("category") or "No data",
                "monitorKey": f"aqms:{row.get('site_id')}",
                "categoryColor": row.get("category_color") or "#9ca3af",
            }
        )
    for row in purpleair_rows or []:
        rows.append(
            {
                "station": row.get("station"),
                "region": "PurpleAir",
            "pollutant": "PM2.5 / PM10",
                "hour": "--",
                "source": "PurpleAir",
                "pm25": row.get("value_label") or "--",
                "pm10": "--",
                "o3": "--",
                "pm25_category": row.get("category") or "No data",
                "pm10_category": "No data",
                "o3_category": "No data",
                "category": row.get("category") or "No data",
                "monitorKey": f"purpleair:{row.get('site_id')}",
                "categoryColor": row.get("category_color") or "#9ca3af",
            }
        )
    return rows


def _monitor_kpi_cards(kpis):
    kpis = kpis or {}

    def tile(kind, title, value, subtitle, color=None, units=None):
        tile_class = "metric-tile" + (f" metric-tile--{kind}" if kind else "")
        icon_class = "metric-icon" + (f" metric-icon--{kind}" if kind else "") + (f" metric-icon--{color}" if color else "")
        value_class = "metric-value" + (f" metric-value--{color}" if color else "")
        return html.Div(
            [
                html.Div(className=icon_class),
                html.Div(
                    [
                        html.Div(title, className="metric-title"),
                        html.Div(
                            [
                                html.Span(value, className=value_class),
                                html.Span(units, className="metric-units") if units else None,
                            ],
                            className="metric-main",
                        ),
                        html.Div(subtitle or "", className="metric-subtitle"),
                    ],
                    className="metric-body",
                ),
            ],
            className=tile_class,
        )

    good = kpis.get("good") or {}
    poor = kpis.get("poor") or {}
    return [
        tile("active", "AQMS stations", str(kpis.get("aqms_total", 0)), "Active", color="blue"),
        tile("ranked", "PurpleAir sensors", str(kpis.get("purpleair_total", 0)), f"Across {kpis.get('clusters', 0)} clusters", color="purple"),
        tile("steps", "Latest snapshot", str(kpis.get("latest_label") or "--"), "Observation window", color="orange"),
        tile("mean", "Good stations", f"{good.get('count', 0)}", f"{good.get('pct', 0):.0f}% of AQMS", color="green"),
        tile("max", "Poor or worse", f"{poor.get('count', 0)}", f"{poor.get('pct', 0):.0f}% of AQMS", color="orange"),
        tile("region", "Most severe category", str(kpis.get("worst") or "No data"), "AQMS network", color="blue"),
        tile("steps", "Data completeness", f"{kpis.get('completeness', 0):.1f}%", "All sources", color="green"),
        tile("ranked", "PurpleAir clusters", str(kpis.get("clusters", 0)), "Grouped summary", color="purple"),
    ]


def _monitor_table_styles(_table_rows=None):
    # Use value-based styling so sorting/paging doesn't break colours.
    base = [
        {
            "if": {"filter_query": '{category} = "Good"', "column_id": "category"},
            "backgroundColor": "#16a34a",
            "color": "#ffffff",
            "fontWeight": "900",
            "borderRadius": "999px",
        },
        {
            "if": {"filter_query": '{category} = "Fair"', "column_id": "category"},
            "backgroundColor": "#facc15",
            "color": "#0f172a",
            "fontWeight": "900",
            "borderRadius": "999px",
        },
        {
            "if": {"filter_query": '{category} = "Poor"', "column_id": "category"},
            "backgroundColor": "#f97316",
            "color": "#ffffff",
            "fontWeight": "900",
            "borderRadius": "999px",
        },
        {
            "if": {"filter_query": '{category} = "Very poor"', "column_id": "category"},
            "backgroundColor": "#ef4444",
            "color": "#ffffff",
            "fontWeight": "900",
            "borderRadius": "999px",
        },
        {
            "if": {"filter_query": '{category} = "Extremely poor"', "column_id": "category"},
            "backgroundColor": "#7f1d1d",
            "color": "#ffffff",
            "fontWeight": "900",
            "borderRadius": "999px",
        },
        {
            "if": {"filter_query": '{category} = "No data"', "column_id": "category"},
            "backgroundColor": "#9ca3af",
            "color": "#0f172a",
            "fontWeight": "900",
            "borderRadius": "999px",
        },
    ]

    # Add same category-based background colouring for PM2.5, PM10 and O3 columns
    pollutants = [
        ("pm25_category", "pm25"),
        ("pm10_category", "pm10"),
        ("o3_category", "o3"),
    ]
    for cat_field, col in pollutants:
        base.extend(
            [
                {"if": {"filter_query": f'{{{cat_field}}} = "Good"', "column_id": col}, "backgroundColor": "#16a34a", "color": "#ffffff"},
                {"if": {"filter_query": f'{{{cat_field}}} = "Fair"', "column_id": col}, "backgroundColor": "#facc15", "color": "#0f172a"},
                {"if": {"filter_query": f'{{{cat_field}}} = "Poor"', "column_id": col}, "backgroundColor": "#f97316", "color": "#ffffff"},
                {"if": {"filter_query": f'{{{cat_field}}} = "Very poor"', "column_id": col}, "backgroundColor": "#ef4444", "color": "#ffffff"},
                {"if": {"filter_query": f'{{{cat_field}}} = "Extremely poor"', "column_id": col}, "backgroundColor": "#7f1d1d", "color": "#ffffff"},
                {"if": {"filter_query": f'{{{cat_field}}} = "No data"', "column_id": col}, "backgroundColor": "#9ca3af", "color": "#0f172a"},
            ]
        )

    return base


def _attach_arrows_to_table_rows(rows, snapshot_by_site=None):
    """Ensure each AQMS row's pm25/pm10/o3 fields include arrows based on the
    in-process `_LAST_AQMS_SNAPSHOT`. This augments rows passed from the store
    so UI interactions (region filter) still show arrows.
    """
    if not rows:
        return rows
    snapshot_by_site = snapshot_by_site or {}
    # Prefer the prior snapshot (the state before the most recent refresh) when
    # available; fall back to the last snapshot.
    prev_snap = globals().get('_PRIOR_AQMS_SNAPSHOT') or globals().get("_LAST_AQMS_SNAPSHOT") or {}
    prev_sites = (prev_snap or {}).get("sites") or {}
    out = []
    for row in rows:
        r = dict(row or {})
        monitor_key = str(r.get("monitorKey") or "")
        site_id = None
        if monitor_key.startswith("aqms:"):
            try:
                site_id = int(monitor_key.split(":", 1)[1])
            except Exception:
                site_id = None
        # Prefer numeric values from the provided `snapshot_by_site` (store payload)
        bucket = None
        try:
            if site_id is not None:
                bucket = snapshot_by_site.get(int(site_id))
        except Exception:
            bucket = None

        # Fallback to previously saved numeric snapshot
        prev_bucket = None
        try:
            if site_id is not None:
                prev_bucket = prev_sites.get(int(site_id))
        except Exception:
            prev_bucket = None

        def _value_for(pollutant_label):
            # Try store bucket first, then prev_bucket
            try:
                if bucket and isinstance(bucket, dict) and bucket.get(pollutant_label) and bucket.get(pollutant_label).get("value") is not None:
                    return bucket.get(pollutant_label).get("value")
            except Exception:
                pass
            try:
                if prev_bucket and isinstance(prev_bucket, dict):
                    return prev_bucket.get(pollutant_label)
            except Exception:
                pass
            return None

        # If the displayed cell has no numeric value, prefill it from the
        # previously-available snapshot so the UI shows a value immediately.
        def _looks_empty(s):
            try:
                s = str(s or "").strip()
                if not s or s in {"--", "-", "n/a"}:
                    return True
                # If there are no digits, treat as empty
                return not any(ch.isdigit() for ch in s)
            except Exception:
                return True

        # Prefill missing numeric display from previous snapshot (no arrow yet)
        try:
            if _looks_empty(r.get("pm25")):
                val = _value_for("PM2.5")
                if val is not None:
                    r["pm25"] = f"{float(val):.1f}"
        except Exception:
            pass
        try:
            if _looks_empty(r.get("pm10")):
                val = _value_for("PM10")
                if val is not None:
                    r["pm10"] = f"{float(val):.1f}"
        except Exception:
            pass
        try:
            if _looks_empty(r.get("o3")):
                val = _value_for("O3")
                if val is not None:
                    r["o3"] = f"{float(val):.1f}"
        except Exception:
            pass

        # Update display fields only if they don't already contain an arrow
        def _has_arrow(s):
            try:
                return ("↑" in str(s)) or ("↓" in str(s))
            except Exception:
                return False

        if not _has_arrow(r.get("pm25")):
            r["pm25"] = _format_value_with_arrow(site_id, "PM2.5", (r.get("pm25") or "--"), _value_for("PM2.5"))
        if not _has_arrow(r.get("pm10")):
            r["pm10"] = _format_value_with_arrow(site_id, "PM10", (r.get("pm10") or "--"), _value_for("PM10"))
        if not _has_arrow(r.get("o3")):
            r["o3"] = _format_value_with_arrow(site_id, "O3", (r.get("o3") or "--"), _value_for("O3"))
        out.append(r)
    return out



_OVERVIEW_CATEGORY_SEVERITY = {
    "GOOD": 0,
    "FAIR": 1,
    "POOR": 2,
    "VERY POOR": 3,
    "EXTREMELY POOR": 4,
}

_OVERVIEW_SEVERITY_TO_LABEL = {
    -1: "No data",
    0: "Good",
    1: "Fair",
    2: "Poor",
    3: "Very poor",
    4: "Extremely poor",
}

_OVERVIEW_SEVERITY_TO_COLOR = {
    -1: "#9ca3af",
    0: "#16a34a",
    1: "#facc15",
    2: "#f97316",
    3: "#ef4444",
    4: "#7f1d1d",
}


def _hex_to_rgba(value, alpha):
    text = str(value or "").strip()
    try:
        alpha_num = float(alpha)
    except (TypeError, ValueError):
        alpha_num = 0.12
    alpha_num = max(0.0, min(1.0, alpha_num))

    if not text:
        return f"rgba(148, 163, 184, {alpha_num})"
    if text.startswith("rgba(") or text.startswith("rgb("):
        return text

    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join([c * 2 for c in text])
    if len(text) != 6:
        return f"rgba(148, 163, 184, {alpha_num})"

    try:
        r = int(text[0:2], 16)
        g = int(text[2:4], 16)
        b = int(text[4:6], 16)
    except ValueError:
        return f"rgba(148, 163, 184, {alpha_num})"

    return f"rgba({r}, {g}, {b}, {alpha_num})"


def _format_value_with_arrow(site_id, pollutant_label, value_label, value_num):
    """Return a display label with an up/down arrow when value changed since last snapshot.

    Compares against the in-process `_LAST_AQMS_SNAPSHOT` per-site values.
    """
    label = str(value_label or "--")
    try:
        val_num = float(value_num) if value_num is not None else None
    except Exception:
        val_num = None

    prev_sites = (_LAST_AQMS_SNAPSHOT or {}).get("sites") or {}
    prev_val = None
    try:
        if site_id is not None:
            prev_entry = prev_sites.get(int(site_id))
            if isinstance(prev_entry, dict):
                prev_val = prev_entry.get(pollutant_label)
    except Exception:
        prev_val = None

    arrow = ""
    if prev_val is not None and val_num is not None:
        try:
            prev_num = float(prev_val)
            diff = val_num - prev_num
            if abs(diff) >= 0.1:
                arrow = " ↑" if diff > 0 else " ↓"
        except Exception:
            arrow = ""

    return f"{label}{arrow}"


def _load_last_aqms_snapshot_from_disk():
    """Load persisted snapshot from disk if available.

    Returns a dict of the form {'ts': float_or_none, 'sites': {int_site_id: {pollutant: value}}}
    """
    try:
        path = str(SNAPSHOT_FILE)
        if not path:
            return None
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        sites = {}
        for k, v in (payload.get("sites") or {}).items():
            try:
                sid = int(k)
            except Exception:
                sid = k
            sites[sid] = v
        return {"ts": payload.get("ts"), "sites": sites}
    except Exception:
        return None


def _save_last_aqms_snapshot_to_disk(snapshot):
    """Save snapshot to disk. Converts int keys to strings for JSON."""
    try:
        if not snapshot:
            return False
        path = str(SNAPSHOT_FILE)
        dirname = os.path.dirname(path)
        if dirname and not os.path.exists(dirname):
            try:
                os.makedirs(dirname, exist_ok=True)
            except Exception:
                pass
        out = {"ts": snapshot.get("ts"), "sites": {str(k): v for k, v in (snapshot.get("sites") or {}).items()}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh)
        return True
    except Exception:
        return False


def _overview_category_severity(value):
    key = str(value or "").strip().upper()
    return _OVERVIEW_CATEGORY_SEVERITY.get(key, -1)


def _overview_worst_row(rows):
    rows = rows or []
    if not rows:
        return {}
    return max(
        rows,
        key=lambda row: (
            _overview_category_severity(row.get("category_key")),
            str(row.get("region") or ""),
            str(row.get("station") or ""),
        ),
    )


def _overview_region_table_rows(aqc_rows):
    by_region = {}
    for row in aqc_rows or []:
        region = str(row.get("region") or "").strip()
        if not region:
            continue
        existing = by_region.get(region)
        if existing is None:
            by_region[region] = row
            continue
        if _overview_category_severity(row.get("category_key")) > _overview_category_severity(existing.get("category_key")):
            by_region[region] = row

    rows = []
    for region, worst in by_region.items():
        rows.append(
            {
                "region": region,
                "category": worst.get("category") or "No data",
                "pollutant": worst.get("determining_pollutant") or "--",
                "station": worst.get("station") or "--",
            }
        )

    def sort_key(item):
        severity = _overview_category_severity((item or {}).get("category"))
        return (-severity, str((item or {}).get("region") or ""))

    rows.sort(key=sort_key)
    return rows


def _overview_discrete_colorscale(zmin=-1, zmax=4):
    span = float(zmax - zmin)
    if span <= 0:
        return [[0.0, "#9ca3af"], [1.0, "#9ca3af"]]

    def norm(v):
        return max(0.0, min(1.0, (float(v) - float(zmin)) / span))

    # Discretize the heatmap by duplicating boundary stops.
    boundaries = [-1, 0, 1, 2, 3, 4]
    scale = []
    for v in boundaries:
        lo = norm(v)
        hi = norm(v + 1e-6)
        color = _OVERVIEW_SEVERITY_TO_COLOR.get(v, "#9ca3af")
        scale.append([lo, color])
        scale.append([hi, color])
    # Ensure end stop
    scale.append([1.0, _OVERVIEW_SEVERITY_TO_COLOR.get(4, "#7f1d1d")])
    return scale


@lru_cache(maxsize=64)
def _fetch_overview_history(parameter_code, start_iso, end_iso):
    site_ids = sorted([int(k) for k in _sites_by_id().keys()])
    try:
        return fetch_observation_history(site_ids, [parameter_code], start_iso, end_iso, timeout=15)
    except Exception:
        return []


def _overview_pollutant_history_rows(hours=48):
    end_dt = datetime.now(SYDNEY_TZ)
    end_dt = end_dt.replace(minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(hours=hours)
    start_iso = start_dt.strftime("%Y-%m-%dT%H:00:00")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:00:00")
    return start_dt, end_dt, start_iso, end_iso


def _overview_recent_hours_window(hours=6):
    """Return (start_dt, end_dt, start_iso, end_iso) for a recent fixed-size window.

    The returned window contains exactly `hours` hourly ticks (inclusive).
    """
    hours = int(hours) if hours else 6
    hours = max(1, min(hours, 48))
    end_dt = datetime.now(SYDNEY_TZ)
    end_dt = end_dt.replace(minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(hours=hours - 1)
    start_iso = start_dt.strftime("%Y-%m-%dT%H:00:00")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:00:00")
    return start_dt, end_dt, start_iso, end_iso


def _overview_build_trends_figure(hours=48):
    start_dt, end_dt, start_iso, end_iso = _overview_pollutant_history_rows(hours=hours)
    # Hour axis, inclusive
    x_times = []
    cursor = start_dt
    while cursor <= end_dt:
        x_times.append(cursor)
        cursor = cursor + timedelta(hours=1)

    pollutants = [
        ("PM2.5", "PM2.5"),
        ("PM10", "PM10"),
        ("O3", "OZONE"),
        ("NO2", "NO2"),
        ("SO2", "SO2"),
        ("CO", "CO"),
    ]

    y_labels = [label for label, _ in pollutants]
    z = []
    hover_text = []

    for label, code in pollutants:
        history = _fetch_overview_history(code, start_iso, end_iso) or []
        worst_by_hour = {}
        max_value_by_hour = {}
        all_values = []

        for item in history:
            date_text = item.get("Date")
            hour = item.get("Hour")
            if not date_text:
                continue
            try:
                base = datetime.strptime(str(date_text), "%Y-%m-%d")
            except ValueError:
                continue
            try:
                hour_int = int(hour)
            except (TypeError, ValueError):
                continue
            # API can occasionally report hour=24 for midnight boundary.
            if hour_int == 24:
                base = base + timedelta(days=1)
                hour_int = 0
            if hour_int < 0 or hour_int > 23:
                continue
            ts = base.replace(hour=hour_int, tzinfo=SYDNEY_TZ)
            if ts < start_dt or ts > end_dt:
                continue

            severity = _overview_category_severity(item.get("AirQualityCategory"))
            existing = worst_by_hour.get(ts)
            if existing is None or severity > existing:
                worst_by_hour[ts] = severity

            value = item.get("Value")
            try:
                value_num = float(value) if value is not None else None
            except (TypeError, ValueError):
                value_num = None
            if value_num is not None:
                all_values.append(value_num)
                existing_max = max_value_by_hour.get(ts)
                if existing_max is None or value_num > existing_max:
                    max_value_by_hour[ts] = value_num

        row = []
        row_hover = []
        for ts in x_times:
            severity = worst_by_hour.get(ts, -1)
            value_num = max_value_by_hour.get(ts)
            value_label = "--" if value_num is None else f"{value_num:.1f}"
            row.append(severity)
            row_hover.append(f"{label}<br>{ts.strftime('%a %H:%M')}<br>{_OVERVIEW_SEVERITY_TO_LABEL.get(severity, 'No data')}<br>Max value: {value_label}<extra></extra>")
        z.append(row)
        hover_text.append(row_hover)

    fig = go.Figure(
        data=[
            go.Heatmap(
                x=x_times,
                y=y_labels,
                z=z,
                zmin=-1,
                zmax=4,
                colorscale=_overview_discrete_colorscale(-1, 4),
                hoverinfo="text",
                text=hover_text,
                showscale=True,
                colorbar=dict(
                    title="Category",
                    tickmode="array",
                    tickvals=[-1, 0, 1, 2, 3, 4],
                    ticktext=[
                        "No data",
                        "Good",
                        "Fair",
                        "Poor",
                        "Very poor",
                        "Extremely poor",
                    ],
                    thickness=14,
                ),
            )
        ]
    )

    fig.update_layout(
        template="plotly_white",
        height=280,
        margin=dict(l=40, r=20, t=30, b=30),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        title=dict(text=f"Pollutant trends (last {hours} hours)", x=0, xanchor="left", font=dict(size=16, family=BASE_FONT_FAMILY)),
    )
    fig.update_xaxes(
        showgrid=False,
        tickformat="%H:%M\n%d %b",
        ticks="outside",
        tickfont=dict(size=10),
    )
    fig.update_yaxes(showgrid=False, tickfont=dict(size=12))
    return fig


def _overview_build_forecast_trends_figure(region, selection_for_panel):
    """Build a regional forecast trends Plotly figure.

    Plots per-station forecast series (faint), the regional mean (prominent),
    and highlights the single worst station (dashed). Falls back to a user
    friendly annotation when no forecast data is available.
    """
    # Plot multiple pollutants together for comparison.
    pollutants = ["PM2.5", "PM10", "O3"]
    horizon = str((selection_for_panel or {}).get("timeScopes") or "12")
    model_name = str((selection_for_panel or {}).get("models") or "")
    run_date = str((selection_for_panel or {}).get("date") or "")

    try:
        max_hours = int(horizon)
    except (TypeError, ValueError):
        max_hours = 12

    # First, discover a common time axis using the first available pollutant file.
    xs = None
    parsed_for_pollutant = {}
    for p in pollutants:
        fp = forecast_file_path([region, p, horizon, model_name, run_date])
        if not fp:
            # fallback: attempt any model that matches date/horizon/region
            for kp in FILE_KEY_PARAMS:
                if kp.get("regions") != region:
                    continue
                if str(kp.get("pollutants")) != str(p):
                    continue
                if str(kp.get("timeScopes")) != str(horizon):
                    continue
                if str(kp.get("date")) != str(run_date):
                    continue
                fp = forecast_file_path([kp.get("regions"), kp.get("pollutants"), kp.get("timeScopes"), kp.get("models"), kp.get("date")])
                if fp:
                    break
        if not fp:
            continue
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            mtime = 0
        parsed = _cached_parse_csv(fp, p, max_hours, mtime)
        if not parsed:
            continue
        parsed_for_pollutant[p] = parsed
        if xs is None:
            times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime") or []
            times = list(times)[:max_hours]
            xs = [_parse_forecast_timestamp(t) for t in times]
            # Ensure timezone
            xs = [((dt.replace(tzinfo=SYDNEY_TZ) if dt and dt.tzinfo is None else dt) if dt else None) for dt in xs]

    # tighter stacked subplots with equal row heights
    # three small panels (one pollutant per row) showing the regional average bars
    # remove top subplot titles; pollutant name will be shown on the y-axis (two lines)
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06, subplot_titles=("", "", ""))
    fig.update_layout(
        template="plotly_white",
        height=360,
        margin=dict(l=12, r=20, t=48, b=30),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        showlegend=False,
        bargap=0.32,
        bargroupgap=0.06,
    )

    if not xs:
        fig.add_annotation(text="No forecast time axis available for this region.", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False, font=dict(size=13, color="#475569"))
        return fig

    color_map = {"PM2.5": "#2563eb", "PM10": "#16a34a", "O3": "#f97316"}

    any_data = False
    # Plot regional mean per pollutant
    for p in pollutants:
        parsed = parsed_for_pollutant.get(p)
        if not parsed:
            continue
        any_data = True
        stations = (parsed.get("data") or {}).get("stations") or {}
        # build per-station series aligned to xs length
        station_series = {}
        for station_name, payload in stations.items():
            vals = (payload or {}).get("forecastValue") or []
            ys = []
            for idx in range(len(xs)):
                try:
                    v = float(vals[idx])
                except (TypeError, ValueError, IndexError):
                    v = None
                ys.append(v)
            station_series[station_name] = ys

        # compute mean across stations per hour
        mean_vals = []
        for i in range(len(xs)):
            vals = [s[i] for s in station_series.values() if s[i] is not None]
            mean_vals.append((sum(vals) / len(vals)) if vals else None)

        # Prepare per-point colors based on category severity for the mean values
        def _hex_to_rgba(h, a=0.28):
            try:
                h = str(h or "").lstrip("#")
                if len(h) == 3:
                    h = "".join([c * 2 for c in h])
                r = int(h[0:2], 16)
                g = int(h[2:4], 16)
                b = int(h[4:6], 16)
                return f"rgba({r},{g},{b},{a})"
            except Exception:
                return f"rgba(100,116,139,{a})"

        base_fill_rgba = _hex_to_rgba(color_map.get(p))
        fill_colors = []
        edge_colors = []
        y_vals = []
        category_labels = []
        for v in mean_vals:
            if v is None:
                fill_colors.append("rgba(0,0,0,0)")
                edge_colors.append("rgba(0,0,0,0)")
                y_vals.append(None)
                category_labels.append("No data")
                continue
            _, edge = category_for_value(p, v)
            edge = edge or "#64748b"
            fill_colors.append(base_fill_rgba)
            edge_colors.append(edge)
            y_vals.append(v)
            category_labels.append(category_for_value(p, v)[0].replace("-", " ").title())

        # Place each pollutant in its own subplot row (PM2.5 -> row1, PM10 -> row2, O3 -> row3)
        row_idx = {"PM2.5": 1, "PM10": 2, "O3": 3}.get(p, 1)
        units = (POLLUTANTS.get(p) or {}).get("Units", "")
        customdata = [[(category_labels[i] if i < len(category_labels) else "No data")] for i in range(len(y_vals))]
        hover_templ = f"<b>{p}</b><br>%{{x|%d %b %H:%M}}<br>%{{y:.1f}} {units}<br>Category: %{{customdata[0]}}<extra></extra>"
        fig.add_trace(
            go.Bar(
                x=xs,
                y=y_vals,
                name=p,
                marker=dict(color=fill_colors, line=dict(color=edge_colors, width=1.6)),
                customdata=customdata,
                hovertemplate=hover_templ,
                showlegend=False,
            ),
            row=row_idx,
            col=1,
        )

    if not any_data:
        fig.add_annotation(text="No forecast series available for PM2.5/PM10/O3 in this region.", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False, font=dict(size=13, color="#475569"))
        return fig

    # set y-axis title to two lines: pollutant name then unit (on the next line)
    for p in pollutants:
        row_idx = {"PM2.5": 1, "PM10": 2, "O3": 3}.get(p, 1)
        units = (POLLUTANTS.get(p) or {}).get("Units", "")
        # two-line label: pollutant on first line, units on second line
        label = f"{p}\n({units})" if units else p
        fig.update_yaxes(title_text=label, row=row_idx, col=1, tickfont=dict(size=10))
    # show x-axis labels only on the bottom subplot, horizontal and larger font
    # Build ticks every 6 hours and show the date on a tick when the date changes.
    tick_positions = []
    tick_texts = []
    prev_date = None
    for dt in xs:
        if not isinstance(dt, datetime):
            continue
        # choose ticks at 4-hour intervals
        if dt.hour % 4 == 0:
            tick_positions.append(dt)
            label = dt.strftime("%I %p").lstrip("0")
            date_label = dt.strftime("%d %b").upper()
            if prev_date is None or dt.date() != prev_date:
                label = f"{label}\n{date_label}"
            tick_texts.append(label)
            prev_date = dt.date()

    # Hide tick labels on upper subplots
    fig.update_xaxes(showticklabels=False, row=1, col=1)
    fig.update_xaxes(showticklabels=False, row=2, col=1)
    # Bottom subplot gets formatted ticks
    if tick_positions:
        fig.update_xaxes(
            title_text="Forecast time (AEST)",
            row=3,
            col=1,
            tickangle=0,
            tickfont=dict(size=12),
            tickvals=tick_positions,
            ticktext=tick_texts,
        )
    else:
        fig.update_xaxes(title_text="Forecast time (AEST)", row=3, col=1, tickangle=0, tickfont=dict(size=12))
    fig.update_layout(hovermode="x unified")
    return fig




def _monitor_network_nodes(feeds):
    feeds = feeds or {}
    aqms_status = (feeds.get("aqms") or {}).get("status") or "--"
    pa_status = (feeds.get("purpleair") or {}).get("status") or "--"
    completeness = feeds.get("completeness")
    completeness_label = "--" if completeness is None else f"{float(completeness):.1f}%"
    return html.Div(
        [
            html.Div([html.Span("AQMS feed", className="network-label"), html.Span(aqms_status, className="network-value")], className="network-row"),
            html.Div([html.Span("PurpleAir feed", className="network-label"), html.Span(pa_status, className="network-value")], className="network-row"),
            html.Hr(className="network-divider"),
            html.Div([html.Span("Data completeness", className="network-label"), html.Span(completeness_label, className="network-value")], className="network-row"),
        ],
        className="network-block",
    )


def _build_file_path(selection):
    chosen = [selection.get(key) for key in ["regions", "pollutants", "timeScopes", "models", "date"] if selection.get(key)]
    if len(chosen) != 5:
        return None
    return forecast_file_path(chosen)


def _display_region_label(value):
    text = str(value or "").strip()
    if not text:
        return "--"
    if text.upper() == "ALL":
        return "All stations"
    return title_case_station_name(text.replace("_", " "))


def _forecast_station_dropdown_options(parsed):
    stations = ((parsed or {}).get("data") or {}).get("stations") or {}
    options = []
    for station_name in sorted(stations, key=lambda name: title_case_station_name(str(name))):
        options.append({"label": title_case_station_name(station_name), "value": station_name})
    return options


def _best_matching_file(selection):
    """Return the closest matching CSV path for the current selection."""
    best = None
    best_score = -1
    for candidate in FILE_KEY_PARAMS:
        score = 0
        for key in ["regions", "pollutants", "timeScopes", "models", "date"]:
            selected_value = selection.get(key)
            candidate_value = candidate.get(key)
            if selected_value and selected_value == candidate_value:
                score += 1
        if score > best_score:
            best = candidate
            best_score = score

    if not best:
        return None, False

    exact_path = forecast_file_path(
        [
            best["regions"],
            best["pollutants"],
            best["timeScopes"],
            best["models"],
            best["date"],
        ]
    )
    return exact_path, best_score == 5


def _station_rows(parsed, pollutant_label, hour_index):
    rows = []
    if not parsed:
        return rows

    stations = parsed.get("data", {}).get("stations", {})
    for station_name, payload in stations.items():
        forecast_values = payload.get("forecastValue", [])
        if not forecast_values:
            continue
        if hour_index >= len(forecast_values):
            value = forecast_values[-1]
        else:
            value = forecast_values[hour_index]
        site = _site_lookup().get(normalize_station_name(station_name))
        if not site:
            continue
        category, color = category_for_value(pollutant_label, value)
        rows.append(
            {
                "station_key": station_name,
                "station": title_case_station_name(station_name),
                "value": value,
                "category": category.replace("-", " ").title(),
                "color": color,
                "lat": site.get("Latitude"),
                "lon": site.get("Longitude"),
                "region": site.get("Region"),
                "timestamp": parsed.get("data", {}).get("time", {}).get("forecastTime", []),
            }
        )

    rows.sort(key=lambda row: row["value"] if row["value"] is not None else float("-inf"), reverse=True)
    return rows


@lru_cache(maxsize=128)
def _cached_parse_csv(file_path, pollutant_label, max_forecast_hours, mtime):
    """Cache `parse_csv` results by path + mtime to keep Overview interactions snappy."""
    parsed = parse_csv(file_path, pollutant_label=pollutant_label, max_forecast_hours=max_forecast_hours)
    if isinstance(parsed, dict) and parsed.get("error"):
        return None
    return parsed


def _find_station_forecast_value(parsed, station_key, hour_index=0):
    """Return (station_name, value) for a station in a parsed forecast dict."""
    if not parsed or not station_key:
        return None, None
    stations = (parsed.get("data") or {}).get("stations") or {}
    target = normalize_station_name(station_key)
    for station_name, payload in stations.items():
        if normalize_station_name(station_name) != target:
            continue
        values = (payload or {}).get("forecastValue") or []
        if not values:
            return station_name, None
        idx = hour_index if isinstance(hour_index, int) else 0
        idx = max(0, min(idx, len(values) - 1))
        try:
            return station_name, float(values[idx])
        except (TypeError, ValueError):
            return station_name, None
    return None, None


def _overview_next3_values(selection):
    """Return (time_labels, rows) for the 'Next 2 hours forecast' grid.

    Aggregation: worst (max) across all forecast stations across all available regions.
    """
    selection_for_panel, regions = _overview_pick_multi_region_forecast_selection(selection or {})
    model_name = selection_for_panel.get("models")
    run_date = selection_for_panel.get("date")
    horizon = selection_for_panel.get("timeScopes") or "12"

    try:
        max_hours = int(horizon)
    except (TypeError, ValueError):
        max_hours = 12

    hours = min(2, max_hours)
    pollutants = ["PM2.5", "PM10", "O3"]

    # Discover the next-hour timestamps (use the first available parsed file).
    time_labels = []
    for region in regions or []:
        fp = forecast_file_path([str(region), "PM2.5", str(horizon), str(model_name), str(run_date)])
        if not fp:
            continue
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            mtime = 0
        parsed = _cached_parse_csv(fp, "PM2.5", max_hours, mtime)
        if not parsed:
            continue
        times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime") or []
        if times:
            time_labels = list(times[:hours])
            break

    # Compute per pollutant, worst across stations for each hour index, plus contributing station counts
    # and the station name responsible for the maximum.
    rows = []
    for pollutant in pollutants:
        maxima = [None for _ in range(hours)]
        max_stations = [None for _ in range(hours)]
        counts = [0 for _ in range(hours)]
        used_model = model_name

        def _iter_files_for_model(model_value):
            for region in regions or []:
                fp = forecast_file_path([str(region), pollutant, str(horizon), str(model_value), str(run_date)])
                if fp:
                    yield fp

        file_paths = list(_iter_files_for_model(model_name))
        if not file_paths:
            # Model fallback: accept any model for this pollutant/date/horizon.
            used_model = None
            for kp in FILE_KEY_PARAMS:
                if kp.get("pollutants") != pollutant:
                    continue
                if kp.get("timeScopes") != str(horizon):
                    continue
                if kp.get("date") != str(run_date):
                    continue
                region = kp.get("regions")
                if region and regions and region not in regions:
                    continue
                fp = forecast_file_path([kp.get("regions"), pollutant, str(horizon), kp.get("models"), str(run_date)])
                if fp:
                    file_paths.append(fp)

        for fp in file_paths:
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                mtime = 0
            parsed = _cached_parse_csv(fp, pollutant, max_hours, mtime)
            if not parsed:
                continue
            stations = ((parsed.get("data") or {}).get("stations") or {})
            for station_name, payload in stations.items():
                values = (payload or {}).get("forecastValue") or []
                for idx in range(hours):
                    if idx >= len(values):
                        continue
                    try:
                        val = float(values[idx])
                    except (TypeError, ValueError):
                        continue
                    counts[idx] += 1
                    current = maxima[idx]
                    if current is None or val > current:
                        maxima[idx] = val
                        max_stations[idx] = station_name

        rows.append({"pollutant": pollutant, "values": maxima, "counts": counts, "stations": max_stations, "model": used_model})
    return time_labels, rows


def _overview_station_outlook(selection):
    """Return a compact station matrix for the next live forecast hour."""
    selection_for_panel, regions = _overview_pick_multi_region_forecast_selection(selection or {})
    model_name = selection_for_panel.get("models")
    run_date = selection_for_panel.get("date")
    horizon = selection_for_panel.get("timeScopes") or "12"

    try:
        max_hours = int(horizon)
    except (TypeError, ValueError):
        max_hours = 12

    pollutants = ["PM2.5", "PM10", "O3"]
    units_by_pollutant = {p: (POLLUTANTS.get(p) or {}).get("Units") or "" for p in pollutants}
    station_rows = {}
    station_order_scores = {}
    time_labels = []
    live_index = 0
    any_upcoming = False
    any_forecast_data = False
    latest_available_time = None

    def _files_for_pollutant(pollutant):
        files = []
        for region_name in regions or []:
            fp = forecast_file_path([str(region_name), pollutant, str(horizon), str(model_name), str(run_date)])
            if fp:
                files.append(fp)
        if files:
            return files
        for kp in FILE_KEY_PARAMS:
            if kp.get("pollutants") != pollutant:
                continue
            if kp.get("timeScopes") != str(horizon) or kp.get("date") != str(run_date):
                continue
            region_name = kp.get("regions")
            if region_name and regions and region_name not in regions:
                continue
            fp = forecast_file_path([kp.get("regions"), pollutant, kp.get("timeScopes"), kp.get("models"), kp.get("date")])
            if fp:
                files.append(fp)
        return files

    for pollutant in pollutants:
        for fp in _files_for_pollutant(pollutant):
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                mtime = 0
            parsed = _cached_parse_csv(fp, pollutant, max_hours, mtime)
            if not parsed:
                continue
            times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime") or []
            idx, has_upcoming = _forecast_live_base_state(times)
            if times and not time_labels:
                live_index = idx
                time_labels = list(times)
            for time_value in times:
                parsed_time = _parse_forecast_timestamp(time_value)
                if parsed_time is None:
                    continue
                if parsed_time.tzinfo is None:
                    parsed_time = parsed_time.replace(tzinfo=SYDNEY_TZ)
                else:
                    parsed_time = parsed_time.astimezone(SYDNEY_TZ)
                if latest_available_time is None or parsed_time > latest_available_time:
                    latest_available_time = parsed_time
            any_upcoming = any_upcoming or has_upcoming
            stations = ((parsed.get("data") or {}).get("stations") or {})
            for station_name, payload in stations.items():
                values = (payload or {}).get("forecastValue") or []
                if idx >= len(values):
                    continue
                try:
                    value = float(values[idx])
                except (TypeError, ValueError):
                    continue
                station_key = normalize_station_name(station_name)
                if not station_key:
                    continue
                row = station_rows.setdefault(
                    station_key,
                    {"station": title_case_station_name(station_name), "values": {}},
                )
                row["values"][pollutant] = value
                if pollutant == "PM2.5":
                    station_order_scores[station_key] = value
                any_forecast_data = True

    if not any_forecast_data:
        latest_label = ""
        if latest_available_time is not None:
            ended_at = latest_available_time + timedelta(hours=1)
            latest_label = f" Latest available forecast ended at {ended_at.strftime('%-I%p')} / {ended_at.day} {ended_at.strftime('%b %Y')}."
        return html.Div(f"No forecast station data is available from the loaded files.{latest_label}", className="card-hint"), "No forecast data"

    if not station_rows:
        return html.Div("No forecast station data available.", className="card-hint"), ""

    ordered_keys = sorted(
        station_rows,
        key=lambda key: (station_order_scores.get(key) is None, -(station_order_scores.get(key) or -1), station_rows[key]["station"]),
    )

    def _time_label():
        if not time_labels:
            return ""
        start_dt = _parse_forecast_timestamp(time_labels[min(live_index, len(time_labels) - 1)])
        end_dt = None
        if live_index + 1 < len(time_labels):
            end_dt = _parse_forecast_timestamp(time_labels[live_index + 1])
        if start_dt and not end_dt:
            end_dt = start_dt + timedelta(hours=1)
        if start_dt and end_dt:
            start_hour = start_dt.strftime("%I").lstrip("0") or "0"
            end_hour = end_dt.strftime("%I").lstrip("0") or "0"
            label = f"@ {start_hour}{start_dt.strftime('%p')}-{end_hour}{end_dt.strftime('%p')} / {start_dt.day} {start_dt.strftime('%b %Y')}"
        else:
            label = f"@ {_format_forecast_label(time_labels[min(live_index, len(time_labels) - 1)])}"
        return label if any_upcoming else f"Latest available {label}"

    matrix_rows = []
    station_count = len(ordered_keys)
    grid_template = f"72px repeat({station_count}, var(--station-card-width))"
    for pollutant in pollutants:
        units = units_by_pollutant.get(pollutant) or ""
        cards = [
            html.Div(
                [
                    html.Div(pollutant, className="overview-station-matrix__pollutant-name"),
                    html.Div(units, className="overview-station-matrix__pollutant-unit"),
                ],
                className="overview-station-matrix__pollutant",
            )
        ]
        for station_key in ordered_keys:
            station = station_rows[station_key]
            value = station.get("values", {}).get(pollutant)
            if value is None:
                category_key, colour = "no-data", _leaflet_category_colour("No data")
                value_text = "--"
            else:
                category_key, colour = category_for_value(pollutant, value)
                value_text = f"{value:.1f} {units}".strip()
            category_label = str(category_key or "no-data").replace("-", " ").upper()
            cards.append(
                html.Div(
                    [
                        html.Div(station["station"], className="overview-station-matrix__station"),
                        html.Div(category_label, className="overview-station-matrix__category", style={"color": colour}),
                        html.Div(value_text, className="overview-station-matrix__value"),
                    ],
                    className="overview-station-matrix__card",
                    style={"borderLeftColor": colour},
                )
            )
        matrix_rows.append(
            html.Div(
                cards,
                className="overview-station-matrix__row",
                style={"gridTemplateColumns": grid_template},
            )
        )

    return (
        html.Div(
            html.Div(html.Div(matrix_rows, className="overview-station-matrix__rows"), className="overview-station-matrix__viewport"),
            className="overview-station-matrix",
        ),
        _time_label(),
    )


def _overview_compact_forecast_time_label(value):
    parsed = _parse_forecast_timestamp(value)
    if not parsed:
        return str(value or "--")
    return f"{parsed.strftime('%b').upper()} {parsed.day} {parsed.strftime('%I').lstrip('0') or '0'}{parsed.strftime('%p')}"


def _overview_region_bar_row(item):
    item = item or {}
    category = item.get("category") or "No data"
    colour = item.get("categoryColor") or _leaflet_category_colour(category)
    return html.Div(
        [
            html.Div(title_case_station_name(str(item.get("region") or "--").replace("_", " ")), className="overview-pm25-row__region"),
            html.Div(html.Div(className="overview-forecast-bar__fill", style={"backgroundColor": colour}), className="overview-forecast-bar"),
            html.Div(category, className="overview-pm25-row__category", style={"color": colour}),
        ],
        className="overview-pm25-row",
    )


def _overview_region_bar_section(pollutant, rows, empty_message="No regional data available."):
    rows = rows or []
    return html.Div(
        [
            html.H5(pollutant, className="overview-forecast-pollutant__title"),
            html.Div(
                [_overview_region_bar_row(row) for row in rows] if rows else [html.Div(empty_message, className="card-hint")],
                className="overview-forecast-list",
            ),
        ],
        className="overview-forecast-pollutant",
    )


def _overview_forecast_region_rows(selection, pollutant):
    selection_for_panel, regions = _overview_pick_multi_region_forecast_selection(selection or {})
    if not regions:
        return [], "--"

    horizon = str(selection_for_panel.get("timeScopes") or "12")
    model_name = str(selection_for_panel.get("models") or "")
    run_date = str(selection_for_panel.get("date") or "")
    try:
        max_hours = int(horizon)
    except (TypeError, ValueError):
        max_hours = 12

    rows = []
    time_label = "--"
    any_upcoming = False
    for region in sorted(regions, key=lambda value: title_case_station_name(str(value).replace("_", " "))):
        fp = forecast_file_path([str(region), pollutant, horizon, model_name, run_date])
        if not fp:
            for kp in FILE_KEY_PARAMS:
                if kp.get("regions") != region or kp.get("pollutants") != pollutant:
                    continue
                if str(kp.get("timeScopes")) != horizon or str(kp.get("date")) != run_date:
                    continue
                fp = forecast_file_path([kp.get("regions"), kp.get("pollutants"), kp.get("timeScopes"), kp.get("models"), kp.get("date")])
                if fp:
                    break
        if not fp:
            continue
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            mtime = 0
        parsed = _cached_parse_csv(fp, pollutant, max_hours, mtime)
        if not parsed:
            continue
        times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime") or []
        idx, has_upcoming = _forecast_live_base_state(times)
        any_upcoming = any_upcoming or has_upcoming
        if time_label == "--" and times:
            compact_label = _overview_compact_forecast_time_label(times[max(0, min(idx, len(times) - 1))])
            time_label = compact_label if has_upcoming else f"Latest available {compact_label}"
        values = []
        for payload in ((parsed.get("data") or {}).get("stations") or {}).values():
            forecast_values = (payload or {}).get("forecastValue") or []
            if idx >= len(forecast_values):
                continue
            try:
                values.append(float(forecast_values[idx]))
            except (TypeError, ValueError):
                continue
        value = (sum(values) / float(len(values))) if values else None
        category_key, colour = category_for_value(pollutant, value)
        rows.append(
            {
                "region": region,
                "value": value,
                "valueLabel": "--" if value is None else f"{value:.1f}",
                "category": str(category_key or "no-data").replace("-", " ").title(),
                "categoryColor": colour,
            }
        )
    rows.sort(key=lambda item: title_case_station_name(str(item.get("region") or "").replace("_", " ")))
    if rows and not any_upcoming and time_label != "--" and not str(time_label).startswith("Latest available"):
        time_label = f"Latest available {time_label}"
    if not rows and time_label == "--":
        time_label = "No forecast data"
    return rows, time_label


def _ranking_rows(parsed):
    rows = []
    if not parsed:
        return rows
    ranking = parsed.get("ranking", {})
    for station_name, payload in ranking.items():
        value = payload.get("maxValue")
        rows.append(
            {
                "station": title_case_station_name(station_name),
                "max_value": None if value is None else round(float(value), 2),
                "timestamp": _format_forecast_label(payload.get("timestamp")),
            }
        )
    rows.sort(key=lambda row: row["max_value"] if row["max_value"] is not None else float("-inf"), reverse=True)
    return rows


def _safe_hour_index(value, length):
    try:
        idx = int(round(float(value or 0)))
    except (TypeError, ValueError):
        idx = 0
    if length <= 0:
        return 0
    return max(0, min(idx, length - 1))


def _ranking_bar_figure(parsed, pollutant_label, hour_index=0, limit=10):
    rows = []
    if parsed:
        stations = parsed.get("data", {}).get("stations", {}) or {}
        times = parsed.get("data", {}).get("time", {}).get("forecastTime", []) or []
        idx = _safe_hour_index(hour_index, len(times))
        selected_time = times[idx] if idx < len(times) else None
        for station_name, payload in stations.items():
            series = (payload or {}).get("forecastValue") or []
            if idx >= len(series):
                continue
            value = series[idx]
            if value is None:
                continue
            value = float(value)
            category, color = category_for_value(pollutant_label, value)
            rows.append(
                {
                    "station_key": station_name,
                    "station": title_case_station_name(station_name),
                    "value": value,
                    "timestamp": _format_forecast_label(selected_time),
                    "category": category.replace("-", " ").title(),
                    "color": color,
                }
            )

    rows.sort(key=lambda row: row["value"], reverse=True)
    rows_for_chart = list(reversed(rows[: max(1, int(limit or 10))]))
    figure = go.Figure()

    if not rows_for_chart:
        figure.update_layout(
            template="plotly_white",
            height=330,
            margin=dict(l=20, r=20, t=20, b=20),
            font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
            annotations=[
                dict(
                    text="No station forecast values are available for this hour.",
                    x=0.5,
                    y=0.5,
                    xref="paper",
                    yref="paper",
                    showarrow=False,
                    font=dict(size=14, color="#64748b"),
                )
            ],
        )
        return figure

    figure.add_trace(
        go.Bar(
            x=[row["value"] for row in rows_for_chart],
            y=[row["station"] for row in rows_for_chart],
            orientation="h",
            marker=dict(color=[row["color"] for row in rows_for_chart]),
            text=[row["category"] for row in rows_for_chart],
            textposition="outside",
            cliponaxis=False,
            customdata=[
                [row["station_key"], row["category"], row["timestamp"]]
                for row in rows_for_chart
            ],
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Forecast value: %{x:.2f}<br>"
                "Category: %{customdata[1]}<br>"
                "Time: %{customdata[2]}<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=360,
        margin=dict(l=118, r=54, t=16, b=24),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(255,255,255,0.86)",
        showlegend=False,
        bargap=0.28,
        xaxis=dict(
            title="",
            gridcolor="#e2e8f0",
            zeroline=False,
            fixedrange=True,
            showticklabels=False,
            showgrid=False,
        ),
        yaxis=dict(
            title="",
            fixedrange=True,
            automargin=True,
        ),
    )
    return figure


def _forecast_file_for_pollutant(selection, pollutant_label):
    selection = selection or {}
    region = str(selection.get("regions") or "")
    horizon = str(selection.get("timeScopes") or "")
    model = str(selection.get("models") or "")
    run_date = str(selection.get("date") or "")
    file_path = forecast_file_path([region, pollutant_label, horizon, model, run_date])
    if file_path:
        return file_path
    for kp in FILE_KEY_PARAMS:
        if str(kp.get("regions") or "") != region:
            continue
        if str(kp.get("pollutants") or "") != str(pollutant_label):
            continue
        if str(kp.get("timeScopes") or "") != horizon:
            continue
        if str(kp.get("date") or "") != run_date:
            continue
        file_path = forecast_file_path([kp.get("regions"), kp.get("pollutants"), kp.get("timeScopes"), kp.get("models"), kp.get("date")])
        if file_path:
            return file_path
    return None


def _forecast_regions_for_map(selection, pollutant_label):
    selection = selection or {}
    horizon = str(selection.get("timeScopes") or "")
    run_date = str(selection.get("date") or "")
    if not horizon or not run_date or not pollutant_label:
        return []
    regions = set()
    for kp in FILE_KEY_PARAMS:
        if str(kp.get("pollutants") or "") != str(pollutant_label):
            continue
        if str(kp.get("timeScopes") or "") != horizon:
            continue
        if str(kp.get("date") or "") != run_date:
            continue
        region = kp.get("regions")
        if region:
            regions.add(region)
    if "Sydney" in regions and any(str(region).startswith("Sydney_") for region in regions):
        regions.discard("Sydney")
    return sorted(regions)


def _merged_forecast_map_payload(selection, pollutant_label):
    selection = dict(selection or {})
    pollutant_label = pollutant_label or selection.get("pollutants") or "PM2.5"
    horizon = str(selection.get("timeScopes") or "")
    run_date = str(selection.get("date") or "")
    model = str(selection.get("models") or "")
    try:
        max_hours = int(horizon)
    except (TypeError, ValueError):
        max_hours = None

    merged_stations = {}
    merged_times = []
    time_block = None
    for region in _forecast_regions_for_map(selection, pollutant_label):
        region_selection = dict(selection, regions=region, pollutants=pollutant_label)
        file_path = forecast_file_path([region, pollutant_label, horizon, model, run_date]) if model else None
        if not file_path:
            file_path = _forecast_file_for_pollutant(region_selection, pollutant_label)
        if not file_path:
            continue
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            mtime = 0
        parsed = _cached_parse_csv(file_path, pollutant_label, max_hours, mtime)
        if not parsed:
            continue
        parsed_time_block = (parsed.get("data") or {}).get("time") or {}
        if time_block is None and parsed_time_block:
            time_block = dict(parsed_time_block)
        for ts in parsed_time_block.get("forecastTime", []) or []:
            if ts not in merged_times:
                merged_times.append(ts)
        for station_name, payload in ((parsed.get("data") or {}).get("stations") or {}).items():
            if station_name not in merged_stations:
                merged_stations[station_name] = payload

    if time_block is None:
        time_block = {}
    if merged_times:
        time_block["forecastTime"] = merged_times
    return {"data": {"time": time_block, "stations": merged_stations}}


def _station_rank_bars_for_pollutants(parsed, selection, hour_index=0, limit=10):
    selection = dict(selection or {})
    pollutants = ["PM2.5", "PM10", "O3"]

    def _severity(category_key):
        order = {"no-data": -1, "good": 0, "fair": 1, "poor": 2, "very-poor": 3, "extremely-poor": 4}
        return order.get(str(category_key or "no-data"), -1)

    sections = []
    for pollutant in pollutants:
        parsed_for_pollutant = parsed if pollutant == selection.get("pollutants") else None
        if parsed_for_pollutant is None:
            file_path = _forecast_file_for_pollutant(selection, pollutant)
            if file_path:
                try:
                    max_hours = int(selection.get("timeScopes") or 0) or None
                except (TypeError, ValueError):
                    max_hours = None
                try:
                    mtime = os.path.getmtime(file_path)
                except OSError:
                    mtime = 0
                parsed_for_pollutant = _cached_parse_csv(file_path, pollutant, max_hours, mtime)

        stations = (((parsed_for_pollutant or {}).get("data") or {}).get("stations") or {})
        times = (((parsed_for_pollutant or {}).get("data") or {}).get("time") or {}).get("forecastTime") or []
        idx = _safe_hour_index(hour_index, len(times))
        rows = []
        for station_name, payload in stations.items():
            series = (payload or {}).get("forecastValue") or []
            if idx >= len(series):
                continue
            try:
                value = float(series[idx])
            except (TypeError, ValueError):
                continue
            category_key, colour = category_for_value(pollutant, value)
            category_label = str(category_key or "no-data").replace("-", " ").title()
            rows.append(
                {
                    "station": title_case_station_name(station_name),
                    "value": value,
                    "categoryKey": category_key,
                    "category": category_label,
                    "colour": colour,
                }
            )
        rows.sort(key=lambda row: (-_severity(row.get("categoryKey")), -row.get("value", 0), row.get("station") or ""))
        rows = rows[: max(1, int(limit or 10))]

        if rows:
            body = [
                html.Div(
                    [
                        html.Div(row["station"], className="station-rank-bars__station"),
                        html.Div(
                            html.Div(className="station-rank-bars__fill", style={"backgroundColor": row["colour"]}),
                            className="station-rank-bars__track",
                        ),
                        html.Div(row["category"], className="station-rank-bars__category", style={"color": row["colour"]}),
                    ],
                    className="station-rank-bars__row",
                    title=f"{row['station']}: {row['value']:.2f}",
                )
                for row in rows
            ]
        else:
            body = [html.Div("No station forecast values are available for this pollutant.", className="forecast-rank-empty")]

        sections.append(
            html.Div(
                [
                    html.Div(pollutant, className="station-rank-bars__pollutant"),
                    html.Div(body, className="station-rank-bars__rows"),
                ],
                className="station-rank-bars__section",
            )
        )

    return html.Div(sections, className="station-rank-bars")


def _station_series_colour(station_name):
    key = normalize_station_name(station_name or "")
    if not key:
        return STATION_SERIES_PALETTE[0]
    index = sum(ord(char) for char in key) % len(STATION_SERIES_PALETTE)
    return STATION_SERIES_PALETTE[index]


def _station_trend_figure(parsed, pollutant_label, station_name):
    figure = go.Figure()
    if not parsed or not station_name:
        figure.update_layout(
            template="plotly_white",
            height=320,
            margin=dict(l=20, r=20, t=30, b=20),
            font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        )
        return figure

    stations = parsed.get("data", {}).get("stations", {})
    payload = None
    target = normalize_station_name(station_name)
    for key, value in stations.items():
        if normalize_station_name(key) == target:
            payload = value
            break
    if not payload:
        figure.update_layout(
            template="plotly_white",
            height=320,
            margin=dict(l=20, r=20, t=30, b=20),
            font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        )
        return figure

    times = parsed.get("data", {}).get("time", {}).get("forecastTime", [])
    values = payload.get("forecastValue", [])
    colour = _station_series_colour(station_name)
    figure.add_trace(
        go.Scatter(
            x=times[: len(values)],
            y=values,
            mode="lines+markers",
            line=dict(color=colour, width=3),
            marker=dict(size=7, color=colour),
            name=title_case_station_name(station_name),
        )
    )
    pollutant = POLLUTANTS.get(pollutant_label, {})
    figure.update_layout(
        template="plotly_white",
        height=300,
        margin=dict(l=52, r=18, t=42, b=42),
        title=f"{title_case_station_name(station_name)} forecast",
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#ffffff",
        hovermode="x unified",
        showlegend=False,
        xaxis=dict(
            title="",
            tickformat="%H:%M",
            nticks=5,
            showgrid=False,
            tickfont=dict(family=BASE_FONT_FAMILY, color="#334155", size=10),
        ),
        yaxis=dict(
            title=dict(text=pollutant.get("Units", "Value"), font=dict(family=BASE_FONT_FAMILY, color="#0f172a")),
            gridcolor="#e2e8f0",
            zeroline=False,
            tickfont=dict(family=BASE_FONT_FAMILY, color="#334155", size=10),
        ),
    )
    return figure


def _all_stations_trend_figure(parsed, pollutant_label, forecast_horizon=None):
    figure = go.Figure()
    stations = parsed.get("data", {}).get("stations", {}) if parsed else {}
    times = parsed.get("data", {}).get("time", {}).get("forecastTime", []) if parsed else []
    pollutant = POLLUTANTS.get(pollutant_label, {})

    if not stations or not times:
        figure.update_layout(
            template="plotly_white",
            height=300,
            margin=dict(l=52, r=18, t=42, b=42),
            font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        )
        return figure

    def _station_peak(item):
        _station_name, payload = item
        values = payload.get("forecastValue", []) if isinstance(payload, dict) else []
        numeric = []
        for value in values:
            try:
                numeric.append(float(value))
            except (TypeError, ValueError):
                continue
        return max(numeric) if numeric else float("-inf")

    ranked_stations = sorted(stations.items(), key=lambda item: (-_station_peak(item), title_case_station_name(item[0])))[:8]
    parsed_times = [_parse_forecast_timestamp(ts) or ts for ts in times]
    time_axis = _forecast_time_tick_axis(times)
    horizon_text = f"{forecast_horizon}H" if forecast_horizon not in (None, "") else f"{len(times)}H"
    title_text = f"Regional Time Series for {pollutant_label} @ {horizon_text} Forecast Horizon"

    for index, (station_name, payload) in enumerate(ranked_stations):
        values = payload.get("forecastValue", [])
        if not values:
            continue
        colour = STATION_SERIES_PALETTE[index % len(STATION_SERIES_PALETTE)]
        figure.add_trace(
            go.Scatter(
                x=parsed_times[: len(values)],
                y=values,
                mode="lines+markers",
                line=dict(color=colour, width=0.8),
                marker=dict(size=2.4, color=colour, line=dict(width=0.45, color="#ffffff")),
                opacity=0.86,
                name=title_case_station_name(station_name),
                hovertemplate="<b>%{fullData.name}</b><br>%{x|%d %b %H:%M}<br>%{y:.2f}<extra></extra>",
            )
        )

    figure.update_layout(
        template="plotly_white",
        height=310,
        margin=dict(l=56, r=18, t=54, b=58),
        title=dict(text=title_text, x=0, xanchor="left", font=dict(size=13.5, color="#0f172a")),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(255,255,255,0.92)",
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.2,
            xanchor="left",
            x=0,
            font=dict(size=7.5, color="#334155"),
            itemsizing="trace",
            itemwidth=30,
        ),
        xaxis=dict(
            title="",
            showgrid=False,
            ticks="outside",
            tickfont=dict(family=BASE_FONT_FAMILY, color="#334155", size=10),
            **time_axis,
        ),
        yaxis=dict(
            title=dict(text=pollutant.get("Units", "Value"), font=dict(family=BASE_FONT_FAMILY, color="#0f172a", size=11)),
            gridcolor="#e2e8f0",
            zeroline=False,
            tickfont=dict(family=BASE_FONT_FAMILY, color="#334155", size=10.5),
        ),
    )
    return figure


def _all_stations_details(parsed):
    stations = parsed.get("data", {}).get("stations", {}) if parsed else {}
    station_count = len(stations)
    step_count = 0
    latest_values = []
    for payload in stations.values():
        values = payload.get("forecastValue", [])
        step_count = max(step_count, len(values))
        if values:
            latest_values.append(values[-1])

    details = [
        html.Div(["Showing: ", html.Strong("All stations")]),
        html.Div(["Stations: ", html.Strong(str(station_count))]),
        html.Div(["Forecast steps: ", html.Strong(str(step_count))]),
    ]
    if latest_values:
        details.append(html.Div(["Latest range: ", html.Strong(f"{min(latest_values):.2f} to {max(latest_values):.2f}")]))
    return details


def _overview_allstations_value_styles():
    """Cell color rules for all-stations forecast table values."""
    category_palette = [
        ("good", "#16a34a", "#ffffff"),
        ("fair", "#eab308", "#111827"),
        ("poor", "#f97316", "#111827"),
        ("very-poor", "#dc2626", "#ffffff"),
        ("extremely-poor", "#7e0023", "#ffffff"),
        ("no-data", "#9ca3af", "#111827"),
    ]
    styles = []
    for hour in (1, 3, 6, 12):
        for pollutant_key in ("pm25", "pm10", "o3"):
            value_col = f"h{hour}_{pollutant_key}"
            cat_col = f"h{hour}_{pollutant_key}_cat"
            for cat_value, bg_color, text_color in category_palette:
                styles.append(
                    {
                        "if": {"filter_query": f"{{{cat_col}}} = '{cat_value}'", "column_id": value_col},
                        "backgroundColor": bg_color,
                        "color": text_color,
                        "fontWeight": "900",
                    }
                )
    return styles


def _parse_hash_params(url_hash):
    if not url_hash or not isinstance(url_hash, str) or not url_hash.startswith("#"):
        return {}
    text = url_hash[1:]
    if not text:
        return {}
    params = {}
    for part in text.split("&"):
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = (key or "").strip()
        if not key:
            continue
        params[key] = unquote(value or "")
    return params


def _station_from_hash(parsed, url_hash):
    if not parsed:
        return None

    params = _parse_hash_params(url_hash)
    selected = (params.get("station") or "").strip()
    if not selected:
        return None

    selected_norm = normalize_station_name(selected)
    stations = parsed.get("data", {}).get("stations", {})
    for station_name in stations:
        if normalize_station_name(station_name) == selected_norm:
            return station_name
    return None


def _forecast_time_tick_axis(times):
    parsed_times = []
    for value in times or []:
        parsed = _parse_forecast_timestamp(value)
        if parsed is None:
            continue
        parsed_times.append(parsed)
    if not parsed_times:
        return {}

    step = max(1, math.ceil(len(parsed_times) / 7))
    tickvals = []
    ticktext = []
    last_date = None
    for index, parsed in enumerate(parsed_times):
        is_day_change = last_date is not None and parsed.date() != last_date
        if index % step != 0 and not is_day_change and index != len(parsed_times) - 1:
            last_date = parsed.date()
            continue
        hour = parsed.strftime("%I").lstrip("0") or "0"
        label = f"{hour}{parsed.strftime('%p')}"
        if index == 0 or is_day_change:
            label = f"{label}<br>{parsed.day} {parsed.strftime('%b')}"
        tickvals.append(parsed)
        ticktext.append(label)
        last_date = parsed.date()
    return {"tickmode": "array", "tickvals": tickvals, "ticktext": ticktext}


def _monitor_from_hash(url_hash):
    params = _parse_hash_params(url_hash)
    monitor = (params.get("monitor") or "").strip()
    if not monitor:
        return None
    monitor_lower = monitor.lower()
    if monitor_lower.startswith("aqms:"):
        return {"source": "AQMS", "id": monitor.split(":", 1)[1]}
    if monitor_lower.startswith("purpleair:"):
        return {"source": "PurpleAir", "id": monitor.split(":", 1)[1]}
    return None


def _mapbox_view(rows):
    if not rows:
        return NSW_MAP_CENTER, 6.0

    lats = [row["lat"] for row in rows if row.get("lat") is not None]
    lons = [row["lon"] for row in rows if row.get("lon") is not None]
    if not lats or not lons:
        return NSW_MAP_CENTER, 6.0

    lat_span = max(lats) - min(lats)
    lon_span = max(lons) - min(lons)
    span = max(lat_span, lon_span)

    if span <= 0.25:
        zoom = 8.0
    elif span <= 0.5:
        zoom = 7.3
    elif span <= 1.0:
        zoom = 6.8
    elif span <= 2.0:
        zoom = 6.2
    elif span <= 4.0:
        zoom = 5.8
    else:
        zoom = 5.6

    return NSW_MAP_CENTER, zoom


def _map_figure(parsed, pollutant_label, hour_index):
    rows = _station_rows(parsed, pollutant_label, hour_index)
    figure = go.Figure()
    map_center, map_zoom = _mapbox_view(rows)
    if not rows:
        figure.update_layout(
            **_geo_map_layout(620),
        )
        figure.add_annotation(
            text="No forecast stations could be mapped for this selection.",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font=dict(family=BASE_FONT_FAMILY, size=16, color="#475569"),
        )
        return figure

    lats = [row["lat"] for row in rows]
    lons = [row["lon"] for row in rows]
    values = [row["value"] for row in rows]
    colors = [row["color"] for row in rows]
    names = [row["station"] for row in rows]
    categories = [row["category"] for row in rows]
    if len(values) > 1 and max(values) != min(values):
        min_value = min(values)
        max_value = max(values)
        marker_sizes = [18 + ((value - min_value) / (max_value - min_value)) * 12 for value in values]
    else:
        marker_sizes = [22 for _ in values]
    hover_data = [
        {
            "station": name,
            "value": value,
            "category": category,
            "region": row["region"],
        }
        for name, value, category, row in zip(names, values, categories, rows)
    ]
    value_labels = [f"{value:.1f}" for value in values]

    figure.add_trace(
        go.Scattergeo(
            lon=lons,
            lat=lats,
            mode="markers+text",
            marker=dict(size=marker_sizes, color=colors, opacity=0.97),
            text=value_labels,
            textposition="top center",
            textfont=dict(size=14, color="#0f172a", family=BASE_FONT_FAMILY),
            customdata=hover_data,
            hovertemplate="<b>%{customdata.station}</b><br>Value: %{customdata.value:.2f}<br>Category: %{customdata.category}<br>Region: %{customdata.region}<extra></extra>",
        )
    )
    figure.update_layout(
        **_geo_map_layout(620),
    )
    return figure


def _best_station_for_hour(parsed, hour_index):
    stations = parsed.get("data", {}).get("stations", {}) if parsed else {}
    best_station = None
    best_value = float("-inf")
    try:
        target_index = int(hour_index or 0)
    except (TypeError, ValueError):
        target_index = 0
    if target_index < 0:
        target_index = 0
    for station_name, payload in stations.items():
        values = payload.get("forecastValue", []) if isinstance(payload, dict) else []
        if not values:
            continue
        idx = min(target_index, len(values) - 1)
        try:
            value = float(values[idx])
        except (TypeError, ValueError):
            continue
        if value > best_value:
            best_station = station_name
            best_value = value
    return best_station


def _forecast_time_series_figure(parsed, pollutant_label, station_name, hour_index):
    figure = go.Figure()
    if not parsed:
        figure.update_layout(template="plotly_white", height=300, margin=dict(l=20, r=20, t=40, b=20))
        return figure

    station_name = station_name or _best_station_for_hour(parsed, hour_index)
    if not station_name:
        figure.update_layout(template="plotly_white", height=300, margin=dict(l=20, r=20, t=40, b=20))
        return figure

    stations = parsed.get("data", {}).get("stations", {})
    payload = None
    target = normalize_station_name(station_name)
    for key, value in stations.items():
        if normalize_station_name(key) == target:
            payload = value
            station_name = key
            break
    if not payload:
        figure.update_layout(template="plotly_white", height=300, margin=dict(l=20, r=20, t=40, b=20))
        return figure

    hist_times_raw = parsed.get("data", {}).get("time", {}).get("histTime", [])
    forecast_times_raw = parsed.get("data", {}).get("time", {}).get("forecastTime", [])
    hist_values = payload.get("histValue", []) if isinstance(payload, dict) else []
    forecast_values = payload.get("forecastValue", []) if isinstance(payload, dict) else []

    def _parse_list(items):
        out = []
        for item in items or []:
            parsed_ts = _parse_forecast_timestamp(item)
            out.append(parsed_ts or item)
        return out

    hist_times = _parse_list(hist_times_raw[: len(hist_values)])
    forecast_times = _parse_list(forecast_times_raw[: len(forecast_values)])
    all_times = hist_times + forecast_times

    observed_colour = "#b45309"
    forecast_colour = "#2563eb"

    # Observed monitoring (hist portion in the CSV)
    if hist_times and hist_values:
        figure.add_trace(
            go.Scatter(
                x=hist_times,
                y=hist_values[: len(hist_times)],
                mode="lines+markers",
                line=dict(color=observed_colour, width=0.95),
                marker=dict(size=2.8, color=observed_colour, line=dict(width=0.5, color="#ffffff")),
                name="Observed (monitoring)",
                hovertemplate="<b>Observed</b><br>%{x|%d %b %H:%M}<br>%{y:.2f}<extra></extra>",
            )
        )

    # Forecast mean
    if forecast_times and forecast_values:
        if hist_times:
            figure.add_vrect(
                x0=forecast_times[0],
                x1=forecast_times[-1],
                fillcolor="rgba(37, 99, 235, 0.065)",
                line_width=0,
                layer="below",
            )
        figure.add_trace(
            go.Scatter(
                x=forecast_times,
                y=forecast_values[: len(forecast_times)],
                mode="lines+markers",
                line=dict(color=forecast_colour, width=1.05),
                marker=dict(size=3.0, color=forecast_colour, line=dict(width=0.5, color="#ffffff")),
                name="Forecast (mean)",
                hovertemplate="<b>Forecast</b><br>%{x|%d %b %H:%M}<br>%{y:.2f}<extra></extra>",
            )
        )

    # Approximate uncertainty bounds using station RMSE (10th/90th percentiles ~ +/- 1.2816 sigma)
    rmse_value = (parsed.get("stats", {}) or {}).get(station_name, {}).get("RMSE")
    try:
        rmse = float(rmse_value)
    except (TypeError, ValueError):
        rmse = None

    if rmse is not None and forecast_times and forecast_values:
        z = 1.28155
        lower = []
        upper = []
        for idx in range(len(forecast_values)):
            try:
                mean = float(forecast_values[idx])
            except (TypeError, ValueError):
                lower.append(None)
                upper.append(None)
                continue
            spread = z * rmse
            lower.append(max(mean - spread, 0.0))
            upper.append(mean + spread)

        figure.add_trace(
            go.Scatter(
                x=forecast_times,
                y=lower,
                mode="lines",
                line=dict(color="rgba(37, 99, 235, 0)", width=0),
                hoverinfo="skip",
                showlegend=False,
                name="Lower bound",
            )
        )
        figure.add_trace(
            go.Scatter(
                x=forecast_times,
                y=upper,
                mode="lines",
                line=dict(color="rgba(37, 99, 235, 0)", width=0),
                fill="tonexty",
                fillcolor="rgba(37, 99, 235, 0.16)",
                hoverinfo="skip",
                name="10th-90th range",
            )
        )

    selected_time = _selected_time_label(parsed, hour_index)
    marker_time = _parse_forecast_timestamp(selected_time) if selected_time else None
    if marker_time is not None:
        figure.add_vline(x=marker_time, line=dict(color="#64748b", width=1.5, dash="dot"))

    pollutant = POLLUTANTS.get(pollutant_label, {})
    station_title = title_case_station_name(station_name)
    time_axis = _forecast_time_tick_axis(all_times)
    figure.update_layout(
        template="plotly_white",
        height=270,
        margin=dict(l=62, r=22, t=32, b=42),
        title=dict(text=f"{station_title} forecast", x=0, xanchor="left", font=dict(size=15, color="#0f172a")),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#ffffff",
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="top",
            y=1.14,
            xanchor="right",
            x=1,
            font=dict(size=8, color="#334155"),
            itemsizing="trace",
            itemwidth=30,
        ),
        xaxis=dict(
            title=dict(text="Time (AEST)", font=dict(size=12, color="#334155")),
            showgrid=False,
            ticks="outside",
            tickfont=dict(family=BASE_FONT_FAMILY, color="#334155", size=11),
            **time_axis,
        ),
        yaxis=dict(
            title=dict(text=pollutant.get("Units", "Value"), font=dict(family=BASE_FONT_FAMILY, color="#0f172a", size=12)),
            gridcolor="#e2e8f0",
            zeroline=False,
            tickfont=dict(family=BASE_FONT_FAMILY, color="#334155", size=12),
        ),
    )
    return figure

def _overview_allstations_offsets_rows(selection, offsets=(1, 3, 6, 12)):
    """Return full station list across selected regions for PM2.5/PM10/O3 for specific hour offsets.

    `offsets` is an iterable of integers representing forecast step indexes (1-based).
    """
    selection_for_panel, regions = _overview_pick_multi_region_forecast_selection(selection or {})
    try:
        max_hours = int(selection_for_panel.get("timeScopes") or 12)
    except (TypeError, ValueError):
        max_hours = 12

    pollutants = ["PM2.5", "PM10", "O3"]
    pollutant_key = {"PM2.5": "pm25", "PM10": "pm10", "O3": "o3"}
    if not regions:
        fallback_regions = set()
        for kp in FILE_KEY_PARAMS:
            if kp.get("pollutants") not in pollutants:
                continue
            if kp.get("timeScopes") != str(selection_for_panel.get("timeScopes")):
                continue
            if kp.get("date") != str(selection_for_panel.get("date")):
                continue
            region_name = kp.get("regions")
            if region_name:
                fallback_regions.add(region_name)
        regions = sorted(fallback_regions)

    rows = []
    station_index = {}
    time_labels = []

    def _ensure_row(station_name, region_name):
        station_norm = normalize_station_name(station_name)
        if not station_norm:
            return None
        existing = station_index.get(station_norm)
        if existing is not None:
            return existing
        site = _site_lookup().get(station_norm) or {}
        region_label = str(site.get("Region") or region_name or "").replace("_", " ").strip()
        row = {
            "station": title_case_station_name(station_name),
            "region": region_label or "--",
        }
        for off in offsets:
            for _pollutant, key in pollutant_key.items():
                row[f"h{off}_{key}"] = "--"
                row[f"h{off}_{key}_cat"] = "no-data"
        station_index[station_norm] = row
        rows.append(row)
        return row

    def _coerce_float(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    for pollutant in pollutants:
        p_key = pollutant_key.get(pollutant)
        for region_name in regions or []:
            file_path = forecast_file_path(
                [
                    str(region_name),
                    str(pollutant),
                    str(selection_for_panel.get("timeScopes")),
                    str(selection_for_panel.get("models")),
                    str(selection_for_panel.get("date")),
                ]
            )
            if not file_path:
                for kp in FILE_KEY_PARAMS:
                    if kp.get("regions") != str(region_name):
                        continue
                    if kp.get("pollutants") != str(pollutant):
                        continue
                    if kp.get("timeScopes") != str(selection_for_panel.get("timeScopes")):
                        continue
                    if kp.get("date") != str(selection_for_panel.get("date")):
                        continue
                    file_path = forecast_file_path(
                        [
                            kp.get("regions"),
                            kp.get("pollutants"),
                            kp.get("timeScopes"),
                            kp.get("models"),
                            kp.get("date"),
                        ]
                    )
                    if file_path:
                        break
            if not file_path:
                continue
            try:
                mtime = os.path.getmtime(file_path)
            except OSError:
                mtime = 0
            parsed = _cached_parse_csv(file_path, pollutant, max_hours, mtime)
            if not parsed:
                continue

            if not time_labels:
                times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime") or []
                if times:
                    time_labels = list(times)

            stations = ((parsed.get("data") or {}).get("stations") or {})
            for station_name, payload in stations.items():
                row = _ensure_row(station_name, region_name)
                if not row:
                    continue
                series = (payload or {}).get("forecastValue") or []
                for off in offsets:
                    hour_idx = off - 1
                    value_num = None
                    if 0 <= hour_idx < len(series):
                        value_num = _coerce_float(series[hour_idx])
                    col = f"h{off}_{p_key}"
                    cat_col = f"h{off}_{p_key}_cat"
                    if value_num is None:
                        continue
                    current_num = _coerce_float(row.get(col))
                    if current_num is None or value_num >= current_num:
                        row[col] = f"{value_num:.1f}"
                        category_key, _color = category_for_value(pollutant, value_num)
                        row[cat_col] = str(category_key or "no-data")

    pretty_labels = []
    for off in offsets:
        label = f"+{off}h"
        # try to compute exact time if available
        if time_labels:
            if len(time_labels) >= off:
                dt = _parse_forecast_timestamp(time_labels[off - 1])
                if dt:
                    pretty = dt.strftime("%H:%M")
                    label = f"{pretty}"
        pretty_labels.append(label)

    columns = [
        {"name": "Station", "id": "station"},
        {"name": "Region", "id": "region"},
    ]
    for off, lbl in zip(offsets, pretty_labels):
        columns.extend(
            [
                {"name": [lbl, "PM2.5"], "id": f"h{off}_pm25"},
                {"name": [lbl, "PM10"], "id": f"h{off}_pm10"},
                {"name": [lbl, "O3"], "id": f"h{off}_o3"},
            ]
        )

    def _row_score(item):
        score = float("-inf")
        for col in [f"h{offsets[0]}_pm25", f"h{offsets[0]}_pm10", f"h{offsets[0]}_o3"]:
            value = _coerce_float(item.get(col))
            if value is not None:
                score = max(score, value)
        return score

    rows.sort(key=lambda item: (-_row_score(item), str(item.get("station") or "")))
    return columns, rows


def _forecast_all_hour_table(parsed, pollutant_label, selection=None):
    """Region-grouped multi-pollutant HTML table for the All Stations modal.

    Mirrors the NSW Government AQ page layout:
      - Region header row (name + worst-AQ-category badge)
      - Station data rows beneath each region
      - Columns: Station | [time-slot × pollutant …]
    """
    from collections import defaultdict

    selection = dict(selection or DEFAULT_SELECTION)
    pollutants_list = ["PM2.5", "PM10", "O3"]
    p_units   = {"PM2.5": "µg/m³",  "PM10": "µg/m³",  "O3": "pphm"}
    p_label   = {"PM2.5": "Particles (PM2.5)", "PM10": "Particles (PM10)", "O3": "Ozone"}

    # ---- load all-region merged data for every pollutant ----
    parsed_by_p = {}
    all_times   = []
    for p in pollutants_list:
        sel    = dict(selection, pollutants=p)
        merged = _merged_forecast_map_payload(sel, p)
        parsed_by_p[p] = merged
        p_times = ((merged.get("data") or {}).get("time") or {}).get("forecastTime") or []
        if len(p_times) > len(all_times):
            all_times = p_times

    if not all_times and parsed:
        all_times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime") or []
        if pollutant_label:
            parsed_by_p[pollutant_label] = parsed

    if not all_times:
        return (
            html.Div("No forecast data available.", className="all-fc-no-data"),
            "All Stations Nowcasting",
            "No forecast data available.",
        )

    # ---- choose up to MAX_SLOTS evenly-spaced time points ----
    MAX_SLOTS = 4
    n = len(all_times)
    if n <= MAX_SLOTS:
        slot_indices = list(range(n))
    else:
        slot_indices = [round(i * (n - 1) / (MAX_SLOTS - 1)) for i in range(MAX_SLOTS)]
        seen = set()
        slot_indices = [x for x in slot_indices if not (x in seen or seen.add(x))]

    time_labels = []
    for tidx in slot_indices:
        parsed_ts = _parse_forecast_timestamp(all_times[tidx])
        if parsed_ts:
            hr = parsed_ts.strftime("%I").lstrip("0") or "0"
            time_labels.append(f"{hr}{parsed_ts.strftime('%p').upper()}")
        else:
            time_labels.append(f"+{tidx + 1}h")

    # ---- AQ colour maps ----
    cat_bg = {
        "good":           "#16a34a",
        "fair":           "#eab308",
        "poor":           "#f97316",
        "very-poor":      "#dc2626",
        "extremely-poor": "#7e0023",
        "no-data":        "#e5e7eb",
    }
    cat_fg = {
        "good":           "#ffffff",
        "fair":           "#111827",
        "poor":           "#111827",
        "very-poor":      "#ffffff",
        "extremely-poor": "#ffffff",
        "no-data":        "#9ca3af",
    }
    cat_order = list(cat_bg.keys())          # used to rank severity

    # ---- collect all stations ----
    station_info = {}
    for p in pollutants_list:
        merged = parsed_by_p.get(p) or {}
        for sname, payload in ((merged.get("data") or {}).get("stations") or {}).items():
            snorm = normalize_station_name(sname)
            if snorm not in station_info:
                site   = _site_lookup().get(snorm) or {}
                region = str(
                    site.get("Region") or (payload or {}).get("region") or "Unknown"
                ).replace("_", " ")
                station_info[snorm] = {
                    "station": title_case_station_name(sname),
                    "region":  region,
                    "data":    {},
                }
            station_info[snorm]["data"][p] = (payload or {}).get("forecastValue") or []

    # group by region
    regions_dict = defaultdict(list)
    for snorm, info in sorted(station_info.items(), key=lambda x: x[1]["station"]):
        regions_dict[info["region"]].append(info)

    # ---- helpers ----
    def _val_cat(info, p, tidx):
        vals = info["data"].get(p) or []
        try:
            v = float(vals[tidx]) if tidx < len(vals) else None
        except (TypeError, ValueError):
            v = None
        if v is None:
            return None, "no-data"
        ck, _ = category_for_value(p, v)
        return v, str(ck or "no-data")

    def _region_cat(stations):
        worst = 0
        for info in stations:
            for p in pollutants_list:
                _, ck = _val_cat(info, p, slot_indices[0] if slot_indices else 0)
                idx = cat_order.index(ck) if ck in cat_order else 0
                if ck != "no-data" and idx > worst:
                    worst = idx
        return cat_order[worst]

    # ---- build <thead> ----
    # row-1: empty station-col (rowspan 2) + time labels (colspan = n_pollutants)
    tr1_cells = [html.Th("", rowSpan=2, className="all-fc-th all-fc-station-col")]
    for tl in time_labels:
        tr1_cells.append(
            html.Th(tl, colSpan=len(pollutants_list), className="all-fc-th all-fc-time-header")
        )
    # row-2: pollutant sub-headers repeated for each slot
    tr2_cells = []
    for _ in time_labels:
        for p in pollutants_list:
            tr2_cells.append(html.Th(
                [
                    html.Span(p_label[p],          className="all-fc-p-name"),
                    html.Span(p_units[p],           className="all-fc-p-unit"),
                    html.Span("Hourly average",     className="all-fc-p-avg"),
                ],
                className="all-fc-th all-fc-pollutant-header",
            ))

    thead = html.Thead([html.Tr(tr1_cells), html.Tr(tr2_cells)])

    # ---- build <tbody> ----
    total_cols = 1 + len(time_labels) * len(pollutants_list)
    tbody_rows = []

    for region_name in sorted(regions_dict.keys()):
        stations = regions_dict[region_name]
        reg_cat  = _region_cat(stations)
        reg_bg   = cat_bg.get(reg_cat, "#e5e7eb")
        reg_fg   = cat_fg.get(reg_cat, "#9ca3af")

        # region header row — name only, no badge
        tbody_rows.append(html.Tr(
            html.Td(
                html.Span(region_name, className="all-fc-region-name"),
                colSpan=total_cols,
                className="all-fc-region-header-cell",
            ),
            className="all-fc-region-row",
        ))

        # station rows — coloured dot, plain cell background
        for info in stations:
            cells = [html.Td(info["station"], className="all-fc-station-name")]
            for tidx in slot_indices:
                for p in pollutants_list:
                    v, ck = _val_cat(info, p, tidx)
                    dot_colour = cat_bg.get(ck, "#e5e7eb")
                    val_str    = f"{v:.1f}" if v is not None else "--"
                    cells.append(html.Td(
                        [
                            html.Span("●", className="all-fc-dot",
                                      style={"color": dot_colour}),
                            val_str,
                        ],
                        className="all-fc-data-cell",
                    ))
            tbody_rows.append(html.Tr(cells, className="all-fc-station-row"))

    tbody = html.Tbody(tbody_rows)

    container = html.Div(
        html.Table([thead, tbody], className="all-fc-table"),
        className="all-fc-scroll-container",
    )

    n_stations = sum(len(v) for v in regions_dict.values())
    title    = "All Stations Nowcasting"
    subtitle = f"{n_stations} stations · PM2.5, PM10, O3"
    return container, title, subtitle


def _leaflet_category_colour(category):
    colours = {
        "Good": "#42a93c",
        "Fair": "#eec900",
        "Poor": "#e47400",
        "Very Poor": "#ba0029",
        "Extremely Poor": "#590019",
        "No Data": "#242424",
        "No data": "#242424",
    }
    return colours.get(category, "#242424")


def _leaflet_forecast_map_html(parsed, pollutant_label, hour_index, extra_by_station=None, include_series=True):
    current_time_raw = _selected_time_label(parsed, hour_index or 0) if parsed else "--"
    current_dt = _parse_forecast_timestamp(current_time_raw)
    if current_dt:
        popup_time = current_dt.strftime("%I:%M %p").lstrip("0") or "0"
        popup_date = f"{current_dt.day} {current_dt.strftime('%b %Y')}"
    else:
        popup_time = str(current_time_raw or "--")
        popup_date = ""

    forecast_times = parsed.get("data", {}).get("time", {}).get("forecastTime", []) if parsed else []

    extra_by_station = extra_by_station or {}
    markers = []
    stations = parsed.get("data", {}).get("stations", {}) if parsed else {}
    for station_name, payload in stations.items():
        site = _site_lookup().get(normalize_station_name(station_name))
        if not site:
            continue
        forecast_values = payload.get("forecastValue", []) if isinstance(payload, dict) else []
        series = []
        if include_series:
            for value in forecast_values:
                category, colour = category_for_value(pollutant_label, value)
                category = category.replace("-", " ").title()
                series.append(
                    {
                        "value": None if value is None else round(float(value), 2),
                        "valueLabel": "--" if value is None else f"{float(value):.1f}",
                        "category": category,
                        "colour": _leaflet_category_colour(category),
                    }
                )
        selected_index = hour_index or 0
        if selected_index < 0:
            selected_index = 0
        if selected_index >= len(series):
            selected_index = len(series) - 1
        if include_series:
            selected_point = (
                series[selected_index]
                if series
                else {"value": None, "valueLabel": "--", "category": "No data", "colour": _leaflet_category_colour("No data")}
            )
        else:
            value = None
            if forecast_values:
                idx = max(0, min(int(hour_index or 0), len(forecast_values) - 1))
                try:
                    value = float(forecast_values[idx])
                except (TypeError, ValueError):
                    value = None
            category, _colour = category_for_value(pollutant_label, value)
            category = str(category or "no-data").replace("-", " ").title()
            selected_point = {
                "value": None if value is None else round(float(value), 2),
                "valueLabel": "--" if value is None else f"{float(value):.1f}",
                "category": category,
                "colour": _leaflet_category_colour(category),
            }
            # Keep a 1-point series so the shared JS can still render without special-casing.
            series = [selected_point]
        markers.append(
            {
                "station": title_case_station_name(station_name),
                "stationKey": station_name,
                "source": "Forecast",
                "value": selected_point["value"],
                "valueLabel": selected_point["valueLabel"],
                "category": selected_point["category"],
                "colour": selected_point["colour"],
                "series": series,
                "bars": extra_by_station.get(station_name) or None,
                "lat": site.get("Latitude"),
                "lon": site.get("Longitude"),
                "region": site.get("Region") or "",
                "hour": popup_time,
                "date": popup_date,
                "pollutant": pollutant_display(pollutant_label) if pollutant_label else "--",
            }
        )

    marker_json = json.dumps(markers)
    times_json = json.dumps(forecast_times)
    initial_hour_index_json = json.dumps(max(0, int(hour_index or 0)))
    if not markers:
        body_message = "No forecast stations could be mapped for this selection."
    else:
        body_message = ""

    # Reuse the monitoring-style leaflet map HTML so forecast maps match monitoring maps
    return f"""<!doctype html>
<html>
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <base href="/" />
    <link rel="stylesheet" href="/assets/vendor/leaflet/leaflet.css" />
    <link rel="stylesheet" href="/assets/vendor/leaflet.markercluster/MarkerCluster.css" />
    <link rel="stylesheet" href="/assets/vendor/leaflet.markercluster/MarkerCluster.Default.css" />
    <style>
        html, body, #map {{ height: 100%; width: 100%; margin: 0; padding: 0; }}
        body {{ font-family: {BASE_FONT_FAMILY}; }}
	        .station-marker {{
	            width: 22px;
	            height: 22px;
			            border: 2px solid #cbd5e1;
	            border-radius: 50%;
	            box-sizing: border-box;
	            display: flex;
            align-items: center;
            justify-content: center;
	            color: #ffffff;
	            font-weight: 800;
	            font-size: 7px;
	            line-height: 1;
	            text-shadow: 0 1px 2px rgba(0, 0, 0, 0.55);
	            box-shadow: 0 2px 5px rgba(15, 23, 42, 0.18);
	        }}
	        .purpleair-marker {{
	            width: 22px;
	            height: 22px;
		            border: 2px solid #cbd5e1;
	            border-radius: 4px;
	            box-sizing: border-box;
	            color: #fff;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 9px;
            font-weight: 900;
            line-height: 1;
	            box-shadow: 0 2px 5px rgba(15, 23, 42, 0.18);
	        }}
        .leaflet-tooltip.station-label {{
            font-size: 12px;
            font-weight: 700;
            color: #0f172a;
            border: 1px solid #cbd5e1;
            box-shadow: none;
        }}
        .legend {{
            background: rgba(255, 255, 255, 0.94);
            padding: 10px 12px;
            border: 1px solid #cbd5e1;
            border-radius: 4px;
            color: #0f172a;
            line-height: 1.4;
            box-shadow: 0 6px 18px rgba(15, 23, 42, 0.14);
        }}
        .legend-row {{ display: flex; align-items: center; gap: 8px; white-space: nowrap; }}
        .legend-swatch {{ width: 13px; height: 13px; border: 2px solid rgba(0,0,0,0.4); display: inline-block; }}
    </style>
</head>
<body>
    <div id="map"></div>
    <script src="/assets/vendor/leaflet/leaflet.js"></script>
    <script src="/assets/vendor/leaflet.markercluster/leaflet.markercluster.js"></script>
    <script>
        const markers = {marker_json};
        const forecastTimes = {times_json};
        const initialHourIndex = {initial_hour_index_json};
        const purpleAirFields = [
            "name",
            "location_type",
            "latitude",
            "longitude",
            "rssi",
            "pm1.0",
            "pm2.5_alt",
            "pm10.0",
            "temperature",
            "humidity",
            "last_seen"
        ];
        const purpleAirApiKey = "D80F3AFD-DDAD-11ED-BD21-42010A800008";
        const disableTiles = {str(DISABLE_LEAFLET_TILES).lower()};
        const map = L.map("map", {{
            minZoom: 5,
            maxZoom: 19,
            maxBounds: [[-37.50528021, 130.9992792], [-28.15701999, 163.638889]],
            maxBoundsViscosity: 0.9
        }}).setView([-33.0, 147.032179], 6);

        const nswBounds = [[{NSW_MAP_BOUNDS["south"]}, {NSW_MAP_BOUNDS["west"]}], [{NSW_MAP_BOUNDS["north"]}, {NSW_MAP_BOUNDS["east"]}]];

            function addTileLayerCandidates(urls) {{
                let idx = 0;
                let layer = null;
                function attach(url) {{
                    layer = L.tileLayer(url, {{
                        maxZoom: 19,
                        subdomains: ["a", "b", "c"],
                        attribution: `{LEAFLET_TILE_ATTRIBUTION}`
                    }});
                layer.on("tileerror", function() {{
                    try {{ map.removeLayer(layer); }} catch (e) {{}}
                    idx += 1;
                    if (idx < urls.length) {{
                        attach(urls[idx]);
                        layer.addTo(map);
                        return;
                    }}
                    try {{ map.getContainer().style.background = "#eef4e8"; }} catch (e) {{}}
                    if (!document.getElementById("tile-warning")) {{
                        const warn = document.createElement("div");
                        warn.id = "tile-warning";
                        warn.textContent = "Basemap tiles unavailable (showing markers only).";
                        warn.style.cssText = "position:absolute; top:10px; left:50%; transform:translateX(-50%); z-index:9999; background:rgba(255,255,255,0.94); border:1px solid rgba(148,163,184,0.55); padding:6px 10px; border-radius:999px; font-weight:800; font-size:12px; color:#0f172a; box-shadow:0 8px 20px rgba(15,23,42,0.14);";
                        document.body.appendChild(warn);
                    }}
                }});
            }}
            if (urls && urls.length) {{
                attach(urls[0]);
                return layer;
            }}
            return null;
        }}

        if (!disableTiles) {{
            const tileUrls = {_leaflet_tile_urls_js()};
            const tileLayer = addTileLayerCandidates(tileUrls);
            if (tileLayer) tileLayer.addTo(map);
        }} else {{
            try {{ map.getContainer().style.background = "#eef4e8"; }} catch (e) {{}}
        }}
        L.control.scale({{ imperial: false }}).addTo(map);

        // Offline-friendly outline: draw Australian state boundaries so NSW context is visible
        // even when basemap tiles are blocked.
        function addStateOutlines() {{
            fetch("/assets/australian-states.json")
                .then((r) => r.json())
                .then((geo) => {{
                    try {{
                        L.geoJSON(geo, {{
                            style: (feature) => {{
                                const name = (feature && feature.properties && feature.properties.STATE_NAME) ? String(feature.properties.STATE_NAME) : "";
                                const isNsw = name.toLowerCase() === "new south wales";
                                return {{
                                    color: isNsw ? "rgba(30,64,175,0.9)" : "rgba(15,23,42,0.35)",
                                    weight: isNsw ? 2.4 : 1.2,
                                    fillColor: isNsw ? "rgba(59,130,246,0.12)" : "rgba(148,163,184,0.08)",
                                    fillOpacity: 0.6,
                                }};
                            }}
                        }}).addTo(map);
                    }} catch (e) {{}}
                }})
                .catch(() => {{}});
        }}
        addStateOutlines();

        const group = L.featureGroup().addTo(map);
        const aqmsGroup = L.featureGroup().addTo(group);
        const sensorGroup = L.featureGroup().addTo(group);
        const purpleAirMarkers = new Map();
        const forecastMarkers = [];

        function purpleAirCategory(value) {{
	            if (value == null || Number.isNaN(Number(value))) {{
	                return {{ label: "No data", colour: "#9ca3af" }};
	            }}
            const pm25 = Number(value);
            if (pm25 < 25) return {{ label: "Good", colour: "#16a34a" }};
            if (pm25 < 50) return {{ label: "Fair", colour: "#facc15" }};
            if (pm25 < 100) return {{ label: "Poor", colour: "#f97316" }};
            if (pm25 < 300) return {{ label: "Very poor", colour: "#ef4444" }};
            return {{ label: "Extremely poor", colour: "#7f1d1d" }};
        }}

	        function purpleAirSize(value) {{
	            return 22;
	        }}

        function purpleAirIcon(item) {{
            const size = purpleAirSize(item.pm25);
            const category = purpleAirCategory(item.pm25);
            return L.divIcon({{
		                className: "",
		                html: `<div class="purpleair-marker" style="width:${{size}}px;height:${{size}}px;background:${{category.colour}};font-size:${{size > 30 ? 10 : 9}}px"></div>`,
                iconSize: [size, size],
                iconAnchor: [size / 2, size / 2],
                popupAnchor: [0, -(size / 2)]
            }});
        }}

        function purpleAirPopupHtml(item, loadingText) {{
            return `
	        <strong>${{item.station}}</strong><br>
	        Source: <strong>PurpleAir</strong><br>
	        Sensor ID: ${{item.siteId}}<br>
	        PM2.5: <strong>${{(item.sensorValue == null && item.pm25 == null) ? "--" : formatPaValue(item.sensorValue == null ? item.pm25 : item.sensorValue)}}</strong><br>
	        Category: <strong>${{item.paCategory || item.category || "No data"}}</strong><br>
                Lat/Lon: ${{item.lat.toFixed(5)}}, ${{item.lon.toFixed(5)}}<br>
                <div id="purpleair-${{item.siteId}}" style="margin-top:8px;">${{loadingText || ""}}</div>
            `;
        }}

        function safeIndex(value, length) {{
            const idx = Number(value);
            if (!Number.isFinite(idx) || length <= 0) return 0;
            return Math.max(0, Math.min(Math.round(idx), length - 1));
        }}

        function forecastTimestamp(index) {{
            if (!Array.isArray(forecastTimes) || forecastTimes.length === 0) return null;
            return forecastTimes[safeIndex(index, forecastTimes.length)];
        }}

        function formatForecastTimestamp(ts) {{
            if (!ts) return {{ time: "--", date: "--" }};
            const parsed = new Date(String(ts).replace(" ", "T"));
            if (Number.isNaN(parsed.getTime())) {{
                return {{ time: String(ts), date: "" }};
            }}
            const time = parsed.toLocaleTimeString("en-AU", {{ hour: "numeric", minute: "2-digit" }});
            const date = parsed.toLocaleDateString("en-AU", {{ day: "2-digit", month: "short", year: "numeric" }});
            return {{ time, date }};
        }}

        function forecastIcon(item) {{
            return L.divIcon({{
	                className: "",
		                html: `<div class="station-marker" style="background:${{item.colour}}"></div>`,
		                iconSize: [22, 22],
		                iconAnchor: [11, 11],
		                popupAnchor: [0, -12]
	            }});
        }}

        function barsHtml(item) {{
            const bars = Array.isArray(item.bars) ? item.bars : null;
            if (!bars || !bars.length) return "";
            const maxVal = Math.max.apply(null, bars.map((b) => (b && b.value != null && !Number.isNaN(Number(b.value))) ? Number(b.value) : 0));
            const denom = maxVal > 0 ? maxVal : 1;
            const rows = bars.map((b) => {{
                const label = b && b.label ? b.label : "--";
                const valueLabel = b && b.valueLabel ? b.valueLabel : "--";
                const colour = b && b.colour ? b.colour : "#94a3b8";
                const category = b && b.category ? b.category : "No data";
                const raw = (b && b.value != null && !Number.isNaN(Number(b.value))) ? Number(b.value) : 0;
                const pct = Math.max(0, Math.min(100, (raw / denom) * 100));
                return `
                    <div style="display:grid; grid-template-columns:64px 1fr 56px; gap:8px; align-items:center; margin-top:6px;">
                        <div style="font-weight:900;">${{label}}</div>
                        <div style="height:12px; border-radius:999px; background: rgba(148,163,184,0.22); overflow:hidden;">
                            <div style="height:12px; width:${{pct}}%; background:${{colour}}; opacity:0.75;"></div>
                        </div>
                        <div style="text-align:right; font-weight:900;">${{valueLabel}}</div>
                    </div>
                    <div style="margin-top:2px; font-size:12px; color: rgba(15,23,42,0.75); font-weight:800;">${{category}}</div>
                `;
            }}).join("");
            return `<div style="margin-top:10px; padding-top:8px; border-top:1px solid rgba(148,163,184,0.35);">${{rows}}</div>`;
        }}

        function forecastPopupHtml(item) {{
            return `
                <strong>${{item.station}}</strong><br>
                Source: <strong>${{item.source || "Forecast"}}</strong><br>
                Value: <strong>${{item.valueLabel}}</strong><br>
                Category: <strong>${{item.category}}</strong><br>
                Time: ${{item.hour || "--"}}<br>
                Date: ${{item.date || "--"}}<br>
                Pollutant: ${{item.pollutant || "--"}}<br>
                Region: ${{item.region || "--"}}
                ${{barsHtml(item)}}
            `;
        }}

        function applyForecastHourIndex(hourIndex) {{
            const ts = forecastTimestamp(hourIndex);
            const label = formatForecastTimestamp(ts);
            forecastMarkers.forEach((entry) => {{
                const item = entry.item;
                const marker = entry.marker;
                const series = Array.isArray(item.series) ? item.series : [];
                const idx = safeIndex(hourIndex, series.length);
                const point = series[idx] || series[series.length - 1] || null;
                if (point) {{
                    item.value = point.value;
                    item.valueLabel = point.valueLabel;
                    item.category = point.category;
                    item.colour = point.colour;
                }}
                item.hour = label.time;
                item.date = label.date;
                marker.setIcon(forecastIcon(item));
                marker.setPopupContent(forecastPopupHtml(item));
            }});
        }}

        window.addEventListener("message", function(event) {{
            const data = event.data || {{}};
            if (data.type !== "forecast-hour-update") return;
            applyForecastHourIndex(data.hourIndex);
        }});

        markers.forEach((item) => {{
            if (item.lat == null || item.lon == null) return;
            const marker = L.marker([item.lat, item.lon], {{
                icon: item.source === "PurpleAir" ? purpleAirIcon(item) : forecastIcon(item),
                zIndexOffset: item.source === "PurpleAir" ? 100 : 500
            }});
            if (item.source === "PurpleAir") {{
                marker.addTo(sensorGroup);
                purpleAirMarkers.set(String(item.siteId), {{ item, marker }});
            }} else {{
                marker.addTo(aqmsGroup);
                forecastMarkers.push({{ item, marker }});
            }}
            marker.bindTooltip(item.station, {{ direction: "top", className: "station-label" }});
            const purpleAirPanelId = `purpleair-${{item.siteId}}`;
            marker.bindPopup(
                item.source === "PurpleAir"
                    ? purpleAirPopupHtml(item, "Click opened. Loading PurpleAir values...")
                    : forecastPopupHtml(item),
                {{ maxWidth: 460, maxHeight: 360, closeButton: true }}
            );
            if (item.source === "PurpleAir") {{
                marker.on("popupopen", () => fetchPurpleAirValues(item, purpleAirPanelId));
            }} else {{
                marker.on("click", () => {{
                    if (!item.stationKey) return;
                    try {{
                        window.parent.postMessage({{ type: "forecast-station-select", station: item.stationKey }}, "*");
                    }} catch (err) {{}}
                }});
            }}
        }});

        function fitToVisibleMarkers() {{
            try {{
                const layers = group.getLayers ? group.getLayers() : [];
                if (!layers || layers.length === 0) {{
                    map.fitBounds(nswBounds, {{ padding: [18, 18] }});
                    return;
                }}
                const bounds = group.getBounds();
                if (!bounds || !bounds.isValid || !bounds.isValid()) {{
                    map.fitBounds(nswBounds, {{ padding: [18, 18] }});
                    return;
                }}
                if (layers.length === 1) {{
                    const latLng = layers[0].getLatLng ? layers[0].getLatLng() : bounds.getCenter();
                    map.setView(latLng, 10);
                    return;
                }}
                map.fitBounds(bounds, {{ padding: [34, 34], maxZoom: 11 }});
            }} catch (e) {{
                try {{ map.fitBounds(nswBounds, {{ padding: [18, 18] }}); }} catch (err) {{}}
            }}
        }}

        // Ensure popups can scroll when content grows (bars section).
        try {{
            if (L && L.DomUtil) {{
                const styleTag = document.createElement("style");
                styleTag.textContent = ".leaflet-popup-content.leaflet-popup-scrolled {{ overflow-y: auto; }}";
                document.head.appendChild(styleTag);
            }}
        }} catch (err) {{}}

        applyForecastHourIndex(initialHourIndex);

        function formatPaValue(value) {{
            if (value == null || Number.isNaN(Number(value))) return "--";
            return Math.max(Number(value), 0.2).toFixed(1);
        }}

        function fetchPurpleAirValues(item, panelId) {{
            const panel = document.getElementById(panelId);
            if (!panel || !item.siteId) return;
            const fields = purpleAirFields.join("%2C%20");
            const url = `https://api.purpleair.com/v1/sensors/${{item.siteId}}?sensor_index=${{item.siteId}}&fields=${{fields}}`;
            fetch(url, {{
                method: "GET",
                headers: {{ "X-API-Key": purpleAirApiKey }}
            }})
                .then((response) => response.json())
                .then((payload) => {{
                    const sensor = payload.sensor || {{}};
                    updatePurpleAirMarker(item, sensor);
                    const seen = sensor.last_seen ? new Date(sensor.last_seen * 1000).toLocaleString("en-AU") : "--";
                    const tempC = sensor.temperature == null ? "--" : (((Number(sensor.temperature) - 32) * 5) / 9).toFixed(1);
                    panel.innerHTML = `
                        <div><strong>PM1.0:</strong> ${{formatPaValue(sensor["pm1.0"])}}</div>
                        <div><strong>PM2.5:</strong> ${{formatPaValue(sensor["pm2.5_alt"])}}</div>
                        <div><strong>PM10:</strong> ${{formatPaValue(sensor["pm10.0"])}}</div>
                        <div><strong>Temperature:</strong> ${{tempC}} &deg;C</div>
                        <div><strong>Humidity:</strong> ${{sensor.humidity == null ? "--" : sensor.humidity + "%"}}</div>
                        <div><strong>Retrieved:</strong> ${{seen}}</div>
                    `;
                }})
                .catch((error) => {{
                    panel.innerHTML = "PurpleAir values could not be loaded.";
                }});
        }}

        function updatePurpleAirMarker(item, sensor) {{
	      const pm25 = sensor["pm2.5_alt"];
	      if (pm25 != null && !Number.isNaN(Number(pm25))) {{
	        item.pm25 = Math.max(Number(pm25), 0.2);
	        item.sensorValue = item.pm25;
	        item.valueLabel = formatPaValue(item.pm25);
	      }}
	      const category = purpleAirCategory(item.sensorValue == null ? item.pm25 : item.sensorValue);
	      item.paCategory = category.label;
	      item.category = category.label;
	      item.colour = category.colour;
            const entry = purpleAirMarkers.get(String(item.siteId));
            if (entry) {{
                entry.marker.setIcon(purpleAirIcon(item));
                entry.marker.setPopupContent(purpleAirPopupHtml(item, ""));
            }}
        }}

        function fetchPurpleAirSnapshot() {{
            if (purpleAirMarkers.size === 0) return;
            const fields = purpleAirFields.join("%2C%20");
            const url = `https://api.purpleair.com/v1/sensors?fields=${{fields}}&nwlng=130.9992792&nwlat=-28.15701999&selng=163.638889&selat=-37.50528021`;
            fetch(url, {{
                method: "GET",
                headers: {{ "X-API-Key": purpleAirApiKey }}
            }})
                .then((response) => response.json())
                .then((payload) => {{
                    const fields = payload.fields || [];
                    const idIndex = fields.indexOf("sensor_index");
                    const pm25Index = fields.indexOf("pm2.5_alt");
                    const lastSeenIndex = fields.indexOf("last_seen");
                    if (idIndex < 0 || pm25Index < 0 || !Array.isArray(payload.data)) return;
                    payload.data.forEach((row) => {{
                        const entry = purpleAirMarkers.get(String(row[idIndex]));
                        if (!entry) return;
                        updatePurpleAirMarker(entry.item, {{
                            "pm2.5_alt": row[pm25Index],
                            "last_seen": lastSeenIndex >= 0 ? row[lastSeenIndex] : null
                        }});
                    }});
                }})
                .catch((error) => {{}});
        }}

        fetchPurpleAirSnapshot();

        fitToVisibleMarkers();

        const legend = L.control({{ position: "bottomright" }});
        legend.onAdd = function() {{
            const div = L.DomUtil.create("div", "legend");
            div.innerHTML = `
                <div class="legend-row"><span class="legend-swatch" style="background:#42a93c"></span>Good</div>
                <div class="legend-row"><span class="legend-swatch" style="background:#eec900"></span>Fair</div>
                <div class="legend-row"><span class="legend-swatch" style="background:#e47400"></span>Poor</div>
                <div class="legend-row"><span class="legend-swatch" style="background:#ba0029"></span>Very poor</div>
                <div class="legend-row"><span class="legend-swatch" style="background:#590019"></span>Extremely poor</div>
	                <div class="legend-row"><span class="legend-swatch" style="background:#242424"></span>No data</div>
            `;
            return div;
        }};
        legend.addTo(map);
    </script>
</body>
</html>"""


def _leaflet_monitoring_map_html(rows):
    rows = rows or []
    markers = []
    if rows:
        for row in rows:
            lat = row.get("lat")
            lon = row.get("lon")
            if lat is None or lon is None:
                continue
            if not _point_in_nsw(lat, lon):
                continue
            category = row.get("category") or "No data"
            markers.append(
                {
                    "siteId": row.get("site_id"),
                    "station": row.get("station", ""),
                    "source": row.get("source", "AQMS"),
                    "category": category,
                    "colour": row.get("category_color") or _leaflet_category_colour(category),
                    "lat": lat,
                    "lon": lon,
                    "region": row.get("region") or "",
                    "hour": row.get("hour_description") or "",
                    "date": row.get("date") or "",
                    "pollutant": row.get("determining_pollutant") or "",
                    "valueLabel": "--" if row.get("value") is None else f"{float(row.get('value')):.1f}",
                    "sensorValue": row.get("value"),
                }
            )

    marker_json = json.dumps(markers)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <base href="/" />
  <link rel="stylesheet" href="/assets/vendor/leaflet/leaflet.css" />
  <link rel="stylesheet" href="/assets/vendor/leaflet.markercluster/MarkerCluster.css" />
  <link rel="stylesheet" href="/assets/vendor/leaflet.markercluster/MarkerCluster.Default.css" />
  <style>
    html, body, #map {{ height: 100%; width: 100%; margin: 0; padding: 0; }}
    body {{ font-family: {BASE_FONT_FAMILY}; }}
	    .station-marker {{
	      width: 22px;
		      height: 22px;
			      border: 2px solid #cbd5e1;
	      border-radius: 50%;
	      box-sizing: border-box;
      display: flex;
      align-items: center;
      justify-content: center;
	      color: #ffffff;
	      font-weight: 800;
	      font-size: 7px;
	      line-height: 1;
	      text-shadow: 0 1px 2px rgba(0, 0, 0, 0.55);
	      box-shadow: 0 2px 5px rgba(15, 23, 42, 0.18);
	    }}
		    .purpleair-marker {{
		      width: 22px;
		      height: 22px;
		      border: 2px solid #cbd5e1;
		      border-radius: 4px;
		      box-sizing: border-box;
		      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 9px;
      font-weight: 900;
      line-height: 1;
		      box-shadow: 0 2px 5px rgba(15, 23, 42, 0.18);
		    }}
		    .dustwatch-marker {{
		      position: relative;
		      width: 22px;
			      height: 22px;
		      box-sizing: border-box;
		      display: flex;
		      align-items: center;
		      justify-content: center;
		      color: #ffffff;
		      font-weight: 900;
		      font-size: 7px;
		      line-height: 1;
		      text-shadow: 0 1px 2px rgba(0, 0, 0, 0.65);
		      filter: drop-shadow(0 2px 5px rgba(15, 23, 42, 0.18));
		    }}
			    .dustwatch-marker svg {{
			      position: absolute;
			      inset: 0;
			    }}
	    .leaflet-tooltip.station-label {{
      font-size: 12px;
      font-weight: 700;
      color: #0f172a;
      border: 1px solid #cbd5e1;
      box-shadow: none;
    }}
    .legend {{
      background: rgba(255, 255, 255, 0.94);
      padding: 10px 12px;
      border: 1px solid #cbd5e1;
      border-radius: 4px;
      color: #0f172a;
      line-height: 1.4;
      box-shadow: 0 6px 18px rgba(15, 23, 42, 0.14);
    }}
	    .legend-row {{ display: flex; align-items: center; gap: 8px; white-space: nowrap; }}
	    .legend-swatch {{ width: 13px; height: 13px; border: 2px solid rgba(0,0,0,0.4); display: inline-block; }}
		    .legend-source-circle {{ width: 13px; height: 13px; border-radius: 50%; border: 2px solid #cbd5e1; display: inline-block; background:#f8fafc; }}
		    .legend-source-square {{ width: 13px; height: 13px; border-radius: 3px; border: 2px solid #cbd5e1; display: inline-block; background:#f8fafc; }}
		    .legend-source-triangle {{ width: 16px; height: 15px; display: inline-block; background: no-repeat center / contain url("data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2016%2015'%3E%3Cpolygon%20points='8,1%2015,14%201,14'%20fill='%23f8fafc'%20stroke='%23cbd5e1'%20stroke-width='2'/%3E%3C/svg%3E"); }}
	  </style>
</head>
<body>
  <div id="map"></div>
  <script src="/assets/vendor/leaflet/leaflet.js"></script>
  <script src="/assets/vendor/leaflet.markercluster/leaflet.markercluster.js"></script>
  <script>
    const markers = {marker_json};
    const purpleAirFields = [
      "name",
      "location_type",
      "latitude",
      "longitude",
      "rssi",
      "pm1.0",
      "pm2.5_alt",
      "pm10.0",
      "temperature",
      "humidity",
      "last_seen"
    ];
    const purpleAirApiKey = "D80F3AFD-DDAD-11ED-BD21-42010A800008";
    const disableTiles = {str(DISABLE_LEAFLET_TILES).lower()};
    const map = L.map("map", {{
      minZoom: 5,
      maxZoom: 19,
      maxBounds: [[-37.50528021, 130.9992792], [-28.15701999, 163.638889]],
      maxBoundsViscosity: 0.9
    }}).setView([-33.0, 147.032179], 6);

    const nswBounds = [[{NSW_MAP_BOUNDS["south"]}, {NSW_MAP_BOUNDS["west"]}], [{NSW_MAP_BOUNDS["north"]}, {NSW_MAP_BOUNDS["east"]}]];
    try {{ map.fitBounds(nswBounds, {{ padding: [18, 18] }}); }} catch (e) {{}}

    function addTileLayerCandidates(urls) {{
      let idx = 0;
      let layer = null;
      function attach(url) {{
        layer = L.tileLayer(url, {{
          maxZoom: 19,
          subdomains: ["a", "b", "c"],
          attribution: `{LEAFLET_TILE_ATTRIBUTION}`
        }});
        layer.on("tileerror", function() {{
          try {{ map.removeLayer(layer); }} catch (e) {{}}
          idx += 1;
          if (idx < urls.length) {{
            attach(urls[idx]);
            layer.addTo(map);
            return;
          }}
          try {{ map.getContainer().style.background = "#eef4e8"; }} catch (e) {{}}
          if (!document.getElementById("tile-warning")) {{
            const warn = document.createElement("div");
            warn.id = "tile-warning";
            warn.textContent = "Basemap tiles unavailable (showing markers only).";
            warn.style.cssText = "position:absolute; top:10px; left:50%; transform:translateX(-50%); z-index:9999; background:rgba(255,255,255,0.94); border:1px solid rgba(148,163,184,0.55); padding:6px 10px; border-radius:999px; font-weight:800; font-size:12px; color:#0f172a; box-shadow:0 8px 20px rgba(15,23,42,0.14);";
            document.body.appendChild(warn);
          }}
        }});
      }}
      if (urls && urls.length) {{
        attach(urls[0]);
        return layer;
      }}
      return null;
    }}

    if (!disableTiles) {{
      const tileUrls = {_leaflet_tile_urls_js()};
      const tileLayer = addTileLayerCandidates(tileUrls);
      if (tileLayer) tileLayer.addTo(map);
    }} else {{
      try {{ map.getContainer().style.background = "#eef4e8"; }} catch (e) {{}}
    }}
    L.control.scale({{ imperial: false }}).addTo(map);

    function addStateOutlines() {{
      fetch("/assets/australian-states.json")
        .then((r) => r.json())
        .then((geo) => {{
          try {{
            L.geoJSON(geo, {{
              style: (feature) => {{
                const name = (feature && feature.properties && feature.properties.STATE_NAME) ? String(feature.properties.STATE_NAME) : "";
                const isNsw = name.toLowerCase() === "new south wales";
                return {{
                  color: isNsw ? "rgba(30,64,175,0.9)" : "rgba(15,23,42,0.35)",
                  weight: isNsw ? 2.4 : 1.2,
                  fillColor: isNsw ? "rgba(59,130,246,0.12)" : "rgba(148,163,184,0.08)",
                  fillOpacity: 0.6,
                }};
              }}
            }}).addTo(map);
          }} catch (e) {{}}
        }})
        .catch(() => {{}});
    }}
    addStateOutlines();

    const group = L.featureGroup().addTo(map);
    const sensorGroup = L.featureGroup().addTo(group);
    const dustWatchGroup = L.featureGroup().addTo(group);
    const aqmsGroup = L.featureGroup().addTo(group);
	    const purpleAirMarkers = new Map();

	    function sourceName(item) {{
	      return String((item && item.source) || "").toLowerCase();
	    }}

	    function isPurpleAir(item) {{
	      return sourceName(item) === "purpleair";
	    }}

	    function isDustWatch(item) {{
	      return sourceName(item).replace(/\s+/g, "") === "dustwatch";
	    }}

	    function purpleAirCategory(value) {{
	      if (value == null || Number.isNaN(Number(value))) {{
	        return {{ label: "No data", colour: "#9ca3af" }};
	      }}
      const pm25 = Number(value);
      if (pm25 < 25) return {{ label: "Good", colour: "#16a34a" }};
      if (pm25 < 50) return {{ label: "Fair", colour: "#facc15" }};
      if (pm25 < 100) return {{ label: "Poor", colour: "#f97316" }};
      if (pm25 < 300) return {{ label: "Very poor", colour: "#ef4444" }};
      return {{ label: "Extremely poor", colour: "#7f1d1d" }};
    }}

		    function purpleAirSize(value) {{
		      return 22;
		    }}

    function purpleAirIcon(item) {{
      const sensorValue = item.sensorValue == null ? item.pm25 : item.sensorValue;
      const size = purpleAirSize(sensorValue);
      const category = purpleAirCategory(sensorValue);
      return L.divIcon({{
        className: "",
	        html: `<div class="purpleair-marker" style="width:${{size}}px;height:${{size}}px;background:${{category.colour}};font-size:${{size > 30 ? 10 : 9}}px"></div>`,
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
        popupAnchor: [0, -(size / 2)]
	      }});
	    }}

	    function aqmsIcon(item) {{
		      return L.divIcon({{
		        className: "",
			        html: `<div class="station-marker" style="background:${{item.colour}}"></div>`,
		        iconSize: [22, 22],
		        iconAnchor: [11, 11],
		        popupAnchor: [0, -12]
		      }});
		    }}

	    function dustWatchIcon(item) {{
		      return L.divIcon({{
		        className: "",
		        html: `<div class="dustwatch-marker"><svg viewBox="0 0 22 22" aria-hidden="true"><polygon points="11,1 21,21 1,21" fill="${{item.colour}}" stroke="#cbd5e1" stroke-width="2"/></svg></div>`,
		        iconSize: [22, 22],
		        iconAnchor: [11, 19],
		        popupAnchor: [0, -20]
		      }});
		    }}

    function purpleAirPopupHtml(item, loadingText) {{
      return `
	        <strong>${{item.station}}</strong><br>
	        Source: <strong>PurpleAir</strong><br>
	        Sensor ID: ${{item.siteId}}<br>
	        PM2.5: <strong>${{(item.sensorValue == null && item.pm25 == null) ? "--" : formatPaValue(item.sensorValue == null ? item.pm25 : item.sensorValue)}}</strong><br>
	        Category: <strong>${{item.paCategory || item.category || "No data"}}</strong><br>
        Lat/Lon: ${{item.lat.toFixed(5)}}, ${{item.lon.toFixed(5)}}<br>
        <div id="purpleair-${{item.siteId}}" style="margin-top:8px;">${{loadingText || ""}}</div>
      `;
    }}
	    markers.forEach((item) => {{
	      if (item.lat == null || item.lon == null) return;
	      const markerIcon = isPurpleAir(item) ? purpleAirIcon(item) : (isDustWatch(item) ? dustWatchIcon(item) : aqmsIcon(item));
	      const marker = L.marker([item.lat, item.lon], {{
	        icon: markerIcon,
	        zIndexOffset: isPurpleAir(item) ? 100 : (isDustWatch(item) ? 200 : 500)
	      }});
	      if (isPurpleAir(item)) {{
	        marker.addTo(sensorGroup);
	        purpleAirMarkers.set(String(item.siteId), {{ item, marker }});
	      }} else if (isDustWatch(item)) {{
	        marker.addTo(dustWatchGroup);
	      }} else {{
        marker.addTo(aqmsGroup);
	      }}
	      marker.bindTooltip(item.station, {{ direction: "top", className: "station-label" }});
	      const purpleAirPanelId = `purpleair-${{item.siteId}}`;
	      marker.bindPopup(isPurpleAir(item) ? purpleAirPopupHtml(item, "Click opened. Loading PurpleAir values...") : `
	        <strong>${{item.station}}</strong><br>
	        Source: <strong>${{item.source || "AQMS"}}</strong><br>
	        Value: <strong>${{item.valueLabel}}</strong><br>
        Category: <strong>${{item.category}}</strong><br>
        Time: ${{item.hour || "--"}}<br>
        Date: ${{item.date || "--"}}<br>
        Pollutant: ${{item.pollutant || "--"}}<br>
        Region: ${{item.region || "--"}}
      `);
	      marker.on("click", () => {{
	        if (!item.siteId) return;
	        const key = isPurpleAir(item) ? `purpleair:${{item.siteId}}` : `aqms:${{item.siteId}}`;
	        try {{
	          window.parent.postMessage({{ type: "monitor-site-select", monitor: key }}, "*");
	        }} catch (err) {{}}
	      }});
	      if (isPurpleAir(item)) {{
	        marker.on("popupopen", () => fetchPurpleAirValues(item, purpleAirPanelId));
	      }}
	    }});

    function formatPaValue(value) {{
      if (value == null || Number.isNaN(Number(value))) return "--";
      return Math.max(Number(value), 0.2).toFixed(1);
    }}

    function fetchPurpleAirValues(item, panelId) {{
      const panel = document.getElementById(panelId);
      if (!panel || !item.siteId) return;
      const fields = purpleAirFields.join("%2C%20");
      const url = `https://api.purpleair.com/v1/sensors/${{item.siteId}}?sensor_index=${{item.siteId}}&fields=${{fields}}`;
      fetch(url, {{
        method: "GET",
        headers: {{ "X-API-Key": purpleAirApiKey }}
      }})
        .then((response) => response.json())
        .then((payload) => {{
          const sensor = payload.sensor || {{}};
          updatePurpleAirMarker(item, sensor);
          const seen = sensor.last_seen ? new Date(sensor.last_seen * 1000).toLocaleString("en-AU") : "--";
          const tempC = sensor.temperature == null ? "--" : (((Number(sensor.temperature) - 32) * 5) / 9).toFixed(1);
          panel.innerHTML = `
            <div><strong>PM1.0:</strong> ${{formatPaValue(sensor["pm1.0"])}}</div>
            <div><strong>PM2.5:</strong> ${{formatPaValue(sensor["pm2.5_alt"])}}</div>
            <div><strong>PM10:</strong> ${{formatPaValue(sensor["pm10.0"])}}</div>
            <div><strong>Temperature:</strong> ${{tempC}} &deg;C</div>
            <div><strong>Humidity:</strong> ${{sensor.humidity == null ? "--" : sensor.humidity + "%"}}</div>
            <div><strong>Retrieved:</strong> ${{seen}}</div>
          `;
        }})
        .catch((error) => {{
          panel.innerHTML = "PurpleAir values could not be loaded.";
        }});
    }}

    function updatePurpleAirMarker(item, sensor) {{
	      const pm25 = sensor["pm2.5_alt"];
	      if (pm25 != null && !Number.isNaN(Number(pm25))) {{
	        item.pm25 = Math.max(Number(pm25), 0.2);
	        item.sensorValue = item.pm25;
	        item.valueLabel = formatPaValue(item.pm25);
	      }}
	      const category = purpleAirCategory(item.sensorValue == null ? item.pm25 : item.sensorValue);
	      item.paCategory = category.label;
	      item.category = category.label;
	      item.colour = category.colour;
      const entry = purpleAirMarkers.get(String(item.siteId));
      if (entry) {{
        entry.marker.setIcon(purpleAirIcon(item));
        entry.marker.setPopupContent(purpleAirPopupHtml(item, ""));
      }}
    }}

    function fetchPurpleAirSnapshot() {{
      if (purpleAirMarkers.size === 0) return;
      const fields = purpleAirFields.join("%2C%20");
      const url = `https://api.purpleair.com/v1/sensors?fields=${{fields}}&nwlng=130.9992792&nwlat=-28.15701999&selng=163.638889&selat=-37.50528021`;
      fetch(url, {{
        method: "GET",
        headers: {{ "X-API-Key": purpleAirApiKey }}
      }})
        .then((response) => response.json())
        .then((payload) => {{
          const fields = payload.fields || [];
          const idIndex = fields.indexOf("sensor_index");
          const pm25Index = fields.indexOf("pm2.5_alt");
          const lastSeenIndex = fields.indexOf("last_seen");
          if (idIndex < 0 || pm25Index < 0 || !Array.isArray(payload.data)) return;
          payload.data.forEach((row) => {{
            const entry = purpleAirMarkers.get(String(row[idIndex]));
            if (!entry) return;
            updatePurpleAirMarker(entry.item, {{
              "pm2.5_alt": row[pm25Index],
              "last_seen": lastSeenIndex >= 0 ? row[lastSeenIndex] : null
            }});
          }});
        }})
        .catch((error) => {{}});
    }}

    fetchPurpleAirSnapshot();

    try {{ map.fitBounds(nswBounds, {{ padding: [18, 18] }}); }} catch (e) {{}}

    const legend = L.control({{ position: "bottomright" }});
    legend.onAdd = function() {{
      const div = L.DomUtil.create("div", "legend");
      div.innerHTML = `
        <div class="legend-row"><span class="legend-swatch" style="background:#42a93c"></span>Good</div>
        <div class="legend-row"><span class="legend-swatch" style="background:#eec900"></span>Fair</div>
        <div class="legend-row"><span class="legend-swatch" style="background:#e47400"></span>Poor</div>
        <div class="legend-row"><span class="legend-swatch" style="background:#ba0029"></span>Very poor</div>
	        <div class="legend-row"><span class="legend-swatch" style="background:#590019"></span>Extremely poor</div>
	        <div class="legend-row"><span class="legend-swatch" style="background:#242424"></span>No data</div>
	        <div class="legend-row"><span class="legend-source-circle"></span>AQMS</div>
	        <div class="legend-row"><span class="legend-source-triangle"></span>DustWatch</div>
	        <div class="legend-row"><span class="legend-source-square"></span>PurpleAir</div>
	      `;
      return div;
    }};
    legend.addTo(map);
  </script>
</body>
</html>"""


def _selected_time_label(parsed, hour_index):
    if not parsed:
        return "--"
    times = parsed.get("data", {}).get("time", {}).get("forecastTime", [])
    if not times:
        return "--"
    if hour_index < 0:
        hour_index = 0
    if hour_index >= len(times):
        hour_index = len(times) - 1
    return times[hour_index]


def _slider_marks(times):
    if not times:
        return {0: "--"}
    last_index = len(times) - 1
    marks = {0: times[0]}
    if last_index > 0:
        mid = last_index // 2
        marks[mid] = times[mid]
        marks[last_index] = times[last_index]
    return marks


def _make_summary_cards(parsed, selection, hour_index, loaded_file_path=None):
    times = parsed.get("data", {}).get("time", {}).get("forecastTime", []) if parsed else []
    stations = parsed.get("data", {}).get("stations", {}) if parsed else {}
    station_count = len(stations)
    ranked_count = len(parsed.get("ranking", {})) if parsed else 0

    def _metric_tile(kind, title, value, subtitle, color=None, units=None):
        tile_class = "metric-tile" + (f" metric-tile--{kind}" if kind else "")
        icon_class = "metric-icon" + (f" metric-icon--{kind}" if kind else "") + (f" metric-icon--{color}" if color else "")
        value_class = "metric-value" + (f" metric-value--{color}" if color else "")
        return html.Div(
            [
                html.Div(className=icon_class),
                html.Div(
                    [
                        html.Div(title, className="metric-title"),
                        html.Div(
                            [
                                html.Span(value, className=value_class),
                                html.Span(units, className="metric-units") if units else None,
                            ],
                            className="metric-main",
                        ),
                        html.Div(subtitle or "", className="metric-subtitle"),
                    ],
                    className="metric-body",
                ),
            ],
            className=tile_class,
        )

    region_code = selection.get("regions") or "--"
    region_label = title_case_station_name(str(region_code).replace("_", " "))
    region_name = _region_lookup().get(str(region_code).strip()) or ""
    region_subtitle = region_name or ""

    time_scope = selection.get("timeScopes")
    horizon_label = f"{time_scope}-hour horizon" if time_scope else "Forecast horizon"

    pollutant_key = selection.get("pollutants")
    units = (POLLUTANTS.get(pollutant_key) or {}).get("Units") or ""

    # Compute station values for the selected hour.
    values = []
    target_index = hour_index or 0
    if target_index < 0:
        target_index = 0
    for station_name, payload in stations.items():
        series = payload.get("forecastValue", [])
        if not series:
            continue
        idx = min(target_index, len(series) - 1)
        try:
            value = float(series[idx])
        except (TypeError, ValueError):
            continue
        values.append((station_name, value))

    max_value = None
    max_station = None
    mean_value = None
    if values:
        max_station, max_value = max(values, key=lambda item: item[1])
        mean_value = sum(val for _, val in values) / float(len(values))

    max_label = f"Max {pollutant_key} (region)" if pollutant_key else "Max (region)"
    mean_label = f"Mean {pollutant_key} (region)" if pollutant_key else "Mean (region)"

    max_value_text = "--" if max_value is None else f"{max_value:.2f}"
    mean_value_text = "--" if mean_value is None else f"{mean_value:.2f}"
    max_subtitle = f"At {title_case_station_name(max_station)}" if max_station else ""
    mean_subtitle = "Average forecast"

    return [
        _metric_tile("region", "Selected region", region_label, region_subtitle, color="blue"),
        _metric_tile("active", "Active stations", str(station_count), "Monitoring sites", color="blue"),
        _metric_tile("ranked", "Stations ranked", str(ranked_count), "With forecast", color="green"),
        _metric_tile("steps", "Forecast steps", str(len(times)), horizon_label, color="purple"),
        _metric_tile("max", max_label, max_value_text, max_subtitle, color="orange", units=units),
        _metric_tile("mean", mean_label, mean_value_text, mean_subtitle, color="green", units=units),
    ]


def _activity_guide_panel():
    if not GENERAL_HEALTH_GUIDE:
        return html.Div(
            "AQ Standard could not be loaded.",
            className="activity-guide-panel activity-guide-panel--empty",
        )

    header_cells = [
        html.Th("Air quality category", rowSpan=2),
        html.Th("Recommended actions"),
        html.Th(""),
    ]
    subheader_cells = [
        html.Th(
            [
                "Sensitive groups including:",
                html.Br(),
                "people with a heart or lung condition, including asthma",
                html.Br(),
                "people over the age of 65",
                html.Br(),
                "infants and children",
                html.Br(),
                "pregnant women",
            ]
        ),
        html.Th("Everyone else"),
    ]
    rows = []
    for category in GENERAL_HEALTH_GUIDE:
        row_class = f"activity-guide__row-header activity-guide__row-header--{category.get('class', '').strip()}"
        rows.append(
            html.Tr(
                [
                    html.Td(html.Strong(category.get("name", "")), className=row_class),
                    html.Td(
                        html.Ul([html.Li(item) for item in category.get("sensitiveGroups", [])]),
                        className="activity-guide__cell",
                    ),
                    html.Td(
                        html.Ul([html.Li(item) for item in category.get("everyoneElse", [])]),
                        className="activity-guide__cell",
                    ),
                ]
            )
        )

    return html.Section(
        [
            html.Div(
                [
                    html.H2("AQ Standard"),
                    html.P("The table below mirrors the NSW guidance used in the original dashboard."),
                ],
                className="activity-guide__heading",
            ),
            html.Div(
                html.Table(
                    [
                        html.Thead(
                            [
                                html.Tr(header_cells),
                                html.Tr(subheader_cells),
                            ]
                        ),
                        html.Tbody(rows),
                    ],
                    className="activity-guide-table",
                ),
                className="activity-guide__table-wrap",
            ),
        ],
        className="activity-guide-panel",
    )


def _load_forecast(selection):
    file_path = _build_file_path(selection)
    exact_match = True
    if not file_path:
        file_path, exact_match = _best_matching_file(selection)
    if not file_path:
        return None, "No forecast file could be matched to the current selection."
    try:
        max_forecast_hours = int(selection.get("timeScopes"))
    except (TypeError, ValueError):
        max_forecast_hours = None
    parsed = parse_csv(file_path, selection.get("pollutants"), max_forecast_hours=max_forecast_hours)
    if isinstance(parsed, dict) and parsed.get("error"):
        return None, parsed["error"]
    parsed["_sourceFile"] = file_path
    parsed["_exactMatch"] = exact_match
    try:
        parsed["_generatedAtEpoch"] = os.path.getmtime(file_path)
    except OSError:
        parsed["_generatedAtEpoch"] = None
    if exact_match:
        return parsed, f"Loaded {Path(file_path).name}"
    return parsed, f"Closest match: {Path(file_path).name}"


"""
Startup performance note
------------------------
This module used to eagerly load and parse the latest forecast CSV + monitoring sensor list at import time.
On remote hosts that delayed binding the Dash server port and made it feel like the app "hangs" before you can open it.

We now start with placeholders and let the existing callbacks (monitor refresh + forecast tab selection)
fetch data once a browser session connects.
"""

INITIAL_MONITOR_ROWS = []
INITIAL_MONITOR_STATUS = "Monitoring feed is loading."

INITIAL_MONITOR_STATION_LIST = _monitor_station_list(INITIAL_MONITOR_ROWS)
INITIAL_MONITOR_SUMMARY_CARDS = _monitoring_summary_cards(INITIAL_MONITOR_ROWS)
INITIAL_MONITOR_TIME_LABEL = _format_monitor_label(_monitoring_time_row(INITIAL_MONITOR_ROWS)) if INITIAL_MONITOR_ROWS else "--"
INITIAL_MONITOR_UPDATED_LABEL = _format_header_timestamp(datetime.now(tz.gettz("Australia/Sydney")).timestamp())

if INITIAL_FORECAST_PARSED:
    INITIAL_FORECAST_TIMES = INITIAL_FORECAST_PARSED.get("data", {}).get("time", {}).get("forecastTime", [])
    INITIAL_FORECAST_TIME_INDEX = _forecast_live_base_index(INITIAL_FORECAST_TIMES)
    INITIAL_FORECAST_SUMMARY_CARDS = _make_summary_cards(INITIAL_FORECAST_PARSED, DEFAULT_SELECTION, INITIAL_FORECAST_TIME_INDEX)
    INITIAL_FORECAST_TIME_LABEL = _format_forecast_label(_selected_time_label(INITIAL_FORECAST_PARSED, INITIAL_FORECAST_TIME_INDEX))
    INITIAL_FORECAST_FILE_INFO = [
        html.Div(["Forecast file: ", html.Code(Path(INITIAL_FORECAST_PARSED.get("_sourceFile", "Unknown")).name)]),
        html.Div(["Pollutant: ", html.Strong(pollutant_display(DEFAULT_SELECTION["pollutants"]))]),
        html.Div(["Stations parsed: ", html.Strong(str(len(INITIAL_FORECAST_PARSED.get("data", {}).get("stations", {}))))]),
        html.Div(["Current time: ", html.Strong(INITIAL_FORECAST_TIME_LABEL)]),
        html.Div(["Steps available: ", html.Strong(str(len(INITIAL_FORECAST_TIMES)))]),
    ]
    INITIAL_FORECAST_MAP = _map_figure(INITIAL_FORECAST_PARSED, DEFAULT_SELECTION["pollutants"], INITIAL_FORECAST_TIME_INDEX)
    INITIAL_FORECAST_MAP_HTML = _leaflet_forecast_map_html(INITIAL_FORECAST_PARSED, DEFAULT_SELECTION["pollutants"], INITIAL_FORECAST_TIME_INDEX)
    INITIAL_FORECAST_RANKING = _ranking_rows(INITIAL_FORECAST_PARSED)
    INITIAL_FORECAST_RANKING_FIGURE = _station_rank_bars_for_pollutants(INITIAL_FORECAST_PARSED, DEFAULT_SELECTION, INITIAL_FORECAST_TIME_INDEX)
    INITIAL_STATION_TREND = _all_stations_trend_figure(
        INITIAL_FORECAST_PARSED,
        DEFAULT_SELECTION["pollutants"],
        DEFAULT_SELECTION.get("timeScopes"),
    )
    INITIAL_TIME_SERIES = _forecast_time_series_figure(INITIAL_FORECAST_PARSED, DEFAULT_SELECTION["pollutants"], None, 0)
    INITIAL_STATION_DETAILS = _all_stations_details(INITIAL_FORECAST_PARSED)
    INITIAL_SELECTED_STATION = None
else:
    INITIAL_FORECAST_TIME_INDEX = 0
    INITIAL_FORECAST_TIMES = []
    INITIAL_FORECAST_SUMMARY_CARDS = _make_summary_cards({}, DEFAULT_SELECTION, 0)
    INITIAL_FORECAST_TIME_LABEL = "Latest forecast run not loaded."
    INITIAL_FORECAST_FILE_INFO = ""
    INITIAL_FORECAST_MAP = go.Figure()
    INITIAL_FORECAST_MAP_HTML = _leaflet_forecast_map_html({}, DEFAULT_SELECTION.get("pollutants"), 0)
    INITIAL_FORECAST_RANKING = []
    INITIAL_FORECAST_RANKING_FIGURE = _station_rank_bars_for_pollutants({}, DEFAULT_SELECTION, 0)
    INITIAL_STATION_TREND = go.Figure()
    INITIAL_TIME_SERIES = go.Figure()
    INITIAL_STATION_DETAILS = "Click a station on the map to inspect its full forecast."
    INITIAL_SELECTED_STATION = None


app = dash.Dash(
    __name__,
    assets_folder=str(CURRENT_DIR / "assets"),
    title="NSW Air Quality Nowcasting Dashboard",
)
server = app.server
app.index_string = """
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        <link rel="icon" type="image/svg+xml" href="/assets/nsw-logo.svg">
        {%css%}
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
"""


@server.route("/favicon.ico")
def favicon():
    return send_from_directory(CURRENT_DIR / "assets", "nsw-logo.svg", mimetype="image/svg+xml")


@app.callback(Output("monitor-highest-aqms", "children"), [Input("monitor-store", "data")])
def render_monitor_highest_aqms(monitor_store):
    """Render a small card showing the highest AQMS observed value (PM2.5) from the current store."""
    monitor_store = monitor_store or {}
    rows = monitor_store.get("aqmsPm25Rows") or []
    if not rows:
        return html.Div("No AQMS data", className="card-hint")
    # Rows are not necessarily sorted; find max value
    best = None
    for r in rows:
        try:
            v = float(r.get("value")) if r.get("value") is not None else None
        except Exception:
            v = None
        if v is None:
            continue
        if best is None or v > (best.get("value") or 0):
            best = dict(r)
            best["value"] = v

    if not best:
        return html.Div("No AQMS measurements", className="card-hint")

    station = best.get("station") or "--"
    value = best.get("value")
    units = (POLLUTANTS.get("PM2.5") or {}).get("Units") or "µg/m³"
    value_label = "--" if value is None else f"{value:.1f} {units}"
    category_label, colour, _ = _category_payload("PM2.5", value, None)

    return html.Div(
        [
            html.Div(station, className="monitor-highest__station"),
            html.Div(category_label, className="monitor-highest__category", style={"backgroundColor": colour, "color": "#fff", "padding": "6px 10px", "borderRadius": "12px", "display": "inline-block"}),
            html.Div(value_label, className="monitor-highest__value"),
        ],
        className="monitor-highest__inner",
    )


@app.callback(Output("monitor-highest-purpleair", "children"), [Input("monitor-store", "data")])
def render_monitor_highest_purpleair(monitor_store):
    """Render a small card showing the highest PurpleAir observed PM2.5 value from the current store."""
    monitor_store = monitor_store or {}
    rows = monitor_store.get("purpleairSensors") or []
    if not rows:
        return html.Div("No PurpleAir data", className="card-hint")
    # rows are produced by _purpleair_rows_for_pollutant and are sorted desc
    best = rows[0]
    value = best.get("value")
    station = best.get("station") or "PurpleAir sensor"
    units = (POLLUTANTS.get("PM2.5") or {}).get("Units") or "µg/m³"
    value_label = "--" if value is None else f"{value:.1f} {units}"
    category_label = best.get("category") or "No data"
    colour = best.get("category_color") or PURPLEAIR_COLOR

    return html.Div(
        [
            html.Div(station, className="monitor-highest__station"),
            html.Div(category_label, className="monitor-highest__category", style={"backgroundColor": colour, "color": "#fff", "padding": "6px 10px", "borderRadius": "12px", "display": "inline-block"}),
            html.Div(value_label, className="monitor-highest__value"),
        ],
        className="monitor-highest__inner",
    )


@app.callback(
    Output("monitor-top-boxes", "children"),
    [Input("monitor-store", "data"), Input("monitor-pollutant", "value"), Input("monitor-source", "value")],
)
def render_monitor_top_boxes(monitor_store, pollutant_label, source):
    """Render up to 5 top boxes for AQMS and PurpleAir based on the selected pollutant."""
    monitor_store = monitor_store or {}
    pollutant = pollutant_label or "PM2.5"
    # Map pollutant to store keys for AQMS
    key_map = {"PM2.5": "aqmsPm25Rows", "PM10": "aqmsPm10Rows", "O3": "aqmsO3Rows"}
    aqms_rows = monitor_store.get(key_map.get(pollutant, "aqmsPm25Rows")) or []
    purple_rows = monitor_store.get("purpleairSensors") or []

    def top_n(rows, n=5):
        selected = []
        for r in rows:
            try:
                v = float(r.get("value")) if r.get("value") is not None else None
            except Exception:
                v = None
            if v is None:
                continue
            selected.append((v, r))
        selected.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in selected[:n]]

    aqms_top = top_n(aqms_rows, 5) if source in ("both", "aqms") else []
    pa_top = top_n(purple_rows, 5) if source in ("both", "purpleair") else []

    def box_for_row(r):
        station = r.get("station") or r.get("name") or "--"
        value = r.get("value") if r.get("value") is not None else r.get("pm25") or r.get("pm10")
        try:
            v = float(value) if value is not None else None
        except Exception:
            v = None
        units = (POLLUTANTS.get(pollutant) or {}).get("Units") or ""
        value_label = "--" if v is None else f"{v:.1f} {units}"
        category_label = r.get("category") or "No data"
        colour = r.get("category_color") or "#9ca3af"
        # Use station text as the index for the clickable id
        station_key = str(station)
        return html.Button(
            [
                html.Div(station, className="monitor-top-box__station"),
                html.Div(category_label, className="monitor-top-box__category", style={"backgroundColor": colour, "color": "#fff"}),
                html.Div(value_label, className="monitor-top-box__value"),
            ],
            id={"type": "monitor-top-box", "index": station_key},
            n_clicks=0,
            className="monitor-top-box",
            style={"width": "150px", "padding": "10px", "borderRadius": "8px", "background": "#fff", "boxShadow": "0 1px 4px rgba(2,6,23,0.08)", "marginRight": "10px", "textAlign": "center", "border": "none", "cursor": "pointer"},
        )

    rows = []
    if aqms_top:
        rows.append(html.Div([html.H4(f"Top {len(aqms_top)} AQMS ({pollutant})", style={"marginTop": "6px", "marginBottom": "8px"})]))
        rows.append(html.Div([box_for_row(r) for r in aqms_top], style={"display": "flex", "gap": "10px", "flexWrap": "nowrap", "overflowX": "auto"}))
    if pa_top:
        rows.append(html.Div([html.H4(f"Top {len(pa_top)} PurpleAir ({pollutant})", style={"marginTop": "12px", "marginBottom": "8px"})]))
        rows.append(html.Div([box_for_row(r) for r in pa_top], style={"display": "flex", "gap": "10px", "flexWrap": "nowrap", "overflowX": "auto"}))

    if not rows:
        return html.Div("No top observations available", className="card-hint")
    return html.Div(rows)


@app.callback(Output("monitor-station", "value"), [Input({"type": "monitor-top-box", "index": MATCH}, "n_clicks")], [State({"type": "monitor-top-box", "index": MATCH}, "id")], prevent_initial_call=True)
def monitor_top_box_clicked(n_clicks, box_id):
    if not n_clicks:
        return no_update
    # box_id is a dict {'type':'monitor-top-box','index': station_key}
    station_key = (box_id or {}).get("index")
    return station_key


@server.route("/debug-page")
def debug_page():
    return """
    <!doctype html>
    <html>
      <head><title>Dashboard Debug</title></head>
      <body style="font-family: sans-serif; padding: 2rem;">
        <h1>Dashboard server is reachable</h1>
        <p>If you can see this page, HTTP forwarding is working.</p>
        <p><a href="/">Open Dash dashboard</a></p>
        <p><a href="/_dash-layout">Dash layout JSON</a></p>
        <p><a href="/assets/style.css">Dashboard CSS</a></p>
      </body>
    </html>
    """

app.layout = html.Div(
    [
        dcc.Location(id="url", refresh=False),
        html.Header(
            [
                html.Div(
                    [
                        # Left: logo
                        html.Div(
                            html.A(
                                [
                                    html.Img(
                                        src=app.get_asset_url("nsw-government-transparent.png"),
                                        className="nsw-header__logo",
                                        alt="NSW Government",
                                    ),
                                ],
                                href="https://www.dpie.nsw.gov.au",
                                className="nsw-header__logo-link",
                                target="_blank",
                                rel="noreferrer",
                            ),
                            className="nsw-header__left",
                        ),

                        # Center: title
                        html.Div(
                            html.Div(
                                html.H1("NSW Air Quality Nowcasting Dashboard"),
                                className="banner",
                            ),
                            className="nsw-header__center",
                        ),

                        # Right: timestamps
                        html.Div(
                            [
                                html.Div(
                                    [
                                        html.Div("Last data update", className="header-small-label"),
                                        html.Div(
                                            INITIAL_MONITOR_UPDATED_LABEL or "--",
                                            id="header-monitor-updated",
                                            className="header-small-value",
                                        ),
                                    ],
                                    className="header-status__item",
                                ),
                                html.Div(
                                    [
                                        html.Div("Forecast generated", className="header-small-label"),
                                        html.Div(
                                            INITIAL_FORECAST_TIME_LABEL or "--",
                                            id="header-forecast-time",
                                            className="header-small-value",
                                        ),
                                    ],
                                    className="header-status__item",
                                ),
                            ],
                            className="nsw-header__right header-status",
                        ),
                    ],
                    className="nsw-header__wrapper",
                )
            ],
            className="nsw-header",
        ),
        dcc.Store(id="forecast-store", data=INITIAL_FORECAST_PARSED),
        dcc.Store(id="selected-station-store", data=INITIAL_SELECTED_STATION),
        dcc.Store(
            id="monitor-settings-store",
            data={
                "pollutant": "PM2.5",
                "source": "both",
                "region": "ALL",
                "stationKey": None,
                "window": "24h",
                "categories": [],
                "search": "",
            },
        ),
        dcc.Store(
            id="monitor-store",
            data={
                "pollutant": "PM2.5",
                "parameterCode": POLLUTANTS.get("PM2.5", {}).get("ParameterCode", "PM2.5"),
                "aqmsRows": [],
                "aqmsPm25Rows": [],
                "aqmsPm10Rows": [],
                "aqmsO3Rows": [],
                "aqmsNo2Rows": [],
                "aqmsSo2Rows": [],
                "aqmsCoRows": [],
                "dustwatchPm25Rows": [],
                "dustwatchPm10Rows": [],
                "aqcRows": [],
                "aqmsSnapshotBySite": {},
                "purpleairSensors": [],
                "purpleairClusters": [],
                "mapRows": [],
                "kpis": {},
                "tableRows": [],
                "tableRowsAll": [],
                "feeds": {},
                "latestLabel": "--",
                "fetchedAtEpoch": None,
                "fetchAttemptEpoch": None,
                "aqmsSource": None,
                "status": INITIAL_MONITOR_STATUS,
                "error": None,
            },
        ),
        dcc.Store(id="selected-monitor-site-store", data=None),
        dcc.Store(id="forecast-catalog-store", data={"latestDate": DEFAULT_SELECTION.get("date")}),
        dcc.Store(id="overview-nsw-region-index", data=0),
        dcc.Store(id="overview-nsw-station-index", data=0),
        dcc.Store(id="overview-met-region-index", data=0),
        # Preload only one region so Overview paints quickly; scrolling loads more.
        dcc.Store(id="overview-trends-visible", data=["Sydney_East"]),
        dcc.Store(id="overview-trends-index", data=0),
        dcc.Interval(id="forecast-playback", interval=1800, n_intervals=0, disabled=True),
        dcc.Interval(id="forecast-catalog-refresh", interval=2 * 60 * 1000, n_intervals=0),
        # Monitoring feed calls external APIs and can be slow on some hosts/networks.
        # Start disabled and enable shortly after first paint so the app opens fast.
        # Keep monitoring refresh disabled on startup to avoid blocking the server on slow external APIs.
        # It is enabled when the Monitoring tab is opened.
        dcc.Interval(id="monitor-refresh", interval=MONITOR_REFRESH_MS, n_intervals=0, disabled=True),
        dcc.Interval(id="overview-clock", interval=60 * 1000, n_intervals=0),
        # Load forecast data shortly after the first page paint so Overview can render forecast panels
        # without delaying server start/bind.
        dcc.Interval(id="overview-forecast-load", interval=800, n_intervals=0, max_intervals=1),
        # Load AQMS monitoring snapshot shortly after first paint so Overview cards have values,
        # but keep it separate from the full Monitoring tab refresh loop.
        # Retry once to avoid a blank status card if the first AQMS call races network readiness.
        dcc.Interval(id="overview-monitor-load", interval=1500, n_intervals=0, max_intervals=2),
        # Meteorology panel uses observation history (slower). Load it after the page paints.
        dcc.Interval(id="overview-met-load", interval=2600, n_intervals=0, max_intervals=1),
        # Autoplay the overview region panels so met/aq status advance automatically
        dcc.Interval(id="overview-region-autoplay", interval=8000, n_intervals=0),
        html.Div(id="forecast-map-updater", style={"display": "none"}),
        html.Div(
            [
                dcc.Tabs(
                    id="dashboard-tabs",
                    value="overview",
                    parent_className="dashboard-tabs",
                    className="dashboard-tabs__inner",
                    children=[
                        dcc.Tab(
                            label="Overview",
                            value="overview",
                            className="dashboard-tab dashboard-tab--overview",
                            selected_className="dashboard-tab--selected",
                            children=html.Div(
                                [
                                    html.Section(
                                        [
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.H2("Overview"),
                                                        ],
                                                        className="monitor__heading",
                                                    ),
                                                ],
                                                className="card-heading-row",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.H4("Air Quality Status", className="overview-aq-status-title"),
                                                                            html.Div(id="overview-nsw-air-quality-updated", className="card-hint"),
                                                                        ],
                                                                        className="overview-card-title-row overview-card-title-row--status",
                                                                    ),
                                                                    html.Div(
                                                                        [
                                                                            html.Button("‹", id="overview-nsw-region-prev", n_clicks=0, className="nsw-aq-status__nav-btn"),
                                                                            html.Div("NSW", id="overview-nsw-region-name", className="nsw-aq-status__nav-label"),
                                                                            html.Button("›", id="overview-nsw-region-next", n_clicks=0, className="nsw-aq-status__nav-btn"),
                                                                        ],
                                                                        className="nsw-aq-status__nav",
                                                                    ),
                                                                    html.Div(id="overview-nsw-air-quality-status"),
                                                                    html.Div(
                                                                        [
                                                                            html.Button("‹", id="overview-nsw-station-prev", n_clicks=0, className="nsw-aq-status__nav-btn"),
                                                                            html.Div("", id="overview-nsw-station-name", className="nsw-aq-status__nav-label"),
                                                                            html.Button("›", id="overview-nsw-station-next", n_clicks=0, className="nsw-aq-status__nav-btn"),
                                                                        ],
                                                                        className="nsw-aq-status__nav",
                                                                        style={"display": "none"},
                                                                    ),
                                                                    html.Div(id="overview-nsw-station-box", style={"display": "none"}),
                                                                ],
                                                                className="control-card overview-status-summary",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.H4("Nowcasting Outlook", className="overview-aq-status-title"),
                                                                            html.Div(id="overview-next3-time-labels", className="card-hint"),
                                                                        ],
                                                                        className="overview-station-outlook-heading",
                                                                    ),
                                                                    html.Div(id="overview-next3-grid", className="overview-next3-table overview-next3-table--tight"),
                                                                    html.Button(
                                                                        "All stations forecast",
                                                                        id="overview-open-allstations",
                                                                        n_clicks=0,
                                                                        className="overview-next2-allstations__summary",
                                                                    ),
                                                                ],
                                                                className="overview-next3-card overview-next3-card--inline overview-station-card",
                                                            ),
                                                        ],
                                                        className="overview-status-content",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.H4("Nowcasting map", className="overview-map-title"),
                                                                            html.Div(
                                                                                [
                                                                                    dcc.Dropdown(
                                                                                        id="overview-forecast-map-pollutant",
                                                                                        options=[
                                                                                            {"label": "PM2.5", "value": "PM2.5"},
                                                                                            {"label": "PM10", "value": "PM10"},
                                                                                            {"label": "O3", "value": "O3"},
                                                                                        ],
                                                                                        value="PM2.5",
                                                                                        clearable=False,
                                                                                        searchable=False,
                                                                                    ),
                                                                                    html.Button("‹", id="overview-forecast-prev-hour", n_clicks=0, className="nsw-aq-status__nav-btn", title="Previous hour"),
                                                                                    html.Button("›", id="overview-forecast-next-hour", n_clicks=0, className="nsw-aq-status__nav-btn", title="Next hour"),
                                                                                    dcc.Store(id="overview-forecast-hour-index", data=0),
                                                                                ],
                                                                                className="overview-nowcasting-controls",
                                                                            ),
                                                                            html.Div(id="overview-next-hour-forecast-time", className="overview-map-time"),
                                                                        ],
                                                                        className="card-heading-row overview-map-card-heading",
                                                                    ),
                                                                    html.Iframe(
                                                                        id="overview-next-hour-forecast-map",
                                                                        srcDoc=_leaflet_forecast_map_html({}, DEFAULT_SELECTION.get("pollutants"), 0),
                                                                        className="map-frame map-frame--overview-next map-frame--forecast",
                                                                    ),
                                                                    html.Div(id="overview-next-hour-station-panel", className="overview-next-hour-station-panel"),
                                                                ],
                                                                className="map-card overview-current-card overview-area--forecast-map",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.H4("Observation map"),
                                                                            html.Div(
                                                                                [
                                                                                    dcc.Dropdown(
                                                                                        id="overview-observation-map-pollutant",
                                                                                        options=[
                                                                                            {"label": "PM2.5", "value": "PM2.5"},
                                                                                            {"label": "PM10", "value": "PM10"},
                                                                                            {"label": "O3", "value": "O3"},
                                                                                        ],
                                                                                        value="PM2.5",
                                                                                        clearable=False,
                                                                                        searchable=False,
                                                                                    ),
                                                                                    html.Div(id="overview-observation-time", className="overview-map-time"),
                                                                                ],
                                                                                className="overview-observation-controls",
                                                                            ),
                                                                        ],
                                                                        className="card-heading-row overview-observation-heading",
                                                                    ),
                                                                    html.Iframe(
                                                                        id="overview-observation-map",
                                                                        srcDoc=_leaflet_monitoring_map_html(INITIAL_MONITOR_ROWS),
                                                                        className="map-frame map-frame--monitor map-frame--monitor-large",
                                                                    ),
                                                                ],
                                                                className="map-card overview-area--observation-map",
                                                            ),
                                                        ],
                                                        className="overview-left-column",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.H4("Regional Forecast"),
                                                                    html.Div(
                                                                        [
                                                                            html.Button("‹", id="overview-trends-prev", n_clicks=0, className="overview-trend-nav"),
                                                                            html.Button("›", id="overview-trends-next", n_clicks=0, className="overview-trend-nav"),
                                                                            html.Div(id="overview-trends-current-region-label", className="overview-regional-forecast-time"),
                                                                        ],
                                                                        className="overview-regional-forecast-controls",
                                                                    ),
                                                                ],
                                                                className="card-heading-row overview-regional-forecast-heading",
                                                            ),
                                                            html.Div(id="overview-forecast-trends", className="overview-trends-grid overview-forecast-pollutants"),
                                                        ],
                                                        className="ranking-card overview-trends-card overview-area--trends",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.H4("Regional Observed", id="overview-regional-air-quality-title"),
                                                                    html.Span("AQMS region average", className="card-hint"),
                                                                ],
                                                                className="card-heading-row",
                                                            ),
                                                            html.Div(id="overview-observed-grid", className="overview-observed-grid overview-observed-pollutants"),
                                                            html.Div(id="overview-pm25-top3", style={"display": "none"}),
                                                            dcc.Dropdown(id="overview-pm25-region-dropdown", options=[], value=None, style={"display": "none"}),
                                                            html.Div(id="overview-pm25-other", style={"display": "none"}),
                                                        ],
                                                        className="ranking-card overview-area--observed",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Button("‹", id="overview-met-region-prev", n_clicks=0),
                                                            html.Div("NSW", id="overview-met-region-name"),
                                                            html.Button("›", id="overview-met-region-next", n_clicks=0),
                                                            html.Div(id="overview-met-6h"),
                                                        ],
                                                        style={"display": "none"},
                                                    ),
                                                ],
                                                className="overview-dashboard",
                                            ),
                                        ],
                                        className="monitor-panel",
                                    )
                                ],
                                className="dashboard-tab-panel",
                            ),
                        ),
                        
                        dcc.Tab(
                            label="Monitoring",
                            value="monitor",
                            className="dashboard-tab dashboard-tab--monitor",
                            selected_className="dashboard-tab--selected",
                            children=html.Div(
                                [
                                    html.Section(
                                        [
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.H2("Monitoring"),
                                                                    html.P("Investigate current and historical observations from AQMS monitoring stations and PurpleAir sensors."),
                                                                ],
                                                                className="monitor__heading",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.Label("Region"),
                                                                            dcc.Dropdown(
                                                                                id="monitor-region",
                                                                                options=MONITOR_REGION_OPTIONS,
                                                                                value="ALL",
                                                                                clearable=False,
                                                                            ),
                                                                        ],
                                                                        className="filter-field",
                                                                    ),
                                                                    html.Div(
                                                                        [
                                                                            html.Label("Station"),
                                                                            dcc.Dropdown(
                                                                                id="monitor-station",
                                                                                options=[],
                                                                                value=None,
                                                                                clearable=True,
                                                                                placeholder="Select a station…",
                                                                            ),
                                                                        ],
                                                                        className="filter-field",
                                                                    ),
                                                                    html.Div(
                                                                        [
                                                                            html.Label("Pollutant"),
                                                                            dcc.Dropdown(
                                                                                id="monitor-pollutant",
                                                                                options=MONITOR_POLLUTANT_OPTIONS,
                                                                                value="PM2.5",
                                                                                clearable=False,
                                                                            ),
                                                                        ],
                                                                        className="filter-field",
                                                                    ),
                                                                    html.Div(
                                                                        [
                                                                            html.Label("Source"),
                                                                            dcc.Dropdown(
                                                                                id="monitor-source",
                                                                                options=[
                                                                                    {"label": "AQMS", "value": "aqms"},
                                                                                    {"label": "PurpleAir", "value": "purpleair"},
                                                                                    {"label": "Both", "value": "both"},
                                                                                ],
                                                                                value="both",
                                                                                clearable=False,
                                                                            ),
                                                                        ],
                                                                        className="filter-field",
                                                                    ),
                                                                    html.Div(
                                                                        [
                                                                            html.Label("Time window"),
                                                                            dcc.Dropdown(
                                                                                id="monitor-window",
                                                                                options=[
                                                                                    {"label": "Last 6 hours", "value": "6h"},
                                                                                    {"label": "Last 24 hours", "value": "24h"},
                                                                                    {"label": "Last 48 hours", "value": "48h"},
                                                                                    {"label": "Last 7 days", "value": "7d"},
                                                                                ],
                                                                                value="24h",
                                                                                clearable=False,
                                                                            ),
                                                                        ],
                                                                        className="filter-field",
                                                                    ),
                                                                    html.Div(
                                                                        [
                                                                            html.Label("AQ category"),
                                                                            dcc.Dropdown(
                                                                                id="monitor-category-filter",
                                                                                options=MONITOR_CATEGORY_OPTIONS,
                                                                                value=[],
                                                                                multi=True,
                                                                                placeholder="All categories",
                                                                            ),
                                                                        ],
                                                                        className="filter-field",
                                                                    ),
                                                                    html.Div(
                                                                        [
                                                                            html.Label("Search station"),
                                                                            dcc.Input(
                                                                                id="monitor-station-search",
                                                                                type="text",
                                                                                value="",
                                                                                placeholder="Type to filter stations…",
                                                                                className="monitor-search",
                                                                            ),
                                                                        ],
                                                                        className="filter-field",
                                                                    ),
                                                                ],
                                                                className="forecast-toolbar__filters monitor-toolbar__filters",
                                                            ),
                                                        ],
                                                        className="forecast-toolbar__row",
                                                    )
                                                ],
                                                className="control-card monitor-toolbar",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(id="monitor-kpi-row", className="metric-row"),
                                                    html.Div(id="monitor-time-label", className="time-label"),
                                                ],
                                                className="monitor-diagnostics",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                            [
                                                                    # Highest AQMS observed box (e.g. PM2.5 highest station)
                                                                    html.Div(
                                                                        [
                                                                            html.Div(
                                                                                [
                                                                                    html.H4("Observation map"),
                                                                                    html.Span("Hover for details. Click to select a station.", className="card-hint"),
                                                                                ],
                                                                                className="card-heading-row",
                                                                            ),
                                                                                html.Iframe(
                                                                                    id="monitor-map",
                                                                                    srcDoc=_leaflet_monitoring_map_html(INITIAL_MONITOR_ROWS),
                                                                                    className="map-frame map-frame--monitor map-frame--monitor-large",
                                                                                ),
                                                                        ],
                                                                        className="map-card",
                                                                    ),
                                                                    # Container for top-N boxes (AQMS and PurpleAir) rendered below the map
                                                                    html.Div(id="monitor-top-boxes", className="monitor-top-boxes", style={"marginTop": "12px"}),
                                                                ],
                                                                className="monitor-main__map",
                                                            ),
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.H4("Station ranking (current snapshot)"),
                                                                            html.Button(
                                                                                "View all stations →",
                                                                                id="monitor-view-all",
                                                                                n_clicks=0,
                                                                                className="table-link",
                                                                            ),
                                                                        ],
                                                                        className="card-heading-row",
                                                                    ),
                                                                    dash_table.DataTable(
                                                                        id="monitor-table",
                                                                        columns=[
                                                                            {"name": "Station", "id": "station"},
                                                                            {"name": "Region", "id": "region"},
                                                                            {"name": "Hour", "id": "hour"},
                                                                            {"name": "Source", "id": "source"},
                                                                            {"name": "PM2.5", "id": "pm25"},
                                                                            {"name": "PM10", "id": "pm10"},
                                                                            {"name": "O3", "id": "o3"},
                                                                            {"name": "Category", "id": "category"},
                                                                        ],
                                                                        data=[],
                                                                        page_size=8,
                                                                        style_table={"overflowX": "auto"},
                                                                        style_cell={
                                                                            "fontFamily": BASE_FONT_FAMILY,
                                                                            "fontSize": "0.92rem",
                                                                            "padding": "8px 10px",
                                                                            "whiteSpace": "nowrap",
                                                                            "border": "none",
                                                                        },
                                                                        style_header={
                                                                            "fontWeight": "900",
                                                                            "backgroundColor": "#f1f5f9",
                                                                            "borderBottom": "1px solid #e2e8f0",
                                                                            "color": "#0f172a",
                                                                        },
                                                                        style_cell_conditional=[
                                                                            {"if": {"column_id": "category"}, "textAlign": "center", "fontWeight": "900"},
                                                                            {"if": {"column_id": "pm25"}, "textAlign": "right"},
                                                                            {"if": {"column_id": "pm10"}, "textAlign": "right"},
                                                                            {"if": {"column_id": "o3"}, "textAlign": "right"},
                                                                        ],
                                                                        style_data={"borderBottom": "1px solid #eef2f7"},
                                                                        style_data_conditional=[],
                                                                    ),
                                                                ],
                                                                className="ranking-card monitor-table-card",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.Div(
                                                                                [
                                                                                    html.H3("All stations", className="modal-title"),
                                                                                    html.Button(
                                                                                        "Close",
                                                                                        id="monitor-modal-close",
                                                                                        n_clicks=0,
                                                                                        className="header-action header-action--ghost modal-close",
                                                                                    ),
                                                                                ],
                                                                                className="modal-header",
                                                                            ),
                                                                    dash_table.DataTable(
                                                                        id="monitor-table-all",
                                                                        columns=[
                                                                            {"name": "Station", "id": "station"},
                                                                            {"name": "Region", "id": "region"},
                                                                            {"name": "Hour", "id": "hour"},
                                                                            {"name": "Source", "id": "source"},
                                                                            {"name": "PM2.5", "id": "pm25"},
                                                                            {"name": "PM10", "id": "pm10"},
                                                                            {"name": "O3", "id": "o3"},
                                                                            {"name": "Category", "id": "category"},
                                                                        ],
                                                                                data=[],
                                                                                page_size=20,
                                                                                sort_action="native",
                                                                                filter_action="native",
                                                                                style_table={"overflowX": "auto"},
                                                                                style_cell={
                                                                                    "fontFamily": BASE_FONT_FAMILY,
                                                                                    "fontSize": "0.92rem",
                                                                                    "padding": "9px 10px",
                                                                                    "whiteSpace": "nowrap",
                                                                                    "border": "none",
                                                                                },
                                                                                style_header={
                                                                                    "fontWeight": "900",
                                                                                    "backgroundColor": "#f1f5f9",
                                                                                    "borderBottom": "1px solid #e2e8f0",
                                                                                    "color": "#0f172a",
                                                                                },
                                                                                style_cell_conditional=[
                                                                                    {"if": {"column_id": "category"}, "textAlign": "center", "fontWeight": "900"},
                                                                                    {"if": {"column_id": "pm25"}, "textAlign": "right"},
                                                                                    {"if": {"column_id": "pm10"}, "textAlign": "right"},
                                                                                    {"if": {"column_id": "o3"}, "textAlign": "right"},
                                                                                ],
                                                                                style_data={"borderBottom": "1px solid #eef2f7"},
                                                                                style_data_conditional=[],
                                                                            ),
                                                                        ],
                                                                        className="modal-card",
                                                                    )
                                                                ],
                                                                id="monitor-modal",
                                                                className="modal-overlay",
                                                                style={"display": "none"},
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.H4("Selected site"),
                                                                    html.Div(id="monitor-selected-site", className="selected-site"),
                                                                ],
                                                                className="control-card selected-site-card",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.H4("Observation vs PurpleAir (nearby)"),
                                                                    dcc.Graph(
                                                                        id="monitor-compare-chart",
                                                                        figure=go.Figure(),
                                                                        config={"displayModeBar": False},
                                                                    ),
                                                                    html.Div(id="monitor-compare-summary", className="monitor-compare-summary"),
                                                                ],
                                                                className="control-card monitor-compare-card",
                                                            ),
                                                        ],
                                                        className="monitor-main__side",
                                                    ),
                                                ],
                                                className="monitor-main",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.H4("Observation trend – selected site"),
                                                            dcc.Graph(
                                                                id="monitor-trend-chart",
                                                                figure=go.Figure(),
                                                                config={"displayModeBar": False},
                                                            ),
                                                            html.Div(
                                                                "AQI based on PM2.5 where available. Data shown is preliminary and subject to change. All times shown in AEST.",
                                                                className="monitor-note",
                                                            ),
                                                        ],
                                                        className="map-card monitor-trend-card",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.H4("Network status"),
                                                            html.Div(id="monitor-network-status", className="network-status"),
                                                        ],
                                                        className="control-card monitor-network-card",
                                                    ),
                                                ],
                                                className="monitor-bottom",
                                            ),
                                            html.Div(INITIAL_MONITOR_STATUS, id="monitor-status", className="monitor-status"),
                                        ],
                                        className="monitor-panel",
                                    )
                                ],
                                className="dashboard-tab-panel",
                            ),
                        ),
                        dcc.Tab(
                            label="Nowcasting",
                            value="forecast",
                            className="dashboard-tab dashboard-tab--forecast",
                            selected_className="dashboard-tab--selected",
                            children=html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.H4("Nowcasting"),
                                                            html.Div(id="selection-message", className="selection-message"),
                                                        ],
                                                        className="forecast-toolbar__heading",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.Label("Run date"),
                                                                    dcc.Dropdown(
                                                                        id="date-dropdown",
                                                                        options=[{"label": value, "value": value} for value in OPTIONS.get("date", [])],
                                                                        value=DEFAULT_SELECTION["date"],
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="filter-field",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Model"),
                                                                    dcc.Dropdown(
                                                                        id="model-dropdown",
                                                                        options=[{"label": value, "value": value} for value in OPTIONS.get("models", [])],
                                                                        value=DEFAULT_SELECTION["models"],
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="filter-field filter-field--wide",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Pollutant"),
                                                                    dcc.Dropdown(
                                                                        id="pollutant-dropdown",
                                                                        options=[{"label": value, "value": value} for value in OPTIONS.get("pollutants", [])],
                                                                        value=DEFAULT_SELECTION["pollutants"],
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="filter-field filter-field--pollutant",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Forecast Hour"),
                                                                    dcc.Dropdown(
                                                                        id="time-dropdown",
                                                                        options=[{"label": f"{value} hours", "value": value} for value in OPTIONS.get("timeScopes", [])],
                                                                        value=DEFAULT_SELECTION["timeScopes"],
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="filter-field filter-field--horizon",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Region"),
                                                                    dcc.Dropdown(
                                                                        id="region-dropdown",
                                                                        options=[{"label": _display_region_label(value), "value": value} for value in OPTIONS.get("regions", [])],
                                                                        value=DEFAULT_SELECTION["regions"],
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="filter-field",
                                                            ),
                                                        ],
                                                        className="forecast-toolbar__filters",
                                                    ),
                                                ],
                                                className="control-card forecast-toolbar",
	                                            ),
	                                            html.Div(
	                                                [
	                                                    html.Div(INITIAL_FORECAST_SUMMARY_CARDS, id="summary-cards", className="metric-row forecast-summary-row"),
	                                                ],
	                                                className="forecast-summary-wrap",
	                                            ),
	                                            html.Div(
	                                                [
		                                                    html.Div(
		                                                        [
		                                                            html.H4("Nowcasting Map"),
                                                                    html.Div(
                                                                        [
                                                                            html.Div(
                                                                                [
                                                                                    html.Label("Pollutant"),
                                                                                    dcc.Dropdown(
                                                                                        id="forecast-map-pollutant-dropdown",
                                                                                        options=[{"label": value, "value": value} for value in OPTIONS.get("pollutants", [])],
                                                                                        value=DEFAULT_SELECTION["pollutants"],
                                                                                        clearable=False,
                                                                                    ),
                                                                                ],
                                                                                className="forecast-map-pollutant-control",
                                                                            ),
                                                                            html.Div(
                                                                                [
                                                                                    html.Button("‹", id="forecast-prev-hour", n_clicks=0, className="nsw-aq-status__nav-btn", title="Previous forecast hour"),
                                                                                    html.Div(INITIAL_FORECAST_TIME_LABEL if INITIAL_FORECAST_PARSED else "No forecast", id="forecast-time-label", className="forecast-map-heading__time"),
                                                                                    html.Button("›", id="forecast-next-hour", n_clicks=0, className="nsw-aq-status__nav-btn", title="Next forecast hour"),
                                                                                ],
                                                                                className="forecast-hour-stepper",
                                                                            ),
                                                                        ],
                                                                        className="forecast-map-heading__right",
                                                                    ),
	                                                        ],
	                                                        className="forecast-map-heading",
	                                                    ),
                                                    html.Iframe(
                                                        id="forecast-map",
                                                        srcDoc=INITIAL_FORECAST_MAP_HTML,
                                                        className="map-frame map-frame--monitor map-frame--monitor-large map-frame--forecast",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Div("Forecast step"),
                                                            dcc.Slider(id="time-slider", min=0, max=max(len(INITIAL_FORECAST_TIMES) - 1, 0), value=INITIAL_FORECAST_TIME_INDEX, step=1, marks=_slider_marks(INITIAL_FORECAST_TIMES)),
                                                        ],
	                                                        className="time-control forecast-hidden-controls",
	                                                    ),
	                                                    html.Div(INITIAL_FORECAST_FILE_INFO, id="file-info", className="file-info forecast-hidden-controls"),
                                                ],
                                                className="map-card",
                                            ),
	                                            html.Div(
	                                                [
	                                                    html.Div(
	                                                        [
	                                                            html.H4(
	                                                                f"Station's Nowcasting @ {INITIAL_FORECAST_TIME_LABEL}" if INITIAL_FORECAST_PARSED else "Station's Nowcasting",
	                                                                id="station-ranking-title",
	                                                            ),
						                                            html.Div(
						                                                INITIAL_FORECAST_RANKING_FIGURE,
						                                                id="ranking-chart",
					                                                    className="station-rank-bars-shell",
					                                                ),
				                                                html.Button(
				                                                    "All Stations Nowcasting",
				                                                    id="forecast-all-stations-open",
				                                                    n_clicks=0,
				                                                    className="forecast-all-stations-link",
				                                                ),
				                                                html.Hr(),
				                                                html.H4("Selected station"),
				                                                html.Div(INITIAL_STATION_DETAILS, id="station-details", style={"marginBottom": "10px"}),
				                                                dcc.Graph(id="station-trend", figure=INITIAL_STATION_TREND, config={"displayModeBar": False}),
	                                                        ],
	                                                        className="ranking-card",
	                                                    ),
	                                                    html.A(
	                                                        "NSW Air Quality Categories (AQC)",
	                                                        href="https://www.airquality.nsw.gov.au/health-advice/air-quality-categories",
	                                                        target="_blank",
	                                                        rel="noopener noreferrer",
	                                                        className="aqc-link-box",
	                                                    ),
	                                                ],
	                                                className="forecast-side-stack",
	                                            ),
	                                            html.Div(
	                                                [
	                                                    html.Div(
	                                                        [
	                                                            html.Div(
	                                                                [
	                                                                    html.H4(
	                                                                        f"Nowcasting Time Series @ {_display_region_label(DEFAULT_SELECTION.get('regions'))}",
	                                                                        id="forecast-time-series-title",
	                                                                    ),
	                                                                    html.P("Observed monitoring and forecast mean for the selected station."),
	                                                                ],
	                                                                className="forecast-series-heading",
	                                                            ),
	                                                            html.Div(
	                                                                [
	                                                                    html.Label("Station"),
	                                                                    dcc.Dropdown(
	                                                                        id="forecast-time-series-station",
	                                                                        options=_forecast_station_dropdown_options(INITIAL_FORECAST_PARSED),
	                                                                        value=None,
	                                                                        clearable=False,
	                                                                    ),
	                                                                ],
	                                                                className="forecast-series-filter",
	                                                            ),
	                                                        ],
	                                                        className="forecast-series-header forecast-series-header--station",
	                                                    ),
	                                                    dcc.Graph(
	                                                        id="forecast-time-series",
	                                                        figure=INITIAL_TIME_SERIES,
                                                        config={"displayModeBar": False},
                                                    )
                                                ],
                                                className="time-series-card",
                                            ),
                                        ],
                                        className="dash-grid",
                                    )
                                ],
                                className="dashboard-tab-panel",
                            ),
                        ),
                        dcc.Tab(
                            label="Validation",
                            value="model-performance",
                            className="dashboard-tab dashboard-tab--performance",
                            selected_className="dashboard-tab--selected",
                            children=html.Div(
                                [
                                    html.Section(
                                        [
                                            html.Div(
                                                [
                                                    html.H2("Validation"),
                                                    html.P("Validation metrics and skill summaries for each model version."),
                                                ],
                                                className="placeholder__heading",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        "This tab is a placeholder in the Python dashboard. Hook it up to your evaluation outputs when ready.",
                                                        className="placeholder__body",
                                                    )
                                                ],
                                                className="placeholder-panel",
                                            ),
                                        ],
                                        className="placeholder-wrap",
                                    )
                                ],
                                className="dashboard-tab-panel",
                            ),
                        ),
                        dcc.Tab(
                            label="AQ Standard",
                            value="aq-standard",
                            className="dashboard-tab dashboard-tab--standard",
                            selected_className="dashboard-tab--selected",
                            children=html.Div(
                                [
                                    _activity_guide_panel(),
                                ],
                                className="dashboard-tab-panel",
                            ),
                        ),
                    ],
                ),
            ],
            id="dashboard-page",
        ),
	        html.Div(
	            id="overview-allstations-overlay",
            children=[
                html.Div(
                    [
                        html.Div(
                            [
                                html.Button("Close", id="allstations-close", n_clicks=0, className="overview-next2-allstations__summary"),
                            ],
                            className="card-heading-row",
                        ),
                        html.Div([
                            html.H4("All stations forecast (next 3 hours)"),
                        ], className="card-heading-row"),
                        html.Div(
                            [
                                dash_table.DataTable(
                                    id="overview-next2-allstations-table",
                                    columns=[
                                        {"name": "Station", "id": "station"},
                                        {"name": "Region", "id": "region"},
                                        {"name": ["+1h", "PM₂.₅"], "id": "h1_pm25"},
                                        {"name": ["+1h", "PM10"], "id": "h1_pm10"},
                                        {"name": ["+1h", "O3"], "id": "h1_o3"},
                                        {"name": ["+3h", "PM₂.₅"], "id": "h3_pm25"},
                                        {"name": ["+3h", "PM10"], "id": "h3_pm10"},
                                        {"name": ["+3h", "O3"], "id": "h3_o3"},
                                        {"name": ["+6h", "PM₂.₅"], "id": "h6_pm25"},
                                        {"name": ["+6h", "PM10"], "id": "h6_pm10"},
                                        {"name": ["+6h", "O3"], "id": "h6_o3"},
                                        {"name": ["+12h", "PM₂.₅"], "id": "h12_pm25"},
                                        {"name": ["+12h", "PM10"], "id": "h12_pm10"},
                                        {"name": ["+12h", "O3"], "id": "h12_o3"},
                                    ],
                                    data=[],
                                    page_action="none",
                                    sort_action="native",
                                    merge_duplicate_headers=True,
                                    style_table={"overflowX": "auto"},
                                    style_cell={
                                        "fontFamily": BASE_FONT_FAMILY,
                                        "fontSize": "0.86rem",
                                        "padding": "6px 8px",
                                        "whiteSpace": "nowrap",
                                        "border": "none",
                                        "textAlign": "center",
                                    },
                                    style_header={
                                        "fontWeight": "900",
                                        "backgroundColor": "#f1f5f9",
                                        "borderBottom": "1px solid #e2e8f0",
                                        "color": "#0f172a",
                                    },
                                    style_cell_conditional=[
                                        {"if": {"column_id": "station"}, "textAlign": "left"},
                                        {"if": {"column_id": "region"}, "textAlign": "left"},
                                        {"if": {"column_type": "text"}, "textAlign": "left"},
                                    ],
                                    style_data_conditional=_overview_allstations_value_styles(),
                                    
                                )
                            ],
                            style={"paddingTop": "8px"},
                        ),
                    ],
                    className="overlay-inner",
                    style={
                        "maxWidth": "1100px",
                        "margin": "40px auto",
                        "background": "#fff",
                        "borderRadius": "8px",
                        "padding": "18px",
                    },
                )
            ],
            style={
                "display": "none",
                "position": "fixed",
                "top": "0",
                "left": "0",
                "right": "0",
                "bottom": "0",
                "background": "rgba(0,0,0,0.45)",
                "zIndex": "1100",
                "overflow": "auto",
                "padding": "20px",
	            },
	        ),
	        html.Div(
	            id="forecast-all-stations-modal",
	            className="modal-overlay",
	            style={"display": "none"},
	            children=[
	                html.Div(
	                    [
	                        html.Div(
	                            [
	                                html.Div(
	                                    [
	                                        html.H3("All Stations Nowcasting", id="forecast-all-stations-title", className="modal-title"),
	                                        html.Div(id="forecast-all-stations-subtitle", className="forecast-all-stations-subtitle"),
	                                    ],
	                                    className="forecast-all-stations-heading",
	                                ),
	                                html.Button("Close", id="forecast-all-stations-close", n_clicks=0, className="header-action header-action--ghost modal-close"),
	                            ],
	                            className="modal-header",
	                        ),
	                        html.Div(
	                            id="forecast-all-stations-table",
	                            className="all-fc-outer",
	                        ),
	                    ],
	                    className="modal-card forecast-all-stations-modal-card",
	                )
	            ],
	        ),
	        html.Div(
	            f"Source data is loaded from forecast CSVs under {CSV_DATA_FILE_PATH}. If you point CSV_DATA_FILE_PATH at another folder, restart the app.",
            className="dash-footer",
        ),
    ],
    className="dash-shell",
)


@app.callback(
    Output("overview-allstations-overlay", "style"),
    [Input("overview-open-allstations", "n_clicks"), Input("allstations-close", "n_clicks")],
    [State("overview-allstations-overlay", "style")],
    prevent_initial_call=True,
)
def toggle_allstations_overlay(open_clicks, close_clicks, current_style):
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if trigger == "overview-open-allstations":
        return {
            "display": "block",
            "position": "fixed",
            "top": "0",
            "left": "0",
            "right": "0",
            "bottom": "0",
            "background": "rgba(0,0,0,0.45)",
            "zIndex": "1100",
            "overflow": "auto",
            "padding": "20px",
        }
    if trigger == "allstations-close":
        return {"display": "none"}
    return current_style or {"display": "none"}


@app.callback(
    Output("forecast-all-stations-modal", "style"),
    [Input("forecast-all-stations-open", "n_clicks"), Input("forecast-all-stations-close", "n_clicks")],
    [State("forecast-all-stations-modal", "style")],
    prevent_initial_call=True,
)
def toggle_forecast_allstations_modal(open_clicks, close_clicks, current_style):
    current_style = current_style or {}
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if trigger == "forecast-all-stations-close":
        return {**current_style, "display": "none"}
    if trigger == "forecast-all-stations-open" and open_clicks:
        return {**current_style, "display": "flex"}
    return current_style


@app.callback(
    [
        Output("forecast-all-stations-table", "children"),
        Output("forecast-all-stations-title", "children"),
        Output("forecast-all-stations-subtitle", "children"),
    ],
    [
        Input("forecast-store", "data"),
        Input("pollutant-dropdown", "value"),
        Input("forecast-all-stations-open", "n_clicks"),
    ],
    [
        State("region-dropdown", "value"),
        State("time-dropdown", "value"),
        State("model-dropdown", "value"),
        State("date-dropdown", "value"),
    ],
)
def render_forecast_allstations_table(parsed, pollutant, _open_clicks,
                                      region, time_scope, model, date):
    selection = {
        "regions":     region      or DEFAULT_SELECTION.get("regions"),
        "pollutants":  pollutant   or DEFAULT_SELECTION.get("pollutants"),
        "timeScopes":  time_scope  or DEFAULT_SELECTION.get("timeScopes"),
        "models":      model       or DEFAULT_SELECTION.get("models"),
        "date":        date        or DEFAULT_SELECTION.get("date"),
    }
    # _forecast_all_hour_table loads via _merged_forecast_map_payload — works without forecast-store.
    return _forecast_all_hour_table(parsed, pollutant, selection)
    return _forecast_all_hour_table(parsed, pollutant, selection)


@app.callback(
    Output("forecast-playback", "disabled"),
    [Input("dashboard-tabs", "value")],
)
def toggle_forecast_playback(active_tab):
    return active_tab != "forecast"


@app.callback(
    Output("monitor-region", "options"),
    [Input("dashboard-tabs", "value")],
)
def load_monitor_region_options(active_tab):
    # Avoid parsing the large site metadata file during initial startup.
    # Populate region options the first time the Monitoring tab is opened.
    if active_tab != "monitor":
        return no_update
    regions = sorted({(site or {}).get("Region") for site in _sites_by_id().values() if (site or {}).get("Region")})
    return [{"label": "All regions", "value": "ALL"}] + [{"label": region, "value": region} for region in regions]


@app.callback(
    [
        Output("date-dropdown", "options"),
        Output("model-dropdown", "options"),
        Output("pollutant-dropdown", "options"),
        Output("time-dropdown", "options"),
        Output("region-dropdown", "options"),
        Output("date-dropdown", "value"),
        Output("model-dropdown", "value"),
        Output("pollutant-dropdown", "value"),
        Output("time-dropdown", "value"),
        Output("region-dropdown", "value"),
        Output("forecast-catalog-store", "data"),
    ],
    [Input("forecast-catalog-refresh", "n_intervals")],
    [
        State("date-dropdown", "value"),
        State("model-dropdown", "value"),
        State("pollutant-dropdown", "value"),
        State("time-dropdown", "value"),
        State("region-dropdown", "value"),
        State("forecast-catalog-store", "data"),
    ],
)
def refresh_forecast_catalog(_tick, date, model, pollutant, time_scope, region, catalog_state):
    options = get_options() or {}
    latest = _latest_file_params() or {}
    previous_latest = (catalog_state or {}).get("latestDate")
    latest_date = latest.get("date")

    dates = options.get("date") or []
    models = options.get("models") or []
    pollutants = options.get("pollutants") or []
    time_scopes = options.get("timeScopes") or []
    regions = options.get("regions") or []

    # Follow a newly arriving run only when the viewer was following the
    # previous latest date. Preserve an intentional historical selection.
    follow_latest = not date or date not in dates or (previous_latest and date == previous_latest and latest_date != previous_latest)
    if follow_latest:
        date = latest.get("date") or (dates[-1] if dates else None)
        model = latest.get("models") or model
        pollutant = latest.get("pollutants") or pollutant
        time_scope = latest.get("timeScopes") or time_scope
        region = latest.get("regions") or region

    model = model if model in models else (latest.get("models") or (models[0] if models else None))
    pollutant = pollutant if pollutant in pollutants else (latest.get("pollutants") or (pollutants[0] if pollutants else None))
    time_scope = time_scope if time_scope in time_scopes else (latest.get("timeScopes") or (time_scopes[0] if time_scopes else None))
    region = region if region in regions else (latest.get("regions") or (regions[0] if regions else None))

    return (
        [{"label": value, "value": value} for value in dates],
        [{"label": value, "value": value} for value in models],
        [{"label": value, "value": value} for value in pollutants],
        [{"label": f"{value} hours", "value": value} for value in time_scopes],
        [{"label": _display_region_label(value), "value": value} for value in regions],
        date,
        model,
        pollutant,
        time_scope,
        region,
        {"latestDate": latest_date},
    )


app.clientside_callback(
    """
    function(hourIndex) {
        var frame = document.getElementById("forecast-map");
        if (frame && frame.contentWindow) {
            frame.contentWindow.postMessage({ type: "forecast-hour-update", hourIndex: hourIndex }, "*");
        }
        return "";
    }
    """,
    Output("forecast-map-updater", "children"),
    [Input("time-slider", "value")],
)


@app.callback(
    [
        Output("forecast-store", "data"),
        Output("selection-message", "children"),
        Output("time-slider", "max"),
        Output("time-slider", "value"),
        Output("time-slider", "marks"),
    ],
    [
        Input("region-dropdown", "value"),
        Input("pollutant-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("forecast-catalog-refresh", "n_intervals"),
        Input("forecast-playback", "n_intervals"),
        Input("overview-forecast-load", "n_intervals"),
        Input("forecast-prev-hour", "n_clicks"),
        Input("forecast-next-hour", "n_clicks"),
    ],
    [State("time-slider", "value"), State("forecast-store", "data"), State("dashboard-tabs", "value")],
    prevent_initial_call=True,
)
def load_forecast(region, pollutant, time_scope, model, date, _catalog_tick, _n_intervals, _overview_load, _prev_hour, _next_hour, current_time_value, current_store, active_tab):
    # Only process when on forecast tab
    if active_tab != "forecast":
        return no_update, no_update, no_update, no_update, no_update

    selection = {
        "regions": region,
        "pollutants": pollutant,
        "timeScopes": time_scope,
        "models": model,
        "date": date,
    }
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    return _load_forecast_payload("forecast", selection, trigger, current_time_value, current_store)


def _load_forecast_payload(active_tab, selection, trigger, current_time_value, current_store):
    # Keep the large forecast payload out of the browser unless the Forecast tab is open.
    # The Overview tab renders its forecast panels by reading CSVs server-side to avoid
    # Dash "Uploading..." delays caused by shipping big dcc.Store payloads back to the server.
    if active_tab != "forecast":
        return no_update, no_update, no_update, no_update, no_update

    current_file = _build_file_path(selection)
    if not current_file:
        current_file, _exact = _best_matching_file(selection)
    try:
        current_file_epoch = os.path.getmtime(current_file) if current_file else None
    except OSError:
        current_file_epoch = None

    store_matches_file = (
        current_store
        and current_store.get("_selection") == selection
        and current_store.get("_sourceFile") == current_file
        and current_store.get("_generatedAtEpoch") == current_file_epoch
    )
    if store_matches_file:
        times = current_store.get("data", {}).get("time", {}).get("forecastTime", [])
        slider_max = max(len(times) - 1, 0)
        base_index = _forecast_live_base_index(times)
        if trigger == "forecast-playback":
            slider_value = current_time_value if current_time_value is not None else base_index
            slider_value += 1
            if slider_value > slider_max:
                slider_value = base_index
        elif trigger == "forecast-next-hour":
            slider_value = min(slider_max, (current_time_value if current_time_value is not None else base_index) + 1)
        elif trigger == "forecast-prev-hour":
            slider_value = max(0, (current_time_value if current_time_value is not None else base_index) - 1)
        else:
            slider_value = base_index
        return current_store, "", slider_max, slider_value, _slider_marks(times)

    parsed, message = _load_forecast(selection)
    if not parsed:
        return None, message, 0, 0, {0: "--"}

    parsed["_selection"] = selection
    parsed["_message"] = message

    times = parsed.get("data", {}).get("time", {}).get("forecastTime", [])
    slider_max = max(len(times) - 1, 0)
    base_index = _forecast_live_base_index(times)
    if trigger == "forecast-playback":
        current_value = current_time_value if current_time_value is not None else base_index
        slider_value = current_value + 1
        if slider_value > slider_max:
            slider_value = base_index
    elif trigger == "forecast-next-hour":
        slider_value = min(slider_max, (current_time_value if current_time_value is not None else base_index) + 1)
    elif trigger == "forecast-prev-hour":
        slider_value = max(0, (current_time_value if current_time_value is not None else base_index) - 1)
    else:
        slider_value = base_index

    return parsed, "", slider_max, slider_value, _slider_marks(times)


@app.callback(
    Output("monitor-refresh", "disabled"),
    [Input("dashboard-tabs", "value")],
)
def toggle_monitor_refresh(active_tab):
    # Only refresh monitoring feeds when the Monitoring tab is open.
    return active_tab != "monitor"


@app.callback(
    Output("monitor-store", "data"),
    [
        Input("monitor-refresh", "n_intervals"),
        Input("monitor-settings-store", "data"),
        Input("overview-monitor-load", "n_intervals"),
    ],
    [State("monitor-store", "data"), State("dashboard-tabs", "value")],
    prevent_initial_call=True,
)
def refresh_monitoring(_n_intervals, monitor_settings, _overview_load, current_store, active_tab):
    # Refresh rules:
    # - Monitoring tab: refresh on its normal interval / setting changes.
    # - Overview tab: run a single AQMS-only snapshot shortly after first paint
    #   (driven by `overview-monitor-load`).
    on_monitor = active_tab == "monitor"
    on_overview_snapshot = active_tab == "overview" and (_overview_load is not None)
    if not on_monitor and not on_overview_snapshot:
        return no_update
    monitor_settings = monitor_settings or {}
    pollutant_label = (monitor_settings.get("pollutant") or "PM2.5").strip()
    source_filter = (monitor_settings.get("source") or "both").strip().lower()

    # module-level snapshot accessed via globals() to avoid local/global parsing issues

    # Overview snapshot: keep AQMS rows for status cards, but load both sensor
    # layers so the observation map can show AQMS, DustWatch, and PurpleAir.
    if on_overview_snapshot and not on_monitor:
        pollutant_label = "PM2.5"
        source_filter = "both"

    pollutant_meta = POLLUTANTS.get(pollutant_label) or {}
    parameter_code = pollutant_meta.get("ParameterCode") or pollutant_label

    status_bits = []
    error = None
    try:
        aqms_result = fetch_observations_result(query=None, timeout=12)
        raw_obs = aqms_result.get("rows") or []
        aqms_source = aqms_result.get("source") or "error"
        aqms_ok = aqms_source in {"live", "official-report"} and bool(raw_obs)
        if aqms_ok:
            status_bits.append(
                "AQMS feed: Live"
                if aqms_source == "live"
                else "AQMS feed: Live (official NSW report)"
            )
        elif raw_obs:
            status_bits.append(f"AQMS feed: {aqms_source.title()}")
        else:
            status_bits.append("AQMS feed: Offline")
        if aqms_result.get("error"):
            error = f"AQMS feed unavailable: {aqms_result['error']}"
    except Exception as exc:
        aqms_result = {"rows": [], "source": "error", "error": str(exc), "latest_epoch": None}
        raw_obs = []
        aqms_ok = False
        error = f"AQMS feed unavailable: {exc}"
        status_bits.append("AQMS feed: Offline")

    purpleair_bounds = NSW_MAP_BOUNDS
    purpleair_payload = {"sensors": [], "fetched_at": None, "error": None}
    if source_filter in {"both", "purpleair"} and pollutant_label in {"PM2.5", "PM10"}:
        purpleair_payload = fetch_purpleair_snapshot(bounds=purpleair_bounds, timeout=12)
        if purpleair_payload.get("error"):
            status_bits.append("PurpleAir feed: Offline")
            # PurpleAir is an optional overlay. Do not replace a healthy AQMS
            # Overview status with a PurpleAir billing/network error.
            if error is None and source_filter == "purpleair":
                error = f"PurpleAir feed unavailable: {purpleair_payload.get('error')}"
        elif purpleair_payload.get("source") != "live":
            status_bits.append(f"PurpleAir feed: {(purpleair_payload.get('source') or 'cached').title()}")
        else:
            status_bits.append("PurpleAir feed: Live")
    else:
        status_bits.append("PurpleAir feed: Filtered")

    aqms_rows = _aqms_rows_for_pollutant(raw_obs, parameter_code, pollutant_label)
    # Keep pollutant snapshots available for Overview (region summary box), independent of current selection.
    aqms_pm25_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["PM2.5"]["ParameterCode"], "PM2.5")
    aqms_pm10_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["PM10"]["ParameterCode"], "PM10")
    aqms_o3_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["O3"]["ParameterCode"], "O3")
    aqms_no2_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["NO2"]["ParameterCode"], "NO2")
    aqms_so2_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["SO2"]["ParameterCode"], "SO2")
    aqms_co_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["CO"]["ParameterCode"], "CO")
    dustwatch_pm25_rows = _aqms_rows_for_pollutant(raw_obs, "PM2.5d", "PM2.5", source_label="DustWatch")
    dustwatch_pm10_rows = _aqms_rows_for_pollutant(raw_obs, "PM10d", "PM10", source_label="DustWatch")
    # Always compute site-level AQC (Air Quality Category) rows for the Overview tab.
    aqc_rows = _aqms_rows_for_pollutant(raw_obs, "AQC", "AQC")

    # Meteorology-capable AQMS sites (from latest snapshot, used to keep history calls small).
    met_site_ids_by_region = {}
    try:
        wanted_met = {"TEMP", "HUMID", "WSP", "RAIN"}
        for item in raw_obs or []:
            param = item.get("Parameter") or {}
            code = str(param.get("ParameterCode") or "").strip()
            if code not in wanted_met:
                continue
            value = item.get("Value")
            if value is None:
                continue
            site_id = item.get("Site_Id")
            if site_id is None:
                continue
            site = _sites_by_id().get(int(site_id), {})
            region = str(site.get("Region") or "").strip() or "Unknown"
            met_site_ids_by_region.setdefault(region, set()).add(int(site_id))
            met_site_ids_by_region.setdefault("All NSW", set()).add(int(site_id))
    except Exception:
        met_site_ids_by_region = {}

    purpleair_sensors = _purpleair_rows_for_pollutant(purpleair_payload.get("sensors") or [], pollutant_label)
    clusters = purpleair_clusters(purpleair_payload.get("sensors") or [], pollutant_label=pollutant_label) if purpleair_sensors else []

    map_rows = []
    if source_filter in {"both", "aqms"}:
        map_rows.extend(aqms_rows)
        if pollutant_label == "PM2.5":
            map_rows.extend(dustwatch_pm25_rows)
        elif pollutant_label == "PM10":
            map_rows.extend(dustwatch_pm10_rows)
    if source_filter in {"both", "purpleair"}:
        map_rows.extend(purpleair_sensors or load_purpleair_sensors())

    # Convenience: snapshot per-site for station detail panels.
    aqms_snapshot_by_site = {}
    for key, rows in {
        "PM2.5": aqms_pm25_rows,
        "PM10": aqms_pm10_rows,
        "O3": aqms_o3_rows,
        "NO2": aqms_no2_rows,
        "SO2": aqms_so2_rows,
        "CO": aqms_co_rows,
        "AQC": aqc_rows,
    }.items():
        for row in rows or []:
            site_id = row.get("site_id")
            if site_id is None:
                continue
            bucket = aqms_snapshot_by_site.setdefault(int(site_id), {})
            bucket[key] = row

    # If we don't yet have an in-process previous snapshot, try to seed it
    # from the browser/store-provided `current_store` so arrows can appear
    # on the first server-side refresh after a restart.
    try:
        prev_snap = globals().get("_LAST_AQMS_SNAPSHOT") or {}
        if (not (prev_snap or {}).get("sites")) and current_store:
            seed = {}
            prev_store_sites = current_store.get("aqmsSnapshotBySite") or {}
            for sid, bucket in (prev_store_sites or {}).items():
                try:
                    sid_int = int(sid)
                except Exception:
                    sid_int = sid
                pm25_val = None
                pm10_val = None
                o3_val = None
                try:
                    pm25_val = float((bucket.get("PM2.5") or {}).get("value")) if bucket and bucket.get("PM2.5") and bucket.get("PM2.5").get("value") is not None else None
                except Exception:
                    pm25_val = None
                try:
                    pm10_val = float((bucket.get("PM10") or {}).get("value")) if bucket and bucket.get("PM10") and bucket.get("PM10").get("value") is not None else None
                except Exception:
                    pm10_val = None
                try:
                    o3_val = float((bucket.get("O3") or {}).get("value")) if bucket and bucket.get("O3") and bucket.get("O3").get("value") is not None else None
                except Exception:
                    o3_val = None
                seed[sid_int] = {"PM2.5": pm25_val, "PM10": pm10_val, "O3": o3_val}
            if seed:
                globals()['_LAST_AQMS_SNAPSHOT'] = {"ts": None, "sites": seed}
    except Exception:
        pass

    latest_label = _format_monitor_label(_monitoring_time_row(aqms_rows)) if aqms_rows else "--"
    completeness = _data_completeness(aqms_rows, purpleair_sensors if source_filter in {"both", "purpleair"} else [])
    kpis = _monitor_kpi_payload(aqms_rows, purpleair_sensors, clusters, latest_label, completeness)
    table_rows = _monitor_table_rows(aqms_rows, purpleair_sensors, aqms_snapshot_by_site, max_rows=40)
    table_rows_all = _monitor_table_rows_all(aqms_rows, purpleair_sensors, aqms_snapshot_by_site)
    feeds = {
        "aqms": {"status": "Live" if aqms_ok else (aqms_result.get("source") or "Offline").title()},
        "purpleair": {
            "status": "Live"
            if not purpleair_payload.get("error") and purpleair_payload.get("source") == "live"
            else (purpleair_payload.get("source") or "Offline").title()
        },
        "completeness": completeness,
    }

    status_text = ". ".join([bit for bit in status_bits if bit])
    if latest_label and latest_label != "--":
        status_text = f"{status_text}. Latest snapshot: {latest_label}"

    # Update module-level per-site snapshot tracker so table builders can show small delta arrows
    try:
        fetched_epoch = datetime.now(SYDNEY_TZ).timestamp()
        new_sites = {}
        for sid, bucket in (aqms_snapshot_by_site or {}).items():
            try:
                sid_int = int(sid)
            except Exception:
                sid_int = sid
            pm25_val = None
            pm10_val = None
            o3_val = None
            try:
                pm25_val = float((bucket.get("PM2.5") or {}).get("value")) if bucket and bucket.get("PM2.5") and bucket.get("PM2.5").get("value") is not None else None
            except Exception:
                pm25_val = None
            try:
                pm10_val = float((bucket.get("PM10") or {}).get("value")) if bucket and bucket.get("PM10") and bucket.get("PM10").get("value") is not None else None
            except Exception:
                pm10_val = None
            try:
                o3_val = float((bucket.get("O3") or {}).get("value")) if bucket and bucket.get("O3") and bucket.get("O3").get("value") is not None else None
            except Exception:
                o3_val = None
            new_sites[sid_int] = {"PM2.5": pm25_val, "PM10": pm10_val, "O3": o3_val}
        # Keep a copy of the previous snapshot for UI comparisons so the
        # rendering logic can compare against the snapshot from just before
        # this refresh (avoids erasing arrow markers during the same update).
        try:
            globals()['_PRIOR_AQMS_SNAPSHOT'] = globals().get('_LAST_AQMS_SNAPSHOT')
        except Exception:
            globals()['_PRIOR_AQMS_SNAPSHOT'] = None
        globals()['_LAST_AQMS_SNAPSHOT'] = {"ts": fetched_epoch, "sites": new_sites}
        try:
            _save_last_aqms_snapshot_to_disk(globals()['_LAST_AQMS_SNAPSHOT'])
        except Exception:
            pass
    except Exception:
        pass

    return {
        "pollutant": pollutant_label,
        "parameterCode": parameter_code,
        "aqmsRows": aqms_rows,
        "aqmsPm25Rows": aqms_pm25_rows,
        "aqmsPm10Rows": aqms_pm10_rows,
        "aqmsO3Rows": aqms_o3_rows,
        "aqmsNo2Rows": aqms_no2_rows,
        "aqmsSo2Rows": aqms_so2_rows,
        "aqmsCoRows": aqms_co_rows,
        "dustwatchPm25Rows": dustwatch_pm25_rows,
        "dustwatchPm10Rows": dustwatch_pm10_rows,
        "aqcRows": aqc_rows,
        "metSiteIdsByRegion": {k: sorted(list(v)) for k, v in (met_site_ids_by_region or {}).items()},
        "aqmsSnapshotBySite": aqms_snapshot_by_site,
        "purpleairSensors": purpleair_sensors,
        "purpleairClusters": clusters,
        "mapRows": map_rows,
        "kpis": kpis,
        "tableRows": table_rows,
        "tableRowsAll": table_rows_all,
        "feeds": feeds,
        "latestLabel": latest_label,
        # This field drives every "last data update" label. Use the actual
        # observation timestamp, not the time of a failed fetch attempt.
        "fetchedAtEpoch": aqms_result.get("latest_epoch") if raw_obs else None,
        "fetchAttemptEpoch": datetime.now(SYDNEY_TZ).timestamp(),
        "aqmsSource": aqms_result.get("source"),
        "status": status_text,
        "error": error,
    }


@app.callback(
    Output("monitor-settings-store", "data"),
    [
        Input("monitor-region", "value"),
        Input("monitor-station", "value"),
        Input("monitor-pollutant", "value"),
        Input("monitor-source", "value"),
        Input("monitor-window", "value"),
        Input("monitor-category-filter", "value"),
        Input("monitor-station-search", "value"),
    ],
    [State("monitor-settings-store", "data")],
)
def sync_monitor_settings(region_value, station_key, pollutant_label, source_filter, window_value, categories_value, search_value, current_settings):
    settings = dict(current_settings or {})
    if region_value is not None:
        settings["region"] = str(region_value).strip() or "ALL"
    if station_key is not None or "stationKey" in settings:
        settings["stationKey"] = station_key
    if pollutant_label:
        settings["pollutant"] = str(pollutant_label).strip()
    if source_filter:
        settings["source"] = str(source_filter).strip().lower()
    if window_value:
        settings["window"] = str(window_value).strip()
    settings["categories"] = categories_value or []
    settings["search"] = str(search_value or "")
    if "pollutant" not in settings or not settings["pollutant"]:
        settings["pollutant"] = "PM2.5"
    if "source" not in settings or not settings["source"]:
        settings["source"] = "both"
    if "region" not in settings or not settings["region"]:
        settings["region"] = "ALL"
    if "window" not in settings or not settings["window"]:
        settings["window"] = "24h"
    return settings


@app.callback(
    [Output("monitor-station", "options"), Output("monitor-station", "value")],
    [
        Input("monitor-store", "data"),
        Input("monitor-region", "value"),
        Input("monitor-source", "value"),
        Input("monitor-station-search", "value"),
    ],
    [State("monitor-station", "value")],
)
def update_monitor_station_options(monitor_store, region_value, source_value, search_value, current_station_key):
    monitor_store = monitor_store or {}
    region_value = str(region_value or "ALL").strip()
    source_value = str(source_value or "both").strip().lower()
    search_value = normalize_station_name(search_value or "")

    options = []

    def _match(text):
        if not search_value:
            return True
        return search_value in normalize_station_name(text)

    if source_value in {"both", "aqms"}:
        for site_id, site in sorted(_sites_by_id().items(), key=lambda item: (item[1].get("Region") or "", item[1].get("SiteName") or "")):
            region = str(site.get("Region") or "").strip()
            if region_value != "ALL" and region != region_value:
                continue
            station = str(site.get("SiteName") or f"Site {site_id}").strip()
            if not _match(station):
                continue
            options.append(
                {
                    "label": f"{station} ({region})" if region else station,
                    "value": f"aqms:{site_id}",
                }
            )

    if source_value in {"both", "purpleair"}:
        for row in load_purpleair_sensors():
            station = str(row.get("station") or "").strip()
            if not station:
                continue
            if not _match(station):
                continue
            site_id = row.get("site_id")
            if site_id is None:
                continue
            options.append({"label": f"{station} (PurpleAir)", "value": f"purpleair:{site_id}"})

    # Keep current value if still in options; otherwise clear.
    valid_values = {opt["value"] for opt in options if opt.get("value")}
    next_value = current_station_key if current_station_key in valid_values else None
    return options, next_value


@app.callback(
    [
        Output("monitor-map", "srcDoc"),
        Output("monitor-kpi-row", "children"),
        Output("monitor-time-label", "children"),
        Output("monitor-table", "data"),
        Output("monitor-table", "style_data_conditional"),
        Output("monitor-network-status", "children"),
        Output("monitor-status", "children"),
        Output("monitor-table-all", "data"),
        Output("monitor-table-all", "style_data_conditional"),
    ],
    [Input("monitor-store", "data"), Input("monitor-settings-store", "data")],
)
def render_monitoring(monitor_store, monitor_settings):
    monitor_store = monitor_store or {}
    monitor_settings = monitor_settings or {}
    map_rows = monitor_store.get("mapRows") or []
    kpis = monitor_store.get("kpis") or {}
    table_rows = monitor_store.get("tableRows") or []
    table_rows_all = monitor_store.get("tableRowsAll") or []
    feeds = monitor_store.get("feeds") or {}
    latest_label = monitor_store.get("latestLabel") or "--"
    status = monitor_store.get("status") or "Monitoring feed is loading."
    error = monitor_store.get("error")

    region_value = str(monitor_settings.get("region") or "ALL").strip()
    categories_value = set([str(v).strip() for v in (monitor_settings.get("categories") or []) if v is not None])
    search_value = normalize_station_name(monitor_settings.get("search") or "")

    def _match_station(row):
        station = str((row or {}).get("station") or "")
        if search_value and search_value not in normalize_station_name(station):
            return False
        region = str((row or {}).get("region") or "").strip()
        if region_value != "ALL" and region and region != region_value and (row or {}).get("source") != "PurpleAir":
            return False
        if categories_value:
            category = str((row or {}).get("category") or "").strip()
            if category not in categories_value:
                return False
        return True

    # Re-attach arrows to table rows using latest snapshot info, then apply filters.
    table_rows = _attach_arrows_to_table_rows(table_rows, monitor_store.get("aqmsSnapshotBySite"))
    table_rows_all = _attach_arrows_to_table_rows(table_rows_all, monitor_store.get("aqmsSnapshotBySite"))
    map_rows = [row for row in map_rows if _match_station(row)]
    table_rows = [row for row in table_rows if _match_station(row)]
    table_rows_all = [row for row in table_rows_all if _match_station(row)]

    map_html = _leaflet_monitoring_map_html(map_rows)
    kpi_nodes = _monitor_kpi_cards(kpis)
    style_conditional = _monitor_table_styles()
    style_conditional_all = _monitor_table_styles()
    network_nodes = _monitor_network_nodes(feeds)
    return (
        map_html,
        kpi_nodes,
        latest_label,
        table_rows,
        style_conditional,
        network_nodes,
        error or status,
        table_rows_all,
        style_conditional_all,
    )


@app.callback(
    [
        Output("overview-observation-map", "srcDoc"),
        Output("overview-observation-time", "children"),
    ],
    [Input("monitor-store", "data"), Input("overview-observation-map-pollutant", "value")],
)
def render_overview_observation_map(monitor_store, pollutant_label):
    monitor_store = monitor_store or {}
    pollutant_label = (pollutant_label or "PM2.5").strip()
    rows = []
    if pollutant_label == "PM2.5":
        rows.extend(monitor_store.get("aqmsPm25Rows") or [])
        rows.extend(monitor_store.get("dustwatchPm25Rows") or [])
        rows.extend(monitor_store.get("purpleairSensors") or load_purpleair_sensors())
    elif pollutant_label == "PM10":
        rows.extend(monitor_store.get("aqmsPm10Rows") or [])
        rows.extend(monitor_store.get("dustwatchPm10Rows") or [])
        rows.extend(monitor_store.get("purpleairSensors") or load_purpleair_sensors())
    elif pollutant_label == "O3":
        rows.extend(monitor_store.get("aqmsO3Rows") or [])
    if not rows:
        rows = monitor_store.get("mapRows") or []

    # Build time label in format: "PM2.5 @ 5PM-6PM /25 Jun 2026"
    time_label = ""
    if rows:
        dated_rows = [
            row
            for row in rows
            if row.get("date") and row.get("hour") not in (None, "")
        ]
        latest_row = max(dated_rows, key=_parse_monitor_time) if dated_rows else None
        if latest_row:
            try:
                latest_date = datetime.strptime(str(latest_row["date"]), "%Y-%m-%d")
                hour_int = int(latest_row["hour"])
                current_dt = latest_date.replace(hour=hour_int)
                next_dt = current_dt + timedelta(hours=1)

                hour_str = current_dt.strftime("%I%p").lstrip("0")
                next_hour_str = next_dt.strftime("%I%p").lstrip("0")
                date_str = current_dt.strftime("%d %b %Y")

                time_label = f"{pollutant_label} @ {hour_str}-{next_hour_str} /{date_str}"
            except Exception:
                time_label = f"{pollutant_label} @ Latest"

    return _leaflet_monitoring_map_html(rows), html.Div(time_label, style={"fontSize": "1rem", "fontWeight": "600", "letterSpacing": "0.5px"})


def _overview_pick_multi_region_forecast_selection(selection):
    """Return (selection_for_panel, regions) for overview panels.

    Prefers 12-hour horizon; if the selected date only has one region, falls back to the
    most recent run date (same model + horizon) with multiple regions.
    """
    selection = dict(selection or {})
    selection["fallbackTimeScopes"] = selection.get("timeScopes")
    selection["timeScopes"] = "12"

    regions = _overview_available_forecast_regions(selection)
    if len(regions) > 1:
        return selection, regions

    model_name = selection.get("models")
    horizon_val = selection.get("timeScopes")
    date_val = selection.get("date")
    if not model_name or not horizon_val or not date_val:
        return selection, regions

    try:
        dates_sorted = sorted({kp.get("date") for kp in FILE_KEY_PARAMS if kp.get("date")})
        dates_sorted = [d for d in dates_sorted if str(d) <= str(date_val)]
    except Exception:
        dates_sorted = []

    for candidate_date in reversed(dates_sorted):
        candidate_sel = dict(selection)
        candidate_sel["date"] = candidate_date
        candidate_regions = _overview_available_forecast_regions(candidate_sel)
        if len(candidate_regions) > 1:
            return candidate_sel, candidate_regions

    return selection, regions


@app.callback(
    [
        Output("overview-next-hour-forecast-map", "srcDoc"),
        Output("overview-next-hour-forecast-time", "children"),
        Output("overview-forecast-hour-index", "data"),
    ],
    [
        Input("region-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
        Input("overview-forecast-map-pollutant", "value"),
        Input("overview-forecast-prev-hour", "n_clicks"),
        Input("overview-forecast-next-hour", "n_clicks"),
    ],
    [State("overview-forecast-hour-index", "data"), State("dashboard-tabs", "value")],
)
def render_overview_next_hour_forecast_map(
    region,
    time_scope,
    model,
    date,
    _overview_load,
    overview_pollutant,
    prev_clicks,
    next_clicks,
    current_index,
    active_tab,
):
    print(f"DEBUG: render_overview_next_hour_forecast_map called active_tab={active_tab} _overview_load={_overview_load}", flush=True)
    if active_tab != "overview":
        print("DEBUG: render_overview_next_hour_forecast_map early exit (not overview)", flush=True)
        return no_update, no_update, no_update

    selection = {
        "regions": region or DEFAULT_SELECTION.get("regions"),
        "pollutants": overview_pollutant or "PM2.5",
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model or DEFAULT_SELECTION.get("models"),
        "date": date or DEFAULT_SELECTION.get("date"),
    }
    selection_for_panel, regions = _overview_pick_multi_region_forecast_selection(selection)
    pollutant = (overview_pollutant or "PM2.5").strip() or "PM2.5"

    try:
        max_hours = int(selection_for_panel.get("timeScopes") or 12)
    except (TypeError, ValueError):
        max_hours = 12

    merged_stations = {}
    time_block_any = None
    times = []
    next_time_value = None
    for region in regions or []:
        # Prefer the exact model + pollutant file, but fall back to any matching
        # model for this pollutant/horizon/date if the exact file is missing.
        file_path = forecast_file_path(
            [
                str(region),
                str(pollutant),
                str(selection_for_panel.get("timeScopes")),
                str(selection_for_panel.get("models")),
                str(selection_for_panel.get("date")),
            ]
        )
        if not file_path:
            # Search FILE_KEY_PARAMS for any file with same pollutant, horizon and date
            for kp in FILE_KEY_PARAMS:
                try:
                    if kp.get("pollutants") != pollutant:
                        continue
                    if str(kp.get("timeScopes")) != str(selection_for_panel.get("timeScopes")):
                        continue
                    if str(kp.get("date")) != str(selection_for_panel.get("date")):
                        continue
                    # If kp region differs from our region selection, skip
                    if kp.get("regions") and str(kp.get("regions")) != str(region):
                        continue
                    candidate = forecast_file_path([kp.get("regions"), kp.get("pollutants"), kp.get("timeScopes"), kp.get("models"), kp.get("date")])
                    if candidate:
                        file_path = candidate
                        break
                except Exception:
                    continue
        if not file_path:
            continue
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            mtime = 0
        parsed = _cached_parse_csv(file_path, pollutant, max_hours, mtime)
        if not parsed:
            continue
        time_block = (parsed.get("data") or {}).get("time") or {}
        if time_block_any is None and time_block:
            time_block_any = time_block
        file_times = time_block.get("forecastTime", []) or []
        if file_times:
            # extend times only with new entries (preserve run order)
            for t in file_times:
                if t not in times:
                    times.append(t)
        if next_time_value is None and file_times:
            next_time_value = file_times[0]
        stations = (parsed.get("data") or {}).get("stations") or {}
        for station_key, payload in stations.items():
            if station_key not in merged_stations:
                merged_stations[station_key] = payload

    # Build parsed payload for map (only stations with forecast payload are included)
    merged_parsed = {"data": {"time": time_block_any or {}, "stations": merged_stations}}

    # If no times were discovered for the selected pollutant (some pollutants use
    # different internal labels), fall back to PM2.5 times so Prev/Next navigation
    # has a sensible shared timeline (map values will still come from the chosen pollutant).
    if not times and pollutant != "PM2.5":
        for region in regions or []:
            file_path = forecast_file_path(
                [
                    str(region),
                    "PM2.5",
                    str(selection_for_panel.get("timeScopes")),
                    str(selection_for_panel.get("models")),
                    str(selection_for_panel.get("date")),
                ]
            )
            if not file_path:
                continue
            try:
                mtime = os.path.getmtime(file_path)
            except OSError:
                mtime = 0
            parsed = _cached_parse_csv(file_path, "PM2.5", max_hours, mtime)
            if not parsed:
                continue
            time_block = (parsed.get("data") or {}).get("time") or {}
            if time_block_any is None and time_block:
                time_block_any = time_block
            file_times = time_block.get("forecastTime", []) or []
            if file_times:
                for t in file_times:
                    if t not in times:
                        times.append(t)
                if next_time_value is None and file_times:
                    next_time_value = file_times[0]
        merged_parsed = {"data": {"time": time_block_any or {}, "stations": merged_stations}}

    # Determine which time index to show. Use stored index and Prev/Next clicks to adjust.
    # Default index 0 (next available hour)
    try:
        current_index = int(current_index or 0)
    except Exception:
        current_index = 0

    # Determine trigger to see which control fired
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""

    live_base_index, has_upcoming_forecast = _forecast_live_base_state(times)

    # Reset to the live/upcoming forecast hour when core selection inputs change.
    if trigger in ("region-dropdown", "time-dropdown", "model-dropdown", "date-dropdown", "overview-forecast-load", "overview-forecast-map-pollutant"):
        new_index = live_base_index
    else:
        new_index = current_index

    if trigger == "overview-forecast-next-hour":
        new_index = min(len(times) - 1, new_index + 1) if times else 0
    elif trigger == "overview-forecast-prev-hour":
        new_index = max(0, new_index - 1)

    # Clamp
    if new_index < 0:
        new_index = 0
    if times and new_index >= len(times):
        new_index = max(0, len(times) - 1)

    # Create map HTML for selected index
    time_index = new_index if times else 0
    map_html = _leaflet_forecast_map_html(merged_parsed if merged_stations else {}, pollutant, time_index, include_series=False)

    # Build user-facing label in format: "PM2.5 @ <5PM-6PM>/25 Jun 2026"
    label_el = ""
    if times:
        sel_time = times[time_index]
        parsed_dt = _parse_forecast_timestamp(sel_time) if sel_time else None
        if parsed_dt:
            # Calculate next hour for time range
            next_dt = parsed_dt + timedelta(hours=1)
            hour_str = parsed_dt.strftime("%I%p").lstrip("0")
            next_hour_str = next_dt.strftime("%I%p").lstrip("0")
            date_str = parsed_dt.strftime("%d %b %Y")

            label_text = f"{pollutant} @ <{hour_str}-{next_hour_str}>/{date_str}"
            if not has_upcoming_forecast:
                label_text = f"{pollutant} @ <{hour_str}-{next_hour_str}>/{date_str}"
            label_el = html.Div(label_text, style={"fontSize": "1rem", "fontWeight": "600", "letterSpacing": "0.5px"})
    else:
        label_el = html.Div("", className="card-hint")

    return map_html, label_el, new_index


@app.callback(
    Output("overview-next-hour-station-panel", "children"),
    [
        Input("url", "hash"),
        Input("region-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
        Input("overview-forecast-map-pollutant", "value"),
    ],
    [State("dashboard-tabs", "value")],
)
def render_overview_next_hour_station_panel(url_hash, region, time_scope, model, date, _overview_load, overview_pollutant, active_tab):
    print(f"DEBUG: render_overview_next_hour_station_panel called active_tab={active_tab} _overview_load={_overview_load}", flush=True)
    if active_tab != "overview":
        print("DEBUG: render_overview_next_hour_station_panel early exit (not overview)", flush=True)
        return no_update

    params = _parse_hash_params(url_hash)
    station_key = (params.get("station") or "").strip()
    if not station_key:
        return html.Div("Click a station on the map to see station details.", className="card-hint")

    pollutant = (overview_pollutant or "PM2.5").strip() or "PM2.5"

    selection = {
        "regions": region or DEFAULT_SELECTION.get("regions"),
        "pollutants": pollutant,
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model or DEFAULT_SELECTION.get("models"),
        "date": date or DEFAULT_SELECTION.get("date"),
    }
    selection_for_panel, regions = _overview_pick_multi_region_forecast_selection(selection)
    try:
        max_hours = int(selection_for_panel.get("timeScopes") or 12)
    except (TypeError, ValueError):
        max_hours = 12

    next_time_value = None
    found_val = None
    forecast_hour_index = 0
    has_upcoming_forecast = True
    for region_name_candidate in regions or []:
        file_path = forecast_file_path(
            [
                str(region_name_candidate),
                str(pollutant),
                str(selection_for_panel.get("timeScopes")),
                str(selection_for_panel.get("models")),
                str(selection_for_panel.get("date")),
            ]
        )
        if not file_path:
            continue
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            mtime = 0
        parsed = _cached_parse_csv(file_path, pollutant, max_hours, mtime)
        if not parsed:
            continue
        if next_time_value is None:
            times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime") or []
            if times:
                forecast_hour_index, has_upcoming_forecast = _forecast_live_base_state(times)
                next_time_value = times[max(0, min(forecast_hour_index, len(times) - 1))]
        _name, found_val = _find_station_forecast_value(parsed, station_key, hour_index=forecast_hour_index)
        if _name is not None:
            break

    dt = _parse_forecast_timestamp(next_time_value) if next_time_value else None
    if dt:
        hour = dt.strftime("%I").lstrip("0") or "0"
        time_label = f"{dt.strftime('%b')} {dt.day} {hour}{dt.strftime('%p')}"
        if not has_upcoming_forecast:
            time_label = f"Latest available {time_label}"
    else:
        time_label = ""

    # Find station region (from static site metadata if available).
    site = _site_lookup().get(normalize_station_name(station_key)) or {}
    region_name = str(site.get("Region") or "").strip()

    station_title = title_case_station_name(station_key)
    value_label = "--" if found_val is None else f"{float(found_val):.1f}"
    category_key, colour = category_for_value(pollutant, found_val)
    category_label = str(category_key or "no-data").replace("-", " ").title()

    header_bits = [station_title]
    if region_name:
        header_bits.append(f"({region_name})")
    header = " ".join(header_bits)
    if time_label:
        header = f"{header} · {time_label}"

    fig = go.Figure(
        data=[
            go.Bar(
                x=[0 if found_val is None else float(found_val)],
                y=[pollutant_display(pollutant)],
                orientation="h",
                marker=dict(color=[colour]),
                text=[value_label],
                textposition="outside",
                cliponaxis=False,
                hovertemplate="%{y}: %{text}<extra></extra>",
                showlegend=False,
            )
        ]
    )
    fig.update_layout(
        template="plotly_white",
        height=160,
        margin=dict(l=80, r=30, t=10, b=20),
        xaxis=dict(showgrid=False, zeroline=False, tickfont=dict(size=14)),
        yaxis=dict(showgrid=False, tickfont=dict(size=18)),
        bargap=0.35,
    )

    return html.Div(
        [
            html.Div(header, style={"fontWeight": 950, "fontSize": "1.25rem", "color": "var(--ink)"}),
            html.Div(
                [
                    html.Span("Category: ", style={"fontWeight": 900}),
                    html.Span(category_label, style={"fontWeight": 900, "color": colour}),
                ],
                className="card-hint",
                style={"marginTop": "-6px"},
            ),
            dcc.Graph(figure=fig, config={"displayModeBar": False}),
        ]
    )


@app.callback(
    [Output("overview-next3-grid", "children"), Output("overview-next3-time-labels", "children")],
    [
        Input("region-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
    ],
    [State("dashboard-tabs", "value")],
)
def render_overview_next3_diamonds(region, time_scope, model, date, _overview_load, active_tab):
    print(f"DEBUG: render_overview_next3_diamonds called active_tab={active_tab} _overview_load={_overview_load}", flush=True)
    if active_tab != "overview":
        print("DEBUG: render_overview_next3_diamonds early exit (not overview)", flush=True)
        return no_update, no_update

    selection = {
        "regions": region or DEFAULT_SELECTION.get("regions"),
        "pollutants": "PM2.5",
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model or DEFAULT_SELECTION.get("models"),
        "date": date or DEFAULT_SELECTION.get("date"),
    }

    return _overview_station_outlook(selection)


def _overview_allstations_multi_hour_rows(selection, hours=3):
    """Return full station list across selected regions for PM2.5/PM10/O3 for +1h/+2h/+3h."""
    selection_for_panel, regions = _overview_pick_multi_region_forecast_selection(selection or {})
    try:
        max_hours = int(selection_for_panel.get("timeScopes") or 12)
    except (TypeError, ValueError):
        max_hours = 12

    hours = max(1, min(int(hours or 3), 3))
    pollutants = ["PM2.5", "PM10", "O3"]
    pollutant_key = {"PM2.5": "pm25", "PM10": "pm10", "O3": "o3"}
    if not regions:
        fallback_regions = set()
        for kp in FILE_KEY_PARAMS:
            if kp.get("pollutants") not in pollutants:
                continue
            if kp.get("timeScopes") != str(selection_for_panel.get("timeScopes")):
                continue
            if kp.get("date") != str(selection_for_panel.get("date")):
                continue
            region_name = kp.get("regions")
            if region_name:
                fallback_regions.add(region_name)
        regions = sorted(fallback_regions)

    rows = []
    station_index = {}
    time_labels = []

    def _ensure_row(station_name, region_name):
        station_norm = normalize_station_name(station_name)
        if not station_norm:
            return None
        existing = station_index.get(station_norm)
        if existing is not None:
            return existing
        site = _site_lookup().get(station_norm) or {}
        region_label = str(site.get("Region") or region_name or "").replace("_", " ").strip()
        row = {
            "station": title_case_station_name(station_name),
            "region": region_label or "--",
        }
        for hour_idx in range(1, hours + 1):
            for _pollutant, key in pollutant_key.items():
                row[f"h{hour_idx}_{key}"] = "--"
                row[f"h{hour_idx}_{key}_cat"] = "no-data"
        station_index[station_norm] = row
        rows.append(row)
        return row

    def _coerce_float(value):
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    for pollutant in pollutants:
        p_key = pollutant_key.get(pollutant)
        for region_name in regions or []:
            file_path = forecast_file_path(
                [
                    str(region_name),
                    str(pollutant),
                    str(selection_for_panel.get("timeScopes")),
                    str(selection_for_panel.get("models")),
                    str(selection_for_panel.get("date")),
                ]
            )
            if not file_path:
                for kp in FILE_KEY_PARAMS:
                    if kp.get("regions") != str(region_name):
                        continue
                    if kp.get("pollutants") != str(pollutant):
                        continue
                    if kp.get("timeScopes") != str(selection_for_panel.get("timeScopes")):
                        continue
                    if kp.get("date") != str(selection_for_panel.get("date")):
                        continue
                    file_path = forecast_file_path(
                        [
                            kp.get("regions"),
                            kp.get("pollutants"),
                            kp.get("timeScopes"),
                            kp.get("models"),
                            kp.get("date"),
                        ]
                    )
                    if file_path:
                        break
            if not file_path:
                continue
            try:
                mtime = os.path.getmtime(file_path)
            except OSError:
                mtime = 0
            parsed = _cached_parse_csv(file_path, pollutant, max_hours, mtime)
            if not parsed:
                continue

            if not time_labels:
                times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime") or []
                if times:
                    time_labels = list(times[:hours])

            stations = ((parsed.get("data") or {}).get("stations") or {})
            for station_name, payload in stations.items():
                row = _ensure_row(station_name, region_name)
                if not row:
                    continue
                series = (payload or {}).get("forecastValue") or []
                for hour_idx in range(hours):
                    value_num = None
                    if hour_idx < len(series):
                        value_num = _coerce_float(series[hour_idx])
                    col = f"h{hour_idx + 1}_{p_key}"
                    cat_col = f"h{hour_idx + 1}_{p_key}_cat"
                    if value_num is None:
                        continue
                    current_num = _coerce_float(row.get(col))
                    if current_num is None or value_num >= current_num:
                        row[col] = f"{value_num:.1f}"
                        category_key, _color = category_for_value(pollutant, value_num)
                        row[cat_col] = str(category_key or "no-data")

    pretty_labels = []
    for idx in range(hours):
        if idx < len(time_labels):
            dt = _parse_forecast_timestamp(time_labels[idx])
            if dt:
                hour = dt.strftime("%I").lstrip("0") or "0"
                pretty_labels.append(f"{hour}{dt.strftime('%p')}")
                continue
        pretty_labels.append(f"+{idx + 1}h")

    columns = [
        {"name": "Station", "id": "station"},
        {"name": "Region", "id": "region"},
    ]
    for idx, hour_label in enumerate(pretty_labels, start=1):
        columns.extend(
            [
                {"name": [hour_label, "PM2.5"], "id": f"h{idx}_pm25"},
                {"name": [hour_label, "PM10"], "id": f"h{idx}_pm10"},
                {"name": [hour_label, "O3"], "id": f"h{idx}_o3"},
            ]
        )

    def _row_score(item):
        score = float("-inf")
        for col in ["h1_pm25", "h1_pm10", "h1_o3"]:
            value = _coerce_float(item.get(col))
            if value is not None:
                score = max(score, value)
        return score

    rows.sort(key=lambda item: (-_row_score(item), str(item.get("station") or "")))
    return columns, rows


@app.callback(
    [Output("overview-next2-allstations-table", "columns"), Output("overview-next2-allstations-table", "data")],
    [
        Input("region-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
        Input("overview-open-allstations", "n_clicks"),
    ],
    [State("dashboard-tabs", "value")],
)
def render_overview_next2_allstations(region, time_scope, model, date, _overview_load, open_allstations_clicks, active_tab):
    # Populate when either the (now-removed) All-stations tab is active or the
    # overlay open button was clicked.
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if active_tab != "allstations" and trigger != "overview-open-allstations":
        return no_update, no_update
    selection = {
        "regions": region or DEFAULT_SELECTION.get("regions"),
        "pollutants": "PM2.5",
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model or DEFAULT_SELECTION.get("models"),
        "date": date or DEFAULT_SELECTION.get("date"),
    }
    # return columns and rows for offsets +1h, +3h, +6h, +12h
    return _overview_allstations_offsets_rows(selection, offsets=(1, 3, 6, 12))


@app.callback(
    [
        Output("overview-regional-air-quality-title", "children"),
        Output("overview-pm25-top3", "children"),
        Output("overview-pm25-region-dropdown", "options"),
        Output("overview-pm25-region-dropdown", "value"),
    ],
    [Input("monitor-store", "data")],
)
def render_overview_pm25_regions(monitor_store):
    monitor_store = monitor_store or {}
    error = monitor_store.get("error")
    latest_label = monitor_store.get("latestLabel") or "--"
    title = f"Regional Observed @ {latest_label}" if latest_label and latest_label != "--" else "Regional Observed"

    region_rows = _overview_multi_pollutant_region_rows(monitor_store)

    if not region_rows:
        message = f"Monitoring feed unavailable: {error}" if error else "Monitoring feed is loading."
        top_box = html.Div(message, className="overview-current-summary__body")
        return title, top_box, [], None

    # Top 3 regions by worst category across pollutants.
    top3 = region_rows[:3]
    rest = region_rows[3:]

    def _region_card(item):
        region = item.get("region") or "--"
        badge_color = item.get("categoryColor") or "#9ca3af"
        pollutants = item.get("pollutants") or {}
        boxes = []
        for pollutant in ["PM2.5", "PM10", "O3"]:
            payload = pollutants.get(pollutant) or {}
            boxes.append(
                html.Div(
                    [
                        html.Div(pollutant, className="overview-pollutant-box__label"),
                        html.Div(payload.get("valueLabel") or "--", className="overview-pollutant-box__value"),
                        html.Div(payload.get("category") or "No data", className="overview-pollutant-box__category"),
                    ],
                    className="overview-pollutant-box",
                    style={"backgroundColor": payload.get("categoryColor") or "#9ca3af"},
                )
            )

        return html.Div(
            [
                html.Div(
                    [
                        html.Div(region, className="overview-region-summary__title"),
                    ],
                    className="overview-region-summary__heading",
                ),
                html.Div(boxes, className="overview-region-summary__boxes"),
            ],
            className="overview-region-summary",
            style={"borderTop": f"6px solid {badge_color}"},
        )

    top_nodes = []
    for item in top3:
        top_nodes.append(_region_card(item))
    top_box = html.Div(top_nodes, className="overview-region-summary-grid")

    options = [{"label": item.get("region") or "", "value": item.get("region") or ""} for item in rest if item.get("region")]
    selected_region = options[0]["value"] if options else None
    return title, top_box, options, selected_region


@app.callback(
    Output("overview-observed-grid", "children"),
    [Input("monitor-store", "data")],
)
def render_overview_observed_grid(monitor_store):
    monitor_store = monitor_store or {}
    rows_by_pollutant = {
        "PM2.5": _overview_region_pollutant_summary_rows(monitor_store.get("aqmsPm25Rows") or [], "PM2.5"),
        "PM10": _overview_region_pollutant_summary_rows(monitor_store.get("aqmsPm10Rows") or [], "PM10"),
        "O3": _overview_region_pollutant_summary_rows(monitor_store.get("aqmsO3Rows") or [], "O3"),
    }
    sections = []
    for pollutant, rows in rows_by_pollutant.items():
        rows = sorted(rows or [], key=lambda item: title_case_station_name(str(item.get("region") or "")))
        sections.append(_overview_region_bar_section(pollutant, rows, "No observed data available."))
    return sections


@app.callback(
    Output("overview-pm25-other", "children"),
    [Input("overview-pm25-region-dropdown", "value")],
    [State("monitor-store", "data")],
)
def render_overview_pm25_other(selected_region, monitor_store):
    monitor_store = monitor_store or {}
    latest_label = monitor_store.get("latestLabel") or "--"
    region_rows = _overview_multi_pollutant_region_rows(monitor_store)
    rest = region_rows[3:]

    if not selected_region:
        return ""

    match = next((row for row in rest if row.get("region") == selected_region), None)
    if not match:
        return ""

    region = match.get("region") or "--"
    badge_color = match.get("categoryColor") or "#9ca3af"
    pollutants = match.get("pollutants") or {}
    boxes = []
    for pollutant in ["PM2.5", "PM10", "O3"]:
        payload = pollutants.get(pollutant) or {}
        boxes.append(
            html.Div(
                [
                    html.Div(pollutant, className="overview-pollutant-box__label"),
                    html.Div(payload.get("valueLabel") or "--", className="overview-pollutant-box__value"),
                    html.Div(payload.get("category") or "No data", className="overview-pollutant-box__category"),
                ],
                className="overview-pollutant-box",
                style={"backgroundColor": payload.get("categoryColor") or "#9ca3af"},
            )
        )

    return html.Div(
        [
            html.Div(
                [
                    html.Div(region, className="overview-region-summary__title"),
                ],
                className="overview-region-summary__heading",
            ),
            html.Div(boxes, className="overview-region-summary__boxes"),
        ],
        className="overview-region-summary",
        style={"borderTop": f"6px solid {badge_color}"},
    )


@app.callback(Output("overview-nsw-air-quality-updated", "children"), [Input("monitor-store", "data")])
def render_overview_nsw_air_quality_updated(monitor_store):
    monitor_store = monitor_store or {}
    rows = monitor_store.get("aqmsPm25Rows") or []
    latest = _monitoring_time_row(rows)
    if not latest:
        error = monitor_store.get("error")
        if error:
            return "@ -- (AQMS feed offline)"
        return "@ -- (loading)"
    hour_desc = latest.get("hour_description") or "--"
    date_value = str(latest.get("date") or "").strip()
    date_label = date_value or "--"
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            date_label = datetime.strptime(date_value, fmt).strftime("%d %b %Y").lstrip("0")
            break
        except (TypeError, ValueError):
            continue
    return f"@ {hour_desc} / {date_label}"


@app.callback(
    Output("overview-nsw-region-index", "data"),
    [
        Input("overview-nsw-region-prev", "n_clicks"),
        Input("overview-nsw-region-next", "n_clicks"),
        Input("overview-region-autoplay", "n_intervals"),
    ],
    [State("monitor-store", "data"), State("overview-nsw-region-index", "data")],
    prevent_initial_call=True,
)
def cycle_overview_nsw_region(_prev_clicks, _next_clicks, _autoplay_intervals, monitor_store, current_index):
    """Cycle the Overview status card between All NSW and individual regions."""
    monitor_store = monitor_store or {}
    # Aggregate available AQMS pollutant rows so station lists include sites present in any pollutant feed
    pm25_rows_all = monitor_store.get("aqmsPm25Rows") or []
    pm10_rows_all = monitor_store.get("aqmsPm10Rows") or []
    o3_rows_all = monitor_store.get("aqmsO3Rows") or []
    combined_rows = list(pm25_rows_all) + list(pm10_rows_all) + list(o3_rows_all)
    latest = _monitoring_time_row(combined_rows)
    date_val = latest.get("date")
    hour_val = latest.get("hour")
    latest_rows = [row for row in pm25_rows_all if row.get("date") == date_val and row.get("hour") == hour_val]
    regions = sorted({str(row.get("region") or "").strip() for row in latest_rows if str(row.get("region") or "").strip()})
    count = 1 + len(regions)  # +1 for All NSW
    if count <= 0:
        return 0

    try:
        idx = int(current_index or 0)
    except (TypeError, ValueError):
        idx = 0

    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    # Determine new NSW index depending on which control triggered (master controls only)
    if trigger == "overview-nsw-region-prev":
        idx = (idx - 1) % count
    elif trigger == "overview-nsw-region-next":
        idx = (idx + 1) % count
    elif trigger == "overview-region-autoplay":
        idx = (idx + 1) % count
    else:
        idx = max(0, min(idx, count - 1))

    # Map internal All NSW (index 0) to display name 'NSW'
    return idx


@app.callback(
    [Output("overview-met-region-index", "data"), Output("overview-met-region-name", "children")],
    [
        Input("overview-met-region-prev", "n_clicks"),
        Input("overview-met-region-next", "n_clicks"),
        Input("overview-nsw-region-prev", "n_clicks"),
        Input("overview-nsw-region-next", "n_clicks"),
        Input("overview-region-autoplay", "n_intervals"),
    ],
    [State("monitor-store", "data"), State("overview-met-region-index", "data"), State("dashboard-tabs", "value")],
    prevent_initial_call=True,
)
def cycle_overview_met_region(_prev_clicks, _next_clicks, _nsw_prev, _nsw_next, _autoplay_intervals, monitor_store, current_index, active_tab):
    """Manage the meteorology panel region selection independently, but respond to master nav."""
    if active_tab != "overview":
        return current_index or 0, dash.no_update

    monitor_store = monitor_store or {}
    pm25_rows_all = monitor_store.get("aqmsPm25Rows") or []
    pm10_rows_all = monitor_store.get("aqmsPm10Rows") or []
    o3_rows_all = monitor_store.get("aqmsO3Rows") or []
    combined_rows = list(pm25_rows_all) + list(pm10_rows_all) + list(o3_rows_all)
    latest = _monitoring_time_row(combined_rows)
    if not latest:
        return current_index or 0, "NSW"
    date_val = latest.get("date")
    hour_val = latest.get("hour")
    latest_rows = [row for row in pm25_rows_all if row.get("date") == date_val and row.get("hour") == hour_val]
    regions = sorted({str(row.get("region") or "").strip() for row in latest_rows if str(row.get("region") or "").strip()})
    count = 1 + len(regions)

    try:
        idx = int(current_index or 0)
    except Exception:
        idx = 0

    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if trigger == "overview-met-region-prev":
        idx = (idx - 1) % count
    elif trigger == "overview-met-region-next":
        idx = (idx + 1) % count
    elif trigger == "overview-nsw-region-prev":
        idx = (idx - 1) % count
    elif trigger == "overview-nsw-region-next":
        idx = (idx + 1) % count
    elif trigger == "overview-region-autoplay":
        idx = (idx + 1) % count
    else:
        idx = max(0, min(idx, count - 1))

    try:
        display = "NSW" if idx <= 0 else (regions[idx - 1] if (idx - 1) < len(regions) else "NSW")
    except Exception:
        display = "NSW"

    return idx, display


@app.callback(
    Output("overview-nsw-station-index", "data"),
    [
        Input("overview-nsw-station-prev", "n_clicks"),
        Input("overview-nsw-station-next", "n_clicks"),
        Input("overview-nsw-region-index", "data"),
    ],
    [State("monitor-store", "data"), State("overview-nsw-station-index", "data")],
    prevent_initial_call=True,
)
def cycle_overview_nsw_station(_prev, _next, region_index, monitor_store, current_index):
    """Cycle stations within the selected region."""
    monitor_store = monitor_store or {}
    pm25_rows_all = monitor_store.get("aqmsPm25Rows") or []
    pm10_rows_all = monitor_store.get("aqmsPm10Rows") or []
    o3_rows_all = monitor_store.get("aqmsO3Rows") or []
    combined_rows = list(pm25_rows_all) + list(pm10_rows_all) + list(o3_rows_all)
    latest = _monitoring_time_row(combined_rows)
    if not latest:
        return 0
    date_val = latest.get("date")
    hour_val = latest.get("hour")
    latest_rows = [r for r in combined_rows if r.get("date") == date_val and r.get("hour") == hour_val]
    regions = sorted({str(r.get("region") or "").strip() for r in latest_rows if str(r.get("region") or "").strip()})

    try:
        idx_reg = int(region_index or 0)
    except Exception:
        idx_reg = 0

    if idx_reg <= 0 or not regions:
        region_name = None
    else:
        region_name = regions[idx_reg - 1]

    stations = []
    if region_name:
        stations = sorted({str(r.get("station") or "").strip() for r in latest_rows if str(r.get("region") or "").strip() == region_name})
    else:
        stations = sorted({str(r.get("station") or "").strip() for r in latest_rows})

    count = len(stations)
    if count <= 0:
        return 0

    try:
        idx = int(current_index or 0)
    except Exception:
        idx = 0

    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if trigger == "overview-nsw-station-prev":
        idx = (idx - 1) % count
    elif trigger == "overview-nsw-station-next":
        idx = (idx + 1) % count
    else:
        # Reset to first station when region or monitor-store changes
        idx = 0

    return idx


@app.callback(
    [Output("overview-nsw-air-quality-status", "children"), Output("overview-nsw-region-name", "children")],
    [Input("monitor-store", "data"), Input("overview-nsw-region-index", "data")],
)
def render_overview_nsw_air_quality_status(monitor_store, region_index):
    """Render NSW/region PM2.5 network average from AQMS (no PurpleAir)."""
    global _LAST_NSW_PM25_AVG
    print(f"DEBUG: render_overview_nsw_air_quality_status called region_index={region_index} monitor_store_present={bool(monitor_store)}", flush=True)
    monitor_store = monitor_store or {}
    pm25_rows_all = monitor_store.get("aqmsPm25Rows") or []
    if not pm25_rows_all:
        error = monitor_store.get("error")
        if error:
            return html.Div(str(error), className="card-hint"), "NSW"
        status = monitor_store.get("status") or ""
        if status and "Offline" in status:
            return html.Div("AQMS monitoring feed is offline.", className="card-hint"), "NSW"
        return html.Div("Loading AQMS monitoring data…", className="card-hint"), "NSW"


    # Station box callback moved to module scope (registered below)

    latest = _monitoring_time_row(pm25_rows_all)
    date_val = latest.get("date")
    hour_val = latest.get("hour")
    latest_rows = [row for row in pm25_rows_all if row.get("date") == date_val and row.get("hour") == hour_val]
    regions = sorted({str(row.get("region") or "").strip() for row in latest_rows if str(row.get("region") or "").strip()})

    try:
        idx = int(region_index or 0)
    except (TypeError, ValueError):
        idx = 0
    if idx <= 0 or not regions:
        region_name = "All NSW"
        filtered_rows = latest_rows
    else:
        region_name = regions[idx - 1] if (idx - 1) < len(regions) else "All NSW"
        filtered_rows = [row for row in latest_rows if str(row.get("region") or "").strip() == region_name]

    values = [row.get("value") for row in filtered_rows if row.get("value") is not None]
    avg = (sum(values) / float(len(values))) if values else None

    def _latest_avg_for_region(rows, region):
        if not rows:
            return None
        snap = _monitoring_time_row(rows)
        if not snap:
            return None
        d = snap.get("date")
        h = snap.get("hour")
        base = [r for r in rows if r.get("date") == d and r.get("hour") == h]
        if region and region != "All NSW":
            base = [r for r in base if str(r.get("region") or "").strip() == region]
        vals = [r.get("value") for r in base if r.get("value") is not None]
        return (sum(vals) / float(len(vals))) if vals else None

    pm10_avg = _latest_avg_for_region(monitor_store.get("aqmsPm10Rows") or [], region_name)
    o3_avg = _latest_avg_for_region(monitor_store.get("aqmsO3Rows") or [], region_name)

    # Compute PurpleAir regional average (PM2.5) using nearby sensors by distance.
    def _haversine_meters(lat1, lon1, lat2, lon2):
        try:
            # coords in degrees
            phi1 = math.radians(float(lat1))
            phi2 = math.radians(float(lat2))
            dphi = math.radians(float(lat2) - float(lat1))
            dlambda = math.radians(float(lon2) - float(lon1))
        except Exception:
            return None
        a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return 6371000.0 * c

    def _sensor_value(s):
        if not s:
            return None
        for k in ("value", "pm25", "pm2_5", "pm2_5_val"):
            if k in s and s.get(k) is not None:
                try:
                    return float(s.get(k))
                except Exception:
                    return None
        return None

    purpleair_rows = (monitor_store or {}).get("purpleairSensors") or []
    purpleair_avg = None
    # If All NSW, use all PurpleAir sensors
    if region_name == "All NSW":
        vals = [_sensor_value(s) for s in purpleair_rows if _sensor_value(s) is not None]
        purpleair_avg = (sum(vals) / float(len(vals))) if vals else None
    else:
        # Use AQMS filtered_rows to compute centroid for proximity selection
        coords = [(r.get("lat"), r.get("lon")) for r in filtered_rows if r.get("lat") is not None and r.get("lon") is not None]
        centroid = None
        if coords:
            lat_c = sum(float(c[0]) for c in coords) / len(coords)
            lon_c = sum(float(c[1]) for c in coords) / len(coords)
            centroid = (lat_c, lon_c)

        nearby_vals = []
        if centroid:
            for s in purpleair_rows:
                lat = s.get("lat")
                lon = s.get("lon")
                if lat is None or lon is None:
                    continue
                dist = _haversine_meters(centroid[0], centroid[1], lat, lon)
                if dist is None:
                    continue
                # include sensors within 20 km
                if dist <= 20000:
                    v = _sensor_value(s)
                    if v is not None:
                        nearby_vals.append(v)
        if nearby_vals:
            purpleair_avg = sum(nearby_vals) / float(len(nearby_vals))
        else:
            # fallback: use sensors tagged to the same region if available
            region_vals = [_sensor_value(s) for s in purpleair_rows if str(s.get("region") or "").strip() == region_name and _sensor_value(s) is not None]
            if region_vals:
                purpleair_avg = sum(region_vals) / float(len(region_vals))

    key, colour = category_for_value("PM2.5", avg)
    category = _pollutant_category_label("PM2.5", colour)

    fetched_epoch = monitor_store.get("fetchedAtEpoch")
    delta = None
    pm10_delta = None
    o3_delta = None
    try:
        if fetched_epoch is not None and _LAST_NSW_PM25_AVG.get("ts") != fetched_epoch:
            prev_avg = _LAST_NSW_PM25_AVG.get("avg")
            if prev_avg is not None and avg is not None:
                delta = avg - prev_avg
            _LAST_NSW_PM25_AVG = {"ts": fetched_epoch, "avg": avg}
    except Exception:
        pass

    # Compute PM10/O3 deltas using a small module-level tracker so we can render arrows
    global _LAST_NSW_AQMS
    try:
        if fetched_epoch is not None and _LAST_NSW_AQMS.get("ts") != fetched_epoch:
            prev_pm10 = _LAST_NSW_AQMS.get("pm10")
            prev_o3 = _LAST_NSW_AQMS.get("o3")
            if prev_pm10 is not None and pm10_avg is not None:
                pm10_delta = pm10_avg - prev_pm10
            if prev_o3 is not None and o3_avg is not None:
                o3_delta = o3_avg - prev_o3
            _LAST_NSW_AQMS = {"ts": fetched_epoch, "pm10": pm10_avg, "o3": o3_avg}
    except Exception:
        pass

    units = (POLLUTANTS.get("PM2.5") or {}).get("Units") or "µg/m³"
    avg_label = "--" if avg is None else f"{avg:.0f}"
    avg_sub = "--" if avg is None else f"{avg:.1f} {units}"

    pm10_units = (POLLUTANTS.get("PM10") or {}).get("Units") or "µg/m³"
    o3_units = (POLLUTANTS.get("O3") or {}).get("Units") or ""
    pm10_label = "--" if pm10_avg is None else f"{pm10_avg:.1f} {pm10_units}".strip()
    o3_label = "--" if o3_avg is None else f"{o3_avg:.1f} {o3_units}".strip()

    purpleair_label = "--"
    if purpleair_avg is not None:
        pa_units = (POLLUTANTS.get("PM2.5") or {}).get("Units") or "µg/m³"
        purpleair_label = f"{purpleair_avg:.1f} {pa_units}".strip()

    delta_label = ""
    def _arrow_span(d):
        if d is None or abs(float(d)) <= 1e-9:
            return ""
        dv = float(d)
        if dv > 0:
            return html.Span(" ↑", style={"color": "#dc2626", "fontWeight": 900, "marginLeft": "6px"})
        return html.Span(" ↓", style={"color": "#16a34a", "fontWeight": 900, "marginLeft": "6px"})

    delta_node = None
    if delta is not None and abs(float(delta)) > 1e-9:
        delta_value = float(delta)
        if delta_value > 0:
            arrow = "↑"
            delta_colour = "#dc2626"  # red
            word = "Increased"
        else:
            arrow = "↓"
            delta_colour = "#16a34a"  # green
            word = "Decreased"
        delta_node = html.Div(
            [
                html.Span(f"{arrow} ", style={"color": delta_colour, "fontWeight": 950}),
                html.Span(f"{word} ", style={"color": "rgba(15, 23, 42, 0.72)", "fontWeight": 900}),
                html.Span(f"{abs(delta_value):.1f} {units}", style={"color": delta_colour, "fontWeight": 950}),
                html.Span(" vs previous", style={"color": "rgba(15, 23, 42, 0.60)", "fontWeight": 900}),
            ],
            className="nsw-aq-status__delta",
        )

    # Use display name 'NSW' for the UI while keeping internal region_name as 'All NSW'
    display_region_name = "NSW" if region_name == "All NSW" else region_name

    return (
        html.Div(
            [
                html.Div(
                    [
                    html.Div(
                        [
                            html.Div("Avg (PM2.5)", className="nsw-aq-status__metric-label"),
                            html.Div([html.Span(avg_label), _arrow_span(delta)], className="nsw-aq-status__metric-value", style={"color": colour}),
                            html.Div(category, className="nsw-aq-status__metric-category", style={"color": colour}),
                        ],
                        className="nsw-aq-status__metric",
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Div("AQMS averages", className="nsw-aq-status__kpi-label"),
                                    html.Div(
                                        [
                                            html.Div([html.Span(f"PM10 · {pm10_label}"), _arrow_span(pm10_delta)], className="nsw-aq-status__kpi-value"),
                                            html.Div([html.Span(f"O3 · {o3_label}"), _arrow_span(o3_delta)], className="nsw-aq-status__kpi-value"),
                                        ]
                                    ),
                                ]
                            ),
                            delta_node or "",
                        ],
                        className="nsw-aq-status__kpis",
                        style={"display": "flex", "alignItems": "center", "gap": "14px"},
                    ),
                ],
                className="nsw-aq-status__row",
            ),
        ],
        className="nsw-aq-status",
        style={"borderTop": f"6px solid {colour}"},
        ),
        display_region_name,
    )


def _overview_region_name_from_index(monitor_store, region_index):
    monitor_store = monitor_store or {}
    pm25_rows_all = monitor_store.get("aqmsPm25Rows") or []
    latest = _monitoring_time_row(pm25_rows_all) if pm25_rows_all else None
    if not latest:
        return "NSW"
    date_val = latest.get("date")
    hour_val = latest.get("hour")
    latest_rows = [row for row in pm25_rows_all if row.get("date") == date_val and row.get("hour") == hour_val]
    regions = sorted({str(row.get("region") or "").strip() for row in latest_rows if str(row.get("region") or "").strip()})
    try:
        idx = int(region_index or 0)
    except (TypeError, ValueError):
        idx = 0
    if idx <= 0 or not regions:
        return "NSW"
    if (idx - 1) < len(regions):
        return regions[idx - 1]
    return "NSW"


def _met_sparkline(values, color, height=46):
    xs = list(range(len(values or [])))
    ys = []
    for v in values or []:
        try:
            ys.append(float(v) if v is not None else None)
        except (TypeError, ValueError):
            ys.append(None)
    fig = go.Figure()
    if any(v is not None for v in ys):
        fig.add_trace(
            go.Bar(
                x=xs,
                y=[v if v is not None else 0 for v in ys],
                marker=dict(color=color, opacity=0.75),
                hoverinfo="skip",
                showlegend=False,
            )
        )
    fig.update_layout(
        template="plotly_white",
        height=height,
        margin=dict(l=6, r=6, t=2, b=2),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    return fig


@app.callback(
    [Output("overview-nsw-station-box", "children"), Output("overview-nsw-station-name", "children")],
    [Input("monitor-store", "data"), Input("overview-nsw-region-index", "data"), Input("overview-nsw-station-index", "data")],
)
def render_overview_nsw_station_box(monitor_store, region_index, station_index):
    """Render a small station pollutant box for the selected region and station index."""
    monitor_store = monitor_store or {}
    pm25_rows_all = monitor_store.get("aqmsPm25Rows") or []
    pm10_rows_all = monitor_store.get("aqmsPm10Rows") or []
    o3_rows_all = monitor_store.get("aqmsO3Rows") or []
    combined_rows = list(pm25_rows_all) + list(pm10_rows_all) + list(o3_rows_all)
    if not combined_rows:
        return html.Div("No data", className="card-hint"), ""

    latest = _monitoring_time_row(combined_rows)
    if not latest:
        return html.Div("No data", className="card-hint"), ""

    date_val = latest.get("date")
    hour_val = latest.get("hour")
    latest_rows = [r for r in combined_rows if r.get("date") == date_val and r.get("hour") == hour_val]
    regions = sorted({str(r.get("region") or "").strip() for r in latest_rows if str(r.get("region") or "").strip()})

    try:
        idx_reg = int(region_index or 0)
    except Exception:
        idx_reg = 0

    if idx_reg <= 0 or not regions:
        region_name = None
    else:
        region_name = regions[idx_reg - 1]

    # Build station list for region (or all) using combined pollutant rows
    if region_name:
        station_rows = [r for r in latest_rows if str(r.get("region") or "").strip() == region_name]
    else:
        station_rows = latest_rows

    # Deduplicate station names while preserving order and exclude names equal to the region
    stations = []
    seen = set()
    region_cmp = (str(region_name or "").strip().lower() if region_name else None)
    for r in station_rows:
        name = str(r.get("station") or "").strip()
        if not name:
            continue
        ncmp = name.lower()
        if region_cmp and ncmp == region_cmp:
            # skip station names that are identical to the region label
            continue
        if ncmp in seen:
            continue
        seen.add(ncmp)
        stations.append(name)
    if not stations:
        return html.Div("No stations"), ""

    try:
        sidx = int(station_index or 0)
    except Exception:
        sidx = 0
    sidx = max(0, min(sidx, len(stations) - 1))
    station_name = stations[sidx]

    # Find PM2.5, PM10, O3 values for that station (from monitor_store rows of respective pollutants)
    def _find_value(rows, station):
        for r in rows or []:
            if str(r.get("station") or "").strip() == station:
                return r.get("value")
        return None

    pm25_val = _find_value(monitor_store.get("aqmsPm25Rows") or [], station_name)
    pm10_val = _find_value(monitor_store.get("aqmsPm10Rows") or [], station_name)
    o3_val = _find_value(monitor_store.get("aqmsO3Rows") or [], station_name)

    def _box(pollutant, val):
        try:
            v = float(val) if val is not None else None
        except Exception:
            v = None
        label = "--" if v is None else f"{v:.1f}"
        key, colour = category_for_value(pollutant, v)
        cat = _pollutant_category_label(pollutant, colour)
        return html.Div(
            [
                html.Div(pollutant, className="overview-pollutant-box__label"),
                html.Div(label, className="overview-pollutant-box__value"),
                html.Div(cat, className="overview-pollutant-box__category"),
            ],
            className="overview-pollutant-box",
            style={"backgroundColor": colour},
        )

    boxes = [_box("PM2.5", pm25_val), _box("PM10", pm10_val), _box("O3", o3_val)]

    # Use PM2.5 category color as the station accent (fallback to neutral)
    try:
        _, station_colour = category_for_value("PM2.5", float(pm25_val) if pm25_val is not None else None)
    except Exception:
        station_colour = "#9ca3af"

    # Render only the pollutant boxes here — the station/region title is shown
    # in the surrounding header so including it again is redundant.
    card = html.Div(
        [
            html.Div(boxes, className="overview-region-summary__boxes"),
        ],
        className="overview-region-summary",
        style={"borderTop": f"6px solid {station_colour}"},
    )

    return card, station_name


@app.callback(
    Output("overview-met-6h", "children"),
    [
        Input("overview-met-load", "n_intervals"),
        Input("overview-nsw-region-index", "data"),
    ],
    [State("monitor-store", "data"), State("dashboard-tabs", "value")],
    prevent_initial_call=True,
)
def render_overview_met6h(_met_load, region_index, monitor_store, active_tab):
    if active_tab != "overview":
        return no_update
    monitor_store = monitor_store or {}
    region_name = _overview_region_name_from_index(monitor_store, region_index)
    met_sites = (monitor_store.get("metSiteIdsByRegion") or {})
    site_ids = met_sites.get(region_name) or met_sites.get("All NSW") or []
    # Cap to keep the history fetch snappy.
    site_ids = list(site_ids)[:40]
    payload = _met_series_for_region(region_name, site_ids, hours=24)
    times = payload.get("times") or []
    series = payload.get("series") or {}
    units_payload = payload.get("units") or {}
    if not times:
        return html.Div("Meteorological data is loading…", className="card-hint")

    def _vals(code):
        return (series.get(code) or []), (units_payload.get(code) or "")

    items = [
        ("Temperature", "TEMP", "#f59e0b"),
        ("Relative Humidity", "HUMID", "#22c55e"),
        ("Wind Speed", "WSP", "#3b82f6"),
        ("Rainfall", "RAIN", "#8b5cf6"),
    ]

    # Build hour labels for the last available 24 timestamps.
    x_labels = []
    time_titles = []
    for t in times[-24:]:
        dt = _parse_forecast_timestamp(t)
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=SYDNEY_TZ)
            dt = dt.astimezone(SYDNEY_TZ)
            x_labels.append(dt.strftime("%H:%M"))
            time_titles.append(dt.strftime("%Y-%m-%d %H:%M %Z"))
        else:
            x_labels.append("--")
            time_titles.append("")

    # Show latest timestamp as the end label. start label not needed.
    axis_start = ""
    # Prefer a friendly latest label (e.g. '23 May 14:00 AEST') when available
    axis_end = "Latest"
    if time_titles:
        try:
            dt_latest = _parse_forecast_timestamp(time_titles[-1])
            if dt_latest:
                if dt_latest.tzinfo is None:
                    dt_latest = dt_latest.replace(tzinfo=SYDNEY_TZ)
                dt_latest = dt_latest.astimezone(SYDNEY_TZ)
                axis_end = dt_latest.strftime("%d %b %H:%M %Z")
        except Exception:
            axis_end = time_titles[-1] or "Latest"

    # The card wrapper already contains the heading; avoid duplicating it here.

    rows = []
    # Prepare formatted per-bar labels (e.g., 1AM, 2PM) for the bottom row only.
    display_times = times[-24:]
    formatted_labels_bottom = []
    prev_date = None
    for i, t_raw in enumerate(display_times):
        dt = _parse_forecast_timestamp(t_raw) if t_raw else None
        date_prefix = ""
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=SYDNEY_TZ)
            dt = dt.astimezone(SYDNEY_TZ)
            if i == 0 or (prev_date is not None and dt.date() != prev_date):
                date_prefix = dt.strftime("%d %b ")
            time_part = dt.strftime("%I %p").lstrip("0")
            lbl = f"{date_prefix}{time_part}"
            prev_date = dt.date()
        else:
            lbl = "--"

        # Last label should be 'Now'
        if i == len(display_times) - 1:
            lbl = "Now"
        else:
            if (i % 4 != 0) and (not date_prefix):
                lbl = ""
        formatted_labels_bottom.append(lbl)

    # Placeholder labels (empty) used for non-bottom rows to keep alignment
    formatted_labels_blank = ["" for _ in formatted_labels_bottom]
    # icon map for nicer visuals
    ICONS = {"Temperature": "🌡️", "Relative Humidity": "💧", "Wind Speed": "💨", "Rainfall": "🌧️"}
    for label, key, color in items:
        values, units = _vals(key)
        values = (values or [])[-24:]
        while len(values) < 24:
            values = [None] + list(values)

        latest_val = next((v for v in reversed(values) if v is not None), None)
        latest_label = "--" if latest_val is None else f"{float(latest_val):.1f}"

        # Compute min/max for the sparkline scale and display
        window_vals = [v for v in values if v is not None]
        min_val = min(window_vals) if window_vals else None
        max_val = max(window_vals) if window_vals else None
        # avoid zero-scale
        scale_max = max_val if (max_val is not None and max_val != 0) else 1.0

        # Build compact sparkline bars (24) — low height baseline to a small max height
        bars = []
        for i, v in enumerate(values[-24:]):
            title = time_titles[i] if i < len(time_titles) else ""
            if v is None:
                h = 6
                fill = "rgba(148, 163, 184, 0.18)"
            else:
                try:
                    ratio = float(v) / float(scale_max)
                except Exception:
                    ratio = 0.0
                ratio = max(0.0, min(1.0, ratio))
                h = int(6 + ratio * 40)
                fill = color
            bars.append(
                html.Div(
                    "",
                    title=(f"{title} — {v} {units}" if v is not None else title),
                    style={
                        "display": "inline-block",
                        "verticalAlign": "bottom",
                            "width": "5px",
                            "height": f"{h}px",
                            "marginRight": "2px",
                        "borderRadius": "2px",
                        "background": fill,
                    },
                )
            )

        # Right column shows min / max
        min_label = "--" if min_val is None else f"{float(min_val):.1f}"
        max_label = "--" if max_val is None else f"{float(max_val):.1f}"

        # Use date/time labels only for the bottom (Rainfall) row; others get blank placeholders.
        use_labels = formatted_labels_bottom if key == "RAIN" else formatted_labels_blank

        rows.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(ICONS.get(label, ""), style={"marginRight": "8px", "fontSize": "18px"}),
                            html.Span(label, style={"fontWeight": 900, "fontSize": "0.94rem", "color": "rgba(15, 23, 42, 0.9)"}),
                        ],
                        style={"display": "flex", "alignItems": "center", "gap": "8px"},
                    ),
                    html.Div(
                        html.Div(bars, style={"height": "48px", "display": "flex", "alignItems": "flex-end", "overflowX": "auto", "paddingBottom": "6px"}),
                        style={"paddingLeft": "6px", "paddingRight": "6px"},
                    ),
                    # Only show inline x-axis labels for Rainfall (bottom row). Others get a placeholder to keep alignment.
                    html.Div(
                        html.Div(
                            [
                                html.Span(
                                    lbl,
                                    style={
                                        "display": "inline-block",
                                        "width": "76px",
                                        "marginRight": "6px",
                                        "textAlign": "center",
                                        "fontSize": "11px",
                                        "color": "rgba(15,23,42,0.65)",
                                        "whiteSpace": "nowrap",
                                        "overflow": "hidden",
                                        "textOverflow": "ellipsis",
                                    },
                                )
                                for lbl in use_labels
                            ],
                            style={"whiteSpace": "nowrap", "overflowX": "auto", "paddingTop": "6px"},
                        ),
                        style={"gridColumn": "2 / span 1"},
                    ),
                    # Right column intentionally left blank to avoid showing numeric labels
                    html.Div("", style={"textAlign": "right"}),
                ],
                style={"display": "grid", "gridTemplateColumns": "110px 1fr 0.25fr", "gap": "4px", "alignItems": "center", "paddingTop": "2px", "paddingBottom": "2px"},
            )
        )

    axis = html.Div(
        [
            html.Div(axis_start, style={"fontWeight": 900, "fontSize": "0.85rem", "color": "rgba(15, 23, 42, 0.60)"}),
            html.Div(axis_end, style={"fontWeight": 900, "fontSize": "0.85rem", "color": "rgba(15, 23, 42, 0.60)"}),
        ],
        style={"display": "flex", "justifyContent": "space-between", "marginTop": "8px", "paddingTop": "6px", "borderTop": "1px solid rgba(148, 163, 184, 0.25)"},
    )

    return html.Div(rows + [axis], style={"display": "flex", "flexDirection": "column"})


@app.callback(
    [
        Output("overview-forecast-trends", "children"),
        Output("overview-trends-current-region-label", "children"),
    ],
    [
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
        Input("overview-trends-index", "data"),
    ],
    [State("dashboard-tabs", "value")],
)
def render_overview_trends(time_scope, model_name, run_date, _overview_load, selected_index, active_tab):
    if active_tab != "overview":
        return no_update, no_update

    selection = {
        "regions": DEFAULT_SELECTION.get("regions"),
        "pollutants": DEFAULT_SELECTION.get("pollutants"),
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model_name or DEFAULT_SELECTION.get("models"),
        "date": run_date or DEFAULT_SELECTION.get("date"),
    }

    sections = []
    time_label = "--"
    for pollutant in ["PM2.5", "PM10", "O3"]:
        rows, pollutant_time_label = _overview_forecast_region_rows(selection, pollutant)
        if time_label == "--" and pollutant_time_label != "--":
            time_label = pollutant_time_label
        sections.append(_overview_region_bar_section(pollutant, rows, "No forecast data available."))

    return sections, time_label


@app.callback(
    Output("overview-trends-index", "data"),
    [
        Input("overview-trends-prev", "n_clicks"),
        Input("overview-trends-next", "n_clicks"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-nsw-region-prev", "n_clicks"),
        Input("overview-nsw-region-next", "n_clicks"),
        Input("overview-region-autoplay", "n_intervals"),
    ],
    [State("overview-trends-index", "data"), State("dashboard-tabs", "value")],
)
def overview_trends_nav(prev_clicks, next_clicks, time_scope, model_name, run_date, _nsw_prev, _nsw_next, _autoplay_intervals, current_index, active_tab):
    """Update the overview trends index when Prev/Next are clicked."""
    # Only operate on Overview tab
    if active_tab != "overview":
        return current_index or 0

    # Recompute available regions for the current selection
    selection = {
        "regions": DEFAULT_SELECTION.get("regions"),
        "pollutants": DEFAULT_SELECTION.get("pollutants"),
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model_name or DEFAULT_SELECTION.get("models"),
        "date": run_date or DEFAULT_SELECTION.get("date"),
    }
    _sel, regions = _overview_pick_multi_region_forecast_selection(selection)
    if not regions:
        return current_index or 0

    # Determine which button triggered the callback
    triggered = callback_context.triggered[0]["prop_id"] if callback_context.triggered else None
    try:
        idx = int(current_index or 0)
    except Exception:
        idx = 0

    if triggered and triggered.startswith("overview-trends-prev"):
        idx = (idx - 1) % len(regions)
    elif triggered and triggered.startswith("overview-trends-next"):
        idx = (idx + 1) % len(regions)
    elif triggered and triggered.startswith("overview-nsw-region-prev"):
        idx = (idx - 1) % len(regions)
    elif triggered and triggered.startswith("overview-nsw-region-next"):
        idx = (idx + 1) % len(regions)
    elif triggered and triggered.startswith("overview-region-autoplay"):
        idx = (idx + 1) % len(regions)
    else:
        # Reset index when the selection changes (time/model/date) or on first load
        idx = 0

    return idx


@app.callback(Output("header-monitor-updated", "children"), [Input("monitor-store", "data")])
def render_header_monitor_timestamps(monitor_store):
    monitor_store = monitor_store or {}
    epoch = monitor_store.get("fetchedAtEpoch")
    updated_label = _format_header_timestamp(epoch) if epoch is not None else "--"
    return updated_label


@app.callback(
    Output("header-forecast-time", "children"),
    [
        Input("region-dropdown", "value"),
        Input("pollutant-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
    ],
    [State("dashboard-tabs", "value")],
)
def render_header_forecast_time(region, pollutant, time_scope, model, date, _overview_load, _active_tab):
    selection = {
        "regions": region or DEFAULT_SELECTION.get("regions"),
        "pollutants": pollutant or DEFAULT_SELECTION.get("pollutants"),
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model or DEFAULT_SELECTION.get("models"),
        "date": date or DEFAULT_SELECTION.get("date"),
    }

    file_path = _build_file_path(selection)
    if not file_path:
        file_path, _exact = _best_matching_file(selection)
    if not file_path:
        return "--"
    try:
        epoch = os.path.getmtime(file_path)
    except OSError:
        epoch = None
    return _format_header_timestamp(epoch)


def _default_monitor_selection(monitor_store):
    aqms_rows = (monitor_store or {}).get("aqmsRows") or []
    purpleair_rows = (monitor_store or {}).get("purpleairSensors") or []
    if aqms_rows:
        return {"source": "AQMS", "id": str(aqms_rows[0].get("site_id"))}
    if purpleair_rows:
        return {"source": "PurpleAir", "id": str(purpleair_rows[0].get("site_id"))}
    return None


def _monitor_from_key(monitor_key):
    monitor_key = str(monitor_key or "").strip().lower()
    if not monitor_key or ":" not in monitor_key:
        return None
    prefix, rest = monitor_key.split(":", 1)
    if not rest:
        return None
    if prefix == "aqms":
        return {"source": "AQMS", "id": rest}
    if prefix == "purpleair":
        return {"source": "PurpleAir", "id": rest}
    return None


def _parse_aqms_time_point(item):
    date_text = item.get("Date")
    hour = item.get("Hour")
    if not date_text:
        return None
    try:
        base = datetime.strptime(str(date_text), "%Y-%m-%d")
    except ValueError:
        return None
    try:
        hour = int(hour)
    except (TypeError, ValueError):
        hour = None
    if not hour:
        return base.replace(tzinfo=SYDNEY_TZ)
    return (base + timedelta(hours=max(hour - 1, 0))).replace(tzinfo=SYDNEY_TZ)


def _monitor_trend_figure(pollutant_label, aqms_history=None, purpleair_history=None, title=None):
    fig = go.Figure()
    fig.update_layout(
        template="plotly_white",
        height=280,
        margin=dict(l=40, r=20, t=30, b=30),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        title=dict(text=title or "", x=0.01, y=0.98, xanchor="left"),
    )

    units = (POLLUTANTS.get(pollutant_label) or {}).get("Units") or "Value"
    fig.update_yaxes(title_text=f"{pollutant_display(pollutant_label)} ({units})")
    fig.update_xaxes(title_text="Time (AEST)")

    if aqms_history:
        xs = []
        ys = []
        for item in aqms_history:
            ts = _parse_aqms_time_point(item)
            if ts is None:
                continue
            value = item.get("Value")
            try:
                value = float(value) if value is not None else None
            except (TypeError, ValueError):
                value = None
            if value is None:
                continue
            xs.append(ts)
            ys.append(value)
        if xs:
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers", name="AQMS", line=dict(color="#2563eb", width=2)))

    if purpleair_history and purpleair_history.get("data"):
        fields = purpleair_history.get("fields") or []
        data = purpleair_history.get("data") or []
        time_idx = fields.index("time_stamp") if "time_stamp" in fields else 0
        field_name = "pm2.5_alt" if pollutant_label == "PM2.5" else "pm10.0_atm"
        if field_name in fields:
            val_idx = fields.index(field_name)
            xs = []
            ys = []
            for row in data:
                if not isinstance(row, list) or len(row) <= max(time_idx, val_idx):
                    continue
                ts = row[time_idx]
                value = row[val_idx]
                try:
                    ts = datetime.fromtimestamp(int(ts), tz=SYDNEY_TZ)
                except (TypeError, ValueError, OSError):
                    continue
                try:
                    value = float(value) if value is not None else None
                except (TypeError, ValueError):
                    value = None
                if value is None:
                    continue
                xs.append(ts)
                ys.append(value)
            if xs:
                fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name="PurpleAir", line=dict(color=PURPLEAIR_COLOR, width=2, dash="dot")))

    if not fig.data:
        fig.add_annotation(
            text="Select a site on the map or table to view recent trends.",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font=dict(family=BASE_FONT_FAMILY, size=14, color="#475569"),
        )
    return fig


def _monitor_strip_figure(pollutant_label, history_items, window_hours, title=None):
    fig = go.Figure()
    fig.update_layout(
        template="plotly_white",
        height=90,
        margin=dict(l=8, r=8, t=18, b=8),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        title=dict(text=title or pollutant_label, x=0.01, y=0.98, xanchor="left", font=dict(size=12)),
    )
    cutoff = datetime.now(SYDNEY_TZ) - timedelta(hours=window_hours)
    points = []
    for item in history_items or []:
        ts = _parse_aqms_time_point(item)
        if ts is None or ts < cutoff:
            continue
        value = item.get("Value")
        try:
            value_num = float(value) if value is not None else None
        except (TypeError, ValueError):
            value_num = None
        if value_num is None:
            continue
        points.append((ts, value_num))
    points.sort(key=lambda x: x[0])
    if not points:
        fig.add_annotation(
            text="No data",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font=dict(family=BASE_FONT_FAMILY, size=11, color="#64748b"),
        )
        fig.update_xaxes(visible=False)
        fig.update_yaxes(visible=False)
        return fig

    times = [p[0] for p in points]
    values = [p[1] for p in points]
    colours = [category_for_value(pollutant_label, v)[1] for v in values]
    fig.add_trace(
        go.Bar(
            x=times,
            y=[1] * len(times),
            marker=dict(color=colours),
            customdata=[f"{v:.1f}" for v in values],
            hovertemplate="%{x}<br>Value: %{customdata}<extra></extra>",
            showlegend=False,
        )
    )
    fig.update_xaxes(showgrid=False, tickfont=dict(size=9), tickangle=0)
    fig.update_yaxes(visible=False, range=[0, 1.2])
    return fig


@app.callback(
    Output("selected-monitor-site-store", "data"),
    [
        Input("url", "hash"),
        Input("monitor-station", "value"),
        Input("monitor-table", "active_cell"),
        Input("monitor-table", "data"),
        Input("monitor-store", "data"),
    ],
    [State("selected-monitor-site-store", "data")],
)
def update_selected_monitor_site(url_hash, station_key, active_cell, table_data, monitor_store, current_selected):
    selection = _monitor_from_hash(url_hash)
    if selection:
        return selection

    picked = _monitor_from_key(station_key)
    if picked:
        return picked

    if active_cell and isinstance(table_data, list):
        try:
            row = table_data[int(active_cell.get("row"))]
        except (TypeError, ValueError, IndexError):
            row = None
        if isinstance(row, dict):
            picked = _monitor_from_key(row.get("monitorKey"))
            if picked:
                return picked

    if current_selected:
        return current_selected

    return _default_monitor_selection(monitor_store)


@app.callback(
    [
        Output("monitor-selected-site", "children"),
        Output("monitor-compare-chart", "figure"),
        Output("monitor-compare-summary", "children"),
        Output("monitor-trend-chart", "figure"),
    ],
    [
        Input("selected-monitor-site-store", "data"),
        Input("monitor-store", "data"),
        Input("monitor-window", "value"),
        Input("monitor-pollutant", "value"),
    ],
)
def render_monitor_selected(selection, monitor_store, window_value, pollutant_label):
    monitor_store = monitor_store or {}
    pollutant_label = (pollutant_label or "PM2.5").strip()
    selection = selection or _default_monitor_selection(monitor_store) or {}
    source = selection.get("source")
    selection_id = selection.get("id")

    aqms_rows = monitor_store.get("aqmsRows") or []
    pa_rows = monitor_store.get("purpleairSensors") or []

    selected_row = None
    if source == "AQMS" and selection_id is not None:
        for row in aqms_rows:
            if str(row.get("site_id")) == str(selection_id):
                selected_row = row
                break
    elif source == "PurpleAir" and selection_id is not None:
        for row in pa_rows:
            if str(row.get("site_id")) == str(selection_id):
                selected_row = row
                break

    if not selected_row:
        empty = html.Div("Select a site on the map or table.", className="selected-site__empty")
        fig = _monitor_trend_figure(pollutant_label)
        return empty, fig, "", fig

    now = datetime.now(SYDNEY_TZ)
    window_value = str(window_value or "24h").strip().lower()
    window_hours = 24
    if window_value == "6h":
        window_hours = 6
    elif window_value == "48h":
        window_hours = 48
    elif window_value == "7d":
        window_hours = 7 * 24
    # Historical API uses date boundaries; pad by 1 day to ensure coverage.
    start_date = (now.date() - timedelta(days=max(1, int(window_hours / 24) + 1))).isoformat()
    end_date = (now.date() + timedelta(days=1)).isoformat()

    compare_fig = go.Figure()
    compare_fig.update_layout(
        template="plotly_white",
        height=220,
        margin=dict(l=40, r=20, t=25, b=25),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    aqms_history = None
    aqms_histories = {}
    pa_history = None
    nearest_pa = None

    if source == "AQMS":
        site_id = selected_row.get("site_id")
        parameter_code = (POLLUTANTS.get(pollutant_label) or {}).get("ParameterCode") or pollutant_label
        mini_pollutants = ["PM2.5", "PM10", "O3", "NO2"]
        mini_codes = []
        for pl in mini_pollutants:
            code = (POLLUTANTS.get(pl) or {}).get("ParameterCode")
            if code and code not in mini_codes:
                mini_codes.append(code)
        if parameter_code and parameter_code not in mini_codes:
            mini_codes.append(parameter_code)
        try:
            raw_history = fetch_observation_history([int(site_id)], mini_codes, start_date, end_date, timeout=14)
            aqms_histories = {}
            for item in raw_history or []:
                param = (item.get("Parameter") or {}).get("ParameterCode")
                if not param:
                    continue
                aqms_histories.setdefault(param, []).append(item)
            aqms_history = aqms_histories.get(parameter_code) or []
        except Exception:
            aqms_history = []

        nearest_pa = nearest_purpleair_sensor(selected_row.get("lat"), selected_row.get("lon"), monitor_store.get("purpleairSensors") or [])
        if nearest_pa and pollutant_label in {"PM2.5", "PM10"}:
            end_ts = int(now.timestamp())
            start_ts = end_ts - window_hours * 3600
            fields = ["pm2.5_alt"] if pollutant_label == "PM2.5" else ["pm10.0_atm"]
            try:
                pa_history = fetch_purpleair_sensor_history(nearest_pa.get("site_id"), start_ts, end_ts, average=60, fields=fields, timeout=14)
            except Exception:
                pa_history = {"fields": [], "data": [], "error": "PurpleAir history unavailable."}

    if source == "PurpleAir":
        end_ts = int(now.timestamp())
        start_ts = end_ts - window_hours * 3600
        fields = ["pm2.5_alt"] if pollutant_label == "PM2.5" else ["pm10.0_atm"]
        try:
            pa_history = fetch_purpleair_sensor_history(int(selected_row.get("site_id")), start_ts, end_ts, average=60, fields=fields, timeout=14)
        except Exception:
            pa_history = {"fields": [], "data": [], "error": "PurpleAir history unavailable."}

    # Filter down to the selected time window for plotting.
    cutoff = now - timedelta(hours=window_hours)
    aqms_history_filtered = []
    for item in aqms_history or []:
        ts = _parse_aqms_time_point(item)
        if ts is None or ts < cutoff:
            continue
        aqms_history_filtered.append(item)
    trend_fig = _monitor_trend_figure(pollutant_label, aqms_history=aqms_history_filtered, purpleair_history=pa_history, title=f"Last {window_hours}h")

    def _aqms_stats(items):
        points = []
        for item in items or []:
            ts = _parse_aqms_time_point(item)
            if ts is None:
                continue
            value = item.get("Value")
            try:
                value = float(value) if value is not None else None
            except (TypeError, ValueError):
                value = None
            if value is None:
                continue
            points.append((ts, value))
        points.sort(key=lambda x: x[0])
        if not points:
            return {}
        values = [v for _t, v in points]
        current = values[-1]
        prev = values[-2] if len(values) > 1 else None
        trend = "↔"
        if prev is not None:
            if current > prev:
                trend = "↑"
            elif current < prev:
                trend = "↓"
        return {
            "current": current,
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / float(len(values)) if values else None,
            "trend": trend,
        }

    stats = _aqms_stats(aqms_history_filtered)

    if aqms_history_filtered:
        xs = []
        ys = []
        compare_cutoff = now - timedelta(hours=min(6, window_hours))
        for item in aqms_history_filtered:
            ts = _parse_aqms_time_point(item)
            if ts is None or ts < compare_cutoff:
                continue
            value = item.get("Value")
            try:
                value = float(value) if value is not None else None
            except (TypeError, ValueError):
                value = None
            if value is None:
                continue
            xs.append(ts)
            ys.append(value)
        if xs:
            compare_fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines+markers", name="AQMS", line=dict(color="#2563eb", width=2)))

    if pa_history and pa_history.get("data"):
        fields = pa_history.get("fields") or []
        data = pa_history.get("data") or []
        time_idx = fields.index("time_stamp") if "time_stamp" in fields else 0
        field_name = "pm2.5_alt" if pollutant_label == "PM2.5" else "pm10.0_atm"
        if field_name in fields:
            val_idx = fields.index(field_name)
            xs = []
            ys = []
            compare_cutoff = now - timedelta(hours=min(6, window_hours))
            for row in data:
                if not isinstance(row, list) or len(row) <= max(time_idx, val_idx):
                    continue
                try:
                    ts = datetime.fromtimestamp(int(row[time_idx]), tz=SYDNEY_TZ)
                except (TypeError, ValueError, OSError):
                    continue
                if ts < compare_cutoff:
                    continue
                try:
                    value = float(row[val_idx]) if row[val_idx] is not None else None
                except (TypeError, ValueError):
                    value = None
                if value is None:
                    continue
                xs.append(ts)
                ys.append(value)
            if xs:
                name = "PurpleAir (nearest)" if nearest_pa else "PurpleAir"
                compare_fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name=name, line=dict(color=PURPLEAIR_COLOR, width=2, dash="dot")))

    if not compare_fig.data:
        compare_fig.add_annotation(
            text="No nearby PurpleAir series available for comparison.",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font=dict(family=BASE_FONT_FAMILY, size=13, color="#475569"),
        )

    compare_summary = ""
    if compare_fig.data and len(compare_fig.data) >= 2:
        # Compute simple agreement stats over the compare window.
        def _series_from_trace(trace):
            xs = list(trace.get("x") or [])
            ys = list(trace.get("y") or [])
            out = {}
            for x, y in zip(xs, ys):
                if x is None or y is None:
                    continue
                try:
                    # Plotly may serialize datetimes as strings.
                    if isinstance(x, str):
                        dt = date_parser.parse(x)
                    else:
                        dt = x
                    dt = dt.replace(minute=0, second=0, microsecond=0)
                except Exception:
                    continue
                out[dt] = float(y)
            return out

        aqms_series = _series_from_trace(compare_fig.data[0])
        pa_series = _series_from_trace(compare_fig.data[1])
        common = sorted(set(aqms_series.keys()) & set(pa_series.keys()))
        diffs = [(pa_series[t] - aqms_series[t]) for t in common if t in aqms_series and t in pa_series]
        if diffs:
            bias = sum(diffs) / float(len(diffs))
            abs_diffs = [abs(d) for d in diffs]
            # Agreement: within +/-5 (PM2.5) or +/-10 (PM10); default to +/-5.
            tol = 5.0
            if pollutant_label == "PM10":
                tol = 10.0
            agreement = 100.0 * sum(1 for d in abs_diffs if d <= tol) / float(len(abs_diffs))
            # Correlation (Pearson).
            xs = [aqms_series[t] for t in common]
            ys = [pa_series[t] for t in common]
            corr = None
            if len(xs) >= 3:
                mx = sum(xs) / float(len(xs))
                my = sum(ys) / float(len(ys))
                num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                denx = sum((x - mx) ** 2 for x in xs)
                deny = sum((y - my) ** 2 for y in ys)
                if denx > 0 and deny > 0:
                    corr = num / ((denx ** 0.5) * (deny ** 0.5))
            units = (POLLUTANTS.get(pollutant_label) or {}).get("Units") or ""
            compare_summary = html.Div(
                [
                    html.Div([html.Strong(f"PurpleAir agreement: {agreement:.0f}%")]),
                    html.Div([f"Bias: {bias:+.1f} {units}".strip()]),
                    html.Div([f"Mean abs diff: {sum(abs_diffs)/float(len(abs_diffs)):.1f} {units}".strip()]),
                    html.Div([f"Correlation: {corr:.2f}"] if corr is not None else "Correlation: --"),
                ],
                className="monitor-compare-summary__body",
            )

    details = [
        html.Div([html.Span("Station", className="selected-label"), html.Span(selected_row.get("station") or "--", className="selected-value")], className="selected-row"),
        html.Div([html.Span("Region", className="selected-label"), html.Span(selected_row.get("region") or "--", className="selected-value")], className="selected-row"),
        html.Div([html.Span("Source", className="selected-label"), html.Span(source or "--", className="selected-value")], className="selected-row"),
    ]
    if stats:
        units = (POLLUTANTS.get(pollutant_label) or {}).get("Units") or ""
        details.append(
            html.Div(
                [
                    html.Span(f"{pollutant_label} ({window_hours}h)", className="selected-label"),
                    html.Span(
                        f"{stats.get('trend','')} cur {stats.get('current'):.1f}  min {stats.get('min'):.1f}  max {stats.get('max'):.1f}  avg {stats.get('avg'):.1f} {units}".strip(),
                        className="selected-value",
                    ),
                ],
                className="selected-row",
            )
        )
    # Multi-pollutant snapshot (AQMS only).
    if source == "AQMS":
        snapshot = (monitor_store.get("aqmsSnapshotBySite") or {}).get(int(selected_row.get("site_id") or 0), {})
        aqc_row = snapshot.get("AQC") or {}
        if aqc_row:
            details.append(
                html.Div(
                    [html.Span("AQI category", className="selected-label"), html.Span(aqc_row.get("category") or "No data", className="selected-value")],
                    className="selected-row",
                )
            )
        for pl in ["PM2.5", "PM10", "O3", "NO2", "SO2", "CO"]:
            row = snapshot.get(pl) or {}
            details.append(
                html.Div(
                    [
                        html.Span(pl, className="selected-label"),
                        html.Span(row.get("value_label") or "--", className="selected-value"),
                        html.Span(row.get("category") or "No data", className="selected-subvalue"),
                    ],
                    className="selected-row selected-row--triple",
                )
            )
    else:
        details.extend(
            [
                html.Div([html.Span(pollutant_display(pollutant_label), className="selected-label"), html.Span(selected_row.get("value_label") or "--", className="selected-value")], className="selected-row"),
                html.Div([html.Span("Category", className="selected-label"), html.Span(selected_row.get("category") or "No data", className="selected-value")], className="selected-row"),
            ]
        )
    if source == "AQMS":
        details.append(
            html.Div(
                [
                    html.Span("Timestamp", className="selected-label"),
                    html.Span(
                        f"{selected_row.get('date') or '--'} {selected_row.get('hour_description') or ''}".strip(),
                        className="selected-value",
                    ),
                ],
                className="selected-row",
            )
        )
    if selected_row.get("lat") is not None and selected_row.get("lon") is not None:
        details.append(
            html.Div(
                [
                    html.Span("Coordinates", className="selected-label"),
                    html.Span(f"{selected_row.get('lat')}, {selected_row.get('lon')}", className="selected-value"),
                ],
                className="selected-row",
            )
        )
    if nearest_pa and source == "AQMS":
        details.append(
            html.Div(
                [
                    html.Span("Nearest PurpleAir", className="selected-label"),
                    html.Span(
                        f"{nearest_pa.get('station') or 'Sensor'} ({nearest_pa.get('distance_km', 0):.1f} km)",
                        className="selected-value",
                    ),
                ],
                className="selected-row",
            )
        )

    mini_strip_nodes = []
    if source == "AQMS" and aqms_histories:
        for pl in ["PM2.5", "PM10", "O3", "NO2"]:
            code = (POLLUTANTS.get(pl) or {}).get("ParameterCode")
            items = aqms_histories.get(code) if code else None
            mini_strip_nodes.append(
                dcc.Graph(
                    figure=_monitor_strip_figure(pl, items or [], window_hours, title=pl),
                    config={"displayModeBar": False},
                    className="monitor-mini-strip",
                )
            )

    selected_children = [
        html.Div(details, className="selected-site__body"),
    ]
    if mini_strip_nodes:
        selected_children.append(html.Div(mini_strip_nodes, className="monitor-mini-strips"))

    return html.Div(selected_children, className="selected-site__wrap"), compare_fig, compare_summary, trend_fig


@app.callback(
    Output("forecast-map", "srcDoc"),
    [Input("forecast-store", "data"), Input("forecast-map-pollutant-dropdown", "value")],
    [
        State("pollutant-dropdown", "value"),
        State("time-dropdown", "value"),
        State("model-dropdown", "value"),
        State("date-dropdown", "value"),
        State("time-slider", "value"),
    ],
)
def render_forecast_map_srcdoc(parsed, map_pollutant, pollutant, time_scope, model, date, hour_index):
    map_pollutant = map_pollutant or pollutant
    selection = {
        "pollutants": map_pollutant,
        "timeScopes": time_scope,
        "models": model,
        "date": date,
    }
    map_payload = _merged_forecast_map_payload(selection, map_pollutant)
    if not ((map_payload.get("data") or {}).get("stations") or {}):
        map_payload = parsed or {}
    if not map_payload:
        return _leaflet_forecast_map_html({}, map_pollutant, 0)
    # Keep the same iframe document while the slider moves; values update via postMessage.
    times = (map_payload.get("data") or {}).get("time", {}).get("forecastTime", [])
    if hour_index is None:
        index, _has_upcoming = _forecast_live_base_state(times)
    else:
        try:
            index = int(hour_index)
        except (TypeError, ValueError):
            index = 0
        if times:
            index = max(0, min(index, len(times) - 1))
    return _leaflet_forecast_map_html(map_payload, map_pollutant, index)


@app.callback(
    Output("monitor-modal", "style"),
    [Input("monitor-view-all", "n_clicks"), Input("monitor-modal-close", "n_clicks")],
    [State("monitor-modal", "style")],
)
def toggle_monitor_modal(open_clicks, close_clicks, current_style):
    current_style = current_style or {}
    is_open = current_style.get("display") not in (None, "", "none")
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if trigger == "monitor-view-all" and open_clicks:
        return {"display": "flex"}
    if trigger == "monitor-modal-close" and close_clicks:
        return {"display": "none"}
    return {"display": "flex"} if is_open else {"display": "none"}


@app.callback(
    [
        Output("ranking-chart", "children"),
        Output("station-ranking-title", "children"),
        Output("summary-cards", "children"),
        Output("forecast-time-label", "children"),
        Output("file-info", "children"),
    ],
    [Input("forecast-store", "data"), Input("time-slider", "value")],
    [
        State("region-dropdown", "value"),
        State("pollutant-dropdown", "value"),
        State("time-dropdown", "value"),
        State("model-dropdown", "value"),
        State("date-dropdown", "value"),
    ],
)
def render_main_visuals(parsed, hour_index, region, pollutant, time_scope, model, date):
    selection = {
        "regions": region,
        "pollutants": pollutant,
        "timeScopes": time_scope,
        "models": model,
        "date": date,
    }
    if not parsed:
        return _station_rank_bars_for_pollutants({}, selection, hour_index or 0), "Station's Nowcasting", _make_summary_cards({}, selection, 0), "Latest forecast run not loaded.", ""

    ranking = _station_rank_bars_for_pollutants(parsed, selection, hour_index or 0)
    summary = _make_summary_cards(parsed, selection, hour_index or 0, None)
    times = parsed.get("data", {}).get("time", {}).get("forecastTime", [])
    current_time = _selected_time_label(parsed, hour_index or 0)
    time_label = _format_forecast_label(current_time)
    file_text = [
        html.Div(["Forecast file: ", html.Code(Path(parsed.get("_sourceFile", "Unknown")).name)]),
        html.Div(["Pollutant: ", html.Strong(pollutant_display(pollutant))]),
        html.Div(["Stations parsed: ", html.Strong(str(len(parsed.get("data", {}).get("stations", {}))))]),
        html.Div(["Current time: ", html.Strong(time_label)]),
        html.Div(["Steps available: ", html.Strong(str(len(times)))]),
    ]
    return ranking, f"Station's Nowcasting @ {time_label}", summary, time_label, file_text


@app.callback(
    [Output("station-trend", "figure"), Output("station-details", "children")],
    [Input("forecast-store", "data"), Input("selected-station-store", "data"), Input("url", "hash"), Input("time-slider", "value")],
    [
        State("pollutant-dropdown", "value"),
        State("time-dropdown", "value"),
        State("model-dropdown", "value"),
        State("date-dropdown", "value"),
    ],
)
def render_station_trend(parsed, selected_station, url_hash, hour_index, pollutant, time_scope, model, date):
    if not parsed:
        figure = go.Figure()
        figure.update_layout(template="plotly_white", height=320, margin=dict(l=20, r=20, t=30, b=20))
        return figure, "Forecast data is not loaded."

    selection = {"pollutants": pollutant, "timeScopes": time_scope, "models": model, "date": date}
    all_region_parsed = None
    selected_station = selected_station or _station_from_hash(parsed, url_hash)
    if not selected_station and url_hash:
        all_region_parsed = _merged_forecast_map_payload(selection, pollutant)
        selected_station = _station_from_hash(all_region_parsed, url_hash)
    if not selected_station:
        return _all_stations_trend_figure(parsed, pollutant, time_scope), _all_stations_details(parsed)

    chart_parsed = parsed
    if normalize_station_name(selected_station) not in {normalize_station_name(name) for name in ((parsed.get("data") or {}).get("stations") or {})}:
        chart_parsed = all_region_parsed or _merged_forecast_map_payload(selection, pollutant)
    figure = _forecast_time_series_figure(chart_parsed, pollutant, selected_station, hour_index or 0)
    stations = (chart_parsed.get("data") or {}).get("stations", {})
    payload = None
    target = normalize_station_name(selected_station)
    for key, value in stations.items():
        if normalize_station_name(key) == target:
            payload = value
            break
    values = payload.get("forecastValue", []) if payload else []
    details = [
        html.Div(["Station: ", html.Strong(title_case_station_name(selected_station))]),
        html.Div(["Forecast steps: ", html.Strong(str(len(values)))]),
    ]
    if values:
        details.append(html.Div(["Latest value: ", html.Strong(f"{values[-1]:.2f}")]))
    return figure, details


@app.callback(
    [
        Output("forecast-time-series-station", "options"),
        Output("forecast-time-series-station", "value"),
        Output("forecast-time-series-title", "children"),
    ],
    [
        Input("forecast-store", "data"),
        Input("region-dropdown", "value"),
        Input("selected-station-store", "data"),
        Input("url", "hash"),
    ],
    [State("forecast-time-series-station", "value")],
)
def sync_forecast_time_series_station(parsed, region, selected_station, url_hash, current_station):
    title = f"Nowcasting Time Series @ {_display_region_label(region)}"
    options = _forecast_station_dropdown_options(parsed)
    if not options:
        return [], None, title

    stations = ((parsed or {}).get("data") or {}).get("stations") or {}
    valid_by_normalized = {normalize_station_name(option["value"]): option["value"] for option in options}
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    hash_station = _station_from_hash(parsed, url_hash)

    if trigger in ("selected-station-store", "url"):
        candidates = [selected_station, hash_station, current_station]
    else:
        candidates = [current_station, selected_station, hash_station]

    for candidate in candidates:
        normalized = normalize_station_name(candidate)
        if normalized in valid_by_normalized:
            return options, valid_by_normalized[normalized], title

    times = ((parsed or {}).get("data") or {}).get("time", {}).get("forecastTime", [])
    default_station = _best_station_for_hour(parsed, _forecast_live_base_index(times))
    normalized_default = normalize_station_name(default_station)
    if normalized_default in valid_by_normalized:
        return options, valid_by_normalized[normalized_default], title

    first_value = options[0]["value"]
    if first_value in stations:
        return options, first_value, title
    return options, first_value, title


@app.callback(
    Output("forecast-time-series", "figure"),
    [
        Input("forecast-store", "data"),
        Input("time-slider", "value"),
        Input("forecast-time-series-station", "value"),
        Input("selected-station-store", "data"),
        Input("url", "hash"),
    ],
    [
        State("pollutant-dropdown", "value"),
        State("time-dropdown", "value"),
        State("model-dropdown", "value"),
        State("date-dropdown", "value"),
    ],
)
def render_forecast_time_series(parsed, hour_index, time_series_station, selected_station, url_hash, pollutant, time_scope, model, date):
    if not parsed:
        figure = go.Figure()
        figure.update_layout(template="plotly_white", height=300, margin=dict(l=20, r=20, t=40, b=20))
        return figure

    selection = {"pollutants": pollutant, "timeScopes": time_scope, "models": model, "date": date}
    all_region_parsed = None
    selected_station = time_series_station or selected_station or _station_from_hash(parsed, url_hash)
    if not selected_station and url_hash:
        all_region_parsed = _merged_forecast_map_payload(selection, pollutant)
        selected_station = _station_from_hash(all_region_parsed, url_hash)
    chart_parsed = parsed
    if selected_station and normalize_station_name(selected_station) not in {normalize_station_name(name) for name in ((parsed.get("data") or {}).get("stations") or {})}:
        chart_parsed = all_region_parsed or _merged_forecast_map_payload(selection, pollutant)
    return _forecast_time_series_figure(chart_parsed, pollutant, selected_station, hour_index or 0)


@app.callback(
    Output("selected-station-store", "data"),
    [Input("forecast-store", "data"), Input("url", "hash")],
)
def update_selected_station(parsed, url_hash):
    if parsed:
        selected_station = _station_from_hash(parsed, url_hash)
        if selected_station:
            return selected_station
    return None


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", "8050")))
