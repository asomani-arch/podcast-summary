/* ── STATE ─────────────────────────────────────────────── */
const state = {
  searchResults: [],
  currentPodcast: null,
  episodes: [],
  panelEpisodeId: null,
  subscriptions: [],
  trayOpen: false,
  expandedSettingsId: null,
};

const SECTION_LABELS = {
  overview:   'Overview',
  takeaways:  'Key takeaways',
};
const ALL_SECTIONS = Object.keys(SECTION_LABELS);

const LENGTH_LABELS = {
  short:    'Short',
  standard: 'Standard',
  deep:     'Deep dive',
};

const FREQUENCY_OPTIONS = [
  { value: 1,  label: 'Daily' },
  { value: 3,  label: 'Every 3 days' },
  { value: 7,  label: 'Weekly' },
  { value: 14, label: 'Fortnightly' },
  { value: 30, label: 'Monthly' },
];

/* ── INIT ──────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('searchInput').addEventListener('input', e => {
    const q = e.target.value.trim();
    if (q.length >= 2) debouncedSearch(q);
    else clearResults();
  });
  loadSubscriptions();
});

/* ── DEBOUNCE ──────────────────────────────────────────── */
function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

const debouncedSearch = debounce(performSearch, 320);

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
  const grid    = document.getElementById('resultsGrid');
  const label   = document.getElementById('resultsLabel');
  const empty   = document.getElementById('emptyState');
  const detail  = document.getElementById('podcastDetail');

  empty.classList.add('hidden');
  detail.classList.add('hidden');
  section.classList.remove('hidden');

  label.textContent = state.searchResults.length
    ? `${state.searchResults.length} results for "${query}"`
    : '';

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
      </div>
    `;
    card.addEventListener('click', () => selectPodcast(p));
    grid.appendChild(card);
  });
}

function clearResults() {
  document.getElementById('searchResults').classList.add('hidden');
  document.getElementById('podcastDetail').classList.add('hidden');
  document.getElementById('emptyState').classList.remove('hidden');
  state.currentPodcast = null;
  state.episodes = [];
  closePanel();
}

/* ── PODCAST DETAIL ────────────────────────────────────── */
async function selectPodcast(podcast) {
  state.currentPodcast = podcast;

  document.getElementById('searchResults').classList.add('hidden');
  const detail = document.getElementById('podcastDetail');
  detail.classList.remove('hidden');

  // Fill header info
  const artwork = podcast.artwork || '';
  document.getElementById('detailArtwork').src = artwork;
  document.getElementById('detailPublisher').textContent = podcast.publisher || '';
  document.getElementById('detailTitle').textContent = podcast.title || '';
  document.getElementById('detailDesc').textContent = podcast.description || '';

  // Blurred background from artwork
  const bg = document.getElementById('detailHeroBg');
  if (artwork) bg.style.backgroundImage = `url(${artwork})`;

  // Subscribe button state
  updateSubscribeBtn(podcast.subscribed);

  // Load episodes
  document.getElementById('episodeList').innerHTML = skeletons(5);

  try {
    const data = await api(
      `/api/podcast-episodes?podcast_index_id=${encodeURIComponent(podcast.id)}&rss_url=${encodeURIComponent(podcast.rss_url)}`
    );
    state.episodes = data.episodes || [];
    if (data.podcast_title) {
      state.currentPodcast.title = data.podcast_title;
      document.getElementById('detailTitle').textContent = data.podcast_title;
    }
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
    list.innerHTML = '<p class="no-results">No episodes found in this feed.</p>';
    return;
  }

  state.episodes.forEach((ep, i) => {
    const row = document.createElement('div');
    row.className = 'episode-row';
    row.id = `ep-row-${i}`;

    const date = ep.published_at
      ? new Date(ep.published_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
      : '';
    const dur = formatDuration(ep.duration);
    const meta = [date, dur].filter(Boolean).join(' · ');

    row.innerHTML = `
      <span class="ep-num">${String(i + 1).padStart(2, '0')}</span>
      <div class="ep-body">
        <p class="ep-title">${esc(ep.title)}</p>
        ${meta ? `<p class="ep-meta">${esc(meta)}</p>` : ''}
      </div>
      <div class="ep-action" id="ep-action-${i}">
        ${ep.has_summary
          ? `<button class="btn-view" onclick="viewSummary(${i})">View Summary</button>`
          : `<button class="btn-summarize" onclick="summarizeEpisode(${i}, this)">Summarize</button>`
        }
      </div>
    `;
    list.appendChild(row);
  });
}

/* ── SUMMARIZE ─────────────────────────────────────────── */
async function summarizeEpisode(index, btn) {
  const ep = state.episodes[index];
  const p  = state.currentPodcast;

  // Open panel immediately in loading state
  openPanel(ep.title, p.title);

  btn.disabled = true;
  btn.textContent = 'Summarizing…';

  try {
    const data = await api('/api/summarize', 'POST', {
      podcast_index_id:    p.id,
      rss_url:             p.rss_url,
      podcast_title:       p.title,
      artwork_url:         p.artwork || '',
      publisher:           p.publisher || '',
      episode_guid:        ep.guid,
      episode_title:       ep.title,
      episode_url:         ep.episode_url || '',
      episode_audio_url:   ep.audio_url || '',
      episode_description: ep.description || '',
      episode_duration:    ep.duration || '',
      episode_published_at: ep.published_at || null,
    });

    // Update local state
    state.episodes[index] = {
      ...ep,
      has_summary:       true,
      episode_id:        data.episode_id,
      summary:           data.summary,
      transcript_source: data.source,
    };

    // Swap button
    const actionEl = document.getElementById(`ep-action-${index}`);
    if (actionEl) {
      actionEl.innerHTML = `<button class="btn-view" onclick="viewSummary(${index})">View Summary</button>`;
    }

    // Show in panel
    showSummaryInPanel(data.episode_id, data.summary, data.source);
  } catch (e) {
    btn.textContent = 'Summarize';
    btn.disabled = false;
    closePanel();
    showToast(e.message || 'Failed to generate summary', 'error');
  }
}

function viewSummary(index) {
  const ep = state.episodes[index];
  const p  = state.currentPodcast;
  openPanel(ep.title, p ? p.title : '');
  showSummaryInPanel(ep.episode_id, ep.summary, ep.transcript_source);
}

/* ── SUMMARY PANEL ─────────────────────────────────────── */
function openPanel(epTitle, podcastName) {
  document.getElementById('panelTitle').textContent   = epTitle;
  document.getElementById('panelPodcast').textContent = podcastName;
  document.getElementById('panelBody').innerHTML = `
    <div class="panel-loading">
      <div class="spinner large"></div>
      <p>Generating summary…</p>
    </div>
  `;
  document.getElementById('emailBtn').classList.add('hidden');
  document.getElementById('summaryPanel').classList.add('open');
  document.getElementById('panelBackdrop').classList.remove('hidden');
  state.panelEpisodeId = null;
}

function showSummaryInPanel(episodeId, summary, source) {
  state.panelEpisodeId = episodeId;
  const html = window.marked ? marked.parse(summary || '') : (summary || '');
  document.getElementById('panelBody').innerHTML = `
    <div class="summary-content">${html}</div>
    <p class="source-tag">Transcript source: ${esc(sourceLabel(source))}</p>
  `;
  if (episodeId) {
    const emailBtn = document.getElementById('emailBtn');
    emailBtn.classList.remove('hidden');
    emailBtn.disabled = false;
    emailBtn.innerHTML = `
      <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="1" y="3" width="14" height="10" rx="1.5" stroke="currentColor" stroke-width="1.4"/>
        <path d="m1 4 7 5 7-5" stroke="currentColor" stroke-width="1.4"/>
      </svg>
      Email me
    `;
  }
}

function closePanel() {
  document.getElementById('summaryPanel').classList.remove('open');
  document.getElementById('panelBackdrop').classList.add('hidden');
  state.panelEpisodeId = null;
}

/* ── EMAIL SUMMARY ─────────────────────────────────────── */
async function emailCurrentSummary() {
  if (!state.panelEpisodeId) return;
  const btn = document.getElementById('emailBtn');
  btn.disabled = true;
  btn.textContent = 'Sending…';
  try {
    await api('/api/email-summary', 'POST', { episode_id: state.panelEpisodeId });
    showToast('Summary sent to your email!', 'success');
    btn.textContent = '✓ Sent';
    setTimeout(() => {
      btn.disabled = false;
      btn.innerHTML = `
        <svg viewBox="0 0 16 16" fill="none"><rect x="1" y="3" width="14" height="10" rx="1.5" stroke="currentColor" stroke-width="1.4"/><path d="m1 4 7 5 7-5" stroke="currentColor" stroke-width="1.4"/></svg>
        Email me
      `;
    }, 3000);
  } catch (e) {
    showToast('Failed to send: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = 'Email me';
  }
}

/* ── SUBSCRIBE ─────────────────────────────────────────── */
async function toggleSubscribe() {
  const p   = state.currentPodcast;
  const btn = document.getElementById('subscribeBtn');

  if (p.subscribed) {
    const feed = state.subscriptions.find(
      s => s.podcast_index_id === p.id || s.rss_url === p.rss_url
    );
    if (!feed) return;
    btn.disabled = true;
    try {
      await api(`/api/feeds?id=${feed.id}`, 'DELETE');
      p.subscribed = false;
      updateSubscribeBtn(false);
      await loadSubscriptions();
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
        rss_url:          p.rss_url,
        podcast_title:    p.title,
        podcast_index_id: p.id,
        artwork_url:      p.artwork || '',
        publisher:        p.publisher || '',
      });
      p.subscribed = true;
      updateSubscribeBtn(true);
      await loadSubscriptions();
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
  const tray     = document.getElementById('subsTray');
  const backdrop = document.getElementById('trayBackdrop');
  if (state.trayOpen) {
    tray.classList.add('open');
    backdrop.classList.remove('hidden');
    loadSubscriptions();
    loadStatus();
  } else {
    tray.classList.remove('open');
    backdrop.classList.add('hidden');
    state.expandedSettingsId = null;
  }
}

async function loadSubscriptions() {
  try {
    const data = await api('/api/feeds');
    state.subscriptions = data.feeds || [];
    document.getElementById('subCount').textContent = state.subscriptions.length;
    renderSubscriptions();
  } catch (_) {}
}

async function loadStatus() {
  const el = document.getElementById('trayStatus');
  try {
    const data = await api('/api/status');
    renderStatus(el, data.last_run);
  } catch (e) {
    el.classList.remove('hidden');
    el.innerHTML = `<p class="status-line status-error">Couldn't load status — ${esc(e.message)}</p>`;
  }
}

function renderStatus(el, run) {
  if (!run) {
    el.classList.remove('hidden');
    el.innerHTML = '<p class="status-line status-muted">No cron runs recorded yet.</p>';
    return;
  }
  el.classList.remove('hidden');
  const when = new Date(run.started_at);
  const relative = relativeTime(when);
  const errs = Array.isArray(run.errors) ? run.errors : [];
  const checked = run.feeds_checked || 0;
  const skipped = run.feeds_skipped || 0;
  const newEp   = run.new_episodes || 0;
  const stateClass = errs.length ? 'status-error' : (run.ok ? 'status-ok' : 'status-error');
  const dot = `<span class="status-dot ${stateClass}"></span>`;
  const summary = errs.length
    ? `Last check ${esc(relative)} — ${errs.length} error${errs.length === 1 ? '' : 's'}`
    : `Last check ${esc(relative)} · ${checked} checked · ${newEp} new`;
  const detail = `${checked} checked, ${skipped} skipped, ${newEp} new episode${newEp === 1 ? '' : 's'}`;
  const errList = errs.length
    ? `<ul class="status-errs">${errs.slice(0, 3).map(e => `<li>${esc(e)}</li>`).join('')}${
        errs.length > 3 ? `<li class="status-more">+${errs.length - 3} more</li>` : ''
      }</ul>`
    : '';
  el.innerHTML = `
    <div class="status-row">${dot}<span class="status-line">${summary}</span></div>
    ${errs.length ? '' : `<p class="status-detail">${esc(detail)}</p>`}
    ${errList}
  `;
}

function relativeTime(when) {
  const diffMs = Date.now() - when.getTime();
  const mins  = Math.round(diffMs / 60000);
  if (mins < 1)  return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs  = Math.round(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  if (days < 14) return `${days}d ago`;
  return when.toLocaleDateString();
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
    const length = s.summary_length || 'standard';
    const freq = s.frequency_days || 1;
    const settingsOpen = state.expandedSettingsId === s.id;

    item.innerHTML = `
      <div class="sub-item">
        ${s.artwork_url
          ? `<img class="sub-artwork" src="${esc(s.artwork_url)}" alt="" />`
          : '<div class="sub-artwork-placeholder"></div>'}
        <div class="sub-info">
          <p class="sub-title">${esc(s.podcast_title || 'Untitled')}</p>
          <p class="sub-meta">${s.episode_count || 0} summarized · ${esc(LENGTH_LABELS[length] || length)} · ${esc(frequencyLabel(freq))}</p>
        </div>
        <button class="btn-gear" title="Customize summaries" onclick="toggleFeedSettings(${s.id})" aria-expanded="${settingsOpen}">
          <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle cx="8" cy="8" r="2.4" stroke="currentColor" stroke-width="1.3"/>
            <path d="M8 1.5v2M8 12.5v2M14.5 8h-2M3.5 8h-2M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4M12.6 12.6l-1.4-1.4M4.8 4.8 3.4 3.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
          </svg>
        </button>
        <button class="btn-unsub" onclick="unsubscribeFeed(${s.id}, this)">Unsubscribe</button>
      </div>
      ${settingsOpen ? renderFeedSettings(s, length, freq) : ''}
    `;
    list.appendChild(item);
  });
}

function frequencyLabel(days) {
  const known = FREQUENCY_OPTIONS.find(o => o.value === days);
  if (known) return known.label.toLowerCase();
  return `every ${days} day${days === 1 ? '' : 's'}`;
}

function renderFeedSettings(s, length, freq) {
  const lengthOpts = Object.entries(LENGTH_LABELS).map(([v, label]) =>
    `<label class="seg-opt ${v === length ? 'seg-opt-on' : ''}">
       <input type="radio" name="length-${s.id}" value="${v}" ${v === length ? 'checked' : ''}
              onchange="saveFeedSettings(${s.id})" />
       ${esc(label)}
     </label>`
  ).join('');

  const freqOpts = FREQUENCY_OPTIONS.map(o =>
    `<option value="${o.value}" ${o.value === freq ? 'selected' : ''}>${esc(o.label)}</option>`
  ).join('');

  return `
    <div class="sub-settings" id="sub-settings-${s.id}">
      <div class="settings-group">
        <p class="settings-label">Length</p>
        <div class="seg-group">${lengthOpts}</div>
      </div>
      <div class="settings-group">
        <p class="settings-label">Frequency</p>
        <select class="settings-select" name="freq-${s.id}" onchange="saveFeedSettings(${s.id})">${freqOpts}</select>
        <p class="settings-hint">Hobby cron runs daily. Frequency gates whether this feed is processed each tick.</p>
      </div>
      <p class="settings-status" id="settings-status-${s.id}"></p>
    </div>
  `;
}

function toggleFeedSettings(id) {
  state.expandedSettingsId = state.expandedSettingsId === id ? null : id;
  renderSubscriptions();
}

async function saveFeedSettings(id) {
  const lenEl = document.querySelector(`input[name="length-${id}"]:checked`);
  const freqEl = document.querySelector(`select[name="freq-${id}"]`);
  const body = {
    summary_length: lenEl ? lenEl.value : undefined,
    sections: ALL_SECTIONS,
    frequency_days: freqEl ? parseInt(freqEl.value, 10) : undefined,
  };
  const statusEl = document.getElementById(`settings-status-${id}`);
  if (statusEl) statusEl.textContent = 'Saving…';
  try {
    const data = await api(`/api/feeds/${id}`, 'PATCH', body);
    // Update local copy without re-rendering (keeps the open settings panel intact)
    const idx = state.subscriptions.findIndex(s => s.id === id);
    if (idx >= 0 && data.feed) state.subscriptions[idx] = { ...state.subscriptions[idx], ...data.feed };
    if (statusEl) statusEl.textContent = '✓ Saved';
    setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 1600);
    // Refresh the sub-item meta line (length / frequency) without collapsing settings
    const meta = document.querySelector(`#sub-settings-${id}`)?.previousElementSibling?.querySelector('.sub-meta');
    if (meta && data.feed) {
      const freqLabel = frequencyLabel(data.feed.frequency_days || 1);
      meta.textContent = `${data.feed.episode_count ?? state.subscriptions[idx]?.episode_count ?? 0} summarized · ${LENGTH_LABELS[data.feed.summary_length] || data.feed.summary_length} · ${freqLabel}`;
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = 'Failed — ' + e.message;
  }
}

async function unsubscribeFeed(id, btn) {
  btn.disabled = true;
  try {
    await api(`/api/feeds?id=${id}`, 'DELETE');
    // Sync subscribed status if current podcast matches
    if (state.currentPodcast) {
      const removed = state.subscriptions.find(s => s.id === id);
      if (removed && (removed.podcast_index_id === state.currentPodcast.id || removed.rss_url === state.currentPodcast.rss_url)) {
        state.currentPodcast.subscribed = false;
        updateSubscribeBtn(false);
      }
    }
    await loadSubscriptions();
    showToast('Unsubscribed', 'success');
  } catch (e) {
    showToast('Failed to unsubscribe: ' + e.message, 'error');
    btn.disabled = false;
  }
}

/* ── API HELPER ────────────────────────────────────────── */
async function api(path, method = 'GET', body = null) {
  const opts = { method, headers: {} };
  if (body) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
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

function formatDuration(dur) {
  if (!dur) return '';
  if (/^\d+$/.test(String(dur))) {
    const secs = parseInt(dur, 10);
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    if (h > 0) return `${h}h ${m}m`;
    return m > 0 ? `${m}m` : '';
  }
  // Already formatted (e.g. "01:23:45")
  return String(dur);
}

function sourceLabel(source) {
  const labels = {
    colossus: 'Colossus transcript',
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
    </div>
  `).join('');
}

function showToast(msg, type = 'success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast ${type}`;
  t.classList.remove('hidden');
  requestAnimationFrame(() => {
    requestAnimationFrame(() => t.classList.add('show'));
  });
  clearTimeout(t._timer);
  t._timer = setTimeout(() => {
    t.classList.remove('show');
    setTimeout(() => t.classList.add('hidden'), 280);
  }, 3200);
}
