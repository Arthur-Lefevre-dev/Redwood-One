/**
 * Anti–ad-block hint when A-ADS is enabled server-side but ads are likely blocked.
 * Uses (1) cosmetic bait + triple rAF, (2) Google script probe, (3) film modal iframes when open,
 * (4) auth sticky iframes when visible.
 * Listens for redwood-film-aads-ready / redwood-auth-aads-ready, polls, and re-probes when the film ad modal opens.
 */
(function () {
  var SESSION_KEY = "redwood_adblock_notice_dismiss";
  var UI_ID = "redwood-adblock-notice";
  var pollTimer = null;
  var lastFullProbe = 0;
  var PROBE_THROTTLE_MS = 3500;

  function expectsAds() {
    return !!(window.__REDWOOD_EXPECT_FILM_AD__ || window.__REDWOOD_EXPECT_AUTH_AD__);
  }

  function filmAdModalIsUsable() {
    var modal = document.getElementById("film-ad-modal");
    if (!modal) return false;
    if (modal.hidden) return false;
    try {
      if (window.getComputedStyle(modal).display === "none") return false;
    } catch (_) {
      return false;
    }
    return true;
  }

  /**
   * Cosmetic bait aligned with common EasyList / uBlock cosmetic rules.
   * Measure after three animation frames so filter engines have applied.
   */
  function cosmeticLikelyBlocked(cb) {
    if (!document.body) {
      cb(false);
      return;
    }
    var root = document.createElement("div");
    root.setAttribute("aria-hidden", "true");
    root.style.cssText =
      "position:absolute!important;left:-9999px!important;top:0!important;width:336px!important;height:280px!important;overflow:hidden!important;pointer-events:none!important;";

    var a = document.createElement("div");
    a.id = "google_ads_test";
    a.className = "adsbox pub_300x250 ad-banner textads banner-ads";
    a.textContent = "\u00a0";
    root.appendChild(a);

    var ins = document.createElement("ins");
    ins.className = "adsbygoogle";
    ins.setAttribute("data-ad-client", "ca-pub-0000000000000000");
    ins.style.cssText = "display:block!important;width:300px!important;height:250px!important;";
    root.appendChild(ins);

    document.body.appendChild(root);

    function measure() {
      var blocked = false;
      try {
        [root, a, ins].forEach(function (el) {
          var cs = window.getComputedStyle(el);
          var r = el.getBoundingClientRect();
          if (cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity) === 0) blocked = true;
          if (r.width < 2 || r.height < 2) blocked = true;
        });
      } catch (_) {
        blocked = true;
      }
      try {
        root.remove();
      } catch (_) {}
      cb(blocked);
    }

    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        requestAnimationFrame(measure);
      });
    });
  }

  /**
   * Script URL widely blocked by ad lists; onload => script reached network; onerror => likely blocked.
   * Timeout => inconclusive (do not treat as blocked — avoids false positives on slow networks).
   */
  function scriptProbeLikelyBlocked(cb) {
    var head = document.head || document.documentElement;
    if (!head) {
      cb(false);
      return;
    }
    var s = document.createElement("script");
    var finished = false;
    var t = setTimeout(function () {
      if (finished) return;
      finished = true;
      try {
        s.onload = s.onerror = null;
        s.remove();
      } catch (_) {}
      cb(false);
    }, 3500);

    function done(val) {
      if (finished) return;
      finished = true;
      clearTimeout(t);
      try {
        s.onload = s.onerror = null;
        s.remove();
      } catch (_) {}
      cb(val);
    }

    s.onload = function () {
      done(false);
    };
    s.onerror = function () {
      done(true);
    };
    s.src = "https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js";
    head.appendChild(s);
  }

  /** Film: visible modal + plausible iframe box suggests blocked network/cosmetic (ignore when modal closed). */
  function filmIframeLooksSuppressed() {
    if (!filmAdModalIsUsable()) return false;
    var host = document.getElementById("film-ad-modal-host");
    if (!host) return false;
    var list = host.querySelectorAll("iframe[data-aa]");
    if (!list || !list.length) return false;
    for (var i = 0; i < list.length; i++) {
      var ifr = list[i];
      try {
        var wrap = ifr.closest(".watch-aads-film-layout");
        if (wrap && window.getComputedStyle(wrap).display === "none") continue;
        var r = ifr.getBoundingClientRect();
        var cs = window.getComputedStyle(ifr);
        if (cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity) === 0) return true;
        if (r.width < 4 && r.height < 4) return true;
      } catch (_) {
        return true;
      }
    }
    return false;
  }

  /** Auth pages: visible sticky mount but iframe collapsed (blocked). */
  function authStickyIframeSuppressed() {
    if (!window.__REDWOOD_EXPECT_AUTH_AD__) return false;
    var list = document.querySelectorAll(
      ".redwood-aads-auth-mount .redwood-aads-auth-iframe[data-aa], .redwood-aads-auth-mount .redwood-aads-auth-top-iframe[data-aa]",
    );
    if (!list || !list.length) return false;
    for (var i = 0; i < list.length; i++) {
      var ifr = list[i];
      try {
        var mount = ifr.closest(".redwood-aads-auth-mount");
        if (!mount || window.getComputedStyle(mount).display === "none") continue;
        var r = ifr.getBoundingClientRect();
        var cs = window.getComputedStyle(ifr);
        if (cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity) === 0) return true;
        if (r.width < 3 && r.height < 3) return true;
      } catch (_) {
        return true;
      }
    }
    return false;
  }

  function combinedLikelyBlocked(cb) {
    var cDone = false;
    var sDone = false;
    var cVal = false;
    var sVal = false;
    function finish() {
      if (!cDone || !sDone) return;
      var film = filmIframeLooksSuppressed();
      var auth = authStickyIframeSuppressed();
      cb(!!(cVal || sVal || film || auth));
    }
    cosmeticLikelyBlocked(function (c) {
      cVal = c;
      cDone = true;
      finish();
    });
    scriptProbeLikelyBlocked(function (s) {
      sVal = s;
      sDone = true;
      finish();
    });
  }

  function dismiss(root) {
    try {
      sessionStorage.setItem(SESSION_KEY, "1");
    } catch (_) {}
    if (root && root.parentNode) root.parentNode.removeChild(root);
  }

  function mountUI() {
    if (document.getElementById(UI_ID)) return;
    if (sessionStorage.getItem(SESSION_KEY) === "1") return;

    var wrap = document.createElement("div");
    wrap.id = UI_ID;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", "Message sur le financement du site");
    var zFilm = document.getElementById("film-ad-modal") ? "10035" : "99999";
    wrap.style.cssText =
      "position:fixed;left:0;right:0;bottom:0;z-index:" +
      zFilm +
      ";padding:12px 16px 16px;box-sizing:border-box;font-family:system-ui,-apple-system,sans-serif;font-size:14px;line-height:1.45;color:#eee;background:rgba(18,18,20,.96);border-top:1px solid #333;box-shadow:0 -8px 32px rgba(0,0,0,.45);";

    var inner = document.createElement("div");
    inner.style.cssText =
      "max-width:720px;margin:0 auto;display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;justify-content:space-between;";
    wrap.appendChild(inner);

    var p = document.createElement("p");
    p.style.cssText = "margin:0;flex:1;min-width:min(100%,280px);";
    p.textContent =
      "Héberger et diffuser des vidéos coûte cher en bande passante et en infrastructure. Nous n’affichons que des encarts discrets (partenaires comme A-ADS), pas de publicité intrusive. Si un bloqueur de pubs masque ces encarts, merci d’ajouter une exception pour ce site : cela aide à garder le service disponible pour tout le monde.";
    inner.appendChild(p);

    var btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "J’ai compris";
    btn.style.cssText =
      "flex-shrink:0;padding:10px 18px;border-radius:10px;border:1px solid #444;background:#2a2a2e;color:#fff;font:inherit;cursor:pointer;";
    btn.addEventListener("click", function () {
      dismiss(wrap);
    });
    inner.appendChild(btn);

    (document.body || document.documentElement).appendChild(wrap);
  }

  function stopPollIfDone() {
    if (document.getElementById(UI_ID) || sessionStorage.getItem(SESSION_KEY) === "1") {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    }
  }

  function tryMount(force) {
    stopPollIfDone();
    if (document.getElementById(UI_ID)) return;
    if (sessionStorage.getItem(SESSION_KEY) === "1") return;
    if (!expectsAds()) return;
    var now = Date.now();
    if (!force && now - lastFullProbe < PROBE_THROTTLE_MS) return;
    lastFullProbe = now;

    combinedLikelyBlocked(function (blocked) {
      if (!blocked) return;
      if (!expectsAds()) return;
      if (sessionStorage.getItem(SESSION_KEY) === "1") return;
      mountUI();
      stopPollIfDone();
    });
  }

  function startPoll() {
    if (pollTimer) return;
    var deadline = Date.now() + 38000;
    pollTimer = setInterval(function () {
      if (Date.now() > deadline) {
        clearInterval(pollTimer);
        pollTimer = null;
        return;
      }
      tryMount(false);
    }, 900);
  }

  function schedule() {
    tryMount(true);
    [400, 1200, 2400, 4000, 8000].forEach(function (ms) {
      setTimeout(function () {
        tryMount(false);
      }, ms);
    });
    startPoll();
  }

  function onAdsReady() {
    lastFullProbe = 0;
    setTimeout(function () {
      tryMount(true);
    }, 50);
    setTimeout(function () {
      tryMount(true);
    }, 600);
    setTimeout(function () {
      tryMount(true);
    }, 2000);
  }

  function observeFilmAdModalOpen() {
    var modal = document.getElementById("film-ad-modal");
    if (!modal || modal.dataset.redwoodAdblockObs) return;
    modal.dataset.redwoodAdblockObs = "1";
    try {
      new MutationObserver(function () {
        if (!expectsAds() || !filmAdModalIsUsable()) return;
        lastFullProbe = 0;
        setTimeout(function () {
          tryMount(true);
        }, 150);
      }).observe(modal, { attributes: true, attributeFilter: ["hidden"] });
    } catch (_) {}
  }

  window.addEventListener("redwood-film-aads-ready", onAdsReady);
  window.addEventListener("redwood-auth-aads-ready", onAdsReady);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", schedule);
    document.addEventListener("DOMContentLoaded", observeFilmAdModalOpen);
  } else {
    schedule();
    observeFilmAdModalOpen();
  }
})();
