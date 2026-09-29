/* Google Sign-In: exchanges a Google ID token (`credential`) for a RideMate
   session. The backend verifies the JWT against Google exactly; there is no
   email-only bypass. Demo mode lives in the password login, not here.

   The OAuth client ID is public (it identifies the app, not the user), so it is
   fetched from GET /api/auth/providers -- the single source of truth -- and
   cached. A copy in index.html is only a fallback, which means a redeploy that
   rotates the client ID can never leave the UI signing in with a stale value. */

import { Api, store } from './api.js';
import { openModal, closeOpenModal, toast, errMsg } from './ui.js';

let _clientId = null;
let _gsi = null;

async function resolveClientId() {
  if (_clientId) return _clientId;
  if (window.RIDEMATE_GOOGLE_CLIENT_ID) {
    _clientId = window.RIDEMATE_GOOGLE_CLIENT_ID;
    return _clientId;
  }
  try {
    const data = await Api.providers();
    _clientId = (data && data.providers && data.providers.google_client_id) || '';
  } catch {
    _clientId = '';
  }
  if (_clientId) window.RIDEMATE_GOOGLE_CLIENT_ID = _clientId;
  return _clientId;
}

function loadGis() {
  if (_gsi) return _gsi;
  _gsi = new Promise((resolve, reject) => {
    if (window.google && window.google.accounts && window.google.accounts.id) {
      resolve(window.google.accounts.id);
      return;
    }
    const script = document.createElement('script');
    script.src = 'https://accounts.google.com/gsi/client';
    script.async = true;
    script.defer = true;
    script.onload = () => {
      if (window.google && window.google.accounts && window.google.accounts.id) {
        resolve(window.google.accounts.id);
      } else {
        reject(new Error('Google Identity Services loaded without an API'));
      }
    };
    script.onerror = () => reject(new Error('Could not load accounts.google.com'));
    document.head.appendChild(script);
  });
  return _gsi;
}

function infoModal(title, html) {
  const modal = openModal({ title, body: `<p class="text-muted text-sm" style="margin-bottom:20px">${html}</p>`, closable: true });
  modal.el.querySelector('[data-close]')?.addEventListener('click', closeOpenModal);
  modal.el.querySelectorAll('button').forEach((b) => b.addEventListener('click', closeOpenModal));
  return modal;
}

/* True when this deployment can actually do Google sign-in. Used by the auth
   screens to hide the button (and its OR divider) instead of offering a dead
   end. */
export async function isGoogleEnabled() {
  return Boolean(await resolveClientId());
}

export async function openGoogleSignIn(mode = 'login') {
  const verb = mode === 'signup' ? 'Create your account' : 'Log in';
  const clientId = await resolveClientId();

  if (!clientId) {
    infoModal('Google Sign-In unavailable',
      'This server has no <code>GOOGLE_CLIENT_ID</code> configured, so the API answers '
      + '<code>google_not_configured</code>. Set it in <code>backend/.env</code> and restart the API. '
      + 'You can still use a demo account &mdash; password <b>Password123</b>.');
    return;
  }

  const modal = openModal({
    title: `${verb} with Google`,
    body: `<div style="display:flex;flex-direction:column;align-items:center;gap:14px">
             <div id="gsiSlot" style="display:flex;justify-content:center;min-height:48px"></div>
             <p id="gsiStatus" class="text-muted text-sm" style="text-align:center;margin:0">Loading Google…</p>
           </div>`,
    closable: true,
  });

  const status = modal.el.querySelector('#gsiStatus');

  try {
    const id = await loadGis();
    id.initialize({
      client_id: clientId,
      callback: async ({ credential }) => {
        modal.close();
        try {
          const data = await Api.google({ credential });
          // Google sign-in both logs in and creates the account on first use.
          store.save(data.token, data.user);
          window.dispatchEvent(new CustomEvent('ridemate:authed'));
        } catch (err) {
          toast(errMsg(err), 'error');
        }
      },
    });
    id.renderButton(modal.el.querySelector('#gsiSlot'), {
      type: 'standard', shape: 'rectangular', width: 260, theme: 'outline', text: 'continue_with',
    });
    if (status) status.textContent = "You'll receive a Google ID token, verified by the server before a session is created.";
  } catch (err) {
    const blocked = window.location.origin;
    infoModal('Google Sign-In blocked',
      `Google could not initialise here (<code>${String(err && err.message || err)}</code>). `
      + `Add <code>${blocked}</code> to this OAuth client's <b>Authorized JavaScript origins</b> in the `
      + 'Google Cloud console, then reload. A demo account also works &mdash; password <b>Password123</b>.');
  }
}
