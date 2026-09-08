const $ = (id) => document.getElementById(id);

let current = null;      // single-video info
let playlistItems = [];  // playlist entries
let selected = new Set();
let pollTimer = null;
let infoToken = 0;       // ignore stale /api/info responses
let jobRunning = false;

$('urlForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = $('urlInput').value.trim();
  if (!url) return;
  hideError();
  $('loading').classList.remove('hidden');
  hideAllViews();
  const token = ++infoToken;
  $('goBtn').disabled = true;
  try {
    const res = await fetch('/api/info', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (token !== infoToken) return;
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Failed to fetch info');
    $('loading').classList.add('hidden');
    if (data.type === 'video' || data.type === 'spotify_track') showVideo(data);
    else showPlaylist(data);
  } catch (err) {
    if (token !== infoToken) return;
    $('loading').classList.add('hidden');
    showError(err.message);
  } finally {
    if (token === infoToken) $('goBtn').disabled = false;
  }
});

function hideAllViews() {
  ['videoView', 'playlistView', 'progressView'].forEach((id) => $(id).classList.add('hidden'));
}

function showError(msg) {
  const el = $('error');
  el.textContent = msg;
  el.classList.remove('hidden');
}

function hideError() {
  $('error').classList.add('hidden');
}

$('posterWrap').addEventListener('click', () => {
  if (!current || current.type === 'spotify_track') return;
  // Only embed YouTube iframes; other platforms don't have embed players.
  if (current.id && !current.url?.includes('youtube.com') && !current.url?.includes('youtu.be')) return;
  $('posterWrap').classList.add('hidden');
  $('player').src = `https://www.youtube.com/embed/${current.id}?autoplay=1&rel=0`;
  $('player').classList.remove('hidden');
});

function showVideo(data) {
  current = data;
  const isSpotify = data.type === 'spotify_track';
  const isYT = data.url && (data.url.includes('youtube.com') || data.url.includes('youtu.be'));
  // Reset player + poster for the new video.
  $('player').src = '';
  $('player').classList.add('hidden');
  $('posterWrap').classList.add('hidden');
  $('trackArt').classList.add('hidden');
  if (isSpotify) {
    $('trackArt').src = data.thumbnail || '';
    $('trackArt').classList.toggle('hidden', !data.thumbnail);
    $('videoVia').classList.remove('hidden');
    $('videoVia').textContent = data.artist ? `via Spotify · ${data.artist}` : 'via Spotify';
    $('videoDuration').textContent = '';
    $('cardMp4').classList.add('hidden');
    $('watchOnYT').classList.add('hidden');
  } else {
    const poster = $('videoPoster');
    if (isYT && data.id) {
      // YouTube: use thumbnail CDN with fallback.
      poster.onerror = () => {
        poster.onerror = null;
        poster.src = `https://i.ytimg.com/vi/${data.id}/hqdefault.jpg`;
      };
      poster.src = data.thumbnail || `https://i.ytimg.com/vi/${data.id}/hqdefault.jpg`;
    } else {
      // Non-YouTube: use the thumbnail from yt-dlp (or hide poster).
      poster.onerror = null;
      poster.src = data.thumbnail || '';
    }
    poster.onerror = () => { poster.classList.add('hidden'); poster.onerror = null; };
    $('posterWrap').classList.toggle('hidden', !poster.src);
    $('videoVia').classList.add('hidden');
    $('videoDuration').textContent = data.duration ? `${data.duration}` : '';
    $('cardMp4').classList.remove('hidden');
    const yt = $('watchOnYT');
    if (isYT) {
      yt.href = `https://www.youtube.com/watch?v=${data.id}`;
      yt.classList.remove('hidden');
    } else {
      yt.classList.add('hidden');
    }
  }
  $('videoTitle').textContent = data.title || '';

  const sel = $('mp4Quality');
  sel.innerHTML = '';
  const quals = (data.qualities && data.qualities.length) ? data.qualities : ['360p', '720p'];
  quals.forEach((q) => {
    const opt = document.createElement('option');
    opt.value = q;
    opt.textContent = q;
    sel.appendChild(opt);
  });
  if (quals.includes('720p')) sel.value = '720p';

  $('videoView').classList.remove('hidden');
  $('videoView').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function showPlaylist(data) {
  playlistItems = data.entries || [];
  selected = new Set();
  const isSpotify = data.type === 'spotify_playlist';
  $('playlistTitle').textContent = data.title || 'Playlist';

  $('plVia').classList.toggle('hidden', !isSpotify);
  $('plVia').textContent = isSpotify ? 'via Spotify' : '';
  $('plMp4').classList.toggle('hidden', isSpotify);
  $('plQuality').closest('.select-row').classList.toggle('hidden', isSpotify);

  const grid = $('playlistGrid');
  grid.innerHTML = '';
  if (!playlistItems.length) {
    grid.innerHTML = '<p class="muted">No songs found in this playlist.</p>';
    $('playlistView').classList.remove('hidden');
    updateCount();
    return;
  }
  playlistItems.forEach((item) => grid.appendChild(makeCard(item)));
  $('selectAll').checked = false;
  updateCount();
  $('playlistView').classList.remove('hidden');
  $('playlistView').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function makeCard(item) {
  const card = document.createElement('div');
  card.className = 'card-item';
  card.innerHTML = `
    <label class="item-select">
      <input type="checkbox" value="${escapeAttr(item.id)}">
      <span class="thumb-wrap">
        <img src="${escapeAttr(item.thumbnail)}" alt="" loading="lazy">
        ${item.duration ? `<span class="dur-badge">${escapeHtml(item.duration)}</span>` : ''}
        <span class="check-badge" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        </span>
      </span>
      <span class="item-info">
        <span class="item-title">${escapeHtml(item.title)}</span>
        ${item.artist ? `<span class="item-artist">${escapeHtml(item.artist)}</span>` : ''}
      </span>
    </label>`;
  const cb = card.querySelector('input');
  cb.addEventListener('change', () => {
    if (cb.checked) selected.add(item.id);
    else selected.delete(item.id);
    card.classList.toggle('selected', cb.checked);
    $('selectAll').checked = selected.size === playlistItems.length;
    updateCount();
  });
  return card;
}

function updateCount() {
  const n = selected.size;
  const spot = playlistItems.some((i) => i.query);
  $('selCount').textContent = n ? `${n} selected` : (spot ? 'No songs selected' : 'No videos selected');
}

$('selectAll').addEventListener('change', (e) => {
  const on = e.target.checked;
  playlistItems.forEach((item) => {
    if (on) selected.add(item.id);
    else selected.delete(item.id);
    const cb = $('playlistGrid').querySelector(`input[value="${CSS.escape(item.id)}"]`);
    if (cb) {
      cb.checked = on;
      cb.closest('.card-item').classList.toggle('selected', on);
    }
  });
  updateCount();
});

function selectedItems() {
  return playlistItems
    .filter((item) => selected.has(item.id))
    .map((item) => {
      const out = { url: item.url, title: item.title };
      if (item.query) out.query = item.query;
      return out;
    });
}

$('dlMp3').addEventListener('click', () => startSingle('mp3'));
$('dlMp4').addEventListener('click', () => startSingle('mp4', $('mp4Quality').value));
$('plMp3').addEventListener('click', () => startBatch('mp3'));
$('plMp4').addEventListener('click', () => startBatch('mp4', $('plQuality').value));

function startSingle(type, quality) {
  if (!current) return;
  const spot = current.type === 'spotify_track';
  if (spot && type === 'mp4') {
    showError('Spotify songs are audio only - MP3 is available.');
    return;
  }
  const item = { url: current.url, title: current.title };
  if (spot) item.query = current.query;
  const items = [item];
  startDownload(type, items, quality);
}

function startBatch(type, quality) {
  const items = selectedItems();
  if (!items.length) {
    showError('Select at least one item first.');
    return;
  }
  if (items.some((i) => i.query) && type === 'mp4') {
    showError('Spotify songs are audio only - use MP3.');
    return;
  }
  startDownload(type, items, quality);
}

async function startDownload(type, items, quality) {
  hideError();
  showProgress();
  setBusy(true);
  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type, quality: quality || null, items }),
    });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Failed to start download');
    poll(data.job_id);
  } catch (err) {
    setBusy(false);
    hideProgress();
    showError(err.message);
  }
}

function setBusy(busy) {
  jobRunning = busy;
  ['dlMp3', 'dlMp4', 'plMp3', 'plMp4', 'goBtn'].forEach((id) => {
    $(id).disabled = busy;
  });
}

function showProgress() {
  $('progressIcon').className = 'spinner';
  $('progressFill').style.width = '0%';
  $('progressPct').textContent = '0%';
  $('progressText').textContent = 'Starting…';
  $('progressCurrent').textContent = '';
  $('progressView').classList.remove('hidden');
  $('progressView').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function hideProgress() {
  $('progressView').classList.add('hidden');
}

const lastStatus = { p: -1, m: '', c: '' };

function poll(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/status/${jobId}`);
      const data = await res.json();
      if (data.error) {
        clearInterval(pollTimer);
        setBusy(false);
        hideProgress();
        showError(data.error);
        return;
      }
      // Only touch the DOM when a value actually changed.
      const pct = data.progress || 0;
      if (pct !== lastStatus.p) {
        lastStatus.p = pct;
        $('progressFill').style.width = `${pct}%`;
        $('progressPct').textContent = `${pct}%`;
      }
      const msg = data.message || '';
      if (msg !== lastStatus.m) {
        lastStatus.m = msg;
        $('progressText').textContent = msg;
      }
      const cur = data.current || '';
      if (cur !== lastStatus.c) {
        lastStatus.c = cur;
        $('progressCurrent').textContent = cur;
      }
      if (data.status === 'done') {
        clearInterval(pollTimer);
        setBusy(false);
        $('progressIcon').className = 'spinner spinner-done';
        const n = data.failed_count || 0;
        if (n > 0) {
          const fails = (data.failed || []).map((f) => `• ${f}`).join('\n');
          $('progressText').textContent = `Ready with ${n} skipped`;
          $('progressCurrent').textContent = `The rest downloaded - these couldn't be fetched:\n${fails}`;
        } else {
          $('progressText').textContent = 'Your download is ready!';
          $('progressCurrent').textContent = 'The file is downloading now…';
        }
        window.location.href = data.download_url;
        setTimeout(hideProgress, 9000);
      } else if (data.status === 'error') {
        clearInterval(pollTimer);
        setBusy(false);
        hideProgress();
        showError(data.error || 'Download failed.');
      }
    } catch (err) {
      clearInterval(pollTimer);
      setBusy(false);
      hideProgress();
      showError('Lost connection to the server.');
    }
  }, 1000);
}

function escapeHtml(str) {
  return String(str || '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function escapeAttr(str) {
  return escapeHtml(str).replace(/`/g, '&#96;');
}
