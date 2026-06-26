(function () {
  const TARGET_ID = "overview-monitor-load";
  const REFRESH_MS = 20 * 60 * 1000;
  let tick = 1000;

  function bumpOverviewObservedRefresh() {
    try {
      if (!window.dash_clientside || typeof window.dash_clientside.set_props !== "function") {
        return;
      }
      // The Python callback already checks the active tab. Updating this prop
      // simply wakes the Overview AQMS path after the startup-only Interval stops.
      tick += 1;
      window.dash_clientside.set_props(TARGET_ID, { n_intervals: tick });
    } catch (e) {
      // Keep this asset silent; the next timer tick can retry.
    }
  }

  function boot() {
    window.setInterval(bumpOverviewObservedRefresh, REFRESH_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
