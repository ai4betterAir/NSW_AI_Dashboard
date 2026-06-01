from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from nowcasting.data.parse_forecast import parse_csv
from nowcasting.data.file_discovery import forecast_file_path

router = APIRouter()

@router.get("/getForecastInfo")
async def get_forecast_info(selectedRegion: Optional[str] = Query(None),
                            selectedPollutant: Optional[str] = Query(None),
                            selectedTime: Optional[str] = Query(None),
                            selectedModel: Optional[str] = Query(None),
                            selectedDate: Optional[str] = Query(None)):
    """Find matching CSV and return parsed forecast info (stubbed)."""
    chosen = [v for v in [selectedRegion, selectedPollutant, selectedTime, selectedModel, selectedDate] if v]
    csv_path = forecast_file_path(chosen)
    if not csv_path:
        raise HTTPException(status_code=404, detail="Forecast CSV not found for selection")
    parsed = parse_csv(csv_path, selectedPollutant)
    return parsed
