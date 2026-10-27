/* Profile: avatar, stats, editable details, password change. */

import { Api, store } from './api.js';
import { isFirebaseEnabled, startFirebasePhoneVerification, confirmFirebasePhoneVerification } from './firebase-email-link.js';
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
          <div class="section-title" style="margin-top:0">Email verification</div>
          <p class="text-faint text-sm">Request a verification code by email, then enter it here to verify your account.</p>
          <button type="button" class="btn btn-outline btn-block" id="sendVerification">Send verification email</button>
          <form id="verifyEmailForm" class="mt-12">
            <div class="field"><label for="verifyEmailToken">Verification code</label>
              <input class="input" id="verifyEmailToken" name="token" autocomplete="one-time-code" required maxlength="64" placeholder="Paste the code from your email" /></div>
            <button type="submit" class="btn btn-primary btn-block">Verify email</button>
          </form>
        </div>
        <div class="card card--pad">
          <div class="section-title" style="margin-top:0">Phone verification</div>
          <p class="text-faint text-sm" id="phoneVerificationStatus">${me.phone_verified ? 'Your phone number is verified.' : 'Verify that you can receive SMS messages on this phone.'}</p>
          <div class="field">
            <label for="phoneOtpNumber">Phone number (international format)</label>
            <input class="input" id="phoneOtpNumber" type="tel" autocomplete="tel" value="${h(me.phone || '')}" placeholder="+919876543210" ${me.phone_verified ? 'disabled' : ''} />
          </div>
          <div id="phoneOtpRecaptcha" class="mt-12"></div>
          <button type="button" class="btn btn-outline btn-block mt-12" id="sendPhoneOtp" ${me.phone_verified ? 'disabled' : ''}>Send SMS code</button>
          <form id="phoneOtpForm" class="mt-12" hidden>
            <div class="field"><label for="phoneOtpCode">6-digit SMS code</label>
              <input class="input" id="phoneOtpCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /></div>
            <button type="submit" class="btn btn-primary btn-block">Verify phone</button>
          </form>
          <p class="text-faint text-xs mt-12">Requires Firebase Phone Authentication to be enabled for this project. SMS rates and regional restrictions may apply.</p>
        </div>
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

  /* Phone OTP: Firebase sends and verifies the SMS; the backend independently
     verifies the signed ID token and only then sets phone_verified=true. */
  const phoneSend = root.querySelector('#sendPhoneOtp');
  const phoneForm = root.querySelector('#phoneOtpForm');
  if (phoneSend && !me.phone_verified) {
    phoneSend.addEventListener('click', async () => {
      try {
        if (!(await isFirebaseEnabled())) {
          throw new Error('Firebase is not configured on this deployment yet.');
        }
        const phone = root.querySelector('#phoneOtpNumber').value;
        await startFirebasePhoneVerification(phone, root.querySelector('#phoneOtpRecaptcha'));
        phoneForm.hidden = false;
        phoneSend.disabled = true;
        phoneSend.textContent = 'SMS code sent';
        toast('Enter the SMS code sent to your phone.', 'success');
      } catch (err) { toast(errMsg(err), 'error'); }
    });
    bindForm(phoneForm, async (form) => {
      try {
        const idToken = await confirmFirebasePhoneVerification(root.querySelector('#phoneOtpCode').value);
        const res = await Api.verifyPhone({ id_token: idToken });
        store.user = res.user;
        toast('Phone number verified successfully.', 'success');
        renderProfile(root);
      } catch (err) {
        // A wrong/expired code should not lock the user out of retrying.
        phoneSend.disabled = false;
        phoneSend.textContent = 'Resend SMS code';
        toast(errMsg(err), 'error');
      }
    });
  }

  /* Email verification */
  root.querySelector('#sendVerification').addEventListener('click', async () => {
    try {
      await Api.sendVerificationEmail();
      toast('Verification email requested. Check your inbox and spam folder.', 'success');
    } catch (err) { toast(errMsg(err), 'error'); }
  });
  bindForm(root.querySelector('#verifyEmailForm'), async (form) => {
    const token = new FormData(form).get('token');
    try {
      const res = await Api.confirmEmail({ token });
      store.user = res.user;
      toast('Email verified successfully.', 'success');
      renderProfile(root);
    } catch (err) { toast(errMsg(err), 'error'); }
  });

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