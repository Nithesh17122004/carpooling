/* Login & signup screens (rendered outside the app shell). */

import { Api, store } from './api.js';
import { icon, bindForm, toast, errMsg, fieldError } from './ui.js';

function brand(light = true) {
  return `<a class="brand" href="#/login" style="${light ? 'color:#fff' : ''}">
    <span class="brand__mark"><svg viewBox="0 0 24 24" width="23" height="23" fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="M5.2 11.2 6.7 6.8A2 2 0 0 1 8.6 5.4h6.8a2 2 0 0 1 1.9 1.4l1.5 4.4"/><path d="M3.6 11.4h16.8v4.4h-1.9a2 2 0 0 1-4 0H9.5a2 2 0 0 1-4 0H3.6z"/><circle cx="7.4" cy="15.9" r=".9"/><circle cx="16.6" cy="15.9" r=".9"/></svg></span>
    <span class="brand__name">ride<i style="color:#c7bfff">mate</i></span>
  </a>`;
}

function authPanels(path) {
  const isLogin = path === '/login';
  const img = isLogin ? 'assets/images/login.jpg' : 'assets/images/signup.jpg';
  const quote = isLogin
    ? '<h2>Find your seat,<br>cut the cost of the <em>commute</em>.</h2><p>Match with verified drivers going your way. Split fares, skip traffic, and make every ride count.</p>'
    : '<h2>Give a lift.<br>Make the road <em>yours</em>.</h2><p>Publish your daily route in seconds, earn back your fuel money, and meet friendly people along the way.</p>';
  return `
  <div class="auth">
    <div class="auth__panel" style="background-image:linear-gradient(135deg,rgba(24,18,80,.82),rgba(74,47,160,.78),rgba(42,96,255,.72)),url('${img}');background-size:cover;background-position:center">
      ${brand(true)}
      <div class="auth__quote">${quote}</div>
      <div class="auth__rider-box">
        <span class="avatar avatar--sm" style="background:rgba(255,255,255,.25)">${icon('users', 18)}</span>
        <p>Join <b>24,000+</b> riders sharing over <b>60,000</b> trips across Bangalore &amp; beyond.</p>
      </div>
    </div>
    <div class="auth__form">
      <div class="auth__card">
        ${isLogin ? loginCard() : signupCard()}
      </div>
    </div>
  </div>`;
}

function googleButton(container, mode) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn btn-outline btn-block';
  btn.style.marginTop = '4px';
  btn.innerHTML = `
    <svg width="17" height="17" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.1H42V20H24v8h11.3C33.7 32.7 29.3 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3l5.7-5.7C34.5 6.1 29.6 4 24 4 13 4 4 13 4 24s9 20 20 20 20-9 20-20c0-1.3-.1-2.6-.4-3.9z"/><path fill="#FF3D00" d="m6.3 14.7 6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.9 1.2 8 3l5.7-5.7C34.5 6.1 29.6 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.3 0-9.7-3.3-11.3-8l-6.5 5C9.8 39.7 16.3 44 24 44z"/><path fill="#1976D2" d="M43.6 20.1H42V20H24v8h11.3c-.8 2.3-2.3 4.3-4.1 5.7l6.2 5.2C36.9 39.5 44 34.5 44 24c0-1.3-.1-2.6-.4-3.9z"/></svg>
    Continue with Google`;
  btn.addEventListener('click', () => {
    import('./google.js').then((m) => m.openGoogleSignIn(mode));
  });
  container.appendChild(btn);
  return btn;
}

/* Show the Google button on both screens only when the API says Google
   sign-in is configured; otherwise drop the button AND its OR divider so the
   form never presents a dead end. */
function wireGoogle(root, isLogin) {
  const slot = root.querySelector('#googleSlot');
  if (!slot) return;
  const btn = googleButton(slot, isLogin ? 'login' : 'signup');
  const divider = slot.previousElementSibling;
  import('./google.js')
    .then((m) => m.isGoogleEnabled())
    .then((ok) => {
      if (ok) return;
      btn.remove();
      if (divider && divider.classList.contains('auth__or')) divider.remove();
    })
    .catch(() => { /* keep the button: clicking it explains the problem */ });
}

function loginCard() {
  const html = `
    <h1>Welcome back</h1>
    <p>Log in to find your next ride.</p>
    <form id="loginForm" novalidate>
      <div class="flex flex-col gap-12">
        <div class="field">
          <label for="email">Email</label>
          <div class="input-wrap">${icon('mail', 17)}
            <input class="input" id="email" name="email" type="email" inputmode="email" autocomplete="email" placeholder="you@example.com" required />
          </div>
        </div>
        <div class="field">
          <label for="password">Password</label>
          <div class="input-wrap">${icon('eye-off', 17)}
            <input class="input" id="password" name="password" type="password" autocomplete="current-password" placeholder="••••••••" required />
            <button type="button" class="btn-icon btn-icon--ghost input-suffix" data-pwtoggle title="Show password">${icon('eye', 16)}</button>
          </div>
        </div>
        <button type="submit" class="btn btn-primary btn-lg btn-block">Log in</button>
      </div>
    </form>
    <div class="auth__or">OR</div>
    <div id="googleSlot"></div>
    <div class="demo-note">${icon('info', 14)}&nbsp; Demo accounts open instantly — password is <b>Password123</b>.</div>
    <p class="auth__foot">New to RideMate? <a href="#/signup" style="color:var(--brand);font-weight:700">Create an account</a></p>`;

  return html;
}

function signupCard() {
  const html = `
    <h1>Create your account</h1>
    <p>Start riding or sharing in minutes.</p>
    <form id="signupForm" novalidate>
      <div class="flex flex-col gap-12">
        <div class="field">
          <label for="name">Full name</label>
          <div class="input-wrap">${icon('user', 17)}
            <input class="input" id="name" name="name" type="text" autocomplete="name" placeholder="Sandeep Kumar" required />
          </div>
        </div>
        <div class="field">
          <label for="uemail">Email</label>
          <div class="input-wrap">${icon('mail', 17)}
            <input class="input" id="uemail" name="email" type="email" inputmode="email" autocomplete="email" placeholder="you@example.com" required />
          </div>
        </div>
        <div class="grid-2">
          <div class="field">
            <label for="phone">Phone</label>
            <input class="input" id="phone" name="phone" type="tel" inputmode="tel" placeholder="+91 98xxxxxx" />
          </div>
          <div class="field">
            <label for="age">Age</label>
            <input class="input" id="age" name="age" type="number" inputmode="numeric" placeholder="25" min="5" max="120" />
          </div>
        </div>
        <div class="field">
          <label for="gender">Gender</label>
          <select class="select" id="gender" name="gender">
            <option value="">Prefer not to say</option>
            <option value="male">Male</option>
            <option value="female">Female</option>
            <option value="other">Other</option>
          </select>
        </div>
        <div class="field">
          <label for="npassword">Password</label>
          <div class="input-wrap">${icon('shield', 17)}
            <input class="input" id="npassword" name="password" type="password" autocomplete="new-password" placeholder="8+ chars with a letter &amp; number" required />
            <button type="button" class="btn-icon btn-icon--ghost input-suffix" data-pwtoggle title="Show password">${icon('eye', 16)}</button>
          </div>
          <span class="form-hint">At least 8 characters, one letter and one number.</span>
        </div>
        <button type="submit" class="btn btn-primary btn-lg btn-block">Create account</button>
      </div>
    </form>
    <div class="auth__or">OR</div>
    <div id="googleSlot"></div>
    <p class="auth__foot">Already a member? <a href="#/login" style="color:var(--brand);font-weight:700">Log in</a></p>`;

  return html;
}

function wirePwToggles(root) {
  root.querySelectorAll('[data-pwtoggle]').forEach((btn) => {
    const input = btn.parentNode.querySelector('input');
    btn.addEventListener('click', () => {
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.innerHTML = icon(show ? 'eye-off' : 'eye', 16);
    });
  });
}

function onAuthed(data) {
  store.save(data.token, data.user);
  window.dispatchEvent(new CustomEvent('ridemate:authed'));
}

export function renderAuth(root, path) {
  root.innerHTML = authPanels(path);
  const isLogin = path === '/login';
  wireGoogle(root, isLogin);

  const handle = async (form) => {
    const values = new FormData(form);
    try {
      if (isLogin) {
        if (!values.get('email') || !values.get('password')) {
          toast('Please enter your email and password.', 'error'); return;
        }
        const data = await Api.login({ email: values.get('email'), password: values.get('password') });
        onAuthed(data);
      } else {
        const email = values.get('email');
        const pw = values.get('password');
        if (!values.get('name')) { toast('Please enter your name.', 'error'); return; }
        if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email || '')) { fieldError(form.querySelector('[name=email]'), 'Enter a valid email.'); return; }
        if (!/^(?=.*[A-Za-z])(?=.*\d).{8,}$/.test(pw || '')) { fieldError(form.querySelector('[name=password]'), 'Minimum 8 chars with letter & number.'); return; }
        const data = await Api.register({
          name: values.get('name'), email, password: pw,
          phone: values.get('phone') || undefined, age: Number(values.get('age')) || undefined,
          gender: values.get('gender') || undefined,
        });
        onAuthed(data);
      }
    } catch (err) {
      toast(errMsg(err), 'error');
    }
  };

  const form = root.querySelector(isLogin ? '#loginForm' : '#signupForm');
  bindForm(form, handle);
  wirePwToggles(root);
}