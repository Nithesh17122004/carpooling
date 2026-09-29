/* API client: token storage, fetch wrapper (401 -> silent refresh retry),
   typed endpoints.

   Security (Part 12): the access token lives ONLY in memory. It is never
   written to localStorage/sessionStorage, so an XSS dump can no longer steal
   a live session token; a page reload rehydrates it by rotating the HttpOnly
   refresh cookie (silent refresh). A non-sensitive `rm_authed` marker tells
   the boot path whether to attempt that rehydration, and `rm_user` is a cache
   of the public profile only. */

export const API_BASE = window.RIDEMATE_API || '';

let _accessToken = '';

export const store = {
  get token() { return _accessToken; },
  get user() {
    try { return JSON.parse(localStorage.getItem('rm_user') || 'null'); } catch { return null; }
  },
  set user(u) {
    if (u == null) localStorage.removeItem('rm_user');
    else localStorage.setItem('rm_user', JSON.stringify(u));
  },
  get hadSession() { return localStorage.getItem('rm_authed') === '1'; },
  save(token, user) {
    _accessToken = token || '';
    localStorage.setItem('rm_user', JSON.stringify(user));
    localStorage.setItem('rm_authed', '1');
  },
  clear() {
    _accessToken = '';
    localStorage.removeItem('rm_user');
    localStorage.removeItem('rm_authed');
  },
};

/* One-time migration from the legacy design: purge any persisted access token
   (older builds kept the bearer in localStorage). If one existed, the HttpOnly
   refresh cookie is still valid, so keep the session alive by enabling the
   boot-time silent-rehydration path. */
try {
  if (localStorage.getItem('rm_token')) {
    localStorage.setItem('rm_authed', '1');
    localStorage.removeItem('rm_token');
  }
} catch {
  /* storage unavailable (private mode / blocked) -- memory-only session */
}

export class ApiError extends Error {
  constructor(message, status, code, details) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function isAuthPath(path) {
  return path.startsWith('/api/auth/');
}

/* Rotate the HttpOnly refresh cookie into a fresh access token. `credentials:
   include` lets the browser attach `rm_refresh`; the body never carries it. */
let _refreshing = null;
async function refreshAccessToken() {
  if (!_refreshing) {
    _refreshing = (async () => {
      let res;
      try {
        res = await fetch(API_BASE + '/api/auth/refresh', { method: 'POST', credentials: 'include' });
      } catch {
        throw new ApiError('Session could not be refreshed.', 0, 'network');
      }
      let data = null;
      try { data = await res.json(); } catch { /* noop */ }
      if (!res.ok) {
        const err = (data && data.error) || {};
        throw new ApiError(err.message || 'Session expired.', res.status, err.code, err.details);
      }
      store.save(data.token, data.user);
      return data;
    })().finally(() => { _refreshing = null; });
  }
  return _refreshing;
}

export async function restoreSession() {
  /* Boot-time session rehydration. In-memory token from this page load wins;
     otherwise (hard reload) the refresh cookie may still be valid, so rotate
     it once. A bearer token is then proven against /me. */
  if (!store.token && store.hadSession) {
    await refreshAccessToken();
  }
  if (store.token) {
    return await Api.me();
  }
  throw new ApiError('No session.', 401, 'auth_required');
}

async function request(method, path, { body, form, retried } = {}) {
  const headers = {};
  const token = store.token;
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const opts = { method, headers, credentials: 'include' };
  if (form) {
    opts.body = form;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(API_BASE + path, opts);
  } catch {
    throw new ApiError('Cannot reach the server. Is the backend running?', 0, 'network');
  }

  let data = null;
  try { data = await res.json(); } catch { /* noop */ }

  if (!res.ok) {
    const err = (data && data.error) || {};
    // Access token expired: try exactly one silent refresh, then retry once.
    if (res.status === 401 && !isAuthPath(path) && !retried) {
      try {
        await refreshAccessToken();
        return request(method, path, { body, form, retried: true });
      } catch { /* refresh failed -> normal 401 handling below */ }
    }
    if (res.status === 401 && !isAuthPath(path)) {
      store.clear();
      window.dispatchEvent(new CustomEvent('ridemate:logout'));
    }
    throw new ApiError(err.message || `Request failed (${res.status})`, res.status, err.code, err.details);
  }
  return data;
}

const get = (p) => request('GET', p);
const post = (p, body) => request('POST', p, { body });
const patch = (p, body) => request('PATCH', p, { body });
const del = (p) => request('DELETE', p);

export const Api = {
  // auth
  register: (b) => post('/api/auth/register', b),
  login: (b) => post('/api/auth/login', b),
  google: (b) => post('/api/auth/google', b),
  me: () => get('/api/auth/me'),
  providers: () => get('/api/auth/providers'),
  updateMe: (b) => patch('/api/auth/me', b),
  changePassword: (b) => post('/api/auth/password', b),
  logout: () => post('/api/auth/logout', null).catch(() => null),

  // profile
  uploadAvatar: (file) => {
    const fd = new FormData();
    fd.append('avatar', file);
    return request('POST', '/api/profile/avatar', { form: fd });
  },
  removeAvatar: () => del('/api/profile/avatar'),
  stats: () => get('/api/profile/stats'),
  earnings: () => get('/api/profile/earnings'),

  // vehicles
  vehicles: () => get('/api/vehicles'),
  vehicle: (id) => get(`/api/vehicles/${id}`),
  createVehicle: (b) => post('/api/vehicles', b),
  updateVehicle: (id, b) => patch(`/api/vehicles/${id}`, b),
  deleteVehicle: (id) => del(`/api/vehicles/${id}`),
  uploadDoc: (vehicleId, doc, file) => {
    const fd = new FormData();
    fd.append('vehicle_id', vehicleId);
    fd.append('doc', doc);
    fd.append('file', file);
    return request('POST', '/api/uploads/vehicle-doc', { form: fd });
  },

  // rides
  createRide: (b) => post('/api/rides', b),
  searchRides: (q) => get('/api/rides/search' + qs(q)),
  myRides: (scope) => get(`/api/rides/mine?scope=${scope || 'all'}`),
  ride: (id) => get(`/api/rides/${id}`),
  updateRide: (id, b) => patch(`/api/rides/${id}`, b),
  cancelRide: (id) => del(`/api/rides/${id}`),
  popularLocations: () => get('/api/rides/meta/locations'),

  // bookings
  createBooking: (b) => post('/api/bookings', b),
  verifyBooking: (id, payload) => post(`/api/bookings/${id}/verify`, payload || {}),
  myBookings: (scope) => get(`/api/bookings/mine?scope=${scope || 'all'}`),
  ridePassengers: (id) => get(`/api/bookings/for-ride/${id}`),
  cancelBooking: (id) => del(`/api/bookings/${id}`),

  // geo
  geoSearch: (q, limit = 6) => get(`/api/geo/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  geoReverse: (lat, lng) => get(`/api/geo/reverse?lat=${lat}&lng=${lng}`),
  geoRoute: (coords) => get(`/api/geo/route?points=${coords.map((c) => c.join(',')).join(',')}`),
};

function qs(params) {
  const parts = [];
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
  }
  return parts.length ? '?' + parts.join('&') : '';
}