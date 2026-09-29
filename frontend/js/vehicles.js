/* Manage vehicles: grid, add/edit modal, documents, delete. */

import { Api } from './api.js';
import {
  icon, h, toast, errMsg, skeleton, emptyState, vehicleLabel, vehicleIconName,
  openModal, confirmDialog, assetUrl,
} from './ui.js';

const MAX_SEATS = { '2-wheeler': 1, '4-wheeler': 7, auto: 8, bus: 24 };
const DEFAULT_SEATS = { '2-wheeler': 1, '4-wheeler': 4, auto: 2, bus: 12 };

export async function renderVehicles(root) {
  root.innerHTML = `
    <div class="page-head"><div><div class="eyebrow">Ride ready</div><h1>My vehicles</h1>
    <p>Add once, reuse for every ride you publish.</p></div>
    <button class="btn btn-primary btn-lg" data-add>${icon('plus', 17)} Add vehicle</button></div>
    <div class="vehicles-grid" id="vhGrid">${skeleton(2)}</div>`;

  const grid = root.querySelector('#vhGrid');
  root.querySelector('[data-add]').addEventListener('click', () => vehicleFormModal(null, refresh));

  async function refresh() {
    grid.innerHTML = skeleton(2);
    try {
      const res = await Api.vehicles();
      if (!res.data.length) {
        grid.innerHTML = emptyState({
          ic: 'car',
          title: 'No vehicles yet',
          desc: 'Add your car, bike or auto to start publishing rides.',
          action: '<button class="btn btn-primary btn-sm mt-12" data-add2>Add your first vehicle</button>',
        });
        grid.querySelector('[data-add2]')?.addEventListener('click', () => vehicleFormModal(null, refresh));
        return;
      }
      grid.innerHTML = res.data.map(card).join('');
      grid.querySelectorAll('[data-edit]').forEach((el) => {
        el.addEventListener('click', () => {
          const v = res.data.find((x) => x.id === el.dataset.edit);
          vehicleFormModal(v, refresh);
        });
      });
      grid.querySelectorAll('[data-del]').forEach((el) => {
        el.addEventListener('click', () => deleteVehicle(el.dataset.del, refresh));
      });
      wireDocs(grid, res.data);
    } catch (err) {
      grid.innerHTML = emptyState({ ic: 'alert', title: 'Could not load vehicles', desc: errMsg(err) });
    }
  }

  refresh();
}

function card(v) {
  return `
  <article class="card card--pad card--hover vehicle-card" data-id="${v.id}">
    <div class="flex items-center justify-between mb-12">
      <span class="vehicle-ico">${icon(vehicleIconName(v.vehicle_type), 22)}</span>
      <div class="flex gap-4">
        <button class="btn-icon btn-icon--ghost" data-edit="${v.id}" title="Edit" aria-label="Edit">${icon('edit', 16)}</button>
        <button class="btn-icon btn-icon--ghost" data-del="${v.id}" title="Delete" aria-label="Delete" style="color:var(--red)">${icon('trash', 16)}</button>
      </div>
    </div>
    <b class="text-sm" style="font-size:15px">${h(v.vehicle_model || 'Unnamed vehicle')}</b>
    <div class="text-faint text-sm" style="margin-top:2px">${h(v.vehicle_number)}</div>
    <div class="flex gap-8 items-center mt-12" style="flex-wrap:wrap">
      <span class="badge badge--brand">${vehicleLabel(v.vehicle_type)}</span>
      <span class="badge badge--ghost">${icon('users', 13)} ${v.seat_count} seats</span>
      ${v.color ? `<span class="badge badge--ghost">${h(v.color)}</span>` : ''}
    </div>
    <div class="mt-16" style="border-top:1px dashed var(--line);padding-top:12px">
      <div class="flex items-center justify-between" style="font-size:12.5px">
        <span class="text-faint">Driving licence</span>
        ${docSlot('dl', v, 'licence')}
      </div>
      <div class="flex items-center justify-between" style="font-size:12.5px;margin-top:8px">
        <span class="text-faint">Insurance</span>
        ${docSlot('insurance', v, 'insurance')}
      </div>
    </div>
  </article>`;
}

function docSlot(kind, v, label) {
  const url = v[`${kind}_document_url`];
  return url
    ? `<a class="badge badge--success" href="${h(assetUrl(url))}" target="_blank" rel="noopener" title="View uploaded ${label}">${icon('check', 12)} ${label}</a>`
    : `<label class="badge badge--ghost" style="cursor:pointer" title="Upload ${label}">${icon('upload', 12)} add
        <input type="file" accept=".pdf,.png,.jpg,.jpeg,.webp" hidden data-doc="${kind}" data-vid="${v.id}" /></label>`;
}

function wireDocs(grid, vehicles) {
  grid.querySelectorAll('[data-doc]').forEach((input) => {
    input.addEventListener('change', async () => {
      if (!input.files || !input.files[0]) return;
      try {
        await Api.uploadDoc(input.dataset.vid, input.dataset.doc, input.files[0]);
        toast('Document uploaded.', 'success');
        renderVehicles(document.getElementById('view'));
      } catch (err) {
        toast(errMsg(err), 'error');
      }
    });
  });
}

async function deleteVehicle(id, refresh) {
  const ok = await confirmDialog({
    title: 'Delete this vehicle?',
    message: 'Stop publishing rides with this vehicle? Active rides must be cancelled first.',
    confirmLabel: 'Delete', danger: true,
  });
  if (!ok) return;
  try {
    await Api.deleteVehicle(id);
    toast('Vehicle deleted.', 'success');
    refresh();
  } catch (err) {
    toast(errMsg(err), 'error');
  }
}

function vehicleFormModal(v, onSaved) {
  const isEdit = !!v;
  const modal = openModal({
    title: isEdit ? 'Edit vehicle' : 'Add a vehicle',
    body: `
      <form id="vhForm" novalidate>
        <div class="flex flex-col gap-12">
          <div class="grid-2">
            <div class="field"><label>Vehicle type</label>
              <select class="select" name="vehicle_type" required>
                ${['2-wheeler', '4-wheeler', 'auto', 'bus'].map((t) => `<option value="${t}" ${v && v.vehicle_type === t ? 'selected' : ''}>${vehicleLabel(t)}</option>`).join('')}
              </select></div>
            <div class="field"><label>Seat count</label>
              <input class="input" type="number" name="seat_count" min="1" max="${MAX_SEATS['4-wheeler']}" value="${v ? v.seat_count : DEFAULT_SEATS['4-wheeler']}" required /></div>
          </div>
          <div class="field"><label>Registration number</label>
            <input class="input" name="vehicle_number" value="${v ? h(v.vehicle_number) : ''}" placeholder="KA 01 AB 1234" required style="text-transform:uppercase" /></div>
          <div class="field"><label>Model</label>
            <input class="input" name="vehicle_model" value="${v ? h(v.vehicle_model) : ''}" placeholder="e.g. Hyundai Creta" /></div>
          <div class="grid-2">
            <div class="field"><label>Driving licence no.</label>
              <input class="input" name="dl_number" value="${v ? h(v.dl_number || '') : ''}" placeholder="KA05 1993 0123456" /></div>
            <div class="field"><label>Insurance no.</label>
              <input class="input" name="insurance_number" value="${v ? h(v.insurance_number || '') : ''}" placeholder="Optional" /></div>
          </div>
          <div class="field"><label>Colour <span class="text-faint">(optional)</span></label>
            <input class="input" name="color" value="${v ? h(v.color || '') : ''}" placeholder="e.g. Pearl White" /></div>
          <div class="field"><label>Notes <span class="text-faint">(optional)</span></label>
            <textarea class="textarea" name="notes" placeholder="Anything riders should know" style="min-height:60px">${v ? h(v.notes || '') : ''}</textarea></div>
        </div>
      </form>`,
    foot: `<button class="btn btn-outline" data-cancel>Cancel</button>
           <button class="btn btn-primary" data-save>${isEdit ? 'Save changes' : 'Add vehicle'}</button>`,
    closable: true,
  });

  const save = () => {
    const form = modal.el.querySelector('#vhForm');
    const data = Object.fromEntries(new FormData(form).entries());
    if (!form.reportValidity()) return;
    const submit = modal.el.querySelector('[data-save]');
    submit.disabled = true;
    submit.innerHTML = `${spinnerInline()}`;
    const payload = isEdit ? changedFields(v, data) : data;
    (isEdit ? Api.updateVehicle(v.id, payload) : Api.createVehicle(payload))
      .then(() => {
        modal.close();
        toast(isEdit ? 'Vehicle updated.' : 'Vehicle added — ready to ride!', 'success');
        onSaved();
      })
      .catch((err) => {
        submit.disabled = false;
        submit.textContent = isEdit ? 'Save changes' : 'Add vehicle';
        toast(errMsg(err), 'error');
      });
  };

  modal.el.querySelector('[data-cancel]').addEventListener('click', () => modal.close());
  modal.el.querySelector('[data-save]').addEventListener('click', save);
  formSubmitEnter(modal.el);

  const typeSel = modal.el.querySelector('[name="vehicle_type"]');
  const seatInput = modal.el.querySelector('[name="seat_count"]');
  const applySeatLimits = () => {
    const t = typeSel.value;
    const max = MAX_SEATS[t] || 24;
    seatInput.max = max;
    seatInput.min = 1;
    if (!isEdit) {
      seatInput.value = DEFAULT_SEATS[t] ?? 4;
      return;
    }
    const cur = Number(seatInput.value) || 1;
    if (cur > max) seatInput.value = max;
    if (cur < 1) seatInput.value = 1;
  };
  typeSel.addEventListener('change', applySeatLimits);
  applySeatLimits();

  function formSubmitEnter(scope) {
    scope.querySelectorAll('input').forEach((input) => {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); save(); }
      });
    });
  }
}

function spinnerInline() {
  return '<span class="spin" style="margin-right:6px"></span>';
}

function changedFields(prev, data) {
  const out = {};
  for (const [k, cur] of Object.entries(data)) {
    const prevVal = prev[k] == null ? '' : String(prev[k]);
    const curVal = k === 'seat_count' ? String(Number(cur)) : cur;
    if (curVal !== prevVal) out[k] = k === 'seat_count' ? Number(cur) : cur;
  }
  return out;
}