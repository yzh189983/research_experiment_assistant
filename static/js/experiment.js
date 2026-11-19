/* 实验详情页交互：方案生成 → 代码编辑 → 运行监控 → 结论 */
const EXP_ID = window.__EXP__.id;
const HOSTS = window.__HOSTS__ || [];
let EXP = window.__EXP__;
let LLM_OK = window.__LLM_OK__;

const S = {
  series: {},          // name -> [{v,s,t}]
  files: EXP.files || [],
  curFile: null,
  dirty: false,
  status: EXP.status,
  run: EXP.latest_run,
  clockSkew: (window.__EXP__.server_now * 1000) - Date.now(),
  es: null,
  drawTimer: null,
  logCount: 0,
  logLines: [],        // 全量日志行 {text, kind}，供搜索/筛选/折叠
  logFilter: 'all',
  logSearch: '',
  logFold: false,
};

const $ = id => document.getElementById(id);

/* ---------------- 渲染：方案 ---------------- */
function renderPlan() {
  const box = $('plan-body');
  const p = EXP.plan;
  if (!p) {
    box.innerHTML = `<div class="empty">还没有实验方案。点右上角「生成方案」，系统会把你的创新点拆成可验证的假设、指标和实现步骤。</div>`;
    $('num-plan').className = 'sec-num';
    $('btn-code').disabled = true;
    return;
  }
  $('num-plan').className = 'sec-num done';
  $('num-plan').textContent = '✓';
  $('btn-code').disabled = false;

  const metrics = (p.metrics || []).map(m =>
    `<span class="chip acc" title="${esc(m.desc)}">${esc(m.name)}${m.higher_better ? ' ↑' : ' ↓'}</span>`).join('');
  const datasets = (p.datasets || []).map(d =>
    `<span class="chip" title="${esc(d.note)}">${esc(d.name)}</span>`).join('');
  const list = (arr) => arr && arr.length
    ? `<ul class="tight">${arr.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : '<span class="faint">—</span>';

  const impl = p.implementation || {};
  const vars = p.variables || {};

  box.innerHTML = `
    <div class="plan-block">
      <div class="plan-label">核心命题</div>
      <div class="claim">${esc(p.core_claim || p.title)}</div>
      ${p.novelty ? `<div class="tiny muted" style="margin-top:8px">新意：${esc(p.novelty)}</div>` : ''}
    </div>
    <div class="plan-block">
      <div class="plan-label">可检验假设</div>
      ${list(p.hypothesis)}
    </div>
    <div class="plan-block">
      <div class="plan-label">数据集 / 指标</div>
      <div class="chips" style="margin-bottom:10px">${datasets || '<span class="faint">未指定</span>'}</div>
      <div class="chips">${metrics}</div>
    </div>
    <div class="plan-block">
      <div class="plan-label">对照与消融</div>
      <dl class="kv">
        <dt>基线</dt><dd>${(p.baselines || []).map(esc).join('、') || '—'}</dd>
        <dt>消融</dt><dd>${(p.ablations || []).map(esc).join('、') || '—'}</dd>
        <dt>自变量</dt><dd>${esc(vars.independent || '—')}</dd>
        <dt>因变量</dt><dd>${esc(vars.dependent || '—')}</dd>
        <dt>控制变量</dt><dd>${esc(vars.controlled || '—')}</dd>
      </dl>
    </div>
    <div class="plan-block">
      <div class="plan-label">实现步骤 · ${esc(impl.framework || '')}</div>
      ${list(impl.steps)}
      ${impl.estimated_runtime ? `<div class="tiny faint" style="margin-top:8px">预计耗时：${esc(impl.estimated_runtime)}</div>` : ''}
    </div>
    <div class="plan-block">
      <div class="plan-label">成功判据 / 失败判据</div>
      <dl class="kv">
        <dt>成功</dt><dd>${esc(p.success_criteria || '—')}</dd>
        <dt>失败</dt><dd>${esc(p.failure_criteria || '—')}</dd>
      </dl>
      ${(p.risks && p.risks.length) ? `<div style="margin-top:10px"><div class="plan-label">风险</div>${list(p.risks)}</div>` : ''}
    </div>
    <div class="plan-block">
      <div class="plan-label">不满意？直接说</div>
      <div style="display:flex;gap:8px">
        <input type="text" id="plan-feedback" placeholder="例：数据集体量再小一点 / 加一个随机初始化的消融 / 指标换成 AUROC">
        <button class="btn sm" id="btn-replan-2" style="flex-shrink:0">按意见重做</button>
      </div>
    </div>`;

  const rp2 = box.querySelector('#btn-replan-2');
  if (rp2) {
    rp2.onclick = () => {
      const v = box.querySelector('#plan-feedback').value.trim();
      if (!v) return toast('先写一句你的意见', true);
      doPlan(v);
    };
  }
}

/* ---------------- 渲染：代码 ---------------- */
function renderCode() {
  const box = $('code-body');
  if (!EXP.code || !S.files.length) {
    box.innerHTML = `<div class="empty">还没有代码。生成方案后点「生成代码」，系统会写出可无人值守运行、并按协议上报指标的实验脚本。</div>`;
    $('num-code').className = 'sec-num';
    $('btn-save-code').style.display = 'none';
    return;
  }
  $('num-code').className = 'sec-num done';
  $('num-code').textContent = '✓';

  const names = S.files.map(f => f.path);
  if (!S.curFile || !names.includes(S.curFile)) {
    S.curFile = names.includes('main.py') ? 'main.py' : names[0];
  }

  const tabs = S.files.map(f =>
    `<div class="code-tab ${f.path === S.curFile ? 'on' : ''}" data-p="${esc(f.path)}">${esc(f.path)}</div>`
  ).join('');

  box.innerHTML = `
    <div class="code-tabs">${tabs}</div>
    <textarea class="code-area" id="code-editor" spellcheck="false"></textarea>
    <div style="padding:10px 18px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;gap:12px">
      <span class="tiny faint" id="code-note">${esc((EXP.code.notes || '').slice(0, 160))}</span>
      <span class="tiny faint mono" id="code-size" style="flex-shrink:0"></span>
    </div>
    <div style="padding:11px 18px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center;background:var(--surface-2)">
      <input type="text" id="code-feedback" placeholder="让代码改点什么？例：别用 torch，太慢 / 加一个日志打印 / 换成真实数据集"
             style="flex:1;background:var(--surface)">
      <button class="btn sm" id="btn-recode-2" style="flex-shrink:0">按意见重写</button>
    </div>`;

  const ta = $('code-editor');
  ta.addEventListener('input', () => { S.dirty = true; $('btn-save-code').style.display = ''; updateSize(); });
  ta.addEventListener('keydown', e => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const s = ta.selectionStart, en = ta.selectionEnd;
      ta.value = ta.value.slice(0, s) + '    ' + ta.value.slice(en);
      ta.selectionStart = ta.selectionEnd = s + 4;
      S.dirty = true;
    }
  });

  box.querySelectorAll('.code-tab').forEach(t => {
    t.onclick = async () => {
      if (S.dirty) await saveCode(true);
      S.curFile = t.dataset.p;
      renderCode();
    };
  });

  const fbBtn = box.querySelector('#btn-recode-2');
  if (fbBtn) fbBtn.onclick = () => doCode(box.querySelector('#code-feedback').value);

  loadFile(S.curFile);
}

async function loadFile(p) {
  try {
    const r = await api(`/api/experiments/${EXP_ID}/file?path=${encodeURIComponent(p)}`);
    const ta = $('code-editor');
    if (!ta) return;
    ta.value = r.content || '';
    S.dirty = false;
    $('btn-save-code').style.display = 'none';
    updateSize();
  } catch (e) { toast(e.message, true); }
}

function updateSize() {
  const ta = $('code-editor'), el = $('code-size');
  if (ta && el) {
    const lines = ta.value.split('\n').length;
    el.textContent = `${lines} 行 · ${(new Blob([ta.value]).size / 1024).toFixed(1)} KB`;
  }
}

async function saveCode(silent) {
  const ta = $('code-editor');
  if (!ta || !S.dirty) return;
  await api(`/api/experiments/${EXP_ID}/file`, {
    method: 'POST', body: { path: S.curFile, content: ta.value }
  });
  S.dirty = false;
  $('btn-save-code').style.display = 'none';
  if (!silent) toast('已保存');
}

/* ---------------- 渲染：结论 ---------------- */
function renderSummary() {
  const box = $('summary-body');
  const s = EXP.summary;
  if (!s) {
    box.innerHTML = `<div class="empty">实验跑完会自动在这里生成结论：假设是否成立、关键数据、异常、局限性、下一步和一段可直接进论文的结果段落。</div>`;
    $('num-sum').className = 'sec-num';
    $('btn-summarize').style.display = 'none';
    $('btn-export').style.display = 'none';
    return;
  }
  if (s.error) {
    box.innerHTML = `<div class="empty" style="color:var(--err)">总结失败：${esc(s.error)}</div>`;
    return;
  }
  $('num-sum').className = 'sec-num done';
  $('num-sum').textContent = '✓';
  $('btn-summarize').style.display = '';
  $('btn-export').style.display = '';
  $('btn-export-docx').style.display = '';
  $('btn-export-pdf').style.display = '';
  $('btn-export-pdf').href = `/report/${EXP_ID}`;

  const v = s.verdict || '';
  let cls = 'part', icon = '≈';
  if (/^支持假设/.test(v)) { cls = 'ok'; icon = '✓'; }
  else if (/^不支持/.test(v)) { cls = 'bad'; icon = '✕'; }
  else if (/证据不足/.test(v)) { cls = 'part'; icon = '?'; }

  const hr = (s.hypothesis_results || []).map(h => {
    const tag = h.verdict === 'supported' ? '<span class="badge ok">支持</span>'
      : h.verdict === 'refuted' ? '<span class="badge err">不支持</span>'
        : '<span class="badge idle">不确定</span>';
    return `<div style="padding:11px 0;border-bottom:1px solid var(--border)">
      <div style="display:flex;gap:10px;align-items:flex-start">
        <div style="flex:1;font-size:13.5px">${esc(h.hypothesis)}</div>${tag}
      </div>
      <div class="tiny muted" style="margin-top:5px">${esc(h.evidence || '')}</div>
    </div>`;
  }).join('');

  const stats = s.metrics_stats || [];
  const rows = stats.map(m => {
    const dir = (m.change_pct ?? 0) > 0;
    const cls2 = (dir === !!m.higher_better) ? 'up' : 'down';
    return `<tr>
      <td>${esc(m.name)}</td>
      <td>${m.points}</td>
      <td>${m.first}</td>
      <td class="${cls2}">${m.last}</td>
      <td><b>${m.best}</b></td>
    </tr>`;
  }).join('');

  const list = (arr) => (arr && arr.length)
    ? `<ul class="tight">${arr.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : '<span class="faint tiny">无</span>';

  box.innerHTML = `
    <div class="verdict ${cls}">
      <div class="verdict-mark">${icon}</div>
      <div>
        <h3>${esc(v || '结论')}</h3>
        <p>${esc(s.one_liner || '')}</p>
      </div>
    </div>
    ${hr ? `<div class="plan-block"><div class="plan-label">假设检验</div>${hr}</div>` : ''}
    <div class="plan-block">
      <div class="plan-label">关键发现</div>${list(s.key_findings)}
    </div>
    ${s.comparison ? `<div class="plan-block"><div class="plan-label">对比结论</div><div style="font-size:13.5px">${esc(s.comparison)}</div></div>` : ''}
    ${rows ? `<div class="plan-block" style="padding:0">
      <div class="plan-label" style="padding:16px 20px 6px">指标汇总</div>
      <table class="mt">
        <thead><tr><th>指标</th><th>点数</th><th>起始</th><th>结束</th><th>最优</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>` : ''}
    <div class="plan-block">
      <div class="plan-label">异常</div>${list(s.anomalies)}
      <div class="plan-label" style="margin-top:14px">局限性</div>${list(s.limitations)}
      <div class="plan-label" style="margin-top:14px">下一步</div>${list(s.next_steps)}
    </div>
    ${s.paper_paragraph ? `<div class="plan-block">
      <div class="plan-label">可直接用于论文的结果段落</div>
      <div class="paper-para">${esc(s.paper_paragraph)}</div>
    </div>` : ''}
    ${renderVerification(s)}`;
}

/* ---------------- 渲染：结果核验 ---------------- */
function renderVerification(s) {
  const v = s.verification;
  if (!v) return '';
  const unverified = v.unverified_numbers || [];
  const sources = v.evidence_sources || [];
  const cc = v.criteria || {};
  const checks = cc.checks || [];
  const obs = v.observed_claims || [];

  const srcRows = sources.slice(0, 12).map(src => {
    const lines = (src.lines || []).slice(0, 2)
      .map(l => `<div class="tiny faint mono">L${l.line} ${esc(l.text)}</div>`).join('');
    return `<div style="padding:7px 0;border-bottom:1px dotted var(--rule)">
      <div class="tiny"><b>${esc(src.metric || '—')}</b> = ${esc(src.value ?? '—')}
        <span class="faint">@step ${esc(src.step ?? '—')}</span></div>${lines}</div>`;
  }).join('');

  const checkRows = checks.map(c => {
    const cls = c.status === '通过' ? 'ok' : c.status === '未通过' ? 'err' : 'idle';
    const desc = c.criterion || `${c.metric || ''} ${c.op || ''} ${c.threshold ?? ''}`;
    return `<tr><td>${esc(c.kind || '')}</td><td>${esc(desc)}</td>
      <td class="mono">${esc(c.observed ?? '—')}</td>
      <td><span class="badge ${cls}">${esc(c.status)}</span></td></tr>`;
  }).join('');

  const warn = unverified.length
    ? `<div class="verify-warn"><b>${unverified.length} 个数字未在指标中找到来源，需人工确认：</b>
        ${unverified.slice(0, 12).map(x => `<span class="chip">${esc(x.token)}</span>`).join(' ')}</div>`
    : `<div class="verify-ok">✓ 结论中的数字均已回查指标，未发现无来源数值</div>`;

  return `<div class="verify-panel">
    <div class="plan-label">结果核验 · 实测 vs 推断</div>
    ${s.needs_review ? '<span class="flag warn">待人工确认</span>' : '<span class="flag ok">已核验</span>'}
    ${warn}
    <div class="verify-cols">
      <div>
        <div class="tiny" style="font-weight:600;margin:10px 0 4px">实测数据（来自 metrics 表）</div>
        <div class="tiny faint">${obs.length ? obs.map(o =>
          `${esc(o.metric)} = ${esc(o.value ?? '—')} @step ${esc(o.step ?? '—')}`).join('　·　') : '无指标数据'}</div>
        <div class="tiny" style="font-weight:600;margin:10px 0 4px">证据来源</div>
        ${srcRows || '<div class="tiny faint">无日志证据</div>'}
      </div>
      <div>
        <div class="tiny" style="font-weight:600;margin:10px 0 4px">判据判定</div>
        <div class="tiny faint" style="margin-bottom:6px">${esc(cc.summary || '未锁定判据')}</div>
        ${checkRows ? `<table class="mt"><thead><tr><th>类型</th><th>判据</th><th>实测</th><th>判定</th></tr></thead>
          <tbody>${checkRows}</tbody></table>` : '<div class="tiny faint">未设置结构化判据</div>'}
      </div>
    </div>
    <div class="tiny faint" style="margin-top:10px">${esc(v.interpretive_notice || '')}</div>
  </div>`;
}

/* ---------------- 协作模式 / checkpoint ---------------- */
const CHECKPOINT_LABEL = { plan: '方案', code: '代码', run: '运行' };

function renderMode() {
  const btn = $('btn-mode');
  if (!btn) return;
  const mode = EXP.mode || 'autonomous';
  btn.textContent = mode === 'copilot' ? '副驾驶模式' : '自主模式';
  btn.className = 'btn sm' + (mode === 'copilot' ? ' accent' : '');
  btn.title = mode === 'copilot'
    ? '副驾驶模式：方案 / 代码 / 运行各阶段会等待你的确认与批注'
    : '自主模式：一键跑到底，阶段间不暂停';

  const banner = $('checkpoint-banner');
  const cp = EXP.checkpoint || '';
  if (mode !== 'copilot' || !cp || cp === 'done') {
    banner.style.display = 'none';
    return;
  }
  const stage = CHECKPOINT_LABEL[cp] || cp;
  banner.className = 'checkpoint-banner';
  banner.style.display = '';
  banner.innerHTML = `<div class="cp-mark">⏸</div>
    <div class="cp-main">
      <b>待确认 · ${esc(stage)}阶段</b>
      <div class="tiny faint">副驾驶模式已在此暂停。可以写一句批注回灌给下一阶段，或直接确认继续。</div>
      <input type="text" id="cp-feedback" class="cp-input" placeholder="例：重点看消融 / 指标换成 AUROC / 数据集体量再小一点">
    </div>
    <div class="cp-actions">
      <button class="btn sm" id="cp-cancel">切回自主</button>
      <button class="btn sm accent" id="cp-confirm">确认并继续 →</button>
    </div>`;
  $('cp-confirm').onclick = () => confirmCheckpoint(cp);
  $('cp-cancel').onclick = async () => {
    try { await api(`/api/experiments/${EXP_ID}/mode`, { method: 'POST', body: { mode: 'autonomous' } }); await reload(); }
    catch (e) { toast(e.message, true); }
  };
  $('cp-feedback').onkeydown = e => { if (e.key === 'Enter') $('cp-confirm').click(); };
}

async function confirmCheckpoint(cp) {
  const feedback = ($('cp-feedback') && $('cp-feedback').value.trim()) || '';
  if (cp === 'plan') return doPlan(feedback);
  if (cp === 'code') return doCode(feedback);
  if (cp === 'run') return doRun();
}

/* ---------------- 预注册判据 ---------------- */
function renderCriteria() {
  const body = $('criteria-body');
  if (!body) return;
  const locked = !!EXP.criteria_locked;
  const badge = $('criteria-lock-badge');
  badge.innerHTML = locked ? '<span class="badge ok">已锁定</span>' : '<span class="badge idle">未锁定</span>';
  const c = EXP.criteria || {};
  const hasStructured = (c.success && c.success.length) || (c.failure && c.failure.length);
  const plan = EXP.plan || {};

  const ruleList = (items, kind) => {
    if (!items || !items.length) return '';
    return items.map(it => {
      if (typeof it !== 'object' || it === null) return `<li>${esc(it)}</li>`;
      return `<li><b>${esc(it.metric || '指标')}</b> ${esc(it.op || '>=')} ${esc(it.threshold ?? '—')}
        <span class="faint">${it.use === 'best' ? '取最优' : '取末值'}</span></li>`;
    }).join('');
  };

  const v = (EXP.summary && EXP.summary.verification) || {};
  const ccheck = v.criteria || {};
  const checkRows = (ccheck.checks || []).map(ch => {
    const cls = ch.status === '通过' ? 'ok' : ch.status === '未通过' ? 'err' : 'idle';
    const desc = ch.criterion || `${ch.metric || ''} ${ch.op || ''} ${ch.threshold ?? ''}`;
    return `<tr><td>${esc(ch.kind)}</td><td>${esc(desc)}</td>
      <td class="mono">${esc(ch.observed ?? '—')}</td><td><span class="badge ${cls}">${esc(ch.status)}</span></td></tr>`;
  }).join('');

  body.innerHTML = `
    ${hasStructured ? `
      <div class="plan-block">
        <div class="plan-label">成功判据</div>
        ${ruleList(c.success) ? `<ul class="tight">${ruleList(c.success)}</ul>` : '<span class="faint tiny">未设置</span>'}
        <div class="plan-label" style="margin-top:14px">失败判据</div>
        ${ruleList(c.failure) ? `<ul class="tight">${ruleList(c.failure)}</ul>` : '<span class="faint tiny">未设置</span>'}
      </div>` : `
      <div class="plan-block tiny faint">尚未设置结构化判据，当前沿用方案中的文字判据：
        <div style="margin-top:6px;color:var(--ink)">成功：${esc(plan.success_criteria || '—')}<br>失败：${esc(plan.failure_criteria || '—')}</div>
      </div>`}
    ${locked && (EXP.criteria_locked_at)
      ? `<div class="tiny faint" style="padding:8px 22px">锁定时间 ${esc(fmtTime(EXP.criteria_locked_at))}；锁定后修改需说明原因</div>` : ''}
    ${(EXP.criteria_history || []).length ? `<div class="plan-block">
      <div class="plan-label">变更记录</div>
      <ul class="tight">${EXP.criteria_history.map(h =>
        `<li>${esc(fmtTime(h.changed_at))} · ${esc(h.reason || '未说明原因')}</li>`).join('')}</ul>
    </div>` : ''}
    ${checkRows ? `<div class="plan-block" style="padding:0"><div class="plan-label" style="padding:14px 20px 6px">结果判定</div>
      <table class="mt"><thead><tr><th>类型</th><th>判据</th><th>实测</th><th>判定</th></tr></thead><tbody>${checkRows}</tbody></table></div>` : ''}
    <div id="criteria-editor" style="display:none"></div>`;
}

function openCriteriaEditor() {
  const box = $('criteria-editor');
  if (!box) return;
  box.style.display = '';
  const c = EXP.criteria || {};
  const toRow = (it) => ({
    metric: (typeof it === 'object' && it) ? (it.metric || '') : '',
    op: (typeof it === 'object' && it) ? (it.op || '>=') : '>=',
    threshold: (typeof it === 'object' && it) ? (it.threshold ?? '') : '',
    use: (typeof it === 'object' && it) ? (it.use || 'last') : 'last',
  });
  const rowsHtml = (items, kind) => (items || []).map((it, i) => {
    const r = toRow(it);
    return `<div class="crit-row" data-kind="${kind}" data-i="${i}">
      <input class="crit-metric" placeholder="指标名" value="${esc(r.metric)}">
      <select class="crit-op">
        ${['>=', '>', '<=', '<', '=='].map(o => `<option value="${o}" ${r.op === o ? 'selected' : ''}>${o}</option>`).join('')}
      </select>
      <input class="crit-threshold" placeholder="阈值" value="${esc(r.threshold)}">
      <select class="crit-use">
        <option value="last" ${r.use === 'last' ? 'selected' : ''}>末值</option>
        <option value="best" ${r.use === 'best' ? 'selected' : ''}>最优</option>
      </select>
      <button class="btn ghost sm crit-del" title="删除">×</button>
    </div>`;
  }).join('');

  box.innerHTML = `<div class="plan-block" style="background:var(--surface-2)">
    <div class="plan-label">成功判据（指标 运算符 阈值）</div>
    <div id="crit-success">${rowsHtml(c.success, 'success')}</div>
    <button class="btn ghost sm" id="crit-add-success">+ 添加成功判据</button>
    <div class="plan-label" style="margin-top:16px">失败判据</div>
    <div id="crit-failure">${rowsHtml(c.failure, 'failure')}</div>
    <button class="btn ghost sm" id="crit-add-failure">+ 添加失败判据</button>
    <div class="plan-label" style="margin-top:16px">变更原因 ${EXP.criteria_locked ? '<span style="color:var(--err)">（已锁定，必填）</span>' : '<span class="faint">（可选）</span>'}</div>
    <input type="text" id="crit-reason" placeholder="例：预注册笔误，更正阈值方向">
    <div style="display:flex;gap:8px;margin-top:14px">
      <button class="btn sm" id="crit-save">保存判据</button>
      <button class="btn sm accent" id="crit-lock">${EXP.criteria_locked ? '更新判据' : '保存并锁定'}</button>
      <button class="btn ghost sm" id="crit-cancel">取消</button>
    </div>
  </div>`;

  const addRow = (kind) => {
    const host = $(kind === 'success' ? 'crit-success' : 'crit-failure');
    const i = host.children.length;
    host.insertAdjacentHTML('beforeend', `<div class="crit-row" data-kind="${kind}" data-i="${i}">
      <input class="crit-metric" placeholder="指标名" value="">
      <select class="crit-op">${['>=', '>', '<=', '<', '=='].map(o => `<option value="${o}">${o}</option>`).join('')}</select>
      <input class="crit-threshold" placeholder="阈值" value="">
      <select class="crit-use"><option value="last">末值</option><option value="best">最优</option></select>
      <button class="btn ghost sm crit-del" title="删除">×</button></div>`);
  };
  $('crit-add-success').onclick = () => addRow('success');
  $('crit-add-failure').onclick = () => addRow('failure');
  box.querySelectorAll('.crit-del').forEach(b => b.onclick = () => b.closest('.crit-row').remove());
  $('crit-cancel').onclick = () => { box.style.display = 'none'; };
  $('crit-save').onclick = () => saveCriteria(false);
  $('crit-lock').onclick = () => saveCriteria(true);
}

async function saveCriteria(lock) {
  const collect = (kind) => Array.from(document.querySelectorAll(`#criteria-editor .crit-row[data-kind="${kind}"]`))
    .map(row => {
      const metric = row.querySelector('.crit-metric').value.trim();
      const op = row.querySelector('.crit-op').value;
      const threshold = row.querySelector('.crit-threshold').value.trim();
      const use = row.querySelector('.crit-use').value;
      const item = { metric, op, use };
      const num = Number(threshold);
      item.threshold = (threshold !== '' && !isNaN(num)) ? num : threshold;
      return item;
    }).filter(x => x.metric);
  const reason = $('crit-reason').value.trim();
  try {
    await api(`/api/experiments/${EXP_ID}/criteria`, {
      method: 'POST',
      body: { criteria: { success: collect('success'), failure: collect('failure') }, reason, lock: !!lock }
    });
    toast(lock ? '判据已保存并锁定' : '判据已保存');
    await reload();
  } catch (e) { toast(e.message, true); }
}

/* ---------------- 审计轨迹 ---------------- */
function renderAudits() {
  const body = $('audit-body');
  if (!body) return;
  const audits = EXP.audits || [];
  $('audit-count').textContent = `${audits.length} 条记录`;
  if (!audits.length) {
    body.innerHTML = '<div class="empty">暂无审计记录。方案的生成与修改、代码改动、判据锁定、运行与总结都会记录在这里。</div>';
    return;
  }
  const stageName = { experiment: '实验', plan: '方案', code: '代码', criteria: '判据',
                      run: '运行', summary: '总结', file: '文件' };
  body.innerHTML = `<div class="timeline">${audits.map(a => {
    const detail = a.detail || {};
    const chips = Object.entries(detail).slice(0, 3).map(([k, val]) => {
      const text = typeof val === 'object' ? JSON.stringify(val) : String(val);
      return `<span class="chip" title="${esc(text)}">${esc(k)}: ${esc(text.slice(0, 28))}</span>`;
    }).join('');
    return `<div class="tl-item">
      <div class="tl-dot"></div>
      <div class="tl-main">
        <div class="tl-top"><b>${esc(a.action)}</b>
          <span class="tiny faint">${esc(fmtTime(a.ts))} · ${esc(stageName[a.stage] || a.stage)}</span>
          ${a.decision ? `<span class="badge idle">${esc(a.decision)}</span>` : ''}</div>
        ${chips ? `<div class="chips" style="margin-top:5px">${chips}</div>` : ''}
      </div>
    </div>`;
  }).join('')}</div>`;
}

/* ---------------- 用量与复现配置 ---------------- */
function renderUsage() {
  const body = $('usage-body');
  if (!body) return;
  const u = EXP.usage || {};
  const run = EXP.latest_run || {};
  let env = {};
  try { env = JSON.parse(run.env_json || '{}'); } catch { }
  const budget = Number(localStorage.getItem('rekit_budget_tokens') || 0);
  const pct = budget > 0 ? Math.min(100, (u.total_tokens || 0) / budget * 100) : 0;
  const over = budget > 0 && (u.total_tokens || 0) > budget;

  body.innerHTML = `<div class="plan-block">
    <div class="plan-label">LLM 用量</div>
    <div class="stat-row plain" style="grid-template-columns:repeat(4,1fr)">
      <div class="stat"><i>调用次数</i><b>${u.calls || 0}</b></div>
      <div class="stat"><i>总 Token</i><b>${(u.total_tokens || 0).toLocaleString()}</b></div>
      <div class="stat"><i>输入 / 输出</i><b>${u.prompt_tokens || 0}/${u.completion_tokens || 0}</b></div>
      <div class="stat"><i>成本</i><b>${u.cost || 0}</b></div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;margin-top:10px" class="tiny faint">
      <span>预算（token）</span>
      <input type="number" id="budget-input" min="0" step="1000" value="${budget || ''}" placeholder="不限"
             style="width:110px">
      <button class="btn ghost sm" id="budget-save">保存</button>
      ${budget > 0 ? `<span class="${over ? 'badge err' : 'badge ok'}">${pct.toFixed(0)}%</span>` : ''}
    </div>
    ${over ? '<div class="tiny" style="color:var(--err);margin-top:6px">⚠ 已超出预算，注意控制开销</div>' : ''}
  </div>
  <div class="plan-block">
    <div class="plan-label">复现实验配置</div>
    <dl class="kv">
      <dt>随机种子</dt><dd class="mono">${esc(env.seed ?? '—')}</dd>
      <dt>Python</dt><dd class="mono">${esc(env.python || '—')}</dd>
      <dt>主机</dt><dd class="mono">${esc(env.host || '—')}</dd>
      <dt>代码快照</dt><dd class="mono">${esc((run.git_sha || '').slice(0, 8) || '—')}</dd>
      <dt>采集时间</dt><dd class="mono">${env.captured_at ? esc(fmtTime(env.captured_at)) : '—'}</dd>
    </dl>
    ${env.pip_freeze ? `<details style="margin-top:12px"><summary class="tiny" style="cursor:pointer">精确依赖（pip freeze，${env.pip_freeze.split('\n').length} 项）</summary>
      <pre class="env-pre">${esc(env.pip_freeze)}</pre></details>` : ''}
    ${env.nvidia_smi ? `<details style="margin-top:8px"><summary class="tiny" style="cursor:pointer">硬件 / GPU（nvidia-smi）</summary>
      <pre class="env-pre">${esc(env.nvidia_smi)}</pre></details>` : ''}
  </div>`;
  $('budget-save').onclick = () => {
    const v = Number($('budget-input').value) || 0;
    localStorage.setItem('rekit_budget_tokens', String(v));
    renderUsage();
    toast(v > 0 ? '预算已保存' : '已取消预算');
  };
}

/* ---------------- 监控面板 ---------------- */
function setStatus(st) {
  S.status = st;
  const b = statusBadge(st);
  $('mon-status').innerHTML = b;
  $('meta-status').innerHTML = b;
  $('st-stage').textContent = ({
    uploading: '上传', setup: '配置环境', running: '运行中',
    finished: '结束', failed: '失败', lost: '失联', stopped: '已停止',
  })[st] || st || '—';
  const busy = ['uploading', 'setup', 'running', 'pending'].includes(st);
  $('btn-stop').style.display = busy ? '' : 'none';
  $('btn-run').disabled = busy;
  if (!busy) { $('btn-run').textContent = '运行实验'; }
}

function renderRun(run) {
  S.run = run;
  if (!run) return;
  $('st-pid').textContent = run.pid || '—';
  $('st-exit').textContent = run.exit_code || (run.status === 'finished' ? '0' : '—');
  const el = $('meta-run');
  if (run.started_at) {
    const sha = run.git_sha ? ` · sha=${run.git_sha.slice(0, 8)}` : '';
    el.textContent = `run #${run.id} · 开始于 ${fmtTime(run.started_at)}${sha}`;
  }
}

function renderLineage() {
  const box = $('lineage-panel');
  if (!box) return;
  const runs = EXP.runs || [];
  if (!runs.length) {
    box.innerHTML = `<div class="lineage-empty">还没有代码快照。每次「运行实验」前都会自动提交当前工作区并锁定 commit。</div>`;
    return;
  }
  box.innerHTML = `
    <div class="lineage-head">
      <b>Git 快照谱系</b>
      <span class="tiny faint">每次运行前的代码都会锁定为独立 commit</span>
    </div>
    <table class="lineage-table">
      <thead><tr><th>Run</th><th>时间</th><th>状态</th><th>Commit</th><th>操作</th></tr></thead>
      <tbody>
        ${runs.map(r => `
          <tr>
            <td class="mono">#${r.id}</td>
            <td class="tiny faint">${r.started_at ? esc(fmtTime(r.started_at)) : '—'}</td>
            <td>${statusBadge(r.status)}</td>
            <td class="mono tiny">${r.git_sha ? esc(r.git_sha.slice(0, 8)) : '<span class="faint">未锁定</span>'}
              ${r.remote_url ? `<br><a class="tiny" href="${esc(r.remote_url)}" target="_blank" rel="noopener" style="color:var(--accent)">已推送 ↗</a>` : ''}
            </td>
            <td>
              ${r.git_sha ? `<button class="btn ghost sm" data-act="diff" data-id="${r.id}">Diff</button>
                              <button class="btn ghost sm" data-act="restore" data-id="${r.id}">恢复</button>
                              <button class="btn ghost sm" data-act="fork" data-id="${r.id}">Fork</button>
                              <button class="btn ghost sm" data-act="push" data-id="${r.id}" title="推送该快照到你的 GitHub 仓库">推送 GitHub</button>` : '<span class="tiny faint">旧运行无快照</span>'}
            </td>
          </tr>`).join('')}
      </tbody>
    </table>
    <div id="diff-view"></div>`;
  box.querySelectorAll('button[data-act]').forEach(btn => {
    btn.onclick = () => {
      const rid = Number(btn.dataset.id);
      if (btn.dataset.act === 'diff') showRunDiff(rid);
      if (btn.dataset.act === 'restore') doRestore(rid);
      if (btn.dataset.act === 'fork') doForkFromRun(rid);
      if (btn.dataset.act === 'push') doPushGitHub(rid);
    };
  });
}

async function doPushGitHub(runId) {
  let configs = [];
  try { configs = await api('/api/github/configs'); } catch (e) { toast(e.message, true); return; }
  if (!configs.length) {
    if (!confirm('还没有配置 GitHub 仓库。去设置页配置？')) return;
    location.href = '/settings'; return;
  }
  let cfg = configs[0];
  if (configs.length > 1) {
    const choice = prompt('输入要推送的仓库序号（逗号前数字）：\n' +
      configs.map((c, i) => `${i + 1}. ${c.repo_full_name}`).join('\n'), '1');
    const idx = Number(choice) - 1;
    if (!configs[idx]) return;
    cfg = configs[idx];
  }
  if (!confirm(`把 run #${runId} 的代码快照推送到 ${cfg.repo_full_name}（分支 ${cfg.default_branch}）？`)) return;

  const body = { config_id: cfg.id, run_id: runId, ack_sensitive: false };
  try {
    let r = await api('/api/experiments/' + EXP.id + '/push', { method: 'POST', body });
    if (r.need_ack) {
      const list = (r.warnings || []).map(w => `· ${w.path}：${w.kind}`).join('\n');
      if (!confirm('检测到疑似敏感信息，仍要推送吗？\n' + list)) return;
      body.ack_sensitive = true;
      r = await api('/api/experiments/' + EXP.id + '/push', { method: 'POST', body });
    }
    toast('已推送：' + r.url);
    if (typeof reload === 'function') await reload();
    else location.reload();
  } catch (e) { toast(e.message, true); }
}

async function showRunDiff(rid) {
  const view = $('diff-view');
  view.innerHTML = `<div class="diff-box">正在读取代码差异…</div>`;
  try {
    const d = await api(`/api/runs/${rid}/diff`);
    const base = d.base ? esc(d.base.slice(0, 8)) : '初始提交';
    view.innerHTML = `<div class="diff-box">
      <div class="diff-head"><b>Run #${rid} 代码差异</b><span class="tiny faint">${base} → ${esc((d.target || '').slice(0, 8))}</span>
        <button class="btn ghost sm" id="btn-close-diff">关闭</button></div>
      <pre class="diff-pre">${esc(d.patch || '无代码变更')}</pre>
    </div>`;
    $('btn-close-diff').onclick = () => { view.innerHTML = ''; };
  } catch (e) {
    view.innerHTML = `<div class="diff-box err">${esc(e.message)}</div>`;
  }
}

async function doRestore(rid) {
  if (!confirm(`确认把 run #${rid} 的代码快照恢复到当前实验？当前未提交修改会被覆盖。`)) return;
  try {
    const r = await api(`/api/runs/${rid}/restore`, { method: 'POST' });
    toast(`已恢复 ${r.files} 个文件，新提交 ${(r.sha || '').slice(0, 8)}`);
    await reload();
  } catch (e) { toast(e.message, true); }
}

async function doForkFromRun(rid) {
  const title = prompt('新实验标题（回车确认）：', `${EXP.title} · fork #${rid}`);
  if (!title) return;
  try {
    const r = await api(`/api/runs/${rid}/fork`, { method: 'POST', body: { title } });
    toast('已从该快照派生新实验');
    location.href = `/exp/${r.id}`;
  } catch (e) { toast(e.message, true); }
}

function tickElapsed() {
  const r = S.run;
  if (!r || !r.started_at) return;
  const nowMs = Date.now() + S.clockSkew;
  const endMs = r.finished_at ? r.finished_at * 1000 : nowMs;
  $('st-elapsed').textContent = fmtDuration((endMs - r.started_at * 1000) / 1000);
}

function pushLog(line, kind) {
  const cls = kind || classify(line);
  S.logLines.push({ text: line, kind: cls });
  if (S.logLines.length > 2500) S.logLines.splice(0, S.logLines.length - 2100);
  S.logCount = S.logLines.length;
  const box = $('logbox');
  if (!box) return;
  if (S.logFilter === 'all' && !S.logSearch && !S.logFold) {
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const span = document.createElement('span');
    span.className = 'l ' + cls;
    span.textContent = line;
    box.appendChild(span);
    if (atBottom) box.scrollTop = box.scrollHeight;
  } else {
    renderLog();
  }
}

let logRenderTimer = null;
function renderLog() {
  const box = $('logbox');
  if (!box) return;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  let lines = S.logLines;
  if (S.logFilter === 'metric') lines = lines.filter(x => x.kind === 'metric');
  else if (S.logFilter === 'error') lines = lines.filter(x => x.kind === 'err' || x.kind === 'warn');
  else if (S.logFilter === 'stage') lines = lines.filter(x => x.kind === 'stage');
  if (S.logSearch) {
    const q = S.logSearch.toLowerCase();
    lines = lines.filter(x => x.text.toLowerCase().includes(q));
  }
  let html = '', prev = null, dup = 0;
  lines.forEach(x => {
    if (S.logFold && x.text === prev) { dup++; return; }
    if (dup > 0) { html += `<span class="log-dup">… 重复 ×${dup + 1}</span>`; dup = 0; }
    prev = x.text;
    html += `<span class="l ${x.kind || ''}">${esc(x.text)}</span>`;
  });
  if (dup > 0) html += `<span class="log-dup">… 重复 ×${dup + 1}</span>`;
  box.innerHTML = html;
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function classify(line) {
  if (!line) return '';
  const l = line.toLowerCase();
  if (l.includes('@@metric')) return 'metric';
  if (l.includes('[stage]')) return 'stage';
  if (l.includes('traceback') || l.includes('error') || l.includes('exception') || l.includes('失败')) return 'err';
  if (l.includes('warn') || l.includes('警告')) return 'warn';
  return '';
}

function addMetric(name, value, step) {
  if (!S.series[name]) S.series[name] = [];
  const arr = S.series[name];
  arr.push({ v: value, s: step });
  if (arr.length > 800) arr.shift();
  scheduleDraw();
}

function scheduleDraw() {
  if (S.drawTimer) return;
  S.drawTimer = setTimeout(() => { S.drawTimer = null; drawCharts(); }, 60);
}

function drawCharts() {
  const box = $('charts');
  const names = Object.keys(S.series).filter(n => S.series[n].length > 1);
  if (!names.length) {
    if (!box.dataset.empty) { box.innerHTML = '<div class="tiny faint" style="padding:8px 2px">训练开始后，指标曲线会出现在这里</div>'; box.dataset.empty = '1'; }
    return;
  }
  box.dataset.empty = '';
  // 期刊插图配色：低饱和、可印刷
  const palette = ['#0F5F46', '#1F5C8B', '#A6702A', '#6B4A8E', '#9A2A1E', '#2A7F7F'];
  names.sort().forEach((n, i) => {
    let el = box.querySelector(`[data-m="${CSS.escape(n)}"]`);
    const pts = S.series[n];
    const last = pts[pts.length - 1].v;
    if (!el) {
      el = document.createElement('div');
      el.className = 'chart-box';
      el.dataset.m = n;
      el.innerHTML = `<div class="chart-title"><span>${esc(n)}</span><b></b></div><canvas></canvas>`;
      box.appendChild(el);
    }
    el.querySelector('b').textContent = fmtNum(last);
    drawLine(el.querySelector('canvas'), pts, { color: palette[i % palette.length], height: 64 });
  });
}

function fmtNum(v) {
  if (v === null || v === undefined) return '—';
  const a = Math.abs(v);
  if (a >= 1000) return v.toFixed(0);
  if (a >= 1) return v.toFixed(3);
  if (a >= 0.001) return v.toFixed(4);
  return v.toExponential(2);
}

function renderGpu(gpus) {
  if (!gpus || !gpus.length) return;
  $('gpu-box').style.display = '';
  $('gpu-box').innerHTML = gpus.map(g => {
    const pct = g.mem_total ? (g.mem_used / g.mem_total * 100) : 0;
    return `<div class="gpu-row">
      <span style="width:20px">${esc(g.index)}</span>
      <span style="width:52px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(g.name)}">${esc(g.name.slice(0, 8))}</span>
      <div class="gpu-bar"><i style="width:${Math.min(100, g.util)}%"></i></div>
      <span style="width:86px;text-align:right">${g.util.toFixed(0)}% · ${(g.mem_used / 1024).toFixed(1)}/${(g.mem_total / 1024).toFixed(0)}G</span>
    </div>`;
  }).join('');
}

/* ---------------- 数据刷新 ---------------- */
async function reload() {
  const d = await api(`/api/experiments/${EXP_ID}`);
  EXP = { ...EXP, ...d };
  S.files = d.files || [];
  S.status = d.status;
  if (d.latest_run) S.run = d.latest_run;
  renderPlan(); renderCode(); renderSummary();
  renderRun(S.run); renderLineage();
  renderMode(); renderCriteria(); renderAudits(); renderUsage();
  setStatus(d.status);
  $('btn-replan').disabled = false;
  $('btn-recode').disabled = false;
  $('btn-plan').disabled = false;
  $('btn-code').disabled = false;
  $('crit-cancel') && ($('criteria-editor').style.display = 'none');
}

async function loadMetrics() {
  const r = await api(`/api/experiments/${EXP_ID}/metrics`);
  S.series = {};
  for (const [k, v] of Object.entries(r.series || {})) S.series[k] = v;
  scheduleDraw();
}

/* ---------------- SSE ---------------- */
function connectSSE() {
  if (S.es) S.es.close();
  const es = new EventSource(`/api/experiments/${EXP_ID}/stream`);
  S.es = es;
  es.onmessage = ev => {
    let d;
    try { d = JSON.parse(ev.data); } catch { return; }
    handle(d);
  };
  es.onerror = () => { /* 浏览器会自动重连 */ };
}

function handle(d) {
  switch (d.type) {
    case 'log':
      pushLog(d.line);
      break;
    case 'note':
      pushLog('· ' + d.message, d.level === 'error' ? 'err' : d.level === 'warn' ? 'warn' : 'sys');
      if (d.level === 'error') toast(d.message, true);
      break;
    case 'metric':
      addMetric(d.name, d.value, d.step);
      break;
    case 'gpu':
      renderGpu(d.gpus);
      break;
    case 'status':
      if (d.run_id) loadRunInfo(d.run_id);
      setStatus(d.status);
      break;
    case 'summary':
      EXP.summary = d.data;
      renderSummary();
      loadMetrics();
      break;
    case 'job':
      onJob(d);
      break;
  }
}

async function loadRunInfo(runId) {
  try {
    const runs = await api(`/api/experiments/${EXP_ID}/runs`);
    const r = runs.find(x => x.id === runId);
    if (r) { renderRun(r); }
  } catch { }
}

function onJob(d) {
  const map = { plan: ['btn-plan', '生成方案', 'btn-replan', '重新生成方案'],
                code: ['btn-code', '生成代码', 'btn-recode', '重新生成代码'],
                run: ['btn-run', '运行实验'] };
  const m = map[d.kind];
  if (!m) return;
  if (d.state === 'started') {
    $(m[0]).disabled = true;
    $(m[0]).textContent = d.kind === 'plan' ? '生成中…' : d.kind === 'code' ? '编写中…' : '部署中…';
    if (m[2]) $(m[2]).disabled = true;
  } else if (d.state === 'done') {
    $(m[0]).disabled = false; $(m[0]).textContent = m[1];
    if (m[2]) { $(m[2]).disabled = false; $(m[2]).textContent = m[3]; }
    reload();
    if (d.kind !== 'run') toast(d.kind === 'plan' ? '方案已生成' : '代码已生成');
  } else if (d.state === 'error') {
    $(m[0]).disabled = false; $(m[0]).textContent = m[1];
    if (m[2]) { $(m[2]).disabled = false; $(m[2]).textContent = m[3]; }
    toast(d.error || '任务失败', true);
    reload();
  }
}

/* ---------------- 动作 ---------------- */
async function doPlan(feedback) {
  $('btn-plan').disabled = true;
  try {
    await api(`/api/experiments/${EXP_ID}/plan`, { method: 'POST', body: { feedback: feedback || '' } });
  } catch (e) { toast(e.message, true); $('btn-plan').disabled = false; }
}

async function doCode(feedback) {
  $('btn-code').disabled = true;
  try {
    await api(`/api/experiments/${EXP_ID}/code`,
      { method: 'POST', body: { feedback: feedback || '' } });
  } catch (e) { toast(e.message, true); $('btn-code').disabled = false; }
}

function pickHost() {
  return new Promise(resolve => {
    if (!HOSTS.length) { toast('请先到「服务器」页添加 SSH 主机', true); resolve(null); return; }
    if (HOSTS.length === 1) { resolve(HOSTS[0].id); return; }
    const m = document.createElement('div');
    m.style.cssText = 'position:fixed;inset:0;background:#00000030;display:flex;align-items:center;justify-content:center;z-index:200';
    m.innerHTML = `<div class="card" style="width:380px;overflow:hidden">
      <div class="sec-head"><div class="sec-title">选择运行服务器</div></div>
      <div>${HOSTS.map(h => `<div class="host-card" data-id="${h.id}" style="cursor:pointer">
        <div class="h-main"><b>${esc(h.name)}</b><small>${esc(h.username)}@${esc(h.host)}</small></div>
        ${h.gpu ? '<span class="badge ok">GPU</span>' : ''}
      </div>`).join('')}</div>
      <div style="padding:12px 18px;border-top:1px solid var(--border);text-align:right">
        <button class="btn sm" id="m-cancel">取消</button>
      </div></div>`;
    document.body.appendChild(m);
    const done = v => { m.remove(); resolve(v); };
    m.querySelectorAll('.host-card').forEach(el => el.onclick = () => done(Number(el.dataset.id)));
    m.querySelector('#m-cancel').onclick = () => done(null);
    m.onclick = e => { if (e.target === m) done(null); };
  });
}

async function doRun() {
  if (S.dirty) await saveCode(true);
  const hid = await pickHost();
  if (!hid) return;
  const btn = $('btn-run');
  btn.disabled = true; btn.textContent = '部署中…';
  try {
    const r = await api(`/api/experiments/${EXP_ID}/run`, { method: 'POST', body: { host_id: hid } });
    S.series = {};
    $('logbox').innerHTML = '';
    S.logLines = [];
    S.logCount = 0;
    toast('已开始部署，日志会实时出现在右侧');
    S.run = { id: r.run_id, started_at: Date.now() / 1000, status: 'uploading' };
    setStatus('uploading');
    $('charts').innerHTML = '';
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false; btn.textContent = '运行实验';
  }
}

async function doSummarize() {
  const run = S.run;
  if (!run) return toast('还没有运行记录', true);
  $('btn-summarize').disabled = true;
  try {
    const s = await api(`/api/runs/${run.id}/summarize`, { method: 'POST' });
    EXP.summary = s;
    renderSummary();
    toast('已重新总结');
  } catch (e) { toast(e.message, true); }
  $('btn-summarize').disabled = false;
}

function exportReport() {
  if (!EXP.summary) return toast('还没有结论可导出', true);
  window.location.href = `/api/experiments/${EXP_ID}/report.md`;
}

function exportDocx() {
  if (!EXP.summary) return toast('还没有结论可导出', true);
  window.location.href = `/api/experiments/${EXP_ID}/report.docx`;
}

async function swapInnovation() {
  const input = $('innov-file');
  input.value = '';
  input.onchange = async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    if (!confirm(`用「${file.name}」的内容替换当前创新点？替换后建议重新生成方案。`)) return;
    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await fetch('/api/extract', { method: 'POST', body: fd });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || '文稿读取失败');
      await api(`/api/experiments/${EXP_ID}/innovation`,
        { method: 'POST', body: { innovation: d.text } });
      EXP.innovation = d.text;
      $('innov-text').textContent = d.text;
      toast('已更换创新点文稿，可重新生成方案');
      await reload();
    } catch (e) { toast(e.message, true); }
  };
  input.click();
}

/* ---------------- 初始化 ---------------- */
function init() {
  renderPlan(); renderCode(); renderSummary();
  renderRun(S.run); renderLineage();
  renderMode(); renderCriteria(); renderAudits(); renderUsage();
  setStatus(EXP.status);

  if (S.run && S.run.log_tail) {
    S.run.log_tail.split('\n').filter(Boolean).forEach(l => pushLog(l));
  }
  loadMetrics().catch(() => { });
  connectSSE();
  setInterval(tickElapsed, 1000);
  tickElapsed();

  $('btn-mode').onclick = async () => {
    const next = (EXP.mode || 'autonomous') === 'copilot' ? 'autonomous' : 'copilot';
    try {
      await api(`/api/experiments/${EXP_ID}/mode`, { method: 'POST', body: { mode: next } });
      toast(next === 'copilot' ? '已切换到副驾驶模式，阶段间将等待确认' : '已切换到自主模式');
      await reload();
    } catch (e) { toast(e.message, true); }
  };
  $('btn-edit-criteria').onclick = openCriteriaEditor;
  $('btn-swap-innov').onclick = swapInnovation;
  $('btn-plan').onclick = doPlan;
  $('btn-replan').onclick = () => { if (confirm('重新生成会覆盖当前方案，继续？')) doPlan(); };
  $('btn-code').onclick = doCode;
  $('btn-recode').onclick = () => { if (confirm('重新生成会覆盖当前代码文件，继续？')) doCode(); };
  $('btn-run').onclick = doRun;
  $('btn-save-code').onclick = () => saveCode();
  $('btn-summarize').onclick = doSummarize;
  $('btn-export').onclick = exportReport;
  $('btn-export-docx').onclick = exportDocx;
  $('btn-toggle-innov').onclick = () => {
    const b = $('innov-body');
    b.style.display = b.style.display === 'none' ? '' : 'none';
  };
  $('log-search').oninput = e => { S.logSearch = e.target.value.trim().toLowerCase(); renderLog(); };
  $('log-filter').onchange = e => { S.logFilter = e.target.value; renderLog(); };
  $('btn-log-fold').onclick = () => {
    S.logFold = !S.logFold;
    $('btn-log-fold').textContent = S.logFold ? '展开' : '折叠';
    $('btn-log-fold').className = 'btn ghost sm log-btn' + (S.logFold ? ' on' : '');
    renderLog();
  };
  $('btn-scroll').onclick = () => {
    const b = $('logbox'); b.scrollTop = b.scrollHeight;
  };
  $('btn-stop').onclick = async () => {
    if (!S.run || !confirm('确认停止远程实验进程？')) return;
    try { await api(`/api/runs/${S.run.id}/stop`, { method: 'POST' }); toast('已发送停止信号'); }
    catch (e) { toast(e.message, true); }
  };
  window.addEventListener('resize', () => scheduleDraw());

  // 页面在跑但没拿到状态时，主动校准一次
  setTimeout(async () => {
    if (['running', 'uploading', 'setup'].includes(EXP.status)) {
      try { await loadMetrics(); } catch { }
    }
  }, 1200);
}

init();

// 语言切换后重绘动态生成的徽章/谱系文案（服务端数据不变）
window.addEventListener('i18n:change', () => {
  if (typeof renderRun === 'function' && S.run) renderRun(S.run);
  if (typeof renderLineage === 'function') renderLineage();
});
