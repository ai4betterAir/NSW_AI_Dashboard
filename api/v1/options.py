from fastapi import APIRouter
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from nowcasting.data.file_discovery import get_options

router = APIRouter()

@router.get("/getOptions")
def get_options_route():
    """Return options from the current forecast directory contents."""
    return get_options()
