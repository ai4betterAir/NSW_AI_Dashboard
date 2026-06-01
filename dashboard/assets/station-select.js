(function () {
  function parseHashParams() {
    var hash = window.location.hash || "";
    if (!hash || hash[0] !== "#") return {};
    var text = hash.slice(1);
    if (!text) return {};
    var params = {};
    text.split("&").forEach(function (part) {
      if (!part) return;
      var idx = part.indexOf("=");
      if (idx < 0) return;
      var key = part.slice(0, idx);
      var value = part.slice(idx + 1);
      if (!key) return;
      params[key] = decodeURIComponent(value || "");
    });
    return params;
  }

  function setHashParam(key, value) {
    var params = parseHashParams();
    params[key] = value;
    var parts = [];
    Object.keys(params).forEach(function (k) {
      if (!params[k]) return;
      parts.push(k + "=" + encodeURIComponent(params[k]));
    });
    var nextHash = parts.length ? "#" + parts.join("&") : "";
    if (window.location.hash !== nextHash) {
      window.history.pushState(null, "", nextHash);
    }
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    window.dispatchEvent(new PopStateEvent("popstate"));
  }

  window.addEventListener("message", function (event) {
    var data = event.data || {};
    if (data.type === "forecast-station-select" && data.station) {
      setHashParam("station", data.station);
      return;
    }
    if (data.type === "monitor-site-select" && data.monitor) {
      setHashParam("monitor", data.monitor);
      return;
    }
  });
})();
