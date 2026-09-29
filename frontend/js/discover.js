/* Discover / Take Ride: hero, search, result feed, booking flow. */

import { Api } from './api.js';
import {
  icon, h, money, toast, errMsg, skeleton, emptyState, avatar, rating,
  timeLabel, dayLabel, fmtKm, vehicleLabel, vehicleIconName, seatsDots,
  openModal, bindForm, debounce,
} from './ui.js';
import { attachAutocomplete, swapFields } from './maps.js';
import { payForBooking, splitLines } from './checkout.js';

const VEHICLE_OPTS = [
  { value: '', label: 'Any vehicle' },
  { value: '4-wheeler', label: '4-wheeler' },
  { value: '2-wheeler', label: '2-wheeler' },
  { value: 'auto', label: 'Auto' },
  { value: 'bus', label: 'Bus' },
];

export function renderDiscover(root, query) {
  const today = todayStr();
  root.innerHTML = `
    <section class="hero">
      <div class="hero__inner">
        <div style="max-width:520px">
          <span class="eyebrow" style="color:#ffd8a8">Shared commutes, smarter spending</span>
          <h1>Own the road.<br>Split the <em>fare</em>.</h1>
          <p>Ride with verified drivers going your way. Choose your seat, agree a fare, and turn everyday travel into a win for you and the planet.</p>
          <div class="hero__stat">
            <div><b>3k+</b><span>rides weekly</span></div>
            <div><b>24k</b><span>happy riders</span></div>
            <div><b>120t</b><span>CO₂ saved</span></div>
          </div>
        </div>
      </div>
    </section>

    <div class="search-card">
      <div class="search-grid">
        <div class="field ac ac--origin">
          <label for="f-origin">From</label>
          <div class="input-wrap">${icon('map-pin', 17)}
            <input class="input" id="f-origin" placeholder="Pickup point, e.g. Marathahalli" autocomplete="off" />
          </div>
        </div>
        <button type="button" class="swap-btn" id="swapBtn" title="Swap" aria-label="Swap">${icon('arrow-up-right', 17)}</button>
        <div class="field ac ac--dest">
          <label for="f-dest">To</label>
          <div class="input-wrap">${icon('flag', 17)}
            <input class="input" id="f-dest" placeholder="Destination, e.g. Whitefield" autocomplete="off" />
          </div>
        </div>
        <div class="field">
          <label for="f-date">Date</label>
          <input class="input" id="f-date" type="date" min="${today}" />
        </div>
        <div class="field">
          <label for="f-rides">Seats</label>
          <div class="stepper" id="seatStepper">
            <button type="button" data-step="-1" aria-label="Fewer seats">−</button>
            <b id="seatCount">1</b>
            <button type="button" data-step="1" aria-label="More seats">+</button>
          </div>
        </div>
        <div class="field">
          <label for="f-type">Vehicle</label>
          <select class="select" id="f-type">${VEHICLE_OPTS.map((o) => `<option value="${o.value}">${o.label}</option>`).join('')}</select>
        </div>
        <div class="field">
          <label for="f-fare">Max fare</label>
          <select class="select" id="f-fare">
            <option value="">Any</option>
            <option value="50">₹50</option><option value="100">₹100</option>
            <option value="150">₹150</option><option value="250">₹250</option><option value="400">₹400</option>
          </select>
        </div>
        <div class="field">
          <label for="f-after">Departs after</label>
          <select class="select" id="f-after">
            <option value="">Any time</option>
            <option value="06:00">6 AM</option><option value="08:00">8 AM</option>
            <option value="10:00">10 AM</option><option value="17:00">5 PM</option><option value="19:00">7 PM</option>
          </select>
        </div>
        <button class="btn btn-primary btn-lg" id="searchBtn" style="grid-column:1/-1">${icon('search', 17)} Search rides</button>
      </div>
    </div>

    <div class="results-head">
      <h2>Available rides</h2>
      <div class="flex items-center gap-12">
        <span class="results-count" id="resultsCount"></span>
        <select class="select" id="sortSel" style="width:150px">
          <option value="early">Earliest</option>
          <option value="cheap">Cheapest</option>
        </select>
      </div>
    </div>
    <div class="rides-grid" id="ridesGrid">${skeleton(3)}</div>`;

  root.querySelector('#f-date').value = today;

  const originAc = attachAutocomplete(root.querySelector('#f-origin'));
  const destAc = attachAutocomplete(root.querySelector('#f-dest'));

  root.querySelector('#swapBtn').addEventListener('click', () => {
    swapFields(root.querySelector('#f-origin'), root.querySelector('#f-dest'));
    search();
  });

  // prefilled route from hash query
  const qOrigin = query.get('origin');
  const qDest = query.get('dest');
  if (qOrigin) root.querySelector('#f-origin').value = qOrigin;
  if (qDest) root.querySelector('#f-dest').value = qDest;

  root.querySelector('#seatStepper').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-step]');
    if (!btn) return;
    const span = root.querySelector('#seatCount');
    let n = Number(span.textContent) + Number(btn.dataset.step);
    n = Math.max(1, Math.min(9, n));
    span.textContent = n;
  });

  const runSearch = debounce(search, 250);
  root.querySelectorAll('#f-date, #f-type, #f-fare, #f-after, #f-origin, #f-dest').forEach((el) => {
    el.addEventListener('change', runSearch);
    el.addEventListener('input', runSearch);
  });
  root.querySelector('#sortSel').addEventListener('change', search);
  root.querySelector('#searchBtn').addEventListener('click', search);

  search();

  async function search() {
    const grid = root.querySelector('#ridesGrid');
    grid.innerHTML = skeleton(3);
    const params = {
      origin: root.querySelector('#f-origin').value.trim() || undefined,
      destination: root.querySelector('#f-dest').value.trim() || undefined,
      date: root.querySelector('#f-date').value || undefined,
      time_from: root.querySelector('#f-after').value || undefined,
      type: root.querySelector('#f-type').value || undefined,
      max_fare: root.querySelector('#f-fare').value || undefined,
      seats: Number(root.querySelector('#seatCount').textContent) || undefined,
      sort: root.querySelector('#sortSel').value || undefined,
    };
    try {
      const res = await Api.searchRides(params);
      root.querySelector('#resultsCount').textContent = res.total
        ? `${res.total} ride${res.total === 1 ? '' : 's'} found`
        : '';
      if (!res.data.length) {
        grid.innerHTML = emptyState({
          ic: 'compass',
          title: 'No matches yet',
          desc: 'Try widening your date, removing the fare cap, or picking a nearby pickup point.',
          action: '<button class="btn btn-soft btn-sm mt-12" data-refresh>Clear filters</button>',
        });
        grid.querySelector('[data-refresh]')?.addEventListener('click', () => {
          root.querySelector('#f-fare').value = '';
          root.querySelector('#f-type').value = '';
          search();
        });
        return;
      }
      grid.innerHTML = res.data.map(rideCard).join('');
      grid.querySelectorAll('[data-cta="book"]').forEach((b) => {
        b.addEventListener('click', async (e) => {
          e.stopPropagation();
          const ride = res.data.find((r) => r.id === b.dataset.id);
          if (ride) await openBooking(ride);
        });
      });
      grid.querySelectorAll('.ride--row').forEach((card) => {
        card.addEventListener('click', () => {
          location.hash = `#/ride/${card.dataset.id}`;
        });
      });
    } catch (err) {
      grid.innerHTML = emptyState({ ic: 'alert', title: 'Search failed', desc: errMsg(err) });
    }
  }
}

function rideCard(r) {
  const seats = Math.max(0, r.seats_available || 0);
  const veh = vehicleLabel(r.vehicle && r.vehicle.type);
  return `
  <article class="card ride ride--row" data-id="${r.id}">
    <div class="ride__route">
      <div class="route">
        <div class="route__row"><span class="dot"></span>
          <div style="flex:1;min-width:0"><div class="route__label">${h(r.origin?.label)}</div>
          <div class="route__sub">${h((r.origin && r.origin.address) || '')}</div></div>
        </div>
        <div class="route__row"><span class="dot dot--green"></span>
          <div style="flex:1;min-width:0"><div class="route__label">${h(r.destination?.label)}</div>
          <div class="route__sub">${h((r.destination && r.destination.address) || '')}</div></div>
        </div>
      </div>
      <div class="flex gap-8 items-center mt-8" style="flex-wrap:wrap">
        <span class="badge badge--brand">${icon('clock', 13)} ${timeLabel(r.departure_time)}</span>
        <span class="badge badge--ghost">${dayLabel(r.departure_date)}</span>
        ${r.distance_km ? `<span class="badge badge--ghost">${fmtKm(r.distance_km)}</span>` : ''}
        <span class="badge ${seats ? 'badge--success' : 'badge--danger'}">${icon('users', 13)} ${seats} seat${seats === 1 ? '' : 's'} left</span>
      </div>
    </div>
    <div class="ride__meta">
      <div class="ride__pills">
        <span class="badge badge--ghost">${icon(vehicleIconName(r.vehicle && r.vehicle.type), 13)} ${veh}</span>
        <span>${seatsDots(seats, r.seats_total)}</span>
      </div>
      <div class="ride__driver">
        ${avatar(r.owner, 36, 'avatar--sm')}
        <div style="text-align:left"><b class="text-sm trunc" style="display:block;max-width:120px">${h((r.owner && r.owner.name) || 'Driver')}</b> ${rating(r.owner && r.owner.rating)}</div>
      </div>
      <div class="flex items-center justify-between" style="width:100%">
        <div class="ride__fare">${money(r.fare_per_seat)} <small>/ seat</small></div>
        ${seats > 0
          ? `<button class="btn btn-primary btn-sm" data-cta="book" data-id="${r.id}">Book</button>`
          : `<button class="btn btn-soft btn-sm" disabled>Full</button>`}
      </div>
    </div>
  </article>`;
}

function todayStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export async function openBooking(ride) {
  let seats = 1;
  const max = Math.max(1, ride.seats_available || 1);
  seats = Math.min(seats, max);

  const { close, el, body } = openModal({
    title: 'Book your seat',
    body: '',
    closable: true,
  });

  function total() { return (ride.fare_per_seat || 0) * seats; }

  body.innerHTML = `
    <div class="flex gap-12 items-center mb-16">
      ${avatar(ride.owner, 44)}
      <div style="min-width:0"><b style="font-size:15px">${h((ride.owner && ride.owner.name) || 'Driver')}</b>
        <div class="text-faint text-xs">${vehicleLabel(ride.vehicle && ride.vehicle.type)} · ${h(ride.vehicle && ride.vehicle.model) || ''}</div></div>
      <div style="margin-left:auto;text-align:right"><b class="text-sm" style="display:block">${dayLabel(ride.departure_date)}</b><span class="text-faint text-xs">${timeLabel(ride.departure_time)}</span></div>
    </div>
    <div class="place-card mb-16">${icon('route', 20)}<div class="flex flex-col" style="min-width:0;flex:1">
      <span class="text-sm fw-700 trunc">${h(ride.origin.label)}</span>
      <span style="height:1px;background:var(--line);margin:6px 0"></span>
      <span class="text-sm fw-700 trunc">${h(ride.destination.label)}</span></div></div>
    <div class="card card--pad mb-16" style="background:var(--surface-2)">
      <div class="flex items-center justify-between mb-8">
        <span class="text-muted text-sm">Seats</span>
        <div class="stepper">
          <button type="button" data-step="-1" aria-label="Fewer seats">−</button>
          <b id="bkN">${seats}</b>
          <button type="button" data-step="1" aria-label="More seats">+</button>
        </div>
      </div>
      <div class="flex items-center justify-between">
        <span class="text-muted text-sm">${money(ride.fare_per_seat)} × <span id="bkSeats">${seats}</span> seat<span id="bkPlural">${seats > 1 ? 's' : ''}</span></span>
        <span style="font-family:var(--font-display);font-weight:800;font-size:20px" id="bkTotal">${money(total())}</span>
      </div>
      <div class="flex items-center justify-between text-xs text-faint">
        <span>You pay now</span>
        <span id="bkSubtotal">${money(total())}</span>
      </div>
    </div>
    <div class="secure-note" id="bkSplit"></div>
    <button class="btn btn-primary btn-lg btn-block mt-16" id="bkPay">Pay ${money(total())} · ${seats} seat${seats > 1 ? 's' : ''}</button>`;

  const update = () => {
    body.querySelector('#bkN').textContent = seats;
    body.querySelector('#bkSeats').textContent = seats;
    body.querySelector('#bkPlural').textContent = seats > 1 ? 's' : '';
    body.querySelector('#bkTotal').textContent = money(total());
    body.querySelector('#bkPay').innerHTML = `Pay ${money(total())} · ${seats} seat${seats > 1 ? 's' : ''}`;
  };

  body.querySelector('.stepper').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-step]');
    if (!btn) return;
    seats = Math.max(1, Math.min(max, seats + Number(btn.dataset.step)));
    update();
  });

  body.querySelector('#bkPay').addEventListener('click', async (e) => {
    const pay = e.currentTarget;
    pay.disabled = true;
    pay.innerHTML = `<span class="spin" style="margin-right:8px"></span> Preparing secure payment…`;
    try {
      // The server creates the order and returns the authoritative amount plus
      // the frozen fee split. Nothing charged is decided in the browser.
      const res = await Api.createBooking({ ride_id: ride.id, seats });
      let booking = res.booking;
      const checkout = res.checkout;
      renderSplit(checkout);

      if (checkout.provider === 'razorpay') {
        pay.innerHTML = `<span class="spin" style="margin-right:8px"></span> Opening checkout…`;
      }

      const result = await payForBooking({
        checkout,
        bookingId: booking.id,
        ride,
        onPending: () => toast('Payment not completed. Your seats are held for a short while.', 'info'),
      });

      if (!result.settled) {
        close();
        toast('Checkout closed. No payment was made.', 'info');
        return;
      }
      showSuccess(result.booking || booking);
    } catch (err) {
      toast(errMsg(err), 'error');
      pay.disabled = false;
      update();
    }
  });

  function renderSplit(checkout) {
    const el = body.querySelector('#bkSplit');
    if (!el) return;
    const lines = splitLines(checkout);
    if (!lines.length) { el.innerHTML = ''; return; }
    el.innerHTML = lines.map((l) => `
      <div class="flex items-center justify-between text-xs" style="padding:2px 0">
        <span class="${l.muted ? 'text-faint' : 'text-muted'}">${h(l.label)}</span>
        <span class="${l.muted ? 'text-faint' : ''}">${money(l.value)}</span>
      </div>`).join('');
  }

  function showSuccess(booking) {
    body.innerHTML = `
      <div class="empty" style="padding:30px 10px">
        <span style="width:64px;height:64px;border-radius:50%;background:var(--green-soft);color:var(--green);display:grid;place-items:center">${icon('check', 30)}</span>
        <b style="font-size:18px">You're booked!</b>
        <p>${money(booking.amount)} paid for ${booking.seats} seat${booking.seats > 1 ? 's' : ''} with ${h((ride.owner && ride.owner.name) || 'driver')}.
        Reference <b>${h(booking.payment && booking.payment.reference)}</b></p>
      </div>
      <div class="flex gap-8">
        <button class="btn btn-outline btn-block" data-close>Done</button>
        <button class="btn btn-primary btn-block" data-trips>View my trips</button>
      </div>`;
    body.querySelector('[data-close]').addEventListener('click', close);
    body.querySelector('[data-trips]').addEventListener('click', () => {
      close();
      location.hash = '#/trips';
    });
  }
}