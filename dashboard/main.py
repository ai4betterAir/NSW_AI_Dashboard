"""App runner / factory that imports tab modules and runs the Dash app.

Use `python -m dashboard.main` to run the dashboard. This keeps a single
`dash.Dash` instance defined in `dash_app.py` while letting you split
tab logic into separate modules.
"""
from __future__ import annotations

try:
    # preferred package import
    from dashboard import dash_app
except Exception:
    import dash_app

from dashboard import overview_dash, mon_dash, forecast_dash


def create_app():
    app = dash_app.app
    # call register hooks (no-ops today; useful once code is moved)
    overview_dash.register_overview(app)
    mon_dash.register_monitoring(app)
    try:
        forecast_dash.register_forecast(app)
    except Exception:
        pass
    return app


if __name__ == "__main__":
    import os

    app = create_app()
    # Allow overriding port/debug via environment variables
    port = int(os.environ.get("DASHBOARD_PORT", "8050"))
    debug = os.environ.get("DASHBOARD_DEBUG", "0") in ("1", "true", "True")
    # Use the modern API: app.run
    app.run(host="0.0.0.0", port=port, debug=debug)
