(function () {
  'use strict';

  var V = window.VAIR;
  if (!V) return;

  var els = {};
  var dirty = false;
  var saveBar = null;
  var initialPrompt = '';
  var currentTab = 'engine';

  var TAB_META = {
    engine: { title: '引擎与输入', desc: '管理素材目录、转写与硬件加速。', icon: 'i-cpu' },
    naming: { title: '命名与输出', desc: '定义文件名结构、冲突策略与附属文件。', icon: 'i-wand' },
    ai: { title: 'AI 与提示词', desc: '配置模型服务，并定义命名风格。', icon: 'i-sparkles' },
    system: { title: '系统与依赖', desc: '调整并发、显存上限与安全策略。', icon: 'i-sliders' },
    glossary: { title: '术语表', desc: '把行业黑话映射成 AI 能理解的表达。', icon: 'i-layers' }
  };

  function q(sel) {
    return els[sel] || (els[sel] = V.$(sel));
  }

  function setDirty(value) {
    dirty = value;
    if (saveBar) saveBar.classList.toggle('is-visible', dirty);
  }

  function showTab(name) {
    currentTab = name;
    var meta = TAB_META[name];
    if (meta) {
      var title = V.$('[data-settings-title]');
      var desc = V.$('[data-settings-desc]');
      var icon = V.$('[data-settings-icon] use');
      if (title) title.textContent = meta.title;
      if (desc) desc.textContent = meta.desc;
      if (icon) icon.setAttribute('href', '#' + meta.icon);
    }
    V.$$('[data-settings-tab]').forEach(function (btn) {
      btn.classList.toggle('is-active', btn.dataset.settingsTab === name);
    });
    V.$$('[data-subnav]').forEach(function (btn) {
      btn.classList.toggle('is-active', btn.dataset.subnav === name);
    });
    V.$$('[data-settings-panel]').forEach(function (panel) {
      var active = panel.dataset.settingsPanel === name;
      panel.hidden = !active;
      panel.classList.toggle('is-active', active);
      if (active) {
        panel.classList.remove('is-active');
        void panel.offsetWidth;
        panel.classList.add('is-active');
      }
    });
    if (history.replaceState) history.replaceState(null, '', '#settings');
    var panels = V.$('.settings-panels');
    if (panels && window.innerWidth < 900) {
      panels.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  function syncRange(input) {
    var output = input.parentElement && input.parentElement.querySelector('output');
    if (!output) return;
    var value = Number(input.value);
    if (input.max === '100' && input.min === '0') output.textContent = (value / 100).toFixed(2);
    else if (input.max === '24') output.textContent = value + ' GB';
    else output.textContent = value;

    var ratio = (value - Number(input.min)) / (Number(input.max) - Number(input.min) || 1);
    input.style.background = 'transparent';
    var track = input.style;
    track.setProperty('--fill', (ratio * 100) + '%');
  }

  function updatePromptCount() {
    var area = q('[data-prompt]');
    var counter = q('[data-prompt-count]');
    if (!area || !counter) return;
    counter.textContent = area.value.length + ' / 1200';
  }

  function save() {
    var btn = q('[data-save-settings]');
    if (!btn) return;
    btn.classList.add('is-loading');
    window.setTimeout(function () {
      btn.classList.remove('is-loading');
      setDirty(false);
      V.toast('设置已保存', '已写入 config.json，下次启动自动生效。', 'success');
      V.log('info', '配置已保存 · config.json');
      var foot = q('.settings-nav__foot span:last-child');
      if (foot) {
        foot.textContent = '刚刚同步';
        window.setTimeout(function () { foot.textContent = '配置已同步'; }, 2600);
      }
    }, 780);
  }

  function setSubnavOpen(open) {
    var subnav = q('[data-subnav]');
    if (!subnav) return;
    subnav.classList.toggle('is-open', open);
    var settingsItem = V.$('[data-nav="settings"]');
    if (settingsItem) settingsItem.setAttribute('aria-expanded', open ? 'true' : 'false');
  }

  function resetAll() {
    V.$$('[data-settings-panel] .switch').forEach(function (sw) {
      sw.setAttribute('aria-checked', 'false');
    });
    var prompt = q('[data-prompt]');
    if (prompt) {
      prompt.value = initialPrompt;
      updatePromptCount();
    }
    V.$$('.range').forEach(function (r) {
      r.value = r.defaultValue;
      syncRange(r);
    });
    setDirty(true);
    V.toast('已恢复默认值', '点击「保存修改」后生效。', 'info');
  }

  function init() {
    if (!V.$('[data-settings-panel]')) return;
    saveBar = q('[data-save-bar]');
    var prompt = q('[data-prompt]');
    if (prompt) initialPrompt = prompt.value;
    updatePromptCount();

    V.$$('[data-settings-tab]').forEach(function (btn) {
      V.on(btn, 'click', function () { showTab(btn.dataset.settingsTab); });
    });

    V.$$('[data-subnav]').forEach(function (btn) {
      V.on(btn, 'click', function () {
        showTab(btn.dataset.subnav);
        if (V.state.view !== 'settings') V.navigate('settings');
      });
    });

    V.bus.on('settings:toggle', function () {
      setSubnavOpen(!q('[data-subnav]').classList.contains('is-open'));
    });

    V.bus.on('view:change', function (view) {
      setSubnavOpen(view === 'settings');
      if (view === 'settings') showTab(currentTab);
    });

    showTab(currentTab);
    setSubnavOpen(false);

    V.on(V.$('.settings-panels'), 'click', function (e) {
      var sw = e.target.closest('.switch');
      if (sw) {
        var next = sw.getAttribute('aria-checked') !== 'true';
        sw.setAttribute('aria-checked', next ? 'true' : 'false');
        setDirty(true);
        return;
      }
      var secret = e.target.closest('[data-toggle-secret]');
      if (secret) {
        var input = secret.parentElement.querySelector('[data-secret]');
        if (!input) return;
        var show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        secret.setAttribute('aria-label', show ? '隐藏密钥' : '显示密钥');
        var use = secret.querySelector('use');
        if (use) use.setAttribute('href', show ? '#i-eye-off' : '#i-eye');
        return;
      }
      if (e.target.closest('[data-reset-prompt]')) {
        var area = q('[data-prompt]');
        if (area) {
          area.value = initialPrompt;
          updatePromptCount();
          setDirty(true);
          V.toast('提示词已恢复默认', '保存后生效。', 'info');
        }
        return;
      }
      if (e.target.closest('[data-open-config]')) {
        V.toast('配置目录', '前端演示环境无法打开本地文件，桌面端会直接定位 config.json。', 'info');
        return;
      }
      if (e.target.closest('[data-add-term]')) {
        var gloss = q('[data-glossary]');
        if (gloss) {
          var row = document.createElement('div');
          row.className = 'glossary__row';
          row.innerHTML = '<span class="mono">新术语</span><span contenteditable="true">点击编辑释义</span>';
          gloss.appendChild(row);
          setDirty(true);
          V.toast('已添加术语', '直接点击释义即可编辑。', 'success');
        }
      }
    });

    V.on(V.$('.settings-panels'), 'input', function (e) {
      if (e.target.matches('[data-prompt]')) updatePromptCount();
      if (e.target.matches('.range')) syncRange(e.target);
      setDirty(true);
    });

    V.on(V.$('.settings-panels'), 'change', function () {
      setDirty(true);
    });

    V.$$('.range').forEach(syncRange);

    V.on(q('[data-save-settings]'), 'click', save);
    V.on(q('[data-reset-settings]'), 'click', resetAll);

    V.bus.on('settings:tab', showTab);

    document.addEventListener('keydown', function (e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 's' && V.state.view === 'settings') {
        e.preventDefault();
        if (dirty) save();
      }
    });
  }

  V.onReady(init);
})();
