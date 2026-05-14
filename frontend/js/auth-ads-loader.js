/**
 * A-ADS sticky bottom banner on login / register pages when enabled server-side.
 * Reads /api/public/watch-ads → aads_auth (WATCH_ADS_AADS_AUTH_* env).
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

  function escCssId(id) {
    if (typeof CSS !== "undefined" && CSS.escape) return CSS.escape(id);
    return String(id).replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function injectAuthSticky(cfg) {
    if (!cfg || !cfg.enabled || !cfg.unit_id) return;
    var uid = String(cfg.unit_id).replace(/\D/g, "");
    if (!uid) return;

    var pad = document.createElement("style");
    pad.textContent =
      "body.redwood-aads-auth-pad .auth-page-wrap{padding-bottom:min(30vh,200px)}";
    document.head.appendChild(pad);
    document.body.classList.add("redwood-aads-auth-pad");

    var cbId = "redwood-aads-auth-" + uid + "-" + Math.random().toString(36).slice(2, 11);
    var mount = document.createElement("div");
    mount.className = "redwood-aads-auth-mount";
    mount.setAttribute("style", "position:fixed;left:0;right:0;bottom:0;z-index:99998;pointer-events:none");

    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.id = cbId;
    cb.hidden = true;
    cb.setAttribute("autocomplete", "off");

    var slide = document.createElement("div");
    slide.className = "redwood-aads-auth-slide";
    slide.setAttribute("style", "pointer-events:auto");

    var bar = document.createElement("div");
    bar.setAttribute(
      "style",
      "width:100%;position:fixed;text-align:center;font-size:0;bottom:0;left:0;right:0;margin:auto;z-index:99998",
    );

    var label = document.createElement("label");
    label.setAttribute("for", cbId);
    label.setAttribute("aria-label", "Masquer la publicité");
    label.setAttribute(
      "style",
      "top:50%;transform:translateY(-50%);right:24px;position:absolute;border-radius:4px;background:rgba(248,248,249,0.70);padding:4px;z-index:99999;cursor:pointer",
    );
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

    var frame = document.createElement("div");
    frame.setAttribute("style", "width:100%;margin:auto;position:relative;z-index:99998");

    var iframe = document.createElement("iframe");
    iframe.setAttribute("data-aa", uid);
    iframe.src = "https://acceptable.a-ads.com/" + encodeURIComponent(uid) + "/?size=Adaptive";
    iframe.setAttribute(
      "style",
      "border:0;padding:0;width:70%;max-width:728px;height:auto;min-height:90px;overflow:hidden;margin:auto;display:block",
    );
    iframe.title = "Publicité";
    iframe.loading = "lazy";
    iframe.referrerPolicy = "strict-origin-when-cross-origin";

    frame.appendChild(iframe);
    bar.appendChild(label);
    bar.appendChild(frame);
    slide.appendChild(bar);

    var st = document.createElement("style");
    st.textContent =
      "#" + escCssId(cbId) + ":checked + .redwood-aads-auth-slide{display:none!important}";

    mount.appendChild(cb);
    mount.appendChild(slide);
    mount.appendChild(st);
    document.body.appendChild(mount);
  }

  function run() {
    if (typeof fetch !== "function") return;
    fetch(watchAdsUrl(), { credentials: "same-origin", cache: "no-store" })
      .then(function (r) {
        if (!r.ok) return null;
        return r.json();
      })
      .then(function (j) {
        if (j && j.aads_auth) injectAuthSticky(j.aads_auth);
      })
      .catch(function () {});
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
