import pandas as pd
import os
import re
from io import StringIO
from datetime import datetime
from functools import lru_cache


def _load_pollutant_mapping():
    """Read server/config/pollutant.js and build a mapping label->ParameterCode if possible."""
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    pj = os.path.join(base, "server", "config", "pollutant.js")
    mapping = {}
    if not os.path.exists(pj):
        return mapping
    try:
        text = open(pj, "r").read()
        # crude regex to capture objects like { ParameterCode: "OZONE", label: "O3", ... }
        entries = re.findall(r"\{([^}]+)\}", text, flags=re.S)
        for e in entries:
            pc_match = re.search(r"ParameterCode\s*:\s*['\"]?([A-Za-z0-9.]+)['\"]?", e)
            label_match = re.search(r"label\s*:\s*['\"]?([^'\"]+)['\"]?", e)
            if pc_match and label_match:
                mapping[label_match.group(1).strip()] = pc_match.group(1).strip()
    except Exception:
        pass
    return mapping


def _split_sections(text):
    """Split CSV text into first table and stats table by blank line(s)."""
    lines = text.splitlines()
    sep_idx = None
    for i, line in enumerate(lines):
        if line.strip() == "":
            sep_idx = i
            break
    if sep_idx is None:
        return "\n".join(lines), None
    # skip successive blank lines
    j = sep_idx
    while j < len(lines) and lines[j].strip() == "":
        j += 1
    first = "\n".join(lines[:sep_idx])
    second = "\n".join(lines[j:]) if j < len(lines) else None
    return first, second


def _limit_forecast_text(first_txt, max_forecast_hours):
    if max_forecast_hours is None:
        return first_txt
    lines = first_txt.splitlines()
    if not lines:
        return first_txt

    limited = [lines[0]]
    for line in lines[1:]:
        if not line.strip():
            continue
        try:
            forecast_hours = int(float(line.rsplit(",", 1)[-1]))
        except ValueError:
            limited.append(line)
            continue
        if forecast_hours <= max_forecast_hours:
            limited.append(line)
    return "\n".join(limited)


def _parse_csv_impl(filepath, pollutant_label=None, max_forecast_hours=None):
    """Implementation of CSV parsing. Accepts `filepath` as string."""
    if not os.path.exists(filepath):
        return {"error": "file not found"}
    # load pollutant mapping from original server config
    mapping = _load_pollutant_mapping()
    pollutant_code = None
    if pollutant_label:
        pollutant_code = mapping.get(pollutant_label) or pollutant_label

    stats_txt = None
    raw = open(filepath, "r", encoding="utf-8", errors="ignore").read()
    first_txt, stats_txt = _split_sections(raw)
    first_txt = _limit_forecast_text(first_txt, max_forecast_hours)

    try:
        df = pd.read_csv(StringIO(first_txt))
    except Exception as e:
        return {"error": f"Could not parse first table: {e}"}

    # prepare result
    result = {"ranking": {}, "data": {"time": {"forecastTime": [], "histTime": []}, "stations": {}}, "stats": {}}
    forecast_time_set = set()
    hist_time_set = set()

    # determine timestamp and hour column names (fallbacks)
    ts_col = None
    hour_col = None
    for c in df.columns:
        lc = c.lower()
        if lc in ("datetime", "date", "timestamp"):
            ts_col = c
        if lc in ("forecast_hours", "forecast_hours", "hour", "hours", "forecasthour"):
            hour_col = c
    if ts_col is None:
        # try common name
        ts_col = df.columns[0]
    if hour_col is None:
        # try to find a numeric column named like 'forecast_hours'
        for c in df.columns:
            if re.search(r"hour", c, flags=re.I):
                hour_col = c
                break

    # find station columns that start with pollutant_code + '_'
    station_cols = []
    if pollutant_code:
        prefix = f"{pollutant_code}_"
        for c in df.columns:
            if c.startswith(prefix):
                station_cols.append(c)
    # fallback: any column with an underscore (assume prefix_site)
    if not station_cols:
        excluded = {
            str(ts_col).lower(),
            str(hour_col).lower() if hour_col else "",
            "forecast_hours",
            "hour",
            "hours",
            "timestamp",
            "date",
        }
        for c in df.columns:
            if "_" in c and c.lower() not in excluded:
                station_cols.append(c)

    # process each row
    for _, row in df.iterrows():
        # parse timestamp
        ts_val = row.get(ts_col)
        try:
            # attempt to normalise timestamp to ISO-like string
            if pd.isna(ts_val):
                timestamp = None
            else:
                try:
                    dt = pd.to_datetime(ts_val)
                    timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    timestamp = str(ts_val)
        except Exception:
            timestamp = str(ts_val)

        try:
            fh_val = row.get(hour_col, 0) if hour_col else 0
            forecast_hours = int(fh_val) if not pd.isna(fh_val) else 0
        except Exception:
            forecast_hours = 0
        if max_forecast_hours is not None and forecast_hours > max_forecast_hours:
            continue

        for col in station_cols:
            if not col.startswith("_") and "_" in col:
                parts = col.split("_", 1)
                site_name = parts[1]
            else:
                site_name = col
            try:
                rawv = row.get(col)
                if pd.isna(rawv):
                    continue
                value = float(rawv)
            except Exception:
                continue

            if site_name not in result["data"]["stations"]:
                result["data"]["stations"][site_name] = {"forecastValue": [], "histValue": []}

            if forecast_hours > 0:
                # ranking
                if (site_name not in result["ranking"]) or (value > result["ranking"][site_name]["maxValue"]):
                    result["ranking"][site_name] = {"maxValue": value, "timestamp": timestamp}
                if timestamp not in forecast_time_set:
                    forecast_time_set.add(timestamp)
                    result["data"]["time"]["forecastTime"].append(timestamp)
                result["data"]["stations"][site_name]["forecastValue"].append(value)
            else:
                if timestamp not in hist_time_set:
                    hist_time_set.add(timestamp)
                    result["data"]["time"]["histTime"].append(timestamp)
                result["data"]["stations"][site_name]["histValue"].append(value)

    # process stats table if present
    if stats_txt:
        try:
            stats_df = pd.read_csv(StringIO(stats_txt), header=0)
            # first row columns after first column are stat names
            stat_names = list(stats_df.columns)[1:]
            for _, row in stats_df.iterrows():
                key = row.iloc[0]
                result["stats"][str(key)] = {}
                for i, stat in enumerate(stat_names):
                    try:
                        result["stats"][str(key)][stat] = row.iloc[i + 1]
                    except Exception:
                        result["stats"][str(key)][stat] = None
        except Exception:
            # ignore stats parsing errors
            pass

    return result


@lru_cache(maxsize=32)
def _parse_csv_cached(filepath_str, pollutant_label, max_forecast_hours):
    return _parse_csv_impl(filepath_str, pollutant_label, max_forecast_hours)


def parse_csv(filepath, pollutant_label=None, max_forecast_hours=None):
    """Public wrapper that normalizes `filepath` and delegates to cached implementation."""
    return _parse_csv_cached(str(filepath), pollutant_label, max_forecast_hours)
