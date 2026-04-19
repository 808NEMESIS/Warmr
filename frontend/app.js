/**
 * app.js — Shared Warmr frontend utilities
 *
 * Provides:
 *  - Supabase client (anon key — for auth only)
 *  - API wrapper (all requests go to FastAPI with JWT bearer)
 *  - requireAuth() — redirect to index.html if not logged in
 *  - Toast notifications
 *  - Shared helpers: formatDate, timeAgo, fmtRate, sparkline
 */

// ── Configuration ────────────────────────────────────────────────────────────
// Fill these in after creating your Supabase project.
const SUPABASE_URL      = window.WARMR_CONFIG?.supabaseUrl     || 'https://YOUR_PROJECT.supabase.co';
const SUPABASE_ANON_KEY = window.WARMR_CONFIG?.supabaseAnonKey || 'YOUR_ANON_KEY';
const API_BASE          = window.WARMR_CONFIG?.apiBase         || 'http://localhost:8000';

// ── Supabase client (auth only) ───────────────────────────────────────────────
const _supabase = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

// ── Auth helpers ──────────────────────────────────────────────────────────────

/** Return the current session, or null if not logged in. */
async function getSession() {
  const { data: { session } } = await _supabase.auth.getSession();
  return session;
}

/**
 * Guard — call at the top of every protected page.
 * Redirects to /index.html if no session exists.
 * Returns { session, client_id }.
 */
async function requireAuth() {
  const session = await getSession();
  if (!session) {
    window.location.href = '/index.html';
    throw new Error('Not authenticated');
  }
  return { session, client_id: session.user.id };
}

/** Log the current user out and redirect to index. */
async function logout() {
  await _supabase.auth.signOut();
  window.location.href = '/index.html';
}

// ── API wrapper ───────────────────────────────────────────────────────────────

/**
 * Make an authenticated request to the FastAPI layer.
 *
 * @param {string} path       e.g. '/inboxes' or '/analytics/overview'
 * @param {object} [options]  fetch options (method, body, etc.)
 * @returns {Promise<any>}    Parsed JSON response body
 * @throws  On non-2xx response with the error detail from the API
 */
async function api(path, options = {}) {
  const session = await getSession();
  if (!session) throw new Error('Not authenticated');

  const headers = {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${session.access_token}`,
    ...(options.headers || {}),
  };

  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const err = await res.json(); detail = err.detail || detail; } catch {}
    throw new Error(detail);
  }

  if (res.status === 204) return null;
  return res.json();
}

/**
 * Upload a file (multipart/form-data) to the FastAPI layer.
 * Used for CSV lead imports.
 */
async function apiUpload(path, formData) {
  const session = await getSession();
  if (!session) throw new Error('Not authenticated');

  const res = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${session.access_token}` },
    body: formData,
  });

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const err = await res.json(); detail = err.detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// ── Toast notifications ───────────────────────────────────────────────────────

let _toastContainer = null;

function _ensureToastContainer() {
  if (!_toastContainer) {
    _toastContainer = document.createElement('div');
    _toastContainer.id = 'toast-container';
    document.body.appendChild(_toastContainer);
  }
}

const _TOAST_ICONS = {
  success: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>`,
  error:   `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`,
  warning: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>`,
  info:    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`,
};

const _TOAST_COLORS = {
  success: 'var(--success)',
  error:   'var(--danger)',
  warning: 'var(--warning)',
  info:    'var(--info)',
};

/**
 * Show a toast message.
 * @param {string} message
 * @param {'success'|'error'|'warning'|'info'} [type]
 * @param {number} [duration] ms
 */
function toast(message, type = 'info', duration = 4000) {
  _ensureToastContainer();
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.innerHTML = `
    <span style="color:${_TOAST_COLORS[type]};flex-shrink:0">${_TOAST_ICONS[type]}</span>
    <span style="flex:1">${message}</span>
    <button class="btn-icon" onclick="this.parentElement.remove()" style="flex-shrink:0;margin-left:.5rem">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  `;
  _toastContainer.appendChild(el);
  setTimeout(() => { el.style.animation = 'none'; el.style.opacity = '0'; el.style.transition = 'opacity .2s'; setTimeout(() => el.remove(), 200); }, duration);
}

// ── Date / time helpers ───────────────────────────────────────────────────────

/** Format ISO date string to 'd MMM yyyy' */
function formatDate(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('nl-NL', { day: 'numeric', month: 'short', year: 'numeric' });
}

/** Format ISO timestamp to 'HH:mm d MMM' */
function formatDateTime(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('nl-NL', { hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'short' });
}

/** Relative time: '3 minutes ago', '2 hours ago', etc. */
function timeAgo(iso) {
  if (!iso) return '—';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60)   return 'zojuist';
  if (diff < 3600) return `${Math.floor(diff / 60)}m geleden`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}u geleden`;
  return `${Math.floor(diff / 86400)}d geleden`;
}

/** Format a decimal rate as a percentage string. */
function fmtRate(val) {
  if (val == null) return '—';
  return (val * 100).toFixed(1) + '%';
}

/** Format a number with thousands separator. */
function fmtNum(val) {
  if (val == null) return '—';
  return Number(val).toLocaleString('nl-NL');
}

// ── Sparkline ─────────────────────────────────────────────────────────────────

/**
 * Generate an inline SVG sparkline from an array of numeric values.
 * @param {number[]} values
 * @param {number} width
 * @param {number} height
 * @param {string} color  CSS color or 'currentColor'
 * @returns {string} SVG markup string
 */
function sparkline(values, width = 120, height = 36, color = 'var(--primary)') {
  if (!values || values.length < 2) {
    return `<svg width="${width}" height="${height}"></svg>`;
  }
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const pad = 3;
  const w = width - pad * 2;
  const h = height - pad * 2;
  const pts = values.map((v, i) => {
    const x = pad + (i / (values.length - 1)) * w;
    const y = pad + h - ((v - min) / range) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const pathD = 'M ' + pts.join(' L ');
  // Build area fill path
  const first = pts[0].split(',');
  const last  = pts[pts.length - 1].split(',');
  const areaD = `M ${first[0]},${pad + h} L ${pathD.slice(2)} L ${last[0]},${pad + h} Z`;
  return `
    <svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" class="sparkline">
      <defs>
        <linearGradient id="sg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%"   stop-color="${color}" stop-opacity=".18"/>
          <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
        </linearGradient>
      </defs>
      <path d="${areaD}" fill="url(#sg)"/>
      <path d="${pathD}" fill="none" stroke="${color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`.trim();
}

// ── Clipboard helper ──────────────────────────────────────────────────────────

function copyToClipboard(text, label = 'Gekopieerd') {
  navigator.clipboard.writeText(text).then(() => toast(`${label} naar klembord`, 'success', 2000));
}

// ── Set loading state on a button ─────────────────────────────────────────────

function setLoading(btn, loading, label = null) {
  if (!btn) return;
  if (loading) {
    btn._origText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="animation:spin 1s linear infinite"><line x1="12" y1="2" x2="12" y2="6"/><line x1="12" y1="18" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="6" y2="12"/><line x1="18" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"/></svg> ${label || 'Laden…'}`;
  } else {
    btn.disabled = false;
    btn.innerHTML = btn._origText || label || btn.innerHTML;
  }
}

// Spin keyframe (injected once) + product switcher init
(function() {
  if (document.getElementById('warmr-spin-style')) return;
  const s = document.createElement('style');
  s.id = 'warmr-spin-style';
  s.textContent = '@keyframes spin { to { transform: rotate(360deg); } }';
  document.head.appendChild(s);
})();

// Auto-inject suppression link into sidebars that don't already have it
function injectSuppressionLink() {
  if (document.querySelector('a[href="suppression.html"]')) return;
  var unifiedLink = document.querySelector('a[href="unified-inbox.html"]');
  if (!unifiedLink) return;
  var supLink = document.createElement('a');
  supLink.className = 'nav-link';
  supLink.href = 'suppression.html';
  supLink.innerHTML =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
      '<circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>' +
    '</svg>' +
    'Suppression';
  unifiedLink.parentNode.insertBefore(supLink, unifiedLink.nextSibling);
}

// Auto-inject funnel link right after Dashboard
function injectFunnelLink() {
  if (document.querySelector('a[href="funnel.html"]')) return;
  var dashLink = document.querySelector('a[href="dashboard.html"]');
  if (!dashLink) return;
  var funnelLink = document.createElement('a');
  funnelLink.className = 'nav-link';
  funnelLink.href = 'funnel.html';
  funnelLink.innerHTML =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
      '<polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>' +
    '</svg>' +
    'Funnel';
  dashLink.parentNode.insertBefore(funnelLink, dashLink.nextSibling);
}

// Auto-inject campaign performance link after Campagnes
function injectPerformanceLink() {
  if (document.querySelector('a[href="campaign-performance.html"]')) return;
  var campLink = document.querySelector('a[href="campaigns.html"]');
  if (!campLink) return;
  var perfLink = document.createElement('a');
  perfLink.className = 'nav-link';
  perfLink.href = 'campaign-performance.html';
  perfLink.innerHTML =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">' +
      '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>' +
    '</svg>' +
    'Performance';
  campLink.parentNode.insertBefore(perfLink, campLink.nextSibling);
}

// Inject mobile sidebar toggle
function initMobileSidebar() {
  if (document.querySelector('.mobile-toggle')) return;
  var sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;

  var btn = document.createElement('button');
  btn.className = 'mobile-toggle';
  btn.title = 'Menu';
  btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>';

  var overlay = document.createElement('div');
  overlay.className = 'mobile-overlay';

  document.body.appendChild(btn);
  document.body.appendChild(overlay);

  btn.addEventListener('click', function() {
    sidebar.classList.add('open');
    overlay.classList.add('open');
  });
  overlay.addEventListener('click', function() {
    sidebar.classList.remove('open');
    overlay.classList.remove('open');
  });
  // Close menu when nav-link clicked on mobile
  sidebar.querySelectorAll('a').forEach(function(a) {
    a.addEventListener('click', function() {
      sidebar.classList.remove('open');
      overlay.classList.remove('open');
    });
  });
}

// Inject favicon if not present
function injectFavicon() {
  if (document.querySelector('link[rel="icon"]')) return;
  var link = document.createElement('link');
  link.rel = 'icon';
  link.type = 'image/png';
  link.href = 'logo-warmr.png';
  document.head.appendChild(link);
}

// ── Real-time notification polling ───────────────────────────────────────────

var _notifPollInterval = null;
var _notifShownIds = new Set();
var _notifLastPoll = null;
var _notifPermissionAsked = false;

function _startNotifPolling() {
  if (_notifPollInterval) return;
  _notifLastPoll = localStorage.getItem('warmr_notif_last_poll')
    || new Date(Date.now() - 5 * 60 * 1000).toISOString(); // start 5 min back
  _pollNotifications();
  _notifPollInterval = setInterval(function() {
    if (document.visibilityState === 'visible') {
      _pollNotifications();
    }
  }, 30000);
}

function _askBrowserNotifPermission() {
  if (_notifPermissionAsked) return;
  _notifPermissionAsked = true;
  if (!('Notification' in window)) return;
  if (Notification.permission === 'default') {
    Notification.requestPermission().catch(function() {});
  }
}

async function _pollNotifications() {
  try {
    var since = encodeURIComponent(_notifLastPoll || new Date(Date.now() - 60000).toISOString());
    var res = await api('/notifications/poll?since=' + since);
    if (!res) return;

    // Update sidebar badge for unread replies
    var replyBadge = document.getElementById('nav-replies');
    if (replyBadge) setNavBadge('nav-replies', res.unread_reply_count || 0);

    // Process new replies
    (res.new_replies || []).forEach(function(r) {
      if (_notifShownIds.has('r:' + r.id)) return;
      _notifShownIds.add('r:' + r.id);

      var subject = String(r.subject || '(geen onderwerp)').slice(0, 60);
      var sender = String(r.from_email || 'onbekend');
      var cls = r.classification || '';
      var msg = 'Nieuwe reply van ' + sender + (cls === 'interested' ? ' (INTERESSE!)' : '') + ': ' + subject;

      if (typeof toast === 'function') {
        toast(msg, cls === 'interested' ? 'success' : 'info', 6000);
      }

      _showBrowserNotification('Nieuwe reply — Warmr', msg);
    });

    // Process new system notifications
    (res.new_notifications || []).forEach(function(n) {
      if (_notifShownIds.has('n:' + n.id)) return;
      _notifShownIds.add('n:' + n.id);

      var type = n.priority === 'urgent' ? 'error' : n.priority === 'high' ? 'warning' : 'info';
      if (typeof toast === 'function') {
        toast(String(n.message || '').slice(0, 200), type, 5000);
      }
      if (n.priority === 'urgent' || n.priority === 'high') {
        _showBrowserNotification('Warmr — ' + (n.type || 'melding'), String(n.message || '').slice(0, 200));
      }
    });

    // Persist last poll time
    if (res.server_time) {
      _notifLastPoll = res.server_time;
      localStorage.setItem('warmr_notif_last_poll', res.server_time);
    }
  } catch (err) {
    // Silent fail — log to console, keep polling
    console.debug('Notification poll failed:', err.message);
  }
}

function _showBrowserNotification(title, body) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  if (document.visibilityState === 'visible') return; // don't double-notify when tab active
  try {
    var n = new Notification(title, {
      body: body,
      icon: '/logo-warmr.png',
      silent: false,
    });
    n.onclick = function() {
      window.focus();
      window.location.href = '/unified-inbox.html';
      n.close();
    };
    setTimeout(function() { n.close(); }, 10000);
  } catch {}
}

// ── Confirm modal (replaces native confirm()) ───────────────────────────────

function confirmDialog(opts) {
  return new Promise(function(resolve) {
    var title = opts.title || 'Bevestig';
    var body = opts.body || 'Weet je het zeker?';
    var confirmText = opts.confirmText || 'Ja';
    var cancelText = opts.cancelText || 'Annuleren';
    var danger = !!opts.danger;

    var overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:9999;display:flex;align-items:center;justify-content:center';

    var modal = document.createElement('div');
    modal.style.cssText = 'background:var(--surface);border-radius:12px;padding:1.5rem;max-width:420px;width:calc(100vw - 2rem);box-shadow:0 12px 48px rgba(0,0,0,.3);animation:spin 0s';

    var h = document.createElement('h3');
    h.style.cssText = 'font-family:var(--font-disp);font-size:1.125rem;margin-bottom:.5rem';
    h.textContent = title;

    var p = document.createElement('p');
    p.style.cssText = 'color:var(--muted);font-size:.875rem;line-height:1.5;margin-bottom:1.5rem';
    p.textContent = body;

    var btns = document.createElement('div');
    btns.style.cssText = 'display:flex;gap:.5rem;justify-content:flex-end';

    var cancelBtn = document.createElement('button');
    cancelBtn.className = 'btn btn-ghost btn-sm';
    cancelBtn.textContent = cancelText;
    cancelBtn.onclick = function() { cleanup(); resolve(false); };

    var okBtn = document.createElement('button');
    okBtn.className = 'btn btn-sm ' + (danger ? 'btn-danger' : 'btn-primary');
    okBtn.textContent = confirmText;
    okBtn.onclick = function() { cleanup(); resolve(true); };

    btns.appendChild(cancelBtn);
    btns.appendChild(okBtn);
    modal.appendChild(h);
    modal.appendChild(p);
    modal.appendChild(btns);
    overlay.appendChild(modal);

    function cleanup() {
      document.removeEventListener('keydown', keyHandler);
      overlay.remove();
    }
    function keyHandler(e) {
      if (e.key === 'Escape') { cleanup(); resolve(false); }
      if (e.key === 'Enter')  { cleanup(); resolve(true); }
    }
    document.addEventListener('keydown', keyHandler);
    overlay.onclick = function(e) { if (e.target === overlay) { cleanup(); resolve(false); } };

    document.body.appendChild(overlay);
    setTimeout(function() { okBtn.focus(); }, 50);
  });
}

// ── Undo-toast (toast with "Ongedaan" action) ───────────────────────────────

function toastWithUndo(message, undoLabel, onUndo, ms) {
  _ensureToastContainer();
  ms = ms || 5000;
  var el = document.createElement('div');
  el.className = 'toast info';
  el.style.minWidth = '280px';
  var undoBtn = '<button style="background:transparent;border:none;color:var(--primary);font-weight:700;cursor:pointer;margin-left:.75rem;padding:0">' + (undoLabel || 'Ongedaan') + '</button>';
  el.innerHTML =
    '<span style="flex:1">' + message + '</span>' +
    undoBtn +
    '<button class="btn-icon" style="margin-left:.25rem" onclick="this.parentElement.remove()">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
    '</button>';
  var btn = el.querySelector('button');
  btn.onclick = function() {
    try { onUndo && onUndo(); } catch(e) { console.error(e); }
    el.remove();
  };
  _toastContainer.appendChild(el);
  setTimeout(function() { if (el.parentElement) el.remove(); }, ms);
}

// ── Dark mode ───────────────────────────────────────────────────────────────

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('warmr_theme', theme);
}

function toggleTheme() {
  var cur = document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(cur === 'dark' ? 'light' : 'dark');
}

function _initTheme() {
  var saved = localStorage.getItem('warmr_theme') || 'light';
  applyTheme(saved);
}

// Inject theme toggle button into sidebar footer
function injectThemeToggle() {
  if (document.querySelector('#theme-toggle-btn')) return;
  var footer = document.querySelector('.sidebar-footer > div');
  if (!footer) return;
  var btn = document.createElement('button');
  btn.id = 'theme-toggle-btn';
  btn.className = 'btn-icon';
  btn.title = 'Thema wisselen (t)';
  btn.onclick = toggleTheme;
  btn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  var logoutBtn = footer.querySelector('button[title="Uitloggen"]');
  if (logoutBtn) footer.insertBefore(btn, logoutBtn);
  else footer.appendChild(btn);
}

// ── Keyboard shortcuts ──────────────────────────────────────────────────────

var _SHORTCUTS = [
  { key: '/', desc: 'Focus zoeken',          action: function() {
      var inp = document.querySelector('input[type="search"], #search');
      if (inp) { inp.focus(); inp.select(); return true; }
      return false;
  }},
  { key: 'Escape', desc: 'Sluit drawer / modal', action: function() {
      var openDrawer = document.querySelector('.drawer.open, .lead-drawer.open, .modal-overlay[style*="flex"]');
      if (openDrawer) {
        if (typeof closeDrawer === 'function') { try { closeDrawer(); } catch{} }
        document.querySelectorAll('.modal-overlay').forEach(function(m) { if (m.style.display === 'flex') m.style.display = 'none'; });
        document.querySelectorAll('.drawer, .lead-drawer').forEach(function(d) { d.classList.remove('open'); });
        document.querySelectorAll('.drawer-overlay').forEach(function(o) { o.classList.remove('open'); });
        return true;
      }
      return false;
  }},
  { key: 'g d', desc: 'Ga naar Dashboard',  action: function() { window.location.href = 'dashboard.html'; return true; }},
  { key: 'g f', desc: 'Ga naar Funnel',     action: function() { window.location.href = 'funnel.html'; return true; }},
  { key: 'g c', desc: 'Ga naar Campagnes',  action: function() { window.location.href = 'campaigns.html'; return true; }},
  { key: 'g l', desc: 'Ga naar Leads',      action: function() { window.location.href = 'leads.html'; return true; }},
  { key: 'g i', desc: 'Ga naar Inboxes',    action: function() { window.location.href = 'inboxes.html'; return true; }},
  { key: 'g u', desc: 'Ga naar Inbox',      action: function() { window.location.href = 'unified-inbox.html'; return true; }},
  { key: 'n',   desc: 'Nieuw (context)',    action: function() {
      var page = (location.pathname || '').toLowerCase();
      if (page.includes('inboxes')) { window.location.hash = '#new'; return true; }
      if (page.includes('campaigns')) { window.location.hash = '#new'; return true; }
      if (page.includes('leads')) { window.location.href = 'campaigns.html#new'; return true; }
      return false;
  }},
  { key: 't',   desc: 'Thema wisselen',     action: function() { toggleTheme(); return true; }},
  { key: '?',   desc: 'Help (shortcuts)',   action: function() { showShortcutHelp(); return true; }},
];

var _keyBuffer = '';
var _keyBufferTimer = null;

function _handleKey(e) {
  // Don't trigger when typing in inputs/textareas/contenteditable
  var t = e.target;
  var tag = (t && t.tagName || '').toLowerCase();
  var typing = tag === 'input' || tag === 'textarea' || (t && t.isContentEditable);
  if (typing && e.key !== 'Escape' && e.key !== '/') return;

  // Modifiers — don't interfere with Cmd+S, Ctrl+C, etc.
  if (e.ctrlKey || e.metaKey || e.altKey) return;

  // Handle Escape and '/' directly
  if (e.key === 'Escape' || e.key === '/') {
    for (var i = 0; i < _SHORTCUTS.length; i++) {
      if (_SHORTCUTS[i].key === e.key) {
        if (_SHORTCUTS[i].action()) { e.preventDefault(); _keyBuffer = ''; return; }
      }
    }
  }

  var k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (k.length !== 1) { _keyBuffer = ''; return; }

  _keyBuffer = (_keyBuffer + ' ' + k).trim();
  if (_keyBuffer.length > 5) _keyBuffer = _keyBuffer.slice(-5);
  clearTimeout(_keyBufferTimer);
  _keyBufferTimer = setTimeout(function() { _keyBuffer = ''; }, 1000);

  for (var j = 0; j < _SHORTCUTS.length; j++) {
    var s = _SHORTCUTS[j];
    if (s.key === _keyBuffer || s.key === k) {
      if (s.action()) {
        e.preventDefault();
        _keyBuffer = '';
        return;
      }
    }
  }
}

function showShortcutHelp() {
  var existing = document.getElementById('shortcut-help-modal');
  if (existing) { existing.remove(); return; }
  var overlay = document.createElement('div');
  overlay.id = 'shortcut-help-modal';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;display:flex;align-items:center;justify-content:center';
  overlay.onclick = function(e) { if (e.target === overlay) overlay.remove(); };
  var rows = _SHORTCUTS.map(function(s) {
    return '<tr><td style="padding:.4rem .5rem"><kbd style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:.1rem .4rem;font-family:monospace;font-size:.75rem">' + s.key + '</kbd></td><td style="padding:.4rem .5rem;font-size:.8125rem">' + s.desc + '</td></tr>';
  }).join('');
  overlay.innerHTML =
    '<div style="background:var(--surface);border-radius:12px;padding:1.5rem;max-width:440px;width:calc(100vw - 2rem)">' +
      '<h3 style="font-family:var(--font-disp);font-size:1.125rem;margin-bottom:.875rem">Keyboard shortcuts</h3>' +
      '<table style="width:100%"><tbody>' + rows + '</tbody></table>' +
      '<div style="margin-top:1rem;text-align:right"><button class="btn btn-ghost btn-sm" onclick="document.getElementById(\'shortcut-help-modal\').remove()">Sluiten</button></div>' +
    '</div>';
  document.body.appendChild(overlay);
}

// ── Impersonation banner ─────────────────────────────────────────────────────

function _decodeJWT(token) {
  try {
    var parts = token.split('.');
    if (parts.length !== 3) return null;
    var payload = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    while (payload.length % 4) payload += '=';
    return JSON.parse(atob(payload));
  } catch { return null; }
}

async function _checkImpersonationBanner() {
  var session = await getSession().catch(function() { return null; });
  if (!session || !session.access_token) return;
  var claims = _decodeJWT(session.access_token);
  if (!claims || !claims.impersonator_id) return;

  var existing = document.getElementById('imp-banner');
  if (existing) return;

  var banner = document.createElement('div');
  banner.id = 'imp-banner';
  banner.style.cssText =
    'position:fixed;top:0;left:0;right:0;z-index:9000;' +
    'background:#dc2626;color:#fff;padding:.4rem .75rem;' +
    'font-size:.8rem;font-weight:600;text-align:center;' +
    'box-shadow:0 2px 8px rgba(0,0,0,.2);';
  banner.innerHTML =
    '\u26A0\uFE0F Impersonation actief \u2014 je bekijkt data als client ' +
    '<span style="font-family:monospace">' + (claims.sub || '').slice(0, 8) + '\u2026</span> ' +
    '(admin: <span style="font-family:monospace">' + String(claims.impersonator_id).slice(0, 8) + '\u2026</span>) ' +
    '<button onclick="logout()" style="background:rgba(0,0,0,.3);border:none;color:#fff;padding:.15rem .6rem;border-radius:4px;cursor:pointer;margin-left:.5rem;font-size:.7rem">Uitloggen</button>';
  document.body.insertBefore(banner, document.body.firstChild);
  document.body.style.paddingTop = '32px';
}

// Auto-init sidebar/logo/switcher when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
  _initTheme();
  injectFavicon();
  replaceSidebarLogo();
  initProductSwitcher();
  injectFunnelLink();
  injectPerformanceLink();
  injectSuppressionLink();
  injectThemeToggle();
  initMobileSidebar();
  _checkImpersonationBanner();
  document.addEventListener('keydown', _handleKey);
  // Start polling after auth is confirmed (delayed to not race with login flow)
  setTimeout(function() {
    getSession().then(function(s) {
      if (s) {
        _askBrowserNotifPermission();
        _startNotifPolling();
      }
    }).catch(function() {});
  }, 2000);
});

// ── Navigation badge setter ───────────────────────────────────────────────────

function setNavBadge(linkId, count) {
  const link = document.getElementById(linkId);
  if (!link) return;
  let badge = link.querySelector('.nav-badge');
  if (!count) { if (badge) badge.remove(); return; }
  if (!badge) { badge = document.createElement('span'); badge.className = 'nav-badge'; link.appendChild(badge); }
  badge.textContent = count > 99 ? '99+' : count;
}

// ── Sidebar active state ──────────────────────────────────────────────────────

function setActiveNav(href) {
  document.querySelectorAll('.nav-link').forEach(l => {
    l.classList.toggle('active', l.getAttribute('href') === href || l.dataset.page === href);
  });
}

// ── Inject admin link in sidebar if user is admin ─────────────────────────

async function injectAdminLink() {
  try {
    // Lightweight check: /admin/stats returns 403 for non-admins
    await api('/admin/stats');
    // If we get here, user is admin — inject link into sidebar
    const sidebar = document.querySelector('.sidebar > div');
    if (!sidebar) return;
    const adminSection = document.createElement('div');
    adminSection.innerHTML = `
      <div class="sidebar-section-label">Admin</div>
      <a class="nav-link" href="admin.html" style="color:var(--primary)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        Admin panel
      </a>
    `;
    sidebar.prepend(adminSection);
  } catch {
    // Not admin — silently do nothing
  }
}

// ── Product Switcher (Warmr / Heatr) ─────────────────────────────────────────

const HEATR_URL = window.WARMR_CONFIG?.heatrUrl || 'http://localhost:8001';

function replaceSidebarLogo() {
  var logo = document.querySelector('.sidebar-logo');
  if (!logo || logo.querySelector('img.warmr-logo-img')) return;
  // Replace the placeholder mark + text with the real logo image
  var mark = logo.querySelector('.logo-mark');
  var text = logo.querySelector('.logo-text');
  if (mark) mark.remove();
  if (text) text.remove();
  var img = document.createElement('img');
  img.src = 'logo-warmr.png';
  img.alt = 'Warmr';
  img.className = 'warmr-logo-img';
  img.style.cssText = 'height:32px;width:auto;max-width:140px;object-fit:contain';
  logo.insertBefore(img, logo.firstChild);
}

function initProductSwitcher() {
  var logo = document.querySelector('.sidebar-logo');
  if (!logo || logo.querySelector('.product-switcher-btn')) return;

  // Switcher toggle button (grid icon)
  var btn = document.createElement('div');
  btn.className = 'product-switcher-btn';
  btn.title = 'Wissel tussen producten';
  btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>';
  logo.appendChild(btn);

  // Dropdown
  var dropdown = document.createElement('div');
  dropdown.className = 'product-switcher-dropdown';
  dropdown.id = 'product-switcher-dropdown';
  dropdown.innerHTML =
    '<a class="product-switcher-item active" href="/">' +
      '<div class="product-switcher-icon warmr">W</div>' +
      '<div style="flex:1;min-width:0">' +
        '<div class="product-switcher-name">Warmr</div>' +
        '<div class="product-switcher-desc">Email warmup & deliverability</div>' +
      '</div>' +
      '<svg class="product-switcher-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>' +
    '</a>' +
    '<a class="product-switcher-item" href="' + HEATR_URL + '" target="_blank">' +
      '<div class="product-switcher-icon heatr">H</div>' +
      '<div style="flex:1;min-width:0">' +
        '<div class="product-switcher-name">Heatr</div>' +
        '<div class="product-switcher-desc">Lead discovery & website intel</div>' +
      '</div>' +
      '<svg class="product-switcher-check" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="20 6 9 17 4 12"/></svg>' +
    '</a>';
  logo.appendChild(dropdown);

  // Toggle
  btn.addEventListener('click', function(e) {
    e.stopPropagation();
    dropdown.classList.toggle('open');
  });

  // Close on outside click
  document.addEventListener('click', function(e) {
    if (!logo.contains(e.target)) dropdown.classList.remove('open');
  });
}

// ── Populate client name in topbar ────────────────────────────────────────────

async function populateTopbar() {
  const { data: { user } } = await _supabase.auth.getUser();
  const el = document.getElementById('topbar-client');
  if (el && user) {
    const nameEl = el.querySelector('.client-name');
    const avatarEl = el.querySelector('.avatar');
    const email = user.email || '';
    const initials = email.slice(0, 2).toUpperCase();
    if (avatarEl) avatarEl.textContent = initials;
    if (nameEl) nameEl.textContent = email.split('@')[0];
  }
}
