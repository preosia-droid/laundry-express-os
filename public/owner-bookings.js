'use strict';
let bookingGeneration = 0, bookingEnabled = false, bookingBusy = false;
const serviceEditor = document.createElement('label');
serviceEditor.textContent = 'Services offered for online booking (one per line)';
const serviceInput = document.createElement('textarea'); serviceInput.id = 'booking-services'; serviceInput.rows = 6;
serviceEditor.append(serviceInput);
const saveServices = document.createElement('button'); saveServices.type = 'button'; saveServices.textContent = 'Save booking services';
const usePosServices = document.createElement('button'); usePosServices.type = 'button'; usePosServices.textContent = 'Use synced POS services';
const serviceSource = document.createElement('p');
$('booking-state').after(serviceSource, serviceEditor, saveServices, usePosServices);
function clearBookingView() {
  bookingGeneration++; bookingCursor = null; bookingEnabled = false; bookingBusy = false;
  serviceInput.value = ''; serviceInput.disabled = true; saveServices.disabled = true;
  usePosServices.disabled = true; serviceSource.textContent = '';
  for (const id of ['bookings','booking-message','booking-scope','booking-state']) $(id).replaceChildren();
  $('booking-link').removeAttribute('href'); $('booking-link').textContent = '';
  $('booking-link-wrap').hidden = true; $('more-bookings').hidden = true;
  $('toggle-bookings').disabled = true; $('toggle-bookings').textContent = 'Enable bookings';
  $('load-bookings').disabled = false;
}
function renderBookings(result, append = false) {
  if (!append) $('bookings').replaceChildren();
  for (const booking of result.items) {
    const card = document.createElement('details'); card.className = 'booking-card';
    const summary = document.createElement('summary');
    summary.textContent = `${booking.customerName} · ${booking.dropOffDate} ${booking.dropOffTime} · ${booking.status}`;
    card.append(summary);
    for (const value of [booking.reference, booking.phone + (booking.email ? ' · ' + booking.email : ''),
      'Pay at Shop · Payment is recorded in the POS', ...booking.items.map(item =>
      `${item.service}: ${item.quantity} pieces${item.estimatedWeightGrams ? ', estimated ' + item.estimatedWeightGrams / 1000 + ' kg' : ''}${item.description ? ' · ' + item.description : ''}`),
      booking.instructions ? 'Instructions: ' + booking.instructions : 'No special instructions.']) {
      const p = document.createElement('p'); p.textContent = value; card.append(p);
    }
    $('bookings').append(card);
  }
  if (!append && !result.items.length) $('bookings').textContent = 'No customer pre-orders for this branch.';
  bookingCursor = result.nextCursor; $('more-bookings').hidden = !bookingCursor;
}
async function loadBookings(append = false, enabled, services, usePosCatalog = false) {
  if (bookingBusy) return;
  if (!append) clearBookingView();
  const g = bookingGeneration, session = token, selection = scope();
  const current = () => g === bookingGeneration && session === token && JSON.stringify(selection) === JSON.stringify(scope());
  bookingBusy = true; $('load-bookings').disabled = true; $('more-bookings').disabled = true; $('toggle-bookings').disabled = true;
  const m = memberships[Number($('branch').value)];
  $('booking-scope').textContent = m.businessName + ' / ' + m.branchName;
  $('booking-message').textContent = 'Loading…';
  try {
    if (!append) {
      const settings = await api('/bookings/settings', {...selection, ...(enabled === undefined ? {} : {enabled}), ...(services === undefined ? {} : {services}), ...(usePosCatalog ? {usePosCatalog:true} : {})});
      if (!current()) return;
      bookingEnabled = settings.enabled;
      serviceInput.value = settings.services.join('\n'); serviceInput.disabled = false; saveServices.disabled = false;
      usePosServices.disabled = false;
      serviceSource.textContent = settings.manualServices ? 'Using your online service list.' : 'Using the synced POS catalog. Exactly one paired POS catalog is required; otherwise save an online service list above.';
      $('booking-state').textContent = bookingEnabled ? 'Customer bookings are enabled.' : 'Customer bookings are paused.';
      $('toggle-bookings').textContent = bookingEnabled ? 'Pause bookings' : 'Enable bookings';
      if (settings.shopCode) {
        const url = new URL('/book.html', location.origin); url.searchParams.set('shop', settings.shopCode);
        $('booking-link').href = url.href; $('booking-link').textContent = url.href; $('booking-link-wrap').hidden = false;
      }
    }
    const result = await api('/bookings/list', {...selection, limit:20, ...(append ? {before:bookingCursor} : {})});
    if (!current()) return;
    renderBookings(result, append); $('booking-message').textContent = '';
  } catch (error) { if (current()) $('booking-message').textContent = error.message; }
  finally { if (current()) { bookingBusy = false; $('load-bookings').disabled = false; $('more-bookings').disabled = false; $('toggle-bookings').disabled = false; } }
}
$('load-bookings').onclick = () => loadBookings();
$('more-bookings').onclick = () => loadBookings(true);
$('toggle-bookings').onclick = () => loadBookings(false, !bookingEnabled);
saveServices.onclick = () => loadBookings(false, undefined, serviceInput.value.split('\n').map(s => s.trim()).filter(Boolean));
usePosServices.onclick = () => loadBookings(false, undefined, undefined, true);
