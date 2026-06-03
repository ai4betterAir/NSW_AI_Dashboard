"""Helpers for latest air-quality observations used by monitoring views."""

import ast
import json
import math
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from nowcasting.config.paths import PURPLEAIR_SENSORS_JS, DASHBOARD_DATA_DIR
import csv
from pathlib import Path
from nowcasting.data.dashboard_data import category_for_value, load_sites, pollutant_by_label, title_case_station_name


OBSERVATION_URL = "https://data.airquality.nsw.gov.au/api/Data/get_Observations"
SITE_DETAILS_URL = "https://data.airquality.nsw.gov.au/api/Data/get_SiteDetails"
PARAMETER_DETAILS_URL = "https://data.airquality.nsw.gov.au/api/Data/get_ParameterDetails"
PURPLEAIR_SENSORS_PATH = PURPLEAIR_SENSORS_JS
_CACHE_DIR = Path(DASHBOARD_DATA_DIR) / "monitoring_cache"
_OBS_CACHE_CSV = _CACHE_DIR / "observations.csv"
_PURPLEAIR_SNAPSHOT_JSON = _CACHE_DIR / "purpleair_snapshot.json"


def _ensure_cache_dir():
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _save_observations_cache(rows):
    """Save a list of observation dicts to CSV cache (overwrites)."""
    _ensure_cache_dir()
    if not rows or not isinstance(rows, list):
        return
    # Ensure each row exposes top-level ParameterCode/ParameterDescription so
    # the CSV includes explicit meteorological variable columns (TEMP, HUMID,
    # WSP, RAIN, etc.) rather than embedding them only inside the `Parameter`
    # JSON string.
    def _ensure_param_fields(item):
        if not isinstance(item, dict):
            return
        param = item.get("Parameter")
        if isinstance(param, str):
            # try to parse JSON-like string
            try:
                parsed = json.loads(param)
                param = parsed
            except Exception:
                param = None
        if isinstance(param, dict):
            code = param.get("ParameterCode") or param.get("Code")
            desc = param.get("ParameterDescription") or param.get("Description")
            if code is not None:
                # store as plain string for CSV column
                item["ParameterCode"] = str(code)
            if desc is not None:
                item["ParameterDescription"] = str(desc)

    # Collect union of keys
    keys = set()
    for r in rows:
        _ensure_param_fields(r)
        if isinstance(r, dict):
            keys.update(r.keys())
    keys = sorted(list(keys))
    try:
        # Merge with existing cache to avoid duplicates. Key by Site_Id/Date/Hour when available.
        existing = _load_observations_cache() or []
        key_fields = ("Site_Id", "SiteId", "SiteId", "site_id", "Date", "Hour")

        def make_key(item):
            # Use Site/Date/Hour plus ParameterCode when present so different
            # parameters (TEMP, HUMID, WSP, etc.) at the same site/hour are
            # preserved rather than overwritten.
            if not isinstance(item, dict):
                return None
            sid = item.get("Site_Id") or item.get("site_id") or item.get("SiteId") or item.get("SiteID")
            date = item.get("Date")
            hour = item.get("Hour")
            param = item.get("Parameter") or {}
            param_code = None
            if isinstance(param, dict):
                param_code = param.get("ParameterCode")
            return f"{sid}::{date}::{hour}::{param_code or ''}"

        merged_map = {}
        # Normalize existing rows from cache as well so they gain ParameterCode
        # if previously missing.
        for r in existing:
            _ensure_param_fields(r)
            k = make_key(r)
            if k is None:
                # fallback to full-json key
                k = json.dumps(r, sort_keys=True)
            merged_map[k] = r

        for r in rows:
            _ensure_param_fields(r)
            if not isinstance(r, dict):
                continue
            k = make_key(r)
            if k is None:
                k = json.dumps(r, sort_keys=True)
            merged_map[k] = r

        merged = list(merged_map.values())

        # Recompute keys union across merged rows
        keys = set()
        for r in merged:
            if isinstance(r, dict):
                keys.update(r.keys())
        keys = sorted(list(keys))

        with _OBS_CACHE_CSV.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            for r in merged:
                row = {}
                for k in keys:
                    v = r.get(k) if isinstance(r, dict) else None
                    # Flatten lists/dicts to JSON-like strings
                    if isinstance(v, (list, dict)):
                        try:
                            row[k] = json.dumps(v, ensure_ascii=False)
                        except Exception:
                            row[k] = str(v)
                    else:
                        row[k] = "" if v is None else str(v)
                writer.writerow(row)
    except Exception:
        return


def _load_observations_cache():
    """Load cached observations from CSV if present, returning list of dicts."""
    if not _OBS_CACHE_CSV.exists():
        return []
    out = []
    try:
        with _OBS_CACHE_CSV.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                # Try to parse JSON-like strings back to python structures when possible
                parsed = {}
                for k, v in row.items():
                    if v is None:
                        parsed[k] = None
                        continue
                    text = v.strip()
                    if (text.startswith("[") and text.endswith("]")) or (text.startswith("{") and text.endswith("}")):
                        try:
                            parsed[k] = json.loads(text)
                            continue
                        except Exception:
                            pass
                    parsed[k] = text
                out.append(parsed)
    except Exception:
        return []
    return out


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


def _load_bundled_purpleair_snapshot():
    """Load a packaged PurpleAir snapshot from the repository when live access fails."""

    module_root = Path(__file__).resolve().parents[4]
    candidate_dirs = [
        module_root / "AI_Dashboard_2026Y" / "data" / "downloads" / "monitoring_test",
        module_root / "AI_Dashboard_2026" / "data" / "downloads" / "monitoring_test",
        module_root / "AI_DASH" / "data" / "downloads" / "monitoring_test",
    ]
    seen = set()
    for directory in candidate_dirs:
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
SYDNEY_TZ = timezone(timedelta(hours=10))
PURPLEAIR_API_KEY = os.environ.get("PURPLEAIR_API_KEY", "D80F3AFD-DDAD-11ED-BD21-42010A800008")
PURPLEAIR_SNAPSHOT_URL = "https://api.purpleair.com/v1/sensors"
PURPLEAIR_HISTORY_URL = "https://api.purpleair.com/v1/sensors/{sensor_index}/history"
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


def fetch_observations(query=None, timeout=30):
    """Fetch observations from the NSW API.

    - When query is None, returns the current hourly snapshot (body is an empty string).
    - When query is a dict, it is POSTed as JSON (used for historical queries).
    """

    if query is None:
        data = b'""'
    else:
        data = json.dumps(query).encode("utf-8")

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
        # On network error, fall back to cached observations if available
        cached = _load_observations_cache()
        return cached or []

    # Save successful fetch to cache for offline reuse
    try:
        if isinstance(raw, list):
            _save_observations_cache(raw)
    except Exception:
        pass

    if not isinstance(raw, list):
        return []
    return raw


def fetch_observation_history(site_ids, parameter_codes, start_date, end_date, timeout=30):
    """Fetch historical hourly observations for one or more sites.

    The NSW API expects a full query object for historical requests.
    """

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
        site_id = site.get("Site_Id")
        if site_id is None:
            continue
        lookup[int(site_id)] = site
    return lookup


def _sydney_today_and_tomorrow():
    now = datetime.now(SYDNEY_TZ)
    today = now.date().isoformat()
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    return today, tomorrow


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
                return ast.literal_eval(text[start : index + 1])

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
        rows.append(
            {
                "site_id": int(sensor_id),
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
    """Fetch the latest AQMS observation for each site and return dashboard rows."""
    # Use fetch_observations which has caching/fallback built-in.
    raw = fetch_observations(query=None, timeout=timeout)
    if not isinstance(raw, list):
        return []

    latest_by_site = {}
    for item in raw:
        site_id = item.get("Site_Id")
        if site_id is None:
            continue

        category = item.get("AirQualityCategory")
        if category in (None, "", "N/A"):
            continue

        try:
            hour_value = int(item.get("Hour") or -1)
        except (TypeError, ValueError):
            hour_value = -1

        existing = latest_by_site.get(site_id)
        if existing is None or hour_value >= existing["_hour"]:
            latest_by_site[site_id] = dict(item, _hour=hour_value)

    rows = []
    site_lookup = get_site_lookup_by_id()
    for site_id, item in latest_by_site.items():
        site = site_lookup.get(int(site_id), {})
        category = item.get("AirQualityCategory")
        value = item.get("Value")
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
                "value": value,
                "category": _format_category(category),
                "category_key": str(category or "").strip().upper(),
                "category_color": _monitoring_color(category),
                "determining_pollutant": item.get("DeterminingPollutant") or "",
                "source": "AQMS",
            }
        )

    rows.sort(
        key=lambda row: (
            _category_sort_key(row.get("category_key")),
            -(int(row.get("hour") or -1)),
            row.get("station") or "",
        )
    )
    return rows


def fetch_monitoring_and_purpleair_rows(timeout=30):
    """Fetch live AQMS rows and append the static PurpleAir sensor layer."""
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
    """Fetch PurpleAir sensors snapshot within bounds."""

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
        query += (
            f"&nwlng={bounds.get('west')}&nwlat={bounds.get('north')}"
            f"&selng={bounds.get('east')}&selat={bounds.get('south')}"
        )
    url = f"{PURPLEAIR_SNAPSHOT_URL}?{query}"

    request = Request(url, headers={"X-API-Key": PURPLEAIR_API_KEY, "accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, ValueError) as exc:
        # Return cached snapshot when network fails
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
        sensor_index = row[id_idx]
        try:
            sensor_index = int(sensor_index)
        except (TypeError, ValueError):
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

    fetched_at = payload.get("time_stamp") or payload.get("data_time_stamp")
    out = {"sensors": snapshot, "fetched_at": fetched_at, "error": None}
    out["source"] = "live"
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
    url = (
        PURPLEAIR_HISTORY_URL.format(sensor_index=int(sensor_index))
        + f"?start_timestamp={int(start_timestamp)}&end_timestamp={int(end_timestamp)}&average={int(average)}&fields={field_param}"
    )

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
    """Cluster PurpleAir sensors into coarse bins for dashboard summaries."""

    sensors = snapshot or []
    bins = {}
    for sensor in sensors:
        lat = sensor.get("lat")
        lon = sensor.get("lon")
        if lat is None or lon is None:
            continue
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            continue

        key = (round(lat / bin_degrees) * bin_degrees, round(lon / bin_degrees) * bin_degrees)
        bins.setdefault(key, []).append(sensor)

    def sensor_value(sensor):
        if pollutant_label == "PM10":
            return sensor.get("pm10")
        return sensor.get("pm25")

    clusters = []
    for cluster_index, ((lat_key, lon_key), members) in enumerate(sorted(bins.items(), key=lambda item: (-len(item[1]), item[0]))):
        values = []
        for member in members:
            value = sensor_value(member)
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = None
            if value is not None:
                values.append(value)
        mean_value = sum(values) / len(values) if values else None

        category_key, colour = (
            category_for_value(pollutant_label, mean_value) if mean_value is not None else ("no-data", "#9ca3af")
        )
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
    except (TypeError, ValueError):
        return None

    for sensor in sensors or []:
        s_lat = sensor.get("lat")
        s_lon = sensor.get("lon")
        if s_lat is None or s_lon is None:
            continue
        try:
            s_lat = float(s_lat)
            s_lon = float(s_lon)
        except (TypeError, ValueError):
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
