(function () {
  "use strict";

  const selector = ".overview-station-matrix__viewport";

  function snapViewport(viewport) {
    const row = viewport.querySelector(".overview-station-matrix__row");
    const cards = row ? row.querySelectorAll(".overview-station-matrix__card") : [];
    if (!row || cards.length < 2) return;

    const first = cards[0].getBoundingClientRect();
    const second = cards[1].getBoundingClientRect();
    const pitch = second.left - first.left;
    if (!Number.isFinite(pitch) || pitch <= 0) return;

    const maxScroll = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
    const maxAlignedScroll = Math.max(0, Math.floor((maxScroll + 1) / pitch) * pitch);
    const target = Math.max(0, Math.min(maxAlignedScroll, Math.round(viewport.scrollLeft / pitch) * pitch));
    if (Math.abs(viewport.scrollLeft - target) > 1) {
      viewport.scrollTo({ left: target, top: viewport.scrollTop, behavior: "auto" });
    }
  }

  function bind(viewport) {
    if (!viewport || viewport.dataset.stationSnapBound === "1") return;
    viewport.dataset.stationSnapBound = "1";

    let timer = null;
    viewport.addEventListener(
      "scroll",
      function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(function () {
          snapViewport(viewport);
        }, 120);
      },
      { passive: true }
    );
    viewport.addEventListener("scrollend", function () {
      snapViewport(viewport);
    });
    window.requestAnimationFrame(function () {
      snapViewport(viewport);
    });
    window.setTimeout(function () {
      snapViewport(viewport);
    }, 250);
  }

  function bindAll() {
    document.querySelectorAll(selector).forEach(bind);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindAll, { once: true });
  } else {
    bindAll();
  }

  new MutationObserver(bindAll).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });
})();
