/* My trips: booked rides (rider) and given rides (driver), upcoming & past. */

import { Api } from './api.js';
import {
  icon, h, money, toast, errMsg, skeleton, emptyState, avatar, rating,
  timeLabel, dayLabel, vehicleLabel, vehicleIconName, seatsDots, confirmDialog,
} from './ui.js';

export function renderTrips(root, query) {
  let tab = query.get('tab') === 'given' ? 'given' : 'booked';
  let scope = 'upcoming';
  let listEl;

  root.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Your movement</div><h1>My trips</h1>
    <p>Every ride you've taken or hosted, in one place.</p></div></div>

    <div class="tabs" role="tablist">
      <button class="tab" data-tab="booked" role="tab">Booked rides</button>
      <button class="tab" data-tab="given" role="tab">Rides I gave</button>
    </div>

    <div class="seg mb-16" id="scopeSeg">
      <button data-scope="upcoming" class="active">Upcoming</button>
      <button data-scope="past">Past</button>
    </div>

    <div class="rides-grid" id="tripsList">${skeleton(3)}</div>`;

  listEl = root.querySelector('#tripsList');

  root.querySelectorAll('.tab').forEach((t) => {
    t.addEventListener('click', () => {
      tab = t.dataset.tab;
      root.querySelectorAll('.tab').forEach((x) => x.classList.toggle('active', x === t));
      load();
    });
  });
  root.querySelectorAll('#scopeSeg [data-scope]').forEach((b) => {
    b.addEventListener('click', () => {
      scope = b.dataset.scope;
      root.querySelectorAll('#scopeSeg [data-scope]').forEach((x) => x.classList.toggle('active', x === b));
      load();
    });
  });

  root.querySelector('#scopeSeg').style.display = 'inline-flex';
  setTabActive();
  load();

  function setTabActive() {
    root.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t.dataset.tab === tab));
  }

  async function load() {
    listEl.innerHTML = skeleton(2);
    try {
      if (tab === 'booked') {
        const res = await Api.myBookings(scope === 'upcoming' ? 'upcoming' : 'past');
        renderBooked(res.data);
      } else {
        const res = await Api.myRides(scope);
        renderGiven(res.data);
      }
    } catch (err) {
      listEl.innerHTML = emptyState({ ic: 'alert', title: 'Could not load trips', desc: errMsg(err) });
    }
  }

  function renderBooked(bookings) {
    if (!bookings.length) {
      listEl.innerHTML = tab === 'booked'
        ? emptyState({
          ic: 'compass', title: scope === 'upcoming' ? 'No upcoming bookings' : 'No past bookings yet',
          desc: scope === 'upcoming' ? 'Find a ride heading your way and lock a seat.'
            : 'Your completed trips will appear here.',
          action: scope === 'upcoming' ? '<a class="btn btn-primary btn-sm mt-12" href="#/discover">Find a ride</a>' : '',
        })
        : '';
      return;
    }
    listEl.innerHTML = bookings.map((b) => {
      const ride = b.ride;
      const snap = b.ride_snapshot;
      const origin = (ride && ride.origin) || (snap && snap.origin) || {};
      const dest = (ride && ride.destination) || (snap && snap.destination) || {};
      const vdate = (ride && ride.departure_date) || (snap && snap.departure_date);
      const vtime = (ride && ride.departure_time) || (snap && snap.departure_time);
      const cancelled = b.status === 'cancelled';
      const departed = cancelled || !ride || ride.status === 'completed';
      const hasLink = !!ride;
      return `
        <article class="card ride ${hasLink ? 'ride--row' : ''}" ${hasLink ? `data-id="${ride.id}"` : ''}>
          <div class="ride__route">
            <div class="route">
              <div class="route__row"><span class="dot"></span>
                <div style="flex:1;min-width:0"><div class="route__label">${h(origin.label || 'Unknown pickup')}</div></div>
              </div>
              <div class="route__row"><span class="dot dot--green"></span>
                <div style="flex:1;min-width:0"><div class="route__label">${h(dest.label || 'Unknown drop')}</div></div>
              </div>
            </div>
            <div class="flex gap-8 items-center mt-8" style="flex-wrap:wrap">
              <span class="badge badge--brand">${icon('clock', 13)} ${timeLabel(vtime)}</span>
              <span class="badge badge--ghost">${dayLabel(vdate)}</span>
              <span class="badge badge--ghost">${b.seats} seat${b.seats > 1 ? 's' : ''}</span>
              <span class="badge ${cancelled ? 'badge--danger' : departed ? '' : 'badge--success'}">
                ${cancelled ? 'Cancelled · refunded' : departed ? 'Completed' : 'Upcoming'}</span>
            </div>
          </div>
          <div class="ride__meta">
            <div class="ride__driver">
              ${avatar(b.driver, 36, 'avatar--sm')}
              <div style="text-align:left"><b class="text-sm trunc" style="display:block;max-width:130px">${h((b.driver && b.driver.name) || 'Driver')}</b> ${b.driver ? rating(b.driver.rating) : ''}</div>
            </div>
            <div class="flex items-center justify-between" style="width:100%">
              <div class="ride__fare">${money(b.amount)}</div>
              ${!cancelled && !departed && b.ride
                ? `<button class="btn btn-danger btn-sm" data-cancel-b="${b.id}">Cancel</button>`
                : `<span class="badge badge--ghost">${h(b.payment && b.payment.reference || '')}</span>`}
            </div>
          </div>
        </article>`;
    }).join('');

    listEl.querySelectorAll('[data-cancel-b]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const bookingId = btn.dataset.cancelB;
        const ok = await confirmDialog({
          title: 'Cancel this booking?',
          message: 'Your seats will be released and the fare refunded.',
          confirmLabel: 'Cancel booking', danger: true,
        });
        if (!ok) return;
        try {
          await Api.cancelBooking(bookingId);
          toast('Booking cancelled & refunded.', 'success');
          load();
        } catch (err) { toast(errMsg(err), 'error'); }
      });
    });
    listEl.querySelectorAll('.ride--row').forEach((card) => {
      card.addEventListener('click', () => { location.hash = `#/ride/${card.dataset.id}`; });
    });
  }

  function renderGiven(rides) {
    if (!rides.length) {
      listEl.innerHTML = emptyState({
        ic: 'car',
        title: scope === 'upcoming' ? 'No upcoming rides given' : 'No past rides given yet',
        desc: scope === 'upcoming' ? 'Share your route and earn back your fuel money.'
          : 'Rides you hosted will appear here.',
        action: scope === 'upcoming' ? '<a class="btn btn-primary btn-sm mt-12" href="#/publish">Publish a ride</a>' : '',
      });
      return;
    }
    listEl.innerHTML = rides.map((r) => {
      const seats = Math.max(0, r.seats_available || 0);
      const departed = new Date(r.departure_at).getTime() < Date.now();
      const status = r.status === 'cancelled' ? 'cancelled' : departed ? 'completed' : 'active';
      const badge = status === 'active' ? '<span class="badge badge--success">Active</span>'
        : status === 'cancelled' ? '<span class="badge badge--danger">Cancelled</span>'
          : '<span class="badge badge--ghost">Completed</span>';
      return `
        <article class="card ride ride--row" data-id="${r.id}">
          <div class="ride__route">
            <div class="route">
              <div class="route__row"><span class="dot"></span>
                <div style="flex:1;min-width:0"><div class="route__label">${h(r.origin.label)}</div></div>
              </div>
              <div class="route__row"><span class="dot dot--green"></span>
                <div style="flex:1;min-width:0"><div class="route__label">${h(r.destination.label)}</div></div>
              </div>
            </div>
            <div class="flex gap-8 items-center mt-8" style="flex-wrap:wrap">
              <span class="badge badge--brand">${icon('clock', 13)} ${timeLabel(r.departure_time)}</span>
              <span class="badge badge--ghost">${dayLabel(r.departure_date)}</span>
              <span class="badge badge--ghost">${icon(vehicleIconName(r.vehicle.type), 13)} ${vehicleLabel(r.vehicle.type)}</span>
              <span class="badge ${seats ? 'badge--success' : 'badge--ghost'}">${seats} seat${seats === 1 ? '' : 's'} free</span>
              ${badge}
            </div>
          </div>
          <div class="ride__meta">
            ${seatsDots(seats, r.seats_total)}
            <div class="flex items-center justify-between" style="width:100%">
              <div class="ride__fare"><small style="font-weight:500">earned</small> ${money(r.earnings || 0)}</div>
              ${status === 'active'
                ? `<button class="btn btn-danger btn-sm" data-cancel-r="${r.id}">Cancel</button>`
                : `<span class="badge badge--ghost">${money(r.fare_per_seat)}/seat</span>`}
            </div>
          </div>
        </article>`;
    }).join('');

    listEl.querySelectorAll('[data-cancel-r]').forEach((btn) => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const ok = await confirmDialog({
          title: 'Cancel this ride?',
          message: 'All booked seats will be refunded to riders.',
          confirmLabel: 'Yes, cancel it', danger: true,
        });
        if (!ok) return;
        try {
          await Api.cancelRide(btn.dataset.cancelR);
          toast('Ride cancelled & bookings refunded.', 'success');
          load();
        } catch (err) { toast(errMsg(err), 'error'); }
      });
    });
    listEl.querySelectorAll('.ride--row').forEach((card) => {
      card.addEventListener('click', () => { location.hash = `#/ride/${card.dataset.id}`; });
    });
  }
}