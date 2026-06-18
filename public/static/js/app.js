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
  trayOpen: false,
  loadingTimer: null,
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
    if (!state.appLoaded) {
      state.appLoaded = true;
      showApp(session.user);
    }
  } else {
    state.appLoaded = false;
    showLogin();
  }
}

function showApp(user) {
  document.getElementById('authOverlay').classList.add('hidden');
  ['appHeader', 'appHero', 'appMain'].forEach(id => document.getElementById(id).classList.remove('hidden'));
  document.getElementById('accountEmail').textContent = user.email || '';
  loadProfile();
  loadSubscriptions();
}

function showLogin() {
  document.getElementById('authOverlay').classList.remove('hidden');
  ['appHeader', 'appHero', 'appMain'].forEach(id => document.getElementById(id).classList.add('hidden'));
}

async function sendMagicLink(event) {
  event.preventDefault();
  const email = document.getElementById('authEmail').value.trim();
  const btn = document.getElementById('authBtn');
  const msg = document.getElementById('authMsg');
  if (!email) return false;
  btn.disabled = true; btn.textContent = 'Sending…'; msg.textContent = '';
  try {
    const { error } = await state.supabase.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.origin + window.location.pathname },
    });
    if (error) throw error;
    msg.textContent = 'Check your email for the sign-in link.';
    msg.className = 'auth-msg ok';
    btn.textContent = 'Link sent';
  } catch (e) {
    msg.textContent = e.message || 'Could not send the link.';
    msg.className = 'auth-msg error';
    btn.disabled = false; btn.textContent = 'Send magic link';
  }
  return false;
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
    card.innerHTML = `
      ${p.subscribed ? '<span class="card-sub-badge">Subscribed</span>' : ''}
      <img class="card-artwork" src="${esc(p.artwork)}" alt=""
           loading="lazy" onerror="this.classList.add('broken')" />
      <div class="card-body">
        <p class="card-pub">${esc(p.publisher)}</p>
        <h3 class="card-title">${esc(p.title)}</h3>
        <p class="card-eps">${p.episode_count ? p.episode_count + ' episodes' : ''}</p>
      </div>`;
    card.addEventListener('click', () => selectPodcast(p));
    grid.appendChild(card);
  });
}

function clearResults() {
  document.getElementById('searchResults').classList.add('hidden');
  document.getElementById('podcastDetail').classList.add('hidden');
  document.getElementById('inboxView').classList.add('hidden');
  document.getElementById('emptyState').classList.remove('hidden');
  state.currentPodcast = null;
  state.episodes = [];
  closePanel();
}

/* ── PODCAST DETAIL ────────────────────────────────────── */
function selectPodcast(podcast) {
  state.currentPodcast = podcast;
  document.getElementById('searchResults').classList.add('hidden');
  document.getElementById('inboxView').classList.add('hidden');
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
  btn.disabled = true;
  btn.textContent = ep.has_summary ? 'Loading…' : 'Summarizing…';
  try {
    const data = await api('/api/summarize', 'POST', {
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
    });
    state.episodes[index] = { ...ep, has_summary: true, episode_id: data.episode_id };
    const actionEl = document.getElementById(`ep-action-${index}`);
    if (actionEl) actionEl.innerHTML =
      `<button class="btn-view" onclick="openEpisodeSummary(${index}, this)">View Summary</button>`;
    showSummaryInPanel(data.summary, data.source);
  } catch (e) {
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
  const html = window.marked ? marked.parse(summary || '') : (summary || '');
  document.getElementById('panelBody').innerHTML = `
    <div class="summary-content">${html}</div>
    <p class="source-tag">Transcript source: ${esc(sourceLabel(source))}</p>`;
}

function closePanel() {
  clearInterval(state.loadingTimer);
  document.getElementById('summaryPanel').classList.remove('open');
  document.getElementById('panelBackdrop').classList.add('hidden');
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
      showToast(`Subscribed to ${p.title}`, 'success');
    } catch (e) {
      showToast('Failed to subscribe: ' + e.message, 'error');
      updateSubscribeBtn(false);
    }
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
    loadPeople();
    loadTopics();
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
  document.getElementById('inboxView').classList.remove('hidden');
  document.getElementById('inboxList').innerHTML = skeletons(4);
  try {
    const data = await api('/api/deliveries');
    state.deliveries = data.deliveries || [];
    renderInbox();
  } catch (e) {
    document.getElementById('inboxList').innerHTML =
      `<p class="no-results">${esc(e.message)}</p>`;
  }
}

function renderInbox() {
  const list = document.getElementById('inboxList');
  list.innerHTML = '';
  if (!state.deliveries.length) {
    list.innerHTML = '<p class="no-results">No summaries yet. Subscribe to a show and new episodes will land here.</p>';
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
  showSummaryInPanel(d.summary_md || '_Summary not available yet._', d.transcript_source);
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
    list.innerHTML = '<p class="no-subs">No one yet. Add a person above.</p>';
    return;
  }
  list.innerHTML = '';
  state.people.forEach(p => {
    const el = document.createElement('div');
    el.className = 'person-item';
    el.innerHTML = `<span class="person-name">${esc(p.name)}</span>
      <button class="person-remove" onclick="removePerson(${p.person_id})">Remove</button>`;
    list.appendChild(el);
  });
}

async function addPerson(event) {
  event.preventDefault();
  const input = document.getElementById('personInput');
  const name = input.value.trim();
  if (name.length < 2) return false;
  try {
    await api('/api/people', 'POST', { name });
    input.value = '';
    await loadPeople();
    showToast(`Now following ${name}`, 'success');
  } catch (e) {
    showToast('Failed to add: ' + e.message, 'error');
  }
  return false;
}

async function removePerson(personId) {
  try {
    await api(`/api/people?person_id=${personId}`, 'DELETE');
    await loadPeople();
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
    list.innerHTML = '<p class="no-subs">No topics yet. Add one above.</p>';
    return;
  }
  list.innerHTML = '';
  state.topics.forEach(t => {
    const el = document.createElement('div');
    el.className = 'person-item';
    el.innerHTML = `<span class="person-name">${esc(t.topic)}</span>
      <button class="person-remove" onclick="removeTopic(${t.id})">Remove</button>`;
    list.appendChild(el);
  });
}

async function addTopic(event) {
  event.preventDefault();
  const input = document.getElementById('topicInput');
  const topic = input.value.trim();
  if (topic.length < 2) return false;
  try {
    await api('/api/topics', 'POST', { topic });
    input.value = '';
    await loadTopics();
    showToast(`Now following "${topic}"`, 'success');
  } catch (e) {
    showToast('Failed to add: ' + e.message, 'error');
  }
  return false;
}

async function removeTopic(topicId) {
  try {
    await api(`/api/topics?topic_id=${topicId}`, 'DELETE');
    await loadTopics();
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
