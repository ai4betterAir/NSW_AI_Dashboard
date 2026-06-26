(function () {
  const MAP_SELECTOR = ".overview-area--forecast-map";
  const OUTLOOK_SELECTOR = ".overview-area--trends";
  const OBSERVATION_MAP_SELECTOR = ".overview-area--observation-map";
  const OBSERVED_SELECTOR = ".overview-area--observed";

  function equalizeOverviewCardHeights() {
    const map = document.querySelector(MAP_SELECTOR);
    const outlook = document.querySelector(OUTLOOK_SELECTOR);
    const observationMap = document.querySelector(OBSERVATION_MAP_SELECTOR);
    const observed = document.querySelector(OBSERVED_SELECTOR);

    if (window.innerWidth <= 760) {
      if (outlook) outlook.style.height = "";
      if (observed) observed.style.height = "";
      return;
    }

    if (map && outlook) {
      const mapHeight = Math.ceil(map.getBoundingClientRect().height);
      if (mapHeight > 0 && Math.abs(outlook.getBoundingClientRect().height - mapHeight) > 1) {
        outlook.style.height = `${mapHeight}px`;
      }
    }

    if (observationMap && observed) {
      const observationHeight = Math.ceil(observationMap.getBoundingClientRect().height);
      if (observationHeight > 0 && Math.abs(observed.getBoundingClientRect().height - observationHeight) > 1) {
        observed.style.height = `${observationHeight}px`;
      }
    }
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
    const root = document.getElementById("dashboard-page") || document.body;
    const observer = new MutationObserver(schedule);
    observer.observe(root, { subtree: true, childList: true, characterData: true });

    const map = document.querySelector(MAP_SELECTOR);
    if (map && window.ResizeObserver) {
      new ResizeObserver(schedule).observe(map);
    }
    const observationMap = document.querySelector(OBSERVATION_MAP_SELECTOR);
    if (observationMap && window.ResizeObserver) {
      new ResizeObserver(schedule).observe(observationMap);
    }
  }

  window.addEventListener("resize", schedule, { passive: true });
  window.addEventListener("load", () => {
    schedule();
    installObserver();
  });
})();
