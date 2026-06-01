from fastapi import APIRouter
from nowcasting.data.file_discovery import get_file_key_params

router = APIRouter()

@router.get("/getForecastFiles")
def get_forecast_files_route():
    """Return key parameters for all forecast files."""
    return get_file_key_params()
