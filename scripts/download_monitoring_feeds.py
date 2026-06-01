#!/usr/bin/env python3
"""
Download live monitoring feeds (AQMS observations + PurpleAir snapshot) to disk.

This is a standalone copy of the data-download logic used by the dashboard,
so operational pipelines can fetch and persist raw JSON payloads on schedule.
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path


def _dump_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)


def main():
    parser = argparse.ArgumentParser(description="Download AQMS + PurpleAir feeds to JSON files.")
    parser.add_argument(
        "--out-dir",
        default="data/downloads/monitoring",
        help="Output folder (relative to server_python/ unless absolute).",
    )
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds.")
    parser.add_argument(
        "--bounds",
        default="130.9992792,163.638889,-37.50528021,-28.15701999",
        help="PurpleAir bounds as: west,east,south,north",
    )
    parser.add_argument(
        "--timestamped",
        action="store_true",
        help="Write timestamped filenames instead of overwriting the latest files.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = (repo_root / out_dir).resolve()

    try:
        west, east, south, north = [float(piece.strip()) for piece in args.bounds.split(",")]
    except ValueError:
        raise SystemExit("Invalid --bounds. Expected: west,east,south,north")

    os.environ.setdefault("PYTHONPATH", str(repo_root / "src"))
    import sys

    if str(repo_root / "src") not in sys.path:
        sys.path.insert(0, str(repo_root / "src"))

    from nowcasting.data.obs_data import fetch_observations, fetch_purpleair_snapshot

    fetched_at = datetime.now().isoformat(timespec="seconds")
    bounds = {"west": west, "east": east, "south": south, "north": north}

    aqms_error = None
    purpleair_error = None
    aqms_payload = []
    purpleair_payload = {"sensors": [], "fetched_at": None, "error": None}

    try:
        aqms_payload = fetch_observations(query=None, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        aqms_error = str(exc)

    try:
        purpleair_payload = fetch_purpleair_snapshot(bounds=bounds, timeout=args.timeout)
        purpleair_error = purpleair_payload.get("error")
    except Exception as exc:  # noqa: BLE001
        purpleair_error = str(exc)

    suffix = ""
    if args.timestamped:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{ts}"

    _dump_json(
        out_dir / f"aqms_observations_snapshot{suffix}.json",
        {"fetched_at": fetched_at, "source": "AQMS", "error": aqms_error, "items": aqms_payload},
    )
    _dump_json(
        out_dir / f"purpleair_snapshot{suffix}.json",
        {"fetched_at": fetched_at, "source": "PurpleAir", "bounds": bounds, **purpleair_payload},
    )
    _dump_json(
        out_dir / f"download_metadata{suffix}.json",
        {
            "fetched_at": fetched_at,
            "aqms": {"items": len(aqms_payload or []), "error": aqms_error},
            "purpleair": {"items": len((purpleair_payload or {}).get("sensors") or []), "error": purpleair_error},
            "out_dir": str(out_dir),
        },
    )

    print(f"Wrote downloads to: {out_dir}")


if __name__ == "__main__":
    main()

