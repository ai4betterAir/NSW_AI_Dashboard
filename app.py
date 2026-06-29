import os
import sys
from pathlib import Path
from functools import lru_cache
from datetime import timedelta

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

SRC_ROOT = Path(__file__).resolve().parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from api.v1.options import router as options_router
from api.v1.files import router as files_router
from api.v1.forecast import router as forecast_router
from api.v1.obs import router as obs_router

app = FastAPI(title="AI Dashboard (Python)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple in-memory cache for API responses
_api_cache = {}
_cache_lock = None

try:
    import threading
    _cache_lock = threading.Lock()
except Exception:
    pass


def get_cached_response(key, max_age_seconds=300):
    """Get cached response if available and not expired."""
    if _cache_lock is None:
        return None
    try:
        with _cache_lock:
            if key in _api_cache:
                cached_data, timestamp = _api_cache[key]
                from datetime import datetime
                if (datetime.now() - timestamp).total_seconds() < max_age_seconds:
                    return cached_data
    except Exception:
        pass
    return None


def set_cached_response(key, data):
    """Cache a response with current timestamp."""
    if _cache_lock is None:
        return
    try:
        with _cache_lock:
            from datetime import datetime
            _api_cache[key] = (data, datetime.now())
    except Exception:
        pass

app.include_router(options_router, prefix="/api")
app.include_router(files_router, prefix="/api")
app.include_router(forecast_router, prefix="/api")
app.include_router(obs_router, prefix="/api")


@app.get("/")
def root():
    return {"message": "AI Dashboard Python API running"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=True)
