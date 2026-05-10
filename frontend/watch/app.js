/**
 * Redwood Plus watch UI — shared fetch helper and TMDB image URL.
 */
const TMDB_IMG = 'https://image.tmdb.org/t/p/w500';

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'include', cache: 'no-store', ...opts });
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
    }
    const err = new Error(msg);
    err.status = response.status;
    throw err;
  }
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch (_) {
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

function injectWatchNavUserStyles() {
  if (document.getElementById('watch-nav-user-styles')) return;
  const style = document.createElement('style');
  style.id = 'watch-nav-user-styles';
  style.textContent = `
    .nav-end{display:flex;align-items:center;flex-wrap:wrap;gap:12px}
    .nav-end > a{color:var(--muted, #a3a3a3);text-decoration:none;font-size:14px;margin-left:0}
    .nav-end > a:hover{color:#fff}
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

function initWatchNavUserMenu() {
  injectWatchNavUserStyles();
  const wrap = document.getElementById('nav-user-wrap');
  if (!wrap) return;
  const trigger = document.getElementById('nav-user-trigger');
  const menu = document.getElementById('nav-user-menu');
  if (!trigger || !menu) return;

  trigger.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const open = !wrap.classList.contains('open');
    setWatchUserMenuOpen(wrap, open);
  });

  document.addEventListener('click', () => setWatchUserMenuOpen(wrap, false));
  wrap.addEventListener('click', (e) => e.stopPropagation());

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && wrap.classList.contains('open')) setWatchUserMenuOpen(wrap, false);
  });

  const logoutBtn = document.getElementById('nav-logout');
  if (logoutBtn && !logoutBtn.dataset.watchBound) {
    logoutBtn.dataset.watchBound = '1';
    logoutBtn.addEventListener('click', (e) => {
      e.preventDefault();
      setWatchUserMenuOpen(wrap, false);
      logout();
    });
  }
}

async function hydrateWatchNavUser() {
  const wrap = document.getElementById('nav-user-wrap');
  if (!wrap) return;
  const elName = document.getElementById('nav-user-name');
  const elIni = document.getElementById('nav-user-initials');
  const trigger = document.getElementById('nav-user-trigger');
  try {
    const me = await apiJson('/api/auth/me');
    const name = me.username || me.email || 'Utilisateur';
    if (elName) elName.textContent = name;
    if (elIni) elIni.textContent = watchNavUserInitials(name);
    if (trigger) trigger.setAttribute('aria-label', 'Menu compte — ' + name);
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

if (typeof document !== 'undefined') {
  initWatchNavUserMenu();
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
