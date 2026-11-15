(function () {
  'use strict';

  var V = window.VAIR;
  if (!V) return;

  var state = V.state;
  var els = {};
  var lastCount = 0;

  function q(sel) {
    return els[sel] || (els[sel] = V.$(sel));
  }

  function highlight(text, query) {
    var safe = V.escapeHTML(text);
    if (!query) return safe;
    var escaped = V.escapeHTML(query).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try {
      return safe.replace(new RegExp('(' + escaped + ')', 'gi'), '<mark>$1</mark>');
    } catch (e) {
      return safe;
    }
  }

  function filtered() {
    var level = state.logFilter;
    var query = state.logQuery.trim().toLowerCase();
    return state.logs.filter(function (l) {
      if (level !== 'all' && l.level !== level) return false;
      if (query && (l.message + ' ' + l.meta).toLowerCase().indexOf(query) < 0) return false;
      return true;
    });
  }

  function render() {
    var lines = q('[data-console-lines]');
    var empty = q('[data-console-empty]');
    var count = q('[data-log-count]');
    var consoleEl = q('[data-console]');
    if (!lines) return;

    var items = filtered();
    if (count) count.textContent = items.length + ' 条记录';
    if (empty) empty.hidden = items.length > 0;

    if (!items.length) {
      lines.innerHTML = '';
      return;
    }

    var frag = document.createDocumentFragment();
    items.forEach(function (l) {
      var row = document.createElement('div');
      row.className = 'log-line';
      row.dataset.level = l.level;
      row.innerHTML =
        '<span class="log-line__time">' + V.escapeHTML(l.time) + '</span>' +
        '<span class="log-line__level"><i></i>' + l.level.toUpperCase() + '</span>' +
        '<span class="log-line__msg">' + highlight(l.message, state.logQuery.trim()) + '</span>' +
        '<span class="log-line__meta">' + V.escapeHTML(l.meta) + '</span>';
      frag.appendChild(row);
    });
    lines.innerHTML = '';
    lines.appendChild(frag);

    if (state.autoscroll && consoleEl && items.length !== lastCount) {
      consoleEl.scrollTop = consoleEl.scrollHeight;
    }
    lastCount = items.length;
  }

  function moveThumb(btn) {
    var thumb = q('.segmented__thumb');
    if (!thumb || !btn) return;
    thumb.style.width = btn.offsetWidth + 'px';
    thumb.style.transform = 'translateX(' + (btn.offsetLeft - 3) + 'px)';
  }

  function renderTimeline() {
    var wrap = q('[data-timeline]');
    var empty = q('[data-timeline-empty]');
    if (!wrap) return;
    var items = state.timelines;
    if (!items.length) {
      if (empty) empty.hidden = false;
      V.$$('.timeline-row, .timeline-head', wrap).forEach(function (n) { n.remove(); });
      return;
    }
    if (empty) empty.hidden = true;

    var html = '<div class="timeline-head"><span>输出文件</span><span>模型</span><span>帧数</span><span>字幕字数</span><span>耗时</span></div>';
    html += items.slice(0, 8).map(function (t, i) {
      return '<div class="timeline-row" style="animation-delay:' + (i * 40) + 'ms">' +
        '<div class="timeline-row__name"><svg class="icon"><use href="#i-file-video"/></svg><span>' + V.escapeHTML(t.name) + '</span></div>' +
        '<div class="timeline-cell"><small>模型</small><span>' + V.escapeHTML(t.model) + '</span></div>' +
        '<div class="timeline-cell"><small>帧数</small><span>' + t.frames + '</span></div>' +
        '<div class="timeline-cell"><small>字幕</small><span>' + t.chars + ' 字</span></div>' +
        '<div class="timeline-cell"><small>耗时</small><span>' + t.cost.toFixed(1) + 's</span></div>' +
      '</div>';
    }).join('');

    V.$$('.timeline-row, .timeline-head', wrap).forEach(function (n) { n.remove(); });
    wrap.insertAdjacentHTML('afterbegin', html);
  }

  function exportLogs() {
    if (!state.logs.length) {
      V.toast('暂无日志可导出', '先运行一次任务吧。', 'warn');
      return;
    }
    var text = state.logs.map(function (l) {
      return '[' + l.time + '] ' + l.level.toUpperCase() + ' ' + l.message + (l.meta ? ' (' + l.meta + ')' : '');
    }).join('\n');
    var blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'vair-log-' + Date.now() + '.txt';
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    V.toast('日志已导出', state.logs.length + ' 条记录已保存为文本文件。', 'success');
  }

  function init() {
    var seg = q('.segmented');
    if (!seg) return;

    var active = V.$('.segmented__item.is-active', seg);
    window.setTimeout(function () { moveThumb(active); }, 60);

    V.$$('.segmented__item', seg).forEach(function (btn) {
      V.on(btn, 'click', function () {
        V.$$('.segmented__item', seg).forEach(function (b) {
          b.classList.toggle('is-active', b === btn);
          b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
        });
        moveThumb(btn);
        state.logFilter = btn.dataset.level;
        render();
      });
    });

    var search = q('[data-log-search]');
    var clear = q('[data-log-search-clear]');
    V.on(search, 'input', function () {
      state.logQuery = search.value;
      if (clear) clear.hidden = !search.value;
      render();
    });
    V.on(clear, 'click', function () {
      search.value = '';
      state.logQuery = '';
      clear.hidden = true;
      render();
      search.focus();
    });

    var auto = q('[data-autoscroll]');
    V.on(auto, 'change', function () {
      state.autoscroll = auto.checked;
    });

    V.on(q('[data-clear-logs]'), 'click', function () {
      if (!state.logs.length) {
        V.toast('日志已经是空的', '运行任务后会实时输出。', 'info');
        return;
      }
      state.logs = [];
      lastCount = 0;
      render();
      V.toast('日志已清空', '后续事件仍会继续记录。', 'success');
    });

    V.on(q('[data-export-logs]'), 'click', exportLogs);

    V.bus.on('logs:update', function () {
      if (state.view === 'logs' || state.view === 'workbench') render();
    });
    V.bus.on('timeline:update', renderTimeline);
    V.bus.on('view:change', function (view) {
      if (view === 'logs') {
        render();
        renderTimeline();
        var btn = V.$('.segmented__item.is-active', seg);
        moveThumb(btn);
      }
    });

    render();
    renderTimeline();
  }

  V.onReady(init);
})();
