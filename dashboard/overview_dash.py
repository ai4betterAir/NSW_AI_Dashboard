"""Overview tab scaffold.

Place overview layout, render helpers and callbacks here when ready.

Currently this module registers no new callbacks — it provides a
`register_overview(app)` hook that imports the existing `dash_app`
module so behavior remains unchanged until code is moved.
"""

def register_overview(app):
    """Ensure overview-related callbacks/layout from the main app are loaded.

    Call this from your main runner after creating the Dash `app`.
    """
    try:
        import dashboard.dash_app as dash_app
    except Exception:
        # allow running from repo root without package import
        import dash_app as dash_app
    # No-op: importing `dash_app` registers the existing overview callbacks.
    return
