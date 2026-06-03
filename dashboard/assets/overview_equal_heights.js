(function () {
  const STATUS_SELECTOR = ".overview-area--status";
  const FORECAST_SELECTOR = ".overview-area--next3";

  function elementsVisible(el) {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function sideBySide(a, b) {
    const ra = a.getBoundingClientRect();
    const rb = b.getBoundingClientRect();
    return Math.abs(ra.top - rb.top) < 24;
  }

  function equalizeOverviewCardHeights() {
    const status = document.querySelector(STATUS_SELECTOR);
    const forecast = document.querySelector(FORECAST_SELECTOR);
    if (!status || !forecast) return;

    // Reset first so we measure natural heights.
    status.style.minHeight = "";
    forecast.style.minHeight = "";

    if (!elementsVisible(status) || !elementsVisible(forecast)) return;
    // If stacked vertically (mobile), don't force equal heights.
    if (!sideBySide(status, forecast)) return;

    const target = Math.max(status.offsetHeight, forecast.offsetHeight);
    status.style.minHeight = `${target}px`;
    forecast.style.minHeight = `${target}px`;
  }

  let scheduled = false;
  function schedule() {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(() => {
      scheduled = false;
      equalizeOverviewCardHeights();
    });
  }

  function installObserver() {
    // Dash updates the DOM as callbacks complete; observe and re-equalize.
    const root = document.getElementById("dashboard-page") || document.body;
    const observer = new MutationObserver(schedule);
    observer.observe(root, { subtree: true, childList: true, characterData: true });
  }

  window.addEventListener("resize", schedule, { passive: true });
  window.addEventListener("load", () => {
    schedule();
    installObserver();
  });
})();

