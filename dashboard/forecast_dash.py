"""Forecast tab scaffold (optional).

Move forecast-specific layout and callbacks here if you want to separate
forecast logic from the main app. Exports `register_forecast(app)`.
"""

def register_forecast(app):
    """Import existing forecast callbacks/layout so behavior remains.

    Call this from your main runner after creating the Dash `app`.
    """
    try:
        import dashboard.dash_app as dash_app
    except Exception:
        import dash_app as dash_app
    return
