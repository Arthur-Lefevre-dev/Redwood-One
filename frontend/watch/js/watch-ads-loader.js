/**
 * A-ADS on watch film page: loads unit(s) into #film-ad-modal-host; playback gate lives in film.html.
 * Configure WATCH_ADS_AADS_* in API. Optional WATCH_ADS_AADS_MOBILE_UNIT_ID = separate Adaptive mobile unit.
 * JSON also includes aads_auth (login/register: /js/auth-ads-loader.js).
 */
(function () {
  if (typeof window !== "undefined") {
    window.__REDWOOD_EXPECT_FILM_AD__ = false;
  }

  function getApiBase() {
    if (typeof window === "undefined" || window.__REDWOOD_API_BASE__ == null)
      return "";
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

    var mobileRaw =
      cfg.mobile_unit_id != null ? String(cfg.mobile_unit_id) : "";
    var mobileUid = mobileRaw.replace(/\D/g, "");
    if (!mobileUid || mobileUid === uid) {
      mobileUid = "";
    }

    host.innerHTML = "";
    host.classList.remove("watch-aads--single", "watch-aads--dual");
    host.classList.add(mobileUid ? "watch-aads--dual" : "watch-aads--single");

    var css = document.createElement("style");
    css.textContent =
      "#film-ad-modal-host .watch-aads-film-layout{" +
      "width:100%;margin:auto;position:relative;text-align:center;z-index:99998" +
      "}" +
      "#film-ad-modal-host .watch-aads-film-layout iframe{" +
      "border:0;padding:0;height:auto;overflow:hidden;display:block;margin:auto" +
      "}" +
      "#film-ad-modal-host.watch-aads--single .watch-aads-film-layout iframe{" +
      "width:100%;max-width:520px;min-height:120px" +
      "}" +
      "@media (max-width:768px){" +
      "#film-ad-modal-host.watch-aads--single .watch-aads-film-layout iframe{" +
      "width:70%;max-width:100%;min-height:100px" +
      "}" +
      "}" +
      "#film-ad-modal-host.watch-aads--dual .watch-aads-desktop{display:none}" +
      "#film-ad-modal-host.watch-aads--dual .watch-aads-mobile{display:block}" +
      "@media (min-width:769px){" +
      "#film-ad-modal-host.watch-aads--dual .watch-aads-desktop{display:block}" +
      "#film-ad-modal-host.watch-aads--dual .watch-aads-mobile{display:none}" +
      "}" +
      "#film-ad-modal-host.watch-aads--dual .watch-aads-desktop iframe{" +
      "width:100%;max-width:520px;min-height:120px" +
      "}" +
      "#film-ad-modal-host.watch-aads--dual .watch-aads-mobile iframe{" +
      "width:70%;max-width:100%;min-height:100px" +
      "}";
    host.appendChild(css);

    function makeIframe(idNum) {
      var ifr = document.createElement("iframe");
      ifr.setAttribute("data-aa", idNum);
      ifr.src =
        "https://acceptable.a-ads.com/" +
        encodeURIComponent(idNum) +
        "/?size=Adaptive";
      ifr.title = "Publicité";
      ifr.loading = "lazy";
      ifr.referrerPolicy = "strict-origin-when-cross-origin";
      return ifr;
    }

    function makeLayout(extraClass) {
      var wrap = document.createElement("div");
      wrap.className = "watch-aads-film-layout " + (extraClass || "").trim();
      return wrap;
    }

    if (mobileUid) {
      var dLayout = makeLayout("watch-aads-desktop");
      dLayout.appendChild(makeIframe(uid));
      host.appendChild(dLayout);
      var mLayout = makeLayout("watch-aads-mobile");
      mLayout.appendChild(makeIframe(mobileUid));
      host.appendChild(mLayout);
      window.__REDWOOD_FILM_AADS__ = { unitId: uid, mobileUnitId: mobileUid };
    } else {
      var layout = makeLayout("");
      layout.appendChild(makeIframe(uid));
      host.appendChild(layout);
      window.__REDWOOD_FILM_AADS__ = { unitId: uid };
    }

    if (typeof window !== "undefined") {
      window.__REDWOOD_EXPECT_FILM_AD__ = true;
    }
    try {
      window.dispatchEvent(
        new CustomEvent("redwood-film-aads-ready", {
          detail: { unitId: uid, mobileUnitId: mobileUid || null },
        }),
      );
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
