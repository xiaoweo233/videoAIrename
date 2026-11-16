/* VAIR · 设置页
 *
 * 后端是配置的唯一事实来源。这里只做「读 → 填表 → 改 → 提交 → 回填」，
 * 前端不保存配置副本，多开几个标签页也不会互相覆盖。
 *
 * 控件绑定是声明式的：HTML 上写 `data-cfg="ai.temperature"` 就自动生效，
 * 本文件不需要知道具体有哪些字段。加配置项 = 在 index.html 加一行，不用改这里。
 */
(function () {
  'use strict';

  var V = window.VAIR;
  if (!V) return;

  var state = V.state;

  var TABS = {
    engine: { title: '引擎与输入', desc: '素材扫描、语音转写与抽帧。', icon: 'i-cpu' },
    naming: { title: '命名与输出', desc: '文件名的结构，以及要生成哪些附属文件。', icon: 'i-wand' },
    ai: { title: 'AI 与提示词', desc: '视觉模型接口、内置服务与提示词。', icon: 'i-sparkles' },
    system: { title: '系统与依赖', desc: '补齐 AI 依赖与模型、并发、访问与镜像。', icon: 'i-sliders' },
    glossary: { title: '术语表', desc: '把行业黑话翻译给 AI 听。', icon: 'i-layers' }
  };

  var els = {};
  var dirty = false;
  var busy = false;
  var config = null;
  var defaults = null;

  var AUTO_SAVE_DELAY = 700;      // 停顿这么久就算「改完了」；再短会在连续输入时反复写盘
  var SAVED_FLASH = 1900;         // 「已自动保存」停留多久
  var autoSaveTimer = 0;
  var barTimer = 0;

  function q(sel) {
    return els[sel] || (els[sel] = V.$(sel));
  }

  function api() { return V.api; }

  function toast(title, message, tone) { V.toast(title, message, tone); }

  /**
   * 自动保存是开还是关。
   *
   * 直接读控件而不是读 config：配置要等一次网络往返才到，而 HTML 里写死的
   * aria-checked 就是出厂默认值（默认开）。这样首屏立刻就是正确行为。
   */
  function autoSaveOn() {
    var el = V.$('[data-cfg="ui.auto_save"]');
    return !!el && el.getAttribute('aria-checked') === 'true';
  }

  /** 提示文案跟着开关走：开着还写「记得点保存」是自相矛盾的。 */
  function saveHint() {
    return autoSaveOn() ? '自动保存已开启，稍后写入配置。' : '记得点「保存修改」写入配置。';
  }

  function timeNow() {
    var d = new Date();
    return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  }

  /* ---------------- 路径读写 ---------------- */

  function getPath(obj, path) {
    var parts = String(path || '').split('.');
    var node = obj;
    for (var i = 0; i < parts.length; i++) {
      if (node === null || node === undefined || typeof node !== 'object') return undefined;
      node = node[parts[i]];
    }
    return node;
  }

  function setPath(obj, path, value) {
    var parts = String(path || '').split('.');
    var node = obj;
    for (var i = 0; i < parts.length - 1; i++) {
      var key = parts[i];
      if (typeof node[key] !== 'object' || node[key] === null) node[key] = {};
      node = node[key];
    }
    node[parts[parts.length - 1]] = value;
  }

  /* ---------------- 控件读写 ---------------- */

  function isSwitch(el) {
    return el.tagName === 'BUTTON' && el.getAttribute('role') === 'switch';
  }

  function controlType(el) {
    if (el.dataset.cfgType) return el.dataset.cfgType;
    if (isSwitch(el)) return 'bool';
    if (el.tagName === 'SELECT') return 'string';
    var type = (el.getAttribute('type') || 'text').toLowerCase();
    if (type === 'number' || type === 'range') return 'number';
    if (type === 'checkbox') return 'bool';
    return 'string';
  }

  function readControl(el) {
    var type = controlType(el);
    if (type === 'bool') {
      return isSwitch(el) ? el.getAttribute('aria-checked') === 'true' : !!el.checked;
    }
    if (isSwitch(el)) return el.getAttribute('aria-checked') === 'true';

    var raw = el.value;
    if (type === 'number') {
      if (raw === '' || raw === null) return undefined;      // 空着就别写，免得把默认值打成 0
      var num = Number(raw);
      if (!isFinite(num)) return undefined;
      if (el.dataset.decimals) return num;
      return Math.round(num);
    }
    return raw;
  }

  /**
   * 滑块的「已填充轨道」靠 CSS 变量 --fill 画，右边的 <output> 靠 data-suffix 拼单位。
   * 这两样浏览器不会自动做：不手动同步的话，拖动时圆点走了、紫色填充和数字都留在原地。
   */
  function syncSlider(el) {
    if (!el || (el.getAttribute('type') || '').toLowerCase() !== 'range') return;

    var min = Number(el.getAttribute('min'));
    var max = Number(el.getAttribute('max'));
    var value = Number(el.value);
    if (!isFinite(min)) min = 0;
    if (!isFinite(max) || max === min) max = min + 1;
    if (!isFinite(value)) value = min;

    var pct = Math.min(100, Math.max(0, (value - min) / (max - min) * 100));
    pct = Math.round(pct * 100) / 100;           // 别让内联样式出现 37.93103448275862%
    el.style.setProperty('--fill', pct + '%');
    // 纵向滑块用 bottom，横向用 left；本项目都是横向
    var out = el.parentElement ? el.parentElement.querySelector('output') : null;
    if (out) {
      // data-decimals 的滑块要固定小数位（温度 0.6 显示成 0.60）
      var text = el.dataset.decimals
        ? Number(el.value).toFixed(Number(el.dataset.decimals))
        : el.value;
      out.textContent = text + (el.dataset.suffix || '');
    }
  }

  function writeControl(el, value) {
    var type = controlType(el);
    if (type === 'bool') {
      var on = value === true || value === 'true' || value === 1;
      if (isSwitch(el)) {
        el.setAttribute('aria-checked', on ? 'true' : 'false');
      } else {
        el.checked = on;
      }
      return;
    }

    if (value === undefined || value === null) return;

    if (isSwitch(el)) {
      el.setAttribute('aria-checked', value === true ? 'true' : 'false');
      return;
    }

    if (el.tagName === 'SELECT') {
      var text = String(value);
      var matched = false;
      V.$$('option', el).forEach(function (opt) {
        if (opt.value === text) matched = true;
      });
      // 配置里的值不在选项里（比如手改过 config.json）：补一个选项，别让用户看不见真实值
      if (!matched && text) {
        var opt = document.createElement('option');
        opt.value = text;
        opt.textContent = text + '（当前配置）';
        el.appendChild(opt);
      }
      el.value = text;
      return;
    }

    el.value = String(value);
    syncSlider(el);
  }

  function eachControl(fn) {
    V.$$('[data-cfg]').forEach(function (el) {
      if (!el.dataset.cfg) return;
      fn(el);
    });
  }

  /**
   * 同一个 data-cfg 可能有两个控件（「自动保存」在设置页顶栏和保存栏各一个）。
   * 点其中一个后必须立刻把另一个摆正，否则 collect() 会按 DOM 顺序读到旧值 ——
   * 后写的赢，用户刚关掉的开关会被另一个控件改回去。
   */
  function syncMirrors(el) {
    var key = el.dataset.cfg;
    if (!key) return;
    var value = readControl(el);
    V.$$('[data-cfg="' + key + '"]').forEach(function (other) {
      if (other !== el) writeControl(other, value);
    });
  }

  /* ---------------- 填表 / 收表 ---------------- */

  function applyToForm(cfg) {
    eachControl(function (el) {
      writeControl(el, getPath(cfg, el.dataset.cfg));
    });
    renderGlossary(cfg && cfg.glossary);
    renderPrompts();
    renderNamingPreview();
    syncModelPicker();
    renderSaveNotes();          // 必须在控件写完值之后读 autoSaveOn()
  }

  function collect() {
    var patch = {};
    eachControl(function (el) {
      var value = readControl(el);
      if (value === undefined) return;
      setPath(patch, el.dataset.cfg, value);
    });
    patch.glossary = readGlossary();
    return patch;
  }

  /* ---------------- 脏标记与保存栏 ----------------
   *
   * 保存栏有三种形态：
   *   dirty  —— 有未保存的改动（手动模式下会一直挂着）
   *   saved  —— 刚自动落盘，露一下就走
   *   hidden —— 收起来
   * 手动点「保存修改」成功时用 toast 反馈，不再占用保存栏。
   */

  function renderSaveBar(mode) {
    var bar = q('[data-save-bar]');
    if (!bar) return;

    if (barTimer) { window.clearTimeout(barTimer); barTimer = 0; }

    if (mode === 'hidden') {
      bar.classList.remove('is-visible');
      return;
    }

    var title = V.$('[data-save-title]', bar);
    var sub = V.$('[data-save-sub]', bar);

    if (mode === 'saved') {
      if (title) title.textContent = autoSaveOn() ? '已自动保存' : '已保存';
      if (sub) sub.textContent = '写入 config.json · ' + timeNow();
      bar.classList.add('is-visible');
      barTimer = window.setTimeout(function () {
        barTimer = 0;
        renderSaveBar(dirty ? 'dirty' : 'hidden');
      }, SAVED_FLASH);
      return;
    }

    var live = !!(api() && api().isLive());
    if (title) title.textContent = live ? '有未保存的修改' : '后端未连接';
    if (sub) {
      if (!live) {
        sub.textContent = '改动只留在当前页面；连上后端后才能写入 config.json';
      } else {
        sub.textContent = autoSaveOn()
          ? '自动保存已开启，停止改动后立即写入 config.json'
          : '保存后会写入 config.json，下次启动自动生效';
      }
    }
    bar.classList.add('is-visible');
  }

  function setDirty(on) {
    dirty = on;
    renderSaveBar(on ? 'dirty' : 'hidden');
  }

  /** 把改动排进自动保存队列；关掉开关或离线时不排。 */
  function scheduleAutoSave() {
    cancelAutoSave();
    if (!autoSaveOn()) return;
    var client = api();
    if (!client || !client.isLive()) return;      // 离线时别定时刷「后端未连接」
    autoSaveTimer = window.setTimeout(function () {
      autoSaveTimer = 0;
      if (!dirty || busy) return;
      save({ auto: true });
    }, AUTO_SAVE_DELAY);
  }

  function cancelAutoSave() {
    if (autoSaveTimer) { window.clearTimeout(autoSaveTimer); autoSaveTimer = 0; }
  }

  function markDirty() {
    setDirty(true);
    renderNamingPreview();
    renderRestartHint();
    scheduleAutoSave();
  }

  /** 文案跟着「自动保存」走，别的角落别再说「记得保存」。 */
  function renderSaveNotes() {
    var on = autoSaveOn();
    V.$$('[data-save-note]').forEach(function (el) {
      el.textContent = on ? '点选后自动保存' : '点选后记得保存';
    });
  }

  /* ---------------- 拉取与保存 ---------------- */

  function pull(force) {
    var client = api();
    if (!client || !client.isLive()) {
      offlineNotice();
      return Promise.resolve();
    }
    return client.refreshConfig().then(function (data) {
      config = data && data.config ? data.config : {};
      defaults = data && data.defaults ? data.defaults : {};
      applyToForm(config);
      setDirty(false);
      updateRestartBaseline();
      return config;
    }).catch(function (err) {
      toast('读取配置失败', client.errorText(err), 'error');
    });
  }

  function save(opts) {
    opts = opts || {};
    var auto = !!opts.auto;

    var client = api();
    if (!client || !client.isLive()) {
      // 自动保存撞上断线就安静放弃：改动还在表单里，连上后手动保存即可
      if (auto) { renderSaveBar('dirty'); return; }
      offlineNotice();
      return;
    }
    if (busy) return;

    cancelAutoSave();
    busy = true;
    var btn = q('[data-save-settings]');
    if (btn) { btn.disabled = true; btn.classList.add('is-loading'); }

    var patch = collect();
    client.saveConfig(patch).then(function (result) {
      config = result.config;
      applyToForm(config);
      setDirty(false);
      updateRestartBaseline();
      if (auto) {
        // 自动保存不弹 toast：连着改十项就会弹十次。用保存栏报一下时间就够。
        renderSaveBar('saved');
      } else if (result.restart_required && result.restart_required.length) {
        toast('已保存', '「' + result.restart_required.join('、') + '」需要重启后端才生效。', 'warn');
      } else {
        toast('已保存', '变更 ' + (result.changed || []).length + ' 项，立即生效。', 'success');
      }
    }).catch(function (err) {
      // 自动保存失败必须说出来，否则用户以为已经存进去了
      toast(auto ? '自动保存失败' : '保存失败', client.errorText(err), 'error');
      if (auto) {
        renderSaveBar('dirty');   // 改动还在，保存栏留着让用户手动点
      }
    }).then(function () {
      busy = false;
      if (btn) { btn.disabled = false; btn.classList.remove('is-loading'); }
    });
  }

  function resetAll() {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    client.resetConfig().then(function () {
      return pull(true);
    }).then(function () {
      toast('已恢复默认', '所有设置回到出厂状态。', 'success');
    }).catch(function (err) {
      toast('重置失败', client.errorText(err), 'error');
    });
  }

  function offlineNotice() {
    toast('后端未连接', '当前是原型演示模式（直接用文件打开），设置无法保存。请通过后端启动页面访问。', 'warn');
  }

  /* ---------------- 提示词 ---------------- */

  var PROMPT_KEYS = { system: 'ai.system_prompt', prompt: 'ai.prompt' };

  function renderPrompts() {
    V.$$('[data-prompt-count]').forEach(function (el) {
      var box = V.$('[data-prompt="' + el.dataset.promptCount + '"]');
      if (box) el.textContent = (box.value || '').length + ' 字';
    });
    // 配置里没有提示词就先用内置默认值填上，用户不至于看到空白框
    V.$$('[data-prompt]').forEach(function (box) {
      if (box.value) return;
      var fallback = getPath(defaults || {}, PROMPT_KEYS[box.dataset.prompt]);
      if (fallback) box.value = fallback;
    });
  }

  function resetPrompt(which) {
    var value = getPath(defaults || {}, PROMPT_KEYS[which]);
    var box = V.$('[data-prompt="' + which + '"]');
    if (!value || !box) {
      toast('没有默认值', '拿不到内置默认提示词，请检查后端连接。', 'warn');
      return;
    }
    box.value = value;
    markDirty();
    renderPrompts();
    toast('已恢复默认提示词', saveHint(), 'info');
  }

  /* ---------------- 命名预览 ---------------- */

  var SAMPLE = {
    title: '手冲咖啡-磨豆-注水',
    original: 'VID_20260801_190533',
    moment: new Date(2026, 7, 1, 19, 5, 33)
  };

  function pad(n) { return String(n).padStart(2, '0'); }

  function strftime(fmt, date) {
    var map = {
      '%Y': String(date.getFullYear()),
      '%m': pad(date.getMonth() + 1),
      '%d': pad(date.getDate()),
      '%H': pad(date.getHours()),
      '%M': pad(date.getMinutes()),
      '%S': pad(date.getSeconds()),
      '%%': '%'
    };
    var out = fmt || '%Y%m%d_%H%M';
    Object.keys(map).forEach(function (token) {
      out = out.split(token).join(map[token]);
    });
    return out;
  }

  function sanitize(text, max) {
    var s = String(text == null ? '' : text)
      .replace(/[<>:"/\\|?*\u0000-\u001f\u007f]/g, '_')
      .replace(/\s+/g, '_')
      .replace(/_+/g, '_')
      .replace(/^[. _-]+/, '')
      .replace(/[. _-]+$/, '');
    if (max && s.length > max) {
      s = s.slice(0, max).replace(/^[. _-]+/, '').replace(/[. _-]+$/, '');
    }
    return s || 'untitled';
  }

  function domValue(path, fallback) {
    var el = V.$('[data-cfg="' + path + '"]');
    if (!el) return fallback;
    var value = readControl(el);
    return value === undefined ? fallback : value;
  }

  function namingPreviewName() {
    var template = String(domValue('naming.template', '{date}_{title}') || '{date}_{title}');
    var includeDate = domValue('naming.include_date', true) === true;
    var values = {
      date: includeDate ? strftime(String(domValue('naming.date_format', '%Y%m%d_%H%M') || ''), SAMPLE.moment) : '',
      title: sanitize(SAMPLE.title, 50),
      original: sanitize(SAMPLE.original, 60),
      marker: sanitize(String(domValue('naming.marker', 'AI_RENAMED') || 'AI_RENAMED'), 30)
    };

    var stem = template;
    Object.keys(values).forEach(function (key) {
      stem = stem.split('{' + key + '}').join(values[key]);
    });

    var suffix = [];
    if (domValue('naming.include_original', false) === true
        && template.indexOf('{original}') < 0 && values.original) {
      suffix.push(values.original);
    }
    if (domValue('naming.enable_marker', true) === true
        && template.indexOf('{marker}') < 0 && values.marker) {
      suffix.push(values.marker);
    }

    stem = sanitize(stem, 120);
    if (suffix.length) stem = sanitize(stem + '_' + suffix.join('_'), 120);
    return (stem || 'untitled') + '.mp4';
  }

  function renderNamingPreview() {
    var out = q('[data-naming-preview]');
    if (out) out.textContent = namingPreviewName();
    var dry = q('[data-output-tag]');
    if (dry) {
      var isDry = domValue('output.dry_run', false) === true;
      dry.textContent = isDry ? '预览模式 · 不落盘' : '写入磁盘';
      dry.classList.toggle('settings-card__tag--cyan', isDry);
    }
  }

  /* ---------------- 术语表 ---------------- */

  function readGlossary() {
    var rows = [];
    V.$$('[data-glossary-row]').forEach(function (row) {
      var term = (V.$('[data-term]', row) || {}).value || '';
      var meaning = (V.$('[data-meaning]', row) || {}).value || '';
      term = term.trim();
      meaning = meaning.trim();
      if (term || meaning) rows.push({ term: term, meaning: meaning });
    });
    return rows;
  }

  function glossaryRowHTML(term, meaning) {
    return '<div class="glossary__row glossary__row--edit" data-glossary-row>' +
      '<input class="input mono" type="text" aria-label="术语" data-term placeholder="口播" value="' + V.escapeHTML(term) + '">' +
      '<input class="input" type="text" aria-label="含义" data-meaning placeholder="出镜人物直接对镜头讲话的片段" value="' + V.escapeHTML(meaning) + '">' +
      '<button class="glossary__remove" type="button" aria-label="删除这条术语" data-term-remove><svg class="icon"><use href="#i-trash"/></svg></button>' +
      '</div>';
  }

  function renderGlossary(list) {
    var wrap = q('[data-glossary]');
    if (!wrap) return;
    var rows = (list || []).filter(function (row) { return row && (row.term || row.meaning); });
    wrap.innerHTML = rows.map(function (row) {
      return glossaryRowHTML(row.term || '', row.meaning || '');
    }).join('');
    var empty = q('[data-glossary-empty]');
    if (empty) empty.hidden = rows.length > 0;
  }

  function addTerm() {
    var wrap = q('[data-glossary]');
    if (!wrap) return;
    wrap.insertAdjacentHTML('beforeend', glossaryRowHTML('', ''));
    var empty = q('[data-glossary-empty]');
    if (empty) empty.hidden = true;
    var rows = V.$$('[data-glossary-row]', wrap);
    var last = rows[rows.length - 1];
    var input = last && V.$('[data-term]', last);
    if (input) input.focus();
    markDirty();
  }

  /* ---------------- AI 服务面板 ---------------- */

  var SVC_TONE = {
    running: { dot: 'dot--green', label: '运行中' },
    external: { dot: 'dot--green', label: '运行中（外部进程）' },
    starting: { dot: 'dot--amber', label: '启动中，正在加载模型…' },
    error: { dot: 'dot--rose', label: '启动失败' },
    stopped: { dot: 'dot--slate', label: '未运行' }
  };

  function renderServices(map) {
    map = map || state.services || {};
    ['llama', 'asr'].forEach(function (name) {
      var card = V.$('[data-service-card="' + name + '"]');
      if (!card) return;
      var svc = map[name];
      var tone = SVC_TONE[(svc && svc.status) || 'stopped'] || SVC_TONE.stopped;

      var dot = V.$('[data-service-dot]', card);
      if (dot) dot.className = 'dot ' + tone.dot;

      var stateEl = V.$('[data-service-state]', card);
      if (stateEl) {
        var message = (svc && svc.message) || tone.label;
        stateEl.textContent = (svc && svc.status === 'running') ? '运行中' : message;
      }

      var endpoint = V.$('[data-service-endpoint]', card);
      if (endpoint) {
        var modelText = svc && svc.model ? ' · ' + svc.model : '';
        endpoint.textContent = svc
          ? svc.endpoint + modelText + (svc.pid ? ' · PID ' + svc.pid : '')
          : '—';
      }

      var tag = V.$('[data-service-tag]', card);
      if (tag) {
        tag.textContent = tone.label;
        tag.classList.toggle('settings-card__tag--cyan', (svc && svc.status === 'starting') === true);
      }

      // 外部进程只做健康探测，绝不能显示能「停止/重启」的按钮诱导用户以为能管它。
      var external = svc && svc.status === 'external';
      V.$$('[data-service-action]', card).forEach(function (btn) {
        var act = btn.dataset.serviceAction;
        btn.disabled = !!(external && act !== 'start');
        if (external && act !== 'start') btn.title = '外部启动的进程，本程序不会终止它';
        else btn.title = '';
      });

      var logs = V.$('[data-service-logs]', card);
      if (logs) {
        var lines = (svc && svc.logs) || [];
        logs.textContent = lines.length ? lines.slice(-30).join('\n') : '（暂无输出）';
      }
    });

    var whisperTag = q('[data-whisper-tag]');
    if (whisperTag) {
      var asr = map.asr;
      whisperTag.textContent = asr
        ? (asr.status === 'running' || asr.status === 'external' ? '服务就绪'
          : asr.status === 'starting' ? '启动中' : '未运行')
        : '未检测';
    }

    var aiTag = q('[data-ai-tag]');
    if (aiTag) {
      var llama = map.llama;
      aiTag.textContent = llama
        ? (llama.status === 'running' || llama.status === 'external'
          ? (llama.model || '服务就绪')
          : llama.status === 'starting' ? '启动中' : '未运行')
        : '未检测';
    }
  }

  function serviceAction(name, action) {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    V.log('info', '正在' + ({ start: '启动', stop: '停止', restart: '重启' }[action] || action)
      + '服务 · ' + name);
    client.serviceAction(name, action).then(function (result) {
      if (result.services) renderServices(result.services);
      toast(result.ok ? '已执行' : '未执行', result.message || '', result.ok ? 'success' : 'warn');
    }).catch(function (err) {
      toast('操作失败', client.errorText(err), 'error');
    });
  }

  /* ---------------- AI 资产与下载进度 ---------------- */

  function humanSize(bytes) {
    if (!bytes) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    var value = bytes / Math.pow(1024, i);
    return (i === 0 ? value : value.toFixed(value < 10 ? 2 : 1)) + ' ' + units[i];
  }

  function assetStateText(item) {
    if (item.status === 'present') return '已就绪';
    if (item.status === 'partial') return '下载未完';
    return item.required ? '必需，缺失' : '可缺省';
  }

  function renderAssets(report) {
    var wrap = q('[data-asset-list]');
    if (!wrap || !report) return;
    var items = report.items || [];

    wrap.innerHTML = items.map(function (item) {
      return '<div class="asset-row" data-state="' + item.status + '" data-asset="' + item.id + '">' +
        '<span class="asset-row__dot"></span>' +
        '<div class="asset-row__main"><strong>' + V.escapeHTML(item.label) + '</strong>' +
        '<small>' + V.escapeHTML(item.detail) + '</small></div>' +
        '<span class="asset-row__size mono">' + humanSize(item.size) + '</span>' +
        '<span class="asset-row__state">' + assetStateText(item) + '</span>' +
        '</div>';
    }).join('');

    var tag = q('[data-asset-tag]');
    if (tag) {
      var missing = report.missing || 0;
      tag.textContent = missing ? missing + ' 项待补齐' : '全部就绪';
      tag.classList.toggle('settings-card__tag--cyan', missing > 0);
      if (missing) {
        tag.style.borderColor = 'rgba(251,191,36,.28)';
        tag.style.background = 'var(--amber-soft)';
        tag.style.color = 'var(--amber)';
      } else {
        tag.style.borderColor = '';
        tag.style.background = '';
        tag.style.color = '';
      }
    }
  }

  function setOut(sel, tone, text) {
    var box = q(sel);
    if (!box) return;
    if (!text) { box.hidden = true; return; }
    box.hidden = false;
    box.dataset.tone = tone || 'info';
    box.textContent = text;
  }

  function checkAssets() {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    setOut('[data-bootstrap-out]', 'info', '正在检查 AI 资产…');
    client.checkAssets().then(function (report) {
      renderAssets(report);
      if (report.runtime) renderRuntime(report.runtime);
      var missing = report.missing || 0;
      setOut('[data-bootstrap-out]', missing ? 'warn' : 'ok',
        missing
          ? '缺失 ' + missing + ' 项，共需下载约 ' + humanSize(report.total_missing_bytes) + '。点「补齐缺失项」开始下载。'
          : 'AI 资产齐备，可以直接开始处理素材了。');
    }).catch(function (err) {
      setOut('[data-bootstrap-out]', 'bad', '检查失败：' + client.errorText(err));
    });
  }

  function fetchAssets() {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    setOut('[data-bootstrap-out]', 'info', '已开始下载，进度在下方实时更新。可以先去忙别的，后端会一直下。');
    client.fetchAssets(null).then(function () {
      var bar = q('[data-dl-bar]');
      if (bar) bar.hidden = false;
    }).catch(function (err) {
      setOut('[data-bootstrap-out]', 'bad', '启动下载失败：' + client.errorText(err));
    });
  }

  function installDeps() {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    setOut('[data-bootstrap-out]', 'info', '正在把缺失的 Python 依赖装进 后端/libs/（不使用系统环境）…');
    client.installDeps(null).then(function () {
      toast('已开始安装依赖', '进度会输出到运行日志。', 'info');
    }).catch(function (err) {
      setOut('[data-bootstrap-out]', 'bad', '安装失败：' + client.errorText(err));
    });
  }

  function renderProgress(p) {
    if (!p) return;
    var bar = q('[data-dl-bar]');
    var label = q('[data-dl-label]');
    var percent = q('[data-dl-percent]');
    var fill = q('[data-dl-fill]');

    if (p.phase === 'all-done' || p.phase === 'install-done') {
      if (bar) bar.hidden = true;
      if (p.phase === 'install-done') {
        setOut('[data-bootstrap-out]', p.ok ? 'ok' : 'bad', p.message || '');
      } else {
        checkAssets();
        toast('补齐流程结束', '可以点「检查缺失项」确认结果。', 'success');
      }
      return;
    }

    if (bar) bar.hidden = false;
    if (p.phase === 'start') {
      if (label) label.textContent = '开始下载：' + (p.label || p.id);
      if (percent) percent.textContent = '0%';
      if (fill) fill.style.width = '0%';
      return;
    }

    if (p.phase === 'done') {
      if (label) label.textContent = (p.message || p.id) + (p.ok ? ' · 完成' : ' · 失败');
      if (percent) percent.textContent = p.ok ? '100%' : '中断';
      if (fill) fill.style.width = p.ok ? '100%' : '100%';
      if (!p.ok) setOut('[data-bootstrap-out]', 'bad', '下载未完成：' + (p.message || p.id));
      return;
    }

    if (p.phase === 'download') {
      var pct = typeof p.percent === 'number' ? p.percent : 0;
      if (label) {
        label.textContent = (p.file || '下载中')
          + (p.speed ? ' · ' + humanSize(p.speed) + '/s' : '')
          + (p.eta ? ' · 剩余约 ' + Math.round(p.eta / 60) + ' 分' : '');
      }
      if (percent) percent.textContent = pct.toFixed(1) + '%';
      if (fill) fill.style.width = Math.min(100, pct) + '%';

      var row = V.$('[data-asset="' + p.id + '"]');
      if (row) {
        row.dataset.state = 'partial';
        var stateEl = V.$('.asset-row__state', row);
        if (stateEl) stateEl.textContent = pct.toFixed(0) + '%';
      }
    }
  }

  /* ---------------- 运行环境 ---------------- */

  function renderRuntime(report) {
    var grid = q('[data-runtime-report]');
    if (!grid || !report) return;

    var tools = report.tools || {};
    var cuda = report.cuda || {};
    var modules = report.modules || {};
    var rows = [
      { key: 'GPU', value: cuda.available
          ? (cuda.name || 'NVIDIA') + ' · ' + Math.round((cuda.memory || 0) / 1024) + ' GB'
          : '未检测到（将走 CPU，会很慢）',
        tone: cuda.available ? 'good' : 'warn' },
      { key: '显卡驱动', value: cuda.driver || '—', tone: cuda.driver ? 'good' : '' },
      { key: 'ffmpeg', value: (tools.ffmpeg && tools.ffmpeg.path) || '缺失', tone: tools.ffmpeg && tools.ffmpeg.ok ? 'good' : 'bad' },
      { key: 'ffprobe', value: (tools.ffprobe && tools.ffprobe.path) || '缺失', tone: tools.ffprobe && tools.ffprobe.ok ? 'good' : 'bad' },
      { key: 'exiftool', value: (tools.exiftool && tools.exiftool.path) || '缺失', tone: tools.exiftool && tools.exiftool.ok ? 'good' : 'bad' },
      { key: 'Python', value: report.python || '—', tone: '' },
      { key: '必需依赖', value: (modules.missing || []).length ? '缺 ' + modules.missing.join('、') : '齐备',
        tone: (modules.missing || []).length ? 'bad' : 'good' },
      { key: '可选依赖', value: (modules.optional_missing || []).length ? '缺 ' + modules.optional_missing.join('、') : '齐备',
        tone: (modules.optional_missing || []).length ? 'warn' : 'good' },
      { key: '仓库根目录', value: report.root || '—', tone: '' }
    ];

    if (report.config_note) {
      rows.push({ key: '配置提示', value: report.config_note, tone: 'bad' });
    }

    grid.innerHTML = rows.map(function (row) {
      return '<div class="kv"><small>' + V.escapeHTML(row.key) + '</small>' +
        '<strong class="' + (row.tone ? 'is-' + row.tone : '') + '">' + V.escapeHTML(row.value) + '</strong></div>';
    }).join('');

    var memory = Number(cuda.memory || 0);
    if (cuda.available && memory > 0 && memory <= 12 * 1024) {
      setOut('[data-vram-hint]', 'warn',
        '检测到约 ' + Math.round(memory / 1024) + ' GB 显存：语音转写服务和视觉模型同时跑时可能爆显存。'
        + '建议把「每个视频最多抽几帧」设为 3–4、「缩放最长边」设为 448–512，'
        + '「同时处理的视频数」设为 1；如果仍爆显存，请先转写并关闭 ASR 服务，'
        + '再单独跑视觉分析。');
    } else {
      setOut('[data-vram-hint]', '', '');
    }
  }

  function refreshRuntime() {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    client.refreshRuntime().then(function (report) {
      renderRuntime(report);
      toast('已重新检测', '结果已刷新。', 'success');
    }).catch(function (err) {
      toast('检测失败', client.errorText(err), 'error');
    });
  }

  /* ---------------- 服务与访问 ---------------- */

  var restartBaseline = null;

  function updateRestartBaseline() {
    restartBaseline = {
      lan: getPath(config || {}, 'runtime.lan'),
      host: getPath(config || {}, 'runtime.host'),
      port: getPath(config || {}, 'runtime.port')
    };
    renderRestartHint();
  }

  function renderRestartHint() {
    var tag = q('[data-restart-tag]');
    if (!tag || !restartBaseline) return;
    var changed = ['lan', 'host', 'port'].some(function (key) {
      var el = V.$('[data-cfg="runtime.' + key + '"]');
      if (!el) return false;
      return readControl(el) !== restartBaseline[key];
    });
    tag.hidden = !changed;
    if (changed) {
      tag.textContent = '需重启生效';
      tag.classList.add('settings-card__tag--cyan');
    }
  }

  function renderServerInfo(info) {
    var grid = q('[data-server-info]');
    if (!grid) return;
    if (!info) {
      grid.innerHTML = '<div class="kv"><small>状态</small><strong>后端未连接</strong></div>';
      return;
    }
    var rows = [
      { key: '本机地址', value: info.local_url || '—', tone: 'good' },
      { key: '局域网地址', value: (info.lan_urls || []).length ? info.lan_urls.join('\n') : '未开启',
        tone: (info.lan_urls || []).length ? 'good' : '' },
      { key: '实际监听', value: info.host + ':' + info.port, tone: info.lan ? 'good' : '' },
      { key: '配置状态', value: info.restart_required ? '已改，需重启后端' : '与配置一致',
        tone: info.restart_required ? 'warn' : 'good' }
    ];
    grid.innerHTML = rows.map(function (row) {
      return '<div class="kv"><small>' + V.escapeHTML(row.key) + '</small>' +
        '<strong class="' + (row.tone ? 'is-' + row.tone : '') + '" style="white-space:pre-line">' +
        V.escapeHTML(row.value) + '</strong></div>';
    }).join('');
  }

  /* ---------------- 镜像预设 ---------------- */

  function renderMirrors(data) {
    var pip = q('[data-pip-presets]');
    var hf = q('[data-hf-presets]');
    if (pip && data && data.pip) {
      pip.innerHTML = Object.keys(data.pip).map(function (key) {
        return '<button class="chip" type="button" data-chip="runtime.pip_index" data-value="'
          + V.escapeHTML(data.pip[key]) + '">' + V.escapeHTML(key) + '</button>';
      }).join('');
    }
    if (hf && data && data.hf) {
      hf.innerHTML = Object.keys(data.hf).map(function (key) {
        return '<button class="chip" type="button" data-chip="runtime.hf_endpoint" data-value="'
          + V.escapeHTML(data.hf[key]) + '">' + V.escapeHTML(key) + '</button>';
      }).join('');
    }
  }

  var loadedOnce = false;

  /** 首屏拉取。必须等连接真的建立之后才调 —— 页面刚加载时握手还没完成。 */
  function loadInitial() {
    var client = api();
    if (!client || !client.isLive() || loadedOnce) return;
    loadedOnce = true;
    pull(true);
    client.refreshServer().catch(function () {});
    client.refreshServices().catch(function () {});
    client.refreshRuntime().catch(function () {});
    refreshAsrRuntime();
    client.get('/api/mirrors').then(renderMirrors).catch(function () {});
    client.checkAssets().then(function (report) {
      renderAssets(report);
      if (report.runtime) renderRuntime(report.runtime);
    }).catch(function () {});
  }

  /* ---------------- AI 测试 ---------------- */

  function currentAIConfig() {
    var cfg = {};
    ['provider', 'backend', 'base_url', 'api_key', 'model', 'remote_model', 'timeout',
      'retry_times', 'max_tokens', 'temperature', 'top_p', 'enforce_json_mode']
      .forEach(function (key) {
        var el = V.$('[data-cfg="ai.' + key + '"]');
        if (el) cfg[key] = readControl(el);
      });
    return cfg;
  }

  function testAI() {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    setOut('[data-ai-test-out]', 'info', '正在请求模型…首次加载模型可能要等十几秒。');
    client.testAI(currentAIConfig()).then(function (result) {
      setOut('[data-ai-test-out]', result.ok ? 'ok' : 'bad', result.message || '');
    }).catch(function (err) {
      setOut('[data-ai-test-out]', 'bad', '测试失败：' + client.errorText(err));
    });
  }

  /**
   * 「拉取模型」之后把服务端的模型列成可点的标签。
   * <datalist> 只有输入时才弹，用户根本发现不了；这里直接铺开，点一下就填进去。
   * 本地 GGUF 与远程模型混在一起：点本地条目会切换权重并重启内置服务，
   * 点远程条目只填「远程模型名」。
   */
  var localModelEntries = [];

  function findLocalEntry(name) {
    for (var i = 0; i < localModelEntries.length; i += 1) {
      if (localModelEntries[i].name === name) return localModelEntries[i];
    }
    return null;
  }

  function renderModelPicker(models, entries) {
    localModelEntries = entries || [];
    var box = q('[data-ai-model-picker]');
    if (!box) return;
    var list = (models || []).filter(Boolean);
    if (!list.length) {
      box.hidden = true;
      box.innerHTML = '';
      return;
    }
    box.hidden = false;
    box.innerHTML = list.map(function (name) {
      var local = findLocalEntry(name);
      return '<button type="button" class="model-picker__item" data-model-pick="' +
        V.escapeHTML(name) + '" data-local="' + (local ? '1' : '0') +
        '" title="' + V.escapeHTML(local ? name + '（本地）' : name) + '">' +
        V.escapeHTML(name) + (local ? ' <small>本地</small>' : '') + '</button>';
    }).join('');
    syncModelPicker();
  }

  function syncModelPicker() {
    var box = q('[data-ai-model-picker]');
    var remote = V.$('[data-cfg="ai.remote_model"]');
    var local = V.$('[data-cfg="ai.model"]');
    if (!box) return;
    var remoteName = (remote && remote.value) || '';
    var localName = (local && local.value) || '';
    V.$$('[data-model-pick]', box).forEach(function (btn) {
      var name = btn.dataset.modelPick;
      var isLocal = btn.dataset.local === '1';
      btn.classList.toggle('is-active', isLocal ? name === localName : name === remoteName);
    });
  }

  function fetchModels() {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    setOut('[data-ai-test-out]', 'info', '正在拉取模型列表…');
    client.listModels().then(function (result) {
      setOut('[data-ai-test-out]', result.ok ? 'ok' : 'bad', result.message || '');
      var list = q('[data-ai-model-list]');
      if (list) {
        list.innerHTML = (result.models || []).map(function (name) {
          return '<option value="' + V.escapeHTML(name) + '"></option>';
        }).join('');
      }
      renderModelPicker(result.models, result.local_entries);
      var input = V.$('[data-cfg="ai.remote_model"]');
      if (input && !input.value && result.remote && result.remote.length) {
        input.value = result.remote[0];
        syncModelPicker();
        markDirty();
      }
    }).catch(function (err) {
      setOut('[data-ai-test-out]', 'bad', '拉取失败：' + client.errorText(err));
    });
  }

  /** 切换本地 GGUF：模型文件、投影层、模型名一起改，然后重启内置服务。 */
  function applyLocalModel(entry) {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    var llama = { model: entry.model };
    if (entry.mmproj) llama.mmproj = entry.mmproj;
    setOut('[data-ai-test-out]', 'info', '正在切换到本地模型 ' + entry.name + '…');
    client.saveConfig({ ai: { model: entry.name }, services: { llama: llama } })
      .then(function (result) {
        applyToForm(result.config);
        setOut('[data-ai-test-out]', 'info', '配置已保存，正在重启内置视觉服务…');
        return client.serviceAction('llama', 'restart');
      })
      .then(function (result) {
        if (result && result.services) renderServices(result.services);
        setOut('[data-ai-test-out]', result && result.ok ? 'ok' : 'bad',
          (result && result.message) || '已提交重启，模型加载需要一些时间');
      })
      .catch(function (err) {
        setOut('[data-ai-test-out]', 'bad', '切换失败：' + client.errorText(err));
      });
  }

  function testWhisper(mode) {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    setOut('[data-whisper-test-out]', 'info',
      mode === 'probe' ? '正在探测转写服务…' : '正在合成测试语音并跑一遍完整转写链路，冷启动可能要几十秒…');
    client.testWhisper(mode).then(function (result) {
      var detail = result.message || '';
      if (result.text) {
        detail += '\n识别结果：' + result.text;
      }
      if (result.local && result.local.message) {
        detail += '\n内置服务：' + result.local.message;
      }
      if (result.remote && result.remote.message) {
        detail += '\n远程接口：' + result.remote.message;
      }
      setOut('[data-whisper-test-out]', result.ok ? 'ok' : 'bad', detail);
    }).catch(function (err) {
      setOut('[data-whisper-test-out]', 'bad', '测试失败：' + client.errorText(err));
    });
  }

  /* ---------------- 内置转写运行环境 ---------------- */

  function renderAsrRuntime(result) {
    var note = q('[data-asr-runtime-note]');
    if (!note) return;
    var text = (result && (result.message_override || result.message)) || '未知状态';
    if (result && result.running) text = '正在安装…' + text;
    else if (result && result.path) text += ' · ' + result.path;
    note.textContent = text;
  }

  function refreshAsrRuntime() {
    var client = api();
    if (!client || !client.isLive()) return;
    client.asrRuntime().then(function (result) {
      renderAsrRuntime(result);
      if (result && result.running) setTimeout(refreshAsrRuntime, 5000);
    }).catch(function () {});
  }

  function installAsrRuntime() {
    var client = api();
    if (!client || !client.isLive()) { offlineNotice(); return; }
    setOut('[data-asr-runtime-out]', 'info', '正在开始安装，约 3 GB，请留意下方日志…');
    client.installAsrRuntime().then(function (result) {
      setOut('[data-asr-runtime-out]', result.ok ? 'info' : 'bad', result.message || '');
      setTimeout(refreshAsrRuntime, 3000);
    }).catch(function (err) {
      setOut('[data-asr-runtime-out]', 'bad', '启动安装失败：' + client.errorText(err));
    });
  }

  /* ---------------- Tab ---------------- */

  var currentTab = 'engine';

  function hashTab() {
    var hash = (location.hash || '').replace('#', '');
    var parts = hash.split('/');
    return parts[0] === 'settings' && TABS[parts[1]] ? parts[1] : '';
  }

  function showTab(name) {
    if (!TABS[name]) return;
    currentTab = name;
    V.$$('[data-settings-tab]').forEach(function (btn) {
      var active = btn.dataset.settingsTab === name;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-current', active ? 'true' : 'false');
    });
    V.$$('[data-settings-panel]').forEach(function (panel) {
      var active = panel.dataset.settingsPanel === name;
      panel.hidden = !active;
      panel.classList.toggle('is-active', active);
    });
    var meta = TABS[name];
    var title = q('[data-settings-title]');
    var desc = q('[data-settings-desc]');
    var icon = V.$('[data-settings-icon] use');
    if (title) title.textContent = meta.title;
    if (desc) desc.textContent = meta.desc;
    if (icon) icon.setAttribute('href', '#' + meta.icon);
    // 把当前 tab 记进 hash，刷新后能回到同一页
    if ((location.hash || '').indexOf('#settings') === 0) {
      history.replaceState(null, '', '#settings/' + name);
    }
    // 通知侧边栏二级菜单高亮（不能复用 settings:tab，那会绕回来递归）
    V.bus.emit('settings:tab:change', name);
  }

  /* ---------------- 事件接线 ---------------- */

  function onAction(name) {
    switch (name) {
      case 'ai-test': testAI(); break;
      case 'ai-models': fetchModels(); break;
      case 'whisper-probe': testWhisper('probe'); break;
      case 'whisper-test': testWhisper('real'); break;
      case 'asr-runtime-install': installAsrRuntime(); break;
      case 'asset-check': checkAssets(); break;
      case 'asset-fetch': fetchAssets(); break;
      case 'deps-install': installDeps(); break;
      case 'runtime-refresh': refreshRuntime(); break;
      default: break;
    }
  }

  function init() {
    if (!q('[data-settings-panel]')) return;

    // Tab 切换
    V.$$('[data-settings-tab]').forEach(function (btn) {
      V.on(btn, 'click', function () { showTab(btn.dataset.settingsTab); });
    });

    // 所有声明式控件：改动即标脏（自动保存开着时顺带排一次落盘）
    eachControl(function (el) {
      var isRange = (el.getAttribute('type') || '').toLowerCase() === 'range';
      var handler = function () {
        if (isRange) syncSlider(el);
        if (el.dataset.cfg === 'ai.model' || el.dataset.cfg === 'ai.remote_model') syncModelPicker();
        syncMirrors(el);
        markDirty();
        // 「自动保存」这个开关两个方向都得立刻落盘：关掉它时定时器已经不排了，
        // 若等用户手动保存，下次启动它还是开着的 —— 等于关了个假的。
        if (el.dataset.cfg === 'ui.auto_save') save({ auto: true });
      };
      // 文本框与滑块用 input（实时），下拉框用 change
      var live = isSwitch(el) || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA';
      V.on(el, live ? 'input' : 'change', handler);
      if (isRange) syncSlider(el);          // 首屏先按 HTML 里的值摆正
      if (isSwitch(el)) {
        V.on(el, 'click', function () {
          el.setAttribute('aria-checked', el.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
          handler();
        });
      }
    });

    // 密钥显隐
    V.$$('[data-toggle-secret]').forEach(function (btn) {
      V.on(btn, 'click', function () {
        var input = V.$('input', btn.parentElement);
        if (!input) return;
        var shown = input.type === 'text';
        input.type = shown ? 'password' : 'text';
        btn.setAttribute('aria-label', shown ? '显示密钥' : '隐藏密钥');
      });
    });

    // 提示词计数
    V.$$('[data-prompt]').forEach(function (box) {
      V.on(box, 'input', function () { renderPrompts(); });
    });

    // 模型标签：点一下即选中；本地条目直接切换权重并重启服务
    var picker = q('[data-ai-model-picker]');
    if (picker) {
      V.on(picker, 'click', function (e) {
        var btn = e.target && e.target.closest ? e.target.closest('[data-model-pick]') : null;
        if (!btn) return;
        var entry = findLocalEntry(btn.dataset.modelPick);
        if (entry) {
          applyLocalModel(entry);
          return;
        }
        var input = V.$('[data-cfg="ai.remote_model"]');
        if (input) input.value = btn.dataset.modelPick;
        syncModelPicker();
        markDirty();
      });
    }

    // 页码头部按钮
    V.on(q('[data-open-config]'), 'click', function () {
      var client = api();
      if (!client || !client.isLive()) { offlineNotice(); return; }
      client.openConfigFolder().then(function (result) {
        toast(result.ok ? '已打开' : '未打开', result.message || '', result.ok ? 'success' : 'warn');
      }).catch(function (err) {
        toast('打开失败', client.errorText(err), 'error');
      });
    });

    V.on(q('[data-save-settings]'), 'click', function () { save(); });
    V.on(q('[data-reset-settings]'), 'click', resetAll);

    // 保存栏
    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        if (state.view !== 'settings') return;
        e.preventDefault();
        if (dirty) save();
        return;
      }
      if (e.key === 'Escape' && dirty) {
        var bar = q('[data-save-bar]');
        if (bar && bar.classList.contains('is-visible')) {
          cancelAutoSave();     // 否则刚撤销的改动会被定时器又写回去
          setDirty(false);
          pull(true);
        }
      }
    });

    // 设置页内的点击委托：按钮、动态行的删除、镜像 chips
    var panelWrap = q('.settings-panels');
    V.on(panelWrap, 'click', function (e) {
      var action = e.target.closest('[data-action]');
      if (action) { onAction(action.dataset.action); return; }

      var svcBtn = e.target.closest('[data-service-action]');
      if (svcBtn) {
        var card = svcBtn.closest('[data-service-card]');
        serviceAction(card.dataset.serviceCard, svcBtn.dataset.serviceAction);
        return;
      }

      var chip = e.target.closest('[data-chip]');
      if (chip) {
        var input = V.$('[data-cfg="' + chip.dataset.chip + '"]');
        if (input) {
          input.value = chip.dataset.value;
          markDirty();
          toast('已填入镜像', saveHint(), 'info');
        }
        return;
      }

      var remove = e.target.closest('[data-term-remove]');
      if (remove) {
        remove.closest('[data-glossary-row]').remove();
        var empty = q('[data-glossary-empty]');
        if (empty) empty.hidden = V.$$('[data-glossary-row]').length > 0;
        markDirty();
        return;
      }

      if (e.target.closest('[data-add-term]')) { addTerm(); return; }

      var reset = e.target.closest('[data-reset-prompt]');
      if (reset) resetPrompt(reset.dataset.resetPrompt);
    });

    // 术语表输入
    V.on(q('[data-glossary]'), 'input', function (e) {
      if (e.target.closest('[data-glossary-row]')) markDirty();
    });

    // 后端事件
    V.bus.on('services:update', renderServices);
    V.bus.on('service:update', function () {
      api() && api().refreshServices().catch(function () {});
    });
    V.bus.on('runtime:update', renderRuntime);
    V.bus.on('server:update', renderServerInfo);
    V.bus.on('progress:update', renderProgress);
    V.bus.on('conn:change', onConn);
    V.bus.on('config:update', function (cfg) {
      // 后端广播的新配置（别的标签页保存了）：没改动就跟着刷新
      if (!dirty && cfg) {
        config = cfg;
        applyToForm(config);
        updateRestartBaseline();
      }
    });
    V.bus.on('settings:tab', showTab);
    V.bus.on('view:change', function (view) {
      if (view !== 'settings') return;
      showTab(hashTab() || currentTab);
      if (api() && api().isLive()) {
        api().refreshServer().catch(function () {});
        api().refreshServices().catch(function () {});
      }
    });

    renderNamingPreview();
    renderSaveNotes();          // 首屏先按 HTML 里的默认值把文案摆正，不等配置回来
    renderServerInfo(state.server);
    renderServices(state.services);

    showTab(hashTab() || 'engine');
    onConn();
  }

  /* 后端没连上时，设置项一律只读：能改不能存是最让人困惑的状态 */
  function onConn() {
    var editable = state.conn.mode === 'live';
    V.$$('[data-cfg]').forEach(function (el) { el.disabled = !editable; });
    ['[data-save-settings]', '[data-reset-settings]', '[data-open-config]'].forEach(function (sel) {
      var el = q(sel);
      if (el) el.disabled = !editable;
    });
    if (editable) loadInitial();
    // 断线/重连时保存栏的措辞会变（「后端未连接」↔「有未保存的修改」），
    // 重连后也把离线期间攒下的改动重新排进自动保存。
    if (dirty) {
      renderSaveBar('dirty');
      if (editable) scheduleAutoSave();
    }
  }

  V.onReady(init);
})();
