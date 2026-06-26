import os
import re
import threading
from dotenv import load_dotenv

from nowcasting.config.paths import DASHBOARD_DATA_DIR

load_dotenv()

FORECAST_EXT = ".csv"
PARTITION = "_"
FILE_NAME_PARAMETERS = ["regions", "pollutants", "timeScopes", "models", "date"]

_KNOWN_POLLUTANTS = {"O3", "PM2.5", "PM10"}

_env_data_path = os.environ.get("CSV_DATA_FILE_PATH")
_default_data_path = DASHBOARD_DATA_DIR

# The operational default is the shared nowcasting forecast folder. Only use a
# different source when CSV_DATA_FILE_PATH is explicitly set to an existing
# directory.
if _env_data_path and os.path.isdir(_env_data_path):
    CSV_DATA_FILE_PATH = os.path.abspath(_env_data_path)
else:
    CSV_DATA_FILE_PATH = str(_default_data_path)

def _date_token(value):
    match = re.search(r"(\d{8})", str(value or ""))
    return match.group(1) if match else str(value or "")


def _run_sort_value(value):
    text = str(value or "")
    match = re.search(r"(\d{2})", text)
    if not match:
        return -1
    try:
        return int(match.group(1))
    except ValueError:
        return -1


def _parse_forecast_filename(filename):
    """Parse forecast CSV filenames into the key parameters used by the dashboard.

    Expected pattern (underscore-delimited, with variable-length region/model names):
      <region>_<pollutant>_<inputs>_<horizon>_<model...>_<date>[_<run_hour>].csv

    Examples:
      Central_Coast_O3_12_3_Sparse_LSTM_v1_20260521AEST.csv
      Central_Coast_O3_12_3_Sparse_LSTM_v1_20260521_06AEST.csv
      CE_Sydney_PM2.5_12_6_Sparse_LSTM_v2_20260515AEST.csv
    """

    base = str(filename).strip()
    if not base.lower().endswith(FORECAST_EXT):
        return None
    base = base[: -len(FORECAST_EXT)]

    parts = [p for p in base.split(PARTITION) if p]
    if len(parts) < 6:
        return None

    run_hour = None
    date_index = len(parts) - 1
    if (
        len(parts) >= 2
        and (
            (len(parts[-1]) == 2 and parts[-1].isdigit())
            or (
                len(parts[-1]) == 6
                and parts[-1][:2].isdigit()
                and (parts[-1].endswith("AEST") or parts[-1].endswith("AEDT"))
            )
        )
        and (
            parts[-2].isdigit()
            or parts[-2].endswith("AEST")
            or parts[-2].endswith("AEDT")
        )
    ):
        run_hour = parts[-1]
        date_index = len(parts) - 2
    date = _date_token(parts[date_index])

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
    if pollutant_idx + 2 >= date_index:
        return None
    horizon = parts[pollutant_idx + 2]

    model_parts = parts[pollutant_idx + 3 : date_index]
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
        "runHour": run_hour or "",
    }

# Cached lists
_file_names = []
_file_all_params = []
_file_key_params = []
_file_options = None
_directory_signature = None
_cache_initialized = False
_cache_lock = threading.RLock()


def _current_directory_signature():
    try:
        stat_result = os.stat(CSV_DATA_FILE_PATH)
    except OSError:
        return None
    return (stat_result.st_mtime_ns, stat_result.st_size)


def _reload(directory_signature=None):
    """Reload discovery data while preserving objects imported by the dashboard."""
    global _file_options, _directory_signature, _cache_initialized

    names = []
    all_params = []
    key_params = []
    if os.path.isdir(CSV_DATA_FILE_PATH):
        for fn in sorted(os.listdir(CSV_DATA_FILE_PATH)):
            if not fn.lower().endswith(FORECAST_EXT):
                continue
            key = _parse_forecast_filename(fn)
            if key is None:
                continue
            names.append(fn)
            all_params.append(fn.replace(FORECAST_EXT, "").split(PARTITION))
            key_params.append(key)

    opts = {k: set() for k in FILE_NAME_PARAMETERS}
    for key in key_params:
        for parameter in FILE_NAME_PARAMETERS:
            value = key.get(parameter)
            if value is not None:
                opts[parameter].add(value)
    option_values = {k: sorted(values) for k, values in opts.items()}

    with _cache_lock:
        # dash_app imports these list objects, so mutate them rather than
        # rebinding them when new forecast files arrive.
        _file_names[:] = names
        _file_all_params[:] = all_params
        _file_key_params[:] = key_params
        if _file_options is None:
            _file_options = option_values
        else:
            _file_options.clear()
            _file_options.update(option_values)
        _directory_signature = directory_signature if directory_signature is not None else _current_directory_signature()
        _cache_initialized = True


def _ensure_fresh():
    signature = _current_directory_signature()
    with _cache_lock:
        if _cache_initialized and signature == _directory_signature:
            return
        _reload(signature)


def get_file_names():
    _ensure_fresh()
    return _file_names


def get_file_key_params():
    _ensure_fresh()
    return _file_key_params


def get_options():
    _ensure_fresh()
    return _file_options


def forecast_file_path(chosen_key_params):
    """chosen_key_params: list of values (order: regions,pollutants,timeScopes,models,date)
    returns full path if exists, else None
    """
    _ensure_fresh()

    matches = []
    for fn, key_params in zip(_file_names, _file_key_params):
        values = [key_params.get(k) for k in FILE_NAME_PARAMETERS]
        if values == chosen_key_params:
            full = os.path.join(CSV_DATA_FILE_PATH, fn)
            if os.path.exists(full):
                matches.append((fn, key_params, full))
    if not matches:
        return None
    matches.sort(key=lambda item: (_run_sort_value(item[1].get("runHour")), item[0]))
    return matches[-1][2]


def forecast_file_paths(chosen_key_params):
    """Return all matching CSV paths for a selection, ordered by run time."""
    _ensure_fresh()

    matches = []
    for fn, key_params in zip(_file_names, _file_key_params):
        values = [key_params.get(k) for k in FILE_NAME_PARAMETERS]
        if values == chosen_key_params:
            full = os.path.join(CSV_DATA_FILE_PATH, fn)
            if os.path.exists(full):
                matches.append(
                    {
                        "path": full,
                        "fileName": fn,
                        "runHour": key_params.get("runHour") or "",
                        "date": key_params.get("date") or "",
                    }
                )
    matches.sort(key=lambda item: (_run_sort_value(item.get("runHour")), item.get("fileName") or ""))
    return matches
