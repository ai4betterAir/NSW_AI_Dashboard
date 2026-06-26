"""Helpers for latest air-quality observations used by monitoring views.

The Dashboard should never present an old bundled/cache snapshot as if it is the
current AQMS observation. This module normalises NSW AQMS date formats and only
falls back to the cache for the live snapshot when the cache is still recent.
"""

import ast
import csv
import json
import math
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nowcasting.config.paths import DASHBOARD_DATA_DIR, PURPLEAIR_SENSORS_JS
from nowcasting.data.dashboard_data import (
    category_for_value,
    load_sites,
    pollutant_by_label,
    title_case_station_name,
)


OBSERVATION_URL = "https://data.airquality.nsw.gov.au/api/Data/get_Observations"
SITE_DETAILS_URL = "https://data.airquality.nsw.gov.au/api/Data/get_SiteDetails"
PARAMETER_DETAILS_URL = "https://data.airquality.nsw.gov.au/api/Data/get_ParameterDetails"
PURPLEAIR_SENSORS_PATH = PURPLEAIR_SENSORS_JS
_CACHE_DIR = Path(DASHBOARD_DATA_DIR) / "monitoring_cache"
_OBS_CACHE_CSV = _CACHE_DIR / "observations.csv"
_PURPLEAIR_SNAPSHOT_JSON = _CACHE_DIR / "purpleair_snapshot.json"

SYDNEY_TZ = timezone(timedelta(hours=10))
PURPLEAIR_API_KEY = os.environ.get("PURPLEAIR_API_KEY", "D80F3AFD-DDAD-11ED-BD21-42010A800008")
PURPLEAIR_SNAPSHOT_URL = "https://api.purpleair.com/v1/sensors"
PURPLEAIR_HISTORY_URL = "https://api.purpleair.com/v1/sensors/{sensor_index}/history"
OBS_CACHE_MAX_AGE_HOURS = int(os.environ.get("DASHBOARD_OBS_CACHE_MAX_AGE_HOURS", "6"))

MONITORING_PARAMETER = {
    "ParameterCode": "AQC",
    "ParameterDescription": "AQC",
    "Units": "category",
    "UnitsDescription": "category",
    "Category": "Site AQC",
    "SubCategory": "Hourly",
    "Frequency": "Hourly average",
}
MONITORING_CATEGORY_ORDER = ["EXTREMELY POOR", "VERY POOR", "POOR", "FAIR", "GOOD"]
MONITORING_CATEGORY_COLORS = {
    "GOOD": "#16a34a",
    "FAIR": "#facc15",
    "POOR": "#f97316",
    "VERY POOR": "#ef4444",
    "EXTREMELY POOR": "#7f1d1d",
    "NO DATA": "#9ca3af",
}
PURPLEAIR_COLOR = "#7e22ce"


def _ensure_cache_dir():
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _coerce_parameter_dict(item):
    if not isinstance(item, dict):
        return {}
    param = item.get("Parameter") or {}
    if isinstance(param, str):
        text = param.strip()
        if text:
            try:
                param = json.loads(text)
            except Exception:
                try:
                    param = ast.literal_eval(text)
                except Exception:
                    param = {}
    return param if isinstance(param, dict) else {}


def _normalise_date_value(value):
    """Return YYYY-MM-DD for common NSW API/cache date formats."""
    text = str(value or "").strip()
    if not text:
        return ""

    # Common API forms: 2026-06-26, 2026-06-26T00:00:00, 26/06/2026.
    candidates = [
        text,
        text.split("T", 1)[0],
        text.split(" ", 1)[0],
    ]
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                continue

    # Last-resort ISO parser without adding heavy dependencies.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return text


def _parse_hour(value):
    try:
        hour = int(float(str(value).strip()))
    except Exception:
        return -1
    return hour


def _observation_datetime(item):
    if not isinstance(item, dict):
        return None
    date_text = _normalise_date_value(item.get("Date") or item.get("date"))
    try:
        base = datetime.strptime(date_text, "%Y-%m-%d")
    except Exception:
        return None
    hour = _parse_hour(item.get("Hour") or item.get("hour"))
    if hour < 0:
        return base.replace(tzinfo=SYDNEY_TZ)
    if hour >= 24:
        base = base + timedelta(days=1)
        hour = 0
    return base.replace(hour=hour, tzinfo=SYDNEY_TZ)


def _normalise_observation_row(row):
    if not isinstance(row, dict):
        return row
    row = dict(row)
    date_value = _normalise_date_value(row.get("Date") or row.get("date"))
    if date_value:
        row["Date"] = date_value
    if "Hour" in row:
        try:
            row["Hour"] = str(int(float(row.get("Hour"))))
        except Exception:
            pass

    param = _coerce_parameter_dict(row)
    if param:
        row["Parameter"] = param
        code = param.get("ParameterCode") or param.get("Code")
        desc = param.get("ParameterDescription") or param.get("Description")
        units = param.get("Units")
        if code is not None:
            row["ParameterCode"] = str(code)
        if desc is not None:
            row["ParameterDescription"] = str(desc)
        if units is not None:
            row["Units"] = str(units)
    elif row.get("ParameterCode"):
        row["Parameter"] = {
            "ParameterCode": row.get("ParameterCode"),
            "ParameterDescription": row.get("ParameterDescription") or row.get("ParameterCode"),
            "Units": row.get("Units") or "",
            "Frequency": row.get("Frequency") or "Hourly average",
        }
    return row


def _normalise_observation_rows(rows):
    if not isinstance(rows, list):
        return []
    return [_normalise_observation_row(row) for row in rows if isinstance(row, dict)]


def _latest_observation_datetime(rows):
    latest = None
    for row in rows or []:
        dt = _observation_datetime(row)
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return latest


def _rows_are_recent(rows, max_age_hours=OBS_CACHE_MAX_AGE_HOURS):
    latest = _latest_observation_datetime(rows)
    if latest is None:
        return False
    now = datetime.now(SYDNEY_TZ)
    age = now - latest
    # Allow a small future tolerance in case the API labels the ending hour.
    return timedelta(hours=-1) <= age <= timedelta(hours=max_age_hours)


def _save_observations_cache(rows):
    """Save observation rows to CSV cache, preserving different parameters."""
    rows = _normalise_observation_rows(rows)
    if not rows:
        return
    _ensure_cache_dir()

    try:
        existing = _load_observations_cache(ignore_staleness=True) or []
    except Exception:
        existing = []

    def make_key(item):
        sid = item.get("Site_Id") or item.get("site_id") or item.get("SiteId") or item.get("SiteID")
        date = item.get("Date") or item.get("date")
        hour = item.get("Hour") or item.get("hour")
        param = _coerce_parameter_dict(item)
        param_code = param.get("ParameterCode") or item.get("ParameterCode") or ""
        return f"{sid}::{date}::{hour}::{param_code}"

    merged_map = {}
    for item in existing + rows:
        if not isinstance(item, dict):
            continue
        item = _normalise_observation_row(item)
        merged_map[make_key(item)] = item
    merged = list(merged_map.values())

    # Keep the cache bounded so an old huge CSV cannot dominate startup.
    latest = _latest_observation_datetime(merged)
    if latest is not None:
        cutoff = latest - timedelta(days=4)
        merged = [r for r in merged if (_observation_datetime(r) or latest) >= cutoff]

    keys = sorted({k for row in merged for k in row.keys()})
    try:
        with _OBS_CACHE_CSV.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            for row in merged:
                out = {}
                for key in keys:
                    value = row.get(key)
                    if isinstance(value, (dict, list)):
                        out[key] = json.dumps(value, ensure_ascii=False)
                    else:
                        out[key] = "" if value is None else str(value)
                writer.writerow(out)
    except Exception:
        return


def _load_observations_cache(ignore_staleness=False):
    """Load cached observations; by default only return them if still recent."""
    if not _OBS_CACHE_CSV.exists():
        return []
    rows = []
    try:
        with _OBS_CACHE_CSV.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                parsed = {}
                for key, value in row.items():
                    if value is None:
                        parsed[key] = None
                        continue
                    text = str(value).strip()
                    if (text.startswith("[") and text.endswith("]")) or (text.startswith("{") and text.endswith("}")):
                        try:
                            parsed[key] = json.loads(text)
                            continue
                        except Exception:
                            try:
                                parsed[key] = ast.literal_eval(text)
                                continue
                            except Exception:
                                pass
                    parsed[key] = text
                rows.append(_normalise_observation_row(parsed))
    except Exception:
        return []

    if ignore_staleness or _rows_are_recent(rows):
        return rows
    return []


def _save_purpleair_snapshot_cache(payload):
    _ensure_cache_dir()
    try:
        with _PURPLEAIR_SNAPSHOT_JSON.open("w", encoding="utf-8") as fh:
            json.dump(payload or {}, fh)
    except Exception:
        return


def _load_purpleair_snapshot_cache():
    if not _PURPLEAIR_SNAPSHOT_JSON.exists():
        return {"sensors": [], "fetched_at": None, "error": "No cache"}
    try:
        with _PURPLEAIR_SNAPSHOT_JSON.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"sensors": [], "fetched_at": None, "error": "Corrupt cache"}


def _bundled_monitoring_dirs():
    module_root = Path(__file__).resolve().parents[4]
    server_root = Path(__file__).resolve().parents[3]
    return [
        server_root / "data" / "downloads" / "monitoring_test",
        module_root / "AI_Dashboard_2026Y" / "data" / "downloads" / "monitoring_test",
        module_root / "AI_Dashboard_2026" / "data" / "downloads" / "monitoring_test",
        module_root / "AI_DASH" / "data" / "downloads" / "monitoring_test",
    ]


def _load_bundled_observations_snapshot():
    """Load packaged AQMS observations only if they are recent enough."""
    seen = set()
    for directory in _bundled_monitoring_dirs():
        if directory in seen or not directory.exists():
            continue
        seen.add(directory)
        candidates = sorted(
            directory.glob("aqms_observations_snapshot*.json"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        for path in candidates:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue
            rows = payload.get("items") if isinstance(payload, dict) else payload
            rows = _normalise_observation_rows(rows)
            if rows and _rows_are_recent(rows):
                return rows
    return []


def _load_bundled_purpleair_snapshot():
    seen = set()
    for directory in _bundled_monitoring_dirs():
        if directory in seen or not directory.exists():
            continue
        seen.add(directory)
        candidates = sorted(
            directory.glob("purpleair_snapshot*.json"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        for path in candidates:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    payload = json.load(fh)
            except Exception:
                continue
            if isinstance(payload, dict) and payload.get("sensors") is not None:
                payload.setdefault("fetched_at", None)
                payload.setdefault("error", None)
                payload["source"] = "bundle"
                return payload
    return None


def _parse_observation_time(item):
    dt = _observation_datetime(item)
    hour = _parse_hour((item or {}).get("Hour") or (item or {}).get("hour"))
    if dt is None:
        return (datetime.min, hour)
    return (dt.replace(tzinfo=None), hour)


def fetch_observations(query=None, timeout=30):
    """Fetch NSW AQMS observations.

    For live snapshots (`query is None`), stale cache/bundled rows are not returned.
    For historical requests, failures return [] rather than unrelated cached rows.
    """
    data = b'""' if query is None else json.dumps(query).encode("utf-8")
    request = Request(
        OBSERVATION_URL,
        data=data,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except Exception:
        if query is not None:
            return []
        cached = _load_observations_cache()
        if cached:
            return cached
        bundled = _load_bundled_observations_snapshot()
        if bundled:
            try:
                _save_observations_cache(bundled)
            except Exception:
                pass
            return bundled
        return []

    rows = _normalise_observation_rows(raw)
    if not rows:
        return []
    try:
        _save_observations_cache(rows)
    except Exception:
        pass
    return rows


def fetch_observation_history(site_ids, parameter_codes, start_date, end_date, timeout=30):
    if not site_ids or not parameter_codes:
        return []
    query = {
        "Parameters": list(parameter_codes),
        "Sites": [int(site_id) for site_id in site_ids],
        "StartDate": str(start_date),
        "EndDate": str(end_date),
        "Categories": ["Averages"],
        "Subcategories": ["Hourly"],
        "Frequency": ["Hourly average"],
    }
    return fetch_observations(query=query, timeout=timeout)


@lru_cache(maxsize=1)
def get_site_lookup_by_id():
    lookup = {}
    for site in load_sites():
        site_id = site.get("Site_Id") or site.get("SiteId") or site.get("SiteID")
        if site_id is None:
            continue
        try:
            lookup[int(site_id)] = site
        except Exception:
            continue
    return lookup


def _category_sort_key(category):
    key = str(category or "").strip().upper()
    if key not in MONITORING_CATEGORY_ORDER:
        return len(MONITORING_CATEGORY_ORDER)
    return MONITORING_CATEGORY_ORDER.index(key)


def _format_category(category):
    key = str(category or "").strip().upper()
    if not key or key == "N/A":
        return "No data"
    return key.replace("_", " ").title()


def _monitoring_color(category):
    key = str(category or "").strip().upper()
    return MONITORING_CATEGORY_COLORS.get(key, MONITORING_CATEGORY_COLORS["NO DATA"])


def _extract_js_array(text, marker):
    start = text.find(marker)
    if start < 0:
        return []
    start = text.find("[", start)
    if start < 0:
        return []
    depth = 0
    in_string = False
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue
        if char in {"'", '"'}:
            in_string = True
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                try:
                    return ast.literal_eval(text[start : index + 1])
                except Exception:
                    return []
    return []


@lru_cache(maxsize=1)
def load_purpleair_sensors():
    try:
        text = PURPLEAIR_SENSORS_PATH.read_text(encoding="utf-8")
    except OSError:
        return []
    rows = []
    for item in _extract_js_array(text, "data:"):
        if len(item) < 4:
            continue
        sensor_id, name, lat, lon = item[:4]
        try:
            sensor_id = int(sensor_id)
        except Exception:
            continue
        rows.append(
            {
                "site_id": sensor_id,
                "station": str(name),
                "lat": lat,
                "lon": lon,
                "region": "PurpleAir",
                "date": "",
                "hour": "",
                "hour_description": "",
                "value": None,
                "category": "PurpleAir sensor",
                "category_key": "PURPLEAIR",
                "category_color": PURPLEAIR_COLOR,
                "determining_pollutant": "PM1.0 / PM2.5 / PM10",
                "source": "PurpleAir",
            }
        )
    return rows


def fetch_latest_monitoring_rows(timeout=30):
    raw = fetch_observations(query=None, timeout=timeout)
    if not isinstance(raw, list):
        return []

    latest_by_site = {}
    for item in raw:
        site_id = item.get("Site_Id") or item.get("SiteId") or item.get("SiteID")
        try:
            site_id_int = int(site_id)
        except Exception:
            continue
        category = item.get("AirQualityCategory")
        if category in (None, "", "N/A"):
            continue
        observation_time = _parse_observation_time(item)
        existing = latest_by_site.get(site_id_int)
        if existing is None or observation_time >= existing["_time"]:
            latest_by_site[site_id_int] = dict(item, _time=observation_time)

    rows = []
    site_lookup = get_site_lookup_by_id()
    for site_id, item in latest_by_site.items():
        site = site_lookup.get(int(site_id), {})
        category = item.get("AirQualityCategory")
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
                "value": item.get("Value"),
                "category": _format_category(category),
                "category_key": str(category or "").strip().upper(),
                "category_color": _monitoring_color(category),
                "determining_pollutant": item.get("DeterminingPollutant") or "",
                "source": "AQMS",
            }
        )

    rows.sort(key=lambda row: (_category_sort_key(row.get("category_key")), -_parse_hour(row.get("hour")), row.get("station") or ""))
    return rows


def fetch_monitoring_and_purpleair_rows(timeout=30):
    return fetch_latest_monitoring_rows(timeout=timeout) + load_purpleair_sensors()


def _haversine_km(lat1, lon1, lat2, lon2):
    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def fetch_purpleair_snapshot(bounds=None, timeout=30):
    if not PURPLEAIR_API_KEY:
        return {"sensors": [], "fetched_at": None, "error": "PurpleAir API key missing."}

    fields = [
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
        "last_seen",
    ]
    query = "fields=" + "%2C%20".join(fields)
    if bounds:
        query += f"&nwlng={bounds.get('west')}&nwlat={bounds.get('north')}&selng={bounds.get('east')}&selat={bounds.get('south')}"
    url = f"{PURPLEAIR_SNAPSHOT_URL}?{query}"

    request = Request(url, headers={"X-API-Key": PURPLEAIR_API_KEY, "accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        cached = _load_purpleair_snapshot_cache()
        cached_error = cached.get("error") if isinstance(cached, dict) else None
        if cached and cached_error != "No cache":
            if isinstance(cached, dict):
                cached.setdefault("source", "cache")
            return cached
        bundled = _load_bundled_purpleair_snapshot()
        if bundled:
            try:
                _save_purpleair_snapshot_cache(bundled)
            except Exception:
                pass
            return bundled
        return {"sensors": [], "fetched_at": None, "error": str(exc), "source": "error"}

    fields = payload.get("fields") or []
    data = payload.get("data") or []
    if not isinstance(fields, list) or not isinstance(data, list):
        bundled = _load_bundled_purpleair_snapshot()
        if bundled:
            try:
                _save_purpleair_snapshot_cache(bundled)
            except Exception:
                pass
            return bundled
        return {"sensors": [], "fetched_at": None, "error": "Unexpected PurpleAir payload.", "source": "error"}

    def idx(field):
        try:
            return fields.index(field)
        except ValueError:
            return -1

    id_idx = idx("sensor_index")
    if id_idx < 0:
        return {"sensors": [], "fetched_at": None, "error": "sensor_index missing from PurpleAir payload."}

    snapshot = []
    for row in data:
        if not isinstance(row, list) or len(row) <= id_idx:
            continue
        try:
            sensor_index = int(row[id_idx])
        except Exception:
            continue

        def get(field):
            j = idx(field)
            if j < 0 or j >= len(row):
                return None
            return row[j]

        snapshot.append(
            {
                "sensor_index": sensor_index,
                "name": get("name") or f"PurpleAir {sensor_index}",
                "location_type": get("location_type"),
                "lat": get("latitude"),
                "lon": get("longitude"),
                "rssi": get("rssi"),
                "pm1": get("pm1.0"),
                "pm25": get("pm2.5_alt"),
                "pm10": get("pm10.0"),
                "temperature": get("temperature"),
                "humidity": get("humidity"),
                "last_seen": get("last_seen"),
            }
        )

    out = {"sensors": snapshot, "fetched_at": payload.get("time_stamp") or payload.get("data_time_stamp"), "error": None, "source": "live"}
    try:
        _save_purpleair_snapshot_cache(out)
    except Exception:
        pass
    return out


def fetch_purpleair_sensor_history(sensor_index, start_timestamp, end_timestamp, average=60, fields=None, timeout=30):
    if not PURPLEAIR_API_KEY:
        return {"fields": [], "data": [], "error": "PurpleAir API key missing."}
    if not sensor_index:
        return {"fields": [], "data": [], "error": "sensor_index missing."}
    fields = fields or ["pm2.5_alt"]
    field_param = "%2C".join(fields)
    url = PURPLEAIR_HISTORY_URL.format(sensor_index=int(sensor_index)) + f"?start_timestamp={int(start_timestamp)}&end_timestamp={int(end_timestamp)}&average={int(average)}&fields={field_param}"
    request = Request(url, headers={"X-API-Key": PURPLEAIR_API_KEY, "accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        return {"fields": [], "data": [], "error": str(exc)}
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return {"fields": [], "data": [], "error": "Unexpected PurpleAir history payload."}
    return {"fields": payload.get("fields") or [], "data": payload.get("data") or [], "error": None}


def purpleair_clusters(snapshot, bin_degrees=0.05, pollutant_label="PM2.5"):
    sensors = snapshot or []
    bins = {}
    for sensor in sensors:
        lat = sensor.get("lat")
        lon = sensor.get("lon")
        try:
            lat = float(lat)
            lon = float(lon)
        except Exception:
            continue
        key = (round(lat / bin_degrees) * bin_degrees, round(lon / bin_degrees) * bin_degrees)
        bins.setdefault(key, []).append(sensor)

    def sensor_value(sensor):
        return sensor.get("pm10") if pollutant_label == "PM10" else sensor.get("pm25")

    clusters = []
    for cluster_index, ((lat_key, lon_key), members) in enumerate(sorted(bins.items(), key=lambda item: (-len(item[1]), item[0]))):
        values = []
        for member in members:
            try:
                value = float(sensor_value(member))
            except Exception:
                value = None
            if value is not None:
                values.append(value)
        mean_value = sum(values) / len(values) if values else None
        category_key, colour = category_for_value(pollutant_label, mean_value) if mean_value is not None else ("no-data", "#9ca3af")
        pollutant_meta = pollutant_by_label(pollutant_label) or {}
        category_label = "No data"
        for category in pollutant_meta.get("categories") or []:
            if category.get("color") == colour:
                category_label = category.get("label") or category_label
                break
        clusters.append(
            {
                "cluster_id": f"PA-{cluster_index + 1}",
                "lat": lat_key,
                "lon": lon_key,
                "count": len(members),
                "value": mean_value,
                "value_label": "--" if mean_value is None else f"{mean_value:.1f}",
                "category": category_label,
                "category_key": category_key,
                "category_color": colour,
                "members": members,
            }
        )
    return clusters


def nearest_purpleair_sensor(lat, lon, sensors, max_distance_km=35):
    best = None
    best_distance = None
    if lat is None or lon is None:
        return None
    try:
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return None
    for sensor in sensors or []:
        try:
            s_lat = float(sensor.get("lat"))
            s_lon = float(sensor.get("lon"))
        except Exception:
            continue
        distance = _haversine_km(lat, lon, s_lat, s_lon)
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = sensor
    if best_distance is None or best_distance > max_distance_km:
        return None
    payload = dict(best)
    payload["distance_km"] = best_distance
    return payload
