/* ── STATE ─────────────────────────────────────────────── */
const state = {
  supabase: null,
  session: null,
  appLoaded: false,
  searchResults: [],
  currentPodcast: null,
  episodes: [],
  subscriptions: [],
  people: [],
  topics: [],
  deliveries: [],
  recommendations: [],
  trayOpen: false,
  settingsOpen: false,
  loadingTimer: null,
  // Context for the currently-open summary panel, used by the Share control.
  share: { episodeId: null, title: '', podcast: '', text: '' },
  pendingDeepLink: null,   // episode_id from a ?ep= share link, opened after sign-in
  lastAuthEmail: '',       // for the auth "resend" affordance
  panelReq: 0,             // monotonic token: ignore stale in-flight summary responses
  // How to (re)generate the currently-open summary at a chosen detail level.
  panelCtx: null,          // { regenerate(level): Promise, level, episodeUrl, audioUrl, episodeId }
};

const CADENCE_LABELS = { instant: 'Instant', daily: 'Daily digest', weekly: 'Weekly digest' };

/* ── INIT ──────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('searchInput').addEventListener('input', e => {
    const q = e.target.value.trim();
    if (q.length >= 2) debouncedSearch(q);
    else clearResults();
  });
  document.getElementById('epSearch').addEventListener('input', e => {
    debouncedEpSearch(e.target.value.trim());
  });
  // A ?ep=<id> share link opens that summary once the user is signed in.
  const epParam = new URLSearchParams(window.location.search).get('ep');
  if (epParam && /^\d+$/.test(epParam)) state.pendingDeepLink = parseInt(epParam, 10);
  // Close the profile / share dropdowns on any outside click.
  document.addEventListener('click', e => {
    if (!e.target.closest('#profileMenu')) closeProfileMenu();
    if (!e.target.closest('#shareMenu')) closeShareMenu();
  });
  // Esc closes whatever overlay is open (dropdowns → panel → trays), top-down.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const dd = document.getElementById('profileDropdown');
    const sd = document.getElementById('shareDropdown');
    if (dd && !dd.classList.contains('hidden')) { closeProfileMenu(); return; }
    if (sd && !sd.classList.contains('hidden')) { closeShareMenu(); return; }
    if (document.getElementById('summaryPanel').classList.contains('open')) { closePanel(); return; }
    if (state.trayOpen) { toggleSubsTray(); return; }
    if (state.settingsOpen) { toggleSettings(); return; }
  });
  // Mobile/browser Back dismisses an open summary panel instead of leaving the app.
  window.addEventListener('popstate', () => {
    if (document.getElementById('summaryPanel').classList.contains('open')) closePanel(true);
  });
  initAuth();
});

/* ── AUTH ──────────────────────────────────────────────── */
async function initAuth() {
  try {
    const cfg = await (await fetch('/api/config')).json();
    state.supabase = window.supabase.createClient(cfg.supabase_url, cfg.supabase_anon_key);
  } catch (e) {
    document.getElementById('authMsg').textContent = 'Could not load app config. Try refreshing.';
    document.getElementById('authMsg').className = 'auth-msg error';
    return;
  }
  state.supabase.auth.onAuthStateChange((_event, session) => applySession(session));
  const { data: { session } } = await state.supabase.auth.getSession();
  applySession(session);
}

function applySession(session) {
  state.session = session;
  if (session) {
    document.getElementById('publicView').classList.add('hidden');
    if (!state.appLoaded) {
      state.appLoaded = true;
      showApp(session.user);
    }
  } else {
    state.appLoaded = false;
    // A shared ?ep= link works signed-out: show the summary publicly with a sign-up CTA,
    // rather than dead-ending newcomers on the login wall.
    if (state.pendingDeepLink) {
      const epId = state.pendingDeepLink;
      state.pendingDeepLink = null;
      showPublicSummary(epId);
    } else {
      showLogin();
    }
  }
}

function showApp(user) {
  document.getElementById('authOverlay').classList.add('hidden');
  document.getElementById('publicView').classList.add('hidden');
  ['appHeader', 'appHero', 'appMain'].forEach(id => document.getElementById(id).classList.remove('hidden'));
  document.getElementById('accountEmail').textContent = user.email || '';
  const sEmail = document.getElementById('settingsEmail');
  if (sEmail) sEmail.textContent = user.email || '';
  setProfileAvatar(user.email || '');
  loadProfile();
  loadSubscriptions();
  loadHomeDiscovery();
  if (state.pendingDeepLink) {
    const epId = state.pendingDeepLink;
    state.pendingDeepLink = null;
    openSharedEpisode(epId);
  }
}

/* ── PROFILE MENU + AVATAR ─────────────────────────────── */
// Try the email's Gravatar (SHA-256 per Gravatar's current API); fall back to a
// colored initial if the user has no Gravatar. No PII leaves the page beyond the
// standard hashed-email Gravatar request.
async function setProfileAvatar(email) {
  const initialEl = document.getElementById('profileInitial');
  const imgEl = document.getElementById('profileAvatar');
  const letter = (email.trim()[0] || '?').toUpperCase();
  if (initialEl) initialEl.textContent = letter;
  if (!imgEl || !email || !window.crypto?.subtle) return;
  try {
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(email.trim().toLowerCase()));
    const hash = Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
    imgEl.onload = () => { imgEl.classList.remove('hidden'); if (initialEl) initialEl.classList.add('hidden'); };
    imgEl.onerror = () => { imgEl.classList.add('hidden'); };
    imgEl.src = `https://www.gravatar.com/avatar/${hash}?d=404&s=72`;
  } catch (_) { /* keep the initial */ }
}

function toggleProfileMenu(event) {
  if (event) event.stopPropagation();
  const dd = document.getElementById('profileDropdown');
  const open = dd.classList.toggle('hidden') === false;
  document.getElementById('profileBtn').setAttribute('aria-expanded', open ? 'true' : 'false');
}
function closeProfileMenu() {
  const dd = document.getElementById('profileDropdown');
  if (dd && !dd.classList.contains('hidden')) {
    dd.classList.add('hidden');
    document.getElementById('profileBtn').setAttribute('aria-expanded', 'false');
  }
}
// Run a menu action, then close the dropdown.
function profileGo(fn) { closeProfileMenu(); fn(); }

/* ── HOME ──────────────────────────────────────────────── */
// Clicking the logo returns to the default search/home view.
function goHome(event) {
  if (event) event.preventDefault();
  closeProfileMenu();
  const input = document.getElementById('searchInput');
  if (input) input.value = '';
  state.searchResults = [];
  clearResults();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function showLogin() {
  document.getElementById('authOverlay').classList.remove('hidden');
  document.getElementById('publicView').classList.add('hidden');
  ['appHeader', 'appHero', 'appMain'].forEach(id => document.getElementById(id).classList.add('hidden'));
}

async function sendMagicLink(event) {
  if (event) event.preventDefault();
  const email = document.getElementById('authEmail').value.trim();
  if (!email) return false;
  state.lastAuthEmail = email;
  await deliverMagicLink(email);
  return false;
}

// Shared by the form submit and the "Resend" link.
async function deliverMagicLink(email) {
  const btn = document.getElementById('authBtn');
  const msg = document.getElementById('authMsg');
  const resend = document.getElementById('authResend');
  btn.disabled = true; btn.textContent = 'Sending…'; msg.textContent = '';
  if (resend) resend.classList.add('hidden');
  try {
    const { error } = await state.supabase.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.origin + window.location.pathname },
    });
    if (error) throw error;
    msg.textContent = 'Check your email for the sign-in link — it expires in 1 hour.';
    msg.className = 'auth-msg ok';
    btn.textContent = 'Link sent';
    // Offer a resend after a short delay in case it didn't arrive.
    if (resend) setTimeout(() => resend.classList.remove('hidden'), 8000);
  } catch (e) {
    msg.textContent = e.message || 'Could not send the link.';
    msg.className = 'auth-msg error';
    btn.disabled = false; btn.textContent = 'Send sign-in link';
  }
}

async function resendMagicLink() {
  const email = state.lastAuthEmail || document.getElementById('authEmail').value.trim();
  if (!email) return;
  await deliverMagicLink(email);
}

async function signOut() {
  await state.supabase.auth.signOut();
  state.appLoaded = false;
  showLogin();
}

/* ── PROFILE (default cadence) ─────────────────────────── */
async function loadProfile() {
  try {
    const { profile } = await api('/api/me');
    if (profile && profile.default_cadence) {
      document.getElementById('defaultCadence').value = profile.default_cadence;
    }
    if (profile && profile.summary_detail) {
      document.getElementById('summaryDetail').value = profile.summary_detail;
    }
  } catch (_) {}
}

async function saveDefaultCadence() {
  const val = document.getElementById('defaultCadence').value;
  try {
    await api('/api/me', 'PATCH', { default_cadence: val });
    showToast('Default delivery updated', 'success');
  } catch (e) {
    showToast('Failed to update: ' + e.message, 'error');
  }
}

async function saveSummaryDetail() {
  const val = document.getElementById('summaryDetail').value;
  try {
    await api('/api/me', 'PATCH', { summary_detail: val });
    showToast('Summary detail updated', 'success');
  } catch (e) {
    showToast('Failed to update: ' + e.message, 'error');
  }
}

/* ── SETTINGS PANEL ─────────────────────────────────────── */
function toggleSettings() {
  state.settingsOpen = !state.settingsOpen;
  const panel = document.getElementById('settingsPanel');
  const backdrop = document.getElementById('settingsBackdrop');
  if (state.settingsOpen) {
    closeProfileMenu();
    panel.classList.add('open');
    backdrop.classList.remove('hidden');
    loadProfile();
  } else {
    panel.classList.remove('open');
    backdrop.classList.add('hidden');
  }
}

// From the Subscriptions tray's "Settings" link: close the tray, open Settings.
function openSettingsFromTray() {
  if (state.trayOpen) toggleSubsTray();
  if (!state.settingsOpen) toggleSettings();
}

async function exportData() {
  try {
    const data = await api('/api/me/export');
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'podcastai-my-data.json';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
    showToast('Your data is downloading', 'success');
  } catch (e) {
    showToast('Export failed: ' + e.message, 'error');
  }
}

async function deleteAccount() {
  const ok = window.confirm(
    'Delete your account and all your data permanently? This cannot be undone.'
  );
  if (!ok) return;
  try {
    await api('/api/me', 'DELETE');
    showToast('Your account has been deleted', 'success');
    await state.supabase.auth.signOut();
    state.appLoaded = false;
    showLogin();
  } catch (e) {
    showToast('Could not delete account: ' + e.message, 'error');
  }
}

/* ── DEBOUNCE ──────────────────────────────────────────── */
function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}
const debouncedSearch = debounce(performSearch, 320);
const debouncedEpSearch = debounce(q => loadEpisodes(q), 320);

/* ── SEARCH ────────────────────────────────────────────── */
async function performSearch(q) {
  showSpinner(true);
  try {
    const data = await api('/api/search?q=' + encodeURIComponent(q));
    state.searchResults = data.podcasts || [];
    renderResults(q);
  } catch (e) {
    showToast('Search failed — ' + e.message, 'error');
  } finally {
    showSpinner(false);
  }
}

function renderResults(query) {
  const section = document.getElementById('searchResults');
  const grid = document.getElementById('resultsGrid');
  const label = document.getElementById('resultsLabel');
  document.getElementById('emptyState').classList.add('hidden');
  document.getElementById('podcastDetail').classList.add('hidden');
  document.getElementById('inboxView').classList.add('hidden');
  document.getElementById('discoverView').classList.add('hidden');
  section.classList.remove('hidden');

  label.textContent = state.searchResults.length ? `${state.searchResults.length} results for "${query}"` : '';
  grid.innerHTML = '';
  if (!state.searchResults.length) {
    grid.innerHTML = '<p class="no-results">No podcasts found. Try a different search term.</p>';
    return;
  }
  state.searchResults.forEach(p => {
    const card = document.createElement('div');
    card.className = 'podcast-card';
    card.innerHTML = podcastCardHTML(p);
    card.addEventListener('click', () => selectPodcast(p));
    grid.appendChild(card);
  });
}

function clearResults() {
  document.getElementById('searchResults').classList.add('hidden');
  document.getElementById('podcastDetail').classList.add('hidden');
  document.getElementById('inboxView').classList.add('hidden');
  document.getElementById('discoverView').classList.add('hidden');
  document.getElementById('emptyState').classList.remove('hidden');
  state.currentPodcast = null;
  state.episodes = [];
  closePanel();
}

/* ── HOME DISCOVERY (popular shows + sample summary) ──────── */
function podcastCardHTML(p) {
  return `
    ${p.subscribed ? '<span class="card-sub-badge">Subscribed</span>' : ''}
    <img class="card-artwork" src="${esc(p.artwork)}" alt=""
         loading="lazy" onerror="this.classList.add('broken')" />
    <div class="card-body">
      <p class="card-pub">${esc(p.publisher)}</p>
      <h3 class="card-title">${esc(p.title)}</h3>
      <p class="card-eps">${p.episode_count ? p.episode_count + ' episodes' : ''}</p>
    </div>`;
}

function loadHomeDiscovery() {
  loadPopular();
  loadSample();
}

async function loadPopular() {
  const wrap = document.getElementById('homePopular');
  const grid = document.getElementById('popularGrid');
  if (!wrap || !grid) return;
  try {
    const data = await api('/api/popular');
    const shows = data.podcasts || [];
    if (!shows.length) { wrap.classList.add('hidden'); return; }
    grid.innerHTML = '';
    shows.forEach(p => {
      const card = document.createElement('div');
      card.className = 'podcast-card';
      card.innerHTML = podcastCardHTML(p);
      card.addEventListener('click', () => selectPodcast(p));
      grid.appendChild(card);
    });
    wrap.classList.remove('hidden');
  } catch (_) {
    wrap.classList.add('hidden');
  }
}

async function loadSample() {
  const el = document.getElementById('homeSample');
  if (!el) return;
  try {
    const data = await api('/api/sample-summary');
    const s = data.sample;
    if (!s || !s.summary) { el.classList.add('hidden'); return; }
    const preview = stripTags(s.summary).slice(0, 240);
    el.innerHTML = `
      <p class="results-label">See a sample summary</p>
      <div class="sample-card" id="sampleCard" role="button" tabindex="0">
        ${s.artwork_url ? `<img class="sample-art" src="${esc(s.artwork_url)}" alt="" onerror="this.style.display='none'"/>` : ''}
        <div class="sample-body">
          <p class="sample-podcast">${esc(s.podcast_title)}</p>
          <p class="sample-title">${esc(s.episode_title)}</p>
          <p class="sample-preview">${esc(preview)}…</p>
          <span class="sample-cta">Read the full summary →</span>
        </div>
      </div>`;
    const card = el.querySelector('#sampleCard');
    const open = () => openSharedEpisode(s.episode_id);
    card.addEventListener('click', open);
    card.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
    el.classList.remove('hidden');
  } catch (_) {
    el.classList.add('hidden');
  }
}

/* ── PUBLIC READER (signed-out shared ?ep= link) ─────────── */
async function showPublicSummary(episodeId) {
  document.getElementById('authOverlay').classList.add('hidden');
  ['appHeader', 'appHero', 'appMain'].forEach(id => document.getElementById(id).classList.add('hidden'));
  const view = document.getElementById('publicView');
  view.classList.remove('hidden');
  document.getElementById('publicTitle').textContent = 'Loading summary…';
  document.getElementById('publicPodcast').textContent = '';
  document.getElementById('publicSource').textContent = '';
  document.getElementById('publicBody').innerHTML =
    '<div class="panel-loading"><div class="spinner large"></div><p>Loading summary…</p></div>';
  try {
    const data = await api(`/api/public/episodes/${episodeId}/summary`);
    document.getElementById('publicTitle').textContent = data.episode_title || 'Summary';
    document.getElementById('publicPodcast').textContent = data.podcast_title || '';
    document.getElementById('publicBody').innerHTML =
      window.marked ? marked.parse(data.summary || '') : (data.summary || '');
    document.getElementById('publicSource').textContent =
      data.source ? `Transcript source: ${sourceLabel(data.source)}` : '';
  } catch (e) {
    document.getElementById('publicTitle').textContent = 'Summary not available';
    document.getElementById('publicBody').innerHTML =
      '<p class="no-results">This summary isn’t available yet. Sign in to generate it yourself.</p>';
  }
}

function showLoginFromPublic() {
  document.getElementById('publicView').classList.add('hidden');
  showLogin();
}

/* ── PODCAST DETAIL ────────────────────────────────────── */
function selectPodcast(podcast) {
  state.currentPodcast = podcast;
  document.getElementById('searchResults').classList.add('hidden');
  document.getElementById('inboxView').classList.add('hidden');
  document.getElementById('discoverView').classList.add('hidden');
  document.getElementById('podcastDetail').classList.remove('hidden');

  const artwork = podcast.artwork || '';
  document.getElementById('detailArtwork').src = artwork;
  document.getElementById('detailPublisher').textContent = podcast.publisher || '';
  document.getElementById('detailTitle').textContent = podcast.title || '';
  document.getElementById('detailDesc').textContent = podcast.description || '';
  const bg = document.getElementById('detailHeroBg');
  if (artwork) bg.style.backgroundImage = `url(${artwork})`;
  document.getElementById('epSearch').value = '';

  updateSubscribeBtn(isSubscribed(podcast));
  loadEpisodes('');
}

async function loadEpisodes(q) {
  const p = state.currentPodcast;
  if (!p) return;
  document.getElementById('episodeList').innerHTML = skeletons(5);
  const params = new URLSearchParams();
  if (p.pi_feed_id) params.set('pi_feed_id', p.pi_feed_id);
  if (p.rss_url) params.set('rss_url', p.rss_url);
  if (q) params.set('q', q);
  try {
    const data = await api('/api/podcast-episodes?' + params.toString());
    state.episodes = data.episodes || [];
    renderEpisodes();
  } catch (e) {
    document.getElementById('episodeList').innerHTML =
      `<p class="no-results" style="color:var(--danger)">${esc(e.message)}</p>`;
  }
}

function closeDetail() {
  document.getElementById('podcastDetail').classList.add('hidden');
  document.getElementById('searchResults').classList.remove('hidden');
  state.currentPodcast = null;
  state.episodes = [];
  closePanel();
}

/* ── EPISODES ──────────────────────────────────────────── */
function renderEpisodes() {
  const list = document.getElementById('episodeList');
  list.innerHTML = '';
  if (!state.episodes.length) {
    list.innerHTML = '<p class="no-results">No episodes found.</p>';
    return;
  }
  state.episodes.forEach((ep, i) => {
    const row = document.createElement('div');
    row.className = 'episode-row';
    const date = ep.published_at
      ? new Date(ep.published_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
      : '';
    const meta = [date, formatDuration(ep.duration_seconds)].filter(Boolean).join(' · ');
    row.innerHTML = `
      <span class="ep-num">${String(i + 1).padStart(2, '0')}</span>
      <div class="ep-body">
        <p class="ep-title">${esc(ep.title)}</p>
        ${meta ? `<p class="ep-meta">${esc(meta)}</p>` : ''}
      </div>
      <div class="ep-action" id="ep-action-${i}">
        <button class="${ep.has_summary ? 'btn-view' : 'btn-summarize'}" onclick="openEpisodeSummary(${i}, this)">
          ${ep.has_summary ? 'View Summary' : 'Summarize'}
        </button>
      </div>`;
    list.appendChild(row);
  });
}

/* ── SUMMARIZE / VIEW (one endpoint; cached returns instantly) ── */
async function openEpisodeSummary(index, btn) {
  const ep = state.episodes[index];
  const p = state.currentPodcast;
  openPanel(ep.title, p.title, ep.description);
  const reqId = ++state.panelReq;
  btn.disabled = true;
  btn.textContent = ep.has_summary ? 'Loading…' : 'Summarizing…';

  const basePayload = {
    rss_url: p.rss_url,
    pi_feed_id: p.pi_feed_id || '',
    podcast_title: p.title,
    artwork_url: p.artwork || '',
    publisher: p.publisher || '',
    categories: p.categories || [],
    episode_guid: ep.guid,
    episode_title: ep.title,
    episode_description: ep.description || '',
    episode_audio_url: ep.audio_url || '',
    episode_url: ep.episode_url || '',
    episode_published_at: ep.published_at || null,
    episode_duration_seconds: ep.duration_seconds || null,
    episode_transcript_url: ep.transcript_url || '',
    episode_transcript_type: ep.transcript_type || '',
  };
  state.panelCtx = {
    level: null,   // set on first response (server picks the user's preference)
    episodeUrl: ep.episode_url || '',
    audioUrl: ep.audio_url || '',
    episodeId: ep.episode_id || null,
    regenerate: (level) => api('/api/summarize', 'POST', { ...basePayload, detail_level: level }),
  };

  try {
    const data = await api('/api/summarize', 'POST', basePayload);
    if (reqId !== state.panelReq) return;   // panel was closed / another episode opened
    state.episodes[index] = { ...ep, has_summary: true, episode_id: data.episode_id };
    state.share.episodeId = data.episode_id;
    state.panelCtx.episodeId = data.episode_id;
    state.panelCtx.level = data.detail_level || 'standard';
    const actionEl = document.getElementById(`ep-action-${index}`);
    if (actionEl) actionEl.innerHTML =
      `<button class="btn-view" onclick="openEpisodeSummary(${index}, this)">View Summary</button>`;
    showSummaryInPanel(data.summary, data.source);
  } catch (e) {
    if (reqId !== state.panelReq) return;
    btn.disabled = false;
    btn.textContent = ep.has_summary ? 'View Summary' : 'Summarize';
    closePanel();
    showToast(e.message || 'Failed to generate summary', 'error');
  }
}

/* ── SUMMARY PANEL ─────────────────────────────────────── */
const LOADING_MSGS = [
  'Finding the transcript…',
  'Reading through the episode…',
  'Pulling out the key insights…',
  'Writing your detailed brief…',
];

function openPanel(epTitle, podcastName, descriptionHtml) {
  state.share = { episodeId: null, title: epTitle, podcast: podcastName, text: '' };
  state.panelCtx = null;
  closeShareMenu();
  document.getElementById('panelDetailBar').classList.add('hidden');
  // History entry so mobile/browser Back closes the panel (popstate handler).
  if (!document.getElementById('summaryPanel').classList.contains('open')) {
    try { history.pushState({ panel: true }, ''); } catch (_) {}
  }
  document.getElementById('panelTitle').textContent = epTitle;
  document.getElementById('panelPodcast').textContent = podcastName;
  const preview = descriptionHtml ? stripTags(descriptionHtml).slice(0, 480) : '';
  document.getElementById('panelBody').innerHTML = `
    <div class="panel-loading">
      <div class="spinner large"></div>
      <p id="loadingMsg">${LOADING_MSGS[0]}</p>
      <p class="loading-hint">Detailed summaries of long episodes can take a minute or two — feel free to keep browsing.</p>
      ${preview ? `<div class="loading-preview">
        <p class="loading-preview-label">While you wait — about this episode</p>
        <p>${esc(preview)}…</p></div>` : ''}
    </div>`;
  let i = 0;
  clearInterval(state.loadingTimer);
  state.loadingTimer = setInterval(() => {
    i = (i + 1) % LOADING_MSGS.length;
    const el = document.getElementById('loadingMsg');
    if (el) el.textContent = LOADING_MSGS[i];
  }, 3500);
  document.getElementById('summaryPanel').classList.add('open');
  document.getElementById('panelBackdrop').classList.remove('hidden');
}

function showSummaryInPanel(summary, source) {
  clearInterval(state.loadingTimer);
  state.share.text = summary || '';
  const html = window.marked ? marked.parse(summary || '') : (summary || '');
  const ctx = state.panelCtx || {};
  const listenUrl = ctx.episodeUrl || ctx.audioUrl || '';
  const listen = listenUrl
    ? `<a class="listen-link" href="${esc(listenUrl)}" target="_blank" rel="noopener">
         <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
           <path d="M3 8a5 5 0 0 1 10 0M5.5 8a2.5 2.5 0 0 1 5 0M8 8v4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
         </svg>
         Listen to this episode</a>`
    : '';
  document.getElementById('panelBody').innerHTML = `
    <div class="summary-content">${html}</div>
    <div class="panel-footer">
      ${listen}
      <p class="ai-disclaimer">AI-generated summary — may contain errors. Open the episode to verify anything important.</p>
      <p class="source-tag">Transcript source: ${esc(sourceLabel(source))}</p>
    </div>`;
  // Show + sync the detail toggle only when we know how to regenerate.
  const bar = document.getElementById('panelDetailBar');
  if (ctx.regenerate) {
    bar.classList.remove('hidden');
    syncPanelDetailToggle(ctx.level || 'standard');
  } else {
    bar.classList.add('hidden');
  }
}

function syncPanelDetailToggle(level) {
  document.querySelectorAll('#panelDetailToggle .seg-opt').forEach(b => {
    const on = b.dataset.level === level;
    b.classList.toggle('seg-opt-on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

// Re-render the current summary at a different detail level (Quick/Standard/Deep).
async function setPanelDetail(level) {
  const ctx = state.panelCtx;
  if (!ctx || !ctx.regenerate || ctx.level === level) return;
  syncPanelDetailToggle(level);
  const reqId = ++state.panelReq;
  showPanelLoading('');
  try {
    const data = await ctx.regenerate(level);
    if (reqId !== state.panelReq) return;   // a newer request superseded this one
    ctx.level = level;
    if (data.episode_id) { state.share.episodeId = data.episode_id; ctx.episodeId = data.episode_id; }
    showSummaryInPanel(data.summary, data.source);
  } catch (e) {
    if (reqId !== state.panelReq) return;
    showToast(e.message || 'Could not change detail level', 'error');
    syncPanelDetailToggle(ctx.level || 'standard');
  }
}

// Render just the loading state in an already-open panel (used by the detail toggle).
function showPanelLoading() {
  let i = 0;
  document.getElementById('panelBody').innerHTML = `
    <div class="panel-loading">
      <div class="spinner large"></div>
      <p id="loadingMsg">${LOADING_MSGS[0]}</p>
      <p class="loading-hint">Re-generating at the new detail level — this can take a minute for long episodes.</p>
    </div>`;
  clearInterval(state.loadingTimer);
  state.loadingTimer = setInterval(() => {
    i = (i + 1) % LOADING_MSGS.length;
    const el = document.getElementById('loadingMsg');
    if (el) el.textContent = LOADING_MSGS[i];
  }, 3500);
}

/* ── SHARE ─────────────────────────────────────────────── */
function toggleShareMenu(event) {
  if (event) event.stopPropagation();
  const dd = document.getElementById('shareDropdown');
  const open = dd.classList.toggle('hidden') === false;
  document.getElementById('shareBtn').setAttribute('aria-expanded', open ? 'true' : 'false');
}
function closeShareMenu() {
  const dd = document.getElementById('shareDropdown');
  if (dd && !dd.classList.contains('hidden')) {
    dd.classList.add('hidden');
    document.getElementById('shareBtn').setAttribute('aria-expanded', 'false');
  }
}

// A shareable link only works once the episode has a cached summary (episodeId set).
function shareLink() {
  if (!state.share.episodeId) return null;
  return `${window.location.origin}${window.location.pathname}?ep=${state.share.episodeId}`;
}

async function shareCopyLink() {
  closeShareMenu();
  const link = shareLink();
  if (!link) { showToast('This summary isn’t ready to share yet — try reopening it.', 'error'); return; }
  try {
    await navigator.clipboard.writeText(link);
    showToast('Link copied to clipboard', 'success');
  } catch (_) {
    window.prompt('Copy this link:', link);
  }
}

function shareEmail() {
  closeShareMenu();
  const link = shareLink();
  const subject = `Podcast summary: ${state.share.title}`;
  const intro = `${state.share.title}${state.share.podcast ? ` — ${state.share.podcast}` : ''}`;
  const bodyParts = [intro, ''];
  if (link) bodyParts.push(`Read it on PodcastAI: ${link}`, '');
  if (state.share.text) bodyParts.push('— — —', state.share.text);
  bodyParts.push('', 'Summarized by PodcastAI');
  const body = bodyParts.join('\n');
  // mailto bodies are length-limited by clients; trim very long summaries.
  const url = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body.slice(0, 1800))}`;
  window.location.href = url;
}

// Open a summary from a ?ep= share link (uses the cached-summary endpoint).
async function openSharedEpisode(episodeId) {
  openPanel('Loading summary…', '');
  const reqId = ++state.panelReq;
  state.panelCtx = {
    level: null, episodeId, episodeUrl: '', audioUrl: '',
    regenerate: (level) => api(`/api/episodes/${episodeId}/summarize?detail_level=${level}`, 'POST'),
  };
  try {
    const data = await api(`/api/episodes/${episodeId}/summarize`, 'POST');
    if (reqId !== state.panelReq) return;
    document.getElementById('panelTitle').textContent = data.episode_title || 'Summary';
    document.getElementById('panelPodcast').textContent = data.podcast_title || '';
    state.share.episodeId = episodeId;
    state.share.title = data.episode_title || 'Summary';
    state.share.podcast = data.podcast_title || '';
    state.panelCtx.level = data.detail_level || 'standard';
    state.panelCtx.episodeUrl = data.episode_url || '';
    state.panelCtx.audioUrl = data.audio_url || '';
    showSummaryInPanel(data.summary, data.source);
  } catch (e) {
    if (reqId !== state.panelReq) return;
    closePanel();
    showToast(e.message || 'Could not open that summary.', 'error');
  }
}

function closePanel(fromPop = false) {
  clearInterval(state.loadingTimer);
  state.panelReq++;   // invalidate any in-flight summary/detail request
  const wasOpen = document.getElementById('summaryPanel').classList.contains('open');
  document.getElementById('summaryPanel').classList.remove('open');
  document.getElementById('panelBackdrop').classList.add('hidden');
  document.getElementById('panelDetailBar').classList.add('hidden');
  state.panelCtx = null;
  // Closing via the UI consumes the history entry openPanel pushed.
  if (!fromPop && wasOpen && history.state && history.state.panel) {
    try { history.back(); } catch (_) {}
  }
}

/* ── SUBSCRIBE ─────────────────────────────────────────── */
function isSubscribed(p) {
  return state.subscriptions.some(s =>
    (p.pi_feed_id && s.pi_feed_id === p.pi_feed_id) || (p.rss_url && s.rss_url === p.rss_url)
  );
}
function subPodcastId(p) {
  const s = state.subscriptions.find(s =>
    (p.pi_feed_id && s.pi_feed_id === p.pi_feed_id) || (p.rss_url && s.rss_url === p.rss_url)
  );
  return s ? s.podcast_id : null;
}

async function toggleSubscribe() {
  const p = state.currentPodcast;
  const btn = document.getElementById('subscribeBtn');
  if (isSubscribed(p)) {
    const pid = subPodcastId(p);
    if (!pid) return;
    btn.disabled = true;
    try {
      await api(`/api/subscriptions?podcast_id=${pid}`, 'DELETE');
      await loadSubscriptions();
      updateSubscribeBtn(false);
      showToast('Unsubscribed', 'success');
    } catch (e) {
      showToast('Failed to unsubscribe: ' + e.message, 'error');
      btn.disabled = false;
    }
  } else {
    btn.disabled = true;
    btn.textContent = 'Subscribing…';
    try {
      await api('/api/subscribe', 'POST', {
        rss_url: p.rss_url,
        pi_feed_id: p.pi_feed_id || '',
        podcast_title: p.title,
        artwork_url: p.artwork || '',
        publisher: p.publisher || '',
        categories: p.categories || [],
      });
      await loadSubscriptions();
      updateSubscribeBtn(true);
      showToast(`Subscribed — summarizing the latest episode for My Summaries…`, 'success');
      const pid = subPodcastId(p);
      if (pid) seedLatestForPodcast(pid);   // background: don't block the button
    } catch (e) {
      showToast('Failed to subscribe: ' + e.message, 'error');
      updateSubscribeBtn(false);
    }
  }
}

// After subscribing, summarize the show's latest episode so My Summaries isn't empty
// until a brand-new episode airs. Runs in the background; surfaces a toast on success.
async function seedLatestForPodcast(podcastId) {
  try {
    const data = await api(`/api/subscriptions/${podcastId}/seed-latest`, 'POST');
    if (data && data.seeded) {
      showToast('Latest episode summarized — see My Summaries', 'success');
      // Refresh the inbox if the user is looking at it.
      if (!document.getElementById('inboxView').classList.contains('hidden')) showInbox();
    }
  } catch (_) {
    // Non-critical: the next scan will still deliver new episodes.
  }
}

function updateSubscribeBtn(subscribed) {
  const btn = document.getElementById('subscribeBtn');
  btn.disabled = false;
  if (subscribed) {
    btn.textContent = '✓ Subscribed';
    btn.classList.add('subscribed');
  } else {
    btn.textContent = 'Subscribe to new episodes';
    btn.classList.remove('subscribed');
  }
}

/* ── SUBSCRIPTIONS TRAY ────────────────────────────────── */
function toggleSubsTray() {
  state.trayOpen = !state.trayOpen;
  const tray = document.getElementById('subsTray');
  const backdrop = document.getElementById('trayBackdrop');
  if (state.trayOpen) {
    tray.classList.add('open');
    backdrop.classList.remove('hidden');
    loadSubscriptions();
    loadStatus();
  } else {
    tray.classList.remove('open');
    backdrop.classList.add('hidden');
  }
}

async function loadSubscriptions() {
  try {
    const data = await api('/api/subscriptions');
    state.subscriptions = data.subscriptions || [];
    document.getElementById('subCount').textContent = state.subscriptions.length;
    renderSubscriptions();
    if (state.currentPodcast) updateSubscribeBtn(isSubscribed(state.currentPodcast));
  } catch (_) {}
}

function renderSubscriptions() {
  const list = document.getElementById('subsList');
  if (!state.subscriptions.length) {
    list.innerHTML = '<p class="no-subs">No subscriptions yet. Search for a podcast and hit Subscribe.</p>';
    return;
  }
  list.innerHTML = '';
  state.subscriptions.forEach(s => {
    const item = document.createElement('div');
    item.className = 'sub-item-wrap';
    const override = s.cadence_override || '';
    const opts = ['', 'instant', 'daily', 'weekly'].map(v =>
      `<option value="${v}" ${v === override ? 'selected' : ''}>${v ? CADENCE_LABELS[v] : 'Use default'}</option>`
    ).join('');
    item.innerHTML = `
      <div class="sub-item">
        ${s.artwork_url ? `<img class="sub-artwork" src="${esc(s.artwork_url)}" alt="" />`
                        : '<div class="sub-artwork-placeholder"></div>'}
        <div class="sub-info">
          <p class="sub-title">${esc(s.title || 'Untitled')}</p>
          <p class="sub-meta">${esc(s.publisher || '')}</p>
          <select class="settings-select" onchange="saveCadence(${s.podcast_id}, this.value)">${opts}</select>
        </div>
        <button class="btn-unsub" onclick="unsubscribeSub(${s.podcast_id}, this)">Unsubscribe</button>
      </div>`;
    list.appendChild(item);
  });
}

async function saveCadence(podcastId, value) {
  try {
    await api(`/api/subscriptions/${podcastId}`, 'PATCH', { cadence_override: value || null });
    const s = state.subscriptions.find(s => s.podcast_id === podcastId);
    if (s) s.cadence_override = value || null;
    showToast('Delivery updated', 'success');
  } catch (e) {
    showToast('Failed to update: ' + e.message, 'error');
  }
}

async function unsubscribeSub(podcastId, btn) {
  btn.disabled = true;
  try {
    await api(`/api/subscriptions?podcast_id=${podcastId}`, 'DELETE');
    await loadSubscriptions();
    showToast('Unsubscribed', 'success');
  } catch (e) {
    showToast('Failed to unsubscribe: ' + e.message, 'error');
    btn.disabled = false;
  }
}

/* ── INBOX (delivered summaries) ───────────────────────── */
async function showInbox() {
  document.getElementById('searchResults').classList.add('hidden');
  document.getElementById('podcastDetail').classList.add('hidden');
  document.getElementById('emptyState').classList.add('hidden');
  document.getElementById('discoverView').classList.add('hidden');
  document.getElementById('inboxView').classList.remove('hidden');
  document.getElementById('inboxList').innerHTML = skeletons(4);
  try {
    const data = await api('/api/deliveries');
    state.deliveries = data.deliveries || [];
    // Empty inbox but the user follows shows? Backfill the latest episode of any
    // subscription that never delivered one (self-heals subs made before seeding worked).
    if (!state.deliveries.length) {
      if (!state.subscriptions.length) { try { await loadSubscriptions(); } catch (_) {} }
      if (state.subscriptions.length) { await backfillInbox(); return; }
    }
    renderInbox();
  } catch (e) {
    document.getElementById('inboxList').innerHTML =
      `<p class="no-results">${esc(e.message)}</p>`;
  }
}

// Seed the latest episode for subscriptions that have nothing delivered yet, then
// reload. Shows a generating state because summarizing can take a minute.
async function backfillInbox() {
  document.getElementById('inboxList').innerHTML = `
    <div class="panel-loading">
      <div class="spinner large"></div>
      <p>Preparing your summaries…</p>
      <p class="loading-hint">Summarizing the latest episode from the shows you follow — this can take a minute.</p>
    </div>`;
  try { await api('/api/deliveries/backfill', 'POST'); } catch (_) {}
  try {
    const data = await api('/api/deliveries');
    state.deliveries = data.deliveries || [];
  } catch (_) {}
  renderInbox();
}

function renderInbox() {
  const list = document.getElementById('inboxList');
  list.innerHTML = '';
  if (!state.deliveries.length) {
    list.innerHTML = state.subscriptions.length
      ? '<p class="no-results">No summaries yet — the latest episodes of your shows may not have transcripts available, or are still processing. New episodes will appear here automatically as they air.</p>'
      : '<p class="no-results">No summaries yet. Subscribe to a show and its latest episode will be summarized here.</p>';
    return;
  }
  state.deliveries.forEach((d, i) => {
    const date = d.published_at
      ? new Date(d.published_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
      : '';
    const meta = [d.podcast_title, date].filter(Boolean).join(' · ');
    const el = document.createElement('div');
    el.className = 'inbox-item';
    el.innerHTML = `
      ${d.artwork_url ? `<img class="inbox-art" src="${esc(d.artwork_url)}" alt="" onerror="this.style.visibility='hidden'" />`
                      : '<div class="inbox-art"></div>'}
      <div class="inbox-body">
        <p class="inbox-ep">${esc(d.episode_title)}</p>
        <p class="inbox-meta">${esc(meta)}</p>
        ${reasonBadges(d.reasons)}
      </div>`;
    el.addEventListener('click', () => openDeliverySummary(i));
    list.appendChild(el);
  });
}

function openDeliverySummary(i) {
  const d = state.deliveries[i];
  openPanel(d.episode_title, d.podcast_title);
  state.share.episodeId = d.episode_id;
  state.panelCtx = {
    level: currentDetailPref(), episodeId: d.episode_id,
    episodeUrl: d.episode_url || '', audioUrl: d.audio_url || '',
    regenerate: (level) => api(`/api/episodes/${d.episode_id}/summarize?detail_level=${level}`, 'POST'),
  };
  showSummaryInPanel(d.summary_md || '_Summary not available yet._', d.transcript_source);
}

/* ── FOR YOU (discovery hub: follow people/topics + recommendations) ── */
async function showDiscover() {
  document.getElementById('searchResults').classList.add('hidden');
  document.getElementById('podcastDetail').classList.add('hidden');
  document.getElementById('emptyState').classList.add('hidden');
  document.getElementById('inboxView').classList.add('hidden');
  document.getElementById('discoverView').classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
  // The follow controls live here now. Load them first so the recommendations'
  // empty-state copy knows whether the user already follows anything.
  document.getElementById('discoverList').innerHTML = skeletons(4);
  await Promise.all([loadPeople(), loadTopics()]);
  loadRecommendations();
}

// From the Subscriptions tray pointer: close the tray, open For You.
function openDiscoverFromTray() {
  if (state.trayOpen) toggleSubsTray();
  showDiscover();
}

async function loadRecommendations() {
  document.getElementById('discoverList').innerHTML = skeletons(4);
  try {
    const data = await api('/api/recommendations');
    state.recommendations = data.recommendations || [];
    renderDiscover();
  } catch (e) {
    document.getElementById('discoverList').innerHTML = `<p class="no-results">${esc(e.message)}</p>`;
  }
}

// Refresh the recommendation list if For You is the visible view (after a follow/unfollow).
function refreshDiscoverRecs() {
  if (!document.getElementById('discoverView').classList.contains('hidden')) loadRecommendations();
}

function renderDiscover() {
  const list = document.getElementById('discoverList');
  list.innerHTML = '';
  if (!state.recommendations.length) {
    list.innerHTML = (state.people.length || state.topics.length)
      ? '<p class="no-results">Nothing yet — we\'ll add episodes here as new shows are scanned for the guests and topics you follow.</p>'
      : '<p class="no-results">Follow a guest or topic above and episodes featuring them will appear here.</p>';
    return;
  }
  state.recommendations.forEach((r, i) => {
    const date = r.published_at
      ? new Date(r.published_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
      : '';
    const meta = [r.podcast_title, date].filter(Boolean).join(' · ');
    const el = document.createElement('div');
    el.className = 'inbox-item';
    el.innerHTML = `
      ${r.artwork_url ? `<img class="inbox-art" src="${esc(r.artwork_url)}" alt="" onerror="this.style.visibility='hidden'" />`
                      : '<div class="inbox-art"></div>'}
      <div class="inbox-body">
        <p class="inbox-ep">${esc(r.episode_title)}</p>
        <p class="inbox-meta">${esc(meta)}</p>
        ${reasonBadges(r.reasons)}
      </div>`;
    el.addEventListener('click', () => openRecSummary(i));
    list.appendChild(el);
  });
}

// Best guess at the user's current detail preference, for the panel toggle highlight.
function currentDetailPref() {
  const sel = document.getElementById('summaryDetail');
  return (sel && sel.value) || 'standard';
}

async function openRecSummary(i) {
  const r = state.recommendations[i];
  openPanel(r.episode_title, r.podcast_title);
  const reqId = ++state.panelReq;
  state.share.episodeId = r.episode_id;
  state.panelCtx = {
    level: currentDetailPref(), episodeId: r.episode_id,
    episodeUrl: r.episode_url || '', audioUrl: r.audio_url || '',
    regenerate: (level) => api(`/api/episodes/${r.episode_id}/summarize?detail_level=${level}`, 'POST'),
  };
  try {
    const data = await api(`/api/episodes/${r.episode_id}/summarize`, 'POST');
    if (reqId !== state.panelReq) return;
    state.panelCtx.level = data.detail_level || state.panelCtx.level;
    state.panelCtx.episodeUrl = data.episode_url || state.panelCtx.episodeUrl;
    state.panelCtx.audioUrl = data.audio_url || state.panelCtx.audioUrl;
    showSummaryInPanel(data.summary, data.source);
  } catch (e) {
    if (reqId !== state.panelReq) return;
    closePanel();
    showToast(e.message || 'Failed to generate summary', 'error');
  }
}

/* ── SCAN STATUS BANNER ────────────────────────────────── */
async function loadStatus() {
  const el = document.getElementById('trayStatus');
  if (!el) return;
  try {
    const data = await api('/api/status');
    renderStatus(el, data.last_run);
  } catch (_) {
    el.textContent = '';
  }
}

function renderStatus(el, run) {
  if (!run) {
    el.textContent = 'No checks have run yet.';
    return;
  }
  const errs = Array.isArray(run.errors) ? run.errors : [];
  const when = run.started_at ? new Date(run.started_at).toLocaleString() : '';
  el.innerHTML = errs.length
    ? `<span class="status-error">Last check ${esc(when)} — ${errs.length} issue${errs.length === 1 ? '' : 's'}</span>`
    : `Last check ${esc(when)} · ${run.episodes_matched || 0} new delivered`;
}

/* ── PEOPLE TRACKING ───────────────────────────────────── */
async function loadPeople() {
  try {
    const data = await api('/api/people');
    state.people = data.people || [];
    renderPeople();
  } catch (_) {}
}

function renderPeople() {
  const list = document.getElementById('peopleList');
  if (!list) return;
  if (!state.people.length) {
    list.innerHTML = '<span class="chip-empty">No guests yet — add one above.</span>';
    return;
  }
  list.innerHTML = '';
  state.people.forEach(p => {
    const el = document.createElement('span');
    el.className = 'chip';
    el.innerHTML = `${esc(p.name)}
      <button class="chip-x" aria-label="Stop following ${esc(p.name)}" onclick="removePerson(${p.person_id})">×</button>`;
    list.appendChild(el);
  });
}

async function addPerson(event) {
  event.preventDefault();
  const input = document.getElementById('personInput');
  const name = input.value.trim();
  if (name.length < 2) return false;
  try {
    const res = await api('/api/people', 'POST', { name });
    input.value = '';
    await loadPeople();
    refreshDiscoverRecs();
    const n = (res && res.recent_matches) || 0;
    showToast(
      n > 0
        ? `Now following ${name} — ${n} recent episode${n === 1 ? '' : 's'} in For You`
        : `Now following ${name} — we'll alert you when they appear on your shows or popular podcasts`,
      'success'
    );
  } catch (e) {
    showToast('Failed to add: ' + e.message, 'error');
  }
  return false;
}

async function removePerson(personId) {
  try {
    await api(`/api/people?person_id=${personId}`, 'DELETE');
    await loadPeople();
    refreshDiscoverRecs();
  } catch (e) {
    showToast('Failed to remove: ' + e.message, 'error');
  }
}

/* ── TOPIC TRACKING ────────────────────────────────────── */
async function loadTopics() {
  try {
    const data = await api('/api/topics');
    state.topics = data.topics || [];
    renderTopics();
  } catch (_) {}
}

function renderTopics() {
  const list = document.getElementById('topicsList');
  if (!list) return;
  if (!state.topics.length) {
    list.innerHTML = '<span class="chip-empty">No topics yet — add one above.</span>';
    return;
  }
  list.innerHTML = '';
  state.topics.forEach(t => {
    const el = document.createElement('span');
    el.className = 'chip';
    el.innerHTML = `${esc(t.topic)}
      <button class="chip-x" aria-label="Stop following ${esc(t.topic)}" onclick="removeTopic(${t.id})">×</button>`;
    list.appendChild(el);
  });
}

async function addTopic(event) {
  event.preventDefault();
  const input = document.getElementById('topicInput');
  const topic = input.value.trim();
  if (topic.length < 2) return false;
  try {
    const res = await api('/api/topics', 'POST', { topic });
    input.value = '';
    await loadTopics();
    refreshDiscoverRecs();
    const n = (res && res.recent_matches) || 0;
    showToast(
      n > 0
        ? `Now following "${topic}" — ${n} recent episode${n === 1 ? '' : 's'} in For You`
        : `Now following "${topic}" — we'll alert you when an episode covers it`,
      'success'
    );
  } catch (e) {
    showToast('Failed to add: ' + e.message, 'error');
  }
  return false;
}

async function removeTopic(topicId) {
  try {
    await api(`/api/topics?topic_id=${topicId}`, 'DELETE');
    await loadTopics();
    refreshDiscoverRecs();
  } catch (e) {
    showToast('Failed to remove: ' + e.message, 'error');
  }
}

function reasonBadges(reasons) {
  if (!Array.isArray(reasons)) return '';
  const badges = reasons.map(r => {
    if (r.type === 'person') return `👤 ${esc(r.name || 'tracked person')}`;
    if (r.type === 'topic') return `🏷️ ${esc(r.topic || 'tracked topic')}`;
    return null;
  }).filter(Boolean);
  if (!badges.length) return '';
  return `<div class="inbox-reasons">${badges.map(b => `<span class="reason-badge">${b}</span>`).join('')}</div>`;
}

/* ── API HELPER ────────────────────────────────────────── */
async function api(path, method = 'GET', body = null) {
  const headers = {};
  const session = state.session || (state.supabase && (await state.supabase.auth.getSession()).data.session);
  if (session) headers['Authorization'] = 'Bearer ' + session.access_token;
  if (body) headers['Content-Type'] = 'application/json';
  const resp = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
  return data;
}

/* ── UTILITIES ─────────────────────────────────────────── */
function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

// Strip HTML tags (regex, so no <img>/script side effects) then decode entities
// via a textarea (which never executes content). Used for the loading preview.
function stripTags(html) {
  const noTags = String(html || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  const t = document.createElement('textarea');
  t.innerHTML = noTags;
  return t.value;
}

function formatDuration(secs) {
  if (!secs) return '';
  secs = parseInt(secs, 10);
  if (isNaN(secs) || secs <= 0) return '';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return m > 0 ? `${m}m` : '';
}

function sourceLabel(source) {
  const labels = {
    published: 'official transcript',
    colossus: 'Colossus transcript',
    deepgram: 'full audio transcript',
    youtube: 'YouTube captions',
    audio: 'audio transcript',
    audio_partial: 'partial audio transcript',
    shownotes: 'RSS show notes',
  };
  return labels[source] || 'unknown';
}

function showSpinner(show) {
  document.getElementById('searchSpinner').classList.toggle('hidden', !show);
}

function skeletons(n) {
  return Array.from({ length: n }, (_, i) => `
    <div class="episode-row skeleton">
      <span class="ep-num skeleton-block" style="width:24px;height:14px;border-radius:3px"></span>
      <div class="ep-body">
        <p class="skeleton-block" style="width:${60 + (i % 3) * 12}%;height:14px;border-radius:3px;margin-bottom:8px"></p>
        <p class="skeleton-block" style="width:30%;height:11px;border-radius:3px"></p>
      </div>
    </div>`).join('');
}

function showToast(msg, type = 'success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast ${type}`;
  t.classList.remove('hidden');
  requestAnimationFrame(() => requestAnimationFrame(() => t.classList.add('show')));
  clearTimeout(t._timer);
  t._timer = setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => t.classList.add('hidden'), 280);
  }, 3200);
}
