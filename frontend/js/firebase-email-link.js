/* Firebase email-link sign-in. Firebase verifies the email link; the backend
   independently verifies the resulting ID token before creating a RideMate session. */
import { Api, store } from './api.js';
import { toast, errMsg } from './ui.js';

const SDK = 'https://www.gstatic.com/firebasejs/10.14.1/';
let _sdkPromise;
let _authPromise;

async function sdk() {
  if (!_sdkPromise) {
    _sdkPromise = Promise.all([
      import(SDK + 'firebase-app.js'),
      import(SDK + 'firebase-auth.js'),
    ]).then(([app, auth]) => ({ ...app, ...auth }));
  }
  return _sdkPromise;
}

async function firebaseAuth() {
  if (!_authPromise) {
    _authPromise = (async () => {
      const data = await Api.providers();
      const config = data?.providers?.firebase_config;
      if (!data?.providers?.firebase || !config) {
        throw new Error('Firebase email-link sign-in is not configured on this deployment yet.');
      }
      const m = await sdk();
      const app = m.getApps().find((item) => item.name === 'ridemate-auth')
        || m.initializeApp(config, 'ridemate-auth');
      return { auth: m.getAuth(app), ...m };
    })();
  }
  return _authPromise;
}

export async function isFirebaseEnabled() {
  try {
    const data = await Api.providers();
    return Boolean(data?.providers?.firebase && data?.providers?.firebase_config);
  } catch {
    return false;
  }
}

export async function isFirebaseEnforced() {
  try {
    const data = await Api.providers();
    return Boolean(data?.providers?.firebase && data?.providers?.firebase_enforced);
  } catch {
    return false;
  }
}

export async function sendFirebaseEmailLink(email, profile = {}) {
  const cleanEmail = String(email || '').trim().toLowerCase();
  if (!cleanEmail) throw new Error('Enter your email address first.');
  const { auth, sendSignInLinkToEmail } = await firebaseAuth();
  const actionCodeSettings = {
    url: window.location.origin + window.location.pathname,
    handleCodeInApp: true,
  };
  await sendSignInLinkToEmail(auth, cleanEmail, actionCodeSettings);
  sessionStorage.setItem('ridemate_firebase_email', cleanEmail);
  if (profile.name) {
    sessionStorage.setItem('ridemate_firebase_profile', JSON.stringify({
      name: String(profile.name).slice(0, 80),
      phone: String(profile.phone || '').slice(0, 30),
      age: profile.age ? Number(profile.age) : null,
      gender: String(profile.gender || '').slice(0, 20),
    }));
  } else {
    sessionStorage.removeItem('ridemate_firebase_profile');
  }
  return true;
}

let _phoneRecaptcha = null;
let _phoneConfirmation = null;
let _phoneUser = null;

export async function startFirebasePhoneVerification(phone, recaptchaElement) {
  const cleanPhone = String(phone || '').trim().replace(/[\s()-]/g, '');
  if (!/^\+[1-9]\d{7,14}$/.test(cleanPhone)) {
    throw new Error('Enter your phone in international format, for example +919876543210.');
  }
  const { auth, RecaptchaVerifier, linkWithPhoneNumber } = await firebaseAuth();
  if (!auth.currentUser) {
    throw new Error('First sign in with your linked Firebase email link, then verify your phone.');
  }
  if (_phoneRecaptcha) {
    try { _phoneRecaptcha.clear(); } catch { /* stale widget */ }
    _phoneRecaptcha = null;
  }
  _phoneRecaptcha = new RecaptchaVerifier(auth, recaptchaElement, { size: 'normal' });
  _phoneUser = auth.currentUser;
  _phoneConfirmation = await linkWithPhoneNumber(_phoneUser, cleanPhone, _phoneRecaptcha);
  return true;
}

export async function confirmFirebasePhoneVerification(code) {
  const cleanCode = String(code || '').trim();
  if (!/^\d{6}$/.test(cleanCode)) throw new Error('Enter the 6-digit SMS code.');
  if (!_phoneConfirmation || !_phoneUser) throw new Error('Request an SMS code first.');
  const { PhoneAuthProvider, linkWithCredential } = await firebaseAuth();
  const credential = PhoneAuthProvider.credential(_phoneConfirmation.verificationId, cleanCode);
  const linked = await linkWithCredential(_phoneUser, credential);
  const idToken = await linked.user.getIdToken(true);
  _phoneConfirmation = null;
  _phoneUser = null;
  if (_phoneRecaptcha) {
    try { _phoneRecaptcha.clear(); } catch { /* noop */ }
    _phoneRecaptcha = null;
  }
  return idToken;
}


/* Complete the provider-backed phone link and send the verified Firebase
   ID token to the backend. The backend verifies UID + phone claim itself. */
export async function completeFirebasePhoneLink(idToken) {
  const data = await Api.verifyPhone({ id_token: idToken });
  store.save(store.token, data.user);
  window.dispatchEvent(new CustomEvent('ridemate:authed'));
  return data.user;
}


/* Offer SMS verification immediately after Firebase email-link sign-in.
   The API remains the authority: it validates the signed Firebase token,
   linked UID and verified phone_number claim before setting phone_verified. */
async function showPhoneVerificationPrompt(phone) {
  const cleanPhone = String(phone || '').trim();
  if (!cleanPhone) return;
  const overlay = document.createElement('div');
  overlay.id = 'ridemate-phone-verify-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:10000;background:rgba(8,24,31,.62);display:grid;place-items:center;padding:18px;';
  overlay.innerHTML = `
    <section role="dialog" aria-modal="true" aria-labelledby="rmPhoneTitle" style="width:min(100%,440px);background:#fff;color:#142c32;border-radius:20px;padding:24px;box-shadow:0 24px 80px rgba(0,0,0,.24)">
      <h2 id="rmPhoneTitle" style="margin:0 0 8px;font-size:22px">Verify your mobile number</h2>
      <p style="margin:0 0 16px;color:#5c7076">We will send a one-time SMS code to <strong></strong>. Your number is marked verified only after Firebase and RideMate confirm it.</p>
      <div id="rmPhoneRecaptcha" style="margin:12px 0"></div>
      <label for="rmPhoneCode" style="display:block;font-weight:600;margin:12px 0 6px">6-digit SMS code</label>
      <input id="rmPhoneCode" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="Enter code" style="width:100%;box-sizing:border-box;padding:13px;border:1px solid #cbd8da;border-radius:10px;font:inherit" />
      <p id="rmPhoneStatus" aria-live="polite" style="min-height:20px;color:#5c7076;font-size:13px"></p>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button type="button" id="rmPhoneSend" style="flex:1;padding:12px;border:0;border-radius:10px;background:#0f8b83;color:white;font-weight:700">Send SMS code</button>
        <button type="button" id="rmPhoneConfirm" style="flex:1;padding:12px;border:0;border-radius:10px;background:#08766f;color:white;font-weight:700">Verify number</button>
        <button type="button" id="rmPhoneSkip" style="padding:12px 16px;border:1px solid #d6e0e2;border-radius:10px;background:white;color:#344b50">Later</button>
      </div>
    </section>`;
  document.body.appendChild(overlay);
  overlay.querySelector('strong').textContent = cleanPhone;
  const status = overlay.querySelector('#rmPhoneStatus');
  const send = overlay.querySelector('#rmPhoneSend');
  const confirm = overlay.querySelector('#rmPhoneConfirm');
  const close = () => { overlay.remove(); };
  overlay.querySelector('#rmPhoneSkip').addEventListener('click', close);
  send.addEventListener('click', async () => {
    send.disabled = true;
    status.textContent = 'Sending SMS…';
    try {
      await startFirebasePhoneVerification(cleanPhone, overlay.querySelector('#rmPhoneRecaptcha'));
      status.textContent = 'Code sent. Check your SMS messages.';
    } catch (err) {
      status.textContent = errMsg(err);
    } finally { send.disabled = false; }
  });
  confirm.addEventListener('click', async () => {
    confirm.disabled = true;
    status.textContent = 'Checking verification…';
    try {
      const token = await confirmFirebasePhoneVerification(overlay.querySelector('#rmPhoneCode').value);
      await completeFirebasePhoneLink(token);
      status.textContent = 'Mobile number verified successfully.';
      toast('Mobile number verified successfully.', 'success');
      window.dispatchEvent(new CustomEvent('ridemate:phone-verified'));
      setTimeout(close, 500);
    } catch (err) {
      status.textContent = errMsg(err);
      confirm.disabled = false;
    }
  });
}

export async function completeFirebaseEmailLink() {
  if (!/[?&]mode=signIn(?:&|$)/.test(window.location.search || '')) return false;
  try {
    const { auth, isSignInWithEmailLink, signInWithEmailLink } = await firebaseAuth();
    const href = window.location.href;
    if (!isSignInWithEmailLink(auth, href)) return false;
    let email = sessionStorage.getItem('ridemate_firebase_email') || '';
    if (!email) email = (window.prompt('Confirm the email address used to receive your RideMate sign-in link:') || '').trim();
    if (!email) {
      toast('Enter the same email address that received the sign-in link.', 'error');
      return false;
    }
    const credential = await signInWithEmailLink(auth, email, href);
    const idToken = await credential.user.getIdToken(true);
    let profile = {};
    try { profile = JSON.parse(sessionStorage.getItem('ridemate_firebase_profile') || '{}'); } catch { /* ignore malformed local state */ }
    const session = await Api.firebaseSession({ id_token: idToken, ...profile });
    store.save(session.token, session.user);
    sessionStorage.removeItem('ridemate_firebase_email');
    sessionStorage.removeItem('ridemate_firebase_profile');
    history.replaceState(null, document.title, window.location.pathname);
    window.location.hash = '#/discover';
    window.dispatchEvent(new CustomEvent('ridemate:authed'));
    toast('You are signed in securely.', 'success');
    if (session.user?.phone && !session.user?.phone_verified) {
      await showPhoneVerificationPrompt(session.user.phone);
    }
    return true;
  } catch (err) {
    toast(errMsg(err), 'error');
    return false;
  }
}
