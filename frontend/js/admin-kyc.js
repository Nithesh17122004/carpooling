/* Admin-only manual review queues for vehicle DL/insurance and RC documents. */
import { Api, store } from './api.js';
import { h, toast, errMsg, icon, emptyState } from './ui.js';

const documentLabels = { dl: 'Driving licence', insurance: 'Insurance', rc: 'Registration certificate (RC)' };

export async function renderAdminKyc(root) {
  if (!store.user || !['admin', 'support'].includes(store.user.role)) {
    root.innerHTML = emptyState({ ic: 'shield', title: 'Reviewer access required',
      desc: 'Only authorised RideMate review staff can access identity documents.' });
    return;
  }
  root.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Trust & safety</div><h1>Driver document review</h1>
    <p>Review DL, insurance and RC documents before drivers can publish rides.</p></div>
    <button class="btn btn-outline btn-sm" data-reload>${icon('refresh-cw', 15)} Refresh queues</button></div>
    <section class="mt-16"><div class="section-title">Driving licence and insurance</div><div id="dlQueue" class="flex flex-col gap-12"><div class="skeleton" style="height:120px"></div></div></section>
    <section class="mt-16"><div class="section-title">Registration certificate (RC)</div><div id="rcQueue" class="flex flex-col gap-12"><div class="skeleton" style="height:120px"></div></div></section>`;
  root.querySelector('[data-reload]').addEventListener('click', load);

  function wireDocuments(scope) {
    scope.querySelectorAll('[data-doc-url]').forEach((button) => {
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const blob = await Api.downloadVehicleDoc(button.dataset.docUrl);
          const tab = window.open(URL.createObjectURL(blob), '_blank');
          if (tab) tab.opener = null;
          else toast('Your browser blocked the document tab. Allow pop-ups and try again.', 'error');
        } catch (err) { toast(errMsg(err), 'error'); }
        finally { button.disabled = false; }
      });
    });
    scope.querySelectorAll('[data-review]').forEach((button) => {
      button.addEventListener('click', async () => {
        const decision = button.dataset.decision;
        const reason = decision === 'reject' ? (window.prompt('Reason for rejection (required):') || '').trim() : '';
        if (decision === 'reject' && !reason) {
          toast('A rejection reason is required.', 'error');
          return;
        }
        button.disabled = true;
        try {
          const payload = { decision, ...(reason ? { reason } : {}) };
          if (button.dataset.review === 'rc') {
            await Api.reviewRc(button.dataset.id, payload);
          } else {
            await Api.reviewVehicle(button.dataset.id, payload);
          }
          toast(decision === 'approve' ? 'Documents approved.' : 'Documents rejected with a reason.', 'success');
          await load();
        } catch (err) {
          toast(errMsg(err), 'error');
          button.disabled = false;
        }
      });
    });
  }

  async function load() {
    const dlRoot = root.querySelector('#dlQueue');
    const rcRoot = root.querySelector('#rcQueue');
    dlRoot.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    rcRoot.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    try {
      const [dlResult, rcResult] = await Promise.all([
        Api.pendingVehicleReviews(),
        Api.rcQueue(),
      ]);
      const dlRows = dlResult.data || [];
      const rcRows = rcResult.queue || [];
      dlRoot.innerHTML = dlRows.length ? dlRows.map((item) => `
        <article class="card card--pad">
          <div class="flex items-start justify-between gap-12"><div>
            <h3>${h(item.owner.name || 'Driver')}</h3>
            <p class="text-muted text-sm">${h(item.owner.email)} ${item.owner.phone ? ' · ' + h(item.owner.phone) : ''}</p>
            <p class="text-sm mt-8"><b>${h(item.vehicle_number)}</b> · ${h(item.vehicle_model || item.vehicle_type || 'Vehicle')}</p>
          </div><span class="badge badge--ghost">Pending review</span></div>
          <div class="grid-2 mt-12">
            <div class="text-sm"><span class="text-faint">DL number</span><br><b>${h(item.dl_number || 'Not provided')}</b></div>
            <div class="text-sm"><span class="text-faint">Insurance policy</span><br><b>${h(item.insurance_number || 'Not provided')}</b></div>
            <div class="text-sm"><span class="text-faint">Submitted</span><br><b>${h(item.submitted_at || '—')}</b></div>
          </div>
          <div class="flex gap-8 mt-12" style="flex-wrap:wrap">
            ${['dl', 'insurance'].map((kind) => item.documents[kind]
              ? `<button class="btn btn-outline btn-sm" data-doc-url="${h(item.documents[kind])}">${icon('file-text', 14)} View ${documentLabels[kind]}</button>`
              : `<span class="badge badge--ghost">${documentLabels[kind]} missing</span>`).join('')}
          </div>
          <div class="flex gap-8 mt-16">
            <button class="btn btn-primary btn-sm" data-review="vehicle" data-decision="approve" data-id="${h(item.id)}">Approve DL/insurance</button>
            <button class="btn btn-outline btn-sm" data-review="vehicle" data-decision="reject" data-id="${h(item.id)}">Reject</button>
          </div>
        </article>`).join('') : emptyState({ ic: 'check-circle', title: 'No DL/insurance packets waiting', desc: 'Submitted packets appear here for manual review.' });

      rcRoot.innerHTML = rcRows.length ? rcRows.map((item) => `
        <article class="card card--pad">
          <div class="flex items-start justify-between gap-12"><div>
            <h3>${h(item.owner?.name || 'Driver')}</h3>
            <p class="text-muted text-sm">${h(item.owner?.email || '')} ${item.owner?.phone ? ' · ' + h(item.owner.phone) : ''}</p>
            <p class="text-sm mt-8"><b>${h(item.vehicle_number || 'Vehicle')}</b> · RC number ${h(item.rc_number_masked || 'not available')}</p>
            <p class="text-faint text-sm mt-8">Submitted: ${h(item.submitted_at || '—')}</p>
          </div><span class="badge badge--ghost">Pending RC review</span></div>
          <div class="flex gap-8 mt-12">
            <button class="btn btn-outline btn-sm" data-doc-url="/api/kyc/vehicles/${h(item.vehicle_id)}/rc/document">${icon('file-text', 14)} View RC</button>
            <button class="btn btn-primary btn-sm" data-review="rc" data-decision="approve" data-id="${h(item.vehicle_id)}">Approve RC</button>
            <button class="btn btn-outline btn-sm" data-review="rc" data-decision="reject" data-id="${h(item.vehicle_id)}">Reject RC</button>
          </div>
        </article>`).join('') : emptyState({ ic: 'check-circle', title: 'No RCs waiting', desc: 'Registration certificates have a separate encrypted review queue.' });
      wireDocuments(dlRoot);
      wireDocuments(rcRoot);
    } catch (err) {
      const message = emptyState({ ic: 'alert', title: 'Could not load review queues', desc: errMsg(err) });
      dlRoot.innerHTML = message;
      rcRoot.innerHTML = message;
    }
  }
  await load();
}
