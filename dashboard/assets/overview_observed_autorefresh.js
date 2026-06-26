(function () {
  const TARGET_ID = "overview-monitor-load";
  const REFRESH_MS = 5 * 60 * 1000;
  let tick = 1000;

  function overviewTabIsActive() {
    try {
      const selected = document.querySelector('.dashboard-tab--selected');
      return !selected || String(selected.textContent || '').toLowerCase().includes('overview');
    } catch (e) {
      return true;
    }
  }

  function bumpOverviewObservedRefresh() {
    try {
      if (!overviewTabIsActive()) return;
      if (!window.dash_clientside || typeof window.dash_clientside.set_props !== "function") {
        return;
      }
      // The Python callback checks the active tab again. Updating this prop wakes
      // the Overview AQMS loader after the startup-only dcc.Interval has stopped.
      tick += 1;
      window.dash_clientside.set_props(TARGET_ID, { n_intervals: tick });
    } catch (e) {
      // Keep this asset silent; the next timer tick can retry.
    }
  }

  function boot() {
    // Do one extra refresh shortly after page load, then continue periodically.
    window.setTimeout(bumpOverviewObservedRefresh, 4000);
    window.setInterval(bumpOverviewObservedRefresh, REFRESH_MS);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) window.setTimeout(bumpOverviewObservedRefresh, 1000);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
