from fastapi import APIRouter
from nowcasting.data.file_discovery import get_options

router = APIRouter()

@router.get("/getOptions")
def get_options_route():
    """Return available options parsed from forecast filenames."""
    return get_options()
