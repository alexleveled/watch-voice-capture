'use strict';
/* Voice Capture dashboard. Vanilla JS, hash routes, relative API paths. */

/* ================= helpers ================= */
const $ = (sel, root) => (root || document).querySelector(sel);
const view = $('#view');

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

const state = {
  nav: 0,            // increments each navigation; stale async renders bail out
  timers: [],        // timeouts/intervals cleared on navigation
  cleanups: [],      // functions run on navigation
  health: null,
  speakerNames: [],
};

function later(fn, ms) { const t = setTimeout(fn, ms); state.timers.push(t); return t; }
function clearRouteState() {
  state.timers.forEach(t => { clearTimeout(t); clearInterval(t); });
  state.timers = [];
  const c = state.cleanups; state.cleanups = [];
  c.forEach(fn => { try { fn(); } catch (e) { /* ignore */ } });
}

/* ---- storage (optional, always guarded) ---- */
function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } }

/* ---- toast ---- */
let toastTimer = null;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3000);
}

/* ---- api ---- */
async function api(path, opts) {
  opts = opts || {};
  const init = { method: opts.method || 'GET', headers: { 'Accept': 'application/json' } };
  if (opts.json !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.json);
  } else if (opts.form) {
    init.body = opts.form; // browser sets multipart boundary
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    throw new Error('Network error: ' + (e && e.message ? e.message : e));
  }
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
  if (!res.ok) {
    let detail = data && data.detail;
    if (detail && typeof detail !== 'string') detail = JSON.stringify(detail);
    throw new Error(detail || ('Request failed (' + res.status + ')'));
  }
  return data;
}

function fail(e) { toast((e && e.message) ? e.message : String(e)); }

/* ---- formatting ---- */
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}
function fmtDateTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' }) + ', ' + fmtTime(iso);
}
function fmtDay(day) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day || '');
  if (!m) return day || '';
  const d = new Date(+m[1], +m[2] - 1, +m[3]);
  return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
}
function dayOf(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return null;
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}
function fmtDur(s) {
  if (s == null || isNaN(s)) return '';
  if (s < 60) return Math.round(s) + ' sec';
  const m = Math.round(s / 60);
  if (m < 60) return m + ' min';
  return Math.floor(m / 60) + ' hr ' + (m % 60) + ' min';
}
function fmtClock(s) { // mm:ss countdown
  s = Math.max(0, s);
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}
function fmtAgo(iso) {
  if (!iso) return 'never';
  const d = new Date(iso);
  if (isNaN(d)) return 'unknown';
  const s = Math.round((Date.now() - d.getTime()) / 1000);
  if (s < 60) return s + ' sec ago';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 86400) return Math.round(s / 3600) + ' hr ago';
  return fmtDateTime(iso);
}

function hashStr(s) {
  let h = 0;
  s = String(s || '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}
function spClassByName(name) { return 'sp-' + (hashStr(name) % 6); }

const TERMINAL = ['done', 'empty', 'failed'];

function statusPill(status) {
  if (!status || status === 'done') return '';
  const cls = status === 'failed' ? 'pill bad' : (status === 'empty' ? 'pill' : 'pill warn');
  const label = { waiting: 'queued', empty: 'no speech', transcribed: 'summary pending' }[status] || status;
  return '<span class="' + cls + '">' + esc(label) + '</span>';
}
function triggerBadge(t) { return t ? '<span class="badge">' + esc(t) + '</span>' : ''; }

function listHtml(items) {
  if (!items || !items.length) return '';
  return '<ul>' + items.map(i => '<li>' + esc(i) + '</li>').join('') + '</ul>';
}
function chipsHtml(items) {
  if (!items || !items.length) return '';
  return '<div class="chips">' + items.map(i => '<span class="chip">' + esc(i) + '</span>').join('') + '</div>';
}
function actionItemsHtml(items) {
  if (!items || !items.length) return '';
  return '<ul>' + items.map(a => {
    if (typeof a === 'string') return '<li>' + esc(a) + '</li>';
    let extra = [];
    if (a.owner) extra.push(esc(a.owner));
    if (a.due) extra.push('due ' + esc(a.due));
    return '<li>' + esc(a.item) + (extra.length ? ' <span class="faint">(' + extra.join(', ') + ')</span>' : '') + '</li>';
  }).join('') + '</ul>';
}

function subBlock(title, inner) {
  return inner ? '<div class="sub-list"><b>' + esc(title) + '</b>' + inner + '</div>' : '';
}

/* two-tap confirm; never window.confirm */
function armConfirm(btn, armedText, action) {
  const orig = btn.textContent;
  btn.addEventListener('click', () => {
    if (btn.dataset.armed === '1') {
      clearTimeout(btn._armTimer);
      btn.dataset.armed = '';
      btn.classList.remove('armed');
      btn.textContent = orig;
      action();
      return;
    }
    btn.dataset.armed = '1';
    btn.classList.add('armed');
    btn.textContent = armedText;
    btn._armTimer = setTimeout(() => {
      btn.dataset.armed = '';
      btn.classList.remove('armed');
      btn.textContent = orig;
    }, 4000);
  });
}

async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* fall through */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch (e) { return false; }
}

function setView(html) { view.innerHTML = html; }

/* ================= health ================= */
function healthSummaryHtml(h) {
  if (!h) return '<span class="muted">Status unavailable.</span>';
  if (h.ok) return '<b>All systems OK.</b>';
  const probs = (h.problems || []);
  return '<b>Problems</b>' + (probs.length ? '<ul>' + probs.map(p => '<li>' + esc(p) + '</li>').join('') + '</ul>' : '<div class="muted">Unknown problem.</div>');
}

async function refreshHealth() {
  const dot = $('#health-dot');
  try {
    const h = await api('api/health');
    state.health = h;
    dot.className = 'status-btn ' + (h && h.ok ? 'ok' : 'warn');
    $('#health-label').textContent = h && h.ok ? 'Pipeline healthy' : 'Needs attention';
    const probs = (h && h.problems) || [];
    dot.title = h && h.ok ? 'All systems OK' : (probs.length ? probs.join('; ') : 'Needs attention');
  } catch (e) {
    state.health = null;
    dot.className = 'status-btn warn';
    $('#health-label').textContent = 'Server unreachable';
    dot.title = 'Cannot reach server: ' + e.message;
  }
  const pop = $('#health-pop');
  if (!pop.hidden) pop.innerHTML = healthSummaryHtml(state.health);
  return state.health;
}

function initTopbar() {
  $('#health-dot').addEventListener('click', e => {
    e.stopPropagation();
    const pop = $('#health-pop');
    if (pop.hidden) {
      pop.innerHTML = healthSummaryHtml(state.health) + '<div style="margin-top:6px"><a href="#/settings">Pipeline details</a></div>';
      pop.hidden = false;
    } else {
      pop.hidden = true;
    }
  });
  document.addEventListener('click', e => {
    const pop = $('#health-pop');
    if (!pop.hidden && !pop.contains(e.target)) pop.hidden = true;
  });
  $('#search-form').addEventListener('submit', e => {
    e.preventDefault();
    const q = $('#search-input').value.trim();
    if (!q) return;
    lsSet('vc_last_q', q);
    location.hash = '#/search?q=' + encodeURIComponent(q);
    $('#search-input').blur();
  });
  document.addEventListener('keydown', e => {
    if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '')) {
      e.preventDefault();
      $('#search-input').focus();
    }
  });
  const last = lsGet('vc_last_q');
  if (last) $('#search-input').value = last;
  refreshHealth();
  setInterval(refreshHealth, 30000);
}

/* ================= router ================= */
function parseHash() {
  const raw = (location.hash || '#/').replace(/^#/, '') || '/';
  const qi = raw.indexOf('?');
  const path = qi >= 0 ? raw.slice(0, qi) : raw;
  const params = new URLSearchParams(qi >= 0 ? raw.slice(qi + 1) : '');
  return { path, params };
}

function route() {
  clearRouteState();
  const token = ++state.nav;
  const { path, params } = parseHash();
  const parts = path.split('/').filter(Boolean);
  window.scrollTo(0, 0);
  let p;
  if (parts.length === 0) p = renderDays(token);
  else if (parts[0] === 'today') p = renderDay(token, todayStr(), 'today');
  else if (parts[0] === 'day' && parts[1]) p = renderDay(token, decodeURIComponent(parts[1]), decodeURIComponent(parts[1]) === todayStr() ? 'today' : 'days');
  else if (parts[0] === 'c' && parts[1]) p = renderConversation(token, decodeURIComponent(parts[1]));
  else if (parts[0] === 'search') p = renderSearch(token, params);
  else if (parts[0] === 'settings') p = renderSettings(token);
  else p = renderDays(token);
  if (p && p.catch) p.catch(e => {
    if (token !== state.nav) return;
    setView('<div class="notice">' + esc(e.message || e) + '</div>');
    fail(e);
  });
}

/* ================= shared UI bits ================= */
const ICON_CHEV = '<svg class="chev" viewBox="0 0 20 20" aria-hidden="true"><path fill="currentColor" d="M7.3 4.3 8.4 3.2 15.2 10l-6.8 6.8-1.1-1.1L12.9 10z"/></svg>';
const ICON_COPY = '<svg viewBox="0 0 20 20" aria-hidden="true"><path fill="currentColor" d="M7 2h8.5A2.5 2.5 0 0 1 18 4.5V13a2.5 2.5 0 0 1-2.5 2.5H15V14h.5c.5 0 .9-.4.9-.9V4.5c0-.5-.4-.9-.9-.9H7c-.5 0-.9.4-.9.9V5H4.5v-.5A2.5 2.5 0 0 1 7 2zM4.5 6H13a2.5 2.5 0 0 1 2.5 2.5V16A2.5 2.5 0 0 1 13 18.5H4.5A2.5 2.5 0 0 1 2 16V8.5A2.5 2.5 0 0 1 4.5 6zm0 1.6c-.5 0-.9.4-.9.9V16c0 .5.4.9.9.9H13c.5 0 .9-.4.9-.9V8.5c0-.5-.4-.9-.9-.9H4.5z"/></svg>';

function initials(name) {
  name = String(name || '?').trim();
  const m = /^speaker\s+([a-z0-9]+)$/i.exec(name);
  if (m) return m[1].slice(0, 2).toUpperCase();
  const parts = name.split(/\s+/).filter(Boolean);
  return ((parts[0] || '?')[0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}
function avatarHtml(name, cls, small) {
  return '<span class="avatar ' + (small ? 'sm ' : '') + (cls || spClassByName(name)) + '" title="' + esc(name) + '">' + esc(initials(name)) + '</span>';
}
function triggerBadgeHtml(t) {
  if (!t) return '';
  const label = { manual: 'Button', auto: 'Auto', calendar: 'Meeting', upload: 'Upload' }[t] || t;
  return '<span class="badge ' + esc(t) + '">' + esc(label) + '</span>';
}
function pageHead(title, subParts, actions, crumb) {
  const sub = (subParts || []).filter(Boolean);
  return '<header class="page-head"><div style="min-width:0">' + (crumb || '') +
    '<h1>' + title + '</h1>' +
    (sub.length ? '<div class="sub">' + sub.join('<span class="sep">/</span>') + '</div>' : '') +
    '</div>' + (actions ? '<div class="actions">' + actions + '</div>' : '') + '</header>';
}
function fmtDayLong(day) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day || '');
  if (!m) return day || '';
  return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
}
function todayStr() { return dayOf(new Date().toISOString()); }
function setActiveNav(name) {
  document.querySelectorAll('.side-nav a').forEach(a => a.classList.toggle('active', a.dataset.nav === name));
}

/* ================= views: all days ================= */
async function renderDays(token) {
  setActiveNav('days');
  setView('<p class="loading">Loading...</p>');
  const days = await api('api/days?limit=60');
  if (token !== state.nav) return;
  const head = pageHead('Conversations', ['Everything your watch has captured, newest first']);
  if (!days || !days.length) {
    setView(head + '<div class="empty">No recordings yet. Start one on the watch, or upload a test file in Settings.</div>');
    return;
  }
  const weekAgo = dayOf(new Date(Date.now() - 6 * 86400000).toISOString());
  const week = days.filter(d => d.day >= weekAgo);
  const sum = (arr, k) => arr.reduce((a, d) => a + (+d[k] || 0), 0);
  const today = days.find(d => d.day === todayStr());
  const stats =
    '<div class="stats">' +
      '<div class="stat"><div class="label">Today</div><div class="value">' + (today ? today.conversations : 0) + '<small>conversations</small></div></div>' +
      '<div class="stat"><div class="label">Last 7 days</div><div class="value">' + sum(week, 'conversations') + '<small>conversations</small></div></div>' +
      '<div class="stat"><div class="label">Speech, last 7 days</div><div class="value">' + Math.round(sum(week, 'speech_minutes')) + '<small>min</small></div></div>' +
      '<div class="stat"><div class="label">Days recorded</div><div class="value">' + days.length + '</div></div>' +
    '</div>';
  const rows = days.map(d => {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d.day);
    const dt = m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
    return '<a class="row" href="#/day/' + esc(d.day) + '">' +
      '<div class="date-tile"><div class="dow">' + (dt ? esc(dt.toLocaleDateString([], { weekday: 'short' })) : '') + '</div><div class="num">' + (dt ? dt.getDate() : '') + '</div></div>' +
      '<div class="grow"><div class="card-title clamp2">' + (d.headline ? esc(d.headline) : '<span class="muted">Daily summary pending</span>') + '</div>' +
        '<div class="row-meta"><span>' + esc(fmtDayLong(d.day)) + '</span><span>' + esc(d.conversations) + ' conversation' + (d.conversations === 1 ? '' : 's') + '</span><span>' + esc(Math.round(d.speech_minutes || 0)) + ' min of speech</span></div></div>' +
      ICON_CHEV + '</a>';
  }).join('');
  setView(head + stats + '<div class="list">' + rows + '</div>');
}

/* ================= views: day ================= */
function convItemHtml(c) {
  const spk = (c.speakers || []);
  return '<div class="tl-item"><div class="tl-time">' + esc(fmtTime(c.started_at)) + (c.duration_s != null ? '<small>' + esc(fmtDur(c.duration_s)) + '</small>' : '') + '</div>' +
    '<a class="card tl-card" href="#/c/' + encodeURIComponent(c.id) + '">' +
      '<div class="top"><div class="card-title">' + (c.title ? esc(c.title) : '<span class="muted">Untitled conversation</span>') + '</div>' +
        '<div style="display:flex;gap:6px;flex:none">' + triggerBadgeHtml(c.trigger) + statusPill(c.status) + '</div></div>' +
      (c.summary_short ? '<div class="card-text clamp2">' + esc(c.summary_short) + '</div>' : '') +
      (spk.length ? '<div class="foot"><div class="avatars">' + spk.slice(0, 5).map(n => avatarHtml(n, null, true)).join('') + '</div>' +
        '<div class="card-meta" style="margin:0">' + esc(spk.join(', ')) + '</div></div>' : '') +
    '</a></div>';
}

async function renderDay(token, day, navName) {
  setActiveNav(navName || 'days');
  setView('<p class="loading">Loading...</p>');
  const d = await api('api/days/' + encodeURIComponent(day));
  if (token !== state.nav) return;
  const s = d.summary;
  // newest first on screen (copy-day text keeps its own oldest-first order)
  const convs = (d.conversations || []).slice().sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)));
  const shown = convs.filter(c => c.status !== 'empty');
  const empties = convs.filter(c => c.status === 'empty');
  const mins = Math.round(shown.reduce((a, c) => a + (+c.duration_s || 0), 0) / 60);
  const people = [...new Set(shown.flatMap(c => c.speakers || []))];

  const summary = s
    ? '<div class="card"><h3>Day summary</h3>' +
        (s.headline ? '<div class="card-title" style="margin-bottom:8px">' + esc(s.headline) + '</div>' : '') +
        (s.summary ? '<p class="card-text" style="margin:0 0 6px">' + esc(s.summary) + '</p>' : '') +
        subBlock('Key points', listHtml(s.key_points)) +
        subBlock('Action items', actionItemsHtml(s.action_items)) +
      '</div>'
    : '<div class="card"><h3>Day summary</h3><p class="muted" style="margin:0">Builds automatically after the conversations finish.</p></div>';
  const peopleCard = people.length
    ? '<div class="card"><h3>People</h3><div class="spk-list">' + people.map(n => '<div class="spk-item" style="cursor:default">' + avatarHtml(n) + '<div class="grow">' + esc(n) + '</div></div>').join('') + '</div></div>'
    : '';

  const isToday = day === todayStr();
  setView(
    pageHead(esc(isToday ? 'Today' : fmtDayLong(d.day || day)),
      [isToday ? esc(fmtDayLong(day)) : '', shown.length + ' conversation' + (shown.length === 1 ? '' : 's'), mins + ' min recorded'],
      '<button class="btn" id="btn-copy-day" type="button">' + ICON_COPY + 'Copy whole day</button>',
      navName === 'today' ? '' : '<a class="crumb" href="#/">&larr; All conversations</a>') +
    '<div class="split"><div>' +
      (shown.length ? '<div class="timeline">' + shown.map(convItemHtml).join('') + '</div>' : '<div class="empty">No conversations with speech yet.</div>') +
      (empties.length ? '<button class="toggle-link" id="empty-toggle" type="button">Show ' + empties.length + ' empty recording' + (empties.length === 1 ? '' : 's') + '</button><div id="empty-list" class="timeline" hidden>' + empties.map(convItemHtml).join('') + '</div>' : '') +
    '</div><aside>' + summary + peopleCard + '</aside></div>'
  );
  $('#btn-copy-day').addEventListener('click', async () => {
    try {
      const full = await Promise.all(shown.map(c => api('api/conversations/' + encodeURIComponent(c.id))));
      const ok = await copyText(dayText(d.day || day, d, full));
      toast(ok ? 'Copied the whole day' : 'Could not copy');
    } catch (e) { fail(e); }
  });
  const tg = $('#empty-toggle');
  if (tg) tg.addEventListener('click', () => {
    const list = $('#empty-list');
    list.hidden = !list.hidden;
    tg.textContent = list.hidden ? 'Show ' + empties.length + ' empty recording' + (empties.length === 1 ? '' : 's') : 'Hide empty recordings';
  });
  if (convs.some(c => !TERMINAL.includes(c.status))) {
    later(() => { if (token === state.nav) renderDay(token, day, navName).catch(() => {}); }, 15000);
  }
}

/* ================= views: conversation ================= */
const convUI = { editLabel: null };

function groupSegments(segments) {
  const blocks = [];
  (segments || []).forEach(seg => {
    const key = seg.label == null ? '' : seg.label;
    const last = blocks[blocks.length - 1];
    // Same speaker continues the paragraph, but start a new one (with its own time) every ~60 s.
    if (last && last.key === key && last.display === seg.display && (seg.start_s - last.start_s) < 60) {
      last.text += ' ' + (seg.text || '').trim();
    } else {
      blocks.push({ key, label: seg.label, display: seg.display, at: seg.at, start_s: seg.start_s, text: (seg.text || '').trim() });
    }
  });
  return blocks;
}

function speakerClassFor(conv, label, display) {
  const spk = conv.speakers || [];
  const idx = spk.findIndex(s => s.label === label);
  if (idx >= 0 && spk[idx].is_self) return 'sp-self';
  if (idx >= 0) return 'sp-' + (idx % 6);
  return spClassByName(label || display);
}

function transcriptPlain(conv) {
  return groupSegments(conv.segments).map(b => (b.display || 'Unknown') + ': ' + b.text).join('\n');
}

/* ---- copy text builders (shared by conversation + whole-day copy) ---- */
function plainTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}
function plainDateTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }).replace(/,\s*(\d{4})$/, ' $1') + ', ' + plainTime(iso);
}
function plainList(arr) { return (arr || []).filter(Boolean).map(x => '- ' + x); }
function plainActions(arr) {
  return (arr || []).filter(a => a && a.item).map(a => {
    const bits = [];
    if (a.owner) bits.push('owner: ' + a.owner);
    if (a.due) bits.push('due: ' + a.due);
    return '- ' + a.item + (bits.length ? ' (' + bits.join(', ') + ')' : '');
  });
}
function conversationText(conv) {
  const out = [];
  out.push(conv.title || (conv.summary && conv.summary.title) || 'Untitled conversation');
  const meta = [];
  const when = plainDateTime(conv.started_at);
  if (when) meta.push(when);
  if (conv.duration_s != null && !isNaN(conv.duration_s)) meta.push(Math.max(1, Math.round(conv.duration_s / 60)) + ' min');
  const names = (conv.speakers || []).map(s => s.display || s.label).filter(Boolean);
  if (names.length) meta.push(names.join(', '));
  if (meta.length) out.push(meta.join(' · '));
  const s = conv.summary;
  if (s) {
    const sec = [];
    if (s.summary) sec.push(s.summary);
    const kp = plainList(s.key_points);
    if (kp.length) sec.push('', 'Key points', ...kp);
    const dc = plainList(s.decisions);
    if (dc.length) sec.push('', 'Decisions', ...dc);
    const ai = plainActions(s.action_items);
    if (ai.length) sec.push('', 'Action items', ...ai);
    if (s.people && s.people.length) sec.push('', 'People: ' + s.people.join(', '));
    if (s.topics && s.topics.length) sec.push('Topics: ' + s.topics.join(', '));
    if (sec.length) out.push('', 'SUMMARY', ...sec);
  }
  const blocks = groupSegments(conv.segments);
  if (blocks.length) {
    out.push('', 'TRANSCRIPT');
    blocks.forEach(b => out.push('[' + plainTime(b.at) + '] ' + (b.display || 'Unknown') + ': ' + b.text));
  }
  return out.join('\n');
}
function dayText(day, d, convs) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day || '');
  const label = m
    ? new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }).replace(/,\s*(\d{4})$/, ' $1')
    : (day || '');
  const out = [label];
  const s = d && d.summary;
  if (s) {
    out.push('', 'DAY SUMMARY');
    if (s.headline) out.push(s.headline);
    if (s.summary) out.push(s.summary);
    out.push(...plainList(s.key_points));
    const ai = plainActions(s.action_items);
    if (ai.length) out.push('Action items', ...ai);
  }
  const sorted = convs.slice().sort((a, b) => String(a.started_at).localeCompare(String(b.started_at)));
  sorted.forEach(c => out.push('', '==============================', conversationText(c)));
  return out.join('\n');
}

const convTab = { name: 'summary' };

function renderConvView(conv, token) {
  setActiveNav('days');
  const s = conv.summary;
  const speakers = conv.speakers || [];
  const blocks = groupSegments(conv.segments);
  const spCls = sp => sp.is_self ? 'sp-self' : 'sp-' + (speakers.indexOf(sp) % 6);

  let summaryPanel;
  if (s) {
    const todos = (s.action_items || []).filter(a => a && (a.item || typeof a === 'string')).map(a => {
      const item = typeof a === 'string' ? a : a.item;
      const tags = typeof a === 'string' ? '' : (a.owner ? '<span class="pill">' + esc(a.owner) + '</span>' : '') + (a.due ? '<span class="pill">Due ' + esc(a.due) + '</span>' : '');
      return '<div class="todo"><span class="box"></span><div><div class="what">' + esc(item) + '</div>' + (tags ? '<div class="tags">' + tags + '</div>' : '') + '</div></div>';
    }).join('');
    const list = (arr, cls) => (arr && arr.length) ? '<ul class="block-list ' + (cls || '') + '">' + arr.map(x => '<li>' + esc(x) + '</li>').join('') + '</ul>' : '';
    summaryPanel =
      (s.summary ? '<div class="card"><h3>Overview</h3><p class="overview">' + esc(s.summary) + '</p></div>' : '') +
      (todos ? '<div class="card"><h3>Action items</h3>' + todos + '</div>' : '') +
      ((s.key_points || []).length ? '<div class="card"><h3>Key points</h3>' + list(s.key_points) + '</div>' : '') +
      ((s.decisions || []).length ? '<div class="card"><h3>Decisions</h3>' + list(s.decisions, 'decisions') + '</div>' : '');
  } else {
    const msg = TERMINAL.includes(conv.status) ? 'No summary for this conversation (too short to summarize).' : 'The summary appears when processing finishes.';
    summaryPanel = '<div class="card"><p class="muted" style="margin:0">' + esc(msg) + '</p></div>';
  }

  const transcriptPanel = '<div class="card" style="padding:0"><div class="transcript" id="transcript">' + (blocks.length ? blocks.map(b => {
      const cls = speakerClassFor(conv, b.label, b.display);
      return '<div class="utt">' + avatarHtml(b.display || 'Unknown', cls) + '<div><div class="utt-head"><span class="sp-name ' + cls + '">' + esc(b.display || 'Unknown') + '</span>' +
        '<span class="t">' + esc(fmtTime(b.at)) + '</span></div><div class="utt-text">' + esc(b.text) + '</div></div></div>';
    }).join('') : '<p class="muted" style="margin:0;padding:16px 20px">' + (conv.status === 'empty' ? 'No speech was found in this recording.' : 'No transcript yet.') + '</p>') + '</div></div>';

  const range = fmtTime(conv.started_at) + (conv.ended_at ? ' - ' + fmtTime(conv.ended_at) : '');
  const details = '<div class="card"><h3>Details</h3><dl class="details">' +
      '<dt>Date</dt><dd>' + esc(fmtDayLong(dayOf(conv.started_at))) + '</dd>' +
      '<dt>Time</dt><dd>' + esc(range) + '</dd>' +
      (conv.duration_s != null ? '<dt>Length</dt><dd>' + esc(fmtDur(conv.duration_s)) + '</dd>' : '') +
      '<dt>Started by</dt><dd>' + triggerBadgeHtml(conv.trigger) + '</dd>' +
      '<dt>Words</dt><dd>' + esc(blocks.reduce((a, b) => a + b.text.split(/\s+/).filter(Boolean).length, 0)) + '</dd>' +
    '</dl></div>';
  const spkCard = speakers.length ? '<div class="card"><h3>Speakers</h3><div class="spk-list" id="spk-chips">' + speakers.map(sp =>
      '<button type="button" class="spk-item" data-label="' + esc(sp.label) + '">' + avatarHtml(sp.display || sp.label, spCls(sp)) +
        '<span class="grow">' + esc(sp.display || sp.label) + (sp.is_self ? ' <span class="you">You</span>' : '') + '</span>' +
        '<span class="hint">' + (sp.speaker_id != null ? 'Rename' : 'Name') + '</span></button>').join('') +
    '</div><div id="spk-form"></div></div>' : '';
  const tagCard = (title, arr) => (arr && arr.length) ? '<div class="card"><h3>' + title + '</h3>' + chipsHtml(arr) + '</div>' : '';

  const tab = convTab.name;
  setView(
    pageHead(conv.title ? esc(conv.title) : 'Untitled conversation',
      [esc(fmtDateTime(conv.started_at)), conv.duration_s != null ? esc(fmtDur(conv.duration_s)) : '', triggerBadgeHtml(conv.trigger) + statusPill(conv.status)],
      '<button class="btn primary" id="btn-copy" type="button">' + ICON_COPY + 'Copy summary + transcript</button>' +
      '<button class="btn" id="btn-resum" type="button">Re-summarize</button>' +
      '<button class="btn" id="btn-merge" type="button">Merge with previous</button>' +
      '<button class="btn danger" id="btn-del" type="button">Delete</button>',
      '<a class="crumb" href="#/day/' + esc(dayOf(conv.started_at) || '') + '">&larr; ' + esc(fmtDayLong(dayOf(conv.started_at))) + '</a>') +
    (conv.error ? '<div class="notice">' + esc(conv.error) + '</div>' : '') +
    '<div class="split"><div>' +
      '<div class="tabs" role="tablist">' +
        '<button class="tab' + (tab === 'summary' ? ' active' : '') + '" data-tab="summary" type="button" role="tab">Summary</button>' +
        '<button class="tab' + (tab === 'transcript' ? ' active' : '') + '" data-tab="transcript" type="button" role="tab">Transcript<span class="count">' + blocks.length + '</span></button>' +
      '</div>' +
      '<div id="pane-summary"' + (tab === 'summary' ? '' : ' hidden') + '>' + summaryPanel + '</div>' +
      '<div id="pane-transcript"' + (tab === 'transcript' ? '' : ' hidden') + '>' + transcriptPanel + '</div>' +
    '</div><aside>' + details + spkCard + tagCard('Topics', s && s.topics) + tagCard('People mentioned', s && s.people) + '</aside></div>'
  );

  document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => {
    convTab.name = b.dataset.tab;
    document.querySelectorAll('.tab').forEach(x => x.classList.toggle('active', x === b));
    $('#pane-summary').hidden = convTab.name !== 'summary';
    $('#pane-transcript').hidden = convTab.name !== 'transcript';
  }));
  $('#btn-copy').addEventListener('click', async () => {
    const ok = await copyText(conversationText(conv));
    toast(ok ? 'Copied summary and transcript' : 'Could not copy');
  });
  $('#btn-resum').addEventListener('click', async () => {
    try {
      await api('api/conversations/' + encodeURIComponent(conv.id) + '/resummarize', { method: 'POST' });
      toast('Re-summarizing...');
    } catch (e) { fail(e); }
  });
  $('#btn-merge').addEventListener('click', async () => {
    if (!confirm('Join this conversation onto the one right before it?')) return;
    try {
      const res = await api('api/conversations/' + encodeURIComponent(conv.id) + '/merge-previous', { method: 'POST' });
      location.hash = '#/c/' + encodeURIComponent(res.into);
    } catch (e) { fail(e); }
  });
  armConfirm($('#btn-del'), 'Tap again to delete', async () => {
    try {
      await api('api/conversations/' + encodeURIComponent(conv.id), { method: 'DELETE' });
      toast('Deleted');
      const dd = dayOf(conv.started_at);
      location.hash = dd ? '#/day/' + dd : '#/';
    } catch (e) { fail(e); }
  });

  const chipsEl = $('#spk-chips');
  if (chipsEl) chipsEl.addEventListener('click', e => {
    const b = e.target.closest('button[data-label]');
    if (!b) return;
    openSpeakerForm(conv, b.dataset.label, token);
  });
  if (convUI.editLabel && speakers.some(sp => sp.label === convUI.editLabel)) {
    openSpeakerForm(conv, convUI.editLabel, token, true);
  } else {
    convUI.editLabel = null;
  }
}

function openSpeakerForm(conv, label, token, keepValue) {
  convUI.editLabel = label;
  const sp = (conv.speakers || []).find(s => s.label === label) || {};
  const host = $('#spk-form');
  if (!host) return;
  const names = state.speakerNames.map(n => '<option value="' + esc(n) + '"></option>').join('');
  host.innerHTML =
    '<form class="spk-form" id="spk-form-el" autocomplete="off">' +
      '<input type="text" id="spk-name" list="spk-names" placeholder="Who is this?" aria-label="Who is this?" value="' + esc(sp.speaker_id != null ? (sp.display || '') : '') + '">' +
      '<datalist id="spk-names">' + names + '</datalist>' +
      '<button class="btn primary" type="submit">Save</button>' +
      '<button class="btn ghost" type="button" id="spk-cancel">Cancel</button>' +
    '</form>';
  const input = $('#spk-name');
  if (!keepValue) input.focus();
  $('#spk-cancel').addEventListener('click', () => { convUI.editLabel = null; host.innerHTML = ''; });
  $('#spk-form-el').addEventListener('submit', async e => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name) { toast('Type a name first'); return; }
    try {
      const updated = await api('api/conversations/' + encodeURIComponent(conv.id) + '/speakers/' + encodeURIComponent(label), { method: 'POST', json: { name } });
      if (token !== state.nav) return;
      convUI.editLabel = null;
      if (!state.speakerNames.includes(name)) state.speakerNames.push(name);
      toast('Saved');
      renderConvView(updated, token);
      scheduleConvRefresh(updated.id, updated.status, token);
    } catch (err) { fail(err); }
  });
}

function scheduleConvRefresh(id, status, token) {
  if (TERMINAL.includes(status)) return;
  later(async () => {
    if (token !== state.nav) return;
    if (convUI.editLabel) { scheduleConvRefresh(id, status, token); return; }
    try {
      const c = await api('api/conversations/' + encodeURIComponent(id));
      if (token !== state.nav) return;
      if (!convUI.editLabel) renderConvView(c, token);
      scheduleConvRefresh(id, c.status, token);
    } catch (e) { scheduleConvRefresh(id, status, token); }
  }, 10000);
}

async function renderConversation(token, id) {
  setView('<p class="loading">Loading...</p>');
  convUI.editLabel = null;
  const [conv, names] = await Promise.all([
    api('api/conversations/' + encodeURIComponent(id)),
    api('api/speakers').catch(() => []),
  ]);
  if (token !== state.nav) return;
  state.speakerNames = (names || []).map(n => n.name).filter(Boolean);
  renderConvView(conv, token);
  scheduleConvRefresh(conv.id, conv.status, token);
}

/* ================= views: search ================= */
function searchTerms(q) {
  const out = [];
  String(q || '').replace(/"([^"]+)"|(\S+)/g, (m, a, b) => { const t = (a || b || '').trim(); if (t.length >= 2) out.push(t); return m; });
  return out;
}
function highlight(text, q) {
  const safe = esc(text);
  const terms = (Array.isArray(q) ? q : searchTerms(q)).map(t => esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!terms.length) return safe;
  return safe.replace(new RegExp('(' + terms.join('|') + ')', 'gi'), '<mark>$1</mark>');
}

async function renderSearch(token, params) {
  setActiveNav('search');
  const q = params.get('q') || '';
  const from = params.get('from') || '';
  const to = params.get('to') || '';
  const sp = params.get('sp') || '';
  $('#search-input').value = q;
  const speakers = await api('api/speakers').catch(() => []);
  if (token !== state.nav) return;

  const filters =
    '<form class="card filters" id="search-page-form" autocomplete="off">' +
      '<label class="q">Keywords<input type="search" name="q" value="' + esc(q) + '" placeholder="e.g. priya security, or &quot;exact phrase&quot;" enterkeyhint="search"></label>' +
      '<label>From<input type="date" name="from" value="' + esc(from) + '"></label>' +
      '<label>To<input type="date" name="to" value="' + esc(to) + '"></label>' +
      '<label>Person<select name="sp"><option value="">Anyone</option>' +
        (speakers || []).map(s => '<option value="' + esc(s.id) + '"' + (String(s.id) === sp ? ' selected' : '') + '>' + esc(s.name) + '</option>').join('') +
      '</select></label>' +
      '<button class="btn primary" type="submit">Search</button>' +
    '</form>';

  const head = pageHead('Search', ['Keyword search across every conversation. All words must appear, in any order. Use quotes for an exact phrase.']);
  const bind = () => {
    $('#search-page-form').addEventListener('submit', e => {
      e.preventDefault();
      const f = new FormData(e.target);
      const p = new URLSearchParams();
      ['q', 'from', 'to', 'sp'].forEach(k => { const v = String(f.get(k) || '').trim(); if (v) p.set(k, v); });
      if (p.get('q')) lsSet('vc_last_q', p.get('q'));
      location.hash = '#/search?' + p.toString();
    });
  };
  if (!searchTerms(q).length) {
    setView(head + filters + '<div class="empty">Type a word or two to search all your conversations.</div>');
    bind();
    return;
  }
  setView(head + filters + '<p class="loading">Searching...</p>');
  bind();
  const qs = new URLSearchParams({ q });
  if (from) qs.set('date_from', from);
  if (to) qs.set('date_to', to);
  if (sp) qs.set('speaker', sp);
  const res = await api('api/search?' + qs.toString());
  if (token !== state.nav) return;
  const results = (res && res.results) || [];
  const terms = (res && res.terms) || searchTerms(q);
  let body;
  if (!results.length) {
    body = '<div class="empty">No conversations match &ldquo;' + esc(q) + '&rdquo;' + (from || to || sp ? ' with these filters' : '') + '.</div>';
  } else {
    const totalHits = results.reduce((a, r) => a + (r.hit_count || 0), 0);
    body = '<div class="section-title"><h2>' + results.length + ' conversation' + (results.length === 1 ? '' : 's') + '</h2><span class="card-meta">' + totalHits + ' matching line' + (totalHits === 1 ? '' : 's') + '</span></div>' +
      results.map(r => {
        const hits = (r.hits || []).map(h =>
          '<a class="hit" href="#/c/' + encodeURIComponent(r.id) + '"><span class="t">' + esc(fmtTime(h.at)) + '</span><div><b>' + esc(h.speaker) + '</b>' + highlight(h.text, terms) + '</div></a>').join('');
        const more = (r.hit_count || 0) - (r.hits || []).length;
        return '<div class="card result">' +
          '<a class="result-head" href="#/c/' + encodeURIComponent(r.id) + '">' +
            '<div style="display:flex;justify-content:space-between;gap:10px;align-items:center"><div class="card-title">' + (r.title ? highlight(r.title, terms) : '<span class="muted">Untitled conversation</span>') + '</div>' + triggerBadgeHtml(r.trigger) + '</div>' +
            '<div class="row-meta"><span>' + esc(fmtDateTime(r.started_at)) + '</span>' + (r.duration_s != null ? '<span>' + esc(fmtDur(r.duration_s)) + '</span>' : '') +
              ((r.speakers || []).length ? '<span>' + esc(r.speakers.join(', ')) + '</span>' : '') + '</div>' +
            (r.summary_hit ? '<div class="card-text">' + highlight(r.summary_hit, terms) + '</div>' : '') +
          '</a>' +
          (hits ? '<div class="hits">' + hits + '</div>' : '') +
          (more > 0 ? '<div class="more">+' + more + ' more matching line' + (more === 1 ? '' : 's') + ' in this conversation</div>' : '') +
        '</div>';
      }).join('');
  }
  const loading = view.querySelector('.loading');
  if (loading) loading.outerHTML = body;
}

/* ================= views: settings ================= */
const DAYS = [['mon', 'Monday'], ['tue', 'Tuesday'], ['wed', 'Wednesday'], ['thu', 'Thursday'], ['fri', 'Friday'], ['sat', 'Saturday'], ['sun', 'Sunday']];

function normalizeSettings(s) {
  s = s || {};
  s.auto = s.auto || {};
  s.auto.always_while_worn = !!s.auto.always_while_worn;
  s.auto.windows = s.auto.windows || {};
  DAYS.forEach(([k]) => { if (!Array.isArray(s.auto.windows[k])) s.auto.windows[k] = []; });
  s.auto.calendar = s.auto.calendar || {};
  s.auto.calendar.enabled = !!s.auto.calendar.enabled;
  s.auto.calendar.lead_min = Number.isFinite(+s.auto.calendar.lead_min) ? +s.auto.calendar.lead_min : 5;
  s.silence = s.silence || {};
  s.silence.auto_end_min = Number.isFinite(+s.silence.auto_end_min) ? +s.silence.auto_end_min : 2;
  s.silence.manual_stop_min = Number.isFinite(+s.silence.manual_stop_min) ? +s.silence.manual_stop_min : 10;
  s.merge = s.merge || {};
  s.merge.gap_min = Number.isFinite(+s.merge.gap_min) ? +s.merge.gap_min : 10;
  s.upload = s.upload || {};
  s.upload.battery_saver = !!s.upload.battery_saver;
  s.summary = s.summary || {};
  s.summary.provider = s.summary.provider || 'claude_subscription';
  s.summary.model = s.summary.model || '';
  s.summary.base_url = s.summary.base_url || '';
  s.summary.api_key_set = !!s.summary.api_key_set;
  s.summary.when = s.summary.when || 'now';
  s.summary.daily_time = s.summary.daily_time || '23:00';
  s.system = s.system || {};
  s.system.pause_when_busy = !!s.system.pause_when_busy;
  s.secrets = s.secrets || {};
  s.secrets.hf_token_set = !!s.secrets.hf_token_set;
  s.secrets.calendar_url_set = !!s.secrets.calendar_url_set;
  return s;
}

function healthBlockHtml(h) {
  if (!h) return '<p class="muted" style="margin:0">Could not load status.</p>';
  const w = h.worker || {};
  const q = h.queue || {};
  const rows = [
    ['Overall', h.ok ? '<span class="pill ok">OK</span>' : '<span class="pill warn">Needs attention</span>'],
    ['Worker', (w.alive ? '<span class="pill ok">running</span>' : '<span class="pill bad">stopped</span>') + ' <span class="muted">' + esc(w.state || '') + (w.last_tick ? ' &middot; last tick ' + esc(fmtAgo(w.last_tick)) : '') + '</span>'],
    ['Queue', esc(q.receiving || 0) + ' receiving, ' + esc(q.waiting || 0) + ' waiting, ' + esc(q.transcribing || 0) + ' transcribing, ' + esc(q.summarizing || 0) + ' summarizing, ' + esc(q.failed || 0) + ' failed'],
    ['Hugging Face token', h.hf_token ? 'Yes' : 'No'],
    ['Summaries', esc(h.summary || 'not set')],
    ['Calendar', h.calendar ? 'Connected' : 'Not set up'],
  ];
  let html = '<dl class="kv">' + rows.map(r => '<dt>' + esc(r[0]) + '</dt><dd>' + r[1] + '</dd>').join('') + '</dl>';
  if (!h.hf_token) html += '<div class="notice" style="margin-top:10px;margin-bottom:0">Speaker labels are off until a Hugging Face token is added under Voices, above.</div>';
  if (h.problems && h.problems.length) html += '<div style="margin-top:10px"><b>Problems</b>' + listHtml(h.problems) + '</div>';
  return html;
}

function windowsHtml(st) {
  return DAYS.map(([k, label]) => {
    const wins = st.auto.windows[k];
    return '<div class="day-row"><div class="day-name">' + esc(label) + '</div>' +
      (wins.length ? wins.map((w, i) =>
        '<div class="win"><input type="time" data-day="' + k + '" data-i="' + i + '" data-k="0" value="' + esc(w[0] || '') + '" aria-label="' + esc(label) + ' start">' +
        '<span class="dash">to</span>' +
        '<input type="time" data-day="' + k + '" data-i="' + i + '" data-k="1" value="' + esc(w[1] || '') + '" aria-label="' + esc(label) + ' end">' +
        '<button type="button" class="btn small ghost" data-act="rm" data-day="' + k + '" data-i="' + i + '">Remove</button></div>'
      ).join('') : '<div class="faint" style="font-size:13px;margin-bottom:6px">No windows</div>') +
      '<button type="button" class="btn small" data-act="add" data-day="' + k + '">Add window</button>' +
    '</div>';
  }).join('');
}

function speakersListHtml(list, editingId) {
  if (!list || !list.length) return '<p class="muted" style="margin:0">No voices enrolled yet.</p>';
  return list.map(sp => {
    const meta = esc(sp.samples || 0) + ' sample' + (sp.samples === 1 ? '' : 's') + ' &middot; last heard ' + (sp.last_heard ? esc(fmtDateTime(sp.last_heard)) : 'never');
    const name = editingId === sp.id
      ? '<input type="text" data-rename-input="' + sp.id + '" value="' + esc(sp.name) + '" aria-label="Speaker name">'
      : '<div class="card-title">' + esc(sp.name) + (sp.is_self ? ' <span class="badge">you</span>' : '') + '</div><div class="card-meta">' + meta + '</div>';
    const actions = editingId === sp.id
      ? '<button class="btn small primary" type="button" data-act="rename-save" data-id="' + sp.id + '">Save</button><button class="btn small ghost" type="button" data-act="rename-cancel">Cancel</button>'
      : '<button class="btn small" type="button" data-act="rename" data-id="' + sp.id + '">Rename</button><button class="btn small danger" type="button" data-act="delete" data-id="' + sp.id + '">Delete</button>';
    return '<div class="spk-row"><div class="grow">' + name + '</div><div class="spk-actions">' + actions + '</div></div>';
  }).join('');
}

const PROVIDERS = [
  ['claude_subscription', 'Claude subscription (Claude Code login)'],
  ['codex_subscription', 'ChatGPT subscription (Codex CLI login)'],
  ['openai_compatible', 'API key or local model (OpenAI-compatible)'],
];
const ENDPOINTS = [
  ['', 'Pick a service...'],
  ['https://api.openai.com/v1', 'OpenAI'],
  ['http://localhost:11434/v1', 'Ollama (local, free)'],
  ['http://localhost:1234/v1', 'LM Studio (local, free)'],
  ['https://openrouter.ai/api/v1', 'OpenRouter'],
  ['https://generativelanguage.googleapis.com/v1beta/openai', 'Google Gemini'],
  ['https://api.anthropic.com/v1', 'Anthropic API'],
];
const PROVIDER_HELP = {
  claude_subscription: 'Uses the Claude Code sign-in on this PC. No API charges. Leave the model empty for Sonnet, or type opus or haiku.',
  codex_subscription: 'Uses the Codex CLI signed in with your ChatGPT account (run "codex login" once). No API charges. Leave the model empty for the Codex default.',
  openai_compatible: 'Any service with an OpenAI-style API. Cloud services need a key and bill per use. Ollama and LM Studio run on this PC for free and need no key.',
};

const WHEN = [
  ['now', 'Right away, a minute after each conversation'],
  ['daily', 'Once a day, all at a set time'],
  ['manual', 'Only when I ask'],
];
const WHEN_HELP = {
  now: 'Each conversation is summarized about a minute after it ends.',
  daily: 'Transcripts appear right away. Summaries and the daily digest run in one batch at this time. If the PC was off then, the batch runs when it is next on.',
  manual: 'Transcripts appear right away. Nothing is summarized until you press "Summarize pending now" here, or "Re-summarize" on a conversation.',
};

function whenFieldsHtml(st) {
  const sm = st.summary;
  return '<div class="field-row"><label for="sm-when">When</label><select id="sm-when">' +
      WHEN.map(([v, l]) => '<option value="' + v + '"' + (sm.when === v ? ' selected' : '') + '>' + esc(l) + '</option>').join('') + '</select>' +
      (sm.when === 'daily' ? '<input type="time" id="sm-time" value="' + esc(sm.daily_time) + '" aria-label="Daily summary time">' : '') + '</div>' +
    '<p class="card-meta" style="margin:0 0 10px">' + esc(WHEN_HELP[sm.when] || '') + '</p>' +
    (sm.when !== 'now' ? '<div class="btn-row" style="margin-top:6px"><button class="btn" type="button" id="sm-run">Summarize pending now</button></div>' : '') +
    '<div class="card-meta" id="sm-run-status"></div>';
}

const HF_MODEL_URL = 'https://huggingface.co/pyannote/speaker-diarization-community-1';
const HF_TOKENS_URL = 'https://huggingface.co/settings/tokens';

function hfFieldsHtml(st) {
  const set = st.secrets.hf_token_set;
  return '<ol class="steps"><li>Make a free account at huggingface.co.</li>' +
      '<li>Open <a href="' + HF_MODEL_URL + '" target="_blank" rel="noopener">the speaker model page</a> and accept its terms.</li>' +
      '<li>Create a read token on <a href="' + HF_TOKENS_URL + '" target="_blank" rel="noopener">your tokens page</a> and paste it here.</li></ol>' +
    '<div class="field-row"><label for="sc-hf">Token</label><input type="password" id="sc-hf" class="wide" value="" autocomplete="off" spellcheck="false" placeholder="' + (set ? 'saved (type to replace)' : 'hf_...') + '"></div>' +
    '<div class="btn-row" style="margin-top:6px"><button class="btn" type="button" id="sc-hf-save">Save token</button>' +
      (set ? '<button class="btn" type="button" id="sc-hf-clear">Remove</button>' : '') + '</div>' +
    '<div class="card-meta" id="sc-hf-status">' + (set ? 'A token is saved. Speaker labels are on for new recordings.' : 'No token yet. Every line is labeled as one speaker.') + '</div>';
}

function calFieldsHtml(st) {
  const set = st.secrets.calendar_url_set;
  return '<div class="field-row indent"><label for="sc-cal">Calendar link</label><input type="password" id="sc-cal" class="wide" value="" autocomplete="off" spellcheck="false" placeholder="' + (set ? 'saved (paste to replace)' : 'private iCal link, https://...') + '"></div>' +
    '<div class="btn-row indent" style="margin-top:6px"><button class="btn" type="button" id="sc-cal-save">Save link</button>' +
      (set ? '<button class="btn" type="button" id="sc-cal-clear">Remove</button>' : '') + '</div>' +
    '<div class="card-meta indent" id="sc-cal-status">' + (set ? 'A calendar link is saved.' : 'In Google Calendar: Settings, your calendar, "Secret address in iCal format". The link is read-only and stays on this PC.') + '</div>';
}

function summaryFieldsHtml(st) {
  const sm = st.summary;
  const compat = sm.provider === 'openai_compatible';
  return '<div class="field-row"><label for="sm-provider">Provider</label><select id="sm-provider">' +
      PROVIDERS.map(([v, l]) => '<option value="' + v + '"' + (sm.provider === v ? ' selected' : '') + '>' + esc(l) + '</option>').join('') + '</select></div>' +
    '<p class="card-meta" style="margin:0 0 10px">' + esc(PROVIDER_HELP[sm.provider] || '') + '</p>' +
    (compat
      ? '<div class="field-row"><label for="sm-preset">Service</label><select id="sm-preset">' +
          ENDPOINTS.map(([v, l]) => '<option value="' + esc(v) + '"' + (v && sm.base_url === v ? ' selected' : '') + '>' + esc(l) + '</option>').join('') + '</select></div>' +
        '<div class="field-row"><label for="sm-url">Base URL</label><input type="text" id="sm-url" class="wide" value="' + esc(sm.base_url) + '" placeholder="https://api.openai.com/v1" spellcheck="false"></div>'
      : '') +
    '<div class="field-row"><label for="sm-model">Model</label><input type="text" id="sm-model" class="wide" value="' + esc(sm.model) + '" placeholder="' + (compat ? 'the model name your service uses' : 'default') + '" spellcheck="false"></div>' +
    (compat
      ? '<div class="field-row"><label for="sm-key">API key</label><input type="password" id="sm-key" class="wide" value="" autocomplete="off" placeholder="' + (sm.api_key_set ? 'saved (type to replace)' : 'not needed for local models') + '"></div>'
      : '') +
    '<div class="btn-row" style="margin-top:6px"><button class="btn" type="button" id="sm-test">Save and test</button></div>' +
    '<div class="card-meta" id="sm-status"></div>';
}

const ENROLL_PROMPT = 'Today started like most days. I woke up a little before seven, made a cup of coffee, and checked the weather while the toast was in the toaster. After breakfast I walked to the corner store for milk and bread, said hello to a few neighbors, and then sat down at my desk to answer some messages. Around noon I stepped outside for lunch, and in the evening I cooked dinner and read a few pages of a book before bed.';

function sRow(title, help, body) {
  return '<section class="s-row"><div class="s-info"><h2>' + esc(title) + '</h2><p>' + esc(help) + '</p></div>' +
    '<div class="card s-body">' + body + '</div></section>';
}

async function renderSettings(token) {
  setActiveNav('settings');
  setView('<p class="loading">Loading...</p>');
  const [health, settingsRaw, speakers] = await Promise.all([
    refreshHealth(),
    api('api/settings'),
    api('api/speakers').catch(() => []),
  ]);
  if (token !== state.nav) return;
  const st = normalizeSettings(settingsRaw);
  let spkList = speakers || [];
  let editingId = null;

  setView(
    pageHead('Settings', ['Changes reach the watch within about 2 minutes'],
      '<button class="btn primary" type="button" id="st-save">Save changes</button>') +
    '<div class="settings">' +

    '<div class="s-group">Auto mode</div>' +
    sRow('When to listen', 'Auto mode listens whenever the watch is on your wrist, unless you set weekly windows below. Calendar meetings also count once you paste a calendar link here.',
      '<label class="row-check"><input type="checkbox" id="st-always"' + (st.auto.always_while_worn ? ' checked' : '') + '> Listen whenever I\'m wearing it, even outside my windows</label>' +
      '<label class="row-check"><input type="checkbox" id="st-cal"' + (st.auto.calendar.enabled ? ' checked' : '') + '> Record calendar meetings</label>' +
      '<div class="field-row indent"><label for="st-lead">Start</label><input type="number" id="st-lead" min="0" max="120" value="' + esc(st.auto.calendar.lead_min) + '"><label for="st-lead">minutes before the meeting</label></div>' +
      '<div id="sc-cal-fields">' + calFieldsHtml(st) + '</div>') +
    sRow('Weekly time windows', 'Optional. Leave every day empty to listen all day. Add windows to limit auto mode to those hours.',
      '<div class="btn-row" style="margin:0 0 6px"><button type="button" class="btn small" id="st-copy-mon">Copy Monday to weekdays</button></div>' +
      '<div id="st-windows">' + windowsHtml(st) + '</div>') +
    sRow('Silence and stitching', 'How long a quiet stretch ends a recording, and when two recordings close together are joined into one conversation.',
      '<div class="field-row"><label for="st-auto-end">End an auto recording after</label><input type="number" id="st-auto-end" min="1" max="30" value="' + esc(st.silence.auto_end_min) + '"><label for="st-auto-end">min of silence</label></div>' +
      '<div class="field-row"><label for="st-manual-stop">Stop a button recording after</label><input type="number" id="st-manual-stop" min="0" max="120" value="' + esc(st.silence.manual_stop_min) + '"><label for="st-manual-stop">min of silence (0 = never)</label></div>' +
      '<div class="field-row"><label for="st-gap">Join recordings less than</label><input type="number" id="st-gap" min="0" max="60" value="' + esc(st.merge.gap_min) + '"><label for="st-gap">min apart (0 = off)</label></div>') +
    sRow('Uploads', 'The watch uploads over Wi-Fi or through your phone. Battery saver waits for Wi-Fi.',
      '<label class="row-check"><input type="checkbox" id="st-batt"' + (st.upload.battery_saver ? ' checked' : '') + '> Battery saver: upload only on Wi-Fi or while charging</label>') +

    '<div class="s-group">Voices</div>' +
    sRow('Speaker labels', 'Telling speakers apart uses a free model from Hugging Face, which needs a token. It is saved in the .env file on this PC and never shown again.',
      '<div id="sc-hf-fields">' + hfFieldsHtml(st) + '</div>') +
    sRow('Enroll a voice', 'Read the paragraph out loud for about 30 seconds, or upload a clip. New recordings then label that person by name.',
      '<div class="field-row"><label for="en-name">Name</label><input type="text" id="en-name" value="" placeholder="Your name" style="max-width:240px"></div>' +
      '<label class="row-check"><input type="checkbox" id="en-self" checked> This is me</label>' +
      '<p class="prompt-text">' + esc(ENROLL_PROMPT) + '</p>' +
      '<div class="btn-row" style="margin-top:0">' +
        '<button class="btn primary" type="button" id="en-rec">Record</button>' +
        '<button class="btn" type="button" id="en-stop" disabled hidden>Stop</button>' +
        '<span class="timer" id="en-timer" aria-live="off"></span>' +
        '<span class="or">or</span>' +
        '<label class="btn file-btn">Upload a clip<input type="file" id="en-file" accept="audio/*"></label>' +
        '<button class="btn" type="button" id="en-upload" hidden>Enroll this clip</button>' +
      '</div>' +
      '<div class="card-meta" id="en-status"></div>') +
    sRow('Known speakers', 'Everyone the pipeline recognizes. Rename or delete a voiceprint here.',
      '<div id="spk-list">' + speakersListHtml(spkList, editingId) + '</div>') +

    '<div class="s-group">Summaries</div>' +
    sRow('AI provider', 'Which AI writes the conversation summaries and the daily digest. Transcription always runs on this PC.',
      '<div id="sm-fields">' + summaryFieldsHtml(st) + '</div>') +
    sRow('When to summarize', 'Summaries are light on a cloud provider. With a local model you may want them batched, so the model only loads once a day.',
      '<div id="sm-when-fields">' + whenFieldsHtml(st) + '</div>') +

    '<div class="s-group">System</div>' +
    sRow('Pipeline status', 'Transcription runs on this PC. Summaries use the provider chosen above.',
      '<div id="health-block">' + healthBlockHtml(health) + '</div>') +
    sRow('Busy PC', 'Useful if you game, edit video or render on this PC. Recordings wait in the queue and are processed when the PC calms down.',
      '<label class="row-check"><input type="checkbox" id="st-busy"' + (st.system.pause_when_busy ? ' checked' : '') + '> Wait while the PC is busy (heavy GPU or CPU use by other programs)</label>') +
    sRow('Test upload', 'Send any audio file through the full pipeline, as if the watch had recorded it.',
      '<div class="btn-row" style="margin-top:0"><label class="btn file-btn">Choose audio file<input type="file" id="up-file" accept="audio/*"></label>' +
      '<button class="btn primary" type="button" id="up-btn" hidden>Upload</button></div>' +
      '<div class="card-meta" id="up-status"></div>') +
    '</div>'
  );

  const fileName = (input, btn, label) => input.addEventListener('change', () => {
    const f = input.files && input.files[0];
    btn.hidden = !f;
    if (f) btn.textContent = label + ' (' + (f.name.length > 28 ? f.name.slice(0, 25) + '...' : f.name) + ')';
  });
  fileName($('#en-file'), $('#en-upload'), 'Enroll');
  fileName($('#up-file'), $('#up-btn'), 'Upload');

  /* ---- summaries ---- */
  const smHost = $('#sm-fields');
  const readSummary = () => {
    st.summary.model = ($('#sm-model') || {}).value ? $('#sm-model').value.trim() : '';
    if ($('#sm-url')) st.summary.base_url = $('#sm-url').value.trim();
  };
  smHost.addEventListener('change', e => {
    if (e.target.id === 'sm-provider') {
      st.summary.provider = e.target.value;
      st.summary.model = '';
      smHost.innerHTML = summaryFieldsHtml(st);
    } else if (e.target.id === 'sm-preset' && e.target.value) {
      st.summary.base_url = e.target.value;
      $('#sm-url').value = e.target.value;
    }
  });
  const saveSettings = async () => {
    readSummary();
    const out = JSON.parse(JSON.stringify(st));
    DAYS.forEach(([k]) => { out.auto.windows[k] = out.auto.windows[k].filter(w => w[0] && w[1]); });
    delete out.summary.api_key_set;
    delete out.secrets;
    const key = $('#sm-key') ? $('#sm-key').value.trim() : '';
    if (key) out.summary.api_key = key;
    const res = await api('api/settings', { method: 'PUT', json: out });
    if (res && typeof res === 'object' && res.auto) Object.assign(st, normalizeSettings(res));
    redrawWindows();
    const status = $('#sm-status') ? $('#sm-status').textContent : '';
    smHost.innerHTML = summaryFieldsHtml(st);
    $('#sm-status').textContent = status;
  };
  const whenHost = $('#sm-when-fields');
  whenHost.addEventListener('change', e => {
    if (e.target.id === 'sm-when') { st.summary.when = e.target.value; whenHost.innerHTML = whenFieldsHtml(st); }
    else if (e.target.id === 'sm-time' && e.target.value) st.summary.daily_time = e.target.value;
  });
  whenHost.addEventListener('click', async e => {
    if (!e.target.closest('#sm-run')) return;
    try {
      await saveSettings();
      const r = await api('api/summary/run', { method: 'POST' });
      if (token !== state.nav) return;
      $('#sm-run-status').textContent = r && r.pending
        ? 'Started. Summarizing ' + r.pending + ' conversation' + (r.pending === 1 ? '' : 's') + ', then the daily digest.'
        : 'Nothing is waiting for a summary. Any digest that needs a refresh will run.';
    } catch (err) { fail(err); }
  });
  $('#st-busy').addEventListener('change', e => { st.system.pause_when_busy = e.target.checked; });

  /* ---- secrets (written to .env on the PC) ---- */
  const secretBox = (hostId, render, inputId, field, clearField, savedMsg, clearedMsg) => {
    const host = $(hostId);
    const send = async (secrets, msg) => {
      const res = await api('api/settings', { method: 'PUT', json: { secrets } });
      if (token !== state.nav) return;
      if (res && res.secrets) st.secrets = normalizeSettings(res).secrets;
      host.innerHTML = render(st);
      host.querySelector('.card-meta').textContent = msg;
      refreshHealth().then(h => { if (token === state.nav && $('#health-block')) $('#health-block').innerHTML = healthBlockHtml(h); });
    };
    host.addEventListener('click', async e => {
      try {
        if (e.target.closest(inputId + '-save')) {
          const v = $(inputId).value.trim();
          if (!v) { host.querySelector('.card-meta').textContent = 'Paste it in the box first.'; return; }
          await send({ [field]: v }, savedMsg);
        } else if (e.target.closest(inputId + '-clear')) {
          await send({ [clearField]: true }, clearedMsg);
        }
      } catch (err) { fail(err); }
    });
  };
  secretBox('#sc-hf-fields', hfFieldsHtml, '#sc-hf', 'hf_token', 'clear_hf_token',
    'Saved. Speaker labels are on for new recordings.', 'Removed. Speaker labels are off.');
  secretBox('#sc-cal-fields', calFieldsHtml, '#sc-cal', 'calendar_url', 'clear_calendar_url',
    'Saved. Tick "Record calendar meetings" above and press Save changes to use it.', 'Removed.');

  smHost.addEventListener('click', async e => {
    if (!e.target.closest('#sm-test')) return;
    try {
      await saveSettings();
      $('#sm-status').textContent = 'Saved. Asking the model for a test summary...';
      const r = await api('api/summary/test', { method: 'POST' });
      if (token !== state.nav) return;
      $('#sm-status').textContent = r && r.ok
        ? 'Works. Answered in ' + r.seconds + ' s: "' + r.title + '"'
        : 'Did not work: ' + ((r && r.error) || 'unknown error');
      refreshHealth().then(h => { if (token === state.nav && $('#health-block')) $('#health-block').innerHTML = healthBlockHtml(h); });
    } catch (err) { fail(err); }
  });

  /* ---- auto mode ---- */
  $('#st-always').addEventListener('change', e => { st.auto.always_while_worn = e.target.checked; });
  $('#st-cal').addEventListener('change', e => { st.auto.calendar.enabled = e.target.checked; });
  $('#st-batt').addEventListener('change', e => { st.upload.battery_saver = e.target.checked; });
  $('#st-lead').addEventListener('input', e => { const n = parseInt(e.target.value, 10); st.auto.calendar.lead_min = Number.isFinite(n) ? n : 0; });

  $('#st-auto-end').addEventListener('input', e => { const n = parseInt(e.target.value, 10); st.silence.auto_end_min = Number.isFinite(n) ? n : 0; });
  $('#st-manual-stop').addEventListener('input', e => { const n = parseInt(e.target.value, 10); st.silence.manual_stop_min = Number.isFinite(n) ? n : 0; });
  $('#st-gap').addEventListener('input', e => { const n = parseInt(e.target.value, 10); st.merge.gap_min = Number.isFinite(n) ? n : 0; });

  const winHost = $('#st-windows');
  const redrawWindows = () => { winHost.innerHTML = windowsHtml(st); };
  winHost.addEventListener('change', e => {
    const t = e.target;
    if (t.matches('input[type="time"]')) st.auto.windows[t.dataset.day][+t.dataset.i][+t.dataset.k] = t.value;
  });
  winHost.addEventListener('click', e => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const day = b.dataset.day;
    if (b.dataset.act === 'add') {
      st.auto.windows[day].push(['09:00', '17:00']);
      redrawWindows();
    } else if (b.dataset.act === 'rm') {
      st.auto.windows[day].splice(+b.dataset.i, 1);
      redrawWindows();
    }
  });
  $('#st-copy-mon').addEventListener('click', () => {
    const mon = JSON.parse(JSON.stringify(st.auto.windows.mon));
    ['tue', 'wed', 'thu', 'fri'].forEach(k => { st.auto.windows[k] = JSON.parse(JSON.stringify(mon)); });
    redrawWindows();
    toast('Copied Monday to Tue-Fri (not saved yet)');
  });
  $('#st-save').addEventListener('click', async () => {
    try {
      await saveSettings();
      toast('Saved');
    } catch (e) { fail(e); }
  });

  /* ---- speakers list ---- */
  const listHost = $('#spk-list');
  const redrawSpk = () => { listHost.innerHTML = speakersListHtml(spkList, editingId); wireSpkConfirm(); };
  const reloadSpk = async () => {
    try {
      spkList = await api('api/speakers');
      if (token !== state.nav) return;
      redrawSpk();
    } catch (e) { fail(e); }
  };
  function wireSpkConfirm() {
    listHost.querySelectorAll('button[data-act="delete"]').forEach(btn => {
      armConfirm(btn, 'Tap again to delete', async () => {
        try {
          await api('api/speakers/' + encodeURIComponent(btn.dataset.id), { method: 'DELETE' });
          toast('Deleted');
          reloadSpk();
        } catch (e) { fail(e); }
      });
    });
  }
  wireSpkConfirm();
  listHost.addEventListener('click', async e => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'rename') {
      editingId = +b.dataset.id;
      redrawSpk();
      const inp = listHost.querySelector('input[data-rename-input]');
      if (inp) inp.focus();
    } else if (act === 'rename-cancel') {
      editingId = null;
      redrawSpk();
    } else if (act === 'rename-save') {
      const inp = listHost.querySelector('input[data-rename-input]');
      const name = inp ? inp.value.trim() : '';
      if (!name) { toast('Name cannot be empty'); return; }
      try {
        await api('api/speakers/' + encodeURIComponent(b.dataset.id), { method: 'PATCH', json: { name } });
        editingId = null;
        toast('Saved');
        reloadSpk();
      } catch (err) { fail(err); }
    }
  });

  /* ---- enroll ---- */
  const enStatus = $('#en-status');
  async function uploadEnroll(blob, filename) {
    const name = $('#en-name').value.trim();
    if (!name) { toast('Enter a name first'); return; }
    const fd = new FormData();
    fd.append('file', blob, filename);
    fd.append('name', name);
    fd.append('is_self', $('#en-self').checked ? '1' : '0');
    enStatus.textContent = 'Uploading…';
    try {
      await api('api/speakers/enroll', { method: 'POST', form: fd });
      enStatus.textContent = 'Processing voiceprint…';
      later(() => { if (token === state.nav) { reloadSpk(); enStatus.textContent = 'Speakers list refreshed.'; } }, 15000);
    } catch (e) { enStatus.textContent = ''; fail(e); }
  }

  const recBtn = $('#en-rec'), stopBtn = $('#en-stop'), timerEl = $('#en-timer');
  let recorder = null, stream = null, tick = null, discard = false;
  const releaseMic = () => {
    if (tick) { clearInterval(tick); tick = null; }
    if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
  };
  state.cleanups.push(() => {
    discard = true;
    try { if (recorder && recorder.state !== 'inactive') recorder.stop(); } catch (e) { /* ignore */ }
    releaseMic();
  });

  recBtn.addEventListener('click', async () => {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === 'undefined') {
      toast('Recording is not supported in this browser. Use the file upload.');
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) { toast('Microphone unavailable: ' + (e && e.message ? e.message : e)); return; }
    discard = false;
    const chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.addEventListener('dataavailable', ev => { if (ev.data && ev.data.size) chunks.push(ev.data); });
    recorder.addEventListener('stop', () => {
      const type = recorder.mimeType || 'audio/webm';
      releaseMic();
      recBtn.disabled = false;
      stopBtn.hidden = true;
      timerEl.textContent = '';
      if (discard) return;
      const blob = new Blob(chunks, { type });
      const ext = /ogg/.test(type) ? 'ogg' : /mp4|aac/.test(type) ? 'm4a' : /wav/.test(type) ? 'wav' : 'webm';
      uploadEnroll(blob, 'voice.' + ext);
    });
    recorder.start();
    recBtn.disabled = true;
    stopBtn.hidden = false;
    stopBtn.disabled = true;
    enStatus.textContent = 'Recording. Read the paragraph above out loud.';
    let elapsed = 0;
    timerEl.textContent = fmtClock(30);
    tick = setInterval(() => {
      elapsed += 1;
      timerEl.textContent = fmtClock(30 - elapsed);
      if (elapsed >= 10) stopBtn.disabled = false;
      if (elapsed >= 30) { try { recorder.stop(); } catch (e) { /* ignore */ } }
    }, 1000);
    state.timers.push(tick);
  });
  stopBtn.addEventListener('click', () => { try { if (recorder && recorder.state !== 'inactive') recorder.stop(); } catch (e) { /* ignore */ } });

  $('#en-upload').addEventListener('click', () => {
    const f = $('#en-file').files[0];
    if (!f) { toast('Choose an audio file first'); return; }
    uploadEnroll(f, f.name);
  });

  /* ---- test upload ---- */
  $('#up-btn').addEventListener('click', async () => {
    const f = $('#up-file').files[0];
    const out = $('#up-status');
    if (!f) { toast('Choose an audio file first'); return; }
    const fd = new FormData();
    fd.append('file', f, f.name);
    out.textContent = 'Uploading…';
    try {
      const res = await api('api/upload', { method: 'POST', form: fd });
      const id = res && res.recording_id;
      out.innerHTML = 'Uploaded, processing… recording id ' + esc(id) +
        (id != null ? ' &middot; <a href="#/c/' + encodeURIComponent(id) + '">Open conversation</a>' : '');
      toast('Uploaded');
    } catch (e) { out.textContent = ''; fail(e); }
  });
}

/* ================= boot ================= */
window.addEventListener('hashchange', route);
initTopbar();
route();
