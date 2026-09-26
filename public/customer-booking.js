'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const form = $('preorder-form'), fields = $('booking-fields');
  const message = text => { $('preorder-message').textContent = text; };
  const storageKey = 'laundry-booking-v2';
  let shopCode = '', pending = null, receipt = null, busy = false, shopGeneration = 0;
  let services = [];
  const randomId = () => btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(32)))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=/g, '');
  async function api(path, data) {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data), cache:'no-store', signal:AbortSignal.timeout(70000)});
    const result = await response.json();
    if (!response.ok) { const error = Error(result.error || 'Please try again.'); error.status = response.status; throw error; }
    return result;
  }
  function save(value) { sessionStorage.setItem(storageKey, JSON.stringify(value)); }
  function addItem(item = {}) {
    if ($('booking-items').children.length >= 30) return;
    const row = document.createElement('div'); row.className = 'item-row';
    row.innerHTML = '<h3>Requested service</h3><label>Service<input data-field="service" maxlength="80" required placeholder="For example, Wash and Fold"></label><label>Items / description (optional)<input data-field="description" maxlength="200" placeholder="For example, shirts and towels"></label><div class="two"><label>Number of pieces<input data-field="quantity" type="number" min="1" max="1000" step="1" required></label><label>Estimated weight in grams (optional)<input data-field="estimatedWeightGrams" type="number" min="1" max="1000000" step="1" placeholder="5000 = 5 kg"></label></div><button type="button" class="secondary remove-item">Remove service</button>';
    const oldService = row.querySelector('[data-field="service"]');
    const select = document.createElement('select'); select.dataset.field = 'service'; select.required = true;
    select.add(new Option('Choose a service', ''));
    for (const service of services) select.add(new Option(service, service));
    // Retain the exact original selection when recovering an uncertain submission.
    if (item.service && !services.includes(item.service)) select.add(new Option(item.service, item.service));
    oldService.replaceWith(select);
    for (const input of row.querySelectorAll('input,select')) input.value = item[input.dataset.field] ?? (input.dataset.field === 'quantity' ? 1 : '');
    row.querySelector('button').onclick = () => { if ($('booking-items').children.length > 1) row.remove(); };
    $('booking-items').append(row);
  }
  function fill(payload) {
    for (const key of ['customerName','phone','email','instructions','dropOffDate','dropOffTime']) form.elements[key].value = payload[key] || '';
    form.elements.termsAccepted.checked = payload.termsAccepted === true;
    $('booking-items').replaceChildren(); payload.items.forEach(addItem);
  }
  function showReceipt(result) {
    receipt = result; pending = null; form.hidden = true; $('booking-result').hidden = false;
    $('booking-reference').textContent = result.reference;
    message('Your request was received. Save your reference before closing this tab.');
    try { save({shopCode, receipt:result}); } catch { /* Keep successful result visible. */ }
  }
  async function loadShop(code) {
    const generation = ++shopGeneration;
    form.hidden = true; $('booking-result').hidden = true;
    try {
      message('Finding your shop…');
      const shop = await api('/bookings/shop', {shopCode:code});
      if (generation !== shopGeneration) return;
      shopCode = code; $('shop-name').textContent = shop.businessName;
      services = shop.services || [];
      $('booking-items').replaceChildren(); addItem();
      $('submit-booking').disabled = !services.length;
      $('add-item').disabled = !services.length;
      $('shop-details').textContent = shop.branchName + ' · ' + shop.timezone;
      $('shop-form').hidden = true;
      $('booking-timezone').textContent = 'Times are in ' + shop.timezone + '. Your preferred time is subject to shop confirmation.';
      form.elements.dropOffDate.min = shop.minDropOffDate; form.elements.dropOffDate.max = shop.maxDropOffDate;
      if (receipt) showReceipt(receipt);
      else { form.hidden = false; message(services.length ? 'Pay at Shop · No online payment is collected.' : 'This shop has not published its services yet. Please contact the shop.'); }
    } catch (error) { if (generation === shopGeneration) message(error.message); }
  }
  async function send() {
    if (busy) return;
    busy = true; fields.disabled = true; $('submit-booking').disabled = true;
    message('Sending your request… This may take up to a minute.');
    try { showReceipt(await api('/bookings/submit', pending)); }
    catch (error) {
      if (error.status === 400) {
        pending = null; fields.disabled = false;
        try { sessionStorage.removeItem(storageKey); } catch { /* Keep error visible. */ }
        $('submit-booking').textContent = 'Send Pay at Shop request'; message(error.message);
      } else {
        $('submit-booking').textContent = 'Check / retry the same request';
        message('We could not confirm the result. Your original details are kept for a safe retry. ' + error.message);
      }
    } finally { busy = false; $('submit-booking').disabled = false; }
  }
  form.onsubmit = async event => {
    event.preventDefault();
    if (!pending) {
      const items = [...$('booking-items').children].map(row => {
        const item = {};
        for (const input of row.querySelectorAll('input,select')) item[input.dataset.field] = ['quantity','estimatedWeightGrams'].includes(input.dataset.field) ? (input.value ? Number(input.value) : null) : input.value;
        return item;
      });
      const value = {shopCode, requestId:randomId(), items, paymentChoice:'PAY_AT_SHOP', termsAccepted:form.elements.termsAccepted.checked};
      for (const key of ['customerName','phone','email','instructions','dropOffDate','dropOffTime']) value[key] = form.elements[key].value;
      try { save({pending:value}); } catch { message('Allow tab storage in this browser before submitting so a lost connection can be retried safely.'); return; }
      pending = value;
    }
    await send();
  };
  $('shop-form').onsubmit = event => { event.preventDefault(); loadShop(event.currentTarget.elements.shopCode.value.trim()); };
  $('add-item').onclick = () => addItem();
  $('new-booking').onclick = () => {
    try { sessionStorage.removeItem(storageKey); } catch { message('Your browser could not clear the previous request. Try reopening this tab.'); return; }
    receipt = null; form.reset(); fields.disabled = false; $('booking-items').replaceChildren(); addItem();
    $('submit-booking').textContent = 'Send Pay at Shop request'; loadShop(shopCode);
  };
  addItem();
  let saved;
  try { saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null'); } catch { saved = null; }
  const queryShop = new URLSearchParams(location.search).get('shop');
  if (saved?.pending && Array.isArray(saved.pending.items)) {
    pending = saved.pending; shopCode = pending.shopCode; fill(pending); fields.disabled = true;
    form.hidden = false; $('shop-form').hidden = true; $('shop-name').textContent = 'Recover your previous request';
    $('shop-details').textContent = 'Check the result before starting another booking.';
    $('submit-booking').textContent = 'Check / retry the same request';
    message('A previous request needs confirmation. Retry it using the saved details to avoid a duplicate.');
  } else if (saved?.receipt && saved.shopCode === queryShop) {
    shopCode = saved.shopCode; showReceipt(saved.receipt); $('shop-form').hidden = true;
  } else if (queryShop) loadShop(queryShop);
})();
