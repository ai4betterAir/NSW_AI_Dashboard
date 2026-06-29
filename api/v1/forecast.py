from fastapi import APIRouter, HTTPException, Query
from typing import Optional
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from nowcasting.data.parse_forecast import parse_csv
from nowcasting.data.file_discovery import forecast_file_path

router = APIRouter()

# Simple cache for forecast responses
_forecast_cache = {}
_forecast_cache_lock = threading.Lock()


@router.get("/getForecastInfo")
async def get_forecast_info(selectedRegion: Optional[str] = Query(None),
                            selectedPollutant: Optional[str] = Query(None),
                            selectedTime: Optional[str] = Query(None),
                            selectedModel: Optional[str] = Query(None),
                            selectedDate: Optional[str] = Query(None)):
    """Find matching CSV and return parsed forecast info with caching."""
    chosen = [v for v in [selectedRegion, selectedPollutant, selectedTime, selectedModel, selectedDate] if v]
    if len(chosen) != 5:
        raise HTTPException(status_code=400, detail="All forecast selection parameters are required")
    csv_path = forecast_file_path(chosen)
    if not csv_path:
        raise HTTPException(status_code=404, detail="Forecast CSV not found for selection")
    try:
        stat = os.stat(csv_path)
        file_version = (stat.st_mtime_ns, stat.st_size)
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Forecast CSV is no longer available") from exc

    # Include the file version so replacing an operational CSV cannot return an
    # older parsed payload under the same selection key.
    cache_key = (tuple(chosen), file_version)
    with _forecast_cache_lock:
        cached = _forecast_cache.get(cache_key)
        if cached:
            cached_data, timestamp = cached
            if (datetime.now() - timestamp).total_seconds() < 600:
                return cached_data

    parsed = parse_csv(csv_path, selectedPollutant)
    with _forecast_cache_lock:
        _forecast_cache.clear()
        _forecast_cache[cache_key] = (parsed, datetime.now())
    return parsed
