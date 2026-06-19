(function () {
  const STATUS_SELECTOR = ".overview-area--status";
  const FORECAST_SELECTOR = ".overview-area--next3";

  function equalizeOverviewCardHeights() {
    const status = document.querySelector(STATUS_SELECTOR);
    const forecast = document.querySelector(FORECAST_SELECTOR);
    if (!status || !forecast) return;

    // The Overview now uses an independent left-column grid; keep natural
    // card heights so the regional chart can sit directly below the top cards.
    status.style.minHeight = "";
    forecast.style.minHeight = "";
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
