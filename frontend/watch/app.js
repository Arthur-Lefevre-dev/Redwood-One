/**
 * Redwood Plus watch UI — shared fetch helper and TMDB image URL.
 */
const TMDB_IMG = 'https://image.tmdb.org/t/p/w500';

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'include', ...opts });
  if (r.status === 401) {
    window.location.href = '/login.html';
    throw new Error('401');
  }
  return r;
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
