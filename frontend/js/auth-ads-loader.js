/**
 * A-ADS on login / register: optional sticky bottom (mobile-first) + optional sticky top (desktop).
 * Reads /api/public/watch-ads → aads_auth (WATCH_ADS_AADS_AUTH_* and WATCH_ADS_AADS_AUTH_TOP_*).
 */
(function () {
  if (typeof window !== "undefined") {
    window.__REDWOOD_EXPECT_AUTH_AD__ = false;
  }

  function getApiBase() {
    if (typeof window === "undefined" || window.__REDWOOD_API_BASE__ == null) return "";
    var b = String(window.__REDWOOD_API_BASE__).trim();
    return b ? b.replace(/\/$/, "") : "";
  }

  function watchAdsUrl() {
    var base = getApiBase();
    return (base ? base : "") + "/api/public/watch-ads";
  }

  function escCssId(id) {
    if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(id);
    return String(id).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function markExpect() {
    if (typeof window !== "undefined") {
      window.__REDWOOD_EXPECT_AUTH_AD__ = true;
    }
  }

  function appendDismissIcon(label) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("fill", "#000000");
    svg.setAttribute("height", "16");
    svg.setAttribute("width", "16");
    svg.setAttribute("viewBox", "0 0 490 490");
    svg.setAttribute("aria-hidden", "true");
    var poly = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
    poly.setAttribute(
      "points",
      "456.851,0 245,212.564 33.149,0 0.708,32.337 212.669,245.004 0.708,457.678 33.149,490 245,277.443 456.851,490 489.292,457.678 277.331,245.004 489.292,32.337 ",
    );
    svg.appendChild(poly);
    label.appendChild(svg);
  }

  /**
   * Sticky bottom strip (narrow viewports, or all viewports if no desktop-top unit).
   * @param {object} cfg - aads_auth slice with enabled + unit_id
   * @param {boolean} pairWithTop - hide this strip on wide screens when a top unit is also active
   */
  function injectAuthStickyBottom(cfg, pairWithTop) {
    if (!cfg || !cfg.enabled || !cfg.unit_id) return;
    var uid = String(cfg.unit_id).replace(/\D/g, "");
    if (!uid) return;

    var cbId = "redwood-aads-auth-btm-" + uid + "-" + Math.random().toString(36).slice(2, 11);
    var escId = escCssId(cbId);

    var pad = document.createElement("style");
    pad.textContent =
      "body.redwood-aads-auth-pad .auth-page-wrap{padding-bottom:min(30vh,200px)}" +
      ".redwood-aads-auth-mount--bottom{position:fixed;left:0;right:0;bottom:0;z-index:99999;pointer-events:none}" +
      ".redwood-aads-auth-slide{pointer-events:auto}" +
      ".redwood-aads-auth-bar{" +
      "width:100%;height:auto;position:fixed;text-align:center;font-size:0;" +
      "bottom:0;left:0;right:0;margin:auto;z-index:99999;" +
      "padding-bottom:max(4px,env(safe-area-inset-bottom,0px))" +
      "}" +
      ".redwood-aads-auth-frame{width:100%;margin:auto;position:relative;z-index:99998}" +
      ".redwood-aads-auth-dismiss{" +
      "top:50%;transform:translateY(-50%);right:24px;position:absolute;" +
      "border-radius:4px;background:rgba(248,248,249,0.70);padding:4px;z-index:99999;cursor:pointer" +
      "}" +
      ".redwood-aads-auth-iframe{" +
      "border:0;padding:0;width:70%;max-width:728px;height:auto;min-height:90px;" +
      "overflow:hidden;margin:auto;display:block" +
      "}" +
      "@media (max-width:768px){" +
      "body.redwood-aads-auth-pad .auth-page-wrap{" +
      "padding-bottom:max(min(46vh,320px),calc(140px + env(safe-area-inset-bottom,0px)))" +
      "}" +
      ".redwood-aads-auth-dismiss{" +
      "right:max(16px,env(safe-area-inset-right,0px));" +
      "min-width:44px;min-height:44px;padding:10px;display:inline-flex;" +
      "align-items:center;justify-content:center;box-sizing:border-box" +
      "}" +
      ".redwood-aads-auth-iframe{width:94%;max-width:100%;min-height:100px}" +
      "}" +
      (pairWithTop
        ? "@media (min-width:769px){.redwood-aads-auth-mount--bottom{display:none!important}}"
        : "") +
      "#" +
      escId +
      ":checked + .redwood-aads-auth-slide{display:none!important}";
    document.head.appendChild(pad);
    document.body.classList.add("redwood-aads-auth-pad");

    var mount = document.createElement("div");
    mount.className = "redwood-aads-auth-mount redwood-aads-auth-mount--bottom";

    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.id = cbId;
    cb.hidden = true;
    cb.setAttribute("autocomplete", "off");

    var slide = document.createElement("div");
    slide.className = "redwood-aads-auth-slide";

    var bar = document.createElement("div");
    bar.className = "redwood-aads-auth-bar";

    var label = document.createElement("label");
    label.setAttribute("for", cbId);
    label.setAttribute("aria-label", "Masquer la publicité");
    label.className = "redwood-aads-auth-dismiss";
    appendDismissIcon(label);

    var frame = document.createElement("div");
    frame.className = "redwood-aads-auth-frame";

    var iframe = document.createElement("iframe");
    iframe.setAttribute("data-aa", uid);
    iframe.src = "https://acceptable.a-ads.com/" + encodeURIComponent(uid) + "/?size=Adaptive";
    iframe.className = "redwood-aads-auth-iframe";
    iframe.title = "Publicité";
    iframe.loading = "lazy";
    iframe.referrerPolicy = "strict-origin-when-cross-origin";

    frame.appendChild(iframe);
    bar.appendChild(label);
    bar.appendChild(frame);
    slide.appendChild(bar);

    mount.appendChild(cb);
    mount.appendChild(slide);
    document.body.appendChild(mount);
    markExpect();
  }

  /** Sticky top strip (wide viewports), A-ADS desktop-style snippet. */
  function injectAuthStickyTop(cfg) {
    if (!cfg || !cfg.top_enabled || !cfg.top_unit_id) return;
    var uid = String(cfg.top_unit_id).replace(/\D/g, "");
    if (!uid) return;

    var cbId = "redwood-aads-auth-top-" + uid + "-" + Math.random().toString(36).slice(2, 11);
    var escId = escCssId(cbId);

    var pad = document.createElement("style");
    pad.textContent =
      "@media (min-width:769px){" +
      "body.redwood-aads-auth-pad-top .auth-page-wrap{" +
      "padding-top:max(min(22vh,180px),calc(16px + env(safe-area-inset-top,0px)))" +
      "}" +
      "}" +
      ".redwood-aads-auth-mount--top{position:fixed;left:0;right:0;top:0;z-index:99999;pointer-events:none}" +
      ".redwood-aads-auth-top-slide{pointer-events:auto}" +
      ".redwood-aads-auth-top-bar{" +
      "width:100%;height:auto;position:fixed;text-align:center;font-size:0;" +
      "top:0;left:0;right:0;margin:auto;z-index:99999;" +
      "padding-top:max(4px,env(safe-area-inset-top,0px))" +
      "}" +
      ".redwood-aads-auth-top-frame{width:100%;margin:auto;position:relative;z-index:99998}" +
      ".redwood-aads-auth-top-dismiss{" +
      "top:50%;transform:translateY(-50%);right:24px;position:absolute;" +
      "border-radius:4px;background:rgba(248,248,249,0.70);padding:4px;z-index:99999;cursor:pointer" +
      "}" +
      ".redwood-aads-auth-top-iframe{" +
      "border:0;padding:0;width:70%;max-width:728px;height:auto;min-height:90px;" +
      "overflow:hidden;margin:auto;display:block" +
      "}" +
      "@media (max-width:768px){" +
      ".redwood-aads-auth-mount--top{display:none!important}" +
      "}" +
      "@media (min-width:769px){" +
      ".redwood-aads-auth-top-dismiss{" +
      "right:max(16px,env(safe-area-inset-right,0px));" +
      "min-width:40px;min-height:40px;display:inline-flex;" +
      "align-items:center;justify-content:center;box-sizing:border-box" +
      "}" +
      "}" +
      "#" +
      escId +
      ":checked + .redwood-aads-auth-top-slide{display:none!important}";
    document.head.appendChild(pad);
    document.body.classList.add("redwood-aads-auth-pad-top");

    var mount = document.createElement("div");
    mount.className = "redwood-aads-auth-mount redwood-aads-auth-mount--top";

    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.id = cbId;
    cb.hidden = true;
    cb.setAttribute("autocomplete", "off");

    var slide = document.createElement("div");
    slide.className = "redwood-aads-auth-top-slide";

    var bar = document.createElement("div");
    bar.className = "redwood-aads-auth-top-bar";

    var label = document.createElement("label");
    label.setAttribute("for", cbId);
    label.setAttribute("aria-label", "Masquer la publicité");
    label.className = "redwood-aads-auth-top-dismiss";
    appendDismissIcon(label);

    var frame = document.createElement("div");
    frame.className = "redwood-aads-auth-top-frame";

    var iframe = document.createElement("iframe");
    iframe.setAttribute("data-aa", uid);
    iframe.src = "https://acceptable.a-ads.com/" + encodeURIComponent(uid) + "/?size=Adaptive";
    iframe.className = "redwood-aads-auth-top-iframe";
    iframe.title = "Publicité";
    iframe.loading = "lazy";
    iframe.referrerPolicy = "strict-origin-when-cross-origin";

    frame.appendChild(iframe);
    bar.appendChild(label);
    bar.appendChild(frame);
    slide.appendChild(bar);

    mount.appendChild(cb);
    mount.appendChild(slide);
    document.body.appendChild(mount);
    markExpect();
  }

  function run() {
    if (typeof fetch !== "function") return;
    fetch(watchAdsUrl(), { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (!r.ok) return null;
        return r.json();
      })
      .then(function (j) {
        var a = j && j.aads_auth;
        if (!a) return;
        var hasBottom = !!(a.enabled && a.unit_id);
        var hasTop = !!(a.top_enabled && a.top_unit_id);
        if (hasBottom) injectAuthStickyBottom(a, hasTop);
        if (hasTop) injectAuthStickyTop(a);
        if (hasBottom || hasTop) {
          try {
            window.dispatchEvent(
              new CustomEvent("redwood-auth-aads-ready", {
                detail: {
                  unitId: hasBottom ? String(a.unit_id).replace(/\D/g, "") : null,
                  topUnitId: hasTop ? String(a.top_unit_id).replace(/\D/g, "") : null,
                },
              }),
            );
          } catch (_) {}
        }
      })
      .catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
