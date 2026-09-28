/*
 * Chat tab — placeholder that names Telegram as the current channel.
 * Renders into the reader column; the list column shows a slim "Chat" title.
 */
(function () {
  'use strict';

  function enter() {
    window.MineruShell.setListHeader('💬 Chat');
    const listBody = document.getElementById('list-body');
    if (listBody) {
      listBody.innerHTML = '<div class="list-empty" style="padding:24px 16px;">Coming soon.</div>';
    }
    window.MineruShell.setReaderHeader('Chat', 'Coming soon', []);
    const readerBody = document.getElementById('reader-body');
    if (readerBody) {
      readerBody.innerHTML = `
        <div class="chat-placeholder">
          <div class="chat-emoji" aria-hidden="true">🦊💬</div>
          <div class="chat-title">Chat lives in Telegram — for now.</div>
          <div class="chat-sub">Web chat lands here once the shape is settled.</div>
        </div>
      `;
    }
    window.MineruShell.setStackLevel(window.MineruShell.isMobile() ? 2 : null);
  }

  window.MineruChat = { enter };
})();
