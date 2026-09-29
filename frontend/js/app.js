/* RideMate app shell: splash boot, hash router, nav, auth guards. */

import { store, Api, restoreSession } from './api.js';
import { icon, assetUrl, h, toast, closeOpenModal, errMsg } from './ui.js';
import { renderAuth } from './auth.js';
import { renderDiscover } from './discover.js';
import { renderRide } from './ride.js';
import { renderPublish } from './publish.js';
import { renderTrips } from './trips.js';
import { renderVehicles } from './vehicles.js';
import { renderProfile } from './profile.js';

const NAV = [
  { key: 'discover', href: '#/discover', label: 'Discover', auth: true },
  { key: 'publish', href: '#/publish', label: 'Publish', auth: true, cta: true },
  { key: 'trips', href: '#/trips', label: 'Trips', auth: true },
  { key: 'vehicles', href: '#/vehicles', label: 'Vehicles', auth: true },
  { key: 'profile', href: '#/profile', label: 'Profile', auth: true },
];

const TITLES = {
  '/discover': 'Discover rides · RideMate',
  '/publish': 'Publish a ride · RideMate',
  '/trips': 'My trips · RideMate',
  '/vehicles': 'My vehicles · RideMate',
  '/profile': 'Profile · RideMate',
  '/ride': 'Ride details · RideMate',
  '/login': 'Log in · RideMate',
  '/signup': 'Create account · RideMate',
};

function parseHash() {
  const raw = (location.hash || '#/discover').slice(1);
  const [pathPart, queryPart] = raw.split('?');
  const segments = pathPart.split('/').filter(Boolean);
  const query = new URLSearchParams(queryPart || '');
  return {
    path: '/' + segments.join('/'),
    segments,
    query,
  };
}

function getShell() {
  return {
    shell: document.getElementById('appShell'),
    authRoot: document.getElementById('authRoot'),
    view: document.getElementById('view'),
  };
}

const NAV_ICON = { discover: 'compass', publish: 'plus', trips: 'calendar', vehicles: 'car', profile: 'user' };

function buildNav() {
  const sideNav = document.getElementById('sideNav');
  const bottomNav = document.getElementById('bottomnav');

  const link = (item, bottom) => {
    if (bottom) {
      const inner = item.cta
        ? `<span class="fab">${icon('plus', 21)}</span>`
        : `${icon(NAV_ICON[item.key] || 'info', 21)}${item.label}`;
      return `<a class="bnav-link ${item.cta ? 'bnav-cta' : ''}" data-nav="${item.key}" href="${item.href}">${inner}</a>`;
    }
    return `<a class="nav-link" data-nav="${item.key}" href="${item.href}">${icon(NAV_ICON[item.key] || 'info')}${item.label}</a>`;
  };

  sideNav.innerHTML = NAV.map((n) => link(n, false)).join('');
  bottomNav.innerHTML = NAV.map((n) => link(n, true)).join('');
}

function setActiveNav(path) {
  let key = path.split('/')[1] || 'discover';
  if (key === 'ride') key = 'discover';
  document.querySelectorAll('[data-nav]').forEach((el) => {
    el.classList.toggle('active', el.dataset.nav === key);
  });
}

function hydrateUser() {
  const u = store.user || {};
  const initials = (u.name || '?').trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join('').toUpperCase();
  const sideAvatar = document.getElementById('sideAvatar');
  const topAvatar = document.getElementById('topAvatar');
  if (sideAvatar) {
    sideAvatar.innerHTML = u.photo_url ? `<img src="${h(assetUrl(u.photo_url))}" alt="${h(u.name || '')}" />` : h(initials);
  }
  if (topAvatar) {
    topAvatar.innerHTML = u.photo_url ? `<img src="${h(assetUrl(u.photo_url))}" alt="${h(u.name || '')}" />` : h(initials);
  }
  const name = document.getElementById('sideUserName');
  const email = document.getElementById('sideUserEmail');
  if (name) name.textContent = u.name || '';
  if (email) email.textContent = u.email || '';
}

function showShell(visible) {
  const { shell, authRoot } = getShell();
  shell.classList.toggle('hidden', !visible);
  authRoot.classList.toggle('hidden', visible);
}

/* ---------- routing ---------- */
async function route() {
  const { path, segments, query } = parseHash();
  closeOpenModal();
  const { view, authRoot } = getShell();

  // public auth pages
  if (path === '/login' || path === '/signup') {
    if (store.user) { location.replace('#/discover'); return; }
    showShell(false);
    document.title = TITLES[path];
    setActiveNav('');
    renderAuth(authRoot, path, query);
    return;
  }

  // everything else requires auth
  if (!store.user) { location.replace('#/login'); return; }

  showShell(true);
  hydrateUser();
  setActiveNav(path);
  document.title = TITLES[path] || 'RideMate';

  const load = (fn) => {
    view.innerHTML = '<div class="skeleton-list">' + Array.from({ length: 3 }, () => '<div class="skeleton" style="height:140px"></div>').join('') + '</div>';
    Promise.resolve(fn(view)).catch((err) => {
      toast(errMsg(err), 'error');
      view.innerHTML = '';
    });
  };

  switch (path) {
    case '/discover': load((v) => renderDiscover(v, query)); break;
    case '/publish': load((v) => renderPublish(v, query)); break;
    case '/ride':
    case '/rides':
      if (!segments[1]) { location.replace('#/discover'); return; }
      load((v) => renderRide(v, { id: segments[1] }));
      break;
    case '/trips': load((v) => renderTrips(v, query)); break;
    case '/vehicles': load((v) => renderVehicles(v, query)); break;
    case '/profile': load((v) => renderProfile(v, query)); break;
    default: location.replace('#/discover');
  }
}

/* ---------- boot ---------- */
function attachGlobalEvents() {
  window.addEventListener('hashchange', route);

  window.addEventListener('ridemate:logout', () => {
    hydrateUser();
    location.replace('#/login');
  });

  window.addEventListener('ridemate:authed', () => {
    hydrateUser();
    location.replace('#/discover');
  });

  document.getElementById('logoutBtn')?.addEventListener('click', async () => {
    await Api.logout();
    store.clear();
    toast('Logged out. See you soon!', 'info');
    hydrateUser();
    location.replace('#/login');
  });

  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-action]')) {
      const el = e.target.closest('[data-action]');
      closeOpenModal();
    }
  });
}

async function bootSplash() {
  const splash = document.getElementById('splash');
  const start = performance.now();
  const minTime = 900;
  const done = () => {
    splash.classList.add('is-out');
    setTimeout(() => splash.remove(), 550);
  };
  try {
    // In-memory token on this page (or a silent rotation of the HttpOnly
    // refresh cookie after a hard reload), then prove it against /me.
    if (store.token || store.hadSession) {
      const res = await restoreSession();
      if (res) store.user = res.user;
    } else {
      store.user = null;
    }
  } catch (err) {
    if (err.status === 401) store.clear();
    else if (!store.user) { /* offline & no cached user */ }
  }
  const elapsed = performance.now() - start;
  if (elapsed < minTime) setTimeout(done, minTime - elapsed);
  else done();
}

async function boot() {
  buildNav();
  attachGlobalEvents();
  await bootSplash();
  route();
}

boot();