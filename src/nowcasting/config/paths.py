"""Central paths for the server_python application."""

from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = SERVER_ROOT / "src"
DATA_ROOT = SERVER_ROOT / "data"
REFERENCE_DATA_DIR = DATA_ROOT / "reference"
# Use the cnn_lstm forecast AI_dashboard_files as the default input folder
# Prefer absolute path for dashboard data (overrides packaged data)
DASHBOARD_DATA_DIR = Path("/mnt/scratch_lustre/ar_ai4ba_scratch/Ai4BetterAir/AI_Nowcasting/cnn_lstm_forecast/AI_dashboard_files")

RECOMMENDATIONS_JSON = REFERENCE_DATA_DIR / "recommendations.json"
SITE_DETAILS_JS = REFERENCE_DATA_DIR / "SiteDetails.js"
PURPLEAIR_SENSORS_JS = REFERENCE_DATA_DIR / "purpleair_sensors.js"
