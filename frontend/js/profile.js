/* Profile: avatar, stats, editable details, password change. */

import { Api, store } from './api.js';
import {
  icon, h, money, toast, errMsg, skeleton, rating, avatar,
  bindForm, fieldError, confirmDialog,
} from './ui.js';

export async function renderProfile(root) {
  root.innerHTML = `<div class="page-head"><h1>Profile</h1></div>${skeleton(2)}`;

  const me = store.user;
  const [statsRes] = await Promise.all([
    Api.stats().catch(() => null),
  ]);
  const stats = (statsRes && statsRes.stats) || {};

  root.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Your account</div><h1>Profile</h1>
    <p>Manage how riders and drivers see you.</p></div></div>

    <div class="card profile-head">
      <div class="avatar-wrap">
        ${avatar(me, 76, 'avatar--xl')}
        <label class="cam" title="Change photo">${icon('camera', 15)}
          <input type="file" id="avatarInput" accept="image/*" hidden /></label>
      </div>
      <div class="meta">
        <h2>${h(me.name)}</h2>
        <div class="text-faint text-sm">${h(me.email)}${me.phone ? ' · ' + h(me.phone) : ''}</div>
        <div class="flex gap-8 items-center mt-8">${rating(me.rating)}<span class="badge badge--ghost">${(me.total_rides || 0)} rides hosted</span></div>
      </div>
      <div style="margin-left:auto" class="stats-head">
        <div class="flex gap-16 text-center">
          <div><b class="fw-800" style="font-family:var(--font-display);font-size:22px">${stats.rides_taken || 0}</b><div class="text-faint text-xs">Trips taken</div></div>
          <div><b class="fw-800" style="font-family:var(--font-display);font-size:22px">${stats.rides_given || 0}</b><div class="text-faint text-xs">Rides given</div></div>
        </div>
      </div>
    </div>

    <div class="stat-grid mt-16">
      ${statCard('wallet', 'Total earned', money(stats.total_earnings || 0))}
      ${statCard('leaf', 'CO₂ saved', `${(stats.co2_saved_kg || 0).toLocaleString('en-IN')} kg`)}
      ${statCard('star', 'Average rating', (me.rating || 0) ? (me.rating).toFixed(1) : 'New')}
      ${statCard('users', 'Completed trips', (stats.taken_completed || 0) + (stats.given_completed || 0))}
    </div>

    <div class="detail-grid mt-16" style="align-items:stretch">
      <div class="card card--pad">
        <div class="section-title" style="margin-top:0">Personal details</div>
        <form id="editForm">
          <div class="flex flex-col gap-12">
            <div class="field"><label for="pf-name">Full name</label>
              <input class="input" id="pf-name" name="name" value="${h(me.name)}" required /></div>
            <div class="grid-2">
              <div class="field"><label for="pf-age">Age</label>
                <input class="input" id="pf-age" name="age" type="number" min="5" max="120" value="${h(me.age || '')}" placeholder="—" /></div>
              <div class="field"><label for="pf-phone">Phone</label>
                <input class="input" id="pf-phone" name="phone" type="tel" value="${h(me.phone || '')}" placeholder="+91 …" /></div>
            </div>
            <div class="field"><label for="pf-gender">Gender</label>
              <select class="select" id="pf-gender" name="gender">
                <option value="" ${!me.gender ? 'selected' : ''}>Prefer not to say</option>
                <option value="male" ${me.gender === 'male' ? 'selected' : ''}>Male</option>
                <option value="female" ${me.gender === 'female' ? 'selected' : ''}>Female</option>
                <option value="other" ${me.gender === 'other' ? 'selected' : ''}>Other</option>
              </select></div>
            <div class="field"><label for="pf-bio">Bio</label>
              <textarea class="textarea" id="pf-bio" name="bio" maxlength="300" placeholder="A short line riders see before they book…">${h(me.bio || '')}</textarea></div>
            <button type="submit" class="btn btn-primary">Save changes</button>
          </div>
        </form>
      </div>

      <div class="detail-stack">
        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Change password</div>
          <form id="pwForm">
            <div class="flex flex-col gap-12">
              <div class="field"><label>Current password</label>
                <input class="input" type="password" name="current_password" required autocomplete="current-password" /></div>
              <div class="field"><label>New password</label>
                <input class="input" type="password" name="new_password" required autocomplete="new-password" placeholder="8+ chars, letter + number" /></div>
              <div class="field"><label>Confirm password</label>
                <input class="input" type="password" name="confirm" required autocomplete="new-password" /></div>
              <button type="submit" class="btn btn-outline">Update password</button>
            </div>
          </form>
        </div>

        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Session</div>
          <button class="btn btn-danger btn-block" data-logout>${icon('logout', 16)} Log out of RideMate</button>
        </div>
      </div>
    </div>`;

  /* avatar upload */
  root.querySelector('#avatarInput').addEventListener('change', async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    try {
      const res = await Api.uploadAvatar(file);
      store.user = res.user;
      toast('Profile photo updated.', 'success');
      renderProfile(root);
    } catch (err) {
      toast(errMsg(err), 'error');
    }
  });

  /* edit profile */
  bindForm(root.querySelector('#editForm'), async (form) => {
    const data = new FormData(form);
    const payload = {
      name: data.get('name'),
      age: data.get('age') ? Number(data.get('age')) : '',
      phone: data.get('phone') || '',
      gender: data.get('gender') || '',
      bio: data.get('bio') || '',
    };
    try {
      const res = await Api.updateMe(payload);
      store.user = res.user;
      toast('Profile updated.', 'success');
      renderProfile(root);
    } catch (err) { toast(errMsg(err), 'error'); }
  });

  /* change password */
  bindForm(root.querySelector('#pwForm'), async (form) => {
    const d = new FormData(form);
    if (d.get('new_password') !== d.get('confirm')) {
      fieldError(form.querySelector('[name=confirm]'), 'Passwords do not match.');
      return;
    }
    try {
      await Api.changePassword({ current_password: d.get('current_password'), new_password: d.get('new_password') });
      form.reset();
      toast('Password updated.', 'success');
    } catch (err) { toast(errMsg(err), 'error'); }
  });

  /* logout */
  root.querySelector('[data-logout]').addEventListener('click', async () => {
    const ok = await confirmDialog({ title: 'Log out?', message: 'You will need to sign back in to see your trips.', confirmLabel: 'Log out' });
    if (!ok) return;
    await Api.logout();
    store.clear();
    location.hash = '#/login';
  });
}

function statCard(ic, label, value) {
  return `<div class="card stat-card">${icon(ic, 22)}<b>${value ?? '—'}</b><span>${label}</span></div>`;
}