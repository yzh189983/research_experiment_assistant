// 通用工具
async function api(url, opts = {}) {
  const cfg = { headers: {}, ...opts };
  // 通过 ?token= 链接访问时（无 session），让 API 也带上 token
  const urlToken = new URLSearchParams(location.search).get('token');
  if (urlToken && !cfg.headers['Authorization']) cfg.headers['Authorization'] = 'Bearer ' + urlToken;
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
  let activeConvId = null;         // 当前会话（由左侧 SuperAgent 设定）
  let selectedSkills = null;       // null=全部启用；数组=本次对话指定技能
  const sessionsBox = document.getElementById('kaka-sessions');
  const skillsBox = document.getElementById('kaka-skills');
  const skillsBtn = document.getElementById('kaka-skills-btn');
  const usageBox = document.getElementById('kaka-usage');
  const usageText = document.getElementById('kaka-usage-text');
  const usageFill = document.getElementById('kaka-usage-fill');
  const usageNote = document.getElementById('kaka-usage-note');
  const topicBar = document.getElementById('kaka-topic-bar');
  const ctxPanel = document.getElementById('kaka-ctx-panel');

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
    // 与左侧 SuperAgent 互斥：打开卡卡时隐藏 SA 入口，关闭时恢复
    const saBuddy = document.getElementById('sa-buddy');
    if (saBuddy) saBuddy.style.display = open ? 'none' : '';
    if (open) {
      const sp = document.getElementById('sa-panel');
      const so = document.getElementById('sa-overlay');
      if (sp) { sp.classList.remove('open'); sp.setAttribute('aria-hidden', 'true'); }
      if (so) so.classList.remove('open');
      say(scope === 'global' ? '有什么想聊的？' : '我在这儿，随时听你差遣。');
      if (!historyLoaded) loadHistory();
      loadUsage();
      setTimeout(() => input.focus(), 220);
    }
  }

  function addMsg(role, text, msgMeta) {
    const div = document.createElement('div');
    div.className = 'kaka-msg ' + role;
    div.textContent = text;
    // 57 上下文：助手消息附加「看依据」按钮；56 反馈：有用/需纠正
    if (role === 'assistant' && msgMeta && msgMeta.id) {
      const tools = document.createElement('div');
      tools.className = 'kaka-msg-tools';
      const ctxBtn = document.createElement('button');
      ctxBtn.className = 'kaka-mini'; ctxBtn.textContent = '看依据';
      ctxBtn.onclick = () => showContext(msgMeta.id);
      tools.appendChild(ctxBtn);
      ['useful', 'corrected'].forEach(fb => {
        const b = document.createElement('button');
        b.className = 'kaka-mini';
        b.textContent = fb === 'useful' ? '有用' : '需纠正';
        b.onclick = async () => {
          try {
            await api(`/api/kaka/message/${msgMeta.id}/feedback`, { method: 'POST', body: { feedback: fb } });
            tools.querySelectorAll('.kaka-mini').forEach(x => x.disabled = true);
            b.textContent = fb === 'useful' ? '✓ 已记下' : '✓ 已记录纠正';
          } catch (e) { toast(e.message, true); }
        };
        tools.appendChild(b);
      });
      div.appendChild(tools);
    }
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function setTopic(topic, routed) {
    if (!topicBar) return;
    if (!topic) { topicBar.style.display = 'none'; return; }
    topicBar.style.display = '';
    const detail = routed === 'existing' ? '⌁ 续接话题' : routed === 'new' ? '＋ 新话题' : '会话';
    topicBar.innerHTML = `<span class="kaka-sa-mark">⬡ SuperAgent</span> <span class="kaka-topic-tag">${esc(detail)}</span> <b>${esc(topic)}</b> <button class="kaka-sa-back" id="kaka-open-sa" title="打开左侧 SuperAgent 会话管理">会话管理</button>`;
    const openSa = document.getElementById('kaka-open-sa');
    if (openSa) openSa.onclick = () => {
      const saBuddy = document.getElementById('sa-buddy');
      if (saBuddy && window.__saSetOpen) window.__saSetOpen(true);
    };
  }

  function setUsage(u) {
    if (!usageBox || !u) return;
    usageBox.style.display = '';
    const pct = Math.max(0, Math.min(100, Number(u.percent) || 0));
    if (usageText) usageText.textContent = u.label || (u.used + ' / ' + u.limit);
    if (usageFill) {
      usageFill.style.width = pct + '%';
      usageFill.classList.toggle('warn', pct >= 60 && pct < 85);
      usageFill.classList.toggle('danger', pct >= 85);
    }
    if (usageNote) {
      const src = u.source === 'api' ? '模型实际用量' : '本地估算';
      usageNote.textContent = src + ' · ' + pct.toFixed(1) + '% · ' + (u.model || '当前模型');
    }
  }

  async function loadUsage() {
    if (!usageBox) return;
    const cid = activeConvId ? '&conversation_id=' + activeConvId : '';
    try {
      setUsage(await api('/api/kaka/context-usage?scope=' + encodeURIComponent(scope) + cid));
    } catch (e) { /* 用量条获取失败不影响对话 */ }
  }

  async function showContext(messageId) {
    if (!ctxPanel) return;
    ctxPanel.style.display = '';
    ctxPanel.innerHTML = '<div class="tiny faint">加载依据…</div>';
    try {
      const r = await api(`/api/kaka/message/${messageId}/context`);
      const c = r.context || {};
      const parts = [];
      parts.push('<div class="kaka-ctx-head">卡卡这次看到了什么 <button class="kaka-pop-close" id="kaka-ctx-close">×</button></div>');
      if (c.system) parts.push(`<div class="kaka-ctx-row"><b>提示体系</b><span>${esc(c.system)}</span></div>`);
      if (c.experiment) parts.push(`<div class="kaka-ctx-row"><b>实验</b><span>${esc(c.experiment.title || '')} · ${esc(c.experiment.status || '')}</span></div>`);
      if (c.project) parts.push(`<div class="kaka-ctx-row"><b>项目</b><span>${esc(c.project.name || '')}</span></div>`);
      if ((c.metrics || []).length) parts.push('<div class="kaka-ctx-row"><b>指标</b><span>' + c.metrics.map(m => `${esc(m.name)}=${m.last == null ? '—' : esc(String(m.last))}（${m.points}点）`).join('；') + '</span></div>');
      if ((c.log_lines || []).length) parts.push('<div class="kaka-ctx-row"><b>日志尾行</b><pre>' + esc(c.log_lines.map(l => l.replace(/&/g,'&amp;').replace(/</g,'&lt;')).join('\n')) + '</pre></div>');
      if (c.context) parts.push(`<div class="kaka-ctx-row"><b>上下文摘要</b><pre>${esc(String(c.context).slice(0, 600))}</pre></div>`);
      const meta = r.meta || {};
      if ((meta.skills || []).length) parts.push(`<div class="kaka-ctx-row"><b>启用技能</b><span>${meta.skills.map(esc).join('、')}</span></div>`);
      if (meta.context_usage) parts.push(`<div class="kaka-ctx-row"><b>上下文用量</b><span>${esc(meta.context_usage.label)}（${meta.context_usage.source === 'api' ? '模型实际用量' : '本地估算'}）</span></div>`);
      parts.push('<div class="kaka-ctx-note">以上均为可核验来源；AI 推断会单独标注，不等同实测。</div>');
      ctxPanel.innerHTML = parts.join('');
      document.getElementById('kaka-ctx-close').onclick = () => ctxPanel.style.display = 'none';
    } catch (e) { ctxPanel.innerHTML = '<div class="tiny" style="color:var(--err)">' + esc(e.message) + '</div>'; }
  }

  async function loadHistory() {
    historyLoaded = true;
    try {
      const url = '/api/kaka/history?scope=' + encodeURIComponent(scope) +
        (activeConvId ? '&conversation_id=' + activeConvId : '');
      const r = await api(url);
      const items = r.items || [];
      if (!items.length) return;
      log.innerHTML = '';
      items.forEach(m => addMsg(m.role, m.content, m.meta ? { id: m.id } : null));
    } catch (e) { /* 历史加载失败不挡新对话 */ }
  }

  // performSend：mode='auto' 让 SuperAgent 自动路由话题；'session' 在指定会话内问
  async function performSend(q, mode) {
    q = String(q || '').trim();
    if (!q || loading) return;
    loading = true;
    sendBtn.disabled = true;
    addMsg('user', q);
    input.value = '';
    const saSend = document.getElementById('sa-send');
    if (saSend) saSend.disabled = true;
    const saInput = document.getElementById('sa-input');
    if (saInput) saInput.value = '';
    const thinking = document.createElement('div');
    thinking.className = 'kaka-thinking';
    thinking.textContent = mode === 'auto' ? 'SuperAgent 正在归类话题并思考…' : '卡卡正在想…';
    log.appendChild(thinking);
    log.scrollTop = log.scrollHeight;
    try {
      const body = { message: q, scope };
      if (mode !== 'auto' && activeConvId) body.conversation_id = activeConvId;
      if (selectedSkills) body.skill_keys = selectedSkills;
      const r = await api('/api/kaka/chat', { method: 'POST', body });
      thinking.remove();
      // auto 模式：SuperAgent 路由到哪个会话就进入哪个会话
      if (mode === 'auto' && r.conversation_id) activeConvId = r.conversation_id;
      if (input) input.placeholder = activeConvId
        ? `在会话「${r.topic || '当前话题'}」中提问…（Enter 发送）`
        : '问卡卡…（Enter 发送，Shift+Enter 换行）';
      setTopic(r.topic, r.routed);
      if (r.context_usage) setUsage(r.context_usage);
      addMsg('assistant', r.answer || r.error || '（没有回复）', { id: r.message_id });
      say(mode === 'auto' ? '话题已归类，答复来啦' : '我找到答案啦');
      if (window.__saRefresh) window.__saRefresh();
      buddy.classList.add('nod');
      setTimeout(() => buddy.classList.remove('nod'), 700);
    } catch (err) {
      thinking.remove();
      addMsg('assistant', '抱歉，刚才没连上：' + err.message);
    } finally {
      loading = false;
      sendBtn.disabled = false;
      if (saSend) saSend.disabled = false;
      input.focus();
    }
  }

  async function send() {
    await performSend(input.value, activeConvId ? 'session' : 'auto');
  }

  // 暴露给左侧 SuperAgent 面板的桥接：直问（自动路由）/ 进入会话 / 打开卡卡
  window.__kakaBridge = {
    sendAuto: q => { setOpen(true); performSend(q, 'auto'); },
    enterConversation: (cid, topic) => {
      activeConvId = Number(cid);
      setTopic(topic, 'explicit');
      historyLoaded = false;
      loadHistory();
      loadUsage();
      if (input) input.placeholder = `在会话「${topic}」中提问…（Enter 发送）`;
      setOpen(true);
    },
    exitConversation: () => {
      activeConvId = null;
      setTopic('', '');
      if (input) input.placeholder = '问卡卡…（Enter 发送，Shift+Enter 换行）';
      loadUsage();
    },
    getConvId: () => activeConvId,
    open: () => setOpen(true),
    scope: () => scope,
  };

  // 55 对话技能选择：本次聊天用哪些技能
  async function renderSkillsPanel() {
    if (!skillsBox) return;
    skillsBox.innerHTML = '<div class="tiny faint">加载技能…</div>';
    try {
      const d = await api('/api/kaka/skills');
      const items = (d.installed || []).filter(s => s.enabled !== false);
      skillsBox.innerHTML = `
        <div class="kaka-skills-head">
          <span>本次对话使用的技能</span>
          <button class="kaka-mini" id="kaka-skills-all">全选</button>
          <button class="kaka-mini" id="kaka-skills-none">清空</button>
        </div>
        <div class="kaka-skills-hint">不选（清空）= 使用你已启用的全部技能</div>
        <div class="kaka-skills-list">
          ${items.length ? items.map(s => `
            <label class="kaka-skill-opt">
              <input type="checkbox" value="${esc(s.key)}" ${selectedSkills ? (selectedSkills.includes(s.key) ? 'checked' : '') : 'checked'}>
              <span>${esc(s.name)}</span>
              ${s.source === 'clawhub' ? '<em>ClawHub</em>' : ''}
            </label>`).join('') : '<div class="tiny faint">还没有启用的技能，去「设置 → 卡卡技能」添加。</div>'}
        </div>
        <div class="kaka-skills-foot"><button class="btn primary sm" id="kaka-skills-done">完成</button></div>`;
      skillsBox.querySelectorAll('input[type=checkbox]').forEach(cb => cb.onchange = () => {
        const boxes = Array.from(skillsBox.querySelectorAll('input[type=checkbox]'));
        const checked = boxes.filter(b => b.checked).map(b => b.value);
        selectedSkills = checked.length === boxes.length ? null : checked; // 全选等同默认全部
      });
      document.getElementById('kaka-skills-all').onclick = () => {
        skillsBox.querySelectorAll('input[type=checkbox]').forEach(b => b.checked = true);
        selectedSkills = null;
      };
      document.getElementById('kaka-skills-none').onclick = () => {
        skillsBox.querySelectorAll('input[type=checkbox]').forEach(b => b.checked = false);
        selectedSkills = [];
      };
      document.getElementById('kaka-skills-done').onclick = () => { skillsBox.style.display = 'none'; };
    } catch (e) { skillsBox.innerHTML = '<div class="tiny" style="color:var(--err)">' + esc(e.message) + '</div>'; }
  }
  if (skillsBtn) skillsBtn.addEventListener('click', () => {
    if (sessionsBox) sessionsBox.style.display = 'none';
    skillsBox.style.display = skillsBox.style.display === 'none' ? '' : 'none';
    if (skillsBox.style.display !== 'none') renderSkillsPanel();
  });

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

// ---------------- SuperAgent：左侧独立侧边栏 ----------------
(function initSuperAgent() {
  const buddy = document.getElementById('sa-buddy');
  const panel = document.getElementById('sa-panel');
  const overlay = document.getElementById('sa-overlay');
  if (!buddy || !panel || !overlay) return;
  const input = document.getElementById('sa-input');
  const sendBtn = document.getElementById('sa-send');
  const listBox = document.getElementById('sa-list');
  const topicBar = document.getElementById('sa-topic');
  const closeBtn = document.getElementById('sa-close');
  const trashBtn = document.getElementById('sa-trash-btn');
  const sectionTitle = document.getElementById('sa-section-title');
  const boxBack = document.getElementById('sa-box-back');
  let trashMode = false;

  function setOpen(open) {
    panel.classList.toggle('open', open);
    overlay.classList.toggle('open', open);
    buddy.setAttribute('aria-expanded', open ? 'true' : 'false');
    panel.setAttribute('aria-hidden', open ? 'false' : 'true');
    buddy.classList.toggle('active', open);
    // 与右侧卡卡互斥：打开 SA 时隐藏卡卡入口，关闭时恢复
    const kakaBuddy = document.getElementById('kaka-buddy');
    if (kakaBuddy) kakaBuddy.style.display = open ? 'none' : '';
    if (open) {
      const kp = document.getElementById('kaka-pop');
      const ko = document.getElementById('kaka-overlay');
      if (kp) { kp.classList.remove('open'); kp.setAttribute('aria-hidden', 'true'); }
      if (ko) ko.classList.remove('open');
      loadSessions(); setTimeout(() => input && input.focus(), 200);
    }
  }

  function setTopicBar(topic, detail) {
    if (!topicBar) return;
    if (!topic) { topicBar.style.display = 'none'; return; }
    topicBar.style.display = '';
    topicBar.innerHTML = `<span class="sa-topic-mark">已选会话</span> <b>${esc(topic)}</b> <span class="sa-topic-hint">${esc(detail || '')}</span> <button class="sa-topic-back" id="sa-topic-back">返回全局</button>`;
    const back = document.getElementById('sa-topic-back');
    if (back) back.onclick = () => {
      if (window.__kakaBridge) window.__kakaBridge.exitConversation();
      setTopicBar('', '');
      markActive(null);
      loadSessions();
    };
  }

  function markActive(cid) {
    if (!listBox) return;
    listBox.querySelectorAll('.sa-session').forEach(el =>
      el.classList.toggle('active', Number(el.dataset.id) === Number(cid)));
  }

  async function loadSessions() {
    if (!listBox) return;
    listBox.innerHTML = '<div class="tiny faint">加载会话…</div>';
    if (trashBtn) trashBtn.classList.toggle('on', trashMode);
    if (sectionTitle) sectionTitle.textContent = trashMode ? '垃圾箱' : '最近一周会话';
    if (boxBack) boxBack.style.display = trashMode ? '' : 'none';
    try {
      const r = await api('/api/kaka/conversations?days=7' + (trashMode ? '&status=trash' : ''));
      const items = r.items || [];
      if (!items.length) {
        listBox.innerHTML = '<div class="tiny faint">' + (trashMode
          ? '垃圾箱是空的。会话移到垃圾箱后可以在这里恢复或彻底删除。'
          : '最近一周还没有会话。上面直接问一句，SuperAgent 会自动建会话。') + '</div>';
        return;
      }
      const current = window.__kakaBridge ? window.__kakaBridge.getConvId() : null;
      listBox.innerHTML = items.map(s => `
        <div class="sa-session ${!trashMode && current === s.id ? 'active' : ''}" data-id="${s.id}">
          <div class="sa-session-topic">${esc(s.topic)}</div>
          <div class="sa-session-preview">${esc(s.preview || '')}</div>
          <div class="sa-session-ops">${trashMode
            ? '<button type="button" class="sa-mini" data-op="restore">恢复</button><button type="button" class="sa-mini danger" data-op="delete">彻底删除</button>'
            : '<button type="button" class="sa-mini" data-op="trash">移到垃圾箱</button>'}</div>
        </div>`).join('');
      listBox.querySelectorAll('.sa-session').forEach(el => {
        if (trashMode) {
          const restoreBtn = el.querySelector('[data-op=restore]');
          const delBtn = el.querySelector('[data-op=delete]');
          if (restoreBtn) restoreBtn.onclick = e => {
            e.stopPropagation(); sessionOp(el.dataset.id, '/restore', '会话已恢复');
          };
          if (delBtn) delBtn.onclick = e => {
            e.stopPropagation(); hardDelete(el.dataset.id);
          };
          return;
        }
        el.onclick = () => {
          const topic = el.querySelector('.sa-session-topic').textContent;
          if (window.__kakaBridge) window.__kakaBridge.enterConversation(el.dataset.id, topic);
          markActive(el.dataset.id);
          setTopicBar(topic, '在卡卡对话里继续提问');
          setOpen(false);
          toast('已进入会话「' + topic + '」，在卡卡里继续提问');
        };
        const tBtn = el.querySelector('[data-op=trash]');
        if (tBtn) tBtn.onclick = e => {
          e.stopPropagation(); sessionOp(el.dataset.id, '/trash', '会话已移到垃圾箱');
        };
      });
    } catch (e) { listBox.innerHTML = '<div class="tiny" style="color:var(--err)">' + esc(e.message) + '</div>'; }
  }

  async function sessionOp(cid, path, msg) {
    try {
      const r = await api(`/api/kaka/conversations/${cid}${path}`, { method: 'POST' });
      if (!r.ok) throw new Error(r.error || '操作失败');
      toast(msg);
      const bridge = window.__kakaBridge;
      if (path === '/trash' && bridge && Number(bridge.getConvId()) === Number(cid)) {
        bridge.exitConversation();
        setTopicBar('', '');
        markActive(null);
      }
      await loadSessions();
    } catch (e) { toast(e.message, true); }
  }

  async function hardDelete(cid) {
    if (!confirm('彻底删除这个会话？其中的消息将无法恢复。')) return;
    try {
      await api(`/api/kaka/conversations/${cid}`, { method: 'DELETE' });
      toast('会话已彻底删除');
      await loadSessions();
    } catch (e) { toast(e.message, true); }
  }

  if (trashBtn) trashBtn.onclick = () => { trashMode = !trashMode; loadSessions(); };
  if (boxBack) boxBack.onclick = () => { trashMode = false; loadSessions(); };

  async function ask() {
    const q = String(input ? input.value : '').trim();
    if (!q) return;
    if (sendBtn) sendBtn.disabled = true;
    if (input) input.value = '';
    if (window.__kakaBridge) {
      window.__kakaBridge.sendAuto(q);   // 自动路由话题，回答显示在卡卡对话
      setTopicBar('', '');
      markActive(null);
      setTimeout(loadSessions, 1500);
      setOpen(false);
      toast('SuperAgent 已归类话题，答复在卡卡对话中');
    }
    if (sendBtn) sendBtn.disabled = false;
  }

  if (sendBtn) sendBtn.addEventListener('click', ask);
  if (input) input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); ask(); } });
  buddy.addEventListener('click', () => setOpen(!panel.classList.contains('open')));
  if (closeBtn) closeBtn.addEventListener('click', () => setOpen(false));
  overlay.addEventListener('click', () => setOpen(false));
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && panel.classList.contains('open')) setOpen(false); });
  window.__saRefresh = () => { if (panel.classList.contains('open')) loadSessions(); };
  window.__saSetOpen = setOpen;
})();
