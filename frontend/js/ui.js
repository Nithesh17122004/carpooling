/* Shared UI helpers: icons, toasts, modals, formatters, components. */

import { API_BASE } from './api.js';

const ICONS = {
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>',
  'map-pin': '<path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/>',
  flag: '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><path d="M4 22v-7"/>',
  compass: '<circle cx="12" cy="12" r="10"/><path d="m16.24 7.76-2.12 6.36-6.36 2.12 2.12-6.36 6.36-2.12z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  user: '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
  star: '<path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/>',
  route: '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>',
  leaf: '<path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z"/><path d="M2 21c0-3 1.85-5.36 5.08-6"/>',
  x: '<path d="M18 6 6 18M6 6l12 12"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  'chevron-down': '<path d="m6 9 6 6 6-6"/>',
  'chevron-left': '<path d="m15 18-6-6 6-6"/>',
  'chevron-right': '<path d="m9 18 6-6-6-6"/>',
  'arrow-right': '<path d="M5 12h14M12 5l7 7-7 7"/>',
  'arrow-up-right': '<path d="M7 17 17 7M7 7h10v10"/>',
  phone: '<path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/>',
  mail: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 5L2 7"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
  camera: '<path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  edit: '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  alert: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4M12 17h.01"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1 1 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
  wallet: '<path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/><path d="M18 12a2 2 0 0 0 0 4h4v-4Z"/>',
  send: '<path d="m22 2-7 20-4-9-9-4Z"/><path d="M22 2 11 13"/>',
  upload: '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/>',
  map: '<path d="M9 3 3 5v16l6-2 6 2 6-2V3l-6 2-6-2z"/><path d="M9 3v16M15 5v16"/>',
  navigation: '<path d="m3 11 19-9-9 19-2-8-8-2z"/>',
  car: '<path d="M5.2 11.2 6.7 6.8A2 2 0 0 1 8.6 5.4h6.8a2 2 0 0 1 1.9 1.4l1.5 4.4"/><path d="M3.6 11.4h16.8v4.4h-1.9a2 2 0 0 1-4 0H9.5a2 2 0 0 1-4 0H3.6z"/><circle cx="7.4" cy="15.9" r=".9"/><circle cx="16.6" cy="15.9" r=".9"/>',
  motorcycle: '<circle cx="5.5" cy="17.5" r="3.5"/><circle cx="18.5" cy="17.5" r="3.5"/><path d="M15 5h2.5l.5 3-3.5 3.5H8.5"/>',
  bike: '<circle cx="5" cy="17" r="3"/><circle cx="19" cy="17" r="3"/><path d="M8 17h4l4-8h3"/>',
  bus: '<rect x="4" y="3" width="16" height="15" rx="2"/><path d="M4 11h16M8 19v2M16 19v2"/><circle cx="8.5" cy="14.5" r="0.9"/><circle cx="15.5" cy="14.5" r="0.9"/>',
  twowheeler: '<circle cx="6" cy="17" r="3"/><circle cx="18" cy="17" r="3"/><path d="M8.5 17H14l1.5-6H8.5M14 6h4l2 5M6 9h4"/>',
  sparkle: '<path d="M12 3l1.9 5.4L19 10.3l-5.1 1.9L12 17.6l-1.9-5.4L5 10.3l5.1-1.9z"/>',
  heart: '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>',
  zap: '<path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/>',
  eye: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
  'eye-off': '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><path d="m1 1 22 22"/>',
};

export function icon(name, size = 18, cls = '') {
  const path = ICONS[name] || ICONS.info;
  return `<svg class="${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;
}

export function h(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

export function initials(name = '') {
  return name.trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join('').toUpperCase() || '?';
}

export function money(n) {
  const v = Math.round(Number(n) || 0);
  return '₹' + v.toLocaleString('en-IN');
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function parseDate(s) {
  if (!s) return null;
  const [y, m, d] = String(s).split('-').map(Number);
  if (!y || !m || !d) return null;
  return new Date(y, m - 1, d);
}

export function dayLabel(dateStr) {
  const d = parseDate(dateStr);
  if (!d) return dateStr;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((d - today) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Tomorrow';
  return `${WEEKDAYS[d.getDay()]}, ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

export function dateLabel(dateStr) {
  const d = parseDate(dateStr);
  if (!d) return dateStr;
  return `${WEEKDAYS[d.getDay()]} ${d.getDate()} ${MONTHS[d.getMonth()]}`;
}

export function timeLabel(t) {
  if (!t) return '';
  const [hh, mm] = String(t).split(':');
  const hour = Number(hh);
  const suffix = hour >= 12 ? 'PM' : 'AM';
  const hr = hour % 12 || 12;
  return `${hr}:${mm} ${suffix}`;
}

export function fmtKm(n) {
  const v = Number(n) || 0;
  return v >= 10 ? `${Math.round(v)} km` : `${v.toFixed(1)} km`;
}

export function fmtMin(n) {
  const v = Number(n) || 0;
  if (v < 60) return `${v} min`;
  const hr = Math.floor(v / 60);
  const mn = v % 60;
  return mn ? `${hr}h ${mn}m` : `${hr}h`;
}

export function durationLabel(min) {
  return fmtMin(min);
}

export function vehicleLabel(type) {
  return {
    '2-wheeler': '2-wheeler',
    '4-wheeler': '4-wheeler',
    auto: 'Auto',
    bus: 'Bus',
  }[type] || type || 'Car';
}

export function vehicleIconName(type) {
  return {
    '2-wheeler': 'twowheeler',
    '4-wheeler': 'car',
    auto: 'motorcycle',
    bus: 'bus',
  }[type] || 'car';
}

export function seatsDots(avail, total) {
  const dots = [];
  const off = Math.max(0, (total || avail) - (avail || 0));
  for (let i = 0; i < (avail || 0); i++) dots.push('<i></i>');
  for (let i = 0; i < off; i++) dots.push('<i class="off"></i>');
  return `<span class="seats-dots" aria-label="${avail} of ${total} seats">${dots.join('')}</span>`;
}

export function avatar(user, size = 44, cls = '') {
  const name = (user && user.name) || '';
  if (user && user.photo_url) {
    return `<span class="avatar ${cls}" style="width:${size}px;height:${size}px;font-size:${size * 0.36}px"><img src="${h(assetUrl(user.photo_url))}" alt="${h(name)}" loading="lazy" /></span>`;
  }
  return `<span class="avatar ${cls}" style="width:${size}px;height:${size}px;font-size:${size * 0.36}px">${h(initials(name))}</span>`;
}

export function rating(r) {
  const v = Number(r || 0);
  const filled = Math.min(5, Math.round(v));
  let stars = '';
  for (let i = 1; i <= 5; i++) stars += icon('star', 13, i <= filled ? '' : 'off');
  return `<span class="rating">${stars}<b>${v ? v.toFixed(1) : '—'}</b></span>`;
}

/* ---------- toasts ---------- */
/** Resolve API-relative asset URLs against the API origin. */
export function assetUrl(p) {
  if (!p) return '';
  if (p.startsWith('/') && API_BASE) return API_BASE + p;
  return p;
}

export function toast(message, type = 'info', ms = 3000) {
  const root = document.getElementById('toastRoot');
  if (!root) return;
  const el = document.createElement('div');
  el.className = `toast toast--${type}`;
  const ic = { success: 'check', error: 'alert', warn: 'alert', info: 'info' }[type] || 'info';
  el.innerHTML = `${icon(ic, 18)}<span>${h(message)}</span>`;
  root.appendChild(el);
  setTimeout(() => {
    el.classList.add('is-out');
    setTimeout(() => el.remove(), 350);
  }, ms);
  return el;
}

/* ---------- modal / sheet ---------- */
let modalOpen = false;

export function openModal({ title = '', body = '', foot = '', wide = false, lg = false, closable = true } = {}) {
  const root = document.getElementById('modalRoot');
  if (!root) return { close() {} };
  closeOpenModal();

  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = `
    <div class="modal ${wide ? 'modal--wide' : lg ? 'modal--lg' : ''}" role="dialog" aria-modal="true">
      <div class="modal__head">
        <h3>${title}</h3>
        ${closable ? `<button class="btn-icon btn-icon--circle" data-close aria-label="Close">${icon('x', 18)}</button>` : ''}
      </div>
      <div class="modal__body">${body}</div>
      ${foot ? `<div class="modal__foot">${foot}</div>` : ''}
    </div>`;
  root.appendChild(backdrop);
  modalOpen = true;
  document.body.classList.add('modal-open');

  const close = (animate = true) => {
    if (!backdrop.isConnected) return;
    if (animate) backdrop.classList.add('closing');
    setTimeout(() => {
      backdrop.remove();
      modalOpen = false;
      document.body.classList.remove('modal-open');
    }, animate ? 190 : 0);
  };
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) close();
  });
  backdrop.querySelector('[data-close]')?.addEventListener('click', () => close());
  return { close, el: backdrop, body: backdrop.querySelector('.modal__body') };
}

export function closeOpenModal() {
  const root = document.getElementById('modalRoot');
  if (root) root.innerHTML = '';
  modalOpen = false;
  document.body.classList.remove('modal-open');
}

export function confirmDialog({ title = 'Are you sure?', message = '', confirmLabel = 'Confirm', danger = false } = {}) {
  return new Promise((resolve) => {
    const { close, el } = openModal({
      title,
      body: `<p class="text-muted" style="font-size:14.5px">${message}</p>`,
      foot: `<button class="btn btn-outline" data-no>Cancel</button>
             <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-yes>${confirmLabel}</button>`,
    });
    el.querySelector('[data-no]').addEventListener('click', () => { close(); resolve(false); });
    el.querySelector('[data-yes]').addEventListener('click', () => { close(); resolve(true); });
    el.addEventListener('click', (e) => { if (e.target === el) { close(); resolve(false); } });
  });
}

/* ---------- misc ---------- */
export function skeleton(count = 3) {
  return Array.from({ length: count }, () => '<div class="skeleton" style="height:120px"></div>').join('');
}

export function emptyState({ ic = 'compass', title = 'Nothing here yet', desc = '', action = '' } = {}) {
  return `<div class="empty">${icon(ic, 46)}<b>${h(title)}</b>${desc ? `<p>${desc}</p>` : ''}${action}</div>`;
}

export function spinner(ink = false) {
  return `<span class="spin ${ink ? 'spin--ink' : ''}"></span>`;
}

export function todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function futureStr(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function val(el) {
  return (el && el.value !== undefined ? el.value : '');
}

export function formData(form) {
  return Object.fromEntries(new FormData(form).entries());
}

/** Wrap a submit handler: disables the submit button while running. */
export function bindForm(form, handler) {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const submit = form.querySelector('[type="submit"]');
    if (submit && submit.disabled) return;
    const original = submit ? submit.innerHTML : '';
    if (submit) { submit.disabled = true; submit.classList.add('btn--loading'); submit.innerHTML = `${spinner()} <span>Please wait…</span>`; }
    try {
      await handler(form);
    } finally {
      if (submit) { submit.disabled = false; submit.classList.remove('btn--loading'); submit.innerHTML = original; }
    }
  });
}

/** Convert an APIError to a user message. */
export function errMsg(err, fallback = 'Something went wrong.') {
  return (err && err.message) || fallback;
}

export function debounce(fn, ms = 350) {
  let t;
  return function (...args) {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(this, args), ms);
  };
}

/* Mark a field invalid and clear it after focus. */
export function fieldError(input, message) {
  const wrap = input.closest('.field');
  input.classList.add('invalid');
  let tag = wrap ? wrap.querySelector('.form-error') : null;
  if (!tag) {
    tag = document.createElement('span');
    tag.className = 'form-error';
    if (wrap) wrap.appendChild(tag);
    else input.parentNode.appendChild(tag);
  }
  tag.textContent = message;
  const clear = () => { input.classList.remove('invalid'); if (tag) tag.remove(); input.removeEventListener('input', clear); };
  input.addEventListener('input', clear, { once: true });
}