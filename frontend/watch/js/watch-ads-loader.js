/**
 * Loads Coinzilla (or compatible) tag on watch video pages when enabled server-side.
 * Configure WATCH_ADS_COINZILLA_* in API environment; paste script src from Coinzilla publisher dashboard.
 */
(function () {
  function injectCoinzilla(cz) {
    if (!cz || !cz.enabled || !cz.script_src) return;
    var s = document.createElement("script");
    s.async = true;
    s.src = cz.script_src;
    if (cz.zone_id) s.setAttribute("data-zone", cz.zone_id);
    document.head.appendChild(s);
  }

  function run() {
    if (typeof fetch !== "function") return;
    fetch("/api/public/watch-ads", {
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        if (!r.ok) return null;
        return r.json();
      })
      .then(function (j) {
        if (j && j.coinzilla) injectCoinzilla(j.coinzilla);
      })
      .catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
