// 通用工具
async function api(url, opts = {}) {
  const cfg = { headers: {}, ...opts };
  if (opts.body && typeof opts.body !== 'string') {
    cfg.body = JSON.stringify(opts.body);
    cfg.headers['Content-Type'] = 'application/json';
  }
  const r = await fetch(url, cfg);
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.error) || `请求失败 ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg, isErr = false) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 2800);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtDuration(sec) {
  if (!sec || sec < 0) return '—';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = Math.floor(sec % 60);
  if (h > 0) return `${h}h${m}m`;
  if (m > 0) return `${m}m${s}s`;
  return `${s}s`;
}

// 状态徽章（文案走 i18n 字典，中文为默认）
const STATUS_CLASS = {
  draft: 'idle', planned: 'idle', coded: 'idle',
  running: 'run', uploading: 'run', setup: 'run', pending: 'run',
  done: 'ok', finished: 'ok', failed: 'err', lost: 'err', stopped: 'warn',
};
function statusLabel(st) {
  const t = (window.I18n && I18n.t) || ((k, f) => f);
  return t('status.' + (st || 'unknown'), st || t('status.unknown', '未知'));
}
function statusBadge(st) {
  const cls = STATUS_CLASS[st] || 'idle';
  return `<span class="badge ${cls}"><i class="dot"></i>${esc(statusLabel(st))}</span>`;
}

// 极简 canvas 折线图
function drawLine(canvas, pts, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth, H = opts.height || 72;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!pts || pts.length < 2) return;

  const pad = { l: 2, r: 2, t: 6, b: 6 };
  const ys = pts.map(p => p.v);
  let min = Math.min(...ys), max = Math.max(...ys);
  if (max - min < 1e-9) { max += 1; min -= 1; }
  const span = max - min;
  const x = i => pad.l + (i / (pts.length - 1)) * (W - pad.l - pad.r);
  const y = v => pad.t + (1 - (v - min) / span) * (H - pad.t - pad.b);

  // 网格（发丝线，别抢曲线）
  ctx.strokeStyle = '#E9E7DE'; ctx.lineWidth = 1;
  for (let i = 0; i <= 2; i++) {
    const yy = pad.t + (i / 2) * (H - pad.t - pad.b);
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(W - pad.r, yy); ctx.stroke();
  }

  // 面积
  const color = opts.color || '#0F5F46';
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, color + '22');
  grad.addColorStop(1, color + '00');
  ctx.beginPath();
  ctx.moveTo(x(0), y(pts[0].v));
  pts.forEach((p, i) => ctx.lineTo(x(i), y(p.v)));
  ctx.lineTo(x(pts.length - 1), H - pad.b);
  ctx.lineTo(x(0), H - pad.b);
  ctx.closePath(); ctx.fillStyle = grad; ctx.fill();

  // 线
  ctx.beginPath();
  ctx.moveTo(x(0), y(pts[0].v));
  pts.forEach((p, i) => ctx.lineTo(x(i), y(p.v)));
  ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.lineJoin = 'round'; ctx.stroke();

  // 末点
  const last = pts[pts.length - 1];
  ctx.beginPath();
  ctx.arc(x(pts.length - 1), y(last.v), 2.6, 0, Math.PI * 2);
  ctx.fillStyle = color; ctx.fill();
}

// ---------------- 卡卡助手：可爱形象 + 多轮对话抽屉 ----------------
(function initKakaBuddy() {
  const buddy = document.getElementById('kaka-buddy');
  const pop = document.getElementById('kaka-pop');
  const overlay = document.getElementById('kaka-overlay');
  if (!buddy || !pop || !overlay) return;
  const bubble = document.getElementById('kaka-bubble');
  const log = document.getElementById('kaka-chat-log');
  const input = document.getElementById('kaka-pop-input');
  const sendBtn = document.getElementById('kaka-pop-send');
  const intro = log ? log.innerHTML : '';
  const path = location.pathname;
  const expMatch = path.match(/\/exp\/(\d+)/);
  const projMatch = path.match(/\/project\/(\d+)/);
  const scope = expMatch ? `exp:${expMatch[1]}`
    : projMatch ? `project:${projMatch[1]}` : 'global';
  let bubbleTimer, loading = false, historyLoaded = false;

  function say(text) {
    bubble.textContent = text;
    buddy.classList.add('speaking');
    clearTimeout(bubbleTimer);
    bubbleTimer = setTimeout(() => buddy.classList.remove('speaking'), 2400);
  }

  function setOpen(open) {
    pop.classList.toggle('open', open);
    overlay.classList.toggle('open', open);
    buddy.setAttribute('aria-expanded', open ? 'true' : 'false');
    pop.setAttribute('aria-hidden', open ? 'false' : 'true');
    buddy.classList.toggle('active', open);
    if (open) {
      say(scope === 'global' ? '有什么想聊的？' : '我在这儿，随时听你差遣。');
      if (!historyLoaded) loadHistory();
      setTimeout(() => input.focus(), 220);
    }
  }

  function addMsg(role, text) {
    const div = document.createElement('div');
    div.className = 'kaka-msg ' + role;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  async function loadHistory() {
    historyLoaded = true;
    try {
      const r = await api('/api/kaka/history?scope=' + encodeURIComponent(scope));
      const items = r.items || [];
      if (!items.length) return;
      log.innerHTML = '';
      items.forEach(m => addMsg(m.role, m.content));
    } catch (e) { /* 历史加载失败不挡新对话 */ }
  }

  async function send() {
    const q = input.value.trim();
    if (!q || loading) return;
    loading = true;
    sendBtn.disabled = true;
    addMsg('user', q);
    input.value = '';
    const thinking = document.createElement('div');
    thinking.className = 'kaka-thinking';
    thinking.textContent = '卡卡正在想…';
    log.appendChild(thinking);
    log.scrollTop = log.scrollHeight;
    try {
      const r = await api('/api/kaka/chat', { method: 'POST', body: { message: q, scope } });
      thinking.remove();
      addMsg('assistant', r.answer || r.error || '（没有回复）');
      say('我找到答案啦');
      buddy.classList.add('nod');
      setTimeout(() => buddy.classList.remove('nod'), 700);
    } catch (err) {
      thinking.remove();
      addMsg('assistant', '抱歉，刚才没连上：' + err.message);
    } finally {
      loading = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  buddy.addEventListener('click', () => setOpen(!pop.classList.contains('open')));
  overlay.addEventListener('click', () => setOpen(false));
  document.getElementById('kaka-pop-close').addEventListener('click', () => setOpen(false));
  document.getElementById('kaka-clear').addEventListener('click', async () => {
    if (!confirm('清空当前对话记录？')) return;
    try {
      await api('/api/kaka/history?scope=' + encodeURIComponent(scope), { method: 'DELETE' });
      log.innerHTML = intro;
      toast('对话已清空');
    } catch (e) { toast(e.message, true); }
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') setOpen(false); });
  sendBtn.addEventListener('click', send);
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  document.addEventListener('kaka:open', () => setOpen(true));

  // 眼睛跟着鼠标走：rAF 节流，位移限制在脸内，眨眼动画会保留这个偏移
  let eyeFrame = 0;
  document.addEventListener('mousemove', e => {
    if (eyeFrame) return;
    eyeFrame = requestAnimationFrame(() => {
      eyeFrame = 0;
      const face = buddy.querySelector('.kaka-face');
      if (!face) return;
      const r = face.getBoundingClientRect();
      const dx = e.clientX - (r.left + r.width / 2);
      const dy = e.clientY - (r.top + r.height / 2);
      const dist = Math.hypot(dx, dy) || 1;
      const reach = Math.min(3.2, dist / 45);
      const root = document.documentElement.style;
      root.setProperty('--kx', ((dx / dist) * reach).toFixed(2) + 'px');
      root.setProperty('--ky', ((dy / dist) * reach).toFixed(2) + 'px');
    });
  });

  // 实验页 SSE 收到运行事件时，让卡卡点头提醒
  document.addEventListener('kaka:alert', () => {
    say('有实验动静，快看卡卡工作台');
    buddy.classList.add('nod');
    setTimeout(() => buddy.classList.remove('nod'), 700);
  });
})();
