/* Admin-only manual DL / RC / insurance review queue. */
import { Api, store } from './api.js';
import { h, toast, errMsg, icon, emptyState } from './ui.js';

const labels = { dl: 'Driving licence', rc: 'Registration certificate (RC)', insurance: 'Insurance' };
const openedUrls = new Set();

export async function renderAdminKyc(root) {
  if (!store.user || !['admin', 'support'].includes(store.user.role)) {
    root.innerHTML = emptyState({ ic: 'shield', title: 'Reviewer access required',
      desc: 'Only authorised RideMate review staff can access identity documents.' });
    return;
  }
  root.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Trust & safety</div><h1>Driver document review</h1>
    <p>Review submitted driving licences, RCs and insurance before a driver can publish rides.</p></div>
    <button class="btn btn-outline btn-sm" data-reload>${icon('refresh-cw', 15)} Refresh queue</button></div>
    <div id="reviewQueue" class="flex flex-col gap-12"><div class="skeleton" style="height:140px"></div></div>`;
  root.querySelector('[data-reload]').addEventListener('click', load);

  async function load() {
    const queue = root.querySelector('#reviewQueue');
    queue.innerHTML = '<div class="skeleton" style="height:140px"></div>';
    try {
      const result = await Api.pendingVehicleReviews();
      if (!result.data.length) {
        queue.innerHTML = emptyState({ ic: 'check-circle', title: 'No documents waiting',
          desc: 'New driver packets appear here after all required details and documents are submitted.' });
        return;
      }
      queue.innerHTML = result.data.map((item) => `
        <article class="card card--pad" data-packet="${h(item.id)}">
          <div class="flex items-start justify-between gap-12">
            <div><h3>${h(item.owner.name || 'Driver')}</h3>
              <p class="text-muted text-sm">${h(item.owner.email)} ${item.owner.phone ? ' · ' + h(item.owner.phone) : ''}</p>
              <p class="text-sm mt-8"><b>${h(item.vehicle_number)}</b> · ${h(item.vehicle_model || item.vehicle_type || 'Vehicle')}</p>
            </div>
            <span class="badge badge--ghost">Pending review</span>
          </div>
          <div class="grid-2 mt-12">
            <div class="text-sm"><span class="text-faint">DL number</span><br><b>${h(item.dl_number || 'Not provided')}</b></div>
            <div class="text-sm"><span class="text-faint">RC number</span><br><b>${h(item.rc_number || 'Not provided')}</b></div>
            <div class="text-sm"><span class="text-faint">Insurance policy</span><br><b>${h(item.insurance_number || 'Not provided')}</b></div>
            <div class="text-sm"><span class="text-faint">Submitted</span><br><b>${h(item.submitted_at || '—')}</b></div>
          </div>
          <div class="flex gap-8 mt-12" style="flex-wrap:wrap">
            ${Object.entries(item.documents).map(([kind, url]) => url
              ? `<button class="btn btn-outline btn-sm" data-doc="${kind}" data-url="${h(url)}">${icon('file-text', 14)} View ${labels[kind]}</button>`
              : `<span class="badge badge--ghost">${labels[kind]} missing</span>`).join('')}
          </div>
          <div class="flex gap-8 mt-16">
            <button class="btn btn-primary btn-sm" data-decision="approve" data-id="${h(item.id)}">Approve packet</button>
            <button class="btn btn-outline btn-sm" data-decision="reject" data-id="${h(item.id)}">Reject</button>
          </div>
        </article>`).join('');
      queue.querySelectorAll('[data-doc]').forEach((button) => {
        button.addEventListener('click', async () => {
          button.disabled = true;
          try {
            const blob = await Api.downloadVehicleDoc(button.dataset.url);
            const objectUrl = URL.createObjectURL(blob);
            openedUrls.add(objectUrl);
            const tab = window.open(objectUrl, '_blank');
            if (tab) tab.opener = null;
            else toast('Your browser blocked the document tab. Allow pop-ups and try again.', 'error');
          } catch (err) {
            toast(errMsg(err), 'error');
          } finally {
            button.disabled = false;
          }
        });
      });
      queue.querySelectorAll('[data-decision]').forEach((button) => {
        button.addEventListener('click', async () => {
          const decision = button.dataset.decision;
          const reason = decision === 'reject' ? (window.prompt('Reason for rejection (required):') || '').trim() : '';
          if (decision === 'reject' && !reason) {
            toast('A rejection reason is required.', 'error');
            return;
          }
          button.disabled = true;
          try {
            await Api.reviewVehicle(button.dataset.id, { decision, ...(reason ? { reason } : {}) });
            toast(decision === 'approve' ? 'Vehicle documents approved.' : 'Packet rejected with a reason.', 'success');
            await load();
          } catch (err) {
            toast(errMsg(err), 'error');
            button.disabled = false;
          }
        });
      });
    } catch (err) {
      queue.innerHTML = emptyState({ ic: 'alert', title: 'Could not load review queue', desc: errMsg(err) });
    }
  }
  await load();
}
