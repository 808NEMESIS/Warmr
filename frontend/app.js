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

// Auto-init sidebar/logo/switcher when DOM is ready
document.addEventListener('DOMContentLoaded', function() {
  injectFavicon();
  replaceSidebarLogo();
  initProductSwitcher();
  injectSuppressionLink();
  initMobileSidebar();
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
