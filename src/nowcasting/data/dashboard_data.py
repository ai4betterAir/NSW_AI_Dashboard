import os
import re
from functools import lru_cache

from nowcasting.config.paths import SITE_DETAILS_JS

SITE_DETAILS_PATH = SITE_DETAILS_JS


POLLUTANTS = {
    "O3": {
        "ParameterCode": "OZONE",
        "label": "O3",
        "ParameterDescription": "Ozone",
        "Units": "pphm",
        "categories": [
            {"label": "No data", "range": "N/A", "color": "#9ca3af"},
            {"label": "Good", "range": "< 5.4", "color": "#16a34a"},
            {"label": "Fair", "range": "5.4-8.0", "color": "#facc15"},
            {"label": "Poor", "range": "8.0 - 12.0", "color": "#f97316"},
            {"label": "Very poor", "range": "12.0-16.0", "color": "#ef4444"},
            {"label": "Extremely poor", "range": "> 16.0", "color": "#7f1d1d"},
        ],
    },
    "PM2.5": {
        "ParameterCode": "PM2.5",
        "label": "PM2.5",
        "ParameterDescription": "PM 2.5",
        "Units": "µg/m³",
        "categories": [
            {"label": "No data", "range": "N/A", "color": "#9ca3af"},
            {"label": "Good", "range": "< 25", "color": "#16a34a"},
            {"label": "Fair", "range": "25 - 50", "color": "#facc15"},
            {"label": "Poor", "range": "50 - 100", "color": "#f97316"},
            {"label": "Very poor", "range": "100 - 300", "color": "#ef4444"},
            {"label": "Extremely poor", "range": "> 300", "color": "#7f1d1d"},
        ],
    },
    "PM10": {
        "ParameterCode": "PM10",
        "label": "PM10",
        "ParameterDescription": "PM 10",
        "Units": "µg/m³",
        "categories": [
            {"label": "No data", "range": "N/A", "color": "#9ca3af"},
            {"label": "Good", "range": "< 50", "color": "#16a34a"},
            {"label": "Fair", "range": "50 - 100", "color": "#facc15"},
            {"label": "Poor", "range": "100 - 200", "color": "#f97316"},
            {"label": "Very poor", "range": "200 - 600", "color": "#ef4444"},
            {"label": "Extremely poor", "range": "> 600", "color": "#7f1d1d"},
        ],
    },
    "NO2": {
        "ParameterCode": "NO2",
        "label": "NO2",
        "ParameterDescription": "Nitrogen dioxide",
        "Units": "pphm",
        "categories": [
            {"label": "No data", "range": "N/A", "color": "#9ca3af"},
        ],
    },
    "SO2": {
        "ParameterCode": "SO2",
        "label": "SO2",
        "ParameterDescription": "Sulfur dioxide",
        "Units": "pphm",
        "categories": [
            {"label": "No data", "range": "N/A", "color": "#9ca3af"},
        ],
    },
    "CO": {
        "ParameterCode": "CO",
        "label": "CO",
        "ParameterDescription": "Carbon monoxide",
        "Units": "ppm",
        "categories": [
            {"label": "No data", "range": "N/A", "color": "#9ca3af"},
        ],
    },
}


def normalize_station_name(name):
    if not name:
        return ""
    cleaned = str(name).replace("_", " ").upper()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def title_case_station_name(name):
    if not name:
        return ""
    cleaned = str(name).replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title()


def _extract_bracket_block(text, marker):
    start = text.find(marker)
    if start < 0:
        return None
    start = text.find("[", start)
    if start < 0:
        return None
    depth = 0
    for idx in range(start, len(text)):
        char = text[idx]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start + 1 : idx]
    return None


def _iter_object_texts(array_body):
    if not array_body:
        return []
    objects = []
    depth = 0
    start = None
    for idx, char in enumerate(array_body):
        if char == "{":
            if depth == 0:
                start = idx + 1
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(array_body[start:idx])
                start = None
    return objects


def _parse_scalar(value):
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    if value in ("true", "false"):
        return value == "true"
    if value == "null":
        return None
    if re.match(r"^-?\d+$", value):
        return int(value)
    if re.match(r"^-?\d+\.\d+$", value):
        return float(value)
    return value


@lru_cache(maxsize=1)
def load_sites():
    if not SITE_DETAILS_JS.exists():
        return []
    text = SITE_DETAILS_JS.read_text(encoding="utf-8", errors="ignore")
    body = _extract_bracket_block(text, "const sitesDetails = [")
    sites = []
    for obj_text in _iter_object_texts(body):
        site = {}
        for match in re.finditer(
            r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\"[^\"]*\"|'[^']*'|-?\d+(?:\.\d+)?|null|true|false)",
            obj_text,
        ):
            site[match.group(1)] = _parse_scalar(match.group(2))
        if site:
            if "SiteName" in site:
                site["SiteName"] = title_case_station_name(site["SiteName"])
            sites.append(site)
    return sites


@lru_cache(maxsize=1)
def load_site_lookup():
    lookup = {}
    for site in load_sites():
        key = normalize_station_name(site.get("SiteName"))
        if key:
            lookup[key] = site
    return lookup


@lru_cache(maxsize=1)
def load_region_lookup():
    """Return mapping of region label (e.g. CE) -> region name (e.g. Sydney East)."""
    if not SITE_DETAILS_JS.exists():
        return {}
    text = SITE_DETAILS_JS.read_text(encoding="utf-8", errors="ignore")
    body = _extract_bracket_block(text, "const regionDetails = [")
    lookup = {}
    for obj_text in _iter_object_texts(body):
        region = {}
        for match in re.finditer(
            r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(\"[^\"]*\"|'[^']*'|-?\d+(?:\.\d+)?|null|true|false)",
            obj_text,
        ):
            region[match.group(1)] = _parse_scalar(match.group(2))
        label = str(region.get("label") or "").strip()
        name = str(region.get("name") or "").strip()
        if label and name:
            lookup[label] = name
    return lookup


def pollutant_by_label(label):
    return POLLUTANTS.get(label)


def pollutant_by_code(code):
    for pollutant in POLLUTANTS.values():
        if pollutant.get("ParameterCode") == code:
            return pollutant
    return None


def category_for_value(pollutant_label, value):
    pollutant = pollutant_by_label(pollutant_label) or pollutant_by_code(pollutant_label)
    if not pollutant:
        return "no-data", "#9ca3af"
    if value is None:
        return "no-data", "#9ca3af"

    for category in pollutant["categories"]:
        rng = category["range"].strip()
        if rng == "N/A":
            continue
        if rng.startswith("<"):
            upper = float(rng[1:].strip())
            if value < upper:
                return category["label"].replace(" ", "-").lower(), category["color"]
        elif rng.startswith(">"):
            lower = float(rng[1:].strip())
            if value > lower:
                return category["label"].replace(" ", "-").lower(), category["color"]
        else:
            parts = [float(part.strip()) for part in rng.split("-")]
            lower = parts[0]
            upper = parts[1] if len(parts) > 1 else None
            if upper is None:
                if value >= lower:
                    return category["label"].replace(" ", "-").lower(), category["color"]
            elif lower <= value <= upper:
                return category["label"].replace(" ", "-").lower(), category["color"]

    return "no-data", "#9ca3af"


def pollutant_display(pollutant_label):
    pollutant = pollutant_by_label(pollutant_label) or pollutant_by_code(pollutant_label)
    if not pollutant:
        return pollutant_label or ""
    return pollutant.get("ParameterDescription", pollutant_label)
