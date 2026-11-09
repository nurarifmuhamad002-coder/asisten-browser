/* Asisten Browser — Simple WhatsApp/Telegram Style Client */

let ws = null;
let reconnectTimer = null;

let appState = {
  state: 'IDLE',
  liveStatus: 'Online · Siap disuruh apa aja',
  manualTakeover: false,
  pendingApproval: null,
  files: [],
  llm: {},
  telegram: {},
  zoomed: false,
  latestFrameB64: '',
};

let prevServerState = 'IDLE';
let activeReaderFilename = '';

const canvas = document.getElementById('browserCanvas');
const ctx = canvas.getContext('2d');
const frameImg = new Image();
let hasFrame = false;

let agentCursor = { x: 640, y: 400, label: '', ts: 0 };

const PRESETS = {
  portal_checkout:
    'Buka portal akun gua, tunggu gua login sendiri, lalu baca file profil_pengajuan.json, isikan ke formulir layanan, dan minta izin gua sebelum klik bayar.',
  wikipedia:
    'Buka Wikipedia Indonesia, cari topik Kecerdasan buatan, baca isi halamannya lalu simpan rangkumannya ke file riset_ai_wikipedia.md',
  hackernews:
    'Buka Hacker News, baca daftar berita teknologi teratas lalu simpan laporannya ke file berita_teknologi.md',
  books_csv:
    'Buka books.toscrape.com, kumpulkan daftar buku di halaman itu lalu simpan jadi tabel katalog_buku.csv',
};

/* ================= WEBSOCKET ================= */

function getWsUrl() {
  const base = new URL('./ws', window.location.href);
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
  return base.href;
}

function connectWs() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  ws = new WebSocket(getWsUrl());

  ws.onopen = () => {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  };

  ws.onmessage = (ev) => {
    try {
      handleEvent(JSON.parse(ev.data));
    } catch (e) {
      console.error(e);
    }
  };

  ws.onclose = () => {
    reconnectTimer = setTimeout(connectWs, 1800);
  };
}

function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

/* ================= EVENT DISPATCH ================= */

function handleEvent(msg) {
  switch (msg.type) {
    case 'init': {
      const snap = msg.snapshot || {};
      document.getElementById('dynamicMessages').innerHTML = '';
      if (msg.frame) setFrame(msg.frame);
      (snap.chat_messages || []).forEach((m) => renderChatBubble(m));
      syncUiState(snap);
      if (snap.files) renderFilesSheet(snap.files);
      break;
    }
    case 'frame': {
      if (msg.data) setFrame(msg.data);
      break;
    }
    case 'agent_cursor': {
      agentCursor = {
        x: msg.x || 640,
        y: msg.y || 400,
        label: msg.label || '',
        ts: Date.now(),
      };
      redrawCanvas();
      break;
    }
    case 'chat_message': {
      renderChatBubble(msg.message);
      break;
    }
    case 'agent_state': {
      syncUiState(msg);
      break;
    }
    case 'files_updated': {
      renderFilesSheet(msg.files || []);
      break;
    }
    case 'file_downloaded': {
      renderFilesSheet(msg.files || []);
      renderChatBubble({
        role: 'file',
        text: `File terunduh: ${msg.filename}`,
        meta: { filename: msg.filename, size: msg.size },
        time: nowTime(),
      });
      break;
    }
    case 'github_status': {
      handleGithubStatusEvent(msg);
      break;
    }
    default:
      break;
  }
}

/* ================= CANVAS & THUMBNAIL ================= */

frameImg.onload = () => {
  hasFrame = true;
  redrawCanvas();
};

function setFrame(b64) {
  appState.latestFrameB64 = b64;
  const dataUri = 'data:image/jpeg;base64,' + b64;
  frameImg.src = dataUri;
  const mini = document.getElementById('miniThumbImg');
  if (mini) mini.src = dataUri;
}

function redrawCanvas() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (hasFrame) {
    ctx.drawImage(frameImg, 0, 0, canvas.width, canvas.height);
  } else {
    ctx.fillStyle = '#0f172a';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }

  // Highlight sensitive target button
  if (appState.state === 'WAITING_APPROVAL' && appState.pendingApproval && appState.pendingApproval.rect) {
    const r = appState.pendingApproval.rect;
    ctx.save();
    ctx.strokeStyle = '#f43f5e';
    ctx.lineWidth = 4;
    ctx.setLineDash([8, 5]);
    ctx.strokeRect(r.x - 5, r.y - 5, r.width + 10, r.height + 10);
    ctx.fillStyle = 'rgba(244, 63, 94, 0.18)';
    ctx.fillRect(r.x - 5, r.y - 5, r.width + 10, r.height + 10);
    ctx.restore();
  }

  // AI cursor
  const age = Date.now() - agentCursor.ts;
  if (age < 3000 && appState.state === 'RUNNING') {
    ctx.save();
    ctx.beginPath();
    ctx.arc(agentCursor.x, agentCursor.y, 16, 0, Math.PI * 2);
    ctx.strokeStyle = '#00a884';
    ctx.lineWidth = 3;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(agentCursor.x, agentCursor.y, 5, 0, Math.PI * 2);
    ctx.fillStyle = '#34d399';
    ctx.fill();
    ctx.restore();
  }
}

/* ================= STATE SYNC ================= */

function syncUiState(data) {
  if (data.state !== undefined) appState.state = data.state;
  if (data.live_status !== undefined) appState.liveStatus = data.live_status;
  if (data.manual_takeover !== undefined) appState.manualTakeover = data.manual_takeover;
  if (data.pending_approval !== undefined) appState.pendingApproval = data.pending_approval;
  if (data.llm !== undefined) appState.llm = data.llm;
  if (data.telegram !== undefined) appState.telegram = data.telegram;

  // Header status
  document.getElementById('liveStatusText').textContent = appState.liveStatus || 'Online';
  document.getElementById('onlineDot').classList.toggle('busy', appState.state !== 'IDLE');

  // Send vs Stop button
  const busy = appState.state !== 'IDLE';
  document.getElementById('sendBtn').classList.toggle('hidden', busy);
  document.getElementById('stopBtn').classList.toggle('hidden', !busy);

  // Floating Mini Bar only while RUNNING in background
  const miniBar = document.getElementById('liveMiniBar');
  if (appState.state === 'RUNNING') {
    miniBar.classList.remove('hidden');
    document.getElementById('miniStepTitle').textContent = appState.liveStatus || 'Sedang bekerja...';
  } else {
    miniBar.classList.add('hidden');
  }

  // OTOMATIS MUNCULKAN LAYAR SAAT BUTUH LOGIN USER!
  if (appState.state === 'WAITING_HUMAN_TAKEOVER' && prevServerState !== 'WAITING_HUMAN_TAKEOVER') {
    openBrowserSheet('login', data.takeover_reason);
  }

  // Footer tombol selesai login di dalam sheet (sembunyikan bila Kartu Isi Cepat sudah tampil)
  const hasQuickCard = !document.getElementById('quickCredCard').classList.contains('hidden');
  document
    .getElementById('sheetTakeoverFooter')
    .classList.toggle('hidden', appState.state !== 'WAITING_HUMAN_TAKEOVER' || hasQuickCard);

  prevServerState = appState.state;
  redrawCanvas();
}

/* ================= RENDER CHAT BUBBLES ================= */

function renderChatBubble(m) {
  if (!m) return;
  const wrap = document.getElementById('dynamicMessages');
  const t = m.time || nowTime();

  // 1. Pesan User (Kanan Hijau WA)
  if (m.role === 'user') {
    const el = document.createElement('div');
    el.className = 'bubble bubble-out';
    el.innerHTML = `${esc(m.text)}<div class="bubble-time">${esc(t)} ✓✓</div>`;
    wrap.appendChild(el);
    scrollToBottom();
    return;
  }

  // 2. Bubble Dokumen / File ala WhatsApp
  if (m.role === 'file') {
    const fname = (m.meta && m.meta.filename) || 'dokumen.md';
    const fsize = formatSize((m.meta && m.meta.size) || 0);
    const el = document.createElement('div');
    el.className = 'bubble bubble-in';
    el.style.width = '86%';
    el.style.maxWidth = '360px';
    el.innerHTML = `
      <div class="doc-card">
        <div class="doc-icon">📄</div>
        <div class="doc-info">
          <div class="doc-name">${esc(fname)}</div>
          <div class="doc-size">${esc(fsize)} · Dokumen Siap</div>
        </div>
      </div>
      <div class="doc-btns">
        <button class="doc-btn" onclick="openFileReader('${escJs(fname)}')">👁️ Baca Isi</button>
        <a class="doc-btn" href="./api/files/${encodeURIComponent(fname)}/download" download>⬇️ Simpan ke HP</a>
      </div>
      <div class="bubble-time">${esc(t)}</div>
    `;
    wrap.appendChild(el);
    scrollToBottom();
    return;
  }

  // 3. Kartu Butuh Login di dalam Chat
  if (m.role === 'takeover') {
    const el = document.createElement('div');
    el.className = 'action-card';
    const thumbHtml = appState.latestFrameB64
      ? `<img class="action-card-thumb" src="data:image/jpeg;base64,${appState.latestFrameB64}" onclick="openBrowserSheet('login')" />`
      : '';
    el.innerHTML = `
      ${thumbHtml}
      <div class="action-card-body">
        <div class="action-card-title">🔐 Butuh Lu Login Sendiri Bro</div>
        <div class="action-card-desc">${esc(m.text)}</div>
        <div class="action-btn-grid">
          <button class="big-btn btn-green" onclick="openBrowserSheet('login')">🔑 Buka Layar Buat Login</button>
        </div>
      </div>
    `;
    wrap.appendChild(el);
    scrollToBottom();
    return;
  }

  // 4. Kartu Minta Izin Tindakan Sensitif di dalam Chat
  if (m.role === 'approval') {
    const meta = m.meta || {};
    const cid = 'appr_' + Math.random().toString(36).slice(2, 8);
    const el = document.createElement('div');
    el.className = 'action-card danger';
    const thumbHtml = appState.latestFrameB64
      ? `<img class="action-card-thumb" src="data:image/jpeg;base64,${appState.latestFrameB64}" onclick="openBrowserSheet('peek')" />`
      : '';
    el.innerHTML = `
      ${thumbHtml}
      <div class="action-card-body">
        <div class="action-card-title">🛡️ Minta Izin Dulu Bro</div>
        <div class="action-card-desc">
          Asisten mau klik tombol <b>"${esc(meta.target_label || '')}"</b>.<br/>
          <span style="color:#94a3b8;font-size:12px;">${esc(meta.reason || '')}</span>
        </div>
        <div id="${cid}" class="action-btn-grid">
          <button class="big-btn btn-green" onclick="decideApproval('${cid}', 'approve')">✅ Ya, Setuju Jalanin</button>
          <button class="big-btn btn-dark" onclick="openBrowserSheet('peek')">👀 Cek Layar Dulu</button>
          <button class="big-btn btn-red" onclick="decideApproval('${cid}', 'reject')">❌ Batalin</button>
        </div>
      </div>
    `;
    wrap.appendChild(el);
    scrollToBottom();
    return;
  }

  // 5. Langkah Kerja Ringkas
  if (m.role === 'step') {
    const el = document.createElement('div');
    el.className = 'bubble-step';
    el.textContent = '⚡ ' + m.text;
    wrap.appendChild(el);
    scrollToBottom();
    return;
  }

  // 6. Info Sistem Tengah
  if (m.role === 'system') {
    const el = document.createElement('div');
    el.className = 'sys-pill';
    el.textContent = m.text;
    wrap.appendChild(el);
    scrollToBottom();
    return;
  }

  // 7. Balasan Akhir Asisten
  const el = document.createElement('div');
  el.className = 'bubble bubble-in';
  el.innerHTML = `${formatMd(m.text)}<div class="bubble-time">${esc(t)}</div>`;
  wrap.appendChild(el);
  scrollToBottom();
}

function scrollToBottom() {
  const feed = document.getElementById('chatFeed');
  setTimeout(() => {
    feed.scrollTop = feed.scrollHeight;
  }, 30);
}

/* ================= CHAT COMMANDS & APPROVAL ================= */

function autoGrowInput(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(100, el.scrollHeight) + 'px';
}

function handleInputKey(ev) {
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault();
    sendChatCommand();
  }
}

function tapPreset(key) {
  const text = PRESETS[key];
  if (!text) return;
  document.getElementById('chatInput').value = text;
  sendChatCommand();
}

async function sendChatCommand() {
  const inp = document.getElementById('chatInput');
  const goal = inp.value.trim();
  if (!goal) return;
  inp.value = '';
  inp.style.height = 'auto';
  inp.blur();

  await fetch('./api/agent/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ goal }),
  });
}

async function stopCommand() {
  await fetch('./api/agent/stop', { method: 'POST' });
}

async function decideApproval(containerId, decision) {
  const box = document.getElementById(containerId);
  if (box) {
    box.innerHTML =
      decision === 'approve'
        ? '<div style="color:#34d399;font-weight:700;text-align:center;padding:6px;">✅ Disetujui oleh lu</div>'
        : '<div style="color:#fca5a5;font-weight:700;text-align:center;padding:6px;">❌ Dibatalkan</div>';
  }
  await fetch('./api/agent/approval', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision }),
  });
}

/* ================= SLIDE-UP LIVE BROWSER SHEET ================= */

let detectedCredFields = [];

async function loadQuickCredentialFields(isLoginMode) {
  const card = document.getElementById('quickCredCard');
  const box = document.getElementById('quickCredInputs');
  const manualBar = document.getElementById('manualTypeBar');
  const footer = document.getElementById('sheetTakeoverFooter');

  if (!isLoginMode) {
    card.classList.add('hidden');
    manualBar.classList.remove('hidden');
    return;
  }

  try {
    const res = await fetch('./api/browser/login-fields');
    const data = await res.json();
    detectedCredFields = (data && data.fields) || [];
  } catch (e) {
    detectedCredFields = [];
  }

  if (!detectedCredFields.length) {
    // Halaman login pakai QR / Tombol Google / Captcha (tidak ada input teks password)
    card.classList.add('hidden');
    manualBar.classList.remove('hidden');
    footer.classList.toggle('hidden', appState.state !== 'WAITING_HUMAN_TAKEOVER');
    return;
  }

  box.innerHTML = '';
  let firstEmptyId = null;

  detectedCredFields.forEach((f, i) => {
    const inpId = 'qc_inp_' + i;
    const isSecret = f.kind === 'password';
    const initVal = f.current_value || '';
    if (!firstEmptyId && !initVal) firstEmptyId = inpId;

    const row = document.createElement('div');
    row.className = 'qc-field';
    row.innerHTML = `
      <label for="${inpId}">${isSecret ? '🔑' : (f.kind === 'otp' ? '🔢' : '👤')} ${esc(f.label)}</label>
      <div class="qc-input-row">
        <input
          id="${inpId}"
          type="${isSecret ? 'password' : 'text'}"
          value="${esc(initVal)}"
          placeholder="Ketik ${esc(f.label)} di sini..."
          autocomplete="off"
          onkeydown="if(event.key==='Enter'){event.preventDefault();submitQuickCredentials();}"
        />
        ${isSecret ? `<button type="button" class="qc-eye-btn" onclick="toggleQuickPass('${inpId}', this)">👁️</button>` : ''}
      </div>
    `;
    box.appendChild(row);
  });

  card.classList.remove('hidden');
  manualBar.classList.add('hidden');
  // Sembunyikan footer dobel saat kartu isi cepat tampil supaya 1 tombol jelas!
  footer.classList.add('hidden');

  setTimeout(() => {
    const target = document.getElementById(firstEmptyId || 'qc_inp_0');
    if (target) target.focus();
  }, 120);
}

function toggleQuickPass(inpId, btn) {
  const inp = document.getElementById(inpId);
  if (!inp) return;
  const toText = inp.type === 'password';
  inp.type = toText ? 'text' : 'password';
  btn.textContent = toText ? '🙈' : '👁️';
}

function toggleManualTypeBar() {
  const manualBar = document.getElementById('manualTypeBar');
  const footer = document.getElementById('sheetTakeoverFooter');
  manualBar.classList.toggle('hidden');
  if (appState.state === 'WAITING_HUMAN_TAKEOVER') {
    footer.classList.remove('hidden');
  }
}

async function submitQuickCredentials() {
  const btn = document.getElementById('quickCredSubmitBtn');
  const items = [];
  detectedCredFields.forEach((f, i) => {
    const inp = document.getElementById('qc_inp_' + i);
    if (inp && inp.value !== '') {
      items.push({ selector: f.selector, value: inp.value });
    }
  });

  if (!items.length) {
    alert('Ketik dulu Password / kodenya di kotak atas ya bro!');
    return;
  }

  const origTxt = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '⏳ Mengisi & Melanjutkan...';

  try {
    await fetch('./api/browser/fill-credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items, auto_resume: true }),
    });
    closeBrowserSheet();
  } finally {
    btn.disabled = false;
    btn.innerHTML = origTxt;
  }
}

function openBrowserSheet(mode, reasonText) {
  const sheet = document.getElementById('browserSheet');
  const title = document.getElementById('sheetTitle');
  const isLogin = mode === 'login' || appState.state === 'WAITING_HUMAN_TAKEOVER';
  if (isLogin) {
    title.textContent = '🔐 Isi Login Cepat';
  } else {
    title.textContent = '🖥️ Layar Browser Live';
  }
  document
    .getElementById('sheetTakeoverFooter')
    .classList.toggle('hidden', appState.state !== 'WAITING_HUMAN_TAKEOVER');
  sheet.classList.remove('hidden');
  redrawCanvas();
  loadQuickCredentialFields(isLogin);
}

function closeBrowserSheet() {
  document.getElementById('browserSheet').classList.add('hidden');
}

async function finishLoginAndResume() {
  closeBrowserSheet();
  await fetch('./api/agent/resume', { method: 'POST' });
}

function toggleZoom() {
  appState.zoomed = !appState.zoomed;
  document.getElementById('canvasWrap').classList.toggle('zoomed', appState.zoomed);
  const btn = document.getElementById('zoomBtn');
  btn.classList.toggle('active', appState.zoomed);
  btn.textContent = appState.zoomed ? '🔍 Perkecil' : '🔍 Perbesar';
}

/* ================= TOUCH & KEYBOARD IN BROWSER SHEET ================= */

function canvasCoords(clientX, clientY) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: ((clientX - rect.left) / rect.width) * canvas.width,
    y: ((clientY - rect.top) / rect.height) * canvas.height,
  };
}

let lastTapTs = 0;
let startTouchY = 0;
let draggedTouch = false;

canvas.addEventListener(
  'touchstart',
  (ev) => {
    if (ev.touches && ev.touches.length === 1) {
      startTouchY = ev.touches[0].clientY;
      draggedTouch = false;
    }
  },
  { passive: true }
);

canvas.addEventListener(
  'touchmove',
  (ev) => {
    if (appState.zoomed) return;
    if (ev.touches && ev.touches.length === 1) {
      const dy = startTouchY - ev.touches[0].clientY;
      if (Math.abs(dy) > 14) {
        draggedTouch = true;
        startTouchY = ev.touches[0].clientY;
        ev.preventDefault();
        wsSend({ type: 'human_wheel', dx: 0, dy: dy * 2.2 });
      }
    }
  },
  { passive: false }
);

canvas.addEventListener('touchend', (ev) => {
  if (ev.changedTouches && ev.changedTouches.length === 1 && !draggedTouch) {
    lastTapTs = Date.now();
    const t = ev.changedTouches[0];
    const { x, y } = canvasCoords(t.clientX, t.clientY);
    wsSend({ type: 'human_click', x, y, button: 'left', click_count: 1 });
  }
});

canvas.addEventListener('mousedown', (ev) => {
  if (Date.now() - lastTapTs < 500) return;
  canvas.focus();
  const { x, y } = canvasCoords(ev.clientX, ev.clientY);
  wsSend({ type: 'human_click', x, y, button: ev.button === 2 ? 'right' : 'left', click_count: ev.detail || 1 });
});

canvas.addEventListener(
  'wheel',
  (ev) => {
    ev.preventDefault();
    wsSend({ type: 'human_wheel', dx: ev.deltaX, dy: ev.deltaY });
  },
  { passive: false }
);

canvas.addEventListener('keydown', (ev) => {
  const keys = ['Enter', 'Backspace', 'Tab', 'Escape', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];
  if (keys.includes(ev.key)) {
    ev.preventDefault();
    wsSend({ type: 'human_key', key: ev.key });
  } else if (ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
    ev.preventDefault();
    wsSend({ type: 'human_type', text: ev.key });
  }
});

function sendMobileText() {
  const inp = document.getElementById('mobileKeyInput');
  if (!inp.value) return;
  wsSend({ type: 'human_type', text: inp.value });
  inp.value = '';
}

function sendBrowserKey(key) {
  wsSend({ type: 'human_key', key });
}

function scrollBrowser(dy) {
  wsSend({ type: 'human_wheel', dx: 0, dy });
}

/* ================= MENU & FILES SHEETS ================= */

function openMenuSheet() {
  const llm = appState.llm || {};
  const tg = appState.telegram || {};
  document.getElementById('cfgProvider').value = llm.provider || 'gemini';
  document.getElementById('cfgModel').value = llm.model || 'gemini-2.5-flash';
  document.getElementById('cfgApiKey').value = llm.has_api_key ? '********' : '';
  document.getElementById('llmKeyStatus').textContent = llm.has_api_key ? '(Sudah aktif)' : '';

  document.getElementById('tgStatusInfo').textContent = tg.configured
    ? `✅ Terhubung ke @${tg.bot_username || 'Bot'}!`
    : '';
  document.getElementById('menuModal').classList.remove('hidden');
}

function closeMenuSheet() {
  document.getElementById('menuModal').classList.add('hidden');
}

function onProviderPick() {
  const p = document.getElementById('cfgProvider').value;
  const map = {
    gemini: 'gemini-2.5-flash',
    openai: 'gpt-4o',
    anthropic: 'claude-sonnet-4-20250514',
    openrouter: 'google/gemini-2.5-flash',
  };
  document.getElementById('cfgModel').value = map[p] || 'gemini-2.5-flash';
}

async function saveLlmConfig() {
  const provider = document.getElementById('cfgProvider').value;
  const model = document.getElementById('cfgModel').value.trim();
  const api_key = document.getElementById('cfgApiKey').value;
  const base_url = provider === 'openrouter' ? 'https://openrouter.ai/api/v1' : '';
  const res = await fetch('./api/settings/llm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider, model, api_key, base_url }),
  });
  const data = await res.json();
  appState.llm = data.llm || {};
  closeMenuSheet();
}

async function saveTelegramToken() {
  const token = document.getElementById('tgTokenInput').value.trim();
  const info = document.getElementById('tgStatusInfo');
  info.style.color = '#94a3b8';
  info.textContent = 'Menghubungkan ke Telegram...';
  const res = await fetch('./api/settings/telegram', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, public_web_url: window.location.origin }),
  });
  const data = await res.json();
  if (!res.ok) {
    info.style.color = '#fca5a5';
    info.textContent = '❌ ' + (data.detail || 'Token tidak valid');
    return;
  }
  appState.telegram = data.telegram || {};
  info.style.color = '#34d399';
  info.textContent = appState.telegram.configured
    ? `✅ Aktif! Buka Telegram & chat ke @${appState.telegram.bot_username}`
    : 'Bot Telegram dimatikan.';
}

async function resetAllSession() {
  await fetch('./api/browser/reset', { method: 'POST' });
  document.getElementById('dynamicMessages').innerHTML = '';
  closeMenuSheet();
}

async function openFilesSheet() {
  const res = await fetch('./api/files');
  const data = await res.json();
  renderFilesSheet(data.files || []);
  document.getElementById('filesModal').classList.remove('hidden');
}

function closeFilesSheet() {
  document.getElementById('filesModal').classList.add('hidden');
}

function renderFilesSheet(files) {
  appState.files = files || [];
  const box = document.getElementById('filesSheetList');
  if (!box) return;
  if (!appState.files.length) {
    box.innerHTML = '<div style="color:#8696a0;font-size:13px;">Belum ada file tersimpan.</div>';
    return;
  }
  box.innerHTML = appState.files
    .map(
      (f) => `
      <div class="doc-card" style="margin-bottom:0;">
        <div class="doc-icon">📄</div>
        <div class="doc-info">
          <div class="doc-name">${esc(f.name)}</div>
          <div class="doc-size">${formatSize(f.size)}</div>
        </div>
        <button class="small-pill" onclick="openFileReader('${escJs(f.name)}')">Buka</button>
        <a class="small-pill" href="./api/files/${encodeURIComponent(f.name)}/download" download style="text-decoration:none;">⬇️</a>
      </div>
    `
    )
    .join('');
}

async function uploadFromPhone(ev) {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  await fetch('./api/files/upload', { method: 'POST', body: fd });
  ev.target.value = '';
  closeFilesSheet();
}

async function openFileReader(filename) {
  activeReaderFilename = filename;
  const res = await fetch(`./api/files/${encodeURIComponent(filename)}`);
  const data = await res.json();
  document.getElementById('readerTitle').textContent = '📄 ' + filename;
  document.getElementById('readerContent').value = data.content || '';
  document.getElementById('readerDownloadBtn').href = `./api/files/${encodeURIComponent(filename)}/download`;
  closeFilesSheet();
  document.getElementById('readerModal').classList.remove('hidden');
}

function closeReaderModal() {
  document.getElementById('readerModal').classList.add('hidden');
}

async function saveEditedFile() {
  if (!activeReaderFilename) return;
  const content = document.getElementById('readerContent').value;
  await fetch('./api/files', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: activeReaderFilename, content }),
  });
  closeReaderModal();
}

/* ================= HELPERS ================= */

function nowTime() {
  const d = new Date();
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}

function esc(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function escJs(s) {
  return String(s || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function formatMd(s) {
  let out = esc(s || '').replace(/\n/g, '<br/>');
  out = out.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  out = out.replace(/`([^`]+)`/g, '<code style="background:#111b21;padding:1px 5px;border-radius:4px;color:#53bdeb;">$1</code>');
  return out;
}

function formatSize(b) {
  const n = Number(b) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

/* ================= 1-TAP GITHUB PUBLISHER ================= */

let currentGhCode = '';
let currentGhVerifyUrl = 'https://github.com/login/device';

function openGithubModal() {
  document.getElementById('ghStepSetup').classList.remove('hidden');
  document.getElementById('ghStepCode').classList.add('hidden');
  document.getElementById('ghStepDone').classList.add('hidden');
  document.getElementById('githubModal').classList.remove('hidden');
}

function closeGithubModal() {
  document.getElementById('githubModal').classList.add('hidden');
}

async function startGithubDeviceFlow() {
  const btn = document.getElementById('ghStartBtn');
  const repo_name = document.getElementById('ghRepoName').value.trim() || 'asisten-browser';
  const privateRepo = document.getElementById('ghRepoVisibility').value === 'private';

  btn.disabled = true;
  btn.textContent = '⏳ Menyiapkan kode...';

  try {
    const res = await fetch('./api/github/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_name, private: privateRepo }),
    });
    const data = await res.json();
    if (!res.ok) {
      alert('Gagal menghubungi GitHub: ' + (data.detail || 'Error'));
      return;
    }
    currentGhCode = data.user_code || '';
    currentGhVerifyUrl = data.verification_uri || 'https://github.com/login/device';
    document.getElementById('ghUserCodeBox').textContent = currentGhCode;
    document.getElementById('ghLiveProgress').textContent = '⏳ Menunggu lu klik Authorize di GitHub...';
    document.getElementById('ghStepSetup').classList.add('hidden');
    document.getElementById('ghStepCode').classList.remove('hidden');
  } finally {
    btn.disabled = false;
    btn.textContent = '🚀 Buat Kode Sambung GitHub';
  }
}

function copyCodeAndOpenGithub() {
  if (currentGhCode && navigator.clipboard) {
    navigator.clipboard.writeText(currentGhCode).catch(() => {});
  }
  window.open(currentGhVerifyUrl, '_blank', 'noopener');
}

function handleGithubStatusEvent(ev) {
  if (ev.stage === 'pushing') {
    document.getElementById('ghLiveProgress').style.color = '#34d399';
    document.getElementById('ghLiveProgress').textContent = ev.message || 'Sedang upload file...';
  } else if (ev.stage === 'done') {
    document.getElementById('ghStepCode').classList.add('hidden');
    document.getElementById('ghStepDone').classList.remove('hidden');
    document.getElementById('ghDoneRepoUrl').textContent = ev.repo_url || '';
    document.getElementById('ghOpenRepoBtn').href = ev.repo_url || '#';
    document.getElementById('ghOpenCodespaceBtn').href = ev.codespace_url || '#';
    renderChatBubble({
      role: 'agent',
      text: `🎉 **Beres bro!** Semua kode aplikasi lu udah masuk ke GitHub:\n**${ev.repo_url}**`,
      time: nowTime(),
    });
  } else if (ev.stage === 'error') {
    document.getElementById('ghLiveProgress').style.color = '#fca5a5';
    document.getElementById('ghLiveProgress').textContent = ev.message || 'Gagal upload ke GitHub.';
  }
}

connectWs();
