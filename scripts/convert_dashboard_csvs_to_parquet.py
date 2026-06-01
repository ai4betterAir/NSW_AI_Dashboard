"""Convert dashboard CSVs (first table) to Parquet for faster reads.

Usage: run from project root with the virtualenv active.
"""
import os
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nowcasting.config.paths import DASHBOARD_DATA_DIR
from nowcasting.data.parse_forecast import _split_sections, _limit_forecast_text
import pandas as pd
from io import StringIO

INPUT_DIR = Path(DASHBOARD_DATA_DIR)
if not INPUT_DIR.exists():
    print('Dashboard data dir not found:', INPUT_DIR)
    raise SystemExit(1)

count = 0
for csv_path in sorted(INPUT_DIR.glob('*.csv')):
    pq_path = csv_path.with_suffix('.parquet')
    try:
        csv_mtime = csv_path.stat().st_mtime
        if pq_path.exists() and pq_path.stat().st_mtime >= csv_mtime:
            print('Skipping up-to-date:', csv_path.name)
            continue
        text = csv_path.read_text(encoding='utf-8', errors='ignore')
        first_txt, stats_txt = _split_sections(text)
        first_txt = _limit_forecast_text(first_txt, None)
        df = pd.read_csv(StringIO(first_txt))
        df.to_parquet(pq_path, index=False)
        print('Wrote parquet:', pq_path.name)
        count += 1
    except Exception as e:
        print('Error converting', csv_path.name, e)

print('Converted', count, 'files')
