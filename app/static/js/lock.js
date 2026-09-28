/*
 * Passphrase lock screen behavior.
 *
 * Tiny, standalone: no app bundle imports, no shared api.js dependency.
 * Submits the passphrase over same-origin fetch to /api/unlock (JSON body,
 * so the server's write-guard for Content-Type=application/json passes)
 * and, on success, reloads the app shell — the freshly-set cookie carries
 * the session forward.
 */
(function () {
  'use strict';

  var form = document.getElementById('lock-form');
  var input = document.getElementById('passphrase');
  var button = document.getElementById('lock-button');
  var message = document.getElementById('lock-message');

  if (!form || !input || !button || !message) return;

  function showMessage(text, tone) {
    message.textContent = text || '';
    message.className = 'lock-message';
    if (tone === 'err') message.classList.add('lock-message--err');
    if (tone === 'ok') message.classList.add('lock-message--ok');
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    var passphrase = input.value;
    if (!passphrase) {
      showMessage('Enter a passphrase.', 'err');
      input.focus();
      return;
    }

    button.disabled = true;
    showMessage('');

    fetch('/api/unlock', {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ passphrase: passphrase })
    }).then(function (response) {
      if (response.status === 200) {
        showMessage('Unlocked. Loading…', 'ok');
        // Full navigation so the shell boots cleanly with the new cookie.
        window.location.replace('/');
        return;
      }
      if (response.status === 429) {
        var retryAfter = response.headers.get('Retry-After') || '?';
        showMessage('Too many attempts. Try again in ' + retryAfter + 's.', 'err');
      } else if (response.status === 401) {
        showMessage('Incorrect passphrase.', 'err');
      } else if (response.status === 415) {
        // Should never fire from this page (we send JSON), but handle it.
        showMessage('Bad request.', 'err');
      } else {
        showMessage('Unlock failed.', 'err');
      }
      button.disabled = false;
      // Keep the field focused and selected so a retry is one keystroke.
      input.focus();
      input.select();
    }).catch(function () {
      showMessage('Network error.', 'err');
      button.disabled = false;
      input.focus();
    });
  });
})();
