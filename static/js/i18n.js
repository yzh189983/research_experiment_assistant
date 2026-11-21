/* 轻量国际化：data-i18n 文案外置，中文默认，英文可选。
 *
 * 用法：
 *   <span data-i18n="nav.experiments">实验</span>
 *   <input data-i18n-ph="settings.apiKeyPlaceholder" placeholder="...">
 *   JS 中取文案：I18n.t('status.running')
 *
 * 语言偏好存 localStorage(rekit-lang)，切换时派发 i18n:change 事件，
 * 动态渲染的界面（状态徽章等）监听该事件即可重绘。
 */
(function (global) {
  'use strict';

  var DICT = {
    'zh-CN': {
      'nav.home': '主页面',
      'home.enter': '进入实验台',
      'home.projects': '项目管理',
      'nav.experiments': '实验',
      'nav.hosts': '服务器',
      'nav.terminal': '终端',
      'nav.profile': '个人主页',
      'nav.settings': '设置',
      'nav.logout': '退出',
      'brand.name': '科研实验助手',
      'footer.local': '本地运行 · 仅监听 127.0.0.1 · 数据存于 data/',
      'a11y.skip': '跳到主要内容',
      'a11y.langSwitch': '切换语言 / Switch language',
      'status.draft': '待开始',
      'status.planned': '方案就绪',
      'status.coded': '代码就绪',
      'status.running': '运行中',
      'status.uploading': '上传中',
      'status.setup': '配置环境',
      'status.pending': '等待中',
      'status.done': '已完成',
      'status.finished': '已完成',
      'status.failed': '失败',
      'status.lost': '失联',
      'status.stopped': '已停止',
      'status.unknown': '未知',
      'common.retry': '重试',
      'common.close': '关闭',
      'common.save': '保存',
      'common.cancel': '取消',
      'auth.login': '登录',
      'auth.register': '注册并进入',
      'auth.registerLink': '还没有账号？注册账号 →',
      'auth.hasAccount': '已有账号？',
      'auth.backToLogin': '返回登录',
      'report.title': '实验报告',
      'report.back': '← 返回实验',
      'report.print': '打印 / 另存为 PDF'
    },
    en: {
      'nav.home': 'Home',
      'home.enter': 'Open workbench',
      'home.projects': 'Projects',
      'nav.experiments': 'Experiments',
      'nav.hosts': 'Servers',
      'nav.terminal': 'Terminal',
      'nav.profile': 'Profile',
      'nav.settings': 'Settings',
      'nav.logout': 'Log out',
      'brand.name': 'Research Assistant',
      'footer.local': 'Runs locally · binds 127.0.0.1 only · data in data/',
      'a11y.skip': 'Skip to main content',
      'a11y.langSwitch': '切换语言 / Switch language',
      'status.draft': 'Draft',
      'status.planned': 'Planned',
      'status.coded': 'Coded',
      'status.running': 'Running',
      'status.uploading': 'Uploading',
      'status.setup': 'Setting up',
      'status.pending': 'Pending',
      'status.done': 'Done',
      'status.finished': 'Finished',
      'status.failed': 'Failed',
      'status.lost': 'Lost',
      'status.stopped': 'Stopped',
      'status.unknown': 'Unknown',
      'common.retry': 'Retry',
      'common.close': 'Close',
      'common.save': 'Save',
      'common.cancel': 'Cancel',
      'auth.login': 'Log in',
      'auth.register': 'Register & enter',
      'auth.registerLink': 'No account yet? Register →',
      'auth.hasAccount': 'Already have an account?',
      'auth.backToLogin': 'Back to login',
      'report.title': 'Experiment Report',
      'report.back': '← Back to experiment',
      'report.print': 'Print / Save as PDF'
    }
  };

  var KEY = 'rekit-lang';
  var lang = 'zh-CN';
  try { lang = localStorage.getItem(KEY) || 'zh-CN'; } catch (e) { /* 隐私模式 */ }
  if (!DICT[lang]) lang = 'zh-CN';

  function t(key, fallback) {
    var d = DICT[lang] || DICT['zh-CN'];
    return d[key] !== undefined ? d[key] : (fallback !== undefined ? fallback : key);
  }

  var MAP = global.I18N_MAP || {};       // zh -> en 整句映射
  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEXTAREA: 0, CODE: 0 };

  function tr(s) {
    if (!s) return s;
    var raw = s.trim();
    if (!raw || !/[\u4e00-\u9fff]/.test(raw)) return s;   // 无中文，原样
    if (raw.indexOf('${') >= 0 || raw.indexOf('{{') >= 0 || raw.indexOf('{%') >= 0) return s; // 模板语法，跳过
    var en = MAP[raw];
    if (!en) return s;                                    // 无映射，保留中文
    if (s.charAt(0) === ' ' && s.charAt(s.length - 1) === ' ') return ' ' + en + ' ';
    if (s.indexOf('\n') >= 0) return s.replace(raw, en);
    return en;
  }

  function translateTextNodes(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null, false);
    var node;
    while ((node = walker.nextNode())) {
      var parent = node.parentNode;
      if (!parent || SKIP_TAGS[parent.nodeName]) continue;
      var en = tr(node.nodeValue);
      if (en !== node.nodeValue) node.nodeValue = en;
    }
  }

  function translateAttrs(root) {
    var els = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      ['title', 'placeholder', 'aria-label'].forEach(function (a) {
        var v = el.getAttribute(a);
        if (v) { var en = tr(v); if (en !== v) el.setAttribute(a, en); }
      });
    }
  }

  function apply() {
    document.documentElement.lang = lang;
    // 1) 显式 key 的文案（优先级最高，保留原机制）
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      var v = t(el.getAttribute('data-i18n'), el.textContent);
      if (el.textContent !== v) el.textContent = v;
    });
    document.querySelectorAll('[data-i18n-ph]').forEach(function (el) {
      el.setAttribute('placeholder', t(el.getAttribute('data-i18n-ph'), el.getAttribute('placeholder') || ''));
    });
    document.querySelectorAll('[data-i18n-title]').forEach(function (el) {
      el.setAttribute('title', t(el.getAttribute('data-i18n-title'), el.getAttribute('title') || ''));
    });
    document.querySelectorAll('[data-i18n-aria]').forEach(function (el) {
      el.setAttribute('aria-label', t(el.getAttribute('data-i18n-aria'), el.getAttribute('aria-label') || ''));
    });
    // 2) 运行时整句映射（英文模式才跑；中文模式保持源码原文）
    if (lang === 'en') {
      translateTextNodes(document.body);
      translateAttrs(document.body);
    }
    global.dispatchEvent(new CustomEvent('i18n:change', { detail: { lang: lang } }));
  }

  function set(next, noReload) {
    if (!DICT[next]) return;
    lang = next;
    try { localStorage.setItem(KEY, next); } catch (e) { /* noop */ }
    // 整句映射是单向的（zh→en），切回中文时页面上的英文无从还原。
    // 刷新页面让源码中文重新生效，双向切换都干净可靠。
    if (noReload) apply(); else location.reload();
  }

  function toggle() { set(lang === 'zh-CN' ? 'en' : 'zh-CN'); }

  function initLangButton() {
    document.querySelectorAll('[data-i18n-toggle]').forEach(function (btn) {
      if (btn.dataset.bound) return;
      btn.dataset.bound = '1';
      btn.setAttribute('aria-label', t('a11y.langSwitch'));
      btn.onclick = toggle;
    });
  }

  function patchPrompts() {
    if (global.__i18nPromptsPatched) return;
    global.__i18nPromptsPatched = true;
    var ctor = global.confirm, at = global.alert;
    global.confirm = function (msg) { return ctor(lang === 'en' ? tr(msg) : msg); };
    global.alert = function (msg) { return at(lang === 'en' ? tr(msg) : msg); };
    global.addEventListener('i18n:repaint', function (e) {
      var el = e.detail && e.detail.el;
      if (el && lang === 'en') { translateTextNodes(el); translateAttrs(el); }
    });
  }

  function watchDom() {
    if (!global.MutationObserver) return;
    var pending = false;
    var obs = new MutationObserver(function (muts) {
      if (lang !== 'en' || pending) return;
      pending = true;
      setTimeout(function () {
        pending = false;
        muts.forEach(function (m) {
          m.addedNodes.forEach(function (n) {
            if (n.nodeType === 1) { translateTextNodes(n); translateAttrs(n); }
          });
        });
      }, 0);
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }

  function boot() {
    initLangButton();
    patchPrompts();
    apply();
    watchDom();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  global.I18n = {
    t: t,
    set: set,
    toggle: toggle,
    get lang() { return lang; },
    apply: apply,
    languages: ['zh-CN', 'en']
  };
})(window);
