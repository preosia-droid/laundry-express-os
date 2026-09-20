'use strict';
const fragment = new URLSearchParams(location.hash.slice(1));
let recoveryToken = fragment.get('type') === 'recovery' ? fragment.get('access_token') : '';
const linkError = fragment.get('error_description');
history.replaceState(null, '', location.pathname);
const status = document.getElementById('status');
const requestForm = document.getElementById('request');
const resetForm = document.getElementById('reset');
if (recoveryToken) { requestForm.hidden = true; resetForm.hidden = false; }
if (linkError) status.textContent = 'This reset link is invalid or expired. Request a new link below.';
async function send(path, data, bearer='') {
 const response = await fetch(path, {method:'POST',headers:{'Content-Type':'application/json',...(bearer?{Authorization:'Bearer '+bearer}:{})},body:JSON.stringify(data),cache:'no-store',signal:AbortSignal.timeout(70000)});
 const result = await response.json();
 if (!response.ok) throw Error(result.error || 'Please try again.');
 return result;
}
requestForm.onsubmit = async event => {
 event.preventDefault(); const button = requestForm.querySelector('button'); button.disabled = true;
 status.textContent = 'Sending reset link…';
 try { status.textContent = (await send('/recover',{email:requestForm.elements.email.value})).message; }
 catch(error) { status.textContent = error.message; } finally { button.disabled = false; }
};
resetForm.onsubmit = async event => {
 event.preventDefault(); const button = resetForm.querySelector('button'); button.disabled = true;
 try {
  if (resetForm.elements.password.value !== resetForm.elements.confirm.value) throw Error('The passwords do not match.');
  status.textContent = (await send('/reset-password',{password:resetForm.elements.password.value},recoveryToken)).message;
  recoveryToken = ''; resetForm.reset(); resetForm.hidden = true;
 } catch(error) { status.textContent = error.message; } finally { button.disabled = false; }
};
addEventListener('pagehide',()=>{recoveryToken='';resetForm.reset();});
