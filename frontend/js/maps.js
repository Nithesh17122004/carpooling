/* Leaflet map helpers + geocoding autocomplete (OSM / OSRM via backend). */

import { Api } from './api.js';
import { h, debounce, icon } from './ui.js';

const attachedRefs = new WeakSet();

function L() {
  return window.L || null;
}

/* ---------- map lifecycle ---------- */
export function createMap(el, { zoom = true } = {}) {
  if (!L() || !el) return null;
  el.innerHTML = '';
  const map = L().map(el, { zoomControl: false, attributionControl: true });
  L().tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  map.setView([12.9716, 77.5946], 11);
  if (zoom) L().control.zoom({ position: 'bottomright' }).addTo(map);
  return map;
}

export function destroyMap(map) {
  if (map) map.remove();
}

/* ---------- markers ---------- */
function pinHtml(color) {
  return `<span style="display:block;width:26px;height:26px;border-radius:50% 50% 50% 4px;transform:rotate(-45deg);background:${color};border:3px solid #fff;box-shadow:0 6px 16px rgba(16,19,38,.28)"><i style="display:block;width:9px;height:9px;border-radius:50%;background:#fff;position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);"></i></span>`;
}

export function addMarker(map, pt, { color = '#635bff', label = '' } = {}) {
  if (!map || !pt) return null;
  const marker = L().marker([pt.lat, pt.lng], {
    icon: L().divIcon({
      className: '', html: pinHtml(color),
      iconSize: [26, 26], iconAnchor: [13, 13],
    }),
  }).addTo(map);
  if (label) marker.bindTooltip(label, { direction: 'top', offset: [0, -14] });
  return marker;
}

export function fitPoints(map, points) {
  if (!map || !points.length) return;
  if (points.length === 1) { map.setView([points[0].lat, points[0].lng], 13); return; }
  map.fitBounds(L().latLngBounds(points.map((p) => [p.lat, p.lng])), { padding: [40, 40] });
}

/* ---------- routing ---------- */
export async function drawRoute(map, a, b) {
  if (!map || !a || !b) return null;
  try {
    const data = await Api.geoRoute([[a.lat, a.lng], [b.lat, b.lng]]);
    const coords = data.route.map((c) => ({ lat: c[0], lng: c[1] }));
    const line = L().polyline(coords, { color: '#635bff', weight: 4.5, opacity: 0.92 }).addTo(map);
    fitPoints(map, coords);
    line.bringToFront();
    return data;
  } catch {
    fitPoints(map, [a, b]);
    return null;
  }
}

/* ---------- autocomplete ---------- */
export function acPoint(input) {
  if (!input || !input.dataset.lat) return null;
  return {
    label: input.dataset.label || input.value,
    address: input.dataset.address || input.dataset.label,
    lat: Number(input.dataset.lat),
    lng: Number(input.dataset.lng),
  };
}

export function setAcPoint(input, pt) {
  if (!input || !pt) return;
  input.value = pt.label || '';
  if (!pt.lat && pt.lat !== 0) return;
  Object.assign(input.dataset, {
    label: pt.label || '',
    address: pt.address || pt.label || '',
    lat: pt.lat,
    lng: pt.lng,
  });
}

export function clearAcPoint(input) {
  if (!input) return;
  delete input.dataset.label;
  delete input.dataset.address;
  delete input.dataset.lat;
  delete input.dataset.lng;
}

/**
 * Attach a geocoding dropdown to a text input.
 * Uses whatever wrapper with class .ac (auto-created if missing).
 * Returns an object with set(value), clear(), point().
 */
export function attachAutocomplete(input, { onPick, min = 2 } = {}) {
  if (!input) return null;
  let wrap = input.closest('.ac');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'ac ac--auto';
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);
  }

  let list = null;
  const close = () => {
    if (list) { list.remove(); list = null; }
  };

  const render = (skipClear) => {
    close();
    if (!skipClear) clearAcPoint(input);
  };

  const pick = (item) => {
    setAcPoint(input, item);
    close();
    input.focus();
    if (onPick) onPick(item);
  };

  const runQuery = debounce(async (q) => {
    if (q.length < min) return close();
    close();
    let items = [];
    try {
      const res = await Api.geoSearch(q);
      items = res.data || [];
    } catch { /* offline: no suggestions */ }
    itemsCache = items;
    if (!items.length) return;
    list = document.createElement('div');
    list.className = 'ac-list';
    list.innerHTML = items.map((it, i) => `
      <div class="ac-item" data-i="${i}">
        ${icon('map-pin', 17)}
        <div><b>${h(it.label)}</b>${it.address && it.address !== it.label ? `<span>${h(it.address)}</span>` : ''}</div>
      </div>`).join('');
    list.addEventListener('mousedown', (e) => {
      const itemEl = e.target.closest('.ac-item');
      if (itemEl) {
        e.preventDefault(); // keep focus in input
        pick(items[Number(itemEl.dataset.i)]);
      }
    });
    wrap.appendChild(list);
  }, 380);

  const onKey = (e) => {
    if ((e.key === 'Escape' || e.key === 'Tab') && list) { close(); }
    if (e.key === 'Enter' && list && list.firstElementChild) {
      // pick the first suggestion if there is one; otherwise let form submit
      e.preventDefault();
      pick(itemsCache[0]);
    }
  };
  let itemsCache = [];
  input.addEventListener('input', (e) => {
    clearAcPoint(input);
    runQuery(e.target.value.trim());
  });
  input.addEventListener('keydown', onKey);
  if (!attachedRefs.has(input)) {
    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) close();
    });
    attachedRefs.add(input);
  }

  return {
    set(point) { setAcPoint(input, point); },
    clear() { render(true); },
    point() { return acPoint(input); },
    close,
  };
}

/* swap origin/destination values between two autocomplete inputs */
export function swapFields(a, b) {
  const va = a.value, da = { ...a.dataset };
  const vb = b.value, db = { ...b.dataset };
  a.value = vb; Object.keys(da).forEach((k) => delete a.dataset[k]); Object.assign(a.dataset, db);
  b.value = va; Object.keys(db).forEach((k) => delete b.dataset[k]); Object.assign(b.dataset, da);
}