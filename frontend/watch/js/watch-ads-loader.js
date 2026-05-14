/**
 * A-ADS on watch film page: loads unit into #film-ad-modal-host; playback gate lives in film.html.
 * Configure WATCH_ADS_AADS_* in API. JSON also includes aads_auth (login/register: /js/auth-ads-loader.js).
 */
(function () {
  function getApiBase() {
    if (typeof window === "undefined" || window.__REDWOOD_API_BASE__ == null) return "";
    var b = String(window.__REDWOOD_API_BASE__).trim();
    return b ? b.replace(/\/$/, "") : "";
  }

  function watchAdsUrl() {
    var base = getApiBase();
    return (base ? base : "") + "/api/public/watch-ads";
  }

  function injectFilmModalAads(cfg) {
    if (!cfg || !cfg.enabled || !cfg.unit_id) return;
    var host = document.getElementById("film-ad-modal-host");
    if (!host) return;
    var uid = String(cfg.unit_id).replace(/\D/g, "");
    if (!uid) return;
    host.innerHTML = "";
    window.__REDWOOD_FILM_AADS__ = { unitId: uid };
    var wrap = document.createElement("div");
    wrap.className = "watch-aads-modal-frame";
    wrap.style.cssText = "width:100%;margin:auto;text-align:center;";
    var ifr = document.createElement("iframe");
    ifr.setAttribute("data-aa", uid);
    ifr.src = "https://acceptable.a-ads.com/" + encodeURIComponent(uid) + "/?size=Adaptive";
    ifr.style.cssText =
      "border:0;padding:0;width:100%;max-width:520px;height:auto;min-height:120px;overflow:hidden;display:block;margin:auto";
    ifr.title = "Publicité";
    ifr.loading = "lazy";
    ifr.referrerPolicy = "strict-origin-when-cross-origin";
    wrap.appendChild(ifr);
    host.appendChild(wrap);
    try {
      window.dispatchEvent(new CustomEvent("redwood-film-aads-ready", { detail: { unitId: uid } }));
    } catch (_) {}
  }

  function run() {
    if (typeof fetch !== "function") return;
    fetch(watchAdsUrl(), {
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(function (r) {
        if (!r.ok) return null;
        return r.json();
      })
      .then(function (j) {
        if (j && j.aads) injectFilmModalAads(j.aads);
      })
      .catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
