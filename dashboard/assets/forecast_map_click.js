(function () {
  if (window.__forecastMapClickBridgeInstalled) {
    return;
  }
  window.__forecastMapClickBridgeInstalled = true;

  window.addEventListener("message", function (event) {
    var data = event && event.data ? event.data : {};
    if (data.type !== "forecast-station-select" || !data.station) {
      return;
    }
    var station = encodeURIComponent(String(data.station));
    var nextHash = "#station=" + station;
    if (window.location.hash !== nextHash) {
      window.location.hash = nextHash;
    } else {
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    }
  });
})();
