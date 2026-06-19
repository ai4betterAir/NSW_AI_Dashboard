"""Dash dashboard that mirrors the core React forecast view."""

import json
import math
import os
import re
import sys
import threading
from html import escape
from pathlib import Path
from datetime import datetime, timedelta
from functools import lru_cache
from urllib.parse import unquote

import dash
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from dash import Input, Output, State, callback_context, dcc, html, no_update, dash_table, MATCH
from flask import Response, send_from_directory
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
    _OBS_CACHE_CSV,
    _load_observations_cache,
    _load_purpleair_snapshot_cache,
    fetch_observation_history,
    fetch_observations,
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
FILE_KEY_PARAMS = get_file_key_params()

GENERAL_HEALTH_GUIDE = []
MONITOR_REFRESH_MS = 20 * 60 * 1000
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
    if LEAFLET_TILE_URLS:
        parts = [part.strip() for part in LEAFLET_TILE_URLS.split(",") if part.strip()]
        if parts:
            url = parts[0]
    return json.dumps(url)


def _leaflet_tile_urls_js():
    """Return a JS array literal of tile URLs."""
    urls = []
    if LEAFLET_TILE_URLS:
        parts = [part.strip() for part in LEAFLET_TILE_URLS.split(",") if part.strip()]
        urls.extend(parts)
    if not urls and LEAFLET_TILE_URL:
        urls.append(LEAFLET_TILE_URL)
    return json.dumps(urls)


def _site_lookup():
    return load_site_lookup()


@lru_cache(maxsize=1)
def _region_lookup():
    return load_region_lookup()


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
_LAST_GOOD_MONITOR_MAP_ROWS = []
_LAST_GOOD_MONITOR_MAP_HTML = None
_LAST_GOOD_FORECAST_MAP_HTML = None
_LAST_GOOD_OVERVIEW_FORECAST_MAP_HTML = None
# Attempt to seed from disk on import so arrows survive restarts
try:
    disk_snap = _load_last_aqms_snapshot_from_disk()
    if disk_snap and (disk_snap.get("sites") or {}):
        _LAST_AQMS_SNAPSHOT = disk_snap
except Exception:
    pass
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
        file_path = forecast_file_path(
            [
                candidate.get("regions"),
                candidate.get("pollutants"),
                candidate.get("timeScopes"),
                candidate.get("models"),
                candidate.get("date"),
            ]
        )
        parsed_date = _parse_run_date(candidate.get("date")) or datetime.min
        file_mtime = os.path.getmtime(file_path) if file_path and os.path.exists(file_path) else 0
        return parsed_date, file_mtime, str(candidate.get("models") or ""), str(candidate.get("timeScopes") or "")

    return max(FILE_KEY_PARAMS, key=sort_key)


LATEST_SELECTION = _latest_file_params() or {}

DEFAULT_SELECTION = {
    "regions": LATEST_SELECTION.get("regions", _default_value("regions")),
    "pollutants": LATEST_SELECTION.get("pollutants", _default_value("pollutants")),
    "timeScopes": LATEST_SELECTION.get("timeScopes", _default_value("timeScopes")),
    "models": LATEST_SELECTION.get("models", _default_value("models")),
    "date": LATEST_SELECTION.get("date", _default_value("date")),
}


FORECAST_FIELD_KEYS = {
    "date": "date",
    "models": "models",
    "pollutants": "pollutants",
    "timeScopes": "timeScopes",
    "regions": "regions",
}


def _complete_forecast_file_params():
    return [
        candidate
        for candidate in FILE_KEY_PARAMS
        if all(candidate.get(key) for key in ("date", "models", "pollutants", "timeScopes", "regions"))
    ]


def _forecast_params_for(date=None, model=None, pollutant=None, horizon=None, region=None):
    records = []
    for candidate in _complete_forecast_file_params():
        if date and str(candidate.get("date") or "") != str(date):
            continue
        if model and str(candidate.get("models") or "") != str(model):
            continue
        if pollutant and str(candidate.get("pollutants") or "") != str(pollutant):
            continue
        if horizon and str(candidate.get("timeScopes") or "") != str(horizon):
            continue
        if region and str(candidate.get("regions") or "") != str(region):
            continue
        records.append(candidate)
    return records


def _sort_validation_dates(values):
    return sorted(
        {str(value) for value in values if value},
        key=lambda value: (_parse_run_date(value) or datetime.min, value),
        reverse=True,
    )


def _validation_date_options():
    return [{"label": value, "value": value} for value in _sort_validation_dates(kp.get("date") for kp in _complete_forecast_file_params())]


def _validation_pollutant_options(date=None):
    values = sorted({str(kp.get("pollutants") or "") for kp in _forecast_params_for(date=date) if kp.get("pollutants")})
    return [{"label": pollutant_display(value), "value": value} for value in values]


def _validation_horizon_options(date=None, pollutant=None):
    def sort_key(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 10**6

    values = sorted(
        {str(kp.get("timeScopes") or "") for kp in _forecast_params_for(date=date, pollutant=pollutant) if kp.get("timeScopes")},
        key=sort_key,
    )
    return [{"label": f"{value} hours", "value": value} for value in values]


def _validation_region_options(date=None, pollutant=None, horizon=None):
    values = sorted(
        {str(kp.get("regions") or "") for kp in _forecast_params_for(date=date, pollutant=pollutant, horizon=horizon) if kp.get("regions")}
    )
    return [{"label": value.replace("_", " "), "value": value} for value in values]


def _validation_pick_value(options, current_value=None):
    values = [option.get("value") for option in options or []]
    if current_value in values:
        return current_value
    return values[0] if values else None


def _validation_model_for_selection(date=None, pollutant=None, horizon=None, region=None):
    matches = _forecast_params_for(date=date, pollutant=pollutant, horizon=horizon, region=region)
    if not matches:
        return DEFAULT_SELECTION.get("models")
    preferred = str(DEFAULT_SELECTION.get("models") or "")
    for candidate in matches:
        model = str(candidate.get("models") or "")
        if model == preferred:
            return model
    return sorted(str(candidate.get("models") or "") for candidate in matches if candidate.get("models"))[0]


def _forecast_sort_values(field, values):
    values = {str(value) for value in values if value}
    if field == "date":
        return _sort_validation_dates(values)
    if field == "timeScopes":
        def horizon_key(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 10**6

        return sorted(values, key=horizon_key)
    return sorted(values)


def _forecast_option_label(field, value):
    value = str(value)
    if field == "pollutants":
        return pollutant_display(value)
    if field == "timeScopes":
        return f"{value} hours"
    if field == "regions":
        return value.replace("_", " ")
    return value


def _forecast_options_for_field(field, selection):
    records = _complete_forecast_file_params()
    for other_field, key in FORECAST_FIELD_KEYS.items():
        if other_field == field:
            continue
        value = selection.get(other_field)
        if value:
            records = [record for record in records if str(record.get(key) or "") == str(value)]
    values = _forecast_sort_values(field, (record.get(FORECAST_FIELD_KEYS[field]) for record in records))
    return [{"label": _forecast_option_label(field, value), "value": value} for value in values]


def _forecast_selection_is_available(selection):
    return bool(
        _forecast_params_for(
            date=selection.get("date"),
            model=selection.get("models"),
            pollutant=selection.get("pollutants"),
            horizon=selection.get("timeScopes"),
            region=selection.get("regions"),
        )
    )


def _normalise_forecast_selection(selection, priority_field=None):
    records = _complete_forecast_file_params()
    if not records:
        return dict(selection or {})

    selected = {field: (selection or {}).get(field) for field in FORECAST_FIELD_KEYS}
    priority_value = selected.get(priority_field) if priority_field else None
    priority_matches = []
    if priority_field and priority_value:
        priority_matches = [
            record
            for record in records
            if str(record.get(FORECAST_FIELD_KEYS[priority_field]) or "") == str(priority_value)
        ]
    pool = priority_matches or records

    def score(record):
        matched = 0
        for field, key in FORECAST_FIELD_KEYS.items():
            value = selected.get(field)
            if value and str(record.get(key) or "") == str(value):
                matched += 1
        parsed_date = _parse_run_date(record.get("date")) or datetime.min
        try:
            horizon_value = int(record.get("timeScopes") or 0)
        except (TypeError, ValueError):
            horizon_value = 0
        return (
            1 if priority_matches else 0,
            matched,
            parsed_date,
            horizon_value,
            str(record.get("models") or ""),
            str(record.get("pollutants") or ""),
            str(record.get("regions") or ""),
        )

    chosen = max(pool, key=score)
    return {
        "date": str(chosen.get("date") or ""),
        "models": str(chosen.get("models") or ""),
        "pollutants": str(chosen.get("pollutants") or ""),
        "timeScopes": str(chosen.get("timeScopes") or ""),
        "regions": str(chosen.get("regions") or ""),
    }


INITIAL_FORECAST_SELECTION = _normalise_forecast_selection(DEFAULT_SELECTION, priority_field="date")
DEFAULT_SELECTION.update(
    {
        "date": INITIAL_FORECAST_SELECTION.get("date") or DEFAULT_SELECTION.get("date"),
        "models": INITIAL_FORECAST_SELECTION.get("models") or DEFAULT_SELECTION.get("models"),
        "pollutants": INITIAL_FORECAST_SELECTION.get("pollutants") or DEFAULT_SELECTION.get("pollutants"),
        "timeScopes": INITIAL_FORECAST_SELECTION.get("timeScopes") or DEFAULT_SELECTION.get("timeScopes"),
        "regions": INITIAL_FORECAST_SELECTION.get("regions") or DEFAULT_SELECTION.get("regions"),
    }
)
INITIAL_FORECAST_DATE_OPTIONS = _forecast_options_for_field("date", INITIAL_FORECAST_SELECTION)
INITIAL_FORECAST_MODEL_OPTIONS = _forecast_options_for_field("models", INITIAL_FORECAST_SELECTION)
INITIAL_FORECAST_POLLUTANT_OPTIONS = _forecast_options_for_field("pollutants", INITIAL_FORECAST_SELECTION)
INITIAL_FORECAST_HORIZON_OPTIONS = _forecast_options_for_field("timeScopes", INITIAL_FORECAST_SELECTION)
INITIAL_FORECAST_REGION_OPTIONS = _forecast_options_for_field("regions", INITIAL_FORECAST_SELECTION)


INITIAL_VALIDATION_DATE_OPTIONS = _validation_date_options()
INITIAL_VALIDATION_DATE = _validation_pick_value(INITIAL_VALIDATION_DATE_OPTIONS, DEFAULT_SELECTION.get("date"))
INITIAL_VALIDATION_POLLUTANT_OPTIONS = _validation_pollutant_options(INITIAL_VALIDATION_DATE)
INITIAL_VALIDATION_POLLUTANT = _validation_pick_value(INITIAL_VALIDATION_POLLUTANT_OPTIONS, DEFAULT_SELECTION.get("pollutants"))
INITIAL_VALIDATION_HORIZON_OPTIONS = _validation_horizon_options(INITIAL_VALIDATION_DATE, INITIAL_VALIDATION_POLLUTANT)
INITIAL_VALIDATION_HORIZON = _validation_pick_value(INITIAL_VALIDATION_HORIZON_OPTIONS, DEFAULT_SELECTION.get("timeScopes"))
INITIAL_VALIDATION_REGION_OPTIONS = _validation_region_options(INITIAL_VALIDATION_DATE, INITIAL_VALIDATION_POLLUTANT, INITIAL_VALIDATION_HORIZON)
INITIAL_VALIDATION_REGION = _validation_pick_value(INITIAL_VALIDATION_REGION_OPTIONS, DEFAULT_SELECTION.get("regions"))


# Defaults for monitoring dropdowns (avoid import-time NameErrors)
MONITOR_REGION_OPTIONS = [
    {"label": "All NSW", "value": "ALL"},
]
for r in sorted(set(OPTIONS.get("regions") or [])):
    if str(r).upper() != "ALL":
        MONITOR_REGION_OPTIONS.append({"label": str(r), "value": str(r)})

MONITOR_DEFAULT_REGION = "ALL"
_MONITOR_SITE_REGIONS = {
    str((site or {}).get("Region") or "").strip()
    for site in load_sites()
    if str((site or {}).get("Region") or "").strip()
}
if MONITOR_DEFAULT_REGION not in _MONITOR_SITE_REGIONS:
    MONITOR_DEFAULT_REGION = "ALL"
elif MONITOR_DEFAULT_REGION not in {opt.get("value") for opt in MONITOR_REGION_OPTIONS}:
    MONITOR_REGION_OPTIONS.append({"label": MONITOR_DEFAULT_REGION, "value": MONITOR_DEFAULT_REGION})

MONITOR_POLLUTANT_OPTIONS = [
    {"label": "PM2.5", "value": "PM2.5"},
    {"label": "PM10", "value": "PM10"},
    {"label": "O3", "value": "O3"},
]

MONITOR_REGION_SERIES_POLLUTANTS = ["PM2.5", "PM10", "O3"]
MONITOR_REGION_SERIES_COLORS = [
    "#2563eb",
    "#7c3aed",
    "#0f766e",
    "#ea580c",
    "#db2777",
    "#16a34a",
    "#ca8a04",
    "#4f46e5",
    "#0891b2",
    "#dc2626",
    "#475569",
]
MONITOR_REGION_SERIES_OPTIONS = [
    {"label": title_case_station_name(site_region), "value": str(site_region).replace(" ", "_")}
    for site_region in sorted(
        {
            str((site or {}).get("Region") or "").strip()
            for site in load_sites()
            if str((site or {}).get("Region") or "").strip()
            and "offline" not in str((site or {}).get("Region") or "").strip().lower()
        }
    )
]
MONITOR_REGION_SERIES_PREFERRED_DEFAULT = "Sydney_East"
MONITOR_REGION_SERIES_DEFAULT_VALUE = MONITOR_REGION_SERIES_PREFERRED_DEFAULT
if MONITOR_REGION_SERIES_DEFAULT_VALUE not in {opt.get("value") for opt in MONITOR_REGION_SERIES_OPTIONS}:
    MONITOR_REGION_SERIES_DEFAULT_VALUE = (MONITOR_REGION_SERIES_OPTIONS[0]["value"] if MONITOR_REGION_SERIES_OPTIONS else None)
MONITOR_REGION_SERIES_PLOT_COLORS = {
    "PM2.5": "#f59e0b",
    "PM10": "#0ea5e9",
    "O3": "#10b981",
}

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


def _forecast_datetime_sydney(value):
    parsed = _parse_forecast_timestamp(value)
    if not parsed:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return parsed.replace(tzinfo=SYDNEY_TZ)
    return parsed.astimezone(SYDNEY_TZ)


def _forecast_live_base_index(times, now=None):
    """Pick the forecast index that best matches the current Sydney hour."""
    if not times:
        return 0
    parsed_times = [_forecast_datetime_sydney(item) for item in times]
    indexed_times = [(idx, dt) for idx, dt in enumerate(parsed_times) if dt is not None]
    if not indexed_times:
        return 0

    now_dt = now or datetime.now(SYDNEY_TZ)
    if now_dt.tzinfo is None or now_dt.tzinfo.utcoffset(now_dt) is None:
        now_dt = now_dt.replace(tzinfo=SYDNEY_TZ)
    else:
        now_dt = now_dt.astimezone(SYDNEY_TZ)

    future_times = [(idx, dt) for idx, dt in indexed_times if dt >= now_dt]
    if future_times:
        return min(future_times, key=lambda item: item[1])[0]

    target_minutes = now_dt.hour * 60

    def minute_delta(item):
        _idx, dt = item
        candidate_minutes = dt.hour * 60 + dt.minute
        return (candidate_minutes - target_minutes) % (24 * 60)

    return min(indexed_times, key=minute_delta)[0]


def _forecast_hour_label(value):
    parsed = _forecast_datetime_sydney(value)
    if not parsed:
        return str(value or "--")
    hour = parsed.strftime("%I").lstrip("0") or "0"
    return f"{hour}{parsed.strftime('%p')}"


def _monitor_region_series_key(value):
    return re.sub(r"[^A-Z0-9]+", " ", str(value or "").upper()).strip()


def _monitor_region_series_station_options(region_value):
    region_key = _monitor_region_series_key(region_value)
    if not region_key:
        return []
    site_lookup = _sites_by_id()
    options = []
    for site_id, site in sorted(
        site_lookup.items(),
        key=lambda item: title_case_station_name(item[1].get("SiteName") or f"Site {item[0]}")
    ):
        if _monitor_region_series_key((site or {}).get("Region")) != region_key:
            continue
        station_name = title_case_station_name(site.get("SiteName") or f"Site {site_id}")
        options.append({"label": station_name, "value": f"aqms:{int(site_id)}"})
    return options


MONITOR_REGION_SERIES_DEFAULT_STATION_OPTIONS = _monitor_region_series_station_options(MONITOR_REGION_SERIES_DEFAULT_VALUE)
MONITOR_REGION_SERIES_DEFAULT_STATION_VALUE = (
    MONITOR_REGION_SERIES_DEFAULT_STATION_OPTIONS[0]["value"]
    if MONITOR_REGION_SERIES_DEFAULT_STATION_OPTIONS
    else None
)


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


MONITOR_MAP_POLLUTANTS = ("PM2.5", "PM10", "O3", "NO2", "NO", "NOX", "SO2", "CO")


def _monitor_category_severity(row):
    key = str((row or {}).get("category") or "").strip().lower()
    order = {"no data": -1, "good": 0, "fair": 1, "poor": 2, "very poor": 3, "extremely poor": 4}
    return order.get(key, -1)


def _monitor_display_value(row):
    if not row:
        return "--"
    value_label = row.get("value_label")
    if value_label not in (None, ""):
        return str(value_label)
    value = row.get("value")
    if value is None:
        return "--"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)


def _monitor_has_numeric_value(row):
    if not row:
        return False
    value = row.get("value")
    if value is None:
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _aqms_parameter(item):
    if not isinstance(item, dict):
        return {}
    param = item.get("Parameter")
    if isinstance(param, str):
        text = param.strip()
        if text:
            try:
                param = json.loads(text)
            except Exception:
                param = {}
        else:
            param = {}
    if not isinstance(param, dict):
        param = {}
    if not param.get("ParameterCode") and item.get("ParameterCode"):
        param = dict(param)
        param["ParameterCode"] = item.get("ParameterCode")
    if not param.get("ParameterDescription") and item.get("ParameterDescription"):
        param = dict(param)
        param["ParameterDescription"] = item.get("ParameterDescription")
    if not param.get("Frequency") and item.get("Frequency"):
        param = dict(param)
        param["Frequency"] = item.get("Frequency")
    return param


def _parse_aqms_time_point(item):
    item = item or {}
    date_text = item.get("Date") or item.get("date")
    hour = item.get("Hour") if item.get("Hour") is not None else item.get("hour")
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


def _monitor_latest_datetime(monitor_store):
    monitor_store = monitor_store or {}
    candidates = []
    for key in ("aqmsRows", "aqmsPm25Rows", "aqmsPm10Rows", "aqmsO3Rows", "aqcRows"):
        latest = _monitoring_time_row(monitor_store.get(key) or [])
        ts = _parse_aqms_time_point(latest) if latest else None
        if ts:
            candidates.append((ts, latest))
    if not candidates:
        return None, {}
    return max(candidates, key=lambda item: item[0])


def _monitor_data_age_hours(monitor_store):
    latest_dt, _latest = _monitor_latest_datetime(monitor_store)
    if not latest_dt:
        return None
    now = datetime.now(SYDNEY_TZ)
    if latest_dt.tzinfo is None:
        latest_dt = latest_dt.replace(tzinfo=SYDNEY_TZ)
    return (now - latest_dt.astimezone(SYDNEY_TZ)).total_seconds() / 3600.0


def _monitor_data_is_stale(monitor_store, max_age_hours=4):
    age_hours = _monitor_data_age_hours(monitor_store)
    if age_hours is None:
        return False
    return age_hours > float(max_age_hours)


def _monitor_time_window_label(row):
    if not row:
        return "--"
    hour_desc = row.get("hour_description") or "--"
    date_text = row.get("date") or ""
    if date_text:
        return f"{hour_desc} AEST, {date_text}"
    return f"{hour_desc} AEST"


def _monitor_updated_label(monitor_store):
    monitor_store = monitor_store or {}
    rows = monitor_store.get("aqmsPm25Rows") or []
    latest = _monitoring_time_row(rows)
    if not latest:
        error = monitor_store.get("error")
        if error:
            return "Updated on: -- (AQMS feed offline)"
        return "Updated on: -- (loading)"
    label = _monitor_time_window_label(latest)
    if _monitor_data_is_stale(monitor_store):
        return f"Last cached: {label} (waiting for live AQMS)"
    return f"Updated on: {label}"


def _monitor_header_timestamp_label(monitor_store):
    latest_dt, _latest = _monitor_latest_datetime(monitor_store)
    if latest_dt:
        return latest_dt.astimezone(SYDNEY_TZ).strftime("%d %b %H:%M %Z")
    epoch = (monitor_store or {}).get("fetchedAtEpoch")
    return _format_header_timestamp(epoch) if epoch is not None else "--"


def _aqms_station_map_rows(row_groups, selected_pollutant="PM2.5"):
    """Build AQMS map markers for stations with available selected pollutant data."""
    row_groups = row_groups or {}
    selected_pollutant = str(selected_pollutant or "PM2.5").strip() or "PM2.5"

    rows_by_site = {}
    sites = _sites_by_id()
    site_ids = set()
    for pollutant_label, rows in row_groups.items():
        for row in rows or []:
            site_id = row.get("site_id")
            if site_id is None:
                continue
            try:
                site_id = int(site_id)
            except (TypeError, ValueError):
                continue
            site_ids.add(site_id)
            rows_by_site.setdefault(site_id, {})[pollutant_label] = row

    out = []
    for site_id in sorted(site_ids):
        site = sites.get(int(site_id), {})
        site_rows = rows_by_site.get(int(site_id), {})
        any_row = next((row for row in site_rows.values() if row), {})
        lat = site.get("Latitude", any_row.get("lat"))
        lon = site.get("Longitude", any_row.get("lon"))
        station = title_case_station_name(site.get("SiteName") or any_row.get("station") or f"Site {site_id}")
        region = site.get("Region") or any_row.get("region") or ""

        selected_row = site_rows.get(selected_pollutant)
        if not _monitor_has_numeric_value(selected_row):
            continue
        latest_row = max([row for row in site_rows.values() if row], key=_parse_monitor_time, default={})
        aqc_row = site_rows.get("AQC")
        ranked_rows = [row for label, row in site_rows.items() if label != "AQC" and row]
        worst_row = max(ranked_rows, key=_monitor_category_severity, default={})
        category_row = aqc_row or worst_row or selected_row or latest_row or {}

        values = []
        for pollutant_label in MONITOR_MAP_POLLUTANTS:
            row = site_rows.get(pollutant_label)
            if not _monitor_has_numeric_value(row):
                continue
            pollutant_meta = POLLUTANTS.get(pollutant_label) or {}
            units = pollutant_meta.get("Units") or ""
            values.append(
                {
                    "pollutant": pollutant_label,
                    "value": _monitor_display_value(row),
                    "units": units,
                    "category": (row or {}).get("category") or "No data",
                    "time": (row or {}).get("hour_description") or "",
                    "date": (row or {}).get("date") or "",
                }
            )

        out.append(
            {
                "site_id": int(site_id),
                "station": station,
                "lat": lat,
                "lon": lon,
                "region": region,
                "date": latest_row.get("date") or category_row.get("date") or "",
                "hour": latest_row.get("hour") or category_row.get("hour") or "",
                "hour_description": latest_row.get("hour_description") or category_row.get("hour_description") or "",
                "value": (selected_row or {}).get("value"),
                "value_label": _monitor_display_value(selected_row),
                "category": category_row.get("category") or "No data",
                "category_key": category_row.get("category_key") or "NO DATA",
                "category_color": category_row.get("category_color") or MONITORING_CATEGORY_COLORS["NO DATA"],
                "determining_pollutant": category_row.get("determining_pollutant") or selected_pollutant,
                "values": values,
                "source": "AQMS",
            }
        )

    return out



def _aqms_rows_for_pollutant(raw_obs, parameter_code, pollutant_label):
    raw_obs = raw_obs or []
    latest_by_site = {}
    for item in raw_obs:
        param = _aqms_parameter(item)
        if param.get("ParameterCode") != parameter_code:
            continue
        if (param.get("Frequency") or "").strip() != "Hourly average":
            continue

        site_id = item.get("Site_Id")
        if site_id is None:
            continue
        try:
            hour_value = int(item.get("Hour") or -1)
        except (TypeError, ValueError):
            hour_value = -1
        existing = latest_by_site.get(site_id)
        if existing is None or hour_value >= existing["_hour"]:
            latest_by_site[site_id] = dict(item, _hour=hour_value)

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
                "source": "AQMS",
            }
        )

    def severity(row):
        key = str(row.get("category") or "").strip().lower()
        order = {"no data": -1, "good": 0, "fair": 1, "poor": 2, "very poor": 3, "extremely poor": 4}
        return order.get(key, -1)

    rows.sort(key=lambda row: (-severity(row), -(row.get("value") or -1), row.get("station") or ""))
    return rows


def _aqms_previous_hour_values_by_site(raw_obs, history_rows=None):
    raw_obs = raw_obs or []
    history_rows = history_rows or []
    code_to_label = {
        (POLLUTANTS.get("PM2.5") or {}).get("ParameterCode"): "PM2.5",
        (POLLUTANTS.get("PM10") or {}).get("ParameterCode"): "PM10",
        (POLLUTANTS.get("O3") or {}).get("ParameterCode"): "O3",
        (POLLUTANTS.get("CO") or {}).get("ParameterCode"): "CO",
        (POLLUTANTS.get("NO2") or {}).get("ParameterCode"): "NO2",
        (POLLUTANTS.get("SO2") or {}).get("ParameterCode"): "SO2",
        "NO": "NO",
        "NOX": "NOX",
    }
    series_by_site = {}
    for item in list(history_rows) + list(raw_obs):
        if not isinstance(item, dict):
            continue
        param = _aqms_parameter(item)
        code = str(param.get("ParameterCode") or "").strip()
        label = code_to_label.get(code)
        if not label:
            continue
        if (param.get("Frequency") or "").strip() != "Hourly average":
            continue
        site_id = item.get("Site_Id")
        if site_id is None:
            continue
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
        try:
            site_key = int(site_id)
        except Exception:
            continue
        series_by_site.setdefault(site_key, {}).setdefault(label, {})[ts] = value_num

    out = {}
    for site_id, pollutant_map in series_by_site.items():
        site_payload = {}
        for label, ts_map in pollutant_map.items():
            points = sorted(ts_map.items(), key=lambda item: item[0])
            if not points:
                continue
            latest_value = points[-1][1]
            previous_value = points[-2][1] if len(points) > 1 else None
            site_payload[label] = {
                "current": latest_value,
                "previous": previous_value,
                "delta": (latest_value - previous_value) if previous_value is not None else None,
            }
        if site_payload:
            out[site_id] = site_payload
    return out


def _aqms_window_by_region(raw_obs, parameter_code, hours=6, aggregator="mean"):
    """Aggregate an AQMS parameter over the last `hours` for each region.

    Returns {"times": [datetime...], "regions": {region: [value_or_None...]}, "units": str}
    Includes synthetic "All NSW" region aggregated across all sites.
    """
    raw_obs = raw_obs or []
    buckets = {}
    units = ""
    for item in raw_obs:
        param = _aqms_parameter(item)
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
    return _monitor_history_rows_for_sites(
        tuple(site_ids_tuple or ()),
        ("TEMP", "HUMID", "WSP", "RAIN"),
        start_iso,
        end_iso,
        live_timeout=6,
        fallback_hours=24,
    )


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
        if value_num is None or not math.isfinite(value_num):
            continue
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


def _monitor_table_rows(aqms_rows, purpleair_rows, snapshot_by_site=None, previous_by_site=None, max_rows=40):
    rows = []
    snapshot_by_site = snapshot_by_site or {}
    previous_by_site = previous_by_site or {}
    for row in (aqms_rows or [])[: max_rows // 2]:
        site_id = row.get("site_id")
        bucket = snapshot_by_site.get(int(site_id)) if site_id is not None else {}
        prev_bucket = previous_by_site.get(int(site_id)) if site_id is not None else {}
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
                "pm25": (pm25_r or {}).get("value_label") or "--",
                "pm10": (pm10_r or {}).get("value_label") or "--",
                "o3": (o3_r or {}).get("value_label") or "--",
                "pm25_category": (pm25_r or {}).get("category") or "No data",
                "pm10_category": (pm10_r or {}).get("category") or "No data",
                "o3_category": (o3_r or {}).get("category") or "No data",
                "category": row.get("category") or "No data",
                "monitorKey": f"aqms:{row.get('site_id')}",
                "categoryColor": row.get("category_color") or "#9ca3af",
                "pm25_previous": (prev_bucket or {}).get("PM2.5", {}).get("previous") if isinstance(prev_bucket, dict) else None,
                "pm10_previous": (prev_bucket or {}).get("PM10", {}).get("previous") if isinstance(prev_bucket, dict) else None,
                "o3_previous": (prev_bucket or {}).get("O3", {}).get("previous") if isinstance(prev_bucket, dict) else None,
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


def _monitor_table_rows_all(aqms_rows, purpleair_rows, snapshot_by_site=None, previous_by_site=None):
    rows = []
    snapshot_by_site = snapshot_by_site or {}
    previous_by_site = previous_by_site or {}
    present_site_ids = set()

    def _aqms_placeholder_row(site_id, site):
        station = title_case_station_name(site.get("SiteName") or site.get("StationName") or f"Site {site_id}")
        region = str(site.get("Region") or "--").strip() or "--"
        return {
            "station": station,
            "region": region,
            "pollutant": "--",
            "hour": "--",
            "source": "AQMS",
            "pm25": "--",
            "pm10": "--",
            "o3": "--",
            "co": "--",
            "no": "--",
            "no2": "--",
            "nox": "--",
            "pm25_category": "No data",
            "pm10_category": "No data",
            "o3_category": "No data",
            "co_category": "No data",
            "no_category": "No data",
            "no2_category": "No data",
            "nox_category": "No data",
            "category": "No data",
            "monitorKey": f"aqms:{site_id}",
            "categoryColor": MONITORING_CATEGORY_COLORS["NO DATA"],
            "is_placeholder": True,
            "pm25_previous": None,
            "pm10_previous": None,
            "o3_previous": None,
            "co_previous": None,
            "no_previous": None,
            "no2_previous": None,
            "nox_previous": None,
        }

    for row in aqms_rows or []:
        site_id = row.get("site_id")
        bucket = snapshot_by_site.get(int(site_id)) if site_id is not None else {}
        prev_bucket = previous_by_site.get(int(site_id)) if site_id is not None else {}
        if site_id is not None:
            try:
                present_site_ids.add(int(site_id))
            except (TypeError, ValueError):
                pass
        pm25_r = bucket.get("PM2.5") if isinstance(bucket, dict) else None
        pm10_r = bucket.get("PM10") if isinstance(bucket, dict) else None
        o3_r = bucket.get("O3") if isinstance(bucket, dict) else None
        co_r = bucket.get("CO") if isinstance(bucket, dict) else None
        no_r = bucket.get("NO") if isinstance(bucket, dict) else None
        no2_r = bucket.get("NO2") if isinstance(bucket, dict) else None
        nox_r = bucket.get("NOX") if isinstance(bucket, dict) else None
        rows.append(
            {
                "station": row.get("station"),
                "region": row.get("region") or "--",
            "pollutant": row.get("determining_pollutant") or "--",
                "hour": row.get("hour_description") or "--",
                "source": "AQMS",
                "pm25": (pm25_r or {}).get("value_label") or "--",
                "pm10": (pm10_r or {}).get("value_label") or "--",
                "o3": (o3_r or {}).get("value_label") or "--",
                "co": (co_r or {}).get("value_label") or "--",
                "no": (no_r or {}).get("value_label") or "--",
                "no2": (no2_r or {}).get("value_label") or "--",
                "nox": (nox_r or {}).get("value_label") or "--",
                "pm25_category": (pm25_r or {}).get("category") or "No data",
                "pm10_category": (pm10_r or {}).get("category") or "No data",
                "o3_category": (o3_r or {}).get("category") or "No data",
                "co_category": (co_r or {}).get("category") or "No data",
                "no_category": (no_r or {}).get("category") or "No data",
                "no2_category": (no2_r or {}).get("category") or "No data",
                "nox_category": (nox_r or {}).get("category") or "No data",
                "category": row.get("category") or "No data",
                "monitorKey": f"aqms:{row.get('site_id')}",
                "categoryColor": row.get("category_color") or "#9ca3af",
                "pm25_previous": (prev_bucket or {}).get("PM2.5", {}).get("previous") if isinstance(prev_bucket, dict) else None,
                "pm10_previous": (prev_bucket or {}).get("PM10", {}).get("previous") if isinstance(prev_bucket, dict) else None,
                "o3_previous": (prev_bucket or {}).get("O3", {}).get("previous") if isinstance(prev_bucket, dict) else None,
                "co_previous": (prev_bucket or {}).get("CO", {}).get("previous") if isinstance(prev_bucket, dict) else None,
                "no_previous": (prev_bucket or {}).get("NO", {}).get("previous") if isinstance(prev_bucket, dict) else None,
                "no2_previous": (prev_bucket or {}).get("NO2", {}).get("previous") if isinstance(prev_bucket, dict) else None,
                "nox_previous": (prev_bucket or {}).get("NOX", {}).get("previous") if isinstance(prev_bucket, dict) else None,
            }
        )

    # Ensure every AQMS station from site metadata appears in "View all stations"
    # even when the live feed returns only a subset of sites.
    site_lookup = _sites_by_id()
    for site_id, site in site_lookup.items():
        try:
            sid = int(site_id)
        except (TypeError, ValueError):
            continue
        if sid in present_site_ids:
            continue
        rows.append(_aqms_placeholder_row(sid, site or {}))

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
                "co": "--",
                "no": "--",
                "no2": "--",
                "nox": "--",
                "pm25_category": row.get("category") or "No data",
                "pm10_category": "No data",
                "o3_category": "No data",
                "co_category": "No data",
                "no_category": "No data",
                "no2_category": "No data",
                "nox_category": "No data",
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
    # Badge rendering is handled via per-cell HTML + CSS rather than DataTable
    # conditional formatting.
    return []


_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _aq_badge_class(category_label: str) -> str:
    key = str(category_label or "").strip().lower()
    if key in {"good"}:
        return "aq-badge--good"
    if key in {"fair", "moderate"}:
        return "aq-badge--fair"
    if key in {"poor"}:
        return "aq-badge--poor"
    if key in {"very poor", "verypoor"}:
        return "aq-badge--very-poor"
    if key in {"extremely poor", "extreme", "extremelypoor"}:
        return "aq-badge--extreme"
    return "aq-badge--no-data"


def _aq_badge_html(value_text, category_label) -> str:
    text = str(value_text or "").strip()
    if text and text != "--":
        match = _NUMBER_RE.search(text)
        if match:
            text = match.group(0)
    if not text or text == "--":
        text = "--"
    css = f"aq-badge {_aq_badge_class(category_label)}"
    return f'<span class="{css}"><span class="aq-badge__value">{escape(text)}</span></span>'


def _inject_badges_into_monitor_rows(rows):
    out = []
    for row in rows or []:
        r = dict(row or {})
        r["pm25_badge"] = _aq_badge_html(r.get("pm25"), r.get("pm25_category"))
        r["pm10_badge"] = _aq_badge_html(r.get("pm10"), r.get("pm10_category"))
        r["o3_badge"] = _aq_badge_html(r.get("o3"), r.get("o3_category"))
        r["co_badge"] = _aq_badge_html(r.get("co"), r.get("co_category"))
        r["no_badge"] = _aq_badge_html(r.get("no"), r.get("no_category"))
        r["no2_badge"] = _aq_badge_html(r.get("no2"), r.get("no2_category"))
        r["nox_badge"] = _aq_badge_html(r.get("nox"), r.get("nox_category"))
        out.append(r)
    return out


def _ordered_monitor_outlook_rows(rows, source_value="both", compact=False, max_rows=None):
    source_value = str(source_value or "both").strip().lower()
    aqms_rows = []
    purpleair_rows = []
    other_rows = []
    for row in rows or []:
        source = str((row or {}).get("source") or "").strip().lower()
        if source == "aqms":
            aqms_rows.append(row)
        elif source == "purpleair":
            purpleair_rows.append(row)
        else:
            other_rows.append(row)

    if source_value == "aqms":
        ordered = aqms_rows + other_rows
    elif source_value == "purpleair":
        ordered = purpleair_rows
    elif compact and (aqms_rows or other_rows):
        ordered = aqms_rows + other_rows
    else:
        ordered = aqms_rows + other_rows + purpleair_rows

    if max_rows is not None:
        return ordered[: int(max_rows)]
    return ordered


def _attach_arrows_to_table_rows(rows, snapshot_by_site=None, previous_by_site=None):
    """Ensure each AQMS row's pm25/pm10/o3 fields include arrows based on the
    in-process `_LAST_AQMS_SNAPSHOT`. This augments rows passed from the store
    so UI interactions (region filter) still show arrows.
    """
    if not rows:
        return rows
    snapshot_by_site = snapshot_by_site or {}
    previous_by_site = previous_by_site or {}
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

        current_prev = {}
        try:
            if site_id is not None:
                current_prev = previous_by_site.get(int(site_id)) or {}
        except Exception:
            current_prev = {}

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
                if current_prev and isinstance(current_prev, dict) and current_prev.get(pollutant_label) and current_prev.get(pollutant_label).get("current") is not None:
                    return current_prev.get(pollutant_label).get("current")
            except Exception:
                pass
            try:
                if prev_bucket and isinstance(prev_bucket, dict):
                    return prev_bucket.get(pollutant_label)
            except Exception:
                pass
            return None

        def _previous_value_for(pollutant_label):
            try:
                if current_prev and isinstance(current_prev, dict) and current_prev.get(pollutant_label):
                    return current_prev.get(pollutant_label).get("previous")
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
            r["pm25"] = _format_value_with_arrow(site_id, "PM2.5", (r.get("pm25") or "--"), _value_for("PM2.5"), previous_value=_previous_value_for("PM2.5"))
        if not _has_arrow(r.get("pm10")):
            r["pm10"] = _format_value_with_arrow(site_id, "PM10", (r.get("pm10") or "--"), _value_for("PM10"), previous_value=_previous_value_for("PM10"))
        if not _has_arrow(r.get("o3")):
            r["o3"] = _format_value_with_arrow(site_id, "O3", (r.get("o3") or "--"), _value_for("O3"), previous_value=_previous_value_for("O3"))
        if not _has_arrow(r.get("co")) and "co" in r:
            r["co"] = _format_value_with_arrow(site_id, "CO", (r.get("co") or "--"), _value_for("CO"), previous_value=_previous_value_for("CO"))
        if not _has_arrow(r.get("no")) and "no" in r:
            r["no"] = _format_value_with_arrow(site_id, "NO", (r.get("no") or "--"), _value_for("NO"), previous_value=_previous_value_for("NO"))
        if not _has_arrow(r.get("no2")) and "no2" in r:
            r["no2"] = _format_value_with_arrow(site_id, "NO2", (r.get("no2") or "--"), _value_for("NO2"), previous_value=_previous_value_for("NO2"))
        if not _has_arrow(r.get("nox")) and "nox" in r:
            r["nox"] = _format_value_with_arrow(site_id, "NOX", (r.get("nox") or "--"), _value_for("NOX"), previous_value=_previous_value_for("NOX"))
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


def _format_value_with_arrow(site_id, pollutant_label, value_label, value_num, previous_value=None):
    """Return a plain display label for monitoring table cells (no trend arrows)."""
    return str(value_label or "--")


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


def _monitor_region_series_parameter_codes():
    parameter_codes = []
    for pollutant_label in MONITOR_REGION_SERIES_POLLUTANTS:
        code = (POLLUTANTS.get(pollutant_label) or {}).get("ParameterCode") or pollutant_label
        if code and code not in parameter_codes:
            parameter_codes.append(code)
    return parameter_codes


def _monitor_region_series_seed_site_id():
    try:
        if MONITOR_REGION_SERIES_DEFAULT_STATION_VALUE:
            return int(str(MONITOR_REGION_SERIES_DEFAULT_STATION_VALUE).split(":", 1)[1])
    except Exception:
        pass
    for site_id in sorted(_sites_by_id()):
        try:
            return int(site_id)
        except Exception:
            continue
    return 0


@lru_cache(maxsize=8)
def _monitor_observation_cache_rows(cache_mtime):
    rows = _load_observations_cache() or []
    return tuple(row for row in rows if isinstance(row, dict))


@lru_cache(maxsize=8)
def _monitor_observation_cache_index(cache_mtime):
    entries = []
    for row in _monitor_observation_cache_rows(cache_mtime):
        param_code = str(_aqms_parameter(row).get("ParameterCode") or row.get("ParameterCode") or "").strip()
        if not param_code:
            continue
        ts = _parse_aqms_time_point(row)
        if ts is None:
            continue
        try:
            site_id = int(row.get("Site_Id") or row.get("site_id") or row.get("SiteId") or row.get("SiteID"))
        except Exception:
            site_id = None
        entries.append((param_code, site_id, ts.replace(minute=0, second=0, microsecond=0), row))
    return tuple(entries)


@lru_cache(maxsize=8)
def _monitor_observation_cache_by_site_param(cache_mtime):
    grouped = {}
    for param_code, site_id, ts, row in _monitor_observation_cache_index(cache_mtime):
        if site_id is None:
            continue
        grouped.setdefault((site_id, param_code), []).append((ts, row))
    return {key: tuple(values) for key, values in grouped.items()}


def _monitor_cache_mtime():
    try:
        return int(os.path.getmtime(_OBS_CACHE_CSV))
    except OSError:
        return None


def _monitor_window_bounds(start_iso, end_iso):
    start_dt = _parse_forecast_timestamp(start_iso)
    end_dt = _parse_forecast_timestamp(end_iso)
    if start_dt is not None:
        start_dt = start_dt.replace(tzinfo=SYDNEY_TZ) if start_dt.tzinfo is None else start_dt.astimezone(SYDNEY_TZ)
    if end_dt is not None:
        end_dt = end_dt.replace(tzinfo=SYDNEY_TZ) if end_dt.tzinfo is None else end_dt.astimezone(SYDNEY_TZ)
    return start_dt, end_dt


def _monitor_cached_history_window(parameter_codes_tuple, start_iso, end_iso, fallback_hours=None):
    cache_mtime = _monitor_cache_mtime()
    if cache_mtime is None:
        return []
    parameter_codes = {str(code or "").strip() for code in parameter_codes_tuple or () if str(code or "").strip()}
    if not parameter_codes:
        return []

    start_dt, end_dt = _monitor_window_bounds(start_iso, end_iso)
    entries = _monitor_observation_cache_index(cache_mtime)
    filtered = []
    latest_ts = None
    for param_code, _site_id, ts, row in entries:
        if param_code not in parameter_codes:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
        if start_dt is not None and ts < start_dt:
            continue
        if end_dt is not None and ts > end_dt:
            continue
        filtered.append(row)

    if filtered:
        return filtered

    if latest_ts is None or not fallback_hours:
        return []
    fallback_start = latest_ts - timedelta(hours=float(fallback_hours))
    fallback = []
    for param_code, _site_id, ts, row in entries:
        if param_code not in parameter_codes:
            continue
        if fallback_start <= ts <= latest_ts:
            fallback.append(row)
    return fallback


def _monitor_cached_history_rows_for_sites(site_ids, parameter_codes, start_iso, end_iso, fallback_hours=None):
    cache_mtime = _monitor_cache_mtime()
    if cache_mtime is None:
        return []
    site_id_set = set()
    for site_id in site_ids or ():
        try:
            site_id_set.add(int(site_id))
        except Exception:
            continue
    parameter_code_set = {str(code or "").strip() for code in parameter_codes or () if str(code or "").strip()}
    if not site_id_set or not parameter_code_set:
        return []

    start_dt, end_dt = _monitor_window_bounds(start_iso, end_iso)
    grouped = _monitor_observation_cache_by_site_param(cache_mtime)
    filtered = []
    latest_ts = None
    for site_id in site_id_set:
        for param_code in parameter_code_set:
            for ts, row in grouped.get((site_id, param_code), ()):
                if latest_ts is None or ts > latest_ts:
                    latest_ts = ts
                if start_dt is not None and ts < start_dt:
                    continue
                if end_dt is not None and ts > end_dt:
                    continue
                filtered.append(row)

    if filtered:
        return filtered

    if latest_ts is None or not fallback_hours:
        return []
    fallback_start = latest_ts - timedelta(hours=float(fallback_hours))
    fallback = []
    for site_id in site_id_set:
        for param_code in parameter_code_set:
            for ts, row in grouped.get((site_id, param_code), ()):
                if fallback_start <= ts <= latest_ts:
                    fallback.append(row)
    return fallback


def _monitor_region_series_cached_window(parameter_codes_tuple, start_iso, end_iso):
    # If the live API/cache lags the current clock, return a latest 24-hour
    # slice from cache. The render callback already adjusts its x-axis to that
    # latest timestamp.
    return _monitor_cached_history_window(parameter_codes_tuple, start_iso, end_iso, fallback_hours=24)


@lru_cache(maxsize=16)
def _fetch_monitor_region_history_window(parameter_codes_tuple, start_iso, end_iso):
    parameter_codes = list(parameter_codes_tuple or ())
    if not parameter_codes:
        return []

    if str(os.environ.get("MONITOR_REGION_SERIES_CACHE_FIRST", "1")).strip().lower() not in {"0", "false", "no", "n"}:
        cached_rows = _monitor_region_series_cached_window(tuple(parameter_codes), start_iso, end_iso)
        if cached_rows:
            return cached_rows

    try:
        timeout = float(os.environ.get("MONITOR_REGION_SERIES_HISTORY_TIMEOUT", "8"))
    except (TypeError, ValueError):
        timeout = 8

    # The NSW history endpoint can return a broad payload even for a single site.
    # Cache it by time window so changing station filters locally instead of
    # redownloading the same large response.
    try:
        return fetch_observation_history([_monitor_region_series_seed_site_id()], parameter_codes, start_iso, end_iso, timeout=timeout)
    except Exception:
        return []


@lru_cache(maxsize=256)
def _monitor_history_rows_for_sites(site_ids_tuple, parameter_codes_tuple, start_iso, end_iso, live_timeout=8, fallback_hours=None):
    site_ids = set()
    for site_id in site_ids_tuple or ():
        try:
            site_ids.add(int(site_id))
        except Exception:
            continue
    if not site_ids:
        return []

    parameter_codes = tuple(sorted({str(code or "").strip() for code in parameter_codes_tuple or () if str(code or "").strip()}))
    if not parameter_codes:
        return []

    rows = _monitor_cached_history_rows_for_sites(site_ids, parameter_codes, start_iso, end_iso, fallback_hours=fallback_hours)
    if not rows:
        try:
            rows = fetch_observation_history(list(site_ids), list(parameter_codes), start_iso, end_iso, timeout=float(live_timeout))
        except Exception:
            rows = []

    filtered = []
    parameter_code_set = set(parameter_codes)
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            row_site_id = int(row.get("Site_Id") or row.get("site_id") or row.get("SiteId") or row.get("SiteID"))
        except Exception:
            continue
        if row_site_id not in site_ids:
            continue
        param_code = str(_aqms_parameter(row).get("ParameterCode") or row.get("ParameterCode") or "").strip()
        if param_code and param_code not in parameter_code_set:
            continue
        filtered.append(row)
    return tuple(filtered)


@lru_cache(maxsize=256)
def _fetch_purpleair_sensor_history_cached(sensor_index, start_ts, end_ts, average, fields_tuple, timeout):
    try:
        return fetch_purpleair_sensor_history(
            int(sensor_index),
            int(start_ts),
            int(end_ts),
            average=int(average),
            fields=list(fields_tuple or ()),
            timeout=float(timeout),
        )
    except Exception:
        return {"fields": [], "data": [], "error": "PurpleAir history unavailable."}


@lru_cache(maxsize=128)
def _fetch_monitor_region_history(site_ids_tuple, start_iso, end_iso):
    return _monitor_history_rows_for_sites(
        tuple(site_ids_tuple or ()),
        tuple(_monitor_region_series_parameter_codes()),
        start_iso,
        end_iso,
        live_timeout=float(os.environ.get("MONITOR_REGION_SERIES_HISTORY_TIMEOUT", "8") or 8),
        fallback_hours=24,
    )


def _prewarm_monitor_observation_cache():
    cache_mtime = _monitor_cache_mtime()
    if cache_mtime is None:
        return
    try:
        _monitor_observation_cache_rows(cache_mtime)
        _monitor_observation_cache_index(cache_mtime)
        _monitor_observation_cache_by_site_param(cache_mtime)
    except Exception:
        return


_prewarm_monitor_observation_cache()


def _monitor_region_series_values(history_rows, pollutant_label, start_dt, end_dt):
    pollutant_code = (POLLUTANTS.get(pollutant_label) or {}).get("ParameterCode") or pollutant_label
    values_by_time = {}
    for item in history_rows or []:
        if not isinstance(item, dict):
            continue
        param = _aqms_parameter(item)
        if str(param.get("ParameterCode") or "").strip() != str(pollutant_code):
            continue
        ts = _parse_aqms_time_point(item)
        if ts is None or ts < start_dt or ts > end_dt:
            continue
        value = item.get("Value")
        try:
            value = float(value) if value is not None else None
        except (TypeError, ValueError):
            value = None
        if value is None:
            continue
        values_by_time[ts.replace(minute=0, second=0, microsecond=0)] = value
    return values_by_time


def _monitor_region_series_latest_timestamp(history_rows):
    latest_ts = None
    for row in history_rows or []:
        ts = _parse_aqms_time_point(row)
        if ts is None:
            continue
        ts = ts.replace(minute=0, second=0, microsecond=0)
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
    return latest_ts


def _monitor_region_series_figure(
    history_rows,
    pollutant_label,
    start_dt,
    end_dt,
    region_label,
    station_name,
    purpleair_history=None,
    purpleair_label=None,
):
    values_by_time = _monitor_region_series_values(history_rows, pollutant_label, start_dt, end_dt)

    x_times = []
    cursor = start_dt.replace(minute=0, second=0, microsecond=0)
    end_cursor = end_dt.replace(minute=0, second=0, microsecond=0)
    while cursor <= end_cursor:
        x_times.append(cursor)
        cursor = cursor + timedelta(hours=1)

    units = (POLLUTANTS.get(pollutant_label) or {}).get("Units") or "Value"
    y_values = [values_by_time.get(ts) for ts in x_times]
    plot_color = MONITOR_REGION_SERIES_PLOT_COLORS.get(pollutant_label, "#6d28d9")
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=x_times,
            y=y_values,
            name=station_name,
            mode="lines+markers",
            line=dict(color=plot_color, width=3, shape="spline", smoothing=0.6),
            marker=dict(size=7, color=plot_color, line=dict(color="#ffffff", width=1.2)),
            fill="tozeroy",
            fillcolor=_hex_to_rgba(plot_color, 0.16),
            hovertemplate=f"<b>{station_name}</b><br>%{{x|%d %b %H:%M}}<br>%{{y:.1f}} {units}<extra></extra>",
        )
    )

    if pollutant_label in {"PM2.5", "PM10"} and purpleair_history and purpleair_history.get("data"):
        fields = purpleair_history.get("fields") or []
        data = purpleair_history.get("data") or []
        time_idx = fields.index("time_stamp") if "time_stamp" in fields else 0
        field_name = "pm2.5_alt" if pollutant_label == "PM2.5" else "pm10.0_atm"
        if field_name in fields:
            val_idx = fields.index(field_name)
            pa_xs = []
            pa_ys = []
            for row in data:
                if not isinstance(row, list) or len(row) <= max(time_idx, val_idx):
                    continue
                try:
                    ts = datetime.fromtimestamp(int(row[time_idx]), tz=SYDNEY_TZ)
                except (TypeError, ValueError, OSError):
                    continue
                if ts < start_dt or ts > end_dt:
                    continue
                try:
                    value = float(row[val_idx]) if row[val_idx] is not None else None
                except (TypeError, ValueError):
                    value = None
                if value is None:
                    continue
                pa_xs.append(ts)
                pa_ys.append(value)
            if pa_xs:
                fig.add_trace(
                    go.Scatter(
                        x=pa_xs,
                        y=pa_ys,
                        name=purpleair_label or "PurpleAir (nearest)",
                        mode="lines+markers",
                        line=dict(color=PURPLEAIR_COLOR, width=2.4, dash="dot"),
                        marker=dict(size=6, color=PURPLEAIR_COLOR, symbol="circle-open"),
                        hovertemplate=f"<b>{purpleair_label or 'PurpleAir (nearest)'}</b><br>%{{x|%d %b %H:%M}}<br>%{{y:.1f}} {units}<extra></extra>",
                    )
                )

    fig.update_layout(
        template="plotly_white",
        height=210,
        margin=dict(l=48, r=14, t=30, b=26),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        hovermode="x",
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0.01, font=dict(size=10)),
        showlegend=True,
        title=dict(text=f"{pollutant_label}", x=0.01, y=0.97, xanchor="left", font=dict(size=15, color=plot_color)),
    )
    fig.update_xaxes(
        showgrid=True,
        gridcolor="#e8eef8",
        tickformat="%H:%M<br>%d %b",
        nticks=6,
        ticks="outside",
        tickfont=dict(size=10),
    )
    fig.update_yaxes(
        title_text=units,
        showgrid=True,
        gridcolor="#e8eef8",
        zeroline=False,
        tickfont=dict(size=11),
    )

    if not fig.data:
        fig.add_annotation(
            text="No AQMS observations found for this pollutant and region.",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font=dict(family=BASE_FONT_FAMILY, size=14, color="#475569"),
        )

    return fig


def _monitor_region_series_combined_figure(history_rows, start_dt, end_dt, region_label, station_name):
    x_times = []
    cursor = start_dt.replace(minute=0, second=0, microsecond=0)
    end_cursor = end_dt.replace(minute=0, second=0, microsecond=0)
    while cursor <= end_cursor:
        x_times.append(cursor)
        cursor = cursor + timedelta(hours=1)

    fig = make_subplots(
        rows=len(MONITOR_REGION_SERIES_POLLUTANTS),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.075,
        subplot_titles=[
            f"{pollutant_display(pollutant_label)} ({(POLLUTANTS.get(pollutant_label) or {}).get('Units') or 'Value'})"
            for pollutant_label in MONITOR_REGION_SERIES_POLLUTANTS
        ],
    )

    has_points = False
    for row_idx, pollutant_label in enumerate(MONITOR_REGION_SERIES_POLLUTANTS, start=1):
        values_by_time = _monitor_region_series_values(history_rows, pollutant_label, start_dt, end_dt)
        y_values = [values_by_time.get(ts) for ts in x_times]
        if any(value is not None for value in y_values):
            has_points = True
        units = (POLLUTANTS.get(pollutant_label) or {}).get("Units") or "Value"
        plot_color = MONITOR_REGION_SERIES_PLOT_COLORS.get(pollutant_label, "#2563eb")
        fig.add_trace(
            go.Scattergl(
                x=x_times,
                y=y_values,
                name=pollutant_display(pollutant_label),
                mode="lines+markers",
                line=dict(color=plot_color, width=2.5),
                marker=dict(size=5, color=plot_color),
                connectgaps=False,
                hovertemplate=(
                    f"<b>{station_name}</b><br>"
                    f"{pollutant_display(pollutant_label)}<br>"
                    "%{x|%d %b %H:%M}<br>%{y:.1f} "
                    f"{units}<extra></extra>"
                ),
            ),
            row=row_idx,
            col=1,
        )
        fig.update_yaxes(title_text=units, showgrid=True, gridcolor="#e8eef8", zeroline=False, row=row_idx, col=1)

    if not has_points:
        fig.add_annotation(
            text="No hourly AQMS history returned for this station in the selected window.",
            xref="paper",
            yref="paper",
            x=0.5,
            y=0.5,
            showarrow=False,
            font=dict(size=13, color="#64748b", family=BASE_FONT_FAMILY),
        )

    fig.update_layout(
        template="plotly_white",
        height=390,
        margin=dict(l=56, r=18, t=42, b=34),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        paper_bgcolor="rgba(255,255,255,0)",
        plot_bgcolor="#ffffff",
        showlegend=False,
        hovermode="x unified",
        title=dict(
            text=f"{region_label} · {station_name} · last 24 hours",
            x=0,
            xanchor="left",
            font=dict(size=14, family=BASE_FONT_FAMILY, color="#0f172a"),
        ),
    )
    fig.update_xaxes(showgrid=False, tickformat="%H:%M", ticks="outside", tickfont=dict(size=10))
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
            html.Div(
                [
                    html.Span("AQMS feed", className="network-label network-label--aqms"),
                    html.Span(aqms_status, className="network-value network-value--aqms"),
                ],
                className="network-row",
            ),
            html.Div(
                [
                    html.Span("PurpleAir feed", className="network-label network-label--purpleair"),
                    html.Span(pa_status, className="network-value network-value--purpleair"),
                ],
                className="network-row",
            ),
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

    # Discover the forecast timestamps (use the first available parsed file).
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
            time_labels = list(times)
            break

    base_index = _forecast_live_base_index(time_labels)
    if time_labels:
        selected_indices = list(range(base_index, min(base_index + hours, len(time_labels))))
        display_time_labels = [time_labels[idx] for idx in selected_indices]
    else:
        selected_indices = list(range(hours))
        display_time_labels = []

    # Compute per pollutant, worst across stations for each hour index, plus contributing station counts
    # and the station name responsible for the maximum.
    rows = []
    for pollutant in pollutants:
        maxima = [None for _ in selected_indices]
        max_stations = [None for _ in selected_indices]
        counts = [0 for _ in selected_indices]
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
                for out_idx, hour_idx in enumerate(selected_indices):
                    if hour_idx >= len(values):
                        continue
                    try:
                        val = float(values[hour_idx])
                    except (TypeError, ValueError):
                        continue
                    counts[out_idx] += 1
                    current = maxima[out_idx]
                    if current is None or val > current:
                        maxima[out_idx] = val
                        max_stations[out_idx] = station_name

        rows.append({"pollutant": pollutant, "values": maxima, "counts": counts, "stations": max_stations, "model": used_model})
    return display_time_labels, rows


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


def _ranking_bar_figure(parsed, pollutant_label):
    rows = []
    if parsed:
        for station_name, payload in parsed.get("ranking", {}).items():
            value = payload.get("maxValue")
            if value is None:
                continue
            value = float(value)
            category, color = category_for_value(pollutant_label, value)
            rows.append(
                {
                    "station_key": station_name,
                    "station": title_case_station_name(station_name),
                    "value": value,
                    "timestamp": _format_forecast_label(payload.get("timestamp")),
                    "category": category.replace("-", " ").title(),
                    "color": color,
                }
            )

    rows.sort(key=lambda row: row["value"], reverse=True)
    rows_for_chart = list(reversed(rows[:12]))
    figure = go.Figure()

    if not rows_for_chart:
        figure.update_layout(
            template="plotly_white",
            height=330,
            margin=dict(l=20, r=20, t=20, b=20),
            font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
            annotations=[
                dict(
                    text="No ranked forecast values are available.",
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
            text=[f"{row['value']:.2f}" for row in rows_for_chart],
            textposition="outside",
            cliponaxis=False,
            customdata=[
                [row["station_key"], row["category"], row["timestamp"]]
                for row in rows_for_chart
            ],
            hovertemplate=(
                "<b>%{y}</b><br>"
                "Max value: %{x:.2f}<br>"
                "Category: %{customdata[1]}<br>"
                "Time: %{customdata[2]}<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=340,
        margin=dict(l=108, r=34, t=16, b=36),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(255,255,255,0.86)",
        showlegend=False,
        bargap=0.28,
        xaxis=dict(
            title="Max value",
            gridcolor="#e2e8f0",
            zeroline=False,
            fixedrange=True,
        ),
        yaxis=dict(
            title="",
            fixedrange=True,
            automargin=True,
        ),
    )
    return figure


def _forecast_hour_options(parsed):
    times = ((parsed or {}).get("data") or {}).get("time", {}).get("forecastTime", [])
    if not times:
        return [{"label": "--", "value": 0}]
    options = []
    for idx, timestamp in enumerate(times):
        options.append(
            {
                "label": _format_forecast_label(timestamp),
                "value": idx,
            }
        )
    return options


def _coerce_forecast_hour_index(parsed, hour_index):
    times = ((parsed or {}).get("data") or {}).get("time", {}).get("forecastTime", [])
    if not times:
        return 0
    try:
        hour_index = int(hour_index)
    except (TypeError, ValueError):
        hour_index = 0
    return max(0, min(hour_index, len(times) - 1))


def _station_pollutant_value_map(parsed, pollutant_label, hour_index):
    if not parsed:
        return {}, None

    hour_index = _coerce_forecast_hour_index(parsed, hour_index)
    stations = ((parsed.get("data") or {}).get("stations") or {})
    times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime", [])
    selected_time = times[hour_index] if hour_index < len(times) else None
    rows = {}

    for station_name, payload in stations.items():
        values = (payload or {}).get("forecastValue") or []
        if hour_index >= len(values):
            continue
        try:
            value = float(values[hour_index])
        except (TypeError, ValueError):
            continue
        category, color = category_for_value(pollutant_label, value)
        rows[station_name] = {
            "station_key": station_name,
            "station": title_case_station_name(station_name),
            "value": value,
            "category": category.replace("-", " ").title(),
            "color": color,
            "units": (POLLUTANTS.get(pollutant_label) or {}).get("Units") or "",
            "pollutant": pollutant_label,
        }

    return rows, selected_time


def _forecast_station_rows(selection, hour_index, focus_pollutant):
    if not selection:
        return [], None

    selection = {
        "regions": str(selection.get("regions") or ""),
        "timeScopes": str(selection.get("timeScopes") or ""),
        "models": str(selection.get("models") or ""),
        "date": str(selection.get("date") or ""),
    }
    pollutants = ("PM2.5", "PM10", "O3")
    merged_rows = {}
    selected_time = None

    for pollutant_label in pollutants:
        forecast_selection = dict(selection)
        forecast_selection["pollutants"] = pollutant_label
        parsed, _message = _load_forecast(forecast_selection)
        if not parsed:
            continue
        pollutant_rows, pollutant_time = _station_pollutant_value_map(parsed, pollutant_label, hour_index)
        if selected_time is None:
            selected_time = pollutant_time
        for station_key, payload in pollutant_rows.items():
            entry = merged_rows.setdefault(
                station_key,
                {
                    "station_key": station_key,
                    "station": payload["station"],
                    "pollutants": {},
                },
            )
            entry["pollutants"][pollutant_label] = payload

    rows = []
    focus_key = str(focus_pollutant or "PM2.5")
    for entry in merged_rows.values():
        focus_payload = entry["pollutants"].get(focus_key)
        focus_value = focus_payload["value"] if focus_payload else float("-inf")
        focus_category = focus_payload["category"] if focus_payload else "No data"
        focus_color = focus_payload["color"] if focus_payload else "#94a3b8"
        rows.append(
            {
                "station_key": entry["station_key"],
                "station": entry["station"],
                "pollutants": entry["pollutants"],
                "focus_value": focus_value,
                "focus_category": focus_category,
                "focus_color": focus_color,
            }
        )

    rows.sort(key=lambda row: row["focus_value"], reverse=True)
    return rows, selected_time


FORECAST_ALL_STATION_HORIZONS = (1, 3, 6, 12)


def _forecast_horizon_indices(times, base_index, offsets=FORECAST_ALL_STATION_HORIZONS):
    times = times or []
    if not times:
        return []
    try:
        base_index = int(base_index or 0)
    except (TypeError, ValueError):
        base_index = 0
    base_index = max(0, min(base_index, len(times) - 1))

    parsed_times = [_parse_forecast_timestamp(item) for item in times]
    base_dt = parsed_times[base_index] if base_index < len(parsed_times) else None
    out = []
    for offset in offsets:
        try:
            offset_hours = int(offset)
        except (TypeError, ValueError):
            offset_hours = 1
        idx = min(base_index + max(offset_hours, 0), len(times) - 1)
        if base_dt:
            target_dt = base_dt + timedelta(hours=offset_hours)
            later_candidates = [
                (i, parsed_dt)
                for i, parsed_dt in enumerate(parsed_times)
                if i > base_index and parsed_dt is not None and parsed_dt >= target_dt
            ]
            if later_candidates:
                idx = later_candidates[0][0]
            else:
                future_candidates = [
                    (i, parsed_dt)
                    for i, parsed_dt in enumerate(parsed_times)
                    if i > base_index and parsed_dt is not None
                ]
                if future_candidates:
                    idx = min(future_candidates, key=lambda item: abs((item[1] - target_dt).total_seconds()))[0]
        out.append({"offset": offset_hours, "index": idx})
    return out


def _forecast_horizon_label(times, base_index, horizon):
    idx = horizon.get("index", 0)
    requested_offset = horizon.get("offset", 0)
    label = _format_forecast_label(times[idx] if idx < len(times) else None)
    lead_label = f"t+{requested_offset}"
    return f"{lead_label} · {label}"


def _format_forecast_table_time_label(value):
    parsed = _parse_forecast_timestamp(value)
    if not parsed:
        return str(value or "--")
    hour = parsed.strftime("%I").lstrip("0") or "0"
    return f"{hour}{parsed.strftime('%p')} / {parsed.strftime('%b')} {parsed.day}"


def _forecast_horizon_table_label(times, horizon):
    idx = horizon.get("index", 0)
    return _format_forecast_table_time_label(times[idx] if idx < len(times) else None)


def _load_exact_forecast(selection):
    file_path = _build_file_path(selection or {})
    if not file_path:
        return None
    try:
        max_forecast_hours = int((selection or {}).get("timeScopes"))
    except (TypeError, ValueError):
        max_forecast_hours = None
    parsed = parse_csv(file_path, (selection or {}).get("pollutants"), max_forecast_hours=max_forecast_hours)
    if isinstance(parsed, dict) and parsed.get("error"):
        return None
    if isinstance(parsed, dict):
        parsed["_sourceFile"] = file_path
        parsed["_exactMatch"] = True
        parsed["_region"] = (selection or {}).get("regions")
    return parsed


def _forecast_all_station_regions(selection):
    regions = _overview_available_forecast_regions(selection or {})
    if regions:
        return regions
    selected_region = (selection or {}).get("regions")
    return [selected_region] if selected_region else []


def _forecast_all_station_table(selection, hour_index):
    selection = selection or {}
    pollutant_label = str(selection.get("pollutants") or "PM2.5")
    selected_parsed = _load_exact_forecast(selection) or (_load_forecast(selection)[0] if selection else None)
    if not selected_parsed:
        return [], [], "All station forecast", "No exact forecast file is available for this selection."

    times = ((selected_parsed.get("data") or {}).get("time") or {}).get("forecastTime") or []
    if not times:
        return [], [], "All station forecast", "No forecast time axis is available for this selection."

    base_index = _coerce_forecast_hour_index(selected_parsed, hour_index)
    base_time = _parse_forecast_timestamp(times[base_index]) if base_index < len(times) else None
    horizons = _forecast_horizon_indices(times, base_index)
    units = (POLLUTANTS.get(pollutant_label) or {}).get("Units") or ""
    region_lookup = load_region_lookup()
    selected_region_code = str(selection.get("regions") or "")
    selected_region_label = region_lookup.get(selected_region_code) or title_case_station_name(selected_region_code.replace("_", " "))

    columns = [
        {"name": "Station", "id": "station"},
        {"name": "Region", "id": "region"},
    ]
    for horizon in horizons:
        col_id = f"h{horizon['offset']}_{horizon['index']}"
        columns.append(
            {
                "name": _forecast_horizon_table_label(times, horizon),
                "id": col_id,
                "presentation": "markdown",
            }
        )

    rows = []
    included_regions = []
    for region_code in _forecast_all_station_regions(selection):
        region_selection = dict(selection)
        region_selection["regions"] = region_code
        parsed = _load_exact_forecast(region_selection)
        if not parsed:
            continue
        region_times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime") or []
        stations = ((parsed.get("data") or {}).get("stations") or {})
        if not region_times or not stations:
            continue

        if base_time is not None:
            region_parsed_times = [_parse_forecast_timestamp(item) for item in region_times]
            matching_indices = [
                idx
                for idx, parsed_time in enumerate(region_parsed_times)
                if parsed_time is not None and parsed_time == base_time
            ]
            region_base_index = matching_indices[0] if matching_indices else min(base_index, len(region_times) - 1)
        else:
            region_base_index = min(base_index, len(region_times) - 1)
        region_horizons = _forecast_horizon_indices(region_times, region_base_index)
        horizon_pairs = list(zip(horizons, region_horizons))
        region_label = region_lookup.get(str(region_code)) or title_case_station_name(str(region_code).replace("_", " "))
        included_regions.append(region_label)

        for station_name, payload in stations.items():
            values = (payload or {}).get("forecastValue") or []
            row = {
                "station": title_case_station_name(station_name),
                "region": region_label,
            }
            sort_value = None
            has_value = False
            for display_horizon, region_horizon in horizon_pairs:
                idx = region_horizon["index"]
                col_id = f"h{display_horizon['offset']}_{display_horizon['index']}"
                value = None
                if idx < len(values):
                    try:
                        value = float(values[idx])
                    except (TypeError, ValueError):
                        value = None
                if value is None:
                    row[col_id] = "--"
                    continue
                category_key, _colour = category_for_value(pollutant_label, value)
                category_label = category_key.replace("-", " ").title()
                row[col_id] = _aq_badge_html(f"{value:.2f}", category_label)
                has_value = True
                if sort_value is None:
                    sort_value = value
            if has_value:
                row["_sort_value"] = sort_value if sort_value is not None else float("-inf")
                rows.append(row)

    rows.sort(key=lambda row: (-(row.get("_sort_value") or float("-inf")), row.get("station") or ""))
    for row in rows:
        row.pop("_sort_value", None)

    base_label = _format_forecast_table_time_label(times[base_index])
    title = f"All available station forecast · {pollutant_display(pollutant_label)}"
    if rows:
        subtitle = f"All available regions for run {selection.get('date') or '--'} · base hour {base_label} · {len(rows)} stations across {len(set(included_regions))} regions"
    else:
        subtitle = f"No station forecast values are available across regions for run {selection.get('date') or '--'}."
    if selected_region_label:
        subtitle = f"{subtitle} · opened from {selected_region_label}"
    if units:
        subtitle = f"{subtitle} · {units}"
    return columns, rows, title, subtitle


def _forecast_overview_allstations_table(selection):
    """Forecast-tab version of the Overview all-stations table."""
    selection = dict(selection or {})
    selection["pollutants"] = "PM2.5"
    selection_for_panel, regions = _overview_pick_multi_region_forecast_selection(selection)
    columns, rows = _overview_allstations_offsets_rows(selection_for_panel, offsets=(1, 3, 6, 12))

    title = "All station forecast"
    if rows:
        subtitle = (
            "PM2.5, PM10 and O3 for all available regions"
            f" · run {selection_for_panel.get('date') or '--'}"
            f" · {len(rows)} stations across {len(set(regions or [])) or '--'} regions"
        )
    else:
        subtitle = f"No all-station forecast values are available for run {selection_for_panel.get('date') or '--'}."
    return columns, rows, title, subtitle


def _default_ranking_station(selection, hour_index):
    rows, _selected_time = _forecast_station_rows(selection, hour_index, selection.get("pollutants"))
    return rows[0]["station_key"] if rows else None


@lru_cache(maxsize=96)
def _forecast_region_mean_series(region_code, pollutant_label, time_scope, model_name, run_date):
    if not region_code or not pollutant_label or not time_scope or not model_name or not run_date:
        return tuple()

    selection = {
        "regions": str(region_code),
        "pollutants": str(pollutant_label),
        "timeScopes": str(time_scope),
        "models": str(model_name),
        "date": str(run_date),
    }
    parsed, _message = _load_forecast(selection)
    if not parsed:
        return tuple()

    times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime", [])
    stations = ((parsed.get("data") or {}).get("stations") or {})
    if not times or not stations:
        return tuple()

    sums = [0.0] * len(times)
    counts = [0] * len(times)
    for payload in stations.values():
        values = (payload or {}).get("forecastValue") or []
        for idx, raw_value in enumerate(values[: len(times)]):
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            sums[idx] += value
            counts[idx] += 1

    means = []
    for idx in range(len(times)):
        if counts[idx] > 0:
            means.append(sums[idx] / float(counts[idx]))
        else:
            means.append(None)
    return tuple(means)


def _station_prediction_cards_unused(selection, hour_index):
    rows, selected_time = _forecast_station_rows(selection, hour_index, selection.get("pollutants"))
    if not rows:
        return html.Div("No forecast values are available for this selection.", className="forecast-rank-empty")

    focus_pollutant = selection.get("pollutants") or "PM2.5"
    pollutant_order = ("PM2.5", "PM10", "O3")

    return html.Div(
        [
            html.Div(
                [
                    html.Button(
                        "All station forecast",
                        n_clicks=0,
                        className="forecast-rank-eyebrow forecast-all-stations-link table-link",
                    ),
                    html.Div(_format_forecast_label(selected_time), className="forecast-rank-time"),
                ],
                className="forecast-rank-summary",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Span(f"#{rank}", className="forecast-station-card__rank"),
                                    html.Span(row["focus_category"], className="forecast-station-card__badge"),
                                ],
                                className="forecast-station-card__top",
                            ),
                            html.Div(row["station"], className="forecast-station-card__station"),
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            html.Div(
                                                [
                                                    html.Div((row["pollutants"].get(pollutant_label) or {}).get("pollutant", pollutant_label), className="forecast-pollutant-box__label"),
                                                    html.Div(
                                                        [
                                                            html.Span(
                                                                "--" if (row["pollutants"].get(pollutant_label) or {}).get("value") is None else f"{(row['pollutants'].get(pollutant_label) or {}).get('value'):.2f}",
                                                                className="forecast-pollutant-box__value",
                                                            ),
                                                            html.Span(
                                                                (row["pollutants"].get(pollutant_label) or {}).get("units") or "",
                                                                className="forecast-pollutant-box__unit",
                                                            ),
                                                        ],
                                                        className="forecast-pollutant-box__metric",
                                                    ),
                                                    html.Span(
                                                        (row["pollutants"].get(pollutant_label) or {}).get("category") or "No data",
                                                        className="forecast-pollutant-box__badge",
                                                    ),
                                                ],
                                                className="forecast-pollutant-box forecast-pollutant-box--focus",
                                                style={"--pollutant-color": row["focus_color"]},
                                            ),
                                        ],
                                        className="forecast-pollutant-boxes",
                                    ),
                                ],
                                className="forecast-station-card__pollutants",
                            ),
                        ],
                        className="forecast-station-card",
                        style={"--rank-color": row["focus_color"]},
                    )
                    for rank, row in enumerate(rows, start=1)
                ],
                className="forecast-rank-grid forecast-rank-grid--stations",
            ),
        ],
        className="forecast-rank-panel",
    )


def _selected_station_details(parsed, pollutant_label, station_name, hour_index):
    if not parsed or not station_name:
        return "Choose a station from the list to inspect its forecast profile."

    stations = ((parsed.get("data") or {}).get("stations") or {})
    payload = stations.get(station_name) or {}
    values = payload.get("forecastValue") or []
    times = ((parsed.get("data") or {}).get("time") or {}).get("forecastTime", [])
    if not values or not times:
        return f"{title_case_station_name(station_name)} selected. No forecast series available."

    idx = _coerce_forecast_hour_index(parsed, hour_index)
    if idx >= len(values):
        idx = len(values) - 1

    try:
        value = float(values[idx])
        value_text = f"{value:.2f}"
    except (TypeError, ValueError):
        value = None
        value_text = "--"

    category, _color = category_for_value(pollutant_label, value)
    category_text = category.replace("-", " ").title()
    units = (POLLUTANTS.get(pollutant_label) or {}).get("Units") or ""
    time_text = _format_forecast_label(times[idx] if idx < len(times) else None)

    return f"{title_case_station_name(station_name)} · {pollutant_label}: {value_text} {units} · {category_text} · {time_text}".strip()


def _station_series_colour(station_name):
    key = normalize_station_name(station_name or "")
    if not key:
        return STATION_SERIES_PALETTE[0]
    index = sum(ord(char) for char in key) % len(STATION_SERIES_PALETTE)
    return STATION_SERIES_PALETTE[index]


def _station_trend_figure(parsed, pollutant_label, station_name):
    selection = (parsed or {}).get("_selection") or {}
    return _region_forecast_figure(parsed, selection, pollutant_label)


def _region_forecast_figure(parsed, selection, pollutant_label):
    figure = go.Figure()
    trend_margin = dict(l=54, r=18, t=42, b=98)
    if not parsed or not selection:
        figure.update_layout(
            template="plotly_white",
            height=340,
            margin=trend_margin,
            font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="#ffffff",
        )
        return figure

    region_code = str(selection.get("regions") or "")
    region_name = load_region_lookup().get(region_code) or title_case_station_name(region_code) or "Selected region"
    time_scope = str(selection.get("timeScopes") or "12")
    times = parsed.get("data", {}).get("time", {}).get("forecastTime", [])
    stations = parsed.get("data", {}).get("stations", {})
    if not times or not stations:
        figure.update_layout(
            template="plotly_white",
            height=340,
            margin=trend_margin,
            font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="#ffffff",
        )
        return figure

    station_series = []
    max_len = len(times)
    for station_name, payload in sorted(stations.items(), key=lambda item: title_case_station_name(item[0])):
        values = []
        for raw_value in ((payload or {}).get("forecastValue") or [])[:max_len]:
            try:
                values.append(float(raw_value))
            except (TypeError, ValueError):
                values.append(None)
        if not any(value is not None for value in values):
            continue
        station_series.append((station_name, values))

    if not station_series:
        figure.update_layout(
            template="plotly_white",
            height=340,
            margin=trend_margin,
            font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="#ffffff",
        )
        return figure

    for station_name, values in station_series:
        station_label = title_case_station_name(station_name)
        colour = _station_series_colour(station_name)
        figure.add_trace(
            go.Scatter(
                x=times[: len(values)],
                y=values,
                mode="lines+markers",
                line=dict(color=colour, width=2.2, shape="spline", smoothing=0.55),
                marker=dict(size=5.5, color=colour, line=dict(color="#ffffff", width=0.8)),
                name=station_label,
                hovertemplate=f"<b>{station_label}</b><br>%{{x}}<br>%{{y:.2f}}<extra></extra>",
            )
        )

    means = []
    for idx in range(max_len):
        values_at_time = [
            values[idx]
            for _station_name, values in station_series
            if idx < len(values) and values[idx] is not None
        ]
        means.append(sum(values_at_time) / len(values_at_time) if values_at_time else None)
    if any(value is not None for value in means):
        figure.add_trace(
            go.Scatter(
                x=times[: len(means)],
                y=means,
                mode="lines",
                line=dict(color="#0f172a", width=3.0, dash="dash"),
                name="Region average",
                hovertemplate="<b>Region average</b><br>%{x}<br>%{y:.2f}<extra></extra>",
            )
        )

    pollutant = POLLUTANTS.get(pollutant_label, {})
    figure.update_layout(
        template="plotly_white",
        height=340,
        margin=trend_margin,
        title=dict(
            text=f"{region_name} @ {time_scope} hr forecast",
            font=dict(family=BASE_FONT_FAMILY, color="#64748b", size=18),
        ),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#ffffff",
        hovermode="x unified",
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.30,
            xanchor="center",
            x=0.5,
            font=dict(family=BASE_FONT_FAMILY, size=10, color="#475569"),
            title=dict(text="Stations", font=dict(family=BASE_FONT_FAMILY, size=11, color="#334155")),
            itemclick="toggle",
            itemdoubleclick="toggleothers",
            traceorder="normal",
        ),
        annotations=[
            dict(
                text=f"{len(station_series)} station forecasts in selected region",
                x=0.0,
                y=1.08,
                xref="paper",
                yref="paper",
                showarrow=False,
                font=dict(family=BASE_FONT_FAMILY, size=12, color="#94a3b8"),
                align="left",
            )
        ],
        xaxis=dict(
            title=dict(text="Forecast time", font=dict(family=BASE_FONT_FAMILY, color="#0f172a"), standoff=12),
            tickfont=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
            gridcolor="#e8eef8",
            zeroline=False,
            automargin=True,
        ),
        yaxis=dict(
            title=dict(text=pollutant.get("Units", "Value"), font=dict(family=BASE_FONT_FAMILY, color="#0f172a")),
            tickfont=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
            gridcolor="#e8eef8",
            zeroline=False,
        ),
    )
    return figure


def _all_stations_details(parsed):
    return ""


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


def _station_prediction_cards(selection, hour_index):
    rows, selected_time = _forecast_station_rows(selection, hour_index, selection.get("pollutants"))
    if not rows:
        return html.Div("No forecast values are available for this selection.", className="forecast-rank-empty")

    focus_pollutant = selection.get("pollutants") or "PM2.5"
    pollutant_order = ("PM2.5", "PM10", "O3")

    cards = []
    for rank, row in enumerate(rows, start=1):
        pollutant_boxes = []
        for pollutant_label in pollutant_order:
            payload = row["pollutants"].get(pollutant_label)
            pollutant_boxes.append(
                html.Div(
                    [
                        html.Div(pollutant_label, className="forecast-pollutant-box__label"),
                        html.Div(
                            [
                                html.Span(
                                    "--" if payload is None or payload.get("value") is None else f"{payload['value']:.2f}",
                                    className="forecast-pollutant-box__value",
                                ),
                                html.Span(
                                    payload.get("units") if payload else "",
                                    className="forecast-pollutant-box__unit",
                                ),
                            ],
                            className="forecast-pollutant-box__metric",
                        ),
                        html.Span(
                            payload.get("category") if payload else "No data",
                            className="forecast-pollutant-box__badge",
                        ),
                    ],
                    className="forecast-pollutant-box",
                    style={"--pollutant-color": (payload or {}).get("color") or "#94a3b8"},
                )
            )

        focus_payload = row["pollutants"].get(focus_pollutant)
        focus_value = "--" if focus_payload is None or focus_payload.get("value") is None else f"{focus_payload['value']:.2f}"
        focus_units = focus_payload.get("units") if focus_payload else ""

        cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Span(f"#{rank}", className="forecast-station-card__rank"),
                            html.Span(row["focus_category"], className="forecast-station-card__badge"),
                        ],
                        className="forecast-station-card__top",
                    ),
                    html.Div(row["station"], className="forecast-station-card__station"),
                    html.Div(pollutant_boxes, className="forecast-pollutant-boxes"),
                    html.Div(
                        [
                            html.Span(focus_pollutant, className="forecast-station-card__focus-label"),
                            html.Span(focus_value, className="forecast-station-card__focus-value"),
                            html.Span(focus_units, className="forecast-station-card__focus-unit"),
                        ],
                        className="forecast-station-card__focus",
                    ),
                ],
                className="forecast-station-card",
                style={"--rank-color": row["focus_color"]},
            )
        )

    return html.Div(cards, className="forecast-rank-grid forecast-rank-grid--stations")

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


def _forecast_station_region(station_name, fallback_region=None):
    site = _site_lookup().get(normalize_station_name(station_name)) or {}
    region = site.get("Region") or fallback_region
    return str(region or "").replace(" ", "_").strip()


def _forecast_station_options(parsed, region_value=None):
    stations = ((parsed or {}).get("data") or {}).get("stations") or {}
    region_key = _monitor_region_series_key(region_value)
    options = []
    for station_name in sorted(stations, key=lambda name: title_case_station_name(name)):
        station_region = _forecast_station_region(station_name, (parsed or {}).get("_region"))
        if region_key and _monitor_region_series_key(station_region) != region_key:
            continue
        options.append({"label": title_case_station_name(station_name), "value": station_name})
    return options


def _forecast_region_options(parsed, selected_region=None):
    stations = ((parsed or {}).get("data") or {}).get("stations") or {}
    regions = []
    for station_name in stations:
        station_region = _forecast_station_region(station_name, selected_region or (parsed or {}).get("_region"))
        if station_region and station_region not in regions:
            regions.append(station_region)
    if selected_region and selected_region not in regions:
        regions.insert(0, selected_region)
    return [{"label": str(region).replace("_", " "), "value": region} for region in sorted(regions)]


def _default_forecast_station(parsed, region_value=None):
    options = _forecast_station_options(parsed, region_value)
    if options:
        return options[0]["value"]
    all_options = _forecast_station_options(parsed, None)
    return all_options[0]["value"] if all_options else None


def _forecast_time_series_figure(parsed, pollutant_label, station_name, hour_index, region_value=None):
    figure = go.Figure()
    if not parsed:
        figure.update_layout(template="plotly_white", height=380, margin=dict(l=20, r=20, t=40, b=20))
        return figure

    station_name = station_name or _default_forecast_station(parsed, region_value)
    if not station_name:
        figure.update_layout(template="plotly_white", height=380, margin=dict(l=20, r=20, t=40, b=20))
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
        figure.update_layout(template="plotly_white", height=380, margin=dict(l=20, r=20, t=40, b=20))
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
    first_forecast_time = forecast_times[0] if forecast_times else None
    joined_forecast_times = list(forecast_times)
    joined_forecast_values = list(forecast_values[: len(forecast_times)])
    if hist_times and hist_values and first_forecast_time:
        joined_forecast_times = [hist_times[-1]] + joined_forecast_times
        joined_forecast_values = [hist_values[min(len(hist_times), len(hist_values)) - 1]] + joined_forecast_values

    # Observed monitoring (hist portion in the CSV)
    if hist_times and hist_values:
        figure.add_trace(
            go.Scatter(
                x=hist_times,
                y=hist_values[: len(hist_times)],
                mode="lines+markers",
                line=dict(color="#0f172a", width=3.3, shape="spline", smoothing=0.55),
                marker=dict(size=7, color="#0f172a", line=dict(color="#ffffff", width=1.4)),
                name="Observed (monitoring)",
                hovertemplate="<b>Observed</b><br>%{x|%d %b %Y, %I:%M %p}<br>%{y:.2f}<extra></extra>",
            )
        )

    # Forecast mean
    if joined_forecast_times and joined_forecast_values:
        figure.add_trace(
            go.Scatter(
                x=joined_forecast_times,
                y=joined_forecast_values[: len(joined_forecast_times)],
                mode="lines+markers",
                line=dict(color="#2563eb", width=4, shape="spline", smoothing=0.62),
                marker=dict(size=8, color="#2563eb", line=dict(color="#ffffff", width=1.6)),
                fill="tozeroy",
                fillcolor="rgba(37, 99, 235, 0.10)",
                name="Forecast",
                hovertemplate="<b>Forecast</b><br>%{x|%d %b %Y, %I:%M %p}<br>%{y:.2f}<extra></extra>",
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
                y=upper,
                mode="lines",
                line=dict(color="rgba(96, 165, 250, 0.0)", width=0),
                hoverinfo="skip",
                showlegend=False,
                name="Upper bound",
            )
        )
        figure.add_trace(
            go.Scatter(
                x=forecast_times,
                y=lower,
                mode="lines",
                line=dict(color="rgba(96, 165, 250, 0.55)", width=2, dash="dot"),
                fill="tonexty",
                fillcolor="rgba(96, 165, 250, 0.18)",
                name="Forecast range",
                hovertemplate="<b>Lower range</b><br>%{x|%d %b %Y, %I:%M %p}<br>%{y:.2f}<extra></extra>",
            )
        )

    # Forecast start marker (use raw string so Plotly shape helpers don't choke on datetimes)
    forecast_start_raw = forecast_times_raw[0] if forecast_times_raw else None
    if forecast_start_raw is not None:
        figure.add_shape(
            type="line",
            x0=forecast_start_raw,
            x1=forecast_start_raw,
            xref="x",
            y0=0,
            y1=1,
            yref="paper",
            line=dict(color="#94a3b8", width=2, dash="dash"),
        )
        figure.add_annotation(
            x=forecast_start_raw,
            xref="x",
            y=1,
            yref="paper",
            text="Forecast start",
            showarrow=False,
            xanchor="left",
            yanchor="bottom",
            font=dict(color="#64748b", size=11, family=BASE_FONT_FAMILY),
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="rgba(148, 163, 184, 0.65)",
            borderwidth=1,
            borderpad=3,
        )

    pollutant = POLLUTANTS.get(pollutant_label, {})
    units = pollutant.get("Units") or "Value"
    station_title = title_case_station_name(station_name)
    region_title = str(region_value or _forecast_station_region(station_name) or "").replace("_", " ")
    title_text = f"{station_title} observed + forecast"

    figure.update_layout(
        template="plotly_white",
        height=390,
        margin=dict(l=62, r=24, t=64, b=56),
        title=dict(
            text=f"{title_text}<br><sup>{region_title} • {pollutant_display(pollutant_label)}</sup>",
            x=0.01,
            xanchor="left",
            font=dict(family=BASE_FONT_FAMILY, size=17, color="#0f172a"),
        ),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#ffffff",
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.03,
            xanchor="right",
            x=1,
            font=dict(size=11),
            bgcolor="rgba(255,255,255,0.72)",
        ),
        xaxis=dict(
            title=dict(text="Time (AEST)", font=dict(family=BASE_FONT_FAMILY, color="#0f172a")),
            gridcolor="#e6eef8",
            zeroline=False,
            tickformat="%H:%M<br>%b %d",
        ),
        yaxis=dict(
            title=dict(text=units, font=dict(family=BASE_FONT_FAMILY, color="#0f172a")),
            gridcolor="#e6eef8",
            zerolinecolor="#dbeafe",
            rangemode="tozero",
        ),
    )
    return figure


def _safe_metric_value(value, digits=2):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "--"
    if not math.isfinite(number):
        return "--"
    return f"{number:.{digits}f}"


def _validation_station_stats(parsed, station_name):
    stats = (parsed or {}).get("stats") or {}
    if station_name in stats:
        return stats.get(station_name) or {}
    target = normalize_station_name(station_name)
    for key, value in stats.items():
        if normalize_station_name(key) == target:
            return value or {}
    return {}


def _validation_time_key(value):
    ts = value if isinstance(value, datetime) else _parse_forecast_timestamp(value)
    if ts is None:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(SYDNEY_TZ).replace(tzinfo=None)
    return ts.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:00")


def _validation_forecast_window(parsed):
    times_raw = ((parsed or {}).get("data") or {}).get("time", {}).get("forecastTime", [])
    parsed_times = []
    for item in times_raw or []:
        ts = _parse_forecast_timestamp(item)
        if ts is not None:
            if ts.tzinfo is not None:
                ts = ts.astimezone(SYDNEY_TZ).replace(tzinfo=None)
            parsed_times.append(ts.replace(minute=0, second=0, microsecond=0))
    if not parsed_times:
        return None, None
    start_dt = min(parsed_times)
    latest_observed_dt = datetime.now(SYDNEY_TZ).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    end_dt = min(max(parsed_times), latest_observed_dt)
    if start_dt > end_dt:
        return None, None
    return start_dt.strftime("%Y-%m-%dT%H:00:00"), end_dt.strftime("%Y-%m-%dT%H:00:00")


@lru_cache(maxsize=96)
def _fetch_validation_observed_history(site_ids_tuple, parameter_code, start_iso, end_iso):
    site_ids = []
    for site_id in site_ids_tuple or ():
        try:
            site_ids.append(int(site_id))
        except (TypeError, ValueError):
            continue
    if not site_ids or not parameter_code or not start_iso or not end_iso:
        return []
    try:
        return fetch_observation_history(site_ids, [parameter_code], start_iso, end_iso, timeout=18)
    except Exception:
        return []


def _validation_observed_by_station(parsed, pollutant_label, region_value=None):
    station_options = _forecast_station_options(parsed, region_value)
    if not station_options:
        return {}

    station_by_site = {}
    for option in station_options:
        station_name = option["value"]
        site = _site_lookup().get(normalize_station_name(station_name)) or {}
        site_id = site.get("Site_Id")
        if site_id is None:
            continue
        try:
            station_by_site[int(site_id)] = station_name
        except (TypeError, ValueError):
            continue
    if not station_by_site:
        return {}

    start_iso, end_iso = _validation_forecast_window(parsed)
    if not start_iso or not end_iso:
        return {station_name: {} for station_name in station_by_site.values()}
    parameter_code = (POLLUTANTS.get(pollutant_label) or {}).get("ParameterCode") or pollutant_label
    rows = _fetch_validation_observed_history(tuple(sorted(station_by_site)), parameter_code, start_iso, end_iso)
    observed = {station_name: {} for station_name in station_by_site.values()}
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        param = _aqms_parameter(item)
        if str(param.get("ParameterCode") or "").strip() != str(parameter_code):
            continue
        if (param.get("Frequency") or "").strip() != "Hourly average":
            continue
        try:
            site_id = int(item.get("Site_Id"))
        except (TypeError, ValueError):
            continue
        station_name = station_by_site.get(site_id)
        if not station_name:
            continue
        key = _validation_time_key(_parse_aqms_time_point(item))
        if not key:
            continue
        try:
            value = float(item.get("Value")) if item.get("Value") is not None else None
        except (TypeError, ValueError):
            value = None
        if value is None:
            continue
        observed.setdefault(station_name, {})[key] = value
    return observed


def _validation_station_matched_pairs(parsed, station_name, observed_map=None):
    stations = ((parsed or {}).get("data") or {}).get("stations") or {}
    payload = stations.get(station_name) or {}
    forecast_times_raw = ((parsed or {}).get("data") or {}).get("time", {}).get("forecastTime", [])
    forecast_values = payload.get("forecastValue", []) if isinstance(payload, dict) else []
    pairs = []

    for idx, raw_time in enumerate(forecast_times_raw[: len(forecast_values)]):
        key = _validation_time_key(raw_time)
        if not key:
            continue
        try:
            forecast_value = float(forecast_values[idx])
            observed_value = float((observed_map or {}).get(key))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(forecast_value) and math.isfinite(observed_value)):
            continue
        pairs.append((_parse_forecast_timestamp(raw_time) or raw_time, observed_value, forecast_value))

    return pairs


def _validation_figure(parsed, pollutant_label, hour_index, region_value=None):
    figure = go.Figure()
    if not parsed:
        figure.update_layout(template="plotly_white", height=430, margin=dict(l=24, r=24, t=44, b=42))
        return figure

    station_options = _forecast_station_options(parsed, region_value)
    station_names = [option["value"] for option in station_options]
    stations = ((parsed or {}).get("data") or {}).get("stations") or {}
    hist_times_raw = ((parsed or {}).get("data") or {}).get("time", {}).get("histTime", [])
    forecast_times_raw = ((parsed or {}).get("data") or {}).get("time", {}).get("forecastTime", [])

    def _parse_list(items):
        parsed_items = []
        for item in items or []:
            parsed_ts = _parse_forecast_timestamp(item)
            parsed_items.append(parsed_ts or item)
        return parsed_items

    hist_times = _parse_list(hist_times_raw)
    forecast_times = _parse_list(forecast_times_raw)
    hour_index = _coerce_forecast_hour_index(parsed, hour_index)
    selected_time = forecast_times[hour_index] if hour_index < len(forecast_times) else None

    palette = ["#2563eb", "#ef4444", "#10b981", "#8b5cf6", "#f59e0b", "#06b6d4", "#ec4899", "#84cc16"]
    for idx, station_name in enumerate(station_names):
        payload = stations.get(station_name) or {}
        colour = palette[idx % len(palette)]
        hist_values = payload.get("histValue", []) if isinstance(payload, dict) else []
        forecast_values = payload.get("forecastValue", []) if isinstance(payload, dict) else []
        station_label = title_case_station_name(station_name)
        hist_x = hist_times[: len(hist_values)]
        forecast_x = forecast_times[: len(forecast_values)]
        forecast_y = list(forecast_values[: len(forecast_x)])
        if hist_x and hist_values and forecast_x:
            forecast_x = [hist_x[-1]] + list(forecast_x)
            forecast_y = [hist_values[min(len(hist_x), len(hist_values)) - 1]] + forecast_y

        if hist_x and hist_values:
            figure.add_trace(
                go.Scatter(
                    x=hist_x,
                    y=hist_values[: len(hist_x)],
                    mode="lines+markers",
                    line=dict(color=colour, width=2.4, shape="spline", smoothing=0.55),
                    marker=dict(size=6, color=colour, line=dict(color="#ffffff", width=1.2)),
                    name=f"{station_label} observed",
                    legendgroup=station_label,
                    hovertemplate=f"<b>{station_label}</b><br>Observed<br>%{{x|%d %b %Y, %I:%M %p}}<br>%{{y:.2f}}<extra></extra>",
                )
            )
        if forecast_x and forecast_y:
            figure.add_trace(
                go.Scatter(
                    x=forecast_x,
                    y=forecast_y,
                    mode="lines+markers",
                    line=dict(color=colour, width=3.1, dash="dash", shape="spline", smoothing=0.6),
                    marker=dict(size=7, color=colour, symbol="diamond", line=dict(color="#ffffff", width=1.2)),
                    name=f"{station_label} forecast",
                    legendgroup=station_label,
                    hovertemplate=f"<b>{station_label}</b><br>Forecast<br>%{{x|%d %b %Y, %I:%M %p}}<br>%{{y:.2f}}<extra></extra>",
                )
            )

    if forecast_times_raw:
        figure.add_shape(
            type="line",
            x0=forecast_times_raw[0],
            x1=forecast_times_raw[0],
            xref="x",
            y0=0,
            y1=1,
            yref="paper",
            line=dict(color="#64748b", width=2, dash="dot"),
        )

    units = (POLLUTANTS.get(pollutant_label) or {}).get("Units") or "Value"
    title_region = str(region_value or (parsed or {}).get("_region") or "Selected region").replace("_", " ")
    figure.update_layout(
        template="plotly_white",
        height=460,
        margin=dict(l=62, r=26, t=72, b=54),
        title=dict(
            text=f"Validation time series<br><sup>{title_region} • {pollutant_display(pollutant_label)} • observed and forecast where available</sup>",
            x=0.01,
            xanchor="left",
            font=dict(family=BASE_FONT_FAMILY, size=18, color="#0f172a"),
        ),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#ffffff",
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.03,
            xanchor="right",
            x=1,
            font=dict(size=10),
            bgcolor="rgba(255,255,255,0.78)",
        ),
        xaxis=dict(title="Time (AEST)", gridcolor="#e6eef8", zeroline=False, tickformat="%H:%M<br>%b %d"),
        yaxis=dict(title=units, gridcolor="#e6eef8", zerolinecolor="#dbeafe", rangemode="tozero"),
    )
    return figure


def _validation_station_comparison_stats(parsed, station_name, observed_map=None):
    pairs = [(observed_value, forecast_value) for _, observed_value, forecast_value in _validation_station_matched_pairs(parsed, station_name, observed_map)]

    if not pairs:
        return _validation_station_stats(parsed, station_name), 0

    errors = [forecast_value - observed_value for observed_value, forecast_value in pairs]
    rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
    mae = sum(abs(error) for error in errors) / len(errors)
    pearson = None
    if len(pairs) >= 2:
        observed_values = [observed_value for observed_value, _ in pairs]
        forecast_values = [forecast_value for _, forecast_value in pairs]
        observed_mean = sum(observed_values) / len(observed_values)
        forecast_mean = sum(forecast_values) / len(forecast_values)
        numerator = sum((observed_value - observed_mean) * (forecast_value - forecast_mean) for observed_value, forecast_value in pairs)
        observed_ss = sum((observed_value - observed_mean) ** 2 for observed_value in observed_values)
        forecast_ss = sum((forecast_value - forecast_mean) ** 2 for forecast_value in forecast_values)
        denominator = math.sqrt(observed_ss * forecast_ss)
        if denominator:
            pearson = numerator / denominator

    return {"PEARSON_R": pearson, "RMSE": rmse, "MAE": mae}, len(pairs)


def _validation_station_figure(parsed, pollutant_label, region_value, station_name, colour, observed_by_station=None):
    figure = go.Figure()
    observed_map = (observed_by_station or {}).get(station_name) or {}
    matched_pairs = _validation_station_matched_pairs(parsed, station_name, observed_map)
    matched_x = [item[0] for item in matched_pairs]
    observed_y = [item[1] for item in matched_pairs]
    forecast_y = [item[2] for item in matched_pairs]

    station_label = title_case_station_name(station_name)
    if matched_pairs:
        figure.add_trace(
            go.Scatter(
                x=matched_x,
                y=observed_y,
                mode="lines+markers",
                line=dict(color="#0f172a", width=2.4, shape="spline", smoothing=0.55),
                marker=dict(size=6, color="#0f172a", line=dict(color="#ffffff", width=1.1)),
                name="Observed",
                hovertemplate="<b>Observed</b><br>%{x|%d %b %Y, %I:%M %p}<br>%{y:.2f}<extra></extra>",
            )
        )
        figure.add_trace(
            go.Scatter(
                x=matched_x,
                y=forecast_y,
                mode="lines+markers",
                line=dict(color=colour, width=3.2, shape="spline", smoothing=0.6),
                marker=dict(size=7, color=colour, symbol="diamond", line=dict(color="#ffffff", width=1.2)),
                fill="tozeroy",
                fillcolor=_hex_to_rgba(colour, 0.10),
                name="Forecast",
                hovertemplate="<b>Forecast</b><br>%{x|%d %b %Y, %I:%M %p}<br>%{y:.2f}<extra></extra>",
            )
        )
    else:
        figure.add_annotation(
            text="No observed values are available for this forecast window yet",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
            font=dict(size=12, color="#64748b", family=BASE_FONT_FAMILY),
            bgcolor="rgba(248,250,252,0.86)",
            bordercolor="#dbeafe",
            borderwidth=1,
            borderpad=5,
        )

    stats, matched_count = _validation_station_comparison_stats(parsed, station_name, observed_map)
    if matched_count:
        matched_label = f" · {matched_count} matched hours"
    else:
        matched_label = " · observed unavailable"
    subtitle = f"R {_safe_metric_value(stats.get('PEARSON_R'), 2)} · RMSE {_safe_metric_value(stats.get('RMSE'), 2)}{matched_label}"
    units = (POLLUTANTS.get(pollutant_label) or {}).get("Units") or "Value"
    figure.update_layout(
        template="plotly_white",
        height=310,
        margin=dict(l=48, r=16, t=54, b=38),
        title=dict(
            text=f"{station_label}<br><sup>{subtitle}</sup>",
            x=0.01,
            xanchor="left",
            font=dict(family=BASE_FONT_FAMILY, size=15, color="#0f172a"),
        ),
        font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="#ffffff",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=10)),
        xaxis=dict(gridcolor="#e6eef8", zeroline=False, tickformat="%H:%M<br>%b %d"),
        yaxis=dict(title=units, gridcolor="#e6eef8", zerolinecolor="#dbeafe", rangemode="tozero"),
    )
    return figure


def _validation_station_plot_grid(parsed, pollutant_label, region_value=None):
    if not parsed:
        return html.Div("No validation time-series data is available for this selection.", className="validation-empty")

    station_options = _forecast_station_options(parsed, region_value)
    observed_by_station = _validation_observed_by_station(parsed, pollutant_label, region_value)
    station_names = [
        option["value"]
        for option in station_options
        if _validation_station_matched_pairs(parsed, option["value"], (observed_by_station or {}).get(option["value"]) or {})
    ]
    if not station_names:
        return html.Div(
            "No observed forecast-hour matches are available for this validation selection yet.",
            className="validation-empty",
        )

    palette = ["#2563eb", "#ef4444", "#10b981", "#8b5cf6", "#f59e0b", "#06b6d4", "#ec4899", "#84cc16"]
    count = len(station_names)
    if count == 1:
        size_class = "validation-station-grid--1"
    elif count == 2:
        size_class = "validation-station-grid--2"
    elif count == 3:
        size_class = "validation-station-grid--3"
    else:
        size_class = "validation-station-grid--many"

    cards = []
    for idx, station_name in enumerate(station_names):
        cards.append(
            html.Div(
                dcc.Graph(
                    figure=_validation_station_figure(
                        parsed,
                        pollutant_label,
                        region_value,
                        station_name,
                        palette[idx % len(palette)],
                        observed_by_station,
                    ),
                    config={"displayModeBar": False},
                ),
                className="validation-station-plot-card",
            )
        )

    return html.Div(cards, className=f"validation-station-grid {size_class}")


def _validation_metric_cards(parsed, pollutant_label, hour_index, region_value=None):
    if not parsed:
        return html.Div("No validation data is available for this selection.", className="validation-empty")

    station_options = _forecast_station_options(parsed, region_value)
    observed_by_station = _validation_observed_by_station(parsed, pollutant_label, region_value)
    matched_station_options = [
        option
        for option in station_options
        if _validation_station_matched_pairs(parsed, option["value"], (observed_by_station or {}).get(option["value"]) or {})
    ]
    station_count = len(matched_station_options)
    if station_count == 1:
        size_class = "validation-metric-grid--1"
    elif station_count == 2:
        size_class = "validation-metric-grid--2"
    elif station_count == 3:
        size_class = "validation-metric-grid--3"
    else:
        size_class = "validation-metric-grid--many"
    cards = []
    for option in matched_station_options:
        station_name = option["value"]
        stats, matched_count = _validation_station_comparison_stats(parsed, station_name, (observed_by_station or {}).get(station_name) or {})
        pearson = _safe_metric_value(stats.get("PEARSON_R"), 2)
        rmse = _safe_metric_value(stats.get("RMSE"), 2)
        mae = _safe_metric_value(stats.get("MAE"), 2)
        footnote = f"{matched_count} observed forecast-hour matches"
        cards.append(
            html.Div(
                [
                    html.Div(title_case_station_name(station_name), className="validation-metric-card__station"),
                    html.Div(
                        [
                            html.Div([html.Span("R"), html.Strong(pearson)], className="validation-metric-card__metric"),
                            html.Div([html.Span("RMSE"), html.Strong(rmse)], className="validation-metric-card__metric"),
                            html.Div([html.Span("MAE"), html.Strong(mae)], className="validation-metric-card__metric"),
                        ],
                        className="validation-metric-card__grid",
                    ),
                    html.Div(footnote, className="validation-metric-card__footnote"),
                ],
                className="validation-metric-card",
            )
        )

    if not cards:
        return html.Div(
            "No observed forecast-hour matches are available for this validation selection yet.",
            className="validation-empty",
        )
    return html.Div(cards, className=f"validation-metric-grid {size_class}")


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
    base_index = 0

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
                    base_index = _forecast_live_base_index(time_labels)

            stations = ((parsed.get("data") or {}).get("stations") or {})
            for station_name, payload in stations.items():
                row = _ensure_row(station_name, region_name)
                if not row:
                    continue
                series = (payload or {}).get("forecastValue") or []
                for off in offsets:
                    hour_idx = base_index + off - 1
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
            time_index = base_index + off - 1
            if 0 <= time_index < len(time_labels):
                label = _forecast_hour_label(time_labels[time_index])
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
            width: 34px;
            height: 34px;
            border: none;
            border-radius: 50%;
            box-sizing: border-box;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #ffffff;
            font-weight: 800;
            font-size: 12px;
            line-height: 1;
            text-shadow: 0 1px 2px rgba(0, 0, 0, 0.55);
            box-shadow: 0 3px 10px rgba(15, 23, 42, 0.22);
        }}
        .purpleair-marker {{
            width: 22px;
            height: 22px;
            border: none;
            border-radius: 50%;
            box-sizing: border-box;
            background: {PURPLEAIR_COLOR};
            color: #fff;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 9px;
            font-weight: 900;
            line-height: 1;
            box-shadow: 0 4px 12px rgba(15, 23, 42, 0.24);
        }}
        .purpleair-cluster {{
            width: 38px;
            height: 38px;
            border-radius: 50%;
            background: {PURPLEAIR_COLOR};
            color: #fff;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 900;
            font-size: 13px;
            box-shadow: 0 5px 14px rgba(15, 23, 42, 0.28);
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
        const sensorGroup = L.markerClusterGroup({{
            iconCreateFunction: function(cluster) {{
                return L.divIcon({{
                    html: `<div class="purpleair-cluster">${{cluster.getChildCount()}}</div>`,
                    className: "",
                    iconSize: [38, 38],
                    iconAnchor: [19, 19]
                }});
            }},
            showCoverageOnHover: false,
            maxClusterRadius: 60
        }}).addTo(group);
        const purpleAirMarkers = new Map();
        const forecastMarkers = [];

        function purpleAirCategory(value) {{
            if (value == null || Number.isNaN(Number(value))) {{
                return {{ label: "PurpleAir sensor", colour: "{PURPLEAIR_COLOR}" }};
            }}
            const pm25 = Number(value);
            if (pm25 < 25) return {{ label: "Good", colour: "#16a34a" }};
            if (pm25 < 50) return {{ label: "Fair", colour: "#facc15" }};
            if (pm25 < 100) return {{ label: "Poor", colour: "#f97316" }};
            if (pm25 < 300) return {{ label: "Very poor", colour: "#ef4444" }};
            return {{ label: "Extremely poor", colour: "#7f1d1d" }};
        }}

        function purpleAirSize(value) {{
            if (value == null || Number.isNaN(Number(value))) return 22;
            const pm25 = Math.max(Number(value), 0);
            return Math.max(22, Math.min(44, 22 + Math.sqrt(pm25) * 2.3));
        }}

        function purpleAirIcon(item) {{
            const size = purpleAirSize(item.pm25);
            const category = purpleAirCategory(item.pm25);
            const label = item.pm25 == null ? "" : formatPaValue(item.pm25);
            return L.divIcon({{
                className: "",
                html: `<div class="purpleair-marker" style="width:${{size}}px;height:${{size}}px;background:${{category.colour}};font-size:${{size > 30 ? 10 : 9}}px">${{label}}</div>`,
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
                PM2.5: <strong>${{item.pm25 == null ? "--" : formatPaValue(item.pm25)}}</strong><br>
                Category: <strong>${{item.paCategory || "PurpleAir sensor"}}</strong><br>
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
                html: `<div class="station-marker" style="background:${{item.colour}}">${{item.valueLabel}}</div>`,
                iconSize: [34, 34],
                iconAnchor: [17, 17],
                popupAnchor: [0, -18]
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
            const marker = L.marker([item.lat, item.lon], {{ icon: item.source === "PurpleAir" ? purpleAirIcon(item) : forecastIcon(item) }});
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

        // Ensure popups can scroll when content grows (bars section).
        try {{
            if (L && L.DomUtil) {{
                const styleTag = document.createElement("style");
                styleTag.textContent = ".leaflet-popup-content.leaflet-popup-scrolled {{ overflow-y: auto; }}";
                document.head.appendChild(styleTag);
            }}
        }} catch (err) {{}}

        applyForecastHourIndex(0);

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
            }}
            const category = purpleAirCategory(item.pm25);
            item.paCategory = category.label;
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

        // PurpleAir rows are injected from the server/cache. Avoid a browser-side
        // API refresh here because public clients may not have a valid API quota.

        // Keep view anchored on NSW rather than tightly fitting station clusters.
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
                <div class="legend-row"><span class="legend-swatch" style="background:{PURPLEAIR_COLOR}; border-radius:50%"></span>PurpleAir sensor / cluster</div>
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

    def _leaflet_value_label(row):
        value_label = row.get("value_label")
        if value_label not in (None, ""):
            return str(value_label)
        if row.get("value") is None:
            return "--"
        try:
            return f"{float(row.get('value')):.1f}"
        except (TypeError, ValueError):
            return str(row.get("value"))

    if rows:
        for row in rows:
            lat = row.get("lat")
            lon = row.get("lon")
            if lat is None or lon is None:
                continue
            if not _monitor_has_numeric_value(row):
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
                    "pm25": row.get("value") if row.get("source") == "PurpleAir" else None,
                    "paCategory": category if row.get("source") == "PurpleAir" else None,
                    "valueLabel": _leaflet_value_label(row),
                    "values": row.get("values") or [],
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
      width: 34px;
      height: 34px;
      border: none;
      border-radius: 50%;
      box-sizing: border-box;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #ffffff;
      font-weight: 800;
      font-size: 12px;
      line-height: 1;
      text-shadow: 0 1px 2px rgba(0, 0, 0, 0.55);
      box-shadow: 0 3px 10px rgba(15, 23, 42, 0.22);
    }}
    .purpleair-marker {{
      width: 22px;
      height: 22px;
      border: none;
      border-radius: 50%;
      box-sizing: border-box;
      background: {PURPLEAIR_COLOR};
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 9px;
      font-weight: 900;
      line-height: 1;
      box-shadow: 0 4px 12px rgba(15, 23, 42, 0.24);
    }}
    .purpleair-cluster {{
      width: 38px;
      height: 38px;
      border-radius: 50%;
      background: {PURPLEAIR_COLOR};
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 900;
      font-size: 13px;
      box-shadow: 0 5px 14px rgba(15, 23, 42, 0.28);
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
    .aqms-popup-values {{ border-collapse: collapse; margin-top: 8px; min-width: 260px; }}
    .aqms-popup-values th, .aqms-popup-values td {{ border-bottom: 1px solid #e2e8f0; padding: 4px 6px; text-align: left; font-size: 12px; }}
    .aqms-popup-values th {{ color: #475569; font-weight: 900; }}
    .aqms-popup-values td:nth-child(2) {{ font-weight: 900; color: #0f172a; }}
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
    const aqmsGroup = L.featureGroup().addTo(group);
    const sensorGroup = L.markerClusterGroup({{
      iconCreateFunction: function(cluster) {{
        return L.divIcon({{
          html: `<div class="purpleair-cluster">${{cluster.getChildCount()}}</div>`,
          className: "",
          iconSize: [38, 38],
          iconAnchor: [19, 19]
        }});
      }},
      showCoverageOnHover: false,
      maxClusterRadius: 60
    }}).addTo(group);
    const purpleAirMarkers = new Map();

    function purpleAirCategory(value) {{
      if (value == null || Number.isNaN(Number(value))) {{
        return {{ label: "PurpleAir sensor", colour: "{PURPLEAIR_COLOR}" }};
      }}
      const pm25 = Number(value);
      if (pm25 < 25) return {{ label: "Good", colour: "#16a34a" }};
      if (pm25 < 50) return {{ label: "Fair", colour: "#facc15" }};
      if (pm25 < 100) return {{ label: "Poor", colour: "#f97316" }};
      if (pm25 < 300) return {{ label: "Very poor", colour: "#ef4444" }};
      return {{ label: "Extremely poor", colour: "#7f1d1d" }};
    }}

    function purpleAirSize(value) {{
      if (value == null || Number.isNaN(Number(value))) return 22;
      const pm25 = Math.max(Number(value), 0);
      return Math.max(22, Math.min(44, 22 + Math.sqrt(pm25) * 2.3));
    }}

    function purpleAirIcon(item) {{
      const size = purpleAirSize(item.pm25);
      const category = purpleAirCategory(item.pm25);
      const label = item.pm25 == null ? "" : formatPaValue(item.pm25);
      return L.divIcon({{
        className: "",
        html: `<div class="purpleair-marker" style="width:${{size}}px;height:${{size}}px;background:${{category.colour}};font-size:${{size > 30 ? 10 : 9}}px">${{label}}</div>`,
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
        PM2.5: <strong>${{item.pm25 == null ? "--" : formatPaValue(item.pm25)}}</strong><br>
        Category: <strong>${{item.paCategory || "PurpleAir sensor"}}</strong><br>
        Lat/Lon: ${{item.lat.toFixed(5)}}, ${{item.lon.toFixed(5)}}<br>
        <div id="purpleair-${{item.siteId}}" style="margin-top:8px;">${{loadingText || ""}}</div>
      `;
    }}

    function escapeHtml(value) {{
      return String(value == null ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }}

    function aqmsValuesHtml(item) {{
      if (!Array.isArray(item.values) || item.values.length === 0) {{
        return "";
      }}
      const rows = item.values.map((entry) => {{
        const units = entry.units ? ` ${{escapeHtml(entry.units)}}` : "";
        const time = entry.time ? `<div style="color:#64748b;font-size:11px;">${{escapeHtml(entry.time)}} ${{escapeHtml(entry.date || "")}}</div>` : "";
        return `
          <tr>
            <td>${{escapeHtml(entry.pollutant || "")}}</td>
            <td>${{escapeHtml(entry.value || "--")}}${{units}}</td>
            <td>${{escapeHtml(entry.category || "No data")}}${{time}}</td>
          </tr>
        `;
      }}).join("");
      return `
        <table class="aqms-popup-values">
          <thead><tr><th>Pollutant</th><th>Value</th><th>Category / time</th></tr></thead>
          <tbody>${{rows}}</tbody>
        </table>
      `;
    }}

    markers.forEach((item) => {{
      if (item.lat == null || item.lon == null) return;
      const icon = L.divIcon({{
        className: "",
        html: item.source === "PurpleAir"
          ? `<div class="purpleair-marker"></div>`
          : `<div class="station-marker" style="background:${{item.colour}}">${{item.valueLabel}}</div>`,
        iconSize: item.source === "PurpleAir" ? [22, 22] : [34, 34],
        iconAnchor: item.source === "PurpleAir" ? [11, 11] : [17, 17],
        popupAnchor: [0, -18]
      }});
      const marker = L.marker([item.lat, item.lon], {{ icon: item.source === "PurpleAir" ? purpleAirIcon(item) : icon }});
      if (item.source === "PurpleAir") {{
        marker.addTo(sensorGroup);
        purpleAirMarkers.set(String(item.siteId), {{ item, marker }});
      }} else {{
        marker.addTo(aqmsGroup);
      }}
      marker.bindTooltip(item.station, {{ direction: "top", className: "station-label" }});
      const purpleAirPanelId = `purpleair-${{item.siteId}}`;
      marker.bindPopup(item.source === "PurpleAir" ? purpleAirPopupHtml(item, "Click opened. Loading PurpleAir values...") : `
        <strong>${{escapeHtml(item.station)}}</strong><br>
        Source: <strong>${{escapeHtml(item.source || "AQMS")}}</strong><br>
        Category: <strong>${{escapeHtml(item.category || "No data")}}</strong><br>
        Latest time: ${{escapeHtml(item.hour || "--")}}<br>
        Latest date: ${{escapeHtml(item.date || "--")}}<br>
        Region: ${{escapeHtml(item.region || "--")}}
        ${{aqmsValuesHtml(item)}}
      `);
      marker.on("click", () => {{
        if (!item.siteId) return;
        const key = item.source === "PurpleAir" ? `purpleair:${{item.siteId}}` : `aqms:${{item.siteId}}`;
        try {{
          window.parent.postMessage({{ type: "monitor-site-select", monitor: key }}, "*");
        }} catch (err) {{}}
      }});
      if (item.source === "PurpleAir") {{
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
      }}
      const category = purpleAirCategory(item.pm25);
      item.paCategory = category.label;
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

    // PurpleAir rows are injected from the server/cache. Avoid a browser-side
    // API refresh here because public clients may not have a valid API quota.

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
        <div class="legend-row"><span class="legend-swatch" style="background:{PURPLEAIR_COLOR}; border-radius:50%"></span>PurpleAir sensor / cluster</div>
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
    region_name = load_region_lookup().get(str(region_code).strip()) or ""
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

    pollutant_tiles = []
    target_idx = 0
    try:
        target_idx = int(hour_index or 0)
    except (TypeError, ValueError):
        target_idx = 0
    if target_idx < 0:
        target_idx = 0

    tile_colors = {
        "PM2.5": "orange",
        "PM10": "purple",
        "O3": "blue",
    }
    for pollutant_name in ("PM2.5", "PM10", "O3"):
        mean_series = _forecast_region_mean_series(
            str(region_code or ""),
            pollutant_name,
            str(time_scope or ""),
            str(selection.get("models") or ""),
            str(selection.get("date") or ""),
        )
        value = mean_series[target_idx] if target_idx < len(mean_series) else None
        category_key, _category_color = category_for_value(pollutant_name, value)
        category_label = category_key.replace("-", " ").title()
        units_local = (POLLUTANTS.get(pollutant_name) or {}).get("Units") or ""
        value_text = "--" if value is None else f"{value:.2f}"
        pollutant_tiles.append(
            _metric_tile(
                "pollutant",
                f"{pollutant_name} (region)",
                value_text,
                category_label,
                color=tile_colors.get(pollutant_name, "blue"),
                units=units_local,
            )
        )

    return [
        _metric_tile("region", "Selected region", str(region_code), region_subtitle, color="blue"),
        *pollutant_tiles,
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

    def _category_colour(category):
        category_name = str(category.get("name") or "").strip().lower()
        for pollutant in ("PM2.5", "PM10", "O3"):
            for item in (POLLUTANTS.get(pollutant) or {}).get("categories") or []:
                if str(item.get("label") or "").strip().lower() == category_name:
                    return item.get("color") or "#64748b"
        return "#64748b"

    def _clean_actions(actions, fallback):
        cleaned = [str(item or "").strip() for item in actions or [] if str(item or "").strip()]
        return cleaned or [fallback]

    category_cards = []
    action_cards = []
    for category in GENERAL_HEALTH_GUIDE:
        category_name = str(category.get("name") or "").strip()
        category_key = str(category.get("class") or "").strip()
        colour = _category_colour(category)
        sensitive_actions = _clean_actions(category.get("sensitiveGroups"), "Continue normal activities and monitor local air quality.")
        everyone_actions = _clean_actions(category.get("everyoneElse"), "Continue normal activities.")
        category_cards.append(
            html.Div(
                [
                    html.Div(category_name, className="aq-standard-category-card__name"),
                    html.Div("Air quality category", className="aq-standard-category-card__label"),
                ],
                className=f"aq-standard-category-card aq-standard-category-card--{category_key}",
                style={"--aq-standard-color": colour},
            )
        )
        action_cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(className="aq-standard-action-card__stripe", style={"backgroundColor": colour}),
                            html.Div(
                                [
                                    html.Div(category_name, className="aq-standard-action-card__title"),
                                    html.Div("Recommended action", className="aq-standard-action-card__subtitle"),
                                ]
                            ),
                        ],
                        className="aq-standard-action-card__header",
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Div("Sensitive groups", className="aq-standard-action-card__group-title"),
                                    html.Div("Heart/lung conditions, asthma, older adults, children, infants and pregnant women.", className="aq-standard-action-card__group-note"),
                                    html.Ul([html.Li(item) for item in sensitive_actions]),
                                ],
                                className="aq-standard-action-card__group aq-standard-action-card__group--sensitive",
                            ),
                            html.Div(
                                [
                                    html.Div("Everyone else", className="aq-standard-action-card__group-title"),
                                    html.Div("General community guidance for outdoor activity and symptoms.", className="aq-standard-action-card__group-note"),
                                    html.Ul([html.Li(item) for item in everyone_actions]),
                                ],
                                className="aq-standard-action-card__group",
                            ),
                        ],
                        className="aq-standard-action-card__body",
                    ),
                ],
                className="aq-standard-action-card",
            )
        )

    threshold_cards = []
    for pollutant_label in ("PM2.5", "PM10", "O3"):
        pollutant = POLLUTANTS.get(pollutant_label) or {}
        categories = [
            item
            for item in pollutant.get("categories") or []
            if str(item.get("label") or "").strip().lower() != "no data"
        ]
        threshold_cards.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(pollutant_display(pollutant_label), className="aq-standard-threshold-card__pollutant"),
                            html.Div(pollutant.get("Units") or "", className="aq-standard-threshold-card__unit"),
                        ],
                        className="aq-standard-threshold-card__header",
                    ),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.Span(item.get("label") or "", className="aq-standard-threshold-card__label"),
                                    html.Strong(item.get("range") or "--", className="aq-standard-threshold-card__range"),
                                ],
                                className="aq-standard-threshold-card__row",
                                style={"--aq-standard-color": item.get("color") or "#64748b"},
                            )
                            for item in categories
                        ],
                        className="aq-standard-threshold-card__scale",
                    ),
                ],
                className="aq-standard-threshold-card",
            )
        )

    return html.Section(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("NSW air quality guidance", className="aq-standard-hero__eyebrow"),
                            html.H2("AQ Standard"),
                            html.P("A practical reference for reading category colours, pollutant thresholds and recommended health actions."),
                        ],
                        className="aq-standard-hero__copy",
                    ),
                    html.Div(
                        [
                            html.Div([html.Span("5"), html.Strong("Categories")], className="aq-standard-hero__stat"),
                            html.Div([html.Span("3"), html.Strong("Key pollutants")], className="aq-standard-hero__stat"),
                            html.Div([html.Span("000"), html.Strong("Emergency")], className="aq-standard-hero__stat aq-standard-hero__stat--urgent"),
                        ],
                        className="aq-standard-hero__stats",
                    ),
                ],
                className="aq-standard-hero",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.H3("Category ladder"),
                            html.P("Colours move from normal conditions to severe pollution episodes."),
                        ],
                        className="aq-standard-section-heading",
                    ),
                    html.Div(category_cards, className="aq-standard-category-grid"),
                ],
                className="aq-standard-card aq-standard-card--ladder",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.H3("Pollutant breakpoints"),
                            html.P("Thresholds used by the dashboard for PM2.5, PM10 and ozone categories."),
                        ],
                        className="aq-standard-section-heading",
                    ),
                    html.Div(threshold_cards, className="aq-standard-threshold-grid"),
                ],
                className="aq-standard-card",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            html.H3("Recommended actions"),
                            html.P("Guidance is separated for sensitive groups and the broader community."),
                        ],
                        className="aq-standard-section-heading",
                    ),
                    html.Div(action_cards, className="aq-standard-action-grid"),
                    html.Div(
                        "If symptoms are concerning, contact HealthDirect on 1800 022 222 or seek medical advice. In an emergency call triple zero (000).",
                        className="aq-standard-emergency-note",
                    ),
                ],
                className="aq-standard-card",
            ),
            html.Div(
                [
                    html.Strong("Source: "),
                    html.Span("NSW air quality health activity guidance mirrored from the dashboard reference data."),
                ],
                className="aq-standard-source-note",
            ),
        ],
        className="activity-guide-panel aq-standard-panel",
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
    parsed["_region"] = selection.get("regions")
    try:
        parsed["_generatedAtEpoch"] = os.path.getmtime(file_path)
    except OSError:
        parsed["_generatedAtEpoch"] = None
    if exact_match:
        return parsed, f"Loaded {Path(file_path).name}"
    return parsed, f"Closest match: {Path(file_path).name}"


def _monitor_store_payload(
    monitor_settings=None,
    current_store=None,
    raw_obs=None,
    purpleair_payload=None,
    source_filter=None,
    pollutant_label=None,
    include_history=False,
    status_prefix=None,
):
    monitor_settings = monitor_settings or {}
    pollutant_label = (pollutant_label or monitor_settings.get("pollutant") or "PM2.5").strip()
    source_filter = (source_filter or monitor_settings.get("source") or "both").strip().lower()
    pollutant_meta = POLLUTANTS.get(pollutant_label) or {}
    parameter_code = pollutant_meta.get("ParameterCode") or pollutant_label

    status_bits = [status_prefix] if status_prefix else []
    error = None
    aqms_ok = True
    if raw_obs is None:
        try:
            raw_obs = fetch_observations(query=None, timeout=25)
            status_bits.append("AQMS feed: Live")
        except Exception as exc:
            raw_obs = []
            aqms_ok = False
            error = f"AQMS feed unavailable: {exc}"
            status_bits.append("AQMS feed: Offline")
    else:
        status_bits.append("AQMS feed: Memory" if raw_obs else "AQMS feed: No cached data")
    if not isinstance(raw_obs, list):
        raw_obs = []

    if purpleair_payload is None:
        purpleair_payload = fetch_purpleair_snapshot(bounds=NSW_MAP_BOUNDS, timeout=12)
        if purpleair_payload.get("error"):
            status_bits.append("PurpleAir feed: Offline")
            if error is None:
                error = f"PurpleAir feed unavailable: {purpleair_payload.get('error')}"
        elif purpleair_payload.get("source") in {"bundle", "cache"}:
            status_bits.append("PurpleAir feed: Cached")
        else:
            status_bits.append("PurpleAir feed: Live")
    else:
        status_bits.append("PurpleAir feed: Memory" if purpleair_payload.get("sensors") else "PurpleAir feed: No cached data")

    aqms_rows = _aqms_rows_for_pollutant(raw_obs, parameter_code, pollutant_label)
    aqms_pm25_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["PM2.5"]["ParameterCode"], "PM2.5")
    aqms_pm10_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["PM10"]["ParameterCode"], "PM10")
    aqms_o3_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["O3"]["ParameterCode"], "O3")
    aqms_no2_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["NO2"]["ParameterCode"], "NO2")
    aqms_so2_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["SO2"]["ParameterCode"], "SO2")
    aqms_co_rows = _aqms_rows_for_pollutant(raw_obs, POLLUTANTS["CO"]["ParameterCode"], "CO")
    aqms_no_rows = _aqms_rows_for_pollutant(raw_obs, "NO", "NO")
    aqms_nox_rows = _aqms_rows_for_pollutant(raw_obs, "NOX", "NOX")
    aqc_rows = _aqms_rows_for_pollutant(raw_obs, "AQC", "AQC")

    aqms_history_rows = []
    if include_history and raw_obs:
        try:
            history_site_ids = sorted({int(item.get("Site_Id")) for item in raw_obs if isinstance(item, dict) and item.get("Site_Id") is not None})
        except Exception:
            history_site_ids = []
        history_parameter_codes = []
        for pollutant_name in ["PM2.5", "PM10", "O3", "CO", "NO", "NO2", "NOX"]:
            code = (POLLUTANTS.get(pollutant_name) or {}).get("ParameterCode") or pollutant_name
            if code and code not in history_parameter_codes:
                history_parameter_codes.append(code)
        if history_site_ids and history_parameter_codes:
            now = datetime.now(SYDNEY_TZ)
            try:
                aqms_history_rows = fetch_observation_history(
                    history_site_ids,
                    history_parameter_codes,
                    (now.date() - timedelta(days=1)).isoformat(),
                    now.date().isoformat(),
                    timeout=12,
                )
            except Exception:
                aqms_history_rows = []
    aqms_previous_by_site = _aqms_previous_hour_values_by_site(raw_obs, aqms_history_rows)

    met_site_ids_by_region = {}
    try:
        wanted_met = {"TEMP", "HUMID", "WSP", "RAIN"}
        for item in raw_obs or []:
            if not isinstance(item, dict):
                continue
            param = _aqms_parameter(item)
            code = str(param.get("ParameterCode") or "").strip()
            if code not in wanted_met or item.get("Value") is None or item.get("Site_Id") is None:
                continue
            site_id = int(item.get("Site_Id"))
            site = _sites_by_id().get(site_id, {})
            region = str(site.get("Region") or "").strip() or "Unknown"
            met_site_ids_by_region.setdefault(region, set()).add(site_id)
            met_site_ids_by_region.setdefault("All NSW", set()).add(site_id)
    except Exception:
        met_site_ids_by_region = {}

    purpleair_sensors = _purpleair_rows_for_pollutant(purpleair_payload.get("sensors") or [], pollutant_label)
    clusters = purpleair_clusters(purpleair_payload.get("sensors") or [], pollutant_label=pollutant_label) if purpleair_sensors else []

    aqms_snapshot_by_site = {}
    for key, rows in {
        "PM2.5": aqms_pm25_rows,
        "PM10": aqms_pm10_rows,
        "O3": aqms_o3_rows,
        "NO": aqms_no_rows,
        "NO2": aqms_no2_rows,
        "NOX": aqms_nox_rows,
        "SO2": aqms_so2_rows,
        "CO": aqms_co_rows,
        "AQC": aqc_rows,
    }.items():
        for row in rows or []:
            site_id = row.get("site_id")
            if site_id is None:
                continue
            aqms_snapshot_by_site.setdefault(int(site_id), {})[key] = row

    aqms_map_rows = _aqms_station_map_rows(
        {
            "PM2.5": aqms_pm25_rows,
            "PM10": aqms_pm10_rows,
            "O3": aqms_o3_rows,
            "NO2": aqms_no2_rows,
            "NO": aqms_no_rows,
            "NOX": aqms_nox_rows,
            "SO2": aqms_so2_rows,
            "CO": aqms_co_rows,
            "AQC": aqc_rows,
        },
        selected_pollutant=pollutant_label,
    )
    map_rows = []
    if source_filter in {"both", "aqms"}:
        map_rows.extend(aqms_map_rows)
    if source_filter in {"both", "purpleair"}:
        map_rows.extend(purpleair_sensors)

    latest_label = _format_monitor_label(_monitoring_time_row(aqms_rows)) if aqms_rows else "--"
    completeness = _data_completeness(aqms_rows, purpleair_sensors if source_filter in {"both", "purpleair"} else [])
    kpis = _monitor_kpi_payload(aqms_rows, purpleair_sensors, clusters, latest_label, completeness)
    table_rows = _monitor_table_rows(aqms_rows, purpleair_sensors, aqms_snapshot_by_site, aqms_previous_by_site, max_rows=40)
    table_rows_all = _monitor_table_rows_all(aqms_rows, purpleair_sensors, aqms_snapshot_by_site, aqms_previous_by_site)
    fetched_epoch = datetime.now(SYDNEY_TZ).timestamp()
    status_text = ". ".join([bit for bit in status_bits if bit])
    if latest_label and latest_label != "--":
        status_text = f"{status_text}. Latest snapshot: {latest_label}"

    return {
        "pollutant": pollutant_label,
        "parameterCode": parameter_code,
        "aqmsRows": aqms_rows,
        "aqmsPm25Rows": aqms_pm25_rows,
        "aqmsPm10Rows": aqms_pm10_rows,
        "aqmsO3Rows": aqms_o3_rows,
        "aqmsNo2Rows": aqms_no2_rows,
        "aqmsNoRows": aqms_no_rows,
        "aqmsNoxRows": aqms_nox_rows,
        "aqmsSo2Rows": aqms_so2_rows,
        "aqmsCoRows": aqms_co_rows,
        "aqcRows": aqc_rows,
        "metSiteIdsByRegion": {k: sorted(list(v)) for k, v in (met_site_ids_by_region or {}).items()},
        "aqmsSnapshotBySite": aqms_snapshot_by_site,
        "aqmsPreviousBySite": aqms_previous_by_site,
        "purpleairSnapshot": purpleair_payload,
        "purpleairSensors": purpleair_sensors,
        "purpleairClusters": clusters,
        "mapRows": map_rows,
        "kpis": kpis,
        "tableRows": table_rows,
        "tableRowsAll": table_rows_all,
        "feeds": {
            "aqms": {"status": "Live" if aqms_ok else "Offline"},
            "purpleair": {"status": "Live" if not purpleair_payload.get("error") else "Offline"},
            "completeness": completeness,
        },
        "latestLabel": latest_label,
        "fetchedAtEpoch": fetched_epoch,
        "status": status_text or INITIAL_MONITOR_STATUS,
        "error": error,
    }


"""
Startup performance note
------------------------
This module used to eagerly live-fetch monitoring data at import time.
On remote hosts that delayed binding the Dash server port and made it feel like the app "hangs" before you can open it.

We now start with disk/memory cache data and let callbacks refresh live feeds once a browser session connects.
"""

INITIAL_MONITOR_ROWS = []
INITIAL_MONITOR_STATUS = "Monitoring feed is loading."
try:
    INITIAL_MONITOR_STORE = _monitor_store_payload(
        raw_obs=_load_observations_cache(),
        purpleair_payload=_load_purpleair_snapshot_cache(),
        pollutant_label="PM2.5",
        source_filter="both",
        include_history=False,
        status_prefix="Monitoring feed: Memory cache",
    )
    try:
        live_startup_store = _monitor_store_payload(
            raw_obs=fetch_observations(query=None, timeout=10),
            purpleair_payload=_load_purpleair_snapshot_cache(),
            pollutant_label="PM2.5",
            source_filter="both",
            include_history=False,
            status_prefix="Monitoring feed: Live startup",
        )
        if live_startup_store.get("aqmsRows") and not _monitor_data_is_stale(live_startup_store):
            live_startup_store["status"] = str(live_startup_store.get("status") or "").replace("AQMS feed: Memory", "AQMS feed: Live")
            INITIAL_MONITOR_STORE = live_startup_store
    except Exception:
        pass
    INITIAL_MONITOR_ROWS = INITIAL_MONITOR_STORE.get("mapRows") or []
    _LAST_GOOD_MONITOR_MAP_ROWS = list(INITIAL_MONITOR_ROWS)
    INITIAL_MONITOR_STATUS = INITIAL_MONITOR_STORE.get("status") or INITIAL_MONITOR_STATUS
except Exception as exc:
    INITIAL_MONITOR_STORE = {
        "pollutant": "PM2.5",
        "parameterCode": POLLUTANTS.get("PM2.5", {}).get("ParameterCode", "PM2.5"),
        "aqmsRows": [],
        "aqmsPm25Rows": [],
        "aqmsPm10Rows": [],
        "aqmsO3Rows": [],
        "aqmsNo2Rows": [],
        "aqmsSo2Rows": [],
        "aqmsCoRows": [],
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
        "status": f"{INITIAL_MONITOR_STATUS} Cache load failed: {exc}",
        "error": str(exc),
    }

INITIAL_MONITOR_STATION_LIST = _monitor_station_list(INITIAL_MONITOR_ROWS)
INITIAL_MONITOR_SUMMARY_CARDS = _monitoring_summary_cards(INITIAL_MONITOR_ROWS)
INITIAL_MONITOR_TIME_LABEL = _format_monitor_label(_monitoring_time_row(INITIAL_MONITOR_ROWS)) if INITIAL_MONITOR_ROWS else "--"
INITIAL_MONITOR_UPDATED_LABEL = _monitor_header_timestamp_label(INITIAL_MONITOR_STORE)
INITIAL_MONITOR_OVERVIEW_UPDATED_LABEL = _monitor_updated_label(INITIAL_MONITOR_STORE)

if INITIAL_FORECAST_PARSED:
    INITIAL_FORECAST_TIME_INDEX = 0
    INITIAL_FORECAST_TIMES = INITIAL_FORECAST_PARSED.get("data", {}).get("time", {}).get("forecastTime", [])
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
    INITIAL_FORECAST_RANKING_FIGURE = _ranking_bar_figure(INITIAL_FORECAST_PARSED, DEFAULT_SELECTION["pollutants"])
    INITIAL_FORECAST_RANKING_HOUR_OPTIONS = _forecast_hour_options(INITIAL_FORECAST_PARSED)
    INITIAL_RANKING_STATION_OPTIONS = []
    INITIAL_RANKING_SELECTED_STATION = _default_ranking_station(DEFAULT_SELECTION, INITIAL_FORECAST_TIME_INDEX)
    INITIAL_FORECAST_RANKING_CARDS = _station_prediction_cards(DEFAULT_SELECTION, INITIAL_FORECAST_TIME_INDEX)
    if INITIAL_RANKING_SELECTED_STATION:
        INITIAL_STATION_TREND = _station_trend_figure(INITIAL_FORECAST_PARSED, DEFAULT_SELECTION["pollutants"], INITIAL_RANKING_SELECTED_STATION)
    else:
        INITIAL_STATION_TREND = _all_stations_trend_figure(INITIAL_FORECAST_PARSED, DEFAULT_SELECTION["pollutants"])
    INITIAL_TIME_SERIES_REGION_OPTIONS = _forecast_region_options(INITIAL_FORECAST_PARSED, DEFAULT_SELECTION["regions"])
    INITIAL_TIME_SERIES_REGION = DEFAULT_SELECTION["regions"]
    INITIAL_TIME_SERIES_STATION_OPTIONS = _forecast_station_options(INITIAL_FORECAST_PARSED, INITIAL_TIME_SERIES_REGION)
    INITIAL_TIME_SERIES_STATION = _default_forecast_station(INITIAL_FORECAST_PARSED, INITIAL_TIME_SERIES_REGION)
    INITIAL_TIME_SERIES = _forecast_time_series_figure(
        INITIAL_FORECAST_PARSED,
        DEFAULT_SELECTION["pollutants"],
        INITIAL_TIME_SERIES_STATION,
        0,
        INITIAL_TIME_SERIES_REGION,
    )
    INITIAL_STATION_DETAILS = _selected_station_details(
        INITIAL_FORECAST_PARSED,
        DEFAULT_SELECTION["pollutants"],
        INITIAL_RANKING_SELECTED_STATION,
        INITIAL_FORECAST_TIME_INDEX,
    )
    INITIAL_SELECTED_STATION = INITIAL_RANKING_SELECTED_STATION
else:
    INITIAL_FORECAST_TIME_INDEX = 0
    INITIAL_FORECAST_TIMES = []
    INITIAL_FORECAST_SUMMARY_CARDS = _make_summary_cards({}, DEFAULT_SELECTION, 0)
    INITIAL_FORECAST_TIME_LABEL = "Latest forecast run not loaded."
    INITIAL_FORECAST_FILE_INFO = ""
    INITIAL_FORECAST_MAP = go.Figure()
    INITIAL_FORECAST_MAP_HTML = _leaflet_forecast_map_html({}, DEFAULT_SELECTION.get("pollutants"), 0)
    INITIAL_FORECAST_RANKING = []
    INITIAL_FORECAST_RANKING_FIGURE = _ranking_bar_figure({}, DEFAULT_SELECTION.get("pollutants"))
    INITIAL_FORECAST_RANKING_HOUR_OPTIONS = _forecast_hour_options({})
    INITIAL_RANKING_STATION_OPTIONS = []
    INITIAL_RANKING_SELECTED_STATION = None
    INITIAL_FORECAST_RANKING_CARDS = _station_prediction_cards(DEFAULT_SELECTION, 0)
    INITIAL_STATION_TREND = go.Figure()
    INITIAL_TIME_SERIES_REGION_OPTIONS = []
    INITIAL_TIME_SERIES_REGION = DEFAULT_SELECTION.get("regions")
    INITIAL_TIME_SERIES_STATION_OPTIONS = []
    INITIAL_TIME_SERIES_STATION = None
    INITIAL_TIME_SERIES = go.Figure()
    INITIAL_STATION_DETAILS = "Choose a station from the list to inspect its forecast profile."
    INITIAL_SELECTED_STATION = None

try:
    _initial_rank_parsed, _initial_rank_message = _load_forecast(DEFAULT_SELECTION)
    INITIAL_FORECAST_RANK_TIME_LABEL = (
        _format_forecast_label(_selected_time_label(_initial_rank_parsed, INITIAL_FORECAST_TIME_INDEX))
        if _initial_rank_parsed
        else INITIAL_FORECAST_TIME_LABEL
    )
except Exception:
    INITIAL_FORECAST_RANK_TIME_LABEL = INITIAL_FORECAST_TIME_LABEL


app = dash.Dash(
    __name__,
    assets_folder=str(CURRENT_DIR / "assets"),
    title="NSW Air Quality Forecast Dashboard",
    suppress_callback_exceptions=True,
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


@server.route("/_dash-component-suites/plotly_cloud/<path:_asset_path>")
def optional_plotly_cloud_asset(_asset_path):
    return Response("", mimetype="text/css")


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
    """Render top boxes for AQMS (PM2.5/PM10/O3) and PurpleAir (selected pollutant)."""
    monitor_store = monitor_store or {}
    current_label = datetime.now(SYDNEY_TZ).strftime("%Y-%m-%d %H:%M")
    selected_pollutant = pollutant_label or "PM2.5"
    # Map pollutant to store keys for AQMS
    key_map = {"PM2.5": "aqmsPm25Rows", "PM10": "aqmsPm10Rows", "O3": "aqmsO3Rows"}
    purple_snapshot = monitor_store.get("purpleairSnapshot") or {}
    purple_rows_pm25 = _purpleair_rows_for_pollutant((purple_snapshot or {}).get("sensors") or [], "PM2.5")
    purple_rows_pm10 = _purpleair_rows_for_pollutant((purple_snapshot or {}).get("sensors") or [], "PM10")
    card_backgrounds = ["#f8fafc", "#eff6ff", "#f0fdf4", "#fff7ed", "#fdf2f8"]
    purple_backgrounds_pm25 = ["#f7f3ff", "#f3edff", "#efe7ff", "#e9e0ff", "#e4d8ff"]
    purple_backgrounds_pm10 = ["#efe1ff", "#e7d5ff", "#ddc7ff", "#d2baff", "#c9aefc"]

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

    def box_for_row(r, pollutant, src_label, card_index=0, background_colour=None):
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
        station_key = str(station)
        # IDs must be unique across all rendered boxes; include pollutant + source.
        box_unique = r.get("site_id") or r.get("monitorKey") or card_index
        box_index = f"{src_label}|{pollutant}|{station_key}|{box_unique}"
        if background_colour is None:
            background_colour = card_backgrounds[card_index % len(card_backgrounds)]
        return html.Button(
            [
                html.Div(station, className="monitor-top-box__station"),
                html.Div(category_label, className="monitor-top-box__category", style={"backgroundColor": colour, "color": "#fff"}),
                html.Div(value_label, className="monitor-top-box__value"),
            ],
            id={"type": "monitor-top-box", "index": box_index},
            n_clicks=0,
            className="monitor-top-box",
            style={
                "padding": "10px",
                "borderRadius": "8px",
                "backgroundColor": background_colour,
                "boxShadow": "0 1px 4px rgba(2,6,23,0.08)",
                "textAlign": "center",
                "border": "1px solid rgba(148, 163, 184, 0.18)",
                "cursor": "pointer",
            },
        )

    def panel_title(label):
        return html.Div(
            [
                html.Span(label),
                html.Span(f" @ {current_label}", style={"color": "#94a3b8", "fontWeight": "500", "fontSize": "0.92rem"}),
            ],
            className="monitor-top-panel__title",
        )

    panels = []

    # AQMS: always show PM2.5 + PM10 + O3 rows so the boxes are visible without switching filters.
    if source in ("both", "aqms"):
        sections = []
        for pollutant in ["PM2.5", "PM10", "O3"]:
            aqms_rows = monitor_store.get(key_map.get(pollutant, "aqmsPm25Rows")) or []
            aqms_top = top_n(aqms_rows, 5)
            if not aqms_top:
                continue
            sections.append(
                html.Div(
                    [
                        html.Div(pollutant, className="monitor-top-section__title"),
                        html.Div([box_for_row(r, pollutant, "AQMS", card_index=i) for i, r in enumerate(aqms_top)], className="monitor-top-box-grid"),
                    ],
                    className="monitor-top-section",
                )
            )
        if sections:
            panels.append(
                html.Div(
                    [
                        panel_title("Top AQMS Stations by Pollutant"),
                        html.Div(sections, className="monitor-top-panel__body"),
                    ],
                    className="monitor-top-panel",
                )
            )

    if source in ("both", "purpleair"):
        purple_sections = []
        for pollutant, rows, palette in [
            ("PM2.5", purple_rows_pm25, purple_backgrounds_pm25),
            ("PM10", purple_rows_pm10, purple_backgrounds_pm10),
        ]:
            pa_top = top_n(rows, 5)
            if not pa_top:
                continue
            purple_sections.append(
                html.Div(
                    [
                        html.Div(pollutant, className="monitor-top-section__title"),
                        html.Div(
                            [box_for_row(r, pollutant, "PurpleAir", card_index=i, background_colour=palette[i % len(palette)]) for i, r in enumerate(pa_top)],
                            className="monitor-top-box-grid",
                        ),
                    ],
                    className="monitor-top-section",
                )
            )
        if purple_sections:
            panels.append(
                html.Div(
                    [
                        panel_title("Top PurpleAir Stations by Pollutant"),
                        html.Div(purple_sections, className="monitor-top-panel__body"),
                    ],
                    className="monitor-top-panel",
                )
            )

    if not panels:
        return html.Div("No top observations available", className="card-hint")
    return html.Div(panels, className="monitor-top-panels")


@app.callback(Output("monitor-station", "value"), [Input({"type": "monitor-top-box", "index": MATCH}, "n_clicks")], [State({"type": "monitor-top-box", "index": MATCH}, "id")], prevent_initial_call=True)
def monitor_top_box_clicked(n_clicks, box_id):
    if not n_clicks:
        return no_update
    # box_id is a dict {'type':'monitor-top-box','index': station_key}
    index = (box_id or {}).get("index") or ""
    # index format: "<source>|<pollutant>|<station>"
    index_parts = str(index).split("|")
    station_key = index_parts[2] if len(index_parts) >= 3 else (str(index) if index else None)
    return station_key


def _initial_monitor_table_seed(rows, region_value=None, compact=False):
    region_value = str(region_value or MONITOR_DEFAULT_REGION).strip()
    seeded_rows = _attach_arrows_to_table_rows(
        rows or [],
        INITIAL_MONITOR_STORE.get("aqmsSnapshotBySite"),
        INITIAL_MONITOR_STORE.get("aqmsPreviousBySite"),
    )
    seeded_rows = _inject_badges_into_monitor_rows(seeded_rows)
    if region_value != "ALL":
        seeded_rows = [
            row
            for row in seeded_rows
            if str((row or {}).get("region") or "").strip() == region_value
            or (row or {}).get("source") == "PurpleAir"
        ]
    return _ordered_monitor_outlook_rows(seeded_rows, source_value="both", compact=compact, max_rows=12 if compact else None)


INITIAL_MONITOR_LATEST_LABEL = INITIAL_MONITOR_STORE.get("latestLabel") or "--"
INITIAL_MONITOR_KPI_NODES = _monitor_kpi_cards(INITIAL_MONITOR_STORE.get("kpis") or {})
INITIAL_MONITOR_TABLE_ROWS = _initial_monitor_table_seed(INITIAL_MONITOR_STORE.get("tableRowsAll") or [], compact=True)
INITIAL_MONITOR_TABLE_ALL_ROWS = _initial_monitor_table_seed(INITIAL_MONITOR_STORE.get("tableRowsAll") or [])
INITIAL_MONITOR_TABLE_STYLES = _monitor_table_styles()
INITIAL_MONITOR_NETWORK_NODES = _monitor_network_nodes(INITIAL_MONITOR_STORE.get("feeds") or {})
INITIAL_MONITOR_STATUS_TEXT = INITIAL_MONITOR_STORE.get("error") or INITIAL_MONITOR_STORE.get("status") or INITIAL_MONITOR_STATUS
INITIAL_MONITOR_TOP_BOXES = render_monitor_top_boxes(INITIAL_MONITOR_STORE, "PM2.5", "both")


def _overview_nsw_region_options(monitor_store):
    monitor_store = monitor_store or {}
    pm25_rows_all = monitor_store.get("aqmsPm25Rows") or []
    pm10_rows_all = monitor_store.get("aqmsPm10Rows") or []
    o3_rows_all = monitor_store.get("aqmsO3Rows") or []
    combined_rows = list(pm25_rows_all) + list(pm10_rows_all) + list(o3_rows_all)
    latest = _monitoring_time_row(combined_rows)
    if not latest:
        return [{"name": "NSW", "stations": []}]

    date_val = latest.get("date")
    hour_val = latest.get("hour")
    latest_combined = [row for row in combined_rows if row.get("date") == date_val and row.get("hour") == hour_val]
    latest_pm25 = [row for row in pm25_rows_all if row.get("date") == date_val and row.get("hour") == hour_val]
    regions = sorted({str(row.get("region") or "").strip() for row in latest_pm25 if str(row.get("region") or "").strip()})

    def station_names(region_name=None):
        rows = latest_combined
        if region_name:
            rows = [row for row in rows if str(row.get("region") or "").strip() == region_name]
        names = []
        seen = set()
        region_cmp = str(region_name or "").strip().lower()
        for row in rows:
            name = str(row.get("station") or "").strip()
            if not name:
                continue
            key = name.lower()
            if region_cmp and key == region_cmp:
                continue
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        return sorted(names)

    options = [{"name": "NSW", "stations": station_names(None)}]
    options.extend({"name": region, "stations": station_names(region)} for region in regions)
    return options


def _overview_nsw_summary_data(monitor_store):
    monitor_store = monitor_store or {}
    pm25_rows_all = monitor_store.get("aqmsPm25Rows") or []
    pm10_rows_all = monitor_store.get("aqmsPm10Rows") or []
    o3_rows_all = monitor_store.get("aqmsO3Rows") or []
    combined_rows = list(pm25_rows_all) + list(pm10_rows_all) + list(o3_rows_all)
    options = _overview_nsw_region_options(monitor_store)
    latest = _monitoring_time_row(combined_rows)
    if not latest:
        return {"options": options, "regions": []}

    date_val = latest.get("date")
    hour_val = latest.get("hour")

    def _latest_rows(rows):
        return [row for row in rows or [] if row.get("date") == date_val and row.get("hour") == hour_val]

    latest_by_pollutant = {
        "PM2.5": _latest_rows(pm25_rows_all),
        "PM10": _latest_rows(pm10_rows_all),
        "O3": _latest_rows(o3_rows_all),
    }

    def _region_rows(rows, region_name):
        if not region_name or region_name == "NSW":
            return rows
        return [row for row in rows if str(row.get("region") or "").strip() == region_name]

    def _avg(rows):
        values = []
        for row in rows or []:
            try:
                if row.get("value") is not None:
                    values.append(float(row.get("value")))
            except (TypeError, ValueError):
                continue
        return (sum(values) / float(len(values))) if values else None

    def _station_value(rows, station_name, region_name):
        for row in _region_rows(rows, region_name):
            if str(row.get("station") or "").strip() != station_name:
                continue
            try:
                return float(row.get("value")) if row.get("value") is not None else None
            except (TypeError, ValueError):
                return None
        return None

    regions = []
    for option in options:
        display_name = str((option or {}).get("name") or "NSW").strip() or "NSW"
        region_filter = None if display_name == "NSW" else display_name
        pm25_avg = _avg(_region_rows(latest_by_pollutant["PM2.5"], region_filter))
        pm10_avg = _avg(_region_rows(latest_by_pollutant["PM10"], region_filter))
        o3_avg = _avg(_region_rows(latest_by_pollutant["O3"], region_filter))
        category_key, colour = category_for_value("PM2.5", pm25_avg)
        pm10_category, pm10_colour = category_for_value("PM10", pm10_avg)
        o3_category, o3_colour = category_for_value("O3", o3_avg)
        station_payloads = []
        for station_name in (option or {}).get("stations") or []:
            station_payload = {"name": station_name}
            for pollutant in ("PM2.5", "PM10", "O3"):
                value = _station_value(latest_by_pollutant[pollutant], station_name, region_filter)
                station_category, station_colour = category_for_value(pollutant, value)
                station_payload[pollutant] = value
                station_payload[f"{pollutant}_category"] = station_category
                station_payload[f"{pollutant}_colour"] = station_colour
            station_payloads.append(
                station_payload
            )
        regions.append(
            {
                "name": display_name,
                "pm25": pm25_avg,
                "pm10": pm10_avg,
                "o3": o3_avg,
                "category": category_key,
                "colour": colour,
                "pm10_category": pm10_category,
                "pm10_colour": pm10_colour,
                "o3_category": o3_category,
                "o3_colour": o3_colour,
                "stations": station_payloads,
            }
        )
    return {"options": options, "regions": regions}


INITIAL_OVERVIEW_NSW_REGION_OPTIONS = _overview_nsw_region_options(INITIAL_MONITOR_STORE)
INITIAL_OVERVIEW_NSW_SUMMARY = _overview_nsw_summary_data(INITIAL_MONITOR_STORE)


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
                                    # Department name intentionally hidden to keep the dashboard title focused.
                                    # html.Div(
                                    #     [
                                    #         "Department of Climate Change,",
                                    #         html.Br(),
                                    #         "Energy, the Environment and Water",
                                    #     ],
                                    #     className="nsw-header__logo-text",
                                    # ),
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
                                html.H1(["NSW Air Quality Monitoring and", html.Br(), "Forecast Operations Dashboard"]),
                                className="banner",
                            ),
                            className="nsw-header__center",
                        ),

                        # Right: timestamps and actions
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
                                    style={"textAlign": "right", "marginBottom": "8px"},
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
                                    style={"textAlign": "right", "marginBottom": "8px"},
                                ),
                            ],
                            className="nsw-header__right",
                        ),
                    ],
                    className="nsw-header__wrapper",
                )
            ],
            className="nsw-header",
        ),
        dcc.Store(id="forecast-store", data=INITIAL_FORECAST_PARSED),
        dcc.Store(id="validation-store", data=INITIAL_FORECAST_PARSED),
        dcc.Store(id="selected-station-store", data=INITIAL_SELECTED_STATION),
        dcc.Store(
            id="monitor-settings-store",
            data={
                "pollutant": "PM2.5",
                "source": "both",
                "region": MONITOR_DEFAULT_REGION,
                "stationKey": None,
                "window": "24h",
                "categories": [],
                "search": "",
            },
        ),
        dcc.Store(
            id="monitor-store",
            data=INITIAL_MONITOR_STORE,
        ),
        dcc.Store(id="monitor-map-html-store", data=_leaflet_monitoring_map_html(INITIAL_MONITOR_ROWS)),
        dcc.Store(id="selected-monitor-site-store", data=None),
        dcc.Store(id="overview-nsw-region-index", data=0),
        dcc.Store(id="overview-nsw-station-index", data=0),
        dcc.Store(id="overview-nsw-region-options", data=INITIAL_OVERVIEW_NSW_REGION_OPTIONS),
        dcc.Store(id="overview-nsw-summary-store", data=INITIAL_OVERVIEW_NSW_SUMMARY),
        dcc.Store(id="overview-met-region-index", data=0),
        # Preload only one region so Overview paints quickly; scrolling loads more.
        dcc.Store(id="overview-trends-visible", data=["Sydney_East"]),
        dcc.Store(id="overview-trends-index", data=0),
        dcc.Store(id="overview-trends-region-options", data=[]),
        dcc.Interval(id="forecast-playback", interval=1800, n_intervals=0, disabled=True),
        # Start with memory/cache data, then refresh live feeds in the background for every session.
        dcc.Interval(id="monitor-refresh", interval=MONITOR_REFRESH_MS, n_intervals=0, disabled=False),
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
        html.Iframe(
            id="monitor-map-preload",
            srcDoc=_leaflet_monitoring_map_html(INITIAL_MONITOR_ROWS),
            title="Monitoring map preload",
            style={
                "position": "absolute",
                "left": "-10000px",
                "top": "0",
                "width": "640px",
                "height": "420px",
                "border": "0",
                "opacity": "0",
                "pointerEvents": "none",
            },
        ),
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
                                                                            html.H4("NSW AIR QUALITY STATUS"),
                                                                            html.Div(
                                                                                INITIAL_MONITOR_OVERVIEW_UPDATED_LABEL,
                                                                                id="overview-nsw-air-quality-updated",
                                                                                className="card-hint overview-updated-at",
                                                                            ),
                                                                        ],
                                                                        className="card-heading-row",
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
                                                            # Station-level pollutant box with navigation
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.Button("‹", id="overview-nsw-station-prev", n_clicks=0, className="nsw-aq-status__nav-btn"),
                                                                            html.Div("", id="overview-nsw-station-name", className="nsw-aq-status__nav-label"),
                                                                            html.Button("›", id="overview-nsw-station-next", n_clicks=0, className="nsw-aq-status__nav-btn"),
                                                                        ],
                                                                        className="nsw-aq-status__nav",
                                                                        style={"marginTop": "8px"},
                                                                    ),
                                                                    html.Div(id="overview-nsw-station-box", style={"marginTop": "8px"}),
                                                                ],
                                                                className="overview-nsw-station-container",
                                                            ),
                                                        ],
                                                        className="control-card overview-area--status",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.H4("Upcoming Forecast"),
                                                                    html.Div(
                                                                        id="overview-next3-time-labels",
                                                                        className="card-hint",
                                                                    ),
                                                                ],
                                                                className="card-heading-row",
                                                            ),
                                                            html.Div(id="overview-next3-grid", className="overview-next3-table overview-next3-table--tight"),
                                                            html.Button(
                                                                "All stations forecast",
                                                                id="overview-open-allstations",
                                                                n_clicks=0,
                                                                className="overview-next2-allstations__summary",
                                                            ),
                                                        ],
                                                        className="overview-next3-card overview-next3-card--inline overview-area--next3",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.H4("Regional Forecast Overview"),
                                                                ],
                                                                className="card-heading-row",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.Button(
                                                                                "⟨",
                                                                                id="overview-trends-prev",
                                                                                n_clicks=0,
                                                                                className="overview-trend-nav",
                                                                            ),
                                                                            html.Div(
                                                                                html.H4(id="overview-trends-current-region-label", children="", style={"margin": "0", "fontSize": "1.05rem"}),
                                                                                style={"flex": "1", "textAlign": "center"},
                                                                            ),
                                                                            html.Button(
                                                                                "⟩",
                                                                                id="overview-trends-next",
                                                                                n_clicks=0,
                                                                                className="overview-trend-nav",
                                                                            ),
                                                                        ],
                                                                        style={"display": "flex", "alignItems": "center", "gap": "12px", "width": "100%"},
                                                                    ),
                                                                ],
                                                                className="card-heading-row",
                                                            ),
                                                            html.Div(id="overview-forecast-trends", className="overview-trends-grid"),
                                                        ],
                                                        className="ranking-card overview-trends-card overview-area--trends",
                                                    ),
                                                                ],
                                                                className="overview-left-column",
                                                            ),
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.Div(
                                                                                [
                                                                                    html.H4("Forecast map"),
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
                                                                                    html.Div(
                                                                                        [
                                                                                            html.Button("‹", id="overview-forecast-prev-hour", n_clicks=0, className="nsw-aq-status__nav-btn", title="Previous hour"),
                                                                                            html.Button("›", id="overview-forecast-next-hour", n_clicks=0, className="nsw-aq-status__nav-btn", title="Next hour"),
                                                                                            dcc.Store(id="overview-forecast-hour-index", data=0),
                                                                                        ],
                                                                                        style={"display": "inline-flex", "gap": "8px", "marginLeft": "12px", "alignItems": "center"},
                                                                                    ),
                                                                                ],
                                                                                className="overview-map-heading",
                                                                            ),
                                                                            html.Div(
                                                                                id="overview-next-hour-forecast-time",
                                                                                className="card-hint",
                                                                            ),
                                                                        ],
                                                                        className="card-heading-row",
                                                                    ),
                                                                    html.Iframe(
                                                                        id="overview-next-hour-forecast-map",
                                                                        srcDoc=INITIAL_FORECAST_MAP_HTML,
                                                                        className="map-frame map-frame--overview-next map-frame--forecast",
                                                                    ),
                                                                    html.Div(id="overview-next-hour-station-panel", className="overview-next-hour-station-panel"),
                                                                ],
                                                                className="control-card overview-current-card",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.Div(
                                                                                [
                                                                                    html.H4("Meteorological conditions (last 24 hours)"),
                                                                                    html.Span("AQMS (region average)", className="card-hint"),
                                                                                ],
                                                                                className="card-heading-row",
                                                                            ),
                                                                            html.Div(
                                                                                [
                                                                                    html.Button("‹", id="overview-met-region-prev", n_clicks=0, className="nsw-aq-status__nav-btn"),
                                                                                    html.Div("NSW", id="overview-met-region-name", className="nsw-aq-status__nav-label"),
                                                                                    html.Button("›", id="overview-met-region-next", n_clicks=0, className="nsw-aq-status__nav-btn"),
                                                                                ],
                                                                                className="nsw-aq-status__nav",
                                                                                style={"marginTop": "6px"},
                                                                            ),
                                                                        ],
                                                                    ),
                                                                    html.Div(id="overview-met-6h", className="overview-met6h"),
                                                                ],
                                                                className="control-card",
                                                            ),
                                                        ],
                                                        className="overview-area--map",
                                                    ),
                                                ],
                                                className="overview-grid",
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
                                                    dcc.Dropdown(
                                                        id="monitor-region",
                                                        options=MONITOR_REGION_OPTIONS,
                                                        value=MONITOR_DEFAULT_REGION,
                                                        clearable=False,
                                                    ),
                                                    dcc.Dropdown(
                                                        id="monitor-station",
                                                        options=[],
                                                        value=None,
                                                        clearable=True,
                                                        placeholder="Select a station…",
                                                    ),
                                                    dcc.Dropdown(
                                                        id="monitor-pollutant",
                                                        options=MONITOR_POLLUTANT_OPTIONS,
                                                        value="PM2.5",
                                                        clearable=False,
                                                    ),
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
                                                    dcc.Dropdown(
                                                        id="monitor-category-filter",
                                                        options=MONITOR_CATEGORY_OPTIONS,
                                                        value=[],
                                                        multi=True,
                                                        placeholder="All categories",
                                                    ),
                                                    dcc.Input(
                                                        id="monitor-station-search",
                                                        type="text",
                                                        value="",
                                                        placeholder="Type to filter stations…",
                                                        className="monitor-search",
                                                    ),
                                                ],
                                                style={"display": "none"},
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(INITIAL_MONITOR_KPI_NODES, id="monitor-kpi-row", className="metric-row"),
                                                    html.Div(INITIAL_MONITOR_LATEST_LABEL, id="monitor-time-label", className="time-label"),
                                                ],
                                                className="monitor-diagnostics",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
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
                                                            html.Div(INITIAL_MONITOR_TOP_BOXES, id="monitor-top-boxes", className="monitor-top-boxes", style={"marginTop": "12px"}),
                                                        ],
                                                        className="monitor-main__map",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.H4(
                                                                                [
                                                                                    "Air quality outlook @ ",
                                                                                    html.Span(INITIAL_MONITOR_LATEST_LABEL, id="monitor-outlook-time", className="monitor-outlook-time"),
                                                                                ]
                                                                            ),
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
                                                                            {"name": "PM2.5", "id": "pm25_badge", "presentation": "markdown"},
                                                                            {"name": "PM10", "id": "pm10_badge", "presentation": "markdown"},
                                                                            {"name": "O3", "id": "o3_badge", "presentation": "markdown"},
                                                                        ],
                                                                        data=INITIAL_MONITOR_TABLE_ROWS,
                                                                        page_size=12,
                                                                        markdown_options={"html": True},
                                                                        style_table={"overflowX": "auto"},
                                                                        style_cell={
                                                                            "fontFamily": BASE_FONT_FAMILY,
                                                                            "fontSize": "0.92rem",
                                                                            "padding": "4px 8px",
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
                                                                            {"if": {"column_id": "pm25_badge"}, "textAlign": "center"},
                                                                            {"if": {"column_id": "pm10_badge"}, "textAlign": "center"},
                                                                            {"if": {"column_id": "o3_badge"}, "textAlign": "center"},
                                                                        ],
                                                                        style_data={"borderBottom": "1px solid #eef2f7"},
                                                                        style_data_conditional=INITIAL_MONITOR_TABLE_STYLES,
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
                                                                                    {"name": "PM2.5", "id": "pm25_badge", "presentation": "markdown"},
                                                                                    {"name": "PM10", "id": "pm10_badge", "presentation": "markdown"},
                                                                                    {"name": "O3", "id": "o3_badge", "presentation": "markdown"},
                                                                                    {"name": "CO", "id": "co_badge", "presentation": "markdown"},
                                                                                    {"name": "NO", "id": "no_badge", "presentation": "markdown"},
                                                                                    {"name": "NO2", "id": "no2_badge", "presentation": "markdown"},
                                                                                    {"name": "NOX", "id": "nox_badge", "presentation": "markdown"},
                                                                                ],
                                                                                data=INITIAL_MONITOR_TABLE_ALL_ROWS,
                                                                                page_size=20,
                                                                                sort_action="native",
                                                                                filter_action="native",
                                                                                markdown_options={"html": True},
                                                                                style_table={"overflowX": "auto"},
                                                                                style_cell={
                                                                                    "fontFamily": BASE_FONT_FAMILY,
                                                                                    "fontSize": "0.92rem",
                                                                                    "padding": "4px 8px",
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
                                                                                    {"if": {"column_id": "pm25_badge"}, "textAlign": "center"},
                                                                                    {"if": {"column_id": "pm10_badge"}, "textAlign": "center"},
                                                                                    {"if": {"column_id": "o3_badge"}, "textAlign": "center"},
                                                                                    {"if": {"column_id": "co_badge"}, "textAlign": "center"},
                                                                                    {"if": {"column_id": "no_badge"}, "textAlign": "center"},
                                                                                    {"if": {"column_id": "no2_badge"}, "textAlign": "center"},
                                                                                    {"if": {"column_id": "nox_badge"}, "textAlign": "center"},
                                                                                ],
                                                                                style_data={"borderBottom": "1px solid #eef2f7"},
                                                                                style_data_conditional=INITIAL_MONITOR_TABLE_STYLES,
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
                                                                    html.Div(
                                                                        [
                                                                            html.H4("Regional pollutant time series"),
                                                                            html.P("Last 24 hours for one AQMS station in the selected region.", className="monitor-region-series-subtitle"),
                                                                        ],
                                                                        className="monitor-region-series-heading",
                                                                    ),
                                                                    html.Div(
                                                                        [
                                                                            html.Div(
                                                                                [
                                                                                    html.Label("Region"),
                                                                                    dcc.Dropdown(
                                                                                        id="monitor-region-series",
                                                                                        options=MONITOR_REGION_SERIES_OPTIONS,
                                                                                        value=MONITOR_REGION_SERIES_DEFAULT_VALUE,
                                                                                        clearable=False,
                                                                                    ),
                                                                                ],
                                                                                className="filter-field monitor-region-series-filter",
                                                                            ),
                                                                            html.Div(
                                                                                [
                                                                                    html.Label("Station"),
                                                                                    dcc.Dropdown(
                                                                                        id="monitor-region-series-station",
                                                                                        options=MONITOR_REGION_SERIES_DEFAULT_STATION_OPTIONS,
                                                                                        value=MONITOR_REGION_SERIES_DEFAULT_STATION_VALUE,
                                                                                        clearable=False,
                                                                                    ),
                                                                                ],
                                                                                className="filter-field monitor-region-series-filter monitor-region-series-station-filter",
                                                                            ),
                                                                        ],
                                                                        className="monitor-region-series-filters",
                                                                    ),
                                                                    html.Div(id="monitor-region-series-note", className="monitor-region-series-note"),
                                                                    dcc.Loading(
                                                                        html.Div(id="monitor-region-series-plots", className="monitor-region-series-plots"),
                                                                        type="circle",
                                                                        color="#005eb8",
                                                                    ),
                                                                ],
                                                                className="control-card monitor-region-series-card",
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
                                                                    html.H4("Network status"),
                                                                    html.Div(INITIAL_MONITOR_NETWORK_NODES, id="monitor-network-status", className="network-status"),
                                                                ],
                                                                className="control-card monitor-network-card",
                                                            ),
                                                        ],
                                                        className="monitor-bottom",
                                                    ),
                                                    html.Div(INITIAL_MONITOR_STATUS_TEXT, id="monitor-status", className="monitor-status"),
                                                    html.Div(
                                                        id="monitor-selected-site",
                                                        className="selected-site",
                                                        style={"display": "none"},
                                                    ),
                                                    html.Div(
                                                        [
                                                            dcc.Graph(
                                                                id="monitor-compare-chart",
                                                                figure=go.Figure(),
                                                                config={"displayModeBar": False},
                                                            ),
                                                            html.Div(id="monitor-compare-summary", className="monitor-compare-summary"),
                                                            dcc.Graph(
                                                                id="monitor-trend-chart",
                                                                figure=go.Figure(),
                                                                config={"displayModeBar": False},
                                                            ),
                                                        ],
                                                        style={"display": "none"},
                                                    ),
                                        ],
                                        className="monitor-panel",
                                    )
                                ],
                                className="dashboard-tab-panel",
                            ),
                        ),
                        dcc.Tab(
                            label="Forecast",
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
                                                            html.H4("Forecasting"),
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
                                                                        options=INITIAL_FORECAST_DATE_OPTIONS,
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
                                                                        options=INITIAL_FORECAST_MODEL_OPTIONS,
                                                                        value=DEFAULT_SELECTION["models"],
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="filter-field",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Pollutant"),
                                                                    dcc.Dropdown(
                                                                        id="pollutant-dropdown",
                                                                        options=INITIAL_FORECAST_POLLUTANT_OPTIONS,
                                                                        value=DEFAULT_SELECTION["pollutants"],
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="filter-field",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Horizon"),
                                                                    dcc.Dropdown(
                                                                        id="time-dropdown",
                                                                        options=INITIAL_FORECAST_HORIZON_OPTIONS,
                                                                        value=DEFAULT_SELECTION["timeScopes"],
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="filter-field",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Region"),
                                                                    dcc.Dropdown(
                                                                        id="region-dropdown",
                                                                        options=INITIAL_FORECAST_REGION_OPTIONS,
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
                                                    html.Div(INITIAL_FORECAST_SUMMARY_CARDS, id="summary-cards", className="metric-row"),
                                                    html.Div(f"Showing {INITIAL_FORECAST_TIME_LABEL}" if INITIAL_FORECAST_PARSED else "Latest forecast run not loaded.", id="forecast-time-label", className="time-label time-label--forecast"),
                                                    html.Iframe(
                                                        id="forecast-map",
                                                        srcDoc=INITIAL_FORECAST_MAP_HTML,
                                                        className="map-frame map-frame--monitor map-frame--monitor-large map-frame--forecast",
                                                    ),
                                                    html.Div(
                                                        [
                                                            dcc.Slider(id="time-slider", min=0, max=max(len(INITIAL_FORECAST_TIMES) - 1, 0), value=0, step=1, marks=_slider_marks(INITIAL_FORECAST_TIMES)),
                                                            html.Div(INITIAL_FORECAST_FILE_INFO, id="file-info", className="file-info"),
                                                        ],
                                                        className="forecast-hidden-controls",
                                                    ),
                                                ],
                                                className="map-card",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.H4("Station ranking"),
                                                                    html.P("Predicted station values for the selected forecast hour."),
                                                                ],
                                                                className="forecast-rank-heading",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Forecast hour"),
                                                                    dcc.Dropdown(
                                                                        id="ranking-hour-dropdown",
                                                                        options=INITIAL_FORECAST_RANKING_HOUR_OPTIONS,
                                                                        value=INITIAL_FORECAST_TIME_INDEX,
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="forecast-rank-hour-control",
                                                            ),
                                                        ],
                                                        className="forecast-rank-header",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.A(
                                                                "All station forecast",
                                                                id="forecast-view-all",
                                                                href="#forecast-all-stations-modal",
                                                                n_clicks=0,
                                                                className="forecast-rank-eyebrow forecast-all-stations-link table-link",
                                                            ),
                                                            html.Div(
                                                                INITIAL_FORECAST_RANK_TIME_LABEL or "--",
                                                                id="forecast-rank-selected-time",
                                                                className="forecast-rank-time",
                                                            ),
                                                        ],
                                                        className="forecast-rank-summary",
                                                    ),
                                                    html.Div(INITIAL_FORECAST_RANKING_CARDS, id="ranking-cards"),
                                                    html.Hr(),
                                                    html.H4("Region Forecast"),
                                                    html.Div(INITIAL_STATION_DETAILS, id="station-details", className="station-forecast-note"),
                                                    dcc.Graph(id="station-trend", figure=INITIAL_STATION_TREND, config={"displayModeBar": False}),
                                                ],
                                                className="ranking-card",
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.H3("All station forecast", id="forecast-all-stations-title", className="modal-title"),
                                                                            html.Div(
                                                                                "Upcoming forecast horizons for every station.",
                                                                                id="forecast-all-stations-subtitle",
                                                                                className="forecast-all-stations-subtitle",
                                                                            ),
                                                                        ],
                                                                        className="forecast-all-stations-heading",
                                                                    ),
                                                                    html.Button(
                                                                        "Close",
                                                                        id="forecast-modal-close",
                                                                        n_clicks=0,
                                                                        className="header-action header-action--ghost modal-close",
                                                                    ),
                                                                ],
                                                                className="modal-header",
                                                            ),
                                                            dash_table.DataTable(
                                                                id="forecast-all-stations-table",
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
                                                                style_table={"overflowX": "auto", "maxHeight": "70vh", "overflowY": "auto"},
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
                                                                    "height": "34px",
                                                                    "padding": "5px 8px",
                                                                },
                                                                style_cell_conditional=[
                                                                    {"if": {"column_id": "station"}, "textAlign": "left", "minWidth": "150px", "width": "170px"},
                                                                    {"if": {"column_id": "region"}, "textAlign": "left", "minWidth": "130px", "width": "150px"},
                                                                    {"if": {"column_type": "text"}, "textAlign": "left"},
                                                                ],
                                                                style_data={"borderBottom": "1px solid #eef2f7", "height": "34px"},
                                                                style_data_conditional=_overview_allstations_value_styles(),
                                                            ),
                                                        ],
                                                        className="modal-card forecast-all-stations-modal-card",
                                                    )
                                                ],
                                                id="forecast-all-stations-modal",
                                                className="modal-overlay",
                                                style={"display": "none"},
                                            ),
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.H4("Observed + Forecast Time Series"),
                                                                    html.P("Choose a region and station explicitly; the plot will not change station automatically."),
                                                                ],
                                                                className="forecast-series-heading",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Div(
                                                                        [
                                                                            html.Label("Region"),
                                                                            dcc.Dropdown(
                                                                                id="time-series-region-dropdown",
                                                                                options=INITIAL_TIME_SERIES_REGION_OPTIONS,
                                                                                value=INITIAL_TIME_SERIES_REGION,
                                                                                clearable=False,
                                                                            ),
                                                                        ],
                                                                        className="forecast-series-filter",
                                                                    ),
                                                                    html.Div(
                                                                        [
                                                                            html.Label("Station"),
                                                                            dcc.Dropdown(
                                                                                id="time-series-station-dropdown",
                                                                                options=INITIAL_TIME_SERIES_STATION_OPTIONS,
                                                                                value=INITIAL_TIME_SERIES_STATION,
                                                                                clearable=False,
                                                                            ),
                                                                        ],
                                                                        className="forecast-series-filter forecast-series-filter--station",
                                                                    ),
                                                                ],
                                                                className="forecast-series-filters",
                                                            ),
                                                        ],
                                                        className="forecast-series-header",
                                                    ),
                                                    dcc.Graph(
                                                        id="forecast-time-series",
                                                        figure=INITIAL_TIME_SERIES,
                                                        config={"displayModeBar": False},
                                                    ),
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
                            value="validation",
                            className="dashboard-tab dashboard-tab--performance",
                            selected_className="dashboard-tab--selected",
                            children=html.Div(
                                [
                                    html.Section(
                                        [
                                            html.Div(
                                                [
                                                    html.Div(
                                                        [
                                                            html.H2("Validation"),
                                                            html.P("Compare observed monitoring values with the selected forecast run and region."),
                                                        ],
                                                        className="validation-heading",
                                                    ),
                                                    html.Div(
                                                        [
                                                            html.Div(
                                                                [
                                                                    html.Label("Date / time"),
                                                                    dcc.Dropdown(
                                                                        id="validation-date-dropdown",
                                                                        options=INITIAL_VALIDATION_DATE_OPTIONS,
                                                                        value=INITIAL_VALIDATION_DATE,
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="validation-filter",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Pollutant"),
                                                                    dcc.Dropdown(
                                                                        id="validation-pollutant-dropdown",
                                                                        options=INITIAL_VALIDATION_POLLUTANT_OPTIONS,
                                                                        value=INITIAL_VALIDATION_POLLUTANT,
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="validation-filter",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Horizon"),
                                                                    dcc.Dropdown(
                                                                        id="validation-horizon-dropdown",
                                                                        options=INITIAL_VALIDATION_HORIZON_OPTIONS,
                                                                        value=INITIAL_VALIDATION_HORIZON,
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="validation-filter",
                                                            ),
                                                            html.Div(
                                                                [
                                                                    html.Label("Region"),
                                                                    dcc.Dropdown(
                                                                        id="validation-region-dropdown",
                                                                        options=INITIAL_VALIDATION_REGION_OPTIONS,
                                                                        value=INITIAL_VALIDATION_REGION,
                                                                        clearable=False,
                                                                    ),
                                                                ],
                                                                className="validation-filter",
                                                            ),
                                                        ],
                                                        className="validation-toolbar",
                                                    ),
                                                    html.Div(id="validation-message", className="validation-message"),
                                                    html.Div(
                                                        html.Div(
                                                            _validation_station_plot_grid(INITIAL_FORECAST_PARSED, INITIAL_VALIDATION_POLLUTANT, INITIAL_VALIDATION_REGION),
                                                            id="validation-time-series-grid",
                                                        ),
                                                        className="validation-plot-card",
                                                    ),
                                                    html.Div(
                                                        _validation_metric_cards(INITIAL_FORECAST_PARSED, INITIAL_VALIDATION_POLLUTANT, INITIAL_FORECAST_TIME_INDEX, INITIAL_VALIDATION_REGION),
                                                        id="validation-metrics",
                                                    ),
                                                ],
                                                className="validation-panel",
                                            ),
                                        ],
                                        className="validation-wrap",
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
                            html.H4("All stations forecast"),
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
    regions = sorted({(site or {}).get("Region") for site in _sites_by_id().values() if (site or {}).get("Region")})
    return [{"label": "All regions", "value": "ALL"}] + [{"label": region, "value": region} for region in regions]


@app.callback(
    [Output("monitor-region", "value"), Output("monitor-region-series", "value")],
    [Input("dashboard-tabs", "value")],
    [State("monitor-region", "value"), State("monitor-region-series", "value")],
)
def initialize_monitor_defaults(active_tab, current_region, current_region_series):
    return MONITOR_DEFAULT_REGION, MONITOR_REGION_SERIES_DEFAULT_VALUE


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
        Output("date-dropdown", "options"),
        Output("date-dropdown", "value"),
        Output("model-dropdown", "options"),
        Output("model-dropdown", "value"),
        Output("pollutant-dropdown", "options"),
        Output("pollutant-dropdown", "value"),
        Output("time-dropdown", "options"),
        Output("time-dropdown", "value"),
        Output("region-dropdown", "options"),
        Output("region-dropdown", "value"),
    ],
    [
        Input("date-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("pollutant-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("region-dropdown", "value"),
    ],
)
def sync_forecast_dropdowns(date, model, pollutant, horizon, region):
    selection = {
        "date": date,
        "models": model,
        "pollutants": pollutant,
        "timeScopes": horizon,
        "regions": region,
    }
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    trigger_field = {
        "date-dropdown": "date",
        "model-dropdown": "models",
        "pollutant-dropdown": "pollutants",
        "time-dropdown": "timeScopes",
        "region-dropdown": "regions",
    }.get(trigger, "date")
    normalised = _normalise_forecast_selection(selection, priority_field=trigger_field)
    return (
        _forecast_options_for_field("date", normalised),
        normalised.get("date"),
        _forecast_options_for_field("models", normalised),
        normalised.get("models"),
        _forecast_options_for_field("pollutants", normalised),
        normalised.get("pollutants"),
        _forecast_options_for_field("timeScopes", normalised),
        normalised.get("timeScopes"),
        _forecast_options_for_field("regions", normalised),
        normalised.get("regions"),
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
        Input("dashboard-tabs", "value"),
        Input("region-dropdown", "value"),
        Input("pollutant-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("forecast-playback", "n_intervals"),
        Input("overview-forecast-load", "n_intervals"),
    ],
    [State("time-slider", "value"), State("forecast-store", "data")],
    prevent_initial_call=True,
)
def load_forecast(active_tab, region, pollutant, time_scope, model, date, _n_intervals, _overview_load, current_time_value, current_store):
    selection = {
        "regions": region,
        "pollutants": pollutant,
        "timeScopes": time_scope,
        "models": model,
        "date": date,
    }
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    return _load_forecast_payload(active_tab, selection, trigger, current_time_value, current_store)


def _load_forecast_payload(active_tab, selection, trigger, current_time_value, current_store):
    if current_store and current_store.get("_selection") == selection:
        times = current_store.get("data", {}).get("time", {}).get("forecastTime", [])
        slider_max = max(len(times) - 1, 0)
        if trigger == "forecast-playback":
            slider_value = current_time_value if current_time_value is not None else 0
            slider_value += 1
            if slider_value > slider_max:
                slider_value = 0
            return no_update, no_update, no_update, slider_value, no_update
        return no_update, no_update, no_update, no_update, no_update

    parsed, message = _load_forecast(selection)
    if not parsed:
        return None, message, 0, 0, {0: "--"}

    parsed["_selection"] = selection
    parsed["_message"] = message

    times = parsed.get("data", {}).get("time", {}).get("forecastTime", [])
    slider_max = max(len(times) - 1, 0)
    if trigger == "forecast-playback":
        current_value = current_time_value if current_time_value is not None else 0
        slider_value = current_value + 1
        if slider_value > slider_max:
            slider_value = 0
    else:
        slider_value = 0

    return parsed, "", slider_max, slider_value, _slider_marks(times)


@app.callback(
    Output("monitor-refresh", "disabled"),
    [Input("dashboard-tabs", "value")],
)
def toggle_monitor_refresh(active_tab):
    return False


@app.callback(
    Output("monitor-map-html-store", "data"),
    [Input("monitor-store", "data")],
    [State("monitor-map-html-store", "data")],
)
def update_monitor_map_html_store(monitor_store, current_html):
    global _LAST_GOOD_MONITOR_MAP_ROWS, _LAST_GOOD_MONITOR_MAP_HTML
    monitor_store = monitor_store or {}
    map_rows = monitor_store.get("mapRows") or []
    if map_rows:
        _LAST_GOOD_MONITOR_MAP_ROWS = list(map_rows)
        next_html = _leaflet_monitoring_map_html(map_rows)
        if current_html and current_html == next_html:
            _LAST_GOOD_MONITOR_MAP_HTML = current_html
            return no_update
        _LAST_GOOD_MONITOR_MAP_HTML = next_html
        return next_html

    if current_html:
        _LAST_GOOD_MONITOR_MAP_HTML = current_html
        return no_update

    if _LAST_GOOD_MONITOR_MAP_HTML:
        return _LAST_GOOD_MONITOR_MAP_HTML

    if _LAST_GOOD_MONITOR_MAP_ROWS:
        _LAST_GOOD_MONITOR_MAP_HTML = _leaflet_monitoring_map_html(_LAST_GOOD_MONITOR_MAP_ROWS)
        return _LAST_GOOD_MONITOR_MAP_HTML

    return _leaflet_monitoring_map_html([])


@app.callback(Output("monitor-map", "srcDoc"), [Input("monitor-map-html-store", "data")])
def render_monitor_map_srcdoc(map_html):
    global _LAST_GOOD_MONITOR_MAP_HTML
    if map_html:
        _LAST_GOOD_MONITOR_MAP_HTML = map_html
        return map_html
    if _LAST_GOOD_MONITOR_MAP_HTML:
        return _LAST_GOOD_MONITOR_MAP_HTML
    if _LAST_GOOD_MONITOR_MAP_ROWS:
        return _leaflet_monitoring_map_html(_LAST_GOOD_MONITOR_MAP_ROWS)
    return _leaflet_monitoring_map_html([])


@app.callback(Output("monitor-map-preload", "srcDoc"), [Input("monitor-map-html-store", "data")])
def preload_monitor_map(map_html):
    global _LAST_GOOD_MONITOR_MAP_HTML
    if map_html:
        _LAST_GOOD_MONITOR_MAP_HTML = map_html
        return map_html
    if _LAST_GOOD_MONITOR_MAP_HTML:
        return _LAST_GOOD_MONITOR_MAP_HTML
    elif _LAST_GOOD_MONITOR_MAP_ROWS:
        return _leaflet_monitoring_map_html(_LAST_GOOD_MONITOR_MAP_ROWS)
    return _leaflet_monitoring_map_html([])


@app.callback(
    Output("monitor-store", "data"),
    [
        Input("monitor-refresh", "n_intervals"),
        Input("dashboard-tabs", "value"),
        Input("monitor-settings-store", "data"),
        Input("overview-monitor-load", "n_intervals"),
    ],
    [State("monitor-store", "data")],
)
def refresh_monitoring(_n_intervals, active_tab, monitor_settings, _overview_load, current_store):
    global _LAST_GOOD_MONITOR_MAP_ROWS
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    on_monitor = True
    on_overview_snapshot = True
    include_history = trigger == "monitor-refresh"
    monitor_settings = _as_dict(monitor_settings)
    current_store = _as_dict(current_store)
    pollutant_label = (monitor_settings.get("pollutant") or "PM2.5").strip()
    source_filter = (monitor_settings.get("source") or "both").strip().lower()

    # module-level snapshot accessed via globals() to avoid local/global parsing issues

    # Overview snapshot: AQMS only (no PurpleAir) so the top-left status card always has data.
    if on_overview_snapshot and not on_monitor:
        pollutant_label = "PM2.5"
        source_filter = "aqms"

    pollutant_meta = POLLUTANTS.get(pollutant_label) or {}
    parameter_code = pollutant_meta.get("ParameterCode") or pollutant_label

    status_bits = []
    error = None
    try:
        raw_obs = fetch_observations(query=None, timeout=25)
        status_bits.append("AQMS feed: Live")
        aqms_ok = True
    except Exception as exc:
        raw_obs = []
        aqms_ok = False
        error = f"AQMS feed unavailable: {exc}"
        status_bits.append("AQMS feed: Offline")

    purpleair_bounds = NSW_MAP_BOUNDS
    purpleair_payload = {"sensors": [], "fetched_at": None, "error": None}
    if on_monitor or on_overview_snapshot:
        purpleair_payload = fetch_purpleair_snapshot(bounds=purpleair_bounds, timeout=12)
        if purpleair_payload.get("error"):
            status_bits.append("PurpleAir feed: Offline")
            if error is None:
                error = f"PurpleAir feed unavailable: {purpleair_payload.get('error')}"
        elif purpleair_payload.get("source") in {"bundle", "cache"}:
            status_bits.append("PurpleAir feed: Cached")
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
    # Optional nitrogen parameters (not always present in the AQMS feed).
    aqms_no_rows = _aqms_rows_for_pollutant(raw_obs, "NO", "NO")
    aqms_nox_rows = _aqms_rows_for_pollutant(raw_obs, "NOX", "NOX")
    # Always compute site-level AQC (Air Quality Category) rows for the Overview tab.
    aqc_rows = _aqms_rows_for_pollutant(raw_obs, "AQC", "AQC")
    aqms_history_rows = []
    if include_history and raw_obs:
        try:
            history_site_ids = sorted({int(item.get("Site_Id")) for item in raw_obs if item.get("Site_Id") is not None})
        except Exception:
            history_site_ids = []
        history_parameter_codes = []
        for pollutant_name in ["PM2.5", "PM10", "O3", "CO", "NO", "NO2", "NOX"]:
            code = (POLLUTANTS.get(pollutant_name) or {}).get("ParameterCode") or pollutant_name
            if code and code not in history_parameter_codes:
                history_parameter_codes.append(code)
        if history_site_ids and history_parameter_codes:
            now = datetime.now(SYDNEY_TZ)
            start_date = (now.date() - timedelta(days=1)).isoformat()
            end_date = now.date().isoformat()
            try:
                aqms_history_rows = fetch_observation_history(history_site_ids, history_parameter_codes, start_date, end_date, timeout=12)
            except Exception:
                aqms_history_rows = []
    aqms_previous_by_site = _aqms_previous_hour_values_by_site(raw_obs, aqms_history_rows)

    # Meteorology-capable AQMS sites (from latest snapshot, used to keep history calls small).
    met_site_ids_by_region = {}
    try:
        wanted_met = {"TEMP", "HUMID", "WSP", "RAIN"}
        for item in raw_obs or []:
            param = _aqms_parameter(item)
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

    aqms_map_rows = _aqms_station_map_rows(
        {
            "PM2.5": aqms_pm25_rows,
            "PM10": aqms_pm10_rows,
            "O3": aqms_o3_rows,
            "NO2": aqms_no2_rows,
            "NO": aqms_no_rows,
            "NOX": aqms_nox_rows,
            "SO2": aqms_so2_rows,
            "CO": aqms_co_rows,
            "AQC": aqc_rows,
        },
        selected_pollutant=pollutant_label,
    )

    map_rows = []
    if source_filter in {"both", "aqms"}:
        map_rows.extend(aqms_map_rows)
    if source_filter in {"both", "purpleair"}:
        map_rows.extend(purpleair_sensors)
    if map_rows:
        _LAST_GOOD_MONITOR_MAP_ROWS = list(map_rows)
    elif current_store and current_store.get("mapRows"):
        map_rows = current_store.get("mapRows") or []
    elif _LAST_GOOD_MONITOR_MAP_ROWS:
        map_rows = list(_LAST_GOOD_MONITOR_MAP_ROWS)

    # Convenience: snapshot per-site for station detail panels.
    aqms_snapshot_by_site = {}
    for key, rows in {
        "PM2.5": aqms_pm25_rows,
        "PM10": aqms_pm10_rows,
        "O3": aqms_o3_rows,
        "NO": aqms_no_rows,
        "NO2": aqms_no2_rows,
        "NOX": aqms_nox_rows,
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
    table_rows = _monitor_table_rows(aqms_rows, purpleair_sensors, aqms_snapshot_by_site, aqms_previous_by_site, max_rows=40)
    table_rows_all = _monitor_table_rows_all(aqms_rows, purpleair_sensors, aqms_snapshot_by_site, aqms_previous_by_site)
    feeds = {
        "aqms": {"status": "Live" if aqms_ok else "Offline"},
        "purpleair": {"status": "Live" if not purpleair_payload.get("error") else "Offline"},
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
        "aqmsNoRows": aqms_no_rows,
        "aqmsNoxRows": aqms_nox_rows,
        "aqmsSo2Rows": aqms_so2_rows,
        "aqmsCoRows": aqms_co_rows,
        "aqcRows": aqc_rows,
        "metSiteIdsByRegion": {k: sorted(list(v)) for k, v in (met_site_ids_by_region or {}).items()},
        "aqmsSnapshotBySite": aqms_snapshot_by_site,
        "aqmsPreviousBySite": aqms_previous_by_site,
        "purpleairSnapshot": purpleair_payload,
        "purpleairSensors": purpleair_sensors,
        "purpleairClusters": clusters,
        "mapRows": map_rows,
        "kpis": kpis,
        "tableRows": table_rows,
        "tableRowsAll": table_rows_all,
        "feeds": feeds,
        "latestLabel": latest_label,
        "fetchedAtEpoch": datetime.now(SYDNEY_TZ).timestamp(),
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
    settings = dict(_as_dict(current_settings))
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
        settings["region"] = MONITOR_DEFAULT_REGION
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
        Output("monitor-kpi-row", "children"),
        Output("monitor-time-label", "children"),
        Output("monitor-outlook-time", "children"),
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
    monitor_store = _as_dict(monitor_store)
    monitor_settings = _as_dict(monitor_settings)
    kpis = monitor_store.get("kpis") or {}
    table_rows = monitor_store.get("tableRows") or []
    table_rows_all = monitor_store.get("tableRowsAll") or []
    feeds = monitor_store.get("feeds") or {}
    latest_label = monitor_store.get("latestLabel") or "--"
    latest_display_label = latest_label
    if _monitor_data_is_stale(monitor_store) and latest_label != "--":
        latest_display_label = f"Last cached: {latest_label}"
    status = monitor_store.get("status") or "Monitoring feed is loading."
    error = monitor_store.get("error")

    region_value = str(monitor_settings.get("region") or MONITOR_DEFAULT_REGION).strip()
    source_value = str(monitor_settings.get("source") or "both").strip().lower()
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
    # Build the compact outlook table from the full row set, not the pre-trimmed
    # store sample, so AQMS stations in the selected region are not dropped before
    # region/category/search filters run.
    table_rows_all = _attach_arrows_to_table_rows(table_rows_all, monitor_store.get("aqmsSnapshotBySite"), monitor_store.get("aqmsPreviousBySite"))
    table_rows_all = _inject_badges_into_monitor_rows(table_rows_all)
    table_rows_all = [row for row in table_rows_all if _match_station(row)]
    compact_seed_rows = [row for row in table_rows_all if not row.get("is_placeholder")]
    if not compact_seed_rows:
        compact_seed_rows = table_rows_all
    table_rows = _ordered_monitor_outlook_rows(compact_seed_rows, source_value=source_value, compact=True, max_rows=12)
    table_rows_all = _ordered_monitor_outlook_rows(table_rows_all, source_value=source_value)

    kpi_nodes = _monitor_kpi_cards(kpis)
    style_conditional = _monitor_table_styles()
    style_conditional_all = _monitor_table_styles()
    network_nodes = _monitor_network_nodes(feeds)
    return (
        kpi_nodes,
        latest_display_label,
        latest_display_label,
        table_rows,
        style_conditional,
        network_nodes,
        error or status,
        table_rows_all,
        style_conditional_all,
    )


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
        Input("dashboard-tabs", "value"),
        Input("region-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
        Input("overview-forecast-map-pollutant", "value"),
        Input("overview-forecast-prev-hour", "n_clicks"),
        Input("overview-forecast-next-hour", "n_clicks"),
    ],
    [State("overview-forecast-hour-index", "data")],
)
def render_overview_next_hour_forecast_map(
    active_tab,
    region,
    time_scope,
    model,
    date,
    _overview_load,
    overview_pollutant,
    prev_clicks,
    next_clicks,
    current_index,
):
    global _LAST_GOOD_OVERVIEW_FORECAST_MAP_HTML
    if active_tab != "overview":
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

    # Reset index to 0 when core selection inputs change
    if trigger in ("dashboard-tabs", "region-dropdown", "time-dropdown", "model-dropdown", "date-dropdown", "overview-forecast-load", "overview-forecast-map-pollutant"):
        new_index = 0
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

    time_index = new_index if times else 0
    if merged_stations:
        map_html = _leaflet_forecast_map_html(merged_parsed, pollutant, time_index, include_series=False)
        _LAST_GOOD_OVERVIEW_FORECAST_MAP_HTML = map_html
        map_output = map_html
    else:
        map_output = no_update

    # Build user-facing label (show only the timestamp in uppercase, larger font)
    label_el = ""
    if times:
        sel_time = times[time_index]
        parsed_dt = _parse_forecast_timestamp(sel_time) if sel_time else None
        if parsed_dt:
            hour = parsed_dt.strftime("%I").lstrip("0") or "0"
            label_text = f"{parsed_dt.strftime('%b').upper()} {parsed_dt.day} {hour}{parsed_dt.strftime('%p')}"
            label_el = html.Div(label_text, style={"fontSize": "1.25rem", "fontWeight": "700", "letterSpacing": "0.5px"})
    else:
        label_el = html.Div("", className="card-hint")

    return map_output, label_el, new_index


@app.callback(
    Output("overview-next-hour-station-panel", "children"),
    [
        Input("url", "hash"),
        Input("dashboard-tabs", "value"),
        Input("region-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
        Input("overview-forecast-map-pollutant", "value"),
    ],
)
def render_overview_next_hour_station_panel(url_hash, active_tab, region, time_scope, model, date, _overview_load, overview_pollutant):
    if active_tab != "overview":
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
                next_time_value = times[0]
        _name, found_val = _find_station_forecast_value(parsed, station_key, hour_index=0)
        if _name is not None:
            break

    dt = _parse_forecast_timestamp(next_time_value) if next_time_value else None
    if dt:
        hour = dt.strftime("%I").lstrip("0") or "0"
        time_label = f"{dt.strftime('%b')} {dt.day} {hour}{dt.strftime('%p')}"
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
        Input("dashboard-tabs", "value"),
        Input("region-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
    ],
)
def render_overview_next3_diamonds(active_tab, region, time_scope, model, date, _overview_load):
    if active_tab != "overview":
        return no_update, no_update

    selection = {
        "regions": region or DEFAULT_SELECTION.get("regions"),
        "pollutants": "PM2.5",
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model or DEFAULT_SELECTION.get("models"),
        "date": date or DEFAULT_SELECTION.get("date"),
    }

    time_labels, rows = _overview_next3_values(selection)

    max_cols = 2

    # Header hour labels.
    hours_pretty = []
    for ts in time_labels or []:
        hours_pretty.append(_forecast_hour_label(ts))

    units_by_pollutant = {k: (POLLUTANTS.get(k) or {}).get("Units") or "" for k in ["PM2.5", "PM10", "O3"]}

    grid_children = []
    grid_children.append(html.Div("", className="overview-next2__corner"))
    for label in (hours_pretty or ["--"] * max_cols)[:max_cols]:
        grid_children.append(html.Div(label, className="overview-next2__hour"))

    for row in rows or []:
        pollutant = row.get("pollutant") or "--"
        units = units_by_pollutant.get(pollutant) or ""
        grid_children.append(
            html.Div(
                [
                    html.Div(
                        [
                            html.Div(pollutant, className="overview-next2__pollutant-name"),
                            html.Div(units, className="overview-next2__pollutant-unit"),
                        ]
                    ),
                ],
                className="overview-next2__pollutant",
            )
        )

        values = (row.get("values") or [])[:max_cols]
        stations = (row.get("stations") or [])[:max_cols]
        while len(values) < max_cols:
            values.append(None)
        while len(stations) < max_cols:
            stations.append(None)

        for idx in range(max_cols):
            val = values[idx]
            station_label = title_case_station_name(stations[idx]) if stations[idx] else "--"
            category_key, colour = category_for_value(pollutant, val)
            category_label = str(category_key or "no-data").replace("-", " ").title()
            value_text = "--" if val is None else f"{float(val):.1f}"
            grid_children.append(
                html.Div(
                    [
                        html.Div(station_label, className="overview-next2__station"),
                        html.Div(category_label, className="overview-next2__category", style={"backgroundColor": colour}),
                        html.Div(f"{value_text} {units}".strip(), className="overview-next2__value"),
                    ],
                    className="overview-next2__card",
                )
            )

    return html.Div(grid_children, className="overview-next2-grid"), ""


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
    base_index = 0

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
                    time_labels = list(times)
                    base_index = _forecast_live_base_index(time_labels)

            stations = ((parsed.get("data") or {}).get("stations") or {})
            for station_name, payload in stations.items():
                row = _ensure_row(station_name, region_name)
                if not row:
                    continue
                series = (payload or {}).get("forecastValue") or []
                for hour_idx in range(hours):
                    value_num = None
                    source_idx = base_index + hour_idx
                    if source_idx < len(series):
                        value_num = _coerce_float(series[source_idx])
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
        time_idx = base_index + idx
        if time_idx < len(time_labels):
            pretty_labels.append(_forecast_hour_label(time_labels[time_idx]))
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
        Input("dashboard-tabs", "value"),
        Input("region-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
        Input("overview-open-allstations", "n_clicks"),
    ],
    [State("overview-allstations-overlay", "style")],
)
def render_overview_next2_allstations(active_tab, region, time_scope, model, date, _overview_load, open_allstations_clicks, overlay_style):
    # Populate when either the (now-removed) All-stations tab is active or the
    # overlay open button was clicked.
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    overlay_style = overlay_style or {}
    overlay_is_open = overlay_style.get("display") not in (None, "", "none")
    if active_tab not in {"overview", "allstations"}:
        return no_update, no_update
    if trigger != "overview-open-allstations" and not overlay_is_open:
        return no_update, no_update
    selection = {
        "regions": region or DEFAULT_SELECTION.get("regions"),
        "pollutants": "PM2.5",
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model or DEFAULT_SELECTION.get("models"),
        "date": date or DEFAULT_SELECTION.get("date"),
    }
    return _overview_allstations_multi_hour_rows(selection, hours=2)


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
    title = f"Regional Air Quality at {latest_label}" if latest_label and latest_label != "--" else "Regional Air Quality"

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
    return _monitor_updated_label(monitor_store)


@app.callback(Output("overview-nsw-region-options", "data"), [Input("monitor-store", "data")], prevent_initial_call=True)
def sync_overview_nsw_region_options(monitor_store):
    return _overview_nsw_region_options(monitor_store)


@app.callback(Output("overview-nsw-summary-store", "data"), [Input("monitor-store", "data")], prevent_initial_call=True)
def sync_overview_nsw_summary_store(monitor_store):
    return _overview_nsw_summary_data(monitor_store)


app.clientside_callback(
    """
    function(prevClicks, nextClicks, options, currentIndex) {
        const ctx = dash_clientside.callback_context;
        const count = Array.isArray(options) && options.length ? options.length : 1;
        let idx = Number.isFinite(Number(currentIndex)) ? Number(currentIndex) : 0;
        if (!ctx.triggered.length) {
            return Math.max(0, Math.min(idx, count - 1));
        }
        const prop = ctx.triggered[0].prop_id || "";
        if (prop.indexOf("overview-nsw-region-prev") === 0) {
            idx = (idx - 1 + count) % count;
        } else if (prop.indexOf("overview-nsw-region-next") === 0) {
            idx = (idx + 1) % count;
        }
        return Math.max(0, Math.min(idx, count - 1));
    }
    """,
    Output("overview-nsw-region-index", "data"),
    [Input("overview-nsw-region-prev", "n_clicks"), Input("overview-nsw-region-next", "n_clicks"), Input("overview-nsw-region-options", "data")],
    [State("overview-nsw-region-index", "data")],
)


app.clientside_callback(
    """
    function(regionIndex, options) {
        const opts = Array.isArray(options) && options.length ? options : [{name: "NSW", stations: []}];
        const idx = Math.max(0, Math.min(Number(regionIndex) || 0, opts.length - 1));
        return opts[idx].name || "NSW";
    }
    """,
    Output("overview-nsw-region-name", "children"),
    [Input("overview-nsw-region-index", "data"), Input("overview-nsw-region-options", "data")],
)


app.clientside_callback(
    """
    function(prevClicks, nextClicks, nswRegionIndex, options, currentIndex) {
        const ctx = dash_clientside.callback_context;
        const opts = Array.isArray(options) && options.length ? options : [{name: "NSW"}];
        const count = opts.length || 1;
        let idx = Number.isFinite(Number(currentIndex)) ? Number(currentIndex) : 0;
        if (!ctx.triggered.length) {
            return Math.max(0, Math.min(idx, count - 1));
        }
        const prop = ctx.triggered[0].prop_id || "";
        if (prop.indexOf("overview-nsw-region-index") === 0) {
            return Math.max(0, Math.min(Number(nswRegionIndex) || 0, count - 1));
        }
        if (prop.indexOf("overview-met-region-prev") === 0) {
            idx = (idx - 1 + count) % count;
        } else if (prop.indexOf("overview-met-region-next") === 0) {
            idx = (idx + 1) % count;
        }
        return Math.max(0, Math.min(idx, count - 1));
    }
    """,
    Output("overview-met-region-index", "data"),
    [
        Input("overview-met-region-prev", "n_clicks"),
        Input("overview-met-region-next", "n_clicks"),
        Input("overview-nsw-region-index", "data"),
        Input("overview-nsw-region-options", "data"),
    ],
    [State("overview-met-region-index", "data")],
)


app.clientside_callback(
    """
    function(regionIndex, options) {
        const opts = Array.isArray(options) && options.length ? options : [{name: "NSW"}];
        const idx = Math.max(0, Math.min(Number(regionIndex) || 0, opts.length - 1));
        return opts[idx].name || "NSW";
    }
    """,
    Output("overview-met-region-name", "children"),
    [Input("overview-met-region-index", "data"), Input("overview-nsw-region-options", "data")],
)


app.clientside_callback(
    """
    function(prevClicks, nextClicks, regionIndex, options, currentIndex) {
        const ctx = dash_clientside.callback_context;
        const opts = Array.isArray(options) && options.length ? options : [{name: "NSW", stations: []}];
        const regionIdx = Math.max(0, Math.min(Number(regionIndex) || 0, opts.length - 1));
        const stations = Array.isArray(opts[regionIdx].stations) ? opts[regionIdx].stations : [];
        const count = stations.length || 1;
        let idx = Number.isFinite(Number(currentIndex)) ? Number(currentIndex) : 0;
        if (!ctx.triggered.length) {
            return Math.max(0, Math.min(idx, count - 1));
        }
        const prop = ctx.triggered[0].prop_id || "";
        if (prop.indexOf("overview-nsw-region-index") === 0 || prop.indexOf("overview-nsw-region-options") === 0) {
            return 0;
        }
        if (prop.indexOf("overview-nsw-station-prev") === 0) {
            idx = (idx - 1 + count) % count;
        } else if (prop.indexOf("overview-nsw-station-next") === 0) {
            idx = (idx + 1) % count;
        }
        return Math.max(0, Math.min(idx, count - 1));
    }
    """,
    Output("overview-nsw-station-index", "data"),
    [
        Input("overview-nsw-station-prev", "n_clicks"),
        Input("overview-nsw-station-next", "n_clicks"),
        Input("overview-nsw-region-index", "data"),
        Input("overview-nsw-region-options", "data"),
    ],
    [State("overview-nsw-station-index", "data")],
)


app.clientside_callback(
    """
    function(regionIndex, stationIndex, options) {
        const opts = Array.isArray(options) && options.length ? options : [{name: "NSW", stations: []}];
        const regionIdx = Math.max(0, Math.min(Number(regionIndex) || 0, opts.length - 1));
        const stations = Array.isArray(opts[regionIdx].stations) ? opts[regionIdx].stations : [];
        if (!stations.length) {
            return "";
        }
        const stationIdx = Math.max(0, Math.min(Number(stationIndex) || 0, stations.length - 1));
        return stations[stationIdx] || "";
    }
    """,
    Output("overview-nsw-station-name", "children"),
    [Input("overview-nsw-region-index", "data"), Input("overview-nsw-station-index", "data"), Input("overview-nsw-region-options", "data")],
)


@app.callback(
    Output("overview-nsw-air-quality-status", "children"),
    [Input("overview-nsw-summary-store", "data"), Input("overview-nsw-region-index", "data")],
)
def render_overview_nsw_air_quality_status(summary_store, region_index):
    """Render NSW/region PM2.5 network average from AQMS (no PurpleAir)."""
    summary_store = summary_store or {}
    regions = summary_store.get("regions") or []
    if not regions:
        return html.Div("Loading AQMS monitoring data…", className="card-hint")

    try:
        idx = int(region_index or 0)
    except (TypeError, ValueError):
        idx = 0
    idx = max(0, min(idx, len(regions) - 1))
    payload = regions[idx] or {}
    avg = payload.get("pm25")
    pm10_avg = payload.get("pm10")
    o3_avg = payload.get("o3")
    colour = payload.get("colour") or category_for_value("PM2.5", avg)[1]
    category = _pollutant_category_label("PM2.5", colour)

    units = (POLLUTANTS.get("PM2.5") or {}).get("Units") or "µg/m³"
    avg_label = "--" if avg is None else f"{float(avg):.1f} {units}".strip()
    pm10_units = (POLLUTANTS.get("PM10") or {}).get("Units") or "µg/m³"
    o3_units = (POLLUTANTS.get("O3") or {}).get("Units") or ""
    pm10_label = "--" if pm10_avg is None else f"{float(pm10_avg):.1f} {pm10_units}".strip()
    o3_label = "--" if o3_avg is None else f"{float(o3_avg):.1f} {o3_units}".strip()

    pm10_category_key, pm10_colour = category_for_value("PM10", pm10_avg)
    o3_category_key, o3_colour = category_for_value("O3", o3_avg)
    delta = None
    pm10_delta = None
    o3_delta = None

    def _arrow_span(d):
        if d is None or abs(float(d)) <= 1e-9:
            return ""
        dv = float(d)
        if dv > 0:
            return html.Span(" ↑", style={"color": "#dc2626", "fontWeight": 900, "marginLeft": "6px"})
        return html.Span(" ↓", style={"color": "#16a34a", "fontWeight": 900, "marginLeft": "6px"})

    return html.Div(
        [
            html.Div(
                [
                    html.Div(
                        [
                            html.Div("Avg (PM2.5)", className="nsw-aq-status__metric-label"),
                            html.Div([html.Span(avg_label), _arrow_span(delta)], className="nsw-aq-status__metric-value", style={"color": colour}),
                            html.Div(category, className="nsw-aq-status__metric-category", style={"color": colour}),
                        ],
                        className="nsw-aq-status__metric nsw-aq-status__metric--pm25",
                    ),
                    html.Div(
                        [
                            html.Div("AQMS averages", className="nsw-aq-status__kpi-label"),
                            html.Div([html.Span(f"PM10 · {pm10_label}"), _arrow_span(pm10_delta)], className="nsw-aq-status__kpi-value"),
                            html.Div([html.Span(f"O3 · {o3_label}"), _arrow_span(o3_delta)], className="nsw-aq-status__kpi-value"),
                        ],
                        className="nsw-aq-status__kpis",
                    ),
                ],
                className="nsw-aq-status__row",
            ),
        ],
        className="nsw-aq-status",
        style={"borderTop": f"6px solid {colour}"},
    )

    global _LAST_NSW_PM25_AVG
    monitor_store = monitor_store or {}
    pm25_rows_all = monitor_store.get("aqmsPm25Rows") or []
    if not pm25_rows_all:
        error = monitor_store.get("error")
        if error:
            return html.Div(str(error), className="card-hint")
        status = monitor_store.get("status") or ""
        if status and "Offline" in status:
            return html.Div("AQMS monitoring feed is offline.", className="card-hint")
        return html.Div("Loading AQMS monitoring data…", className="card-hint")


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

    return html.Div(
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
    Output("overview-nsw-station-box", "children"),
    [Input("overview-nsw-summary-store", "data"), Input("overview-nsw-region-index", "data"), Input("overview-nsw-station-index", "data")],
)
def render_overview_nsw_station_box(summary_store, region_index, station_index):
    """Render a small station pollutant box for the selected region and station index."""
    summary_store = summary_store or {}
    regions = summary_store.get("regions") or []
    if not regions:
        return html.Div("No data", className="card-hint")

    try:
        idx_reg = int(region_index or 0)
    except (TypeError, ValueError):
        idx_reg = 0
    idx_reg = max(0, min(idx_reg, len(regions) - 1))
    stations = (regions[idx_reg] or {}).get("stations") or []
    if not stations:
        return html.Div("No stations", className="card-hint")

    try:
        sidx = int(station_index or 0)
    except (TypeError, ValueError):
        sidx = 0
    sidx = max(0, min(sidx, len(stations) - 1))
    station_payload = stations[sidx] or {}

    def _box(pollutant):
        val = station_payload.get(pollutant)
        try:
            v = float(val) if val is not None else None
        except (TypeError, ValueError):
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

    return html.Div(
        [
            _box("PM2.5"),
            _box("PM10"),
            _box("O3"),
        ],
        className="overview-pollutant-box-grid",
    )

    monitor_store = monitor_store or {}
    pm25_rows_all = monitor_store.get("aqmsPm25Rows") or []
    pm10_rows_all = monitor_store.get("aqmsPm10Rows") or []
    o3_rows_all = monitor_store.get("aqmsO3Rows") or []
    combined_rows = list(pm25_rows_all) + list(pm10_rows_all) + list(o3_rows_all)
    if not combined_rows:
        return html.Div("No data", className="card-hint")

    latest = _monitoring_time_row(combined_rows)
    if not latest:
        return html.Div("No data", className="card-hint")

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
        return html.Div("No stations")

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

    return card


@app.callback(
    Output("overview-met-6h", "children"),
    [
        Input("dashboard-tabs", "value"),
        Input("overview-met-load", "n_intervals"),
        Input("overview-nsw-region-index", "data"),
    ],
    [State("monitor-store", "data")],
    prevent_initial_call=True,
)
def render_overview_met6h(active_tab, _met_load, region_index, monitor_store):
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
        date_label = ""
        time_label = ""
        if dt:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=SYDNEY_TZ)
            dt = dt.astimezone(SYDNEY_TZ)
            if i == 0 or (prev_date is not None and dt.date() != prev_date):
                date_label = dt.strftime("%d %b")
            time_label = dt.strftime("%I %p").lstrip("0")
            prev_date = dt.date()
        else:
            time_label = "--"

        # Last label should be 'Now'
        if i == len(display_times) - 1:
            time_label = "Now"
            date_label = ""
        else:
            if (i % 4 != 0) and (not date_label):
                time_label = ""
        formatted_labels_bottom.append({"time": time_label, "date": date_label})

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

        rows.append(
            html.Div(
                [
                    html.Div(
                        ICONS.get(label, ""),
                        className="overview-met6h__icon",
                    ),
                    html.Div(
                        label,
                        className="overview-met6h__label",
                    ),
                    html.Div(
                        bars,
                        className="overview-met6h__bars",
                    ),
                ],
                className="overview-met6h__row",
            )
        )

    axis = html.Div(
        [
            html.Div("", className="overview-met6h__axis-spacer"),
            html.Div("", className="overview-met6h__axis-spacer"),
            html.Div(
                [
                    html.Span(
                        [
                            html.Span(label.get("time", ""), className="overview-met6h__axis-time"),
                            html.Span(label.get("date", ""), className="overview-met6h__axis-date") if label.get("date") else None,
                        ],
                        className="overview-met6h__axis-label",
                    )
                    for label in formatted_labels_bottom
                ],
                className="overview-met6h__axis-track",
            ),
        ],
        className="overview-met6h__axis",
    )

    return html.Div(rows + [axis], className="overview-met6h__body")


@app.callback(
    Output("overview-forecast-trends", "children"),
    [
        Input("dashboard-tabs", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
        Input("overview-trends-index", "data"),
    ],
)
def render_overview_trends(active_tab, time_scope, model_name, run_date, _overview_load, selected_index):
    if active_tab != "overview":
        return no_update

    selection = {
        "regions": DEFAULT_SELECTION.get("regions"),
        "pollutants": DEFAULT_SELECTION.get("pollutants"),
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model_name or DEFAULT_SELECTION.get("models"),
        "date": run_date or DEFAULT_SELECTION.get("date"),
    }

    selection_for_panel, regions = _overview_pick_multi_region_forecast_selection(selection)

    if not regions:
        return html.Div("No forecast regions found for the current run selection.", className="placeholder__body")

    def _run_label_for_region(region):
        """Best-effort run timestamp label (e.g. 'May 21 8PM') from file mtime."""
        for pollutant in ("PM2.5", "PM10", "O3"):
            file_path = forecast_file_path(
                [
                    str(region),
                    pollutant,
                    str(selection_for_panel.get("timeScopes")),
                    str(selection_for_panel.get("models")),
                    str(selection_for_panel.get("date")),
                ]
            )
            if not file_path:
                continue
            try:
                epoch = os.path.getmtime(file_path)
            except OSError:
                epoch = None
            if epoch is None:
                continue
            try:
                dt = datetime.fromtimestamp(float(epoch), tz=SYDNEY_TZ)
                hour = dt.strftime("%I").lstrip("0") or "0"
                return f"{dt.strftime('%b')} {dt.day} {hour}{dt.strftime('%p')}"
            except (TypeError, ValueError, OSError):
                return ""
        return ""

    def _sort_key(value):
        norm = _overview_normalize_forecast_region(value)
        if str(norm).lower() in {"sydney_east", "sydney east"}:
            return (0, str(norm))
        return (1, str(norm))

    regions_sorted = sorted(regions, key=_sort_key)

    # Determine index (wrap within bounds)
    try:
        idx = int(selected_index or 0)
    except Exception:
        idx = 0

    if not regions_sorted:
        return html.Div("No forecast regions found for the current run selection.", className="placeholder__body")

    idx = max(0, min(idx, len(regions_sorted) - 1))
    region = regions_sorted[idx]
    fig = _overview_build_forecast_trends_figure(region, selection_for_panel)
    card = html.Div(
        [
            html.Div(
                className="overview-region-card__body",
                children=dcc.Graph(figure=fig, config={"displayModeBar": False}, className="overview-region-card__graph"),
            ),
        ],
        className="overview-region-card overview-region-card--collapsible",
    )

    return [card]


def _overview_forecast_region_options(time_scope, model_name, run_date):
    selection = {
        "regions": DEFAULT_SELECTION.get("regions"),
        "pollutants": DEFAULT_SELECTION.get("pollutants"),
        "timeScopes": time_scope or DEFAULT_SELECTION.get("timeScopes"),
        "models": model_name or DEFAULT_SELECTION.get("models"),
        "date": run_date or DEFAULT_SELECTION.get("date"),
    }
    _sel, regions = _overview_pick_multi_region_forecast_selection(selection)
    if not regions:
        return []

    def _sort_key(value):
        norm = _overview_normalize_forecast_region(value)
        if str(norm).lower() in {"sydney_east", "sydney east"}:
            return (0, str(norm))
        return (1, str(norm))

    return [
        {
            "value": str(region),
            "label": title_case_station_name(str(region).replace("_", " ")),
        }
        for region in sorted(regions, key=_sort_key)
    ]


@app.callback(
    Output("overview-trends-region-options", "data"),
    [
        Input("dashboard-tabs", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
    ],
)
def sync_overview_trends_region_options(active_tab, time_scope, model_name, run_date, _overview_load):
    if active_tab != "overview":
        return no_update
    return _overview_forecast_region_options(time_scope, model_name, run_date)


app.clientside_callback(
    """
    function(prevClicks, nextClicks, options, currentIndex) {
        const ctx = dash_clientside.callback_context;
        const opts = Array.isArray(options) && options.length ? options : [{label: ""}];
        const count = opts.length || 1;
        let idx = Number.isFinite(Number(currentIndex)) ? Number(currentIndex) : 0;
        if (!ctx.triggered.length) {
            return Math.max(0, Math.min(idx, count - 1));
        }
        const prop = ctx.triggered[0].prop_id || "";
        if (prop.indexOf("overview-trends-region-options") === 0) {
            return 0;
        }
        if (prop.indexOf("overview-trends-prev") === 0) {
            idx = (idx - 1 + count) % count;
        } else if (prop.indexOf("overview-trends-next") === 0) {
            idx = (idx + 1) % count;
        }
        return idx;
    }
    """,
    Output("overview-trends-index", "data"),
    [
        Input("overview-trends-prev", "n_clicks"),
        Input("overview-trends-next", "n_clicks"),
        Input("overview-trends-region-options", "data"),
    ],
    [State("overview-trends-index", "data")],
)


app.clientside_callback(
    """
    function(indexValue, options) {
        const opts = Array.isArray(options) && options.length ? options : [{label: ""}];
        const idx = Math.max(0, Math.min(Number(indexValue) || 0, opts.length - 1));
        return opts[idx].label || "";
    }
    """,
    Output("overview-trends-current-region-label", "children"),
    [Input("overview-trends-index", "data")],
    [State("overview-trends-region-options", "data")],
)


@app.callback(Output("header-monitor-updated", "children"), [Input("monitor-store", "data")])
def render_header_monitor_timestamps(monitor_store):
    return _monitor_header_timestamp_label(monitor_store)


@app.callback(
    Output("header-forecast-time", "children"),
    [
        Input("dashboard-tabs", "value"),
        Input("region-dropdown", "value"),
        Input("pollutant-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("overview-forecast-load", "n_intervals"),
    ],
)
def render_header_forecast_time(_active_tab, region, pollutant, time_scope, model, date, _overview_load):
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


def _haversine_meters(lat1, lon1, lat2, lon2):
    try:
        lat1 = float(lat1)
        lon1 = float(lon1)
        lat2 = float(lat2)
        lon2 = float(lon2)
    except (TypeError, ValueError):
        return None
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return 6371000.0 * c


def _nearest_aqms_site(lat, lon):
    best_site = None
    best_distance = None
    for site in (_sites_by_id() or {}).values():
        site_lat = site.get("Latitude")
        site_lon = site.get("Longitude")
        if site_lat is None or site_lon is None:
            continue
        distance = _haversine_meters(lat, lon, site_lat, site_lon)
        if distance is None:
            continue
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best_site = site
    return best_site, best_distance


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
        fig = _monitor_trend_figure(pollutant_label)
        return "", fig, "", fig

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
            raw_history = _monitor_history_rows_for_sites(
                (site_id,),
                tuple(mini_codes),
                start_date,
                end_date,
                live_timeout=6,
                fallback_hours=window_hours + 24,
            )
            aqms_histories = {}
            for item in raw_history or []:
                param = _aqms_parameter(item).get("ParameterCode")
                if not param:
                    continue
                aqms_histories.setdefault(param, []).append(item)
            aqms_history = aqms_histories.get(parameter_code) or []
        except Exception:
            aqms_history = []

        nearest_pa = nearest_purpleair_sensor(selected_row.get("lat"), selected_row.get("lon"), monitor_store.get("purpleairSensors") or [])
        if nearest_pa and pollutant_label in {"PM2.5", "PM10"}:
            end_ts = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
            start_ts = end_ts - window_hours * 3600
            fields = ["pm2.5_alt"] if pollutant_label == "PM2.5" else ["pm10.0_atm"]
            pa_history = _fetch_purpleair_sensor_history_cached(nearest_pa.get("site_id"), start_ts, end_ts, 60, tuple(fields), 5)

    if source == "PurpleAir":
        end_ts = int(now.replace(minute=0, second=0, microsecond=0).timestamp())
        start_ts = end_ts - window_hours * 3600
        fields = ["pm2.5_alt"] if pollutant_label == "PM2.5" else ["pm10.0_atm"]
        pa_history = _fetch_purpleair_sensor_history_cached(int(selected_row.get("site_id")), start_ts, end_ts, 60, tuple(fields), 5)

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

    return "", compare_fig, compare_summary, trend_fig


def _prewarm_monitor_selected_render():
    if str(os.environ.get("MONITOR_SELECTED_RENDER_PREWARM", "1")).strip().lower() in {"0", "false", "no", "n"}:
        return
    try:
        store = INITIAL_MONITOR_STORE if isinstance(INITIAL_MONITOR_STORE, dict) else {}
        selection = _default_monitor_selection(store)
        if selection:
            render_monitor_selected(selection, store, "24h", "PM2.5")
    except Exception:
        return


_prewarm_monitor_selected_render()


@app.callback(
    [Output("monitor-region-series-station", "options"), Output("monitor-region-series-station", "value")],
    [Input("monitor-region-series", "value"), Input("selected-monitor-site-store", "data")],
    [State("monitor-region-series-station", "value")],
)
def sync_monitor_region_series_station(region_value, selected_monitor_site, current_station_value):
    options = _monitor_region_series_station_options(region_value)
    if not options:
        return [], None

    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    option_values = {option.get("value") for option in options}

    if trigger == "monitor-region-series":
        return options, options[0]["value"]

    if current_station_value in option_values:
        return options, current_station_value

    selected_value = None
    try:
        if isinstance(selected_monitor_site, dict) and str(selected_monitor_site.get("source") or "").strip() == "AQMS":
            selected_candidate = f"aqms:{int(selected_monitor_site.get('id'))}"
            if selected_candidate in option_values:
                selected_value = selected_candidate
    except Exception:
        selected_value = None

    if selected_value is None:
        selected_value = options[0]["value"]

    return options, selected_value


@app.callback(
    [
        Output("monitor-region-series-note", "children"),
        Output("monitor-region-series-plots", "children"),
    ],
    [
        Input("dashboard-tabs", "value"),
        Input("monitor-region-series", "value"),
        Input("monitor-region-series-station", "value"),
    ],
)
def render_monitor_region_series(active_tab, region_value, station_value):
    if active_tab != "monitor":
        return no_update, no_update

    region_value = str(region_value or "ALL").strip()
    if not region_value or region_value.upper() == "ALL":
        region_value = str(MONITOR_REGION_SERIES_DEFAULT_VALUE or "").strip()

    region_label = next((option.get("label") for option in MONITOR_REGION_SERIES_OPTIONS if str(option.get("value") or "").strip() == region_value), title_case_station_name(region_value) or "Selected region")
    station_options = _monitor_region_series_station_options(region_value)
    if not station_options:
        note = html.Div("No AQMS stations found for the selected region.", className="monitor-region-series-empty")
        return note, []

    station_value = str(station_value or "").strip()
    if station_value not in {option["value"] for option in station_options}:
        station_value = station_options[0]["value"]

    try:
        selected_site_id = int(str(station_value).split(":", 1)[1])
    except Exception:
        selected_site_id = None
    if selected_site_id is None:
        note = html.Div("No AQMS station selected.", className="monitor-region-series-empty")
        return note, []

    site_lookup = _sites_by_id()
    station = site_lookup.get(selected_site_id) or {}
    station_name = title_case_station_name(station.get("SiteName") or station.get("StationName") or f"Site {selected_site_id}")

    now = datetime.now(SYDNEY_TZ).replace(minute=0, second=0, microsecond=0)
    start_dt = now - timedelta(hours=24)
    start_iso = start_dt.strftime("%Y-%m-%dT%H:00:00")
    end_iso = now.strftime("%Y-%m-%dT%H:00:00")
    history_rows = _fetch_monitor_region_history((selected_site_id,), start_iso, end_iso) or []
    if history_rows:
        has_current_window_points = False
        for pollutant_label in MONITOR_REGION_SERIES_POLLUTANTS:
            if _monitor_region_series_values(history_rows, pollutant_label, start_dt, now):
                has_current_window_points = True
                break
        if not has_current_window_points:
            latest_ts = _monitor_region_series_latest_timestamp(history_rows)
            if latest_ts is not None:
                now = latest_ts
                start_dt = now - timedelta(hours=24)

    plot_cards = []
    for pollutant_label in MONITOR_REGION_SERIES_POLLUTANTS:
        fig = _monitor_region_series_figure(
            history_rows,
            pollutant_label,
            start_dt,
            now,
            region_label,
            station_name,
        )
        plot_cards.append(
            html.Div(
                dcc.Graph(
                    figure=fig,
                    config={"displayModeBar": False, "responsive": True},
                    className="monitor-region-series-graph",
                ),
                className=f"monitor-region-series-plot-card monitor-region-series-plot-card--{pollutant_label.lower().replace('.', '').replace(' ', '')}",
            )
        )

    note = html.Div(
        [
            html.Span(f"{region_label}", className="monitor-region-series-note__region"),
            html.Span(f" • {station_name} • PM2.5 / PM10 / O3 • last 24 hours", className="monitor-region-series-note__meta"),
        ],
        className="monitor-region-series-note__text",
    )
    return note, plot_cards


def _prewarm_monitor_region_series_render():
    if str(os.environ.get("MONITOR_REGION_SERIES_RENDER_PREWARM", "1")).strip().lower() in {"0", "false", "no", "n"}:
        return
    try:
        render_monitor_region_series("monitor", MONITOR_REGION_SERIES_DEFAULT_VALUE, MONITOR_REGION_SERIES_DEFAULT_STATION_VALUE)
    except Exception:
        return


_prewarm_monitor_region_series_render()


@app.callback(
    Output("forecast-map", "srcDoc"),
    [Input("forecast-store", "data")],
    [State("pollutant-dropdown", "value")],
)
def render_forecast_map_srcdoc(parsed, pollutant):
    global _LAST_GOOD_FORECAST_MAP_HTML
    if not parsed:
        if _LAST_GOOD_FORECAST_MAP_HTML:
            return _LAST_GOOD_FORECAST_MAP_HTML
        return no_update
    # Keep the same iframe document while the slider moves; values update via postMessage.
    next_html = _leaflet_forecast_map_html(parsed, pollutant, 0)
    _LAST_GOOD_FORECAST_MAP_HTML = next_html
    return next_html


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
    Output("forecast-all-stations-modal", "style"),
    [Input("forecast-view-all", "n_clicks"), Input("forecast-modal-close", "n_clicks")],
    [State("forecast-all-stations-modal", "style")],
)
def toggle_forecast_all_stations_modal(open_clicks, close_clicks, current_style):
    current_style = current_style or {}
    is_open = current_style.get("display") not in (None, "", "none")
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    if trigger == "forecast-view-all" and open_clicks:
        return {"display": "flex"}
    if trigger == "forecast-modal-close" and close_clicks:
        return {"display": "none"}
    return {"display": "flex"} if is_open else {"display": "none"}


@app.callback(
    [
        Output("forecast-all-stations-table", "columns"),
        Output("forecast-all-stations-table", "data"),
        Output("forecast-all-stations-title", "children"),
        Output("forecast-all-stations-subtitle", "children"),
    ],
    [
        Input("region-dropdown", "value"),
        Input("pollutant-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("ranking-hour-dropdown", "value"),
        Input("forecast-view-all", "n_clicks"),
    ],
    [State("forecast-all-stations-modal", "style")],
)
def render_forecast_all_stations_table(region, pollutant, time_scope, model, date, ranking_hour_index, _open_clicks, modal_style):
    trigger = callback_context.triggered[0]["prop_id"].split(".")[0] if callback_context.triggered else ""
    modal_style = modal_style or {}
    modal_is_open = modal_style.get("display") not in (None, "", "none")
    if trigger != "forecast-view-all" and not modal_is_open:
        return no_update, no_update, no_update, no_update
    selection = {
        "regions": region,
        "pollutants": "PM2.5",
        "timeScopes": time_scope,
        "models": model,
        "date": date,
    }
    return _forecast_overview_allstations_table(selection)


@app.callback(
    [Output("ranking-hour-dropdown", "options"), Output("ranking-hour-dropdown", "value")],
    [Input("forecast-store", "data")],
    [State("ranking-hour-dropdown", "value")],
)
def sync_ranking_hour_dropdown(parsed, current_value):
    options = _forecast_hour_options(parsed)
    valid_values = {option["value"] for option in options}
    value = current_value if current_value in valid_values else 0
    return options, value


@app.callback(
    [Output("ranking-cards", "children"), Output("forecast-rank-selected-time", "children")],
    [
        Input("region-dropdown", "value"),
        Input("pollutant-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
        Input("ranking-hour-dropdown", "value"),
    ],
)
def render_station_ranking(region, pollutant, time_scope, model, date, ranking_hour_index):
    selection = {
        "regions": region,
        "pollutants": pollutant,
        "timeScopes": time_scope,
        "models": model,
        "date": date,
    }
    cards = _station_prediction_cards(selection, ranking_hour_index)
    parsed, _message = _load_forecast(selection)
    selected_time = _selected_time_label(parsed, ranking_hour_index) if parsed else None
    return cards, _format_forecast_label(selected_time)


@app.callback(
    [
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
        empty = go.Figure()
        empty.update_layout(
            template="plotly_white",
            height=620,
            margin=dict(l=20, r=20, t=30, b=20),
            font=dict(family=BASE_FONT_FAMILY, color="#0f172a"),
        )
        return _make_summary_cards({}, selection, 0), "Latest forecast run not loaded.", ""

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
    return summary, f"Showing {time_label}", file_text


@app.callback(
    [Output("validation-pollutant-dropdown", "options"), Output("validation-pollutant-dropdown", "value")],
    [Input("validation-date-dropdown", "value")],
    [State("validation-pollutant-dropdown", "value")],
)
def sync_validation_pollutants(date, current_pollutant):
    options = _validation_pollutant_options(date)
    return options, _validation_pick_value(options, current_pollutant)


@app.callback(
    [Output("validation-horizon-dropdown", "options"), Output("validation-horizon-dropdown", "value")],
    [Input("validation-date-dropdown", "value"), Input("validation-pollutant-dropdown", "value")],
    [State("validation-horizon-dropdown", "value")],
)
def sync_validation_horizons(date, pollutant, current_horizon):
    options = _validation_horizon_options(date, pollutant)
    return options, _validation_pick_value(options, current_horizon)


@app.callback(
    [Output("validation-region-dropdown", "options"), Output("validation-region-dropdown", "value")],
    [
        Input("validation-date-dropdown", "value"),
        Input("validation-pollutant-dropdown", "value"),
        Input("validation-horizon-dropdown", "value"),
    ],
    [State("validation-region-dropdown", "value")],
)
def sync_validation_regions(date, pollutant, horizon, current_region):
    options = _validation_region_options(date, pollutant, horizon)
    return options, _validation_pick_value(options, current_region)


@app.callback(
    [
        Output("validation-store", "data"),
        Output("validation-message", "children"),
    ],
    [
        Input("dashboard-tabs", "value"),
        Input("validation-region-dropdown", "value"),
        Input("validation-pollutant-dropdown", "value"),
        Input("validation-horizon-dropdown", "value"),
        Input("validation-date-dropdown", "value"),
    ],
)
def load_validation_forecast(active_tab, region, pollutant, horizon, date):
    if not all([region, pollutant, horizon, date]):
        return None, "Choose a date, pollutant, horizon, and region to load validation data."
    model = _validation_model_for_selection(date, pollutant, horizon, region)
    selection = {
        "regions": region,
        "pollutants": pollutant,
        "timeScopes": horizon,
        "models": model,
        "date": date,
    }
    parsed, message = _load_forecast(selection)
    if not parsed:
        return None, message

    parsed["_selection"] = selection
    parsed["_message"] = message
    label = f"{message} · {len(((parsed.get('data') or {}).get('stations') or {}))} stations"
    return parsed, label


@app.callback(
    [Output("validation-time-series-grid", "children"), Output("validation-metrics", "children")],
    [
        Input("validation-store", "data"),
    ],
    [
        State("validation-region-dropdown", "value"),
        State("validation-pollutant-dropdown", "value"),
    ],
)
def render_validation_outputs(parsed, region, pollutant):
    if not parsed:
        return html.Div("No validation forecast file is available for this selection.", className="validation-empty"), _validation_metric_cards({}, pollutant, 0, region)

    return (
        _validation_station_plot_grid(parsed, pollutant, region),
        _validation_metric_cards(parsed, pollutant, 0, region),
    )


@app.callback(
    [Output("station-trend", "figure"), Output("station-details", "children")],
    [
        Input("region-dropdown", "value"),
        Input("pollutant-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
    ],
)
def render_station_trend(region, pollutant, time_scope, model, date):
    selection = {
        "regions": region,
        "pollutants": pollutant,
        "timeScopes": time_scope,
        "models": model,
        "date": date,
    }
    parsed, _message = _load_forecast(selection)
    if not parsed:
        figure = go.Figure()
        figure.update_layout(template="plotly_white", height=320, margin=dict(l=20, r=20, t=30, b=20))
        return figure, "Choose a region forecast to inspect its 12 hour profile."

    trend_figure = _region_forecast_figure(parsed, selection, pollutant)

    region_code = str(selection.get("regions") or "")
    region_name = load_region_lookup().get(region_code) or title_case_station_name(region_code) or "Selected region"
    station_count = len(((parsed.get("data") or {}).get("stations") or {}))
    details = f"{region_name} @ {selection.get('timeScopes') or '12'} hr forecast · {station_count} station forecasts in the selected region."
    return trend_figure, details


@app.callback(
    [Output("time-series-region-dropdown", "options"), Output("time-series-region-dropdown", "value")],
    [Input("forecast-store", "data")],
    [State("region-dropdown", "value"), State("time-series-region-dropdown", "value")],
)
def sync_time_series_region_options(parsed, selected_region, current_region):
    options = _forecast_region_options(parsed, selected_region)
    valid_values = {option["value"] for option in options}
    if selected_region in valid_values:
        value = selected_region
    elif current_region in valid_values:
        value = current_region
    else:
        value = options[0]["value"] if options else None
    return options, value


@app.callback(
    [Output("time-series-station-dropdown", "options"), Output("time-series-station-dropdown", "value")],
    [Input("forecast-store", "data"), Input("time-series-region-dropdown", "value")],
    [State("time-series-station-dropdown", "value")],
)
def sync_time_series_station_options(parsed, region_value, current_station):
    options = _forecast_station_options(parsed, region_value)
    valid_values = {option["value"] for option in options}
    value = current_station if current_station in valid_values else (options[0]["value"] if options else None)
    return options, value


@app.callback(
    Output("forecast-time-series", "figure"),
    [
        Input("forecast-store", "data"),
        Input("time-slider", "value"),
        Input("time-series-region-dropdown", "value"),
        Input("time-series-station-dropdown", "value"),
    ],
    [State("pollutant-dropdown", "value")],
)
def render_forecast_time_series(parsed, hour_index, region_value, station_name, pollutant):
    if not parsed:
        figure = go.Figure()
        figure.update_layout(template="plotly_white", height=380, margin=dict(l=20, r=20, t=40, b=20))
        return figure

    return _forecast_time_series_figure(parsed, pollutant, station_name, hour_index or 0, region_value)


@app.callback(
    Output("selected-station-store", "data"),
    [
        Input("url", "hash"),
        Input("ranking-hour-dropdown", "value"),
        Input("region-dropdown", "value"),
        Input("pollutant-dropdown", "value"),
        Input("time-dropdown", "value"),
        Input("model-dropdown", "value"),
        Input("date-dropdown", "value"),
    ],
)
def update_selected_station(url_hash, ranking_hour_index, region, pollutant, time_scope, model, date):
    selection = {
        "regions": region,
        "pollutants": pollutant,
        "timeScopes": time_scope,
        "models": model,
        "date": date,
    }
    parsed, _message = _load_forecast(selection)
    if parsed:
        selected_station = _station_from_hash(parsed, url_hash)
        if selected_station:
            return selected_station

    return _default_ranking_station(selection, ranking_hour_index)


def _warm_monitor_region_series_cache():
    if str(os.environ.get("DISABLE_MONITOR_REGION_SERIES_WARMUP", "")).strip().lower() in {"1", "true", "yes", "y"}:
        return

    def worker():
        try:
            station_value = str(MONITOR_REGION_SERIES_DEFAULT_STATION_VALUE or "")
            if ":" not in station_value:
                return
            selected_site_id = int(station_value.split(":", 1)[1])
            now = datetime.now(SYDNEY_TZ).replace(minute=0, second=0, microsecond=0)
            start_dt = now - timedelta(hours=24)
            start_iso = start_dt.strftime("%Y-%m-%dT%H:00:00")
            end_iso = now.strftime("%Y-%m-%dT%H:00:00")
            _fetch_monitor_region_history((selected_site_id,), start_iso, end_iso)
        except Exception:
            pass

    threading.Thread(target=worker, name="monitor-region-series-cache-warmup", daemon=True).start()


_warm_monitor_region_series_cache()


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", "8050")))
