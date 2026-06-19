"""Central paths for the server_python application."""

from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = SERVER_ROOT / "src"
DATA_ROOT = SERVER_ROOT / "data"
REFERENCE_DATA_DIR = DATA_ROOT / "reference"
# Use the shared forecast dashboard CSV folder as the default input folder.
DASHBOARD_DATA_DIR = Path("/mnt/scratch_lustre/ar_aichem_scratch/2AI_dashboard_files")

RECOMMENDATIONS_JSON = REFERENCE_DATA_DIR / "recommendations.json"
SITE_DETAILS_JS = REFERENCE_DATA_DIR / "SiteDetails.js"
PURPLEAIR_SENSORS_JS = REFERENCE_DATA_DIR / "purpleair_sensors.js"
