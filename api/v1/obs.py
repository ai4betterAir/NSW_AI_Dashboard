from fastapi import APIRouter
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from nowcasting.data.obs_data import fetch_latest_monitoring_rows, fetch_observations_result

router = APIRouter()


@router.get("/getNewestObs")
def get_newest_obs():
    rows = fetch_latest_monitoring_rows()
    return [
        {
            "Site_Id": row.get("site_id"),
            "SiteName": row.get("station"),
            "Latitude": row.get("lat"),
            "Longitude": row.get("lon"),
            "Region": row.get("region"),
            "Date": row.get("date"),
            "Hour": row.get("hour"),
            "HourDescription": row.get("hour_description"),
            "Value": row.get("value"),
            "AirQualityCategory": row.get("category_key"),
            "DeterminingPollutant": row.get("determining_pollutant"),
        }
        for row in rows
    ]


@router.get("/getObservationStatus")
def get_observation_status():
    """Report source freshness without presenting a failed fetch as live data."""
    result = fetch_observations_result()
    return {
        "source": result.get("source"),
        "rowCount": len(result.get("rows") or []),
        "latest": result.get("latest"),
        "recent": bool(result.get("recent")),
        "error": result.get("error"),
    }
