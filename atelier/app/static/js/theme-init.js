/*
 * Atelier Sayyar - theme-init.js
 * Tiny blocking script loaded in <head> so there is no flash of the wrong
 * theme / sidebar state.  Applies the stored theme (light | dark; absent
 * means "follow the system"), the collapsed-sidebar preference and drops the
 * no-js class.  Everything is wrapped: storage may be blocked or throw.
 */
(function () {
  var root = document.documentElement;
  root.className = root.className.replace(/(^|\s)no-js(\s|$)/, ' ').trim() + ' js';
  try {
    var theme = window.localStorage.getItem('atelier:theme');
    if (theme === 'light' || theme === 'dark') root.setAttribute('data-theme', theme);
    if (window.localStorage.getItem('atelier:sidebar') === 'collapsed') root.classList.add('sb-collapsed');
  } catch (e) {
    /* private mode / blocked storage: fall back to system theme */
  }
})();
