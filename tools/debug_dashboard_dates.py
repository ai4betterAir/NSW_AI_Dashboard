#!/usr/bin/env python3
"""Print the dates/times the dashboard can actually see.

Run from the repository root:

    python tools/debug_dashboard_dates.py

This avoids expensive stat() calls across the full Lustre forecast folder.
"""

import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def run(cmd):
    try:
        return subprocess.check_output(cmd, cwd=str(REPO_ROOT), universal_newlines=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return "ERROR: %s" % exc


def parse_dt(value):
    text = str(value or "").strip()
    if not text:
        return None
    text_variants = [text, text.replace("T", " "), text.replace("Z", "").replace("T", " ")]
    cleaned = []
    for item in text_variants:
        if "+" in item:
            item = item.split("+", 1)[0].strip()
        cleaned.append(item)
    for candidate in cleaned:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                return datetime.strptime(candidate, fmt)
            except Exception:
                pass
    return None


def filename_date_score(name):
    """Score file names by date/run tokens without touching filesystem metadata."""
    text = str(name or "")
    dates = re.findall(r"(20\d{6})", text)
    date_score = int(dates[-1]) if dates else 0
    run_score = 0
    # Common forms: _09AEST.csv, _09.csv, 20260626_09AEST.csv
    matches = re.findall(r"(?:_|^)(\d{2})(?:AEST|AEDT)?(?:\.csv|$)", text)
    if matches:
        try:
            run_score = int(matches[-1])
        except Exception:
            run_score = 0
    return (date_score, run_score, text)


def latest_forecast_time_in_csv(path):
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            candidates = []
            for row in reader:
                for key in ("forecastTime", "ForecastTime", "time", "Time", "datetime", "dateTime"):
                    if key in row and row.get(key):
                        dt = parse_dt(row.get(key))
                        if dt:
                            candidates.append(dt)
                if len(candidates) > 5000:
                    break
            return max(candidates) if candidates else None
    except Exception:
        return None


def latest_raw_obs_time(rows):
    try:
        from nowcasting.data.obs_data import _observation_datetime
    except Exception:
        _observation_datetime = None
    latest = None
    for row in rows or []:
        dt = None
        if _observation_datetime:
            try:
                dt = _observation_datetime(row)
                if dt is not None:
                    dt = dt.replace(tzinfo=None)
            except Exception:
                dt = None
        if dt is None:
            date = str(row.get("Date") or row.get("date") or "").strip()
            hour = str(row.get("Hour") or row.get("hour") or "0").strip()
            try:
                base = datetime.strptime(date.split("T", 1)[0], "%Y-%m-%d")
                dt = base.replace(hour=int(float(hour)))
            except Exception:
                try:
                    base = datetime.strptime(date, "%d/%m/%Y")
                    dt = base.replace(hour=int(float(hour)))
                except Exception:
                    pass
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return latest


def list_forecast_candidates(data_dir, limit=40):
    """List likely latest forecast CSVs without stat() on every file."""
    try:
        names = [name for name in os.listdir(str(data_dir)) if name.lower().endswith(".csv")]
    except Exception as exc:
        print("ERROR listing forecast folder: %s" % exc)
        return [], 0
    # Prefer filenames carrying recent date tokens. This is much faster than stat()
    # on a large shared Lustre folder.
    names_sorted = sorted(names, key=filename_date_score, reverse=True)
    return [data_dir / name for name in names_sorted[:limit]], len(names)


def main():
    print("=== Dashboard date diagnostics ===")
    print("Repo root: %s" % REPO_ROOT)
    print("Git branch: %s" % run(["git", "branch", "--show-current"]))
    print("Git commit: %s" % run(["git", "rev-parse", "--short", "HEAD"]))
    print("Git latest message: %s" % run(["git", "log", "-1", "--pretty=%s"]))
    print()

    try:
        from nowcasting.config import paths
        from nowcasting.data import file_discovery, obs_data
    except Exception as exc:
        print("ERROR importing dashboard modules: %s" % exc)
        return 2

    print("=== Imported modules ===")
    print("obs_data.py: %s" % Path(obs_data.__file__).resolve())
    print("file_discovery.py: %s" % Path(file_discovery.__file__).resolve())
    print("DASHBOARD_DATA_DIR: %s" % getattr(paths, "DASHBOARD_DATA_DIR", None))
    print("CSV_DATA_FILE_PATH: %s" % getattr(file_discovery, "CSV_DATA_FILE_PATH", None))
    print()

    data_dir = Path(file_discovery.CSV_DATA_FILE_PATH)
    print("=== Forecast CSV files visible to dashboard ===")
    csvs, total_count = list_forecast_candidates(data_dir, limit=40)
    print("CSV count: %s" % total_count)
    newest_by_name = []
    for p in csvs:
        latest_ft = latest_forecast_time_in_csv(p)
        newest_by_name.append((latest_ft, p))
        print("filenameScore=%s | latest forecastTime=%s | %s" % (filename_date_score(p.name)[:2], latest_ft or "not found", p.name))
    latest_found = [item for item in newest_by_name if item[0] is not None]
    if latest_found:
        dt, path = max(latest_found, key=lambda item: item[0])
        print("\nLatest forecast timestamp found in scanned files: %s from %s" % (dt, path.name))
    else:
        print("\nNo forecastTime column found in scanned candidate CSVs.")
    print()

    print("=== Live AQMS observation fetch ===")
    try:
        rows = obs_data.fetch_observations(query=None, timeout=20)
        latest = latest_raw_obs_time(rows)
        print("Rows fetched: %s" % (len(rows) if isinstance(rows, list) else "not a list"))
        print("Latest AQMS observation visible: %s" % (latest or "not found"))
        if isinstance(rows, list) and rows:
            sample = max(rows, key=lambda row: latest_raw_obs_time([row]) or datetime.min)
            print("Sample latest row:")
            print(json.dumps({k: sample.get(k) for k in ["Site_Id", "Date", "Hour", "HourDescription", "ParameterCode", "AirQualityCategory", "Value"]}, indent=2, default=str))
    except Exception as exc:
        print("AQMS fetch error: %s" % exc)
    print()

    print("=== AQMS cache files ===")
    cache_files = sorted(REPO_ROOT.glob("**/monitoring_cache/observations.csv"))
    if not cache_files:
        print("No observations.csv cache file found under repo.")
    for path in cache_files:
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            print("%s | rows=%s | latest=%s" % (path, len(rows), latest_raw_obs_time(rows) or "not found"))
        except Exception as exc:
            print("%s | ERROR reading: %s" % (path, exc))

    print("\nExpected: forecast files should include 20260626 and latest AQMS should be around 2026-06-26 15:00-16:00. If forecast latest remains 2026-06-25, copy/generate 26 Jun forecast CSVs into CSV_DATA_FILE_PATH above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
