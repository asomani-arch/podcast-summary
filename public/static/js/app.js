// --- Subscribe ---
async function subscribe() {
  const rss_url = document.getElementById("rssUrl").value.trim();
  const email = document.getElementById("email").value.trim();
  const btn = document.getElementById("subscribeBtn");
  const msg = document.getElementById("msg");

  if (!rss_url || !email) {
    return showMsg("Please enter both an RSS URL and an email.", "error");
  }

  btn.disabled = true;
  msg.classList.add("hidden");

  try {
    const resp = await fetch("/api/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rss_url, email }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "Subscribe failed.");
    showMsg(`✓ Subscribed to "${data.podcast_title}". You'll get an email when a new episode is published.`, "success");
    document.getElementById("rssUrl").value = "";
    loadFeeds();
  } catch (e) {
    showMsg(e.message, "error");
  } finally {
    btn.disabled = false;
  }
}

function showMsg(text, kind) {
  const msg = document.getElementById("msg");
  msg.textContent = text;
  msg.className = `msg ${kind}`;
}

// --- Feeds ---
async function loadFeeds() {
  const list = document.getElementById("feedList");
  list.innerHTML = "<p style='color:#999'>Loading…</p>";
  try {
    const resp = await fetch("/api/feeds");
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error);
    list.innerHTML = "";
    data.feeds.forEach(renderFeed);
  } catch (e) {
    list.innerHTML = `<p style="color:#cc3333">Error: ${e.message}</p>`;
  }
}

function renderFeed(f) {
  const list = document.getElementById("feedList");
  const div = document.createElement("div");
  div.className = "feed-item";
  div.innerHTML = `
    <div class="feed-info">
      <div class="feed-title">${escape(f.podcast_title || "(untitled)")}</div>
      <div class="feed-meta">${escape(f.email)} · ${f.episode_count} summarized · ${escape(f.rss_url)}</div>
    </div>
    <button class="danger" data-id="${f.id}">Unsubscribe</button>
  `;
  div.querySelector("button").onclick = () => unsubscribe(f.id);
  list.appendChild(div);
}

async function unsubscribe(id) {
  if (!confirm("Unsubscribe from this feed?")) return;
  await fetch(`/api/feeds?id=${id}`, { method: "DELETE" });
  loadFeeds();
}

// --- Episodes ---
async function loadEpisodes() {
  const list = document.getElementById("episodeList");
  list.innerHTML = "<p style='color:#999'>Loading…</p>";
  try {
    const resp = await fetch("/api/episodes");
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error);
    list.innerHTML = "";
    data.episodes.forEach(renderEpisode);
  } catch (e) {
    list.innerHTML = `<p style="color:#cc3333">Error: ${e.message}</p>`;
  }
}

function renderEpisode(e) {
  const list = document.getElementById("episodeList");
  const div = document.createElement("div");
  div.className = "episode-item";
  const date = e.published_at ? new Date(e.published_at).toLocaleDateString() : "";
  const html = window.marked ? marked.parse(e.summary || "") : (e.summary || "");
  div.innerHTML = `
    <h3>${escape(e.title || "(untitled)")}</h3>
    <div class="episode-meta">${escape(e.podcast_title)} · ${date} · source: ${escape(e.transcript_source || "?")}</div>
    <details>
      <summary>Show summary</summary>
      <div class="episode-summary">${html}</div>
    </details>
  `;
  list.appendChild(div);
}

// --- helpers ---
function escape(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

document.addEventListener("DOMContentLoaded", () => {
  loadFeeds();
  loadEpisodes();
});
