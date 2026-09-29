/* Ride detail page: map, route, driver, booking / driver tools. */

import { Api, store } from './api.js';
import {
  icon, h, money, toast, errMsg, emptyState, avatar, rating,
  timeLabel, dayLabel, fmtKm, vehicleLabel, vehicleIconName, seatsDots,
  confirmDialog,
} from './ui.js';
import { createMap, destroyMap, addMarker, drawRoute } from './maps.js';
import { openBooking } from './discover.js';

export async function renderRide(root, { id }) {
  if (root._rmMap) destroyMap(root._rmMap);
  root._rmMap = null;
  root.innerHTML = '<div class="skeleton" style="height:200px"></div>';
  let ride;
  try {
    const res = await Api.ride(id);
    ride = res.ride;
  } catch (err) {
    root.innerHTML = emptyState({ ic: 'alert', title: 'Ride not found', desc: errMsg(err) });
    return;
  }

  const me = store.user;
  const isOwner = me && ride.owner_id === String(me._id || me.id);
  const seats = Math.max(0, ride.seats_available || 0);
  const canBook = !isOwner && ride.status === 'active' && seats > 0;
  const hasDeparted = new Date(ride.departure_at).getTime() < Date.now();

  root.innerHTML = `
    <a class="back-link" href="#/discover">${icon('chevron-left', 16)} Back to rides</a>
    <div class="detail-grid">
      <div class="detail-stack">
        <div class="card card--pad">
          <div class="flex items-center justify-between gap-16" style="flex-wrap:wrap">
            <div>
              <div class="eyebrow">${dayLabel(ride.departure_date)} · ${timeLabel(ride.departure_time)}</div>
              <h1 style="font-size:21px;margin-top:6px" >${h(ride.origin.label)} <span class="text-faint" style="font-weight:400">to</span> ${h(ride.destination.label)}</h1>
            </div>
            <div class="ride__fare" style="font-size:26px">${money(ride.fare_per_seat)} <small>/ seat</small></div>
          </div>
          <div class="map-box mt-16"><div class="map-canvas" id="rideMap"></div></div>
        </div>

        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Ride details</div>
          <div class="stat-grid" style="grid-template-columns:repeat(auto-fit,minmax(130px,1fr))">
            <div class="card card--pad" style="box-shadow:none"><span class="text-faint text-xs">Departure</span><b class="fw-800" style="font-size:16px;display:block;margin-top:4px">${timeLabel(ride.departure_time)}</b></div>
            <div class="card card--pad" style="box-shadow:none"><span class="text-faint text-xs">Distance</span><b class="fw-800" style="font-size:16px;display:block;margin-top:4px">${ride.distance_km ? fmtKm(ride.distance_km) : '—'}</b></div>
            <div class="card card--pad" style="box-shadow:none"><span class="text-faint text-xs">Seats left</span><b class="fw-800" style="font-size:16px;display:block;margin-top:4px">${seats} ${seatsDots(seats, ride.seats_total)}</b></div>
            <div class="card card--pad" style="box-shadow:none"><span class="text-faint text-xs">Status</span><b class="fw-800" style="font-size:16px;display:block;margin-top:4px;color:${ride.status === 'active' ? 'var(--green)' : ride.status === 'cancelled' ? 'var(--red)' : 'var(--muted)'}">${h(ride.status)}</b></div>
          </div>
          ${ride.notes ? `<div class="mt-16"><div class="section-title">Notes from the driver</div><p class="text-muted">${h(ride.notes)}</p></div>` : ''}
        </div>
      </div>

      <div class="detail-stack">
        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Driver</div>
          <div class="flex gap-12 items-center">
            ${avatar(ride.owner, 52, 'avatar--lg')}
            <div style="min-width:0"><b style="font-size:16px">${h(ride.owner?.name || 'Driver')}</b>
              <div class="text-faint text-xs">${(ride.owner?.total_rides || 0)} trips hosted</div></div>
            <div style="margin-left:auto">${rating(ride.owner?.rating || 0)}</div>
          </div>
          <div class="mt-16" style="border-top:1px dashed var(--line);padding-top:12px">
            <div class="flex items-center gap-12">
              ${icon(vehicleIconName(ride.vehicle.type), 20)}
              <div class="flex flex-col" style="flex:1">
                <b class="text-sm">${h(ride.vehicle.model) || vehicleLabel(ride.vehicle.type)}</b>
                <span class="text-faint text-xs">${vehicleLabel(ride.vehicle.type)} · ${h(ride.vehicle.number || '')} ${ride.vehicle.color ? '· ' + h(ride.vehicle.color) : ''}</span>
              </div>
            </div>
          </div>
        </div>

        <div class="card card--pad">
          ${isOwner ? driverPanel(ride, root) : riderPanel(ride, canBook, hasDeparted, root)}
        </div>
      </div>
    </div>`;

  const canvas = root.querySelector('#rideMap');
  const map = createMap(canvas);
  if (map) {
    addMarker(map, ride.origin, { color: '#635bff' });
    addMarker(map, ride.destination, { color: '#0ea96b' });
    drawRoute(map, ride.origin, ride.destination);
    root._rmMap = map;
  }

  const bookBtn = root.querySelector('[data-ride-book]');
  if (bookBtn) {
    bookBtn.addEventListener('click', async () => {
      const refreshed = await Api.ride(id).catch(() => null);
      if (refreshed && refreshed.ride) openBooking(refreshed.ride);
    });
  }

  const cancelBtn = root.querySelector('[data-cancel-ride]');
  if (cancelBtn) {
    cancelBtn.addEventListener('click', async () => {
      const ok = await confirmDialog({
        title: 'Cancel this ride?',
        message: 'All booked seats will be refunded and passengers will be notified.',
        confirmLabel: 'Yes, cancel it',
        danger: true,
      });
      if (!ok) return;
      try {
        await Api.cancelRide(id);
        toast('Ride cancelled. Seats refunded.', 'success');
        setTimeout(() => { location.hash = '#/trips?tab=given'; }, 700);
      } catch (err) {
        toast(errMsg(err), 'error');
      }
    });
  }
}

function riderPanel(ride, canBook, hasDeparted) {
  if (ride.status === 'cancelled') {
    return `<div class="badge badge--danger mb-8">Cancelled</div><p class="text-muted text-sm">This ride was cancelled by the driver.</p>`;
  }
  if (hasDeparted) {
    return `<div class="empty" style="padding:16px">${icon('clock', 28)}<b>Ride already departed</b></div>`;
  }
  if (!canBook) {
    return `<div class="empty" style="padding:16px">${icon('users', 28)}<b>No seats left</b><p>Hop onto another ride — new ones are added every hour.</p>
      <a class="btn btn-soft btn-sm" href="#/discover">Discover more</a></div>`;
  }
  return `
    <div class="flex items-center justify-between mb-16">
      <div><span class="text-faint text-xs">Seats available</span><b style="font-size:20px;display:block">${ride.seats_available} left</b></div>
      <div class="ride__fare" style="font-size:22px">${money(ride.fare_per_seat)}<small>/ seat</small></div>
    </div>
    <button class="btn btn-primary btn-lg btn-block" data-ride-book>${icon('check', 17)} Book ${money(ride.fare_per_seat)} per seat</button>
    <p class="form-hint mt-8" style="text-align:center">Free cancellation before departure · secure payment</p>`;
}

function driverPanel(ride, root) {
  const passengers = ride.passengers || [];
  root._passengers = passengers;
  return `
    <div class="section-title" style="margin-top:0">Your passengers (${passengers.length})</div>
    ${passengers.length
      ? passengers.map((p) => `
        <div class="list-row">
          ${avatar(p.rider, 38)}
          <div style="flex:1;min-width:0"><b class="text-sm">${h((p.rider && p.rider.name) || 'Rider')}</b>
            <span class="text-faint text-xs">${p.seats} seat${p.seats > 1 ? 's' : ''} · ${money(p.amount)}</span></div>
        </div>`).join('')
      : `<p class="text-muted text-sm">Nobody has booked yet. Share your ride to fill those seats!</p>`}
    <div style="border-top:1px dashed var(--line);margin-top:12px;padding-top:14px">
      <div class="flex items-center justify-between mb-8">
        <span class="text-muted text-sm">Fare you collected</span>
        <b>${money(ride.earnings || 0)}</b>
      </div>
      <button class="btn btn-danger btn-block" data-cancel-ride>${icon('trash', 16)} Cancel this ride</button>
      <p class="form-hint mt-8" style="text-align:center">Cancelling refunds all booked seats.</p>
    </div>`;
}