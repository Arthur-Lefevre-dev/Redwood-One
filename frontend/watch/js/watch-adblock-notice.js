/**
 * Hard anti–ad-block gate when A-ADS is enabled server-side but ads are likely blocked.
 * Full-screen lock until probes show ads are no longer blocked (no dismiss bypass).
 */
(function () {
  try {
    sessionStorage.removeItem("redwood_adblock_notice_dismiss");
  } catch (_) {}

  var UI_ID = "redwood-adblock-notice";
  var LOCK_CLASS = "redwood-adblock-locked";
  var STYLE_ID = "redwood-adblock-notice-style";
  var pollTimer = null;
  var lastFullProbe = 0;
  var PROBE_THROTTLE_MS = 2500;

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

  function ensureStyles() {
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent =
      "html." +
      LOCK_CLASS +
      ",html." +
      LOCK_CLASS +
      " body{overflow:hidden!important;height:100%!important}" +
      "#" +
      UI_ID +
      "{position:fixed;inset:0;z-index:2147483646;display:flex;align-items:center;justify-content:center;padding:24px 20px;box-sizing:border-box;background:rgba(6,6,8,.94);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}" +
      "#" +
      UI_ID +
      " .rw-adblock-panel{max-width:520px;width:100%;padding:28px 26px;border-radius:16px;border:1px solid rgba(139,37,0,.55);background:linear-gradient(165deg,#1a1210 0%,#121214 55%,#0c0c0e 100%);box-shadow:0 24px 64px rgba(0,0,0,.55);color:#f5f5f5;text-align:center}" +
      "#" +
      UI_ID +
      " .rw-adblock-title{margin:0 0 14px;font-size:1.2rem;font-weight:700;color:#fecaca;letter-spacing:.02em}" +
      "#" +
      UI_ID +
      " .rw-adblock-text{margin:0 0 20px;font-size:14px;line-height:1.55;color:#d4d4d4}" +
      "#" +
      UI_ID +
      " .rw-adblock-hint{margin:0 0 18px;font-size:12px;line-height:1.45;color:#a3a3a3}" +
      "#" +
      UI_ID +
      " .rw-adblock-actions{display:flex;flex-wrap:wrap;gap:10px;justify-content:center}" +
      "#" +
      UI_ID +
      " .rw-adblock-btn{padding:11px 20px;border-radius:10px;border:1px solid rgba(139,37,0,.65);background:rgba(139,37,0,.35);color:#fff;font:inherit;font-weight:600;cursor:pointer}" +
      "#" +
      UI_ID +
      " .rw-adblock-btn:hover{background:rgba(139,37,0,.5)}" +
      "#" +
      UI_ID +
      " .rw-adblock-spinner{width:36px;height:36px;margin:0 auto 16px;border-radius:50%;border:3px solid rgba(255,255,255,.12);border-top-color:#fecaca;animation:rw-adblock-spin .85s linear infinite}" +
      "@keyframes rw-adblock-spin{to{transform:rotate(360deg)}}" +
      "@media (prefers-reduced-motion:reduce){#" +
      UI_ID +
      " .rw-adblock-spinner{animation:none;opacity:.7}}";
    (document.head || document.documentElement).appendChild(s);
  }

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

  function setPageLocked(locked) {
    var root = document.documentElement;
    if (!root) return;
    if (locked) root.classList.add(LOCK_CLASS);
    else root.classList.remove(LOCK_CLASS);
  }

  function unmountUI() {
    var el = document.getElementById(UI_ID);
    if (el && el.parentNode) el.parentNode.removeChild(el);
    setPageLocked(false);
  }

  function mountUI() {
    if (document.getElementById(UI_ID)) {
      setPageLocked(true);
      return;
    }
    ensureStyles();

    var wrap = document.createElement("div");
    wrap.id = UI_ID;
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-modal", "true");
    wrap.setAttribute("aria-labelledby", "rw-adblock-title");
    wrap.tabIndex = -1;

    var panel = document.createElement("div");
    panel.className = "rw-adblock-panel";

    var spinner = document.createElement("div");
    spinner.className = "rw-adblock-spinner";
    spinner.setAttribute("aria-hidden", "true");
    panel.appendChild(spinner);

    var title = document.createElement("h2");
    title.id = "rw-adblock-title";
    title.className = "rw-adblock-title";
    title.textContent = "Bloqueur de publicités détecté";
    panel.appendChild(title);

    var p = document.createElement("p");
    p.className = "rw-adblock-text";
    p.textContent =
      "Héberger et diffuser des vidéos coûte cher en bande passante et en infrastructure. Redwood Plus s’appuie sur de petits encarts discrets (partenaires comme A-ADS), pas sur de la publicité intrusive.";
    panel.appendChild(p);

    var hint = document.createElement("p");
    hint.className = "rw-adblock-hint";
    hint.textContent =
      "Pour continuer, désactivez votre bloqueur de publicités sur ce site ou ajoutez une exception pour ce domaine. L’accès se débloquera automatiquement dès que les encarts pourront s’afficher.";
    panel.appendChild(hint);

    var actions = document.createElement("div");
    actions.className = "rw-adblock-actions";

    var retry = document.createElement("button");
    retry.type = "button";
    retry.className = "rw-adblock-btn";
    retry.textContent = "Vérifier à nouveau";
    retry.addEventListener("click", function () {
      lastFullProbe = 0;
      runProbe(true);
    });
    actions.appendChild(retry);
    panel.appendChild(actions);

    wrap.appendChild(panel);
    (document.body || document.documentElement).appendChild(wrap);
    setPageLocked(true);
    try {
      wrap.focus();
    } catch (_) {}
  }

  function runProbe(force) {
    if (!expectsAds()) {
      unmountUI();
      return;
    }
    var now = Date.now();
    if (!force && now - lastFullProbe < PROBE_THROTTLE_MS) return;
    lastFullProbe = now;

    combinedLikelyBlocked(function (blocked) {
      if (!expectsAds()) {
        unmountUI();
        return;
      }
      if (blocked) mountUI();
      else unmountUI();
    });
  }

  function startPoll() {
    if (pollTimer) return;
    pollTimer = setInterval(function () {
      runProbe(false);
    }, 1200);
  }

  function schedule() {
    runProbe(true);
    [400, 1200, 2400, 5000, 10000].forEach(function (ms) {
      setTimeout(function () {
        runProbe(false);
      }, ms);
    });
    startPoll();
  }

  function onAdsReady() {
    lastFullProbe = 0;
    setTimeout(function () {
      runProbe(true);
    }, 50);
    setTimeout(function () {
      runProbe(true);
    }, 800);
    setTimeout(function () {
      runProbe(true);
    }, 2500);
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
          runProbe(true);
        }, 200);
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
