(function () {
  const STORE_ID = "overview-trends-visible";
  let observer = null;
  // Keep an in-memory copy of the store so we can accumulate regions without reading Dash internals.
  // Must match the initial `dcc.Store(id="overview-trends-visible")` defaults in `dashboard/dash_app.py`.
  if (!Array.isArray(window.__overviewTrendsVisible) || !window.__overviewTrendsVisible.length) {
    window.__overviewTrendsVisible = ["Sydney_East"];
  }

  function setStore(data) {
    try {
      if (window.dash_clientside && typeof window.dash_clientside.set_props === "function") {
        window.dash_clientside.set_props(STORE_ID, { data: data });
      }
    } catch (e) {
      // Ignore failures during early page init.
    }
  }

  function ensureObserver(root) {
    if (observer) return observer;
    observer = new IntersectionObserver(
      (entries) => {
        // Accumulate regions that are actually on screen (so scrolling always loads the next card).
        const prev = Array.isArray(window.__overviewTrendsVisible) ? window.__overviewTrendsVisible : [];
        const next = prev.slice();

        const sorted = entries
          .filter((e) => e && e.isIntersecting && (e.intersectionRatio || 0) > 0.01)
          .sort((a, b) => (b.intersectionRatio || 0) - (a.intersectionRatio || 0));

        for (const entry of sorted) {
          const region = entry.target && entry.target.dataset ? entry.target.dataset.region : null;
          if (!region) continue;
          if (next.indexOf(region) === -1) next.push(region);
        }

        // Keep only 1 region active at a time (default shows one; scrolling loads the next).
        const limited = next.slice(-1);
        window.__overviewTrendsVisible = limited;
        if (limited.join("|") !== prev.join("|")) setStore(limited);
      },
      {
        root: root,
        // Be more forgiving so scrolling quickly still updates the visible region.
        rootMargin: "80px 0px 80px 0px",
        threshold: [0, 0.15, 0.3, 0.6],
      }
    );
    return observer;
  }

  function attach() {
    const container = document.querySelector(".overview-trends-grid");
    if (!container) return;

    const targets = container.querySelectorAll(".overview-trends-observe[data-region]");
    if (!targets || !targets.length) return;

    const obs = ensureObserver(container);
    for (const t of targets) {
      if (t.dataset && t.dataset.observed === "1") continue;
      if (t.dataset) t.dataset.observed = "1";
      obs.observe(t);
    }
  }

  function boot() {
    attach();
    const mo = new MutationObserver(() => attach());
    mo.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
