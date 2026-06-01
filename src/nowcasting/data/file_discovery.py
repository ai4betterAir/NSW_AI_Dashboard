import os
from dotenv import load_dotenv

from nowcasting.config.paths import DASHBOARD_DATA_DIR

load_dotenv()

FORECAST_EXT = ".csv"
PARTITION = "_"
FILE_NAME_PARAMETERS = ["regions", "pollutants", "timeScopes", "models", "date"]

_KNOWN_POLLUTANTS = {"O3", "PM2.5", "PM10"}

_env_data_path = os.environ.get("CSV_DATA_FILE_PATH")
_default_data_path = DASHBOARD_DATA_DIR

# Prefer the project's cnn_lstm `AI_dashboard_files` if present and non-empty.
# Otherwise respect an explicit `CSV_DATA_FILE_PATH` env var, then fall back to
# the packaged default folder so the dashboard can still render when `.env`
# contains a placeholder.
try:
    default_has_files = os.path.isdir(str(_default_data_path)) and any(
        fn.lower().endswith(FORECAST_EXT) for fn in os.listdir(str(_default_data_path))
    )
except Exception:
    default_has_files = False

if default_has_files:
    CSV_DATA_FILE_PATH = str(_default_data_path)
elif _env_data_path and os.path.isdir(_env_data_path):
    CSV_DATA_FILE_PATH = os.path.abspath(_env_data_path)
else:
    CSV_DATA_FILE_PATH = str(_default_data_path)

def _parse_forecast_filename(filename):
    """Parse forecast CSV filenames into the key parameters used by the dashboard.

    Expected pattern (underscore-delimited, with variable-length region/model names):
      <region>_<pollutant>_<inputs>_<horizon>_<model...>_<date>.csv

    Examples:
      Central_Coast_O3_12_3_Sparse_LSTM_v1_20260521AEST.csv
      CE_Sydney_PM2.5_12_6_Sparse_LSTM_v2_20260515AEST.csv
    """

    base = str(filename).strip()
    if not base.lower().endswith(FORECAST_EXT):
        return None
    base = base[: -len(FORECAST_EXT)]

    parts = [p for p in base.split(PARTITION) if p]
    if len(parts) < 6:
        return None

    date = parts[-1]

    pollutant_idx = None
    for idx, token in enumerate(parts):
        if token in _KNOWN_POLLUTANTS:
            pollutant_idx = idx
            break
    if pollutant_idx is None:
        return None
    if pollutant_idx == 0:
        return None

    # Inputs and horizon tokens should be immediately after pollutant.
    # We only keep `horizon` for the dashboard filters.
    if pollutant_idx + 2 >= len(parts) - 1:
        return None
    horizon = parts[pollutant_idx + 2]

    model_parts = parts[pollutant_idx + 3 : -1]
    if not model_parts:
        return None
    model = PARTITION.join(model_parts)

    region = PARTITION.join(parts[:pollutant_idx])
    pollutant = parts[pollutant_idx]
    return {
        "regions": region,
        "pollutants": pollutant,
        "timeScopes": horizon,
        "models": model,
        "date": date,
    }

# Cached lists
_file_names = []
_file_all_params = []
_file_key_params = []
_file_options = None


def _reload():
    global _file_names, _file_all_params, _file_key_params, _file_options
    _file_names = []
    _file_all_params = []
    _file_key_params = []
    _file_options = None

    if not os.path.isdir(CSV_DATA_FILE_PATH):
        return

    for fn in os.listdir(CSV_DATA_FILE_PATH):
        if not fn.lower().endswith(FORECAST_EXT):
            continue
        key = _parse_forecast_filename(fn)
        if key is None:
            continue
        _file_names.append(fn)
        elements = fn.replace(FORECAST_EXT, "").split(PARTITION)
        _file_all_params.append(elements)
        _file_key_params.append(key)


def get_file_names():
    if not _file_names:
        _reload()
    return _file_names


def get_file_key_params():
    if not _file_key_params:
        _reload()
    return _file_key_params


def get_options():
    global _file_options
    if _file_options is not None:
        return _file_options
    opts = {k: set() for k in FILE_NAME_PARAMETERS}
    for kp in get_file_key_params():
        for k, v in kp.items():
            if v is not None:
                opts[k].add(v)
    # convert to lists
    opts_list = {k: sorted(list(v)) for k, v in opts.items()}
    _file_options = opts_list
    return _file_options


def forecast_file_path(chosen_key_params):
    """chosen_key_params: list of values (order: regions,pollutants,timeScopes,models,date)
    returns full path if exists, else None
    """
    if not _file_all_params:
        _reload()

    for fn, key_params in zip(_file_names, _file_key_params):
        values = [key_params.get(k) for k in FILE_NAME_PARAMETERS]
        if values == chosen_key_params:
            full = os.path.join(CSV_DATA_FILE_PATH, fn)
            if os.path.exists(full):
                return full
    return None
