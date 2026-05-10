/**
 * Redwood Plus watch UI — shared fetch helper and TMDB image URL.
 */
const TMDB_IMG = 'https://image.tmdb.org/t/p/w500';

/** Optional: set before loading app.js when the UI is not served behind the same origin as FastAPI (e.g. Live Server on :5500, API on :8000). Cross-origin cookies may require CORS + cookie settings on the API. */
function getApiBase() {
  if (typeof window === 'undefined') return '';
  const b = window.__REDWOOD_API_BASE__;
  if (b == null || String(b).trim() === '') return '';
  return String(b).replace(/\/$/, '');
}

function apiUrl(path) {
  const p = path.startsWith('/') ? path : '/' + path;
  const base = getApiBase();
  return base ? base + p : p;
}

function looksLikeHtmlBody(text) {
  const t = String(text || '')
    .trim()
    .slice(0, 80)
    .toLowerCase();
  return t.startsWith('<!doctype') || t.startsWith('<html');
}

async function api(path, opts = {}) {
  const r = await fetch(apiUrl(path), { credentials: 'include', cache: 'no-store', ...opts });
  if (r.status === 401) {
    window.location.href = '/login.html';
    throw new Error('401');
  }
  return r;
}

/**
 * Read JSON from a fetch Response; never throws SyntaxError on HTML/plain error bodies.
 * On !ok, throws Error with message from JSON detail or first line of body.
 */
async function readJsonSafe(response) {
  const text = await response.text();
  const ct = (response.headers.get('content-type') || '').toLowerCase();
  if (!response.ok) {
    let msg = (text && text.trim()) || 'HTTP ' + response.status;
    if (ct.includes('application/json')) {
      try {
        const j = JSON.parse(text);
        if (j.detail != null) {
          msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
        }
      } catch (_) {
        /* keep msg */
      }
    } else {
      const line = text.trim().split(/\r?\n/)[0] || '';
      msg = line.slice(0, 240) || 'HTTP ' + response.status;
      if (looksLikeHtmlBody(text)) {
        if (response.status === 502 || response.status === 503 || response.status === 504) {
          msg =
            'HTTP ' +
            response.status +
            ' — la passerelle (nginx) ne reçoit pas de réponse de l’API FastAPI. Vérifiez : (1) le conteneur api est « healthy » : docker compose ps ; (2) les logs : docker logs redwood_api ; (3) vous ouvrez le site via le même hôte/port que nginx (pas un fichier local ni Live Server seul). En dev sans Docker : définissez window.__REDWOOD_API_BASE__ vers l’URL de l’API (ex. http://localhost:8000).';
        } else {
          msg =
            'HTTP ' +
            response.status +
            ' — réponse HTML (pas JSON). Cause fréquente : API injoignable, ou page ouverte sans proxy /api. Utilisez l’URL servie par nginx du stack, ou définissez window.__REDWOOD_API_BASE__ vers l’URL de l’API.';
        }
      }
    }
    const err = new Error(msg);
    err.status = response.status;
    throw err;
  }
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch (_) {
    if (looksLikeHtmlBody(text)) {
      const err = new Error(
        'Réponse HTML au lieu de JSON — les requêtes /api/* n’atteignent probablement pas FastAPI. Ouvrez l’interface via nginx du projet ou définissez window.__REDWOOD_API_BASE__.'
      );
      err.status = response.status;
      throw err;
    }
    const err = new Error(
      'Réponse invalide (JSON attendu). Cause fréquente : erreur serveur ou base non migrée. Début de réponse : ' +
        text.slice(0, 120).replace(/\s+/g, ' ')
    );
    err.status = response.status;
    throw err;
  }
}

/** fetch + 401 redirect + readJsonSafe */
async function apiJson(path, opts = {}) {
  const r = await api(path, opts);
  return readJsonSafe(r);
}

function posterUrl(p) {
  if (!p) return '';
  if (p.startsWith('http')) return p;
  return TMDB_IMG + p;
}

async function logout() {
  await api('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login.html';
}

/** Initials for avatar chip from username or email. */
function watchNavUserInitials(display) {
  const s = String(display || '').trim();
  if (!s) return '?';
  if (s.includes('@')) return s[0].toUpperCase();
  const parts = s.split(/[\s._-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return s.slice(0, 2).toUpperCase();
}

function injectWatchMobileNavStyles() {
  if (document.getElementById('watch-mobile-nav-styles')) return;
  const style = document.createElement('style');
  style.id = 'watch-mobile-nav-styles';
  style.textContent = `
    .nav-burger{display:none}
    @media (max-width:768px){
      .nav-burger{
        display:inline-flex;align-items:center;justify-content:center;
        width:44px;height:44px;padding:0;flex-shrink:0;
        border:1px solid #333;border-radius:10px;background:#141414;color:#e5e5e5;
        cursor:pointer;font:inherit;
      }
      .nav-burger svg{display:block}
      .nav-burger:focus-visible{outline:2px solid var(--accent,#8B2500);outline-offset:2px}
      nav.watch-nav{
        display:grid!important;
        grid-template-columns:48px 1fr;
        grid-template-rows:auto auto;
        align-items:center;
        column-gap:10px;row-gap:10px;
        padding-left:16px!important;padding-right:16px!important;
        padding-top:12px!important;padding-bottom:12px!important;
      }
      nav.watch-nav:not(.watch-nav--with-search){grid-template-rows:auto}
      nav.watch-nav .nav-burger{grid-column:1;grid-row:1}
      nav.watch-nav a.brand{
        grid-column:2;grid-row:1;justify-self:center;text-align:center;margin:0!important;
        width:100%;max-width:100%;
      }
      nav.watch-nav .nav-end{display:none!important}
      nav.watch-nav .nav-primary-links{display:none!important}
      nav.watch-nav.watch-nav--with-search > .nav-mid,
      nav.watch-nav.watch-nav--with-search > input.nav-search{
        grid-column:1/-1;grid-row:2;width:100%!important;max-width:none!important;min-width:0;
      }
      nav.watch-nav.watch-nav--with-search .nav-mid{display:flex}
    }
    .watch-nav-drawer[hidden]{display:none!important}
    .watch-nav-drawer:not([hidden]){
      position:fixed;inset:0;z-index:400;
    }
    .watch-nav-drawer-backdrop{
      position:absolute;inset:0;background:rgba(0,0,0,.55);cursor:pointer;
    }
    .watch-nav-drawer-panel{
      position:absolute;top:0;left:0;bottom:0;width:min(88vw,300px);
      background:#111;border-right:1px solid #333;
      padding:56px 16px 24px;overflow-y:auto;
      box-shadow:8px 0 40px rgba(0,0,0,.55);
    }
    .watch-nav-drawer-close{
      position:absolute;top:12px;right:12px;width:40px;height:40px;
      border-radius:10px;border:1px solid #333;background:#1a1a1a;color:#e5e5e5;
      font-size:22px;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;font-family:inherit;
    }
    .watch-nav-drawer-close:hover{border-color:var(--accent,#8B2500);color:#fff}
    .watch-nav-drawer-links{display:flex;flex-direction:column;gap:4px;padding-top:8px}
    .watch-nav-drawer-links a.watch-nav-drawer-link{
      display:block;padding:12px 14px;border-radius:10px;color:#e5e5e5;text-decoration:none;font-size:15px;
    }
    .watch-nav-drawer-links a.watch-nav-drawer-link:hover{background:#222;color:#fff}
    .watch-nav-drawer-link--invite{font-weight:600}
    .watch-nav-drawer-links + .watch-nav-drawer-footer{
      margin-top:16px;padding-top:16px;border-top:1px solid #2a2a2a;
    }
    .watch-nav-drawer-footer{
      margin-top:0;padding-top:0;border-top:none;
      display:flex;flex-direction:column;gap:12px;
    }
    .watch-nav-drawer-footer .nav-user-wrap{margin:0}
    .watch-nav-drawer-footer .nav-user-name{max-width:min(200px,55vw)!important}
    .watch-nav-drawer-footer .nav-user-menu{
      left:0!important;right:0!important;min-width:100%!important;
    }
    .watch-nav-drawer-invite{
      display:flex!important;align-items:center;gap:12px;font-weight:600;
      color:#e5e5e5!important;text-decoration:none!important;
    }
    .watch-nav-drawer-invite:visited{color:#e5e5e5!important}
    .watch-nav-drawer-admin{
      font-weight:600;color:#e5e5e5!important;text-decoration:none!important;
    }
    .watch-nav-drawer-admin:visited{color:#e5e5e5!important}
    .watch-nav-drawer-invite svg{width:28px;height:28px;flex-shrink:0;display:block;fill:currentColor}
    .watch-nav-drawer-invite-txt{flex:1}
    .watch-nav-drawer-footer .nav-user-menu{z-index:410}
    #watch-nav-drawer-admin[hidden]{display:none!important}
  `;
  document.head.appendChild(style);
}

/** Clone account pill + dropdown into the mobile drawer (suffix `-drawer` ids). */
function cloneNavUserWrapForDrawer(footer) {
  const src = document.getElementById('nav-user-wrap');
  if (!src || footer.querySelector('#nav-user-wrap-drawer')) return;
  const c = src.cloneNode(true);
  c.classList.add('nav-user-wrap--drawer');
  c.id = 'nav-user-wrap-drawer';
  const idSuffixMap = {
    'nav-user-trigger': 'nav-user-trigger-drawer',
    'nav-user-menu': 'nav-user-menu-drawer',
    'nav-user-name': 'nav-user-name-drawer',
    'nav-user-initials': 'nav-user-initials-drawer',
    'nav-logout': 'nav-logout-drawer',
  };
  c.querySelectorAll('[id]').forEach((el) => {
    const oid = el.id;
    const next = idSuffixMap[oid] || (oid.endsWith('-drawer') ? oid : oid + '-drawer');
    el.id = next;
  });
  const trig = c.querySelector('#nav-user-trigger-drawer');
  const menu = c.querySelector('#nav-user-menu-drawer');
  if (trig && menu) trig.setAttribute('aria-controls', menu.id);
  footer.insertBefore(c, footer.firstChild);
}

/** Wrap primary nav links, inject burger + drawer (mobile menu). Idempotent. */
function upgradeWatchNavForMobile() {
  const nav = document.querySelector('body > nav:first-of-type');
  if (!nav || nav.dataset.watchMobileUpgraded) return;
  const end = nav.querySelector('.nav-end');
  if (!end) return;

  // Use .children only (ignore whitespace text nodes between tags — firstChild was often \n and aborted the loop).
  const primaryAnchors = Array.from(end.children).filter(
    (el) => el.tagName === 'A' && !el.classList.contains('nav-invite-link'),
  );
  const invitePre = nav.querySelector('a.nav-invite-link');
  const userPre = nav.querySelector('#nav-user-wrap');
  if (!primaryAnchors.length && !invitePre && !userPre) return;

  if (primaryAnchors.length) {
    const primary = document.createElement('div');
    primary.className = 'nav-primary-links';
    primaryAnchors.forEach((a) => primary.appendChild(a));
    end.insertBefore(primary, end.firstChild);
  }

  if (nav.querySelector('.nav-mid') || nav.querySelector('input.nav-search')) {
    nav.classList.add('watch-nav--with-search');
  }
  nav.classList.add('watch-nav');

  const burger = document.createElement('button');
  burger.type = 'button';
  burger.className = 'nav-burger';
  burger.id = 'watch-nav-burger';
  burger.setAttribute('aria-label', 'Ouvrir le menu');
  burger.setAttribute('aria-expanded', 'false');
  burger.setAttribute('aria-controls', 'watch-nav-drawer');
  burger.innerHTML =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M4 7h16M4 12h16M4 17h16"/></svg>';
  nav.insertBefore(burger, nav.firstChild);

  const drawer = document.createElement('div');
  drawer.id = 'watch-nav-drawer';
  drawer.className = 'watch-nav-drawer';
  drawer.hidden = true;
  drawer.setAttribute('role', 'dialog');
  drawer.setAttribute('aria-modal', 'true');
  drawer.setAttribute('aria-label', 'Menu de navigation');

  const backdrop = document.createElement('div');
  backdrop.className = 'watch-nav-drawer-backdrop';
  backdrop.tabIndex = -1;

  const panel = document.createElement('div');
  panel.className = 'watch-nav-drawer-panel';

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'watch-nav-drawer-close';
  closeBtn.setAttribute('aria-label', 'Fermer le menu');
  closeBtn.innerHTML = '&times;';

  let linksWrap = null;
  const primaryEl = nav.querySelector('.nav-primary-links');
  if (primaryEl) {
    linksWrap = document.createElement('div');
    linksWrap.className = 'watch-nav-drawer-links';
    primaryEl.querySelectorAll('a').forEach((a) => {
      const c = a.cloneNode(true);
      c.classList.add('watch-nav-drawer-link');
      linksWrap.appendChild(c);
    });
  }

  const footer = document.createElement('div');
  footer.className = 'watch-nav-drawer-footer';

  cloneNavUserWrapForDrawer(footer);

  const inv = nav.querySelector('a.nav-invite-link');
  if (inv) {
    const ia = document.createElement('a');
    ia.href = inv.getAttribute('href') || '/watch/invitations.html';
    ia.className = 'watch-nav-drawer-link watch-nav-drawer-invite';
    ia.setAttribute('aria-label', 'Invitations');
    ia.innerHTML =
      watchNavInviteIconSvg() +
      '<span class="watch-nav-drawer-invite-txt">Invitations</span>';
    footer.appendChild(ia);
  }

  const adminA = document.createElement('a');
  adminA.id = 'watch-nav-drawer-admin';
  adminA.href = '/admin/';
  adminA.className = 'watch-nav-drawer-link watch-nav-drawer-admin';
  adminA.textContent = 'Administration';
  adminA.hidden = true;
  footer.appendChild(adminA);

  panel.appendChild(closeBtn);
  if (linksWrap) panel.appendChild(linksWrap);
  panel.appendChild(footer);
  drawer.appendChild(backdrop);
  drawer.appendChild(panel);
  nav.parentNode.insertBefore(drawer, nav.nextSibling);

  nav.dataset.watchMobileUpgraded = '1';
}

function initWatchMobileNav() {
  const burger = document.getElementById('watch-nav-burger');
  const drawer = document.getElementById('watch-nav-drawer');
  if (!burger || !drawer || drawer.dataset.watchBound) return;
  drawer.dataset.watchBound = '1';
  const backdrop = drawer.querySelector('.watch-nav-drawer-backdrop');
  const closeBtn = drawer.querySelector('.watch-nav-drawer-close');

  function setOpen(open) {
    drawer.hidden = !open;
    burger.setAttribute('aria-expanded', open ? 'true' : 'false');
    document.body.style.overflow = open ? 'hidden' : '';
    if (open) {
      document.querySelectorAll('.nav-user-wrap').forEach((w) => setWatchUserMenuOpen(w, false));
      closeBtn && closeBtn.focus();
    } else {
      burger.focus();
    }
  }

  burger.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    setOpen(drawer.hidden);
  });
  backdrop &&
    backdrop.addEventListener('click', () => {
      setOpen(false);
    });
  closeBtn &&
    closeBtn.addEventListener('click', () => {
      setOpen(false);
    });

  drawer.querySelectorAll('.watch-nav-drawer-link').forEach((a) => {
    a.addEventListener('click', () => setOpen(false));
  });
  drawer.querySelectorAll('#nav-user-menu-drawer a[href]').forEach((a) => {
    a.addEventListener('click', () => setOpen(false));
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !drawer.hidden) setOpen(false);
  });
}

function injectWatchNavUserStyles() {
  if (document.getElementById('watch-nav-user-styles')) return;
  const style = document.createElement('style');
  style.id = 'watch-nav-user-styles';
  style.textContent = `
    .nav-end{display:flex;align-items:center;flex-wrap:wrap;gap:18px}
    .nav-primary-links{display:flex;flex-wrap:wrap;align-items:center;gap:18px}
    .nav-primary-links a{color:var(--muted, #a3a3a3);text-decoration:none;font-size:14px;margin:0;line-height:1.2}
    .nav-primary-links a:hover{color:#fff}
    a.nav-invite-link{
      display:inline-flex;align-items:center;justify-content:center;
      padding:0;margin:0;border-radius:0;color:var(--muted, #a3a3a3);
      font-size:14px;line-height:1;flex-shrink:0;
    }
    a.nav-invite-link:hover{color:#fff;background:transparent}
    a.nav-invite-link svg{width:28px;height:28px;display:block;fill:currentColor;flex-shrink:0}
    a.nav-invite-link--current{color:#fecaca}
    a.nav-invite-link--current:hover{color:#fff;background:transparent}
    .nav-user-wrap{position:relative;margin-left:0}
    .nav-user-trigger{
      display:inline-flex;align-items:center;gap:10px;
      padding:5px 12px 5px 5px;border-radius:999px;
      border:1px solid #333;background:#141414;color:var(--text, #f5f5f5);
      cursor:pointer;font:inherit;font-size:14px;
    }
    .nav-user-trigger:hover,.nav-user-wrap.open .nav-user-trigger{
      border-color:var(--accent, #8B2500);color:#fff;
    }
    .nav-user-trigger:focus-visible{outline:2px solid var(--accent, #8B2500);outline-offset:2px}
    .nav-user-avatar{
      width:34px;height:34px;border-radius:50%;
      background:linear-gradient(145deg,var(--accent, #8B2500),#4a1500);
      display:inline-flex;align-items:center;justify-content:center;
      font-size:12px;font-weight:700;color:#fff;flex-shrink:0;
      letter-spacing:0.02em;
    }
    .nav-user-name{
      max-width:min(160px,28vw);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
      color:var(--muted, #a3a3a3);text-align:left;
    }
    .nav-user-wrap.open .nav-user-name{color:#fff}
    .nav-user-menu{
      position:absolute;top:calc(100% + 8px);right:0;min-width:220px;
      padding:6px;border-radius:12px;border:1px solid #333;background:#141414;
      box-shadow:0 16px 40px rgba(0,0,0,.55);z-index:300;
    }
    .nav-user-menu[hidden]{display:none!important}
    .nav-user-menu a,.nav-user-menu button{
      display:block;width:100%;text-align:left;padding:10px 12px;border-radius:8px;
      border:none;background:transparent;color:#e5e5e5;font-size:14px;
      text-decoration:none;cursor:pointer;font:inherit;margin:0;
    }
    .nav-user-menu a:hover,.nav-user-menu button:hover{background:#222;color:#fff}
    .nav-user-menu button.nav-logout-btn{color:#f87171}
    .nav-user-menu button.nav-logout-btn:hover{color:#fca5a5;background:rgba(239,68,68,.12)}
  `;
  document.head.appendChild(style);
}

function setWatchUserMenuOpen(wrap, open) {
  const trigger = wrap.querySelector('.nav-user-trigger');
  const menu = wrap.querySelector('.nav-user-menu');
  if (!trigger || !menu) return;
  wrap.classList.toggle('open', open);
  trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
  menu.hidden = !open;
}

/** Show / hide drawer “Administration” from same source as user menu. */
function syncWatchMobileDrawerAdmin() {
  const drawerAdmin = document.getElementById('watch-nav-drawer-admin');
  const menuAdmin = document.getElementById('nav-admin-dashboard');
  if (!drawerAdmin) return;
  if (menuAdmin) {
    drawerAdmin.href = menuAdmin.getAttribute('href') || '/admin/';
    drawerAdmin.hidden = false;
  } else {
    drawerAdmin.hidden = true;
  }
}

function ensureWatchNavAdminLink(isAdmin) {
  const pairs = [
    ['nav-user-menu', 'nav-admin-dashboard'],
    ['nav-user-menu-drawer', 'nav-admin-dashboard-drawer'],
  ];
  pairs.forEach(([menuId, linkId]) => {
    const menu = document.getElementById(menuId);
    if (!menu) return;
    const existing = document.getElementById(linkId);
    if (isAdmin) {
      if (!existing) {
        const link = document.createElement('a');
        link.id = linkId;
        link.href = '/admin/';
        link.setAttribute('role', 'menuitem');
        link.className = 'nav-admin-dashboard';
        link.textContent = 'Administration';
        menu.insertBefore(link, menu.firstChild);
      }
    } else if (existing) {
      existing.remove();
    }
  });
  syncWatchMobileDrawerAdmin();
}

function initWatchNavUserMenu() {
  injectWatchNavUserStyles();
  document.querySelectorAll('.nav-user-wrap').forEach((wrap) => {
    if (wrap.dataset.watchNavBound) return;
    wrap.dataset.watchNavBound = '1';
    const trigger = wrap.querySelector('.nav-user-trigger');
    const menu = wrap.querySelector('.nav-user-menu');
    if (!trigger || !menu) return;

    trigger.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const open = !wrap.classList.contains('open');
      document.querySelectorAll('.nav-user-wrap').forEach((w) => {
        if (w !== wrap) setWatchUserMenuOpen(w, false);
      });
      setWatchUserMenuOpen(wrap, open);
    });

    wrap.addEventListener('click', (e) => e.stopPropagation());
  });

  document.addEventListener('click', () => {
    document.querySelectorAll('.nav-user-wrap').forEach((w) => setWatchUserMenuOpen(w, false));
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      document.querySelectorAll('.nav-user-wrap.open').forEach((w) => setWatchUserMenuOpen(w, false));
    }
  });

  document.querySelectorAll('.nav-logout-btn').forEach((logoutBtn) => {
    if (logoutBtn.dataset.watchBound) return;
    logoutBtn.dataset.watchBound = '1';
    logoutBtn.addEventListener('click', (e) => {
      e.preventDefault();
      const wrap = logoutBtn.closest('.nav-user-wrap');
      if (wrap) setWatchUserMenuOpen(wrap, false);
      logout();
    });
  });
}

async function hydrateWatchNavUser() {
  if (!document.getElementById('nav-user-wrap') && !document.getElementById('nav-user-wrap-drawer')) return;
  const elNames = document.querySelectorAll('#nav-user-name, #nav-user-name-drawer');
  const elInis = document.querySelectorAll('#nav-user-initials, #nav-user-initials-drawer');
  const triggers = document.querySelectorAll('#nav-user-trigger, #nav-user-trigger-drawer');
  try {
    const me = await apiJson('/api/auth/me');
    const name = me.username || me.email || 'Utilisateur';
    elNames.forEach((el) => {
      el.textContent = name;
    });
    elInis.forEach((el) => {
      el.textContent = watchNavUserInitials(name);
    });
    triggers.forEach((t) => {
      t.setAttribute('aria-label', 'Menu compte — ' + name);
    });
    const role = me.role != null ? String(me.role).toLowerCase() : '';
    ensureWatchNavAdminLink(role === 'admin');
  } catch (_) {
    /* 401 → redirect in api() */
  }
}

function watchEscapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

const WATCH_ANNOUNCE_DISMISS_KEY = 'redwood_announce_dismiss';

function injectWatchAnnouncementStyles() {
  if (document.getElementById('watch-announcement-styles')) return;
  const style = document.createElement('style');
  style.id = 'watch-announcement-styles';
  style.textContent = `
    .watch-announcement-host{margin:0;padding:0}
    .watch-announcement{
      margin:12px 28px 12px;
      padding:12px 16px;
      border-radius:10px;
      border:1px solid rgba(139,37,0,.45);
      background:linear-gradient(135deg,rgba(139,37,0,.22),rgba(20,10,8,.95));
      color:#fef2f2;
      font-size:14px;
      line-height:1.5;
      display:flex;
      align-items:flex-start;
      gap:12px;
      box-shadow:0 4px 24px rgba(0,0,0,.35);
    }
    .watch-announcement .wa-icon{flex-shrink:0;font-size:18px;line-height:1.2}
    .watch-announcement .wa-body{flex:1;min-width:0;word-break:break-word}
    .watch-announcement .wa-meta{font-size:11px;color:#fca5a5;margin-top:8px;opacity:.95}
    .watch-announcement .wa-dismiss{
      flex-shrink:0;background:transparent;border:none;color:#fecaca;cursor:pointer;
      padding:4px 8px;font-size:18px;line-height:1;border-radius:6px;opacity:.85;
    }
    .watch-announcement .wa-dismiss:hover{opacity:1;background:rgba(0,0,0,.2)}
    @media (max-width:560px){
      .watch-announcement{margin-left:16px;margin-right:16px}
    }
  `;
  document.head.appendChild(style);
}

async function initViewerAnnouncement() {
  const host = document.getElementById('watch-announcement');
  if (!host) return;
  injectWatchAnnouncementStyles();
  try {
    const data = await apiJson('/api/announcement');
    if (!data || !data.active || !data.message) {
      host.hidden = true;
      host.innerHTML = '';
      return;
    }
    const token = (data.ends_at || '') + '|' + data.message;
    if (sessionStorage.getItem(WATCH_ANNOUNCE_DISMISS_KEY) === token) {
      host.hidden = true;
      host.innerHTML = '';
      return;
    }
    host.hidden = false;
    let meta = '';
    if (data.ends_at) {
      const raw = data.ends_at.endsWith('Z') ? data.ends_at : data.ends_at + 'Z';
      const d = new Date(raw);
      if (!Number.isNaN(d.getTime())) {
        meta =
          "Jusqu'au " +
          d.toLocaleString('fr-FR', { dateStyle: 'medium', timeStyle: 'short' });
      }
    }
    host.innerHTML =
      '<div class="watch-announcement" role="region" aria-label="Annonce">' +
      '<span class="wa-icon" aria-hidden="true">&#128226;</span>' +
      '<div class="wa-body">' +
      watchEscapeHtml(data.message).replace(/\n/g, '<br>') +
      (meta ? '<div class="wa-meta">' + watchEscapeHtml(meta) + '</div>' : '') +
      '</div>' +
      '<button type="button" class="wa-dismiss" aria-label="Masquer pour cette session">&times;</button>' +
      '</div>';
    const btn = host.querySelector('.wa-dismiss');
    if (btn) {
      btn.addEventListener('click', () => {
        sessionStorage.setItem(WATCH_ANNOUNCE_DISMISS_KEY, token);
        host.hidden = true;
        host.innerHTML = '';
      });
    }
  } catch (_) {
    host.hidden = true;
    host.innerHTML = '';
  }
}

/** SVG icon for member invitations (injected into .nav-invite-link anchors). */
function watchNavInviteIconSvg() {
  return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" aria-hidden="true"><path d="M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0h-.29c-.45.68-1.18 1.25-2.15 1.59.77.53 1.44 1.1 1.89 1.69h3.55v-1.79c0-1.94-3.48-2.49-6-2.49z"/></svg>';
}

function initWatchNavInviteIcons() {
  document.querySelectorAll('a.nav-invite-link').forEach((a) => {
    if (a.querySelector('svg')) return;
    a.innerHTML = watchNavInviteIconSvg();
  });
}

if (typeof document !== 'undefined') {
  injectWatchMobileNavStyles();
  upgradeWatchNavForMobile();
  initWatchNavUserMenu();
  initWatchMobileNav();
  initWatchNavInviteIcons();
  hydrateWatchNavUser();
  initViewerAnnouncement();
}

// Expose on window (inline scripts rely on globals; absolute /watch/app.js avoids failed load when URL is /watch without trailing slash)
window.api = api;
window.apiJson = apiJson;
window.readJsonSafe = readJsonSafe;
window.posterUrl = posterUrl;
window.logout = logout;
window.initWatchNavUserMenu = initWatchNavUserMenu;
window.hydrateWatchNavUser = hydrateWatchNavUser;
window.initViewerAnnouncement = initViewerAnnouncement;
window.watchEscapeHtml = watchEscapeHtml;
window.initWatchNavInviteIcons = initWatchNavInviteIcons;
