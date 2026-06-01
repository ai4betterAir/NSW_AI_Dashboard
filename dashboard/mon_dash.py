"""Monitoring tab scaffold.

Place monitoring layout, render helpers and callbacks here when ready.

This module exposes `register_monitoring(app)` which currently imports
the existing `dash_app` so the current monitoring callbacks stay active.
"""

def register_monitoring(app):
    """Ensure monitoring-related callbacks/layout from the main app are loaded.

    Call this from your main runner after creating the Dash `app`.
    """
    try:
        import dashboard.dash_app as dash_app
    except Exception:
        import dash_app as dash_app
    return
