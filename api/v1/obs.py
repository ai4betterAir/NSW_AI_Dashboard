from fastapi import APIRouter

from nowcasting.data.obs_data import fetch_latest_monitoring_rows

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
