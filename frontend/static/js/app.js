async function summarize() {
  const rssUrl = document.getElementById("rssUrl").value.trim();
  const numEpisodes = parseInt(document.getElementById("numEpisodes").value);
  const btn = document.getElementById("summarizeBtn");
  const loading = document.getElementById("loading");
  const error = document.getElementById("error");
  const results = document.getElementById("results");

  if (!rssUrl) {
    showError("Please enter a podcast RSS feed URL.");
    return;
  }

  // Reset UI
  error.classList.add("hidden");
  results.classList.add("hidden");
  loading.classList.remove("hidden");
  btn.disabled = true;

  try {
    const response = await fetch("/api/summarize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rss_url: rssUrl, num_episodes: numEpisodes }),
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Something went wrong.");
    }

    displayResults(data);
  } catch (err) {
    showError(err.message);
  } finally {
    loading.classList.add("hidden");
    btn.disabled = false;
  }
}

function displayResults(data) {
  document.getElementById("podcastTitle").textContent = data.podcast_title;

  const list = document.getElementById("episodeList");
  list.innerHTML = "";

  data.episodes.forEach((ep) => {
    const card = document.createElement("div");
    card.className = "episode-card";
    const summaryHtml = window.marked ? marked.parse(ep.summary) : ep.summary;
    card.innerHTML = `
      <h3>${ep.title}</h3>
      <div class="episode-date">${ep.published}</div>
      <div class="episode-summary">${summaryHtml}</div>
    `;
    list.appendChild(card);
  });

  document.getElementById("results").classList.remove("hidden");
}

function showError(msg) {
  const error = document.getElementById("error");
  error.textContent = msg;
  error.classList.remove("hidden");
}

// Allow Enter key to trigger summarize
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("rssUrl").addEventListener("keydown", (e) => {
    if (e.key === "Enter") summarize();
  });
});
