/*
 * Theme selection.
 *
 * Persists the choice in localStorage under mineru.theme; applied by stamping
 * a data-theme attribute on the <html> root. First-load defaults respect
 * prefers-color-scheme via the fallback block in tokens.css.
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'mineru.theme';
  const THEMES = [
    { id: 'mail', label: 'Mail', swatch: 'linear-gradient(135deg, #ffffff 0%, #ececee 60%, #2a62d9 100%)' },
    { id: 'forest', label: 'Forest', swatch: 'linear-gradient(135deg, #1c5240, #061713)' },
    { id: 'ocean', label: 'Ocean', swatch: 'linear-gradient(135deg, #12385e, #020a17)' },
    { id: 'warm', label: 'Warm', swatch: 'linear-gradient(135deg, #ffe7c0, #d97a2b)' },
    { id: 'dark', label: 'Dark', swatch: 'linear-gradient(135deg, #1c1c22, #101014)' },
  ];

  function currentTheme() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (storageError) {
      return null;
    }
  }

  function applyTheme(themeId) {
    if (!themeId) return;
    document.documentElement.setAttribute('data-theme', themeId);
    updateThemeColorMeta(themeId);
  }

  function saveTheme(themeId) {
    try {
      localStorage.setItem(STORAGE_KEY, themeId);
    } catch (storageError) {
      // localStorage may be unavailable in private mode; the picker still
      // works, it just doesn't persist. Non-fatal.
    }
  }

  const META_COLORS = {
    mail: '#ffffff',
    forest: '#0f2e26',
    ocean: '#061c31',
    warm: '#f4b874',
    dark: '#101014',
  };

  function updateThemeColorMeta(themeId) {
    const meta = document.querySelector('meta[name="theme-color"]:not([media])');
    if (meta) meta.setAttribute('content', META_COLORS[themeId] || '#ffffff');
  }

  function renderPicker(container, onSelect) {
    container.innerHTML = '';
    // Highlight whatever theme is actually applied to the document (set
    // via applyTheme on load or by a previous pick), not just what's in
    // localStorage — an OS-light-mode user with no persisted pick still
    // gets 'warm' applied but nothing saved, and reading localStorage
    // alone would misleadingly highlight Forest.
    const active = document.documentElement.getAttribute('data-theme')
      || currentTheme()
      || 'mail';
    THEMES.forEach(function (theme) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'theme-swatch' + (theme.id === active ? ' active' : '');
      btn.setAttribute('aria-pressed', theme.id === active ? 'true' : 'false');
      const preview = document.createElement('div');
      preview.className = 'swatch-preview';
      preview.style.background = theme.swatch;
      btn.appendChild(preview);
      const label = document.createElement('span');
      label.textContent = theme.label;
      btn.appendChild(label);
      btn.addEventListener('click', function () {
        applyTheme(theme.id);
        saveTheme(theme.id);
        renderPicker(container, onSelect);
        if (typeof onSelect === 'function') onSelect(theme.id);
      });
      container.appendChild(btn);
    });
  }

  // Apply the persisted theme before the first paint of any tab content.
  // Mail is the default — flat light Apple-Mail look — regardless of OS
  // scheme preference. Users on dark-mode OS still see mail unless they
  // pick another theme in the picker; that keeps the app's identity
  // consistent across boots.
  const initial = currentTheme() || 'mail';
  applyTheme(initial);

  window.MineruTheme = { applyTheme, renderPicker, currentTheme, THEMES };
})();
