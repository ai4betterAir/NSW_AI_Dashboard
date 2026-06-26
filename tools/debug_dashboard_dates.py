#!/usr/bin/env python3
"""Print the dates/times the dashboard can actually see.

Run from the repository root:

    python tools/debug_dashboard_dates.py

This checks:
- current git commit
- Python module paths being imported
- dashboard forecast data directory
- newest forecast CSV files and their latest forecastTime
- live AQMS latest observation timestamp
- local AQMS cache latest timestamp
"""

from __future__ import annotations

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


def run(cmd: list[str]) -> str:
    try:
        return subprocess.check_output(cmd, cwd=str(REPO_ROOT), text=True, stderr=subprocess.STDOUT).strip()
    except Exception as exc:
        return f"ERROR: {exc}"


def parse_dt(value):
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in [text, text.replace(" ", "T")]:
        try:
            return datetime.fromisoformat(candidate.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    return None


def latest_forecast_time_in_csv(path: Path):
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
                # Some dashboard CSVs may store one timestamp column and station columns.
                # Stop scanning after enough rows for performance.
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
                pass
        if dt is not None and (latest is None or dt > latest):
            latest = dt
    return latest


def main() -> int:
    print("=== Dashboard date diagnostics ===")
    print(f"Repo root: {REPO_ROOT}")
    print(f"Git branch: {run(['git', 'branch', '--show-current'])}")
    print(f"Git commit: {run(['git', 'rev-parse', '--short', 'HEAD'])}")
    print(f"Git latest message: {run(['git', 'log', '-1', '--pretty=%s'])}")
    print()

    try:
        from nowcasting.config import paths
        from nowcasting.data import file_discovery, obs_data
    except Exception as exc:
        print(f"ERROR importing dashboard modules: {exc}")
        return 2

    print("=== Imported modules ===")
    print(f"obs_data.py: {Path(obs_data.__file__).resolve()}")
    print(f"file_discovery.py: {Path(file_discovery.__file__).resolve()}")
    print(f"DASHBOARD_DATA_DIR: {getattr(paths, 'DASHBOARD_DATA_DIR', None)}")
    print(f"CSV_DATA_FILE_PATH: {getattr(file_discovery, 'CSV_DATA_FILE_PATH', None)}")
    print()

    data_dir = Path(file_discovery.CSV_DATA_FILE_PATH)
    csvs = sorted(data_dir.glob("*.csv"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True) if data_dir.exists() else []
    print("=== Forecast CSV files visible to dashboard ===")
    print(f"CSV count: {len(csvs)}")
    newest_by_name = []
    for p in csvs[:25]:
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        latest_ft = latest_forecast_time_in_csv(p)
        newest_by_name.append((latest_ft, p))
        print(f"{mtime} | latest forecastTime={latest_ft or 'not found'} | {p.name}")
    latest_found = [item for item in newest_by_name if item[0] is not None]
    if latest_found:
        dt, path = max(latest_found, key=lambda item: item[0])
        print(f"\nLatest forecast timestamp found in scanned files: {dt} from {path.name}")
    else:
        print("\nNo forecastTime column found in the first 25 newest CSVs.")
    print()

    print("=== Live AQMS observation fetch ===")
    try:
        rows = obs_data.fetch_observations(query=None, timeout=20)
        latest = latest_raw_obs_time(rows)
        print(f"Rows fetched: {len(rows) if isinstance(rows, list) else 'not a list'}")
        print(f"Latest AQMS observation visible: {latest or 'not found'}")
        if isinstance(rows, list) and rows:
            sample = max(rows, key=lambda row: latest_raw_obs_time([row]) or datetime.min)
            print("Sample latest row:")
            print(json.dumps({k: sample.get(k) for k in ['Site_Id', 'Date', 'Hour', 'HourDescription', 'ParameterCode', 'AirQualityCategory', 'Value']}, indent=2, default=str))
    except Exception as exc:
        print(f"AQMS fetch error: {exc}")
    print()

    print("=== AQMS cache files ===")
    cache_files = sorted(REPO_ROOT.glob("**/monitoring_cache/observations.csv"))
    if not cache_files:
        print("No observations.csv cache file found under repo.")
    for path in cache_files:
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            print(f"{path} | rows={len(rows)} | latest={latest_raw_obs_time(rows) or 'not found'}")
        except Exception as exc:
            print(f"{path} | ERROR reading: {exc}")

    print("\nExpected for your dashboard right now: latest AQMS should be 2026-06-26 around 15:00-16:00, and forecast CSVs should include 2026-06-26 times. If not, the server is reading old data files or old process/code.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
