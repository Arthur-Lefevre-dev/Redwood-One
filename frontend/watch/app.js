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
