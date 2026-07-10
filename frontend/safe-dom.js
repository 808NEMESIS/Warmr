/**
 * safe-dom.js — shared output-encoding helpers for every Warmr frontend page.
 * Load this BEFORE app.js (and before any inline <script> on pages that skip
 * app.js) so escapeHtml/safeText are available to every page's renderers.
 *
 * Consolidates the escapeHtml pattern that previously existed independently
 * (and inconsistently) in campaign-performance.html, funnel.html, and an
 * incomplete variant in unified-inbox.html.
 */
(function (window) {
  'use strict';

  var ENTITY_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/[&<>"']/g, function (c) { return ENTITY_MAP[c]; });
  }

  function safeText(el, s) {
    if (!el) return;
    el.textContent = s == null ? '' : String(s);
  }

  window.escapeHtml = escapeHtml;
  window.safeText = safeText;
})(window);
