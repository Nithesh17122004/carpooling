/* Give Ride / Publish: pick a vehicle, plan a route, set seats + fare. */

import { Api } from './api.js';
import {
  icon, h, money, toast, errMsg, emptyState, fmtKm, fmtMin, todayStr, futureStr,
  bindForm, fieldError,
} from './ui.js';
import { attachAutocomplete, createMap, destroyMap, addMarker, drawRoute } from './maps.js';

const TIME_CHIPS = [
  { label: 'Morning', values: ['06:00', '07:00', '08:00'], active: '08:00' },
  { label: 'Midday', values: ['12:00', '14:00'], active: '12:00' },
  { label: 'Evening', values: ['17:30', '18:30', '20:00'], active: '18:30' },
];

export async function renderPublish(root) {
  let vehicles = [];
  try {
    vehicles = (await Api.vehicles()).data || [];
  } catch (err) {
    root.innerHTML = emptyState({ ic: 'alert', title: 'Could not load vehicles', desc: errMsg(err) });
    return;
  }

  if (!vehicles.length) {
    root.innerHTML = `
      <div class="page-head"><div><div class="eyebrow">Driving</div><h1>Publish a ride</h1>
      <p>You need at least one vehicle to share your first ride.</p></div></div>
      <div class="card card--pad">${emptyState({
        ic: 'car', title: 'No vehicles yet',
        desc: 'Add your vehicle once and reuse it for every ride.',
        action: '<a class="btn btn-primary btn-sm mt-12" href="#/vehicles">Add a vehicle</a>',
      })}</div>`;
    return;
  }

  root.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Driving soon?</div><h1>Publish a ride</h1>
    <p>Set your route and seats — riders will find you instantly.</p></div>
    <a class="btn btn-outline" href="#/vehicles">${icon('car', 16)} Manage vehicles</a></div>

    <form id="publishForm" class="detail-grid" novalidate>
      <div class="detail-stack">
        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Route</div>
          <div class="grid-2">
            <div class="field ac">
              <label for="p-origin">Pickup point</label>
              <div class="input-wrap">${icon('map-pin', 17)}
                <input class="input" id="p-origin" placeholder="e.g. Marathahalli" autocomplete="off" required />
              </div>
            </div>
            <div class="field ac">
              <label for="p-dest">Destination</label>
              <div class="input-wrap">${icon('flag', 17)}
                <input class="input" id="p-dest" placeholder="e.g. Whitefield" autocomplete="off" required />
              </div>
            </div>
          </div>
          <div class="map-box map-box--sm mt-16"><div class="map-canvas" id="pubMap"></div></div>
          <div class="form-hint mt-8" id="routeMeta"></div>
        </div>

        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Date &amp; time</div>
          <div class="chip-row mb-8" id="dateChips">
            <button type="button" class="chip active" data-days="0">Today</button>
            <button type="button" class="chip" data-days="1">Tomorrow</button>
          </div>
          <div class="grid-2">
            <div class="field"><label for="p-date">Pick a date</label>
              <input class="input" id="p-date" type="date" min="${todayStr()}" required /></div>
            <div class="field"><label for="p-time">Departure time</label>
              <input class="input" id="p-time" type="time" required /></div>
          </div>
          <div class="chip-row" id="timeChips" style="margin-top:12px"></div>
        </div>

        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Notes to riders <span class="text-faint" style="font-weight:500">(optional)</span></div>
          <textarea class="textarea" id="p-notes" maxlength="300" placeholder="Luggage space, favourite music, meeting point…"></textarea>
        </div>
      </div>

      <div class="detail-stack">
        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Vehicle</div>
          <div class="field">
            <label for="p-vehicle">Choose vehicle</label>
            <select class="select" id="p-vehicle">${vehicles.map((v) => `<option value="${v.id}" data-seats="${v.seat_count}" data-vt="${v.vehicle_type}">${h(v.vehicle_model || v.vehicle_number)} · ${h(v.vehicle_number)}</option>`).join('')}</select>
          </div>
          <div id="vehicleInfo" class="mt-8 form-hint"></div>
        </div>

        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Seats to offer</div>
          <div class="flex items-center justify-between">
            <div class="stepper" id="seatsStepper">
              <button type="button" data-step="-1">−</button><b id="seatsVal">—</b><button type="button" data-step="1">+</button>
            </div>
            <span class="text-faint text-sm" id="seatsMax"></span>
          </div>
        </div>

        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Fare per seat</div>
          <div class="flex items-center gap-8">
            <div class="input-wrap" style="flex:1">
              <input class="input" id="p-fare" type="number" inputmode="numeric" min="1" placeholder="₹" required style="padding-left:34px" />
              <span class="input-suffix" style="left:16px;right:auto"><b>₹</b></span>
            </div>
          </div>
          <div class="form-hint mt-8" id="fareHint"></div>
        </div>

        <button type="submit" class="btn btn-primary btn-lg btn-block">${icon('send', 17)} Publish ride</button>
        <p class="form-hint" style="text-align:center">You can cancel anytime before departure.</p>
      </div>
    </form>`;

  /* vehicle seat bookkeeping */
  const vehicleSel = root.querySelector('#p-vehicle');
  const seatsVal = root.querySelector('#seatsVal');
  const seatsMax = root.querySelector('#seatsMax');
  let currentSeats = 0;

  function setSeatRange() {
    const opt = vehicleSel.options[vehicleSel.selectedIndex];
    const max = Number(opt.dataset.seats) || 1;
    currentSeats = Math.max(1, Math.min(currentSeats || max, max));
    seatsVal.textContent = currentSeats;
    seatsMax.textContent = `of ${max} available`;
    const info = opt.dataset.vt;
    root.querySelector('#vehicleInfo').textContent = `${info} · up to ${max} passenger seats`.replace('· up to', '— up to');
  }
  vehicleSel.addEventListener('change', setSeatRange);
  setSeatRange();

  root.querySelector('#seatsStepper').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-step]');
    if (!btn) return;
    const max = Number(vehicleSel.options[vehicleSel.selectedIndex].dataset.seats) || 1;
    currentSeats = Math.max(1, Math.min(max, currentSeats + Number(btn.dataset.step)));
    seatsVal.textContent = currentSeats;
  });

  /* date chips */
  const dateInput = root.querySelector('#p-date');
  dateInput.value = todayStr();
  root.querySelectorAll('#dateChips .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      root.querySelectorAll('#dateChips .chip').forEach((c) => c.classList.remove('active'));
      chip.classList.add('active');
      dateInput.value = futureStr(Number(chip.dataset.days));
    });
  });

  /* time chips + input */
  const timeInput = root.querySelector('#p-time');
  const timeChips = root.querySelector('#timeChips');
  TIME_CHIPS.forEach((group) => {
    const sep = document.createElement('span');
    sep.style.cssText = 'align-self:center;font-size:11px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:.05em';
    sep.textContent = group.label;
    timeChips.appendChild(sep);
    group.values.forEach((t) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip' + (group.active === t ? ' active' : '');
      chip.textContent = t;
      chip.addEventListener('click', () => {
        const all = timeChips.querySelectorAll('.chip');
        all.forEach((c) => c.classList.remove('active'));
        chip.classList.add('active');
        timeInput.value = t;
      });
      timeChips.appendChild(chip);
    });
  });
  timeInput.addEventListener('input', () => {
    timeChips.querySelectorAll('.chip').forEach((c) => c.classList.remove('active'));
  });

  /* route + map */
  let routeDist = null;
  const mapBox = root.querySelector('#pubMap');
  const map = createMap(mapBox, { zoom: false });
  let markers = [];

  function refreshRoute() {
    const o = originAc.point();
    const d = destAc.point();
    root.querySelector('#routeMeta').textContent = '';
    if (markers.length) { markers.forEach((m) => m.remove()); markers = []; }
    if (!map) { return; }
    if (o) { markers.push(addMarker(map, o, { color: '#635bff' })); }
    if (d) { markers.push(addMarker(map, d, { color: '#0ea96b' })); }
    routeDist = null;
    if (o && d) {
      drawRoute(map, o, d).then((data) => {
        if (!data) return;
        routeDist = data.distance_km;
        root.querySelector('#routeMeta').innerHTML =
          `${icon('route', 13)}&nbsp; ≈ ${fmtKm(data.distance_km)} · ${fmtMin(data.duration_min)} drive`;
        suggestFare();
      });
    } else if (o || d) {
      map.setView([(o || d).lat, (o || d).lng], 13);
    }
  }

  const originAc = attachAutocomplete(root.querySelector('#p-origin'), { onPick: () => refreshRoute() });
  const destAc = attachAutocomplete(root.querySelector('#p-dest'), { onPick: () => refreshRoute() });

  /* fare suggestion */
  const fareInput = root.querySelector('#p-fare');
  function suggestFare() {
    if (!routeDist) return;
    const suggested = Math.max(30, Math.round(routeDist * 2.5 / 5) * 5);
    root.querySelector('#fareHint').innerHTML =
      `Suggested for ${currentSeats} seats: <b>${money(suggested)}/seat</b> · ${fmtKm(routeDist)} total.`;
    if (!fareInput.value) fareInput.value = suggested;
  }
  fareInput.addEventListener('focus', () => { if (!fareInput.value && routeDist) suggestFare(); });

  /* submit */
  const form = root.querySelector('#publishForm');
  bindForm(form, async () => {
    const origin = originAc.point();
    const dest = destAc.point();
    if (!origin) { fieldError(root.querySelector('#p-origin'), 'Choose a pickup from the suggestions.'); toast('Pick a pickup point.', 'error'); return; }
    if (!dest) { fieldError(root.querySelector('#p-dest'), 'Choose a destination from the suggestions.'); toast('Pick a destination.', 'error'); return; }
    const vdate = dateInput.value;
    const vtime = timeInput.value;
    if (!vdate || !vtime) { toast('Pick a date and time for your ride.', 'error'); return; }

    try {
      const res = await Api.createRide({
        vehicle_id: vehicleSel.value,
        origin, destination: dest,
        departure_date: vdate, departure_time: vtime,
        seats_total: currentSeats,
        fare_per_seat: Number(fareInput.value),
        notes: root.querySelector('#p-notes').value.trim() || undefined,
        distance_km: routeDist || undefined,
      });
      toast('Ride published — riders can book now! 🎉', 'success');
      setTimeout(() => { location.hash = '#/trips?tab=given'; }, 800);
    } catch (err) {
      toast(errMsg(err), 'error');
    }
  });
}