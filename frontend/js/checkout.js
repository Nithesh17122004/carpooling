/* Razorpay Checkout: opens the hosted payment sheet and hands the result to
   the server for verification.

   Security model:
     - The KEY SECRET is never in the browser. The KEY ID is a publishable
       identifier and arrives from the server with the order (Api.createBooking
       -> `checkout`); it is never hardcoded in index.html.
     - The amount sent to the gateway is the server's `amount`, not anything
       typed or stored in the DOM. A tampered local total cannot change what is
       charged.
     - A successful gateway callback is NOT treated as proof of payment. It only
       triggers POST /api/bookings/{id}/verify, where the server re-checks the
       signature and the captured amount. If the user closes the sheet, the
       webhook still settles the order.

   The `demo` provider is a development-only path kept so the whole booking
   flow can be exercised without a gateway. The server hard-rejects it in
   production, so this branch is unreachable there. */

import { Api } from './api.js';

const SCRIPT_SRC = 'https://checkout.razorpay.com/v1/checkout.js';
let _loader = null;

function loadCheckout() {
  if (window.Razorpay) return Promise.resolve(window.Razorpay);
  if (_loader) return _loader;
  _loader = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = SCRIPT_SRC;
    script.async = true;
    script.onload = () => (window.Razorpay
      ? resolve(window.Razorpay)
      : reject(new Error('Razorpay Checkout loaded without an API')));
    script.onerror = () => {
      _loader = null;
      reject(new Error('Could not reach checkout.razorpay.com -- check your connection.'));
    };
    document.head.appendChild(script);
  });
  return _loader;
}

function rupees(amount) {
  return Math.round(Number(amount) * 100);
}

/**
 * Run a booking payment end to end.
 *
 * @param {object} opts
 * @param {object} opts.checkout  server-supplied checkout config (provider,
 *                                key_id, order_id, amount, breakdown, prefill)
 * @param {string} opts.bookingId
 * @param {object} opts.ride      for the Checkout title/description
 * @param {function} [opts.onPending] called after a closed sheet with no payment
 * @returns {Promise<{settled: boolean, booking?: object}>}
 */
export async function payForBooking({ checkout, bookingId, ride, onPending }) {
  if (!checkout) throw new Error('The server did not return a payment order.');
  if (checkout.provider === 'demo') return settleDemo(bookingId);

  if (!checkout.key_id) {
    throw new Error('Payments are not configured on the server. Please try again later.');
  }

  const Razorpay = await loadCheckout();

  return new Promise((resolve, reject) => {
    const instance = new Razorpay({
      key: checkout.key_id,
      amount: rupees(checkout.amount),          // server-computed gross
      currency: checkout.currency || 'INR',
      order_id: checkout.order_id,
      name: 'RideMate',
      description: ride
        ? `${ride.origin?.label || 'Ride'} to ${ride.destination?.label || 'destination'}`
        : 'Ride booking',
      prefill: checkout.prefill || {},
      notes: { booking_id: bookingId },
      // Never let the browser auto-charge a saved instrument without the user
      // seeing the sheet and confirming the amount.
      modal: {
        ondismiss: () => {
          if (onPending) onPending();
          resolve({ settled: false });
        },
      },
      theme: { color: '#4F46E5' },
    });

    instance.on('payment.failed', (resp) => {
      const desc = (resp?.error && (resp.error.description || resp.error.code)) || 'The payment was not completed.';
      reject(new Error(desc));
    });

    instance.on('payment.success', async (resp) => {
      try {
        // Hand the gateway's signed result to the server. It re-verifies the
        // signature and the captured amount before confirming anything.
        const out = await Api.verifyBooking(bookingId, {
          razorpay_order_id: resp.razorpay_order_id,
          razorpay_payment_id: resp.razorpay_payment_id,
          razorpay_signature: resp.razorpay_signature,
        });
        resolve({ settled: true, booking: out && out.booking });
      } catch (err) {
        reject(err);
      }
    });

    instance.open();
  });
}

async function settleDemo(bookingId) {
  const out = await Api.verifyBooking(bookingId, {});
  return { settled: true, booking: out && out.booking };
}

/** Human-readable split for the checkout summary. Money shown is server-sent. */
export function splitLines(checkout) {
  const b = checkout && checkout.breakdown;
  if (!b) return [];
  return [
    { label: 'Fare', value: b.gross },
    { label: `Platform fee (${b.commission_rate_percent}%)`, value: b.platform_fee, muted: true },
    { label: 'Driver receives', value: b.driver_payout, muted: true },
  ];
}
