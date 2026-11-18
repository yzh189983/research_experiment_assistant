/* 网页终端：xterm.js + WebSocket → 后端 paramiko shell 通道 */
(function () {
  const HOST = window.__HOST__ || {};
  const el = document.getElementById('terminal');
  const dot = document.getElementById('conn-dot');
  const statusEl = document.getElementById('term-status');
  const overlay = document.getElementById('overlay');
  const overlayMsg = document.getElementById('overlay-msg');
  const READ_ONLY = !!window.__READ_ONLY__;

  let ws = null;
  let retries = 0;
  let closedByUser = false;
  let pingTimer = null;
  let fontSize = Number(localStorage.getItem('rekit.term.fontSize') || 13);

  const term = new Terminal({
    fontFamily: '"SF Mono", ui-monospace, Menlo, Monaco, Consolas, "PingFang SC", monospace',
    fontSize: fontSize,
    lineHeight: 1.25,
    letterSpacing: 0,
    cursorBlink: true,
    cursorStyle: 'bar',
    scrollback: 5000,
    allowProposedApi: true,
    macOptionIsMeta: true,
    theme: {
      background: '#0E1110',
      foreground: '#D6D6CF',
      cursor: '#6FE0BE',
      cursorAccent: '#0E1110',
      selectionBackground: '#2A423B',
      black: '#22271F', red: '#E06C75', green: '#98C379', yellow: '#E5C07B',
      blue: '#61AFEF', magenta: '#C678DD', cyan: '#56B6C2', white: '#D6D6CF',
      brightBlack: '#5C6370', brightRed: '#EF7B85', brightGreen: '#A9D48C',
      brightYellow: '#F0CD8B', brightBlue: '#7CC0F5', brightMagenta: '#D08FE5',
      brightCyan: '#6FCAD6', brightWhite: '#F2F2EE'
    }
  });

  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);

  function doFit() {
    try { fit.fit(); } catch (e) { /* 容器还没尺寸时忽略 */ }
  }
  doFit();
  term.focus();

  // ---------- 连接 ----------
  function setDot(cls) {
    dot.className = 'term-dot ' + cls;
  }

  function showOverlay(msg, isErr) {
    overlayMsg.innerHTML = isErr ? `<span class="err">${escapeHtml(msg)}</span>` : escapeHtml(msg);
    overlay.classList.remove('hide');
  }

  function hideOverlay() {
    overlay.classList.add('hide');
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  async function connect() {
    if (ws) { try { ws.close(); } catch (e) { } }
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    closedByUser = false;
    setDot('wait');
    statusEl.textContent = '票据签发中…';
    hideOverlay();

    let ticket;
    try {
      const r = await api(`/api/terminal/${HOST.id}/ticket`, {
        method: 'POST',
        body: READ_ONLY ? { read_only: true } : {},
      });
      ticket = r.ticket;
    } catch (e) {
      setDot('off');
      statusEl.textContent = '票据获取失败';
      showOverlay(e.message || '无法获取终端票据', true);
      return;
    }

    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/terminal/${HOST.id}?cols=${term.cols}&rows=${term.rows}&ticket=${encodeURIComponent(ticket)}`;
    ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      retries = 0;
      setDot('on');
      statusEl.textContent = READ_ONLY ? '已连接（只读）' : '已连接';
      term.focus();
      doFit();
      sendCtl({ type: 'resize', cols: term.cols, rows: term.rows });
      if (pingTimer) clearInterval(pingTimer);
      pingTimer = setInterval(() => sendCtl({ type: 'ping' }), 15000);
    };

    ws.onmessage = ev => {
      if (typeof ev.data === 'string') {
        let m = null;
        try { m = JSON.parse(ev.data); } catch (e) { }
        if (m && m.type === 'error') {
          closedByUser = true;
          setDot('off');
          statusEl.textContent = '连接失败';
          showOverlay(m.message || '连接失败', true);
          return;
        }
        if (m && m.type === 'ready') {
          setDot('on');
          statusEl.textContent = m.read_only || READ_ONLY ? '已连接（只读）' : '已连接';
          if (m.read_only || READ_ONLY) {
            term.writeln('\x1b[38;5;245m[只读观察模式：输入已禁用]\x1b[0m');
          }
          return;
        }
        if (m && m.type === 'exit') {
          setDot('off');
          statusEl.textContent = '会话已结束';
          term.write('\r\n\x1b[38;5;245m[会话已结束]\x1b[0m\r\n');
          showOverlay('远程会话已结束。点「重新连接」开一个新会话。', false);
          return;
        }
        term.write(ev.data);
        return;
      }
      term.write(new Uint8Array(ev.data));
    };

    ws.onclose = () => {
      if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
      setDot('off');
      if (closedByUser) return;
      statusEl.textContent = '已断开';
      if (retries < 2) {
        retries++;
        statusEl.textContent = `重连中…(${retries}/2)`;
        setTimeout(connect, 700);
      } else {
        showOverlay('连接已断开。可能是网络中断，或服务器主动关闭了会话。', true);
      }
    };

    ws.onerror = () => {
      // onclose 会接着触发，这里不重复处理
    };
  }

  function sendCtl(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  function sendInput(data) {
    if (READ_ONLY) return;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'input', data }));
  }

  // ---------- 输入 ----------
  term.onData(sendInput);
  term.onBinary(d => sendInput(d));

  term.onResize(({ cols, rows }) => sendCtl({ type: 'resize', cols, rows }));

  // ---------- 尺寸 ----------
  let rt = null;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(doFit, 120);
  });

  // ---------- 工具栏 ----------
  function applyFont(delta) {
    fontSize = Math.min(22, Math.max(10, fontSize + delta));
    term.options.fontSize = fontSize;
    localStorage.setItem('rekit.term.fontSize', fontSize);
    doFit();
  }

  document.getElementById('btn-font-plus').onclick = () => applyFont(1);
  document.getElementById('btn-font-minus').onclick = () => applyFont(-1);
  document.getElementById('btn-reconnect').onclick = () => {
    term.reset();
    connect();
  };
  document.getElementById('btn-overlay-retry').onclick = () => {
    term.reset();
    connect();
  };

  window.addEventListener('beforeunload', () => {
    closedByUser = true;
    if (ws) ws.close();
  });

  connect();
})();
