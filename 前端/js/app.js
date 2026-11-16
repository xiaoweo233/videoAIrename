(function () {
  'use strict';

  var VAIR = window.VAIR = window.VAIR || {};

  /* ---------------- DOM helpers ---------------- */

  VAIR.$ = function (sel, root) {
    return (root || document).querySelector(sel);
  };

  VAIR.$$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  VAIR.on = function (el, type, fn, opts) {
    if (el) el.addEventListener(type, fn, opts);
  };

  VAIR.escapeHTML = function (str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  };

  VAIR.clamp = function (n, min, max) {
    return Math.min(max, Math.max(min, n));
  };

  VAIR.pad = function (n) {
    return String(n).padStart(2, '0');
  };

  VAIR.formatTime = function (date) {
    return VAIR.pad(date.getHours()) + ':' + VAIR.pad(date.getMinutes()) + ':' + VAIR.pad(date.getSeconds());
  };

  VAIR.formatClock = function (seconds) {
    var s = Math.max(0, Math.floor(seconds));
    return VAIR.pad(Math.floor(s / 60)) + ':' + VAIR.pad(s % 60);
  };

  VAIR.formatBytes = function (bytes) {
    if (!bytes) return '0 MB';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    var value = bytes / Math.pow(1024, i);
    return (i === 0 ? value : value.toFixed(value < 10 ? 1 : 0)) + ' ' + units[i];
  };

  /* ---------------- Event bus ---------------- */

  var listeners = {};

  VAIR.bus = {
    on: function (name, fn) {
      (listeners[name] || (listeners[name] = [])).push(fn);
      return function () {
        listeners[name] = listeners[name].filter(function (f) { return f !== fn; });
      };
    },
    emit: function (name, payload) {
      (listeners[name] || []).slice().forEach(function (fn) {
        try { fn(payload); } catch (err) { console.error('[VAIR] listener error', err); }
      });
    }
  };

  /* ---------------- Global state ---------------- */

  var STAGES = [
    { key: 'probe', name: '探测', sub: 'Probe', from: 0, to: 6 },
    { key: 'frames', name: '抽帧', sub: 'Frames', from: 6, to: 30 },
    { key: 'transcribe', name: '转写', sub: 'Qwen3-ASR', from: 6, to: 30 },
    { key: 'merge', name: '合并', sub: 'Merge', from: 30, to: 62 },
    { key: 'analyze', name: 'AI 分析', sub: 'Reasoning', from: 62, to: 90 },
    { key: 'commit', name: '落盘', sub: 'Commit', from: 90, to: 100 }
  ];

  var SAMPLES = [
    { name: 'DJI_20240612_143022_0042.MP4', size: 428, duration: 186, title: '海边日落延时摄影空镜', tags: ['风光', '延时', '空镜'] },
    { name: 'VID_20240615_091544.mp4', size: 962, duration: 412, title: '咖啡店手冲过程特写口播', tags: ['美食', '口播', '特写'] },
    { name: 'screen-record-2024-06-18.mkv', size: 274, duration: 148, title: '产品后台批量导入操作演示', tags: ['教程', '录屏', '产品'] },
    { name: 'A001_C004_0612AB.mov', size: 1580, duration: 624, title: '城市夜景车流航拍素材', tags: ['城市', '航拍', '夜景'] },
    { name: '微信视频2024-06-20-1.mp4', size: 68, duration: 52, title: '团队成员采访花絮片段', tags: ['访谈', '花絮', '人物'] }
  ];

  VAIR.STAGES = STAGES;
  VAIR.SAMPLES = SAMPLES;

  VAIR.state = {
    view: 'workbench',
    theme: 'dark',
    queue: [],
    preview: false,
    task: {
      status: 'idle',
      progress: 0,
      stage: -1,
      currentFile: null,
      currentIndex: 0,
      startedAt: 0,
      elapsed: 0
    },
    stats: { success: 0, skipped: 0, failed: 0, canceled: 0, seconds: 0, completed: 0 },
    logs: [],
    approvals: [],
    timelines: [],
    logFilter: 'all',
    logQuery: '',
    autoscroll: true
  };

  /* ---------------- Storage ---------------- */

  var store = {
    get: function (key, fallback) {
      try {
        var raw = localStorage.getItem('vair:' + key);
        return raw ? JSON.parse(raw) : fallback;
      } catch (e) { return fallback; }
    },
    set: function (key, value) {
      try { localStorage.setItem('vair:' + key, JSON.stringify(value)); } catch (e) { /* ignore */ }
    }
  };

  VAIR.store = store;

  /* ---------------- Theme ---------------- */

  function applyTheme(theme, animate) {
    VAIR.state.theme = theme;
    if (animate) {
      document.body.classList.add('theme-transition');
      window.setTimeout(function () { document.body.classList.remove('theme-transition'); }, 460);
    }
    document.documentElement.setAttribute('data-theme', theme);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'light' ? '#f3f4fa' : '#06070d');
    var btn = VAIR.$('.theme-toggle');
    if (btn) {
      btn.setAttribute('aria-pressed', theme === 'light' ? 'true' : 'false');
      btn.setAttribute('aria-label', theme === 'light' ? '切换深色主题' : '切换浅色主题');
    }
    store.set('theme', theme);
  }

  VAIR.applyTheme = applyTheme;

  /* ---------------- Toast ---------------- */

  var TOAST_ICONS = { info: 'i-info', success: 'i-check', warn: 'i-alert', error: 'i-alert' };

  VAIR.toast = function (title, message, type) {
    var stack = VAIR.$('[data-toasts]');
    if (!stack) return;
    type = type || 'info';
    var node = document.createElement('div');
    node.className = 'toast toast--' + type;
    node.setAttribute('role', 'status');
    node.innerHTML =
      '<span class="toast__icon"><svg class="icon"><use href="#' + (TOAST_ICONS[type] || TOAST_ICONS.info) + '"/></svg></span>' +
      '<div class="toast__body"><strong>' + VAIR.escapeHTML(title) + '</strong>' +
      (message ? '<p>' + VAIR.escapeHTML(message) + '</p>' : '') + '</div>' +
      '<button class="toast__close" type="button" aria-label="关闭提示"><svg class="icon"><use href="#i-x"/></svg></button>' +
      '<span class="toast__progress"></span>';
    stack.appendChild(node);

    var timer = window.setTimeout(dismiss, 3400);

    function dismiss() {
      window.clearTimeout(timer);
      if (node.classList.contains('is-out')) return;
      node.classList.add('is-out');
      window.setTimeout(function () { node.remove(); }, 320);
    }

    VAIR.on(VAIR.$('.toast__close', node), 'click', dismiss);

    while (stack.children.length > 3) {
      stack.firstElementChild.remove();
    }
  };

  /* ---------------- Logging ---------------- */

  VAIR.log = function (level, message, meta) {
    var entry = {
      id: 'log-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7),
      time: VAIR.formatTime(new Date()),
      level: level,
      message: message,
      meta: meta || ''
    };
    VAIR.state.logs.push(entry);
    if (VAIR.state.logs.length > 240) VAIR.state.logs.shift();
    VAIR.bus.emit('logs:update', entry);
  };

  /* ---------------- Navigation ---------------- */

  var VIEW_NAMES = {
    workbench: '工作台',
    realtime: '实时进度',
    approvals: '命名审批',
    logs: '运行日志',
    settings: '设置'
  };

  VAIR.VIEW_NAMES = VIEW_NAMES;

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function syncNav(view) {
    VAIR.$$('.view').forEach(function (el) {
      var active = el.dataset.view === view;
      el.classList.toggle('is-active', active);
      el.hidden = !active;
      if (active) {
        el.classList.remove('is-active');
        void el.offsetWidth;
        el.classList.add('is-active');
      }
    });
    VAIR.$$('[data-nav]').forEach(function (el) {
      if (el.dataset.nav === view) el.setAttribute('aria-current', 'page');
      else el.removeAttribute('aria-current');
    });
    VAIR.state.view = view;
    if (history.replaceState) history.replaceState(null, '', '#' + view);
  }

  VAIR.navigate = function (view, opts) {
    if (!VIEW_NAMES[view]) return;
    if (VAIR.state.view === view && !(opts && opts.force)) return;
    if (document.startViewTransition && !reduceMotion) {
      document.startViewTransition(function () { syncNav(view); });
    } else {
      syncNav(view);
    }
    window.scrollTo({ top: 0, behavior: 'auto' });
    VAIR.bus.emit('view:change', view);
  };

  /* ---------------- Command palette ---------------- */

  var COMMANDS = [
    { group: '页面', label: '工作台', hint: '1', icon: 'i-grid', view: 'workbench' },
    { group: '页面', label: '实时进度', hint: '2', icon: 'i-activity', view: 'realtime' },
    { group: '页面', label: '命名审批', hint: '3', icon: 'i-check-square', view: 'approvals' },
    { group: '页面', label: '运行日志', hint: '4', icon: 'i-terminal', view: 'logs' },
    { group: '页面', label: '设置', hint: '5', icon: 'i-settings', view: 'settings' },
    { group: '操作', label: '添加示例素材', hint: '', icon: 'i-plus', action: 'add-sample' },
    { group: '操作', label: '开始处理任务', hint: '', icon: 'i-play', action: 'start' },
    { group: '操作', label: '切换浅色 / 深色主题', hint: '', icon: 'i-moon', action: 'theme' },
    { group: '设置', label: 'AI 与提示词', hint: '', icon: 'i-sparkles', action: 'settings-ai' },
    { group: '设置', label: '系统与依赖', hint: '', icon: 'i-sliders', action: 'settings-system' }
  ];

  var paletteState = { open: false, index: 0, items: COMMANDS.slice(), lastFocus: null };

  function renderPalette(query) {
    var list = VAIR.$('[data-palette-list]');
    if (!list) return;
    var q = (query || '').trim().toLowerCase();
    var items = COMMANDS.filter(function (c) {
      return !q || c.label.toLowerCase().indexOf(q) > -1 || c.group.toLowerCase().indexOf(q) > -1;
    });
    paletteState.items = items;
    paletteState.index = 0;
    if (!items.length) {
      list.innerHTML = '<div class="palette__empty">没有匹配的命令，换个关键词试试</div>';
      return;
    }
    var html = '';
    var group = '';
    items.forEach(function (item, i) {
      if (item.group !== group) {
        group = item.group;
        html += '<div class="palette__group">' + VAIR.escapeHTML(group) + '</div>';
      }
      html += '<button type="button" class="palette__item' + (i === 0 ? ' is-active' : '') + '" data-palette-index="' + i + '">' +
        '<svg class="icon"><use href="#' + item.icon + '"/></svg><span>' + VAIR.escapeHTML(item.label) + '</span>' +
        (item.hint ? '<small>' + item.hint + '</small>' : '') + '</button>';
    });
    list.innerHTML = html;
  }

  function setPaletteActive(index) {
    var items = VAIR.$$('[data-palette-index]');
    if (!items.length) return;
    paletteState.index = (index + items.length) % items.length;
    items.forEach(function (el, i) {
      el.classList.toggle('is-active', i === paletteState.index);
    });
    var active = items[paletteState.index];
    if (active && active.scrollIntoView) active.scrollIntoView({ block: 'nearest' });
  }

  function openPalette() {
    var el = VAIR.$('[data-palette]');
    if (!el || paletteState.open) return;
    paletteState.open = true;
    paletteState.lastFocus = document.activeElement;
    el.hidden = false;
    document.body.classList.add('is-locked');
    var input = VAIR.$('[data-palette-input]');
    input.value = '';
    renderPalette('');
    window.setTimeout(function () { input.focus(); }, 30);
  }

  function closePalette() {
    var el = VAIR.$('[data-palette]');
    if (!el || !paletteState.open) return;
    paletteState.open = false;
    el.hidden = true;
    document.body.classList.remove('is-locked');
    if (paletteState.lastFocus && paletteState.lastFocus.focus) paletteState.lastFocus.focus();
  }

  function runCommand(item) {
    if (!item) return;
    closePalette();
    if (item.view) { VAIR.navigate(item.view); return; }
    if (item.action === 'theme') { applyTheme(VAIR.state.theme === 'light' ? 'dark' : 'light', true); return; }
    if (item.action === 'add-sample') { VAIR.bus.emit('action:add-sample'); return; }
    if (item.action === 'start') { VAIR.navigate('workbench'); VAIR.bus.emit('action:start'); return; }
    if (item.action === 'settings-ai') { VAIR.navigate('settings'); VAIR.bus.emit('settings:tab', 'ai'); return; }
    if (item.action === 'settings-system') { VAIR.navigate('settings'); VAIR.bus.emit('settings:tab', 'system'); }
  }

  /* ---------------- 后端连接徽标 ---------------- */

  var CONN_TEXT = {
    connecting: { label: '连接中…', title: '正在连接后端' },
    live: { label: '已连接后端', title: '后端运行中' },
    offline: { label: '后端已断开', title: '连接断开，点这里重试' },
    demo: { label: '演示模式', title: '未连接后端，界面数据为模拟内容' }
  };

  function renderConn(conn) {
    conn = conn || VAIR.state.conn || { mode: 'connecting' };
    var mode = conn.mode || 'connecting';
    var text = CONN_TEXT[mode] || CONN_TEXT.connecting;
    var btn = VAIR.$('[data-connection]');
    if (!btn) return;
    btn.dataset.conn = mode;

    var label = VAIR.$('[data-conn-label]', btn);
    if (label) label.textContent = text.label;

    var meta = VAIR.$('[data-conn-meta]', btn);
    if (meta) {
      if (mode === 'live') meta.textContent = conn.version ? 'v' + conn.version : '在线';
      else if (mode === 'offline') meta.textContent = '重试中';
      else meta.textContent = '—';
    }
    btn.setAttribute('aria-label', text.title);
  }

  /* ---------------- 设置二级菜单 ---------------- */

  // 侧边栏「设置」下面那组分类。HTML/CSS 早就有，缺的是把点击接上：
  // 点父项展开/收起，点子项跳到对应 tab，切页时自动开合。
  function setSubnav(open) {
    var parent = VAIR.$('.nav__item--parent');
    var box = VAIR.$('.subnav');
    if (!parent || !box) return;
    parent.setAttribute('aria-expanded', open ? 'true' : 'false');
    box.classList.toggle('is-open', open);
  }

  function markSubnav(tab) {
    VAIR.$$('[data-subnav]').forEach(function (item) {
      if (!item.dataset.subnav) return;            // 容器本身没有值，跳过
      var active = item.dataset.subnav === tab;
      item.classList.toggle('is-active', active);
      if (active) item.setAttribute('aria-current', 'true');
      else item.removeAttribute('aria-current');
    });
  }

  function initSubnav() {
    VAIR.$$('[data-subnav]').forEach(function (item) {
      if (!item.dataset.subnav) return;
      VAIR.on(item, 'click', function () {
        VAIR.navigate('settings');
        VAIR.bus.emit('settings:tab', item.dataset.subnav);
        setSubnav(true);
      });
    });

    // 已在设置页时再点「设置」= 开合二级菜单
    VAIR.bus.on('settings:toggle', function () {
      var box = VAIR.$('.subnav');
      setSubnav(!(box && box.classList.contains('is-open')));
    });
    VAIR.bus.on('settings:tab:change', markSubnav);
    VAIR.bus.on('view:change', function (view) {
      setSubnav(view === 'settings');
    });

    setSubnav(VAIR.state.view === 'settings');
  }

  /* ---------------- Global wiring ---------------- */

  function initGlobal() {
    applyTheme(store.get('theme', 'light'), false);

    document.addEventListener('click', function (e) {
      var nav = e.target.closest('[data-nav]');
      if (nav) {
        e.preventDefault();
        if (nav.dataset.nav === 'settings' && VAIR.state.view === 'settings') {
          VAIR.bus.emit('settings:toggle');
          return;
        }
        VAIR.navigate(nav.dataset.nav);
        return;
      }
      var toastBtn = e.target.closest('[data-toast]');
      if (toastBtn) {
        VAIR.toast(toastBtn.dataset.toast, '本地推理服务运行中，模型已加载。', 'success');
        return;
      }
      var conn = e.target.closest('[data-connection]');
      if (conn) {
        e.preventDefault();
        var mode = (VAIR.state.conn || {}).mode;
        VAIR.navigate('settings');
        if (mode === 'offline' && VAIR.api) VAIR.api.connect();
        return;
      }
      var cmd = e.target.closest('[data-command-open]');
      if (cmd) { e.preventDefault(); openPalette(); return; }
      if (e.target.closest('[data-palette-close]')) closePalette();
      var pItem = e.target.closest('[data-palette-index]');
      if (pItem) {
        runCommand(paletteState.items[Number(pItem.dataset.paletteIndex)]);
      }
    });

    VAIR.on(VAIR.$('.theme-toggle'), 'click', function () {
      applyTheme(VAIR.state.theme === 'light' ? 'dark' : 'light', true);
    });

    var paletteInput = VAIR.$('[data-palette-input]');
    VAIR.on(paletteInput, 'input', function () { renderPalette(paletteInput.value); });
    VAIR.on(paletteInput, 'keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setPaletteActive(paletteState.index + 1); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); setPaletteActive(paletteState.index - 1); }
      else if (e.key === 'Enter') { e.preventDefault(); runCommand(paletteState.items[paletteState.index]); }
    });

    document.addEventListener('keydown', function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || '';
      var typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || document.activeElement.isContentEditable;

      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if (paletteState.open) closePalette(); else openPalette();
        return;
      }
      if (e.key === 'Escape') {
        if (paletteState.open) { e.preventDefault(); closePalette(); }
        return;
      }
      if (typing || paletteState.open || e.metaKey || e.ctrlKey || e.altKey) return;
      var map = { '1': 'workbench', '2': 'realtime', '3': 'approvals', '4': 'logs', '5': 'settings' };
      if (map[e.key]) {
        e.preventDefault();
        VAIR.navigate(map[e.key]);
      }
    });

    var glow = VAIR.$('#cursorGlow');
    var pointer = { x: window.innerWidth / 2, y: 200, tx: window.innerWidth / 2, ty: 200, raf: 0, active: false };

    function animateGlow() {
      pointer.x += (pointer.tx - pointer.x) * 0.14;
      pointer.y += (pointer.ty - pointer.y) * 0.14;
      glow.style.transform = 'translate3d(' + pointer.x + 'px,' + pointer.y + 'px,0) translate(-50%,-50%)';
      if (Math.abs(pointer.tx - pointer.x) > 0.4 || Math.abs(pointer.ty - pointer.y) > 0.4) {
        pointer.raf = requestAnimationFrame(animateGlow);
      } else {
        pointer.raf = 0;
      }
    }

    document.addEventListener('pointermove', function (e) {
      if (e.pointerType === 'touch') return;
      pointer.tx = e.clientX;
      pointer.ty = e.clientY;
      if (!pointer.active) {
        pointer.active = true;
        pointer.x = e.clientX;
        pointer.y = e.clientY;
        glow.classList.add('is-on');
      }
      if (!pointer.raf) pointer.raf = requestAnimationFrame(animateGlow);
    }, { passive: true });

    document.addEventListener('pointerleave', function () {
      pointer.active = false;
      glow.classList.remove('is-on');
    });

    var zone = VAIR.$('[data-dropzone]');
    VAIR.on(zone, 'pointermove', function (e) {
      var rect = zone.getBoundingClientRect();
      zone.style.setProperty('--mx', ((e.clientX - rect.left) / rect.width * 100).toFixed(2) + '%');
      zone.style.setProperty('--my', ((e.clientY - rect.top) / rect.height * 100).toFixed(2) + '%');
    });

    var initial = (location.hash || '').replace('#', '');
    if (VIEW_NAMES[initial]) {
      VAIR.state.view = initial;
      syncNav(initial);
    } else {
      syncNav('workbench');
    }

    window.addEventListener('hashchange', function () {
      var v = (location.hash || '').replace('#', '');
      if (VIEW_NAMES[v]) VAIR.navigate(v);
    });
  }

  /* ---------------- Ready ---------------- */

  var readyFns = [];

  VAIR.onReady = function (fn) {
    readyFns.push(fn);
  };

  if (document.readyState === 'complete') {
    boot();
  } else {
    document.addEventListener('DOMContentLoaded', boot);
  }

  function boot() {
    initGlobal();
    initSubnav();
    VAIR.bus.on('conn:change', renderConn);
    renderConn();
    readyFns.forEach(function (fn) {
      try { fn(); } catch (err) { console.error('[VAIR] init error', err); }
    });
    VAIR.bus.emit('app:ready');
  }
})();
