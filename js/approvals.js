(function () {
  'use strict';

  var V = window.VAIR;
  if (!V) return;

  var state = V.state;
  var els = {};
  var undoStack = [];

  function q(sel) {
    return els[sel] || (els[sel] = V.$(sel));
  }

  function pending() {
    return state.approvals.filter(function (a) { return a.status === 'pending'; });
  }

  function renderSummary() {
    var list = state.approvals;
    var p = list.filter(function (a) { return a.status === 'pending'; }).length;
    var ok = list.filter(function (a) { return a.status === 'approved'; }).length;
    var skip = list.filter(function (a) { return a.status === 'skipped'; }).length;

    var map = {
      '[data-approval-pending]': p,
      '[data-approval-approved]': ok,
      '[data-approval-skipped]': skip
    };
    Object.keys(map).forEach(function (sel) {
      var el = $(sel);
      if (el) el.textContent = map[sel];
    });

    var caption = q('[data-approval-caption]');
    if (caption) caption.textContent = p + ' 条建议 · ' + list.length + ' 条总计';

    var badge = q('[data-approval-badge]');
    if (badge) {
      badge.textContent = p;
      badge.hidden = p === 0;
    }

    var undoBtn = q('[data-approval-undo]');
    if (undoBtn) undoBtn.hidden = undoStack.length === 0;

    var empty = q('[data-approval-empty]');
    var listEl = q('[data-approval-list]');
    var allDone = list.length > 0 && p === 0;
    if (empty) empty.hidden = !allDone;
    if (listEl) listEl.hidden = allDone;
  }

  function rowHTML(item) {
    var tags = item.tags.map(function (t, i) {
      return '<span class="tag' + (i === 0 ? ' tag--violet' : '') + '">' + V.escapeHTML(t) + '</span>';
    }).join('');
    var conf = Math.round(item.confidence * 100);
    var minutes = Math.floor(item.duration / 60);
    var seconds = item.duration % 60;
    return '<article class="approval-row" data-approval="' + item.id + '">' +
      '<div class="approval-thumb">' +
        '<span class="approval-thumb__play"><svg class="icon"><use href="#i-play"/></svg></span>' +
        '<span class="approval-thumb__time">' + V.pad(minutes) + ':' + V.pad(seconds) + '</span>' +
      '</div>' +
      '<div class="approval-main">' +
        '<p class="approval-original"><svg class="icon"><use href="#i-file-video"/></svg>' + V.escapeHTML(item.original) + '</p>' +
        '<div class="approval-name">' +
          '<input type="text" value="' + V.escapeHTML(item.name) + '" aria-label="编辑新文件名" data-approval-name="' + item.id + '" spellcheck="false">' +
        '</div>' +
        '<div class="approval-meta">' + tags +
          '<span class="confidence"><span class="confidence__track"><span style="width:' + conf + '%"></span></span>置信度 ' + conf + '%</span>' +
        '</div>' +
      '</div>' +
      '<div class="approval-actions">' +
        '<button class="btn btn--ghost" type="button" data-skip="' + item.id + '"><svg class="icon"><use href="#i-skip"/></svg>忽略</button>' +
        '<button class="btn btn--primary" type="button" data-approve="' + item.id + '"><svg class="icon"><use href="#i-check"/></svg>通过</button>' +
      '</div>' +
    '</article>';
  }

  function renderList() {
    var listEl = q('[data-approval-list]');
    if (!listEl) return;
    var items = state.approvals.filter(function (a) { return a.status === 'pending'; });
    listEl.innerHTML = items.map(rowHTML).join('');
  }

  function render() {
    renderList();
    renderSummary();
  }

  function decide(id, status) {
    var item = state.approvals.find(function (a) { return a.id === id; });
    if (!item || item.status !== 'pending') return;
    var row = V.$('[data-approval="' + id + '"]');
    undoStack.push({ id: id, previous: item.status });
    if (undoStack.length > 12) undoStack.shift();

    var nameInput = V.$('[data-approval-name="' + id + '"]');
    if (nameInput) item.name = nameInput.value.trim() || item.name;

    if (row) {
      row.classList.add('is-leaving');
      window.setTimeout(function () {
        item.status = status;
        render();
      }, 300);
    } else {
      item.status = status;
      render();
    }

    if (status === 'approved') {
      V.log('info', '命名已通过 · ' + item.name, item.cost ? item.cost.toFixed(1) + 's' : '');
    } else {
      V.log('warn', '命名建议已忽略 · ' + item.original);
    }
  }

  function decideAll(status) {
    var items = pending();
    if (!items.length) {
      V.toast('没有待处理的建议', '运行一次任务后会生成新的命名建议。', 'info');
      return;
    }
    items.forEach(function (a) {
      undoStack.push({ id: a.id, previous: a.status });
      a.status = status;
    });
    if (undoStack.length > 24) undoStack.splice(0, undoStack.length - 24);
    render();
    V.log(status === 'approved' ? 'info' : 'warn',
      (status === 'approved' ? '批量通过 ' : '批量忽略 ') + items.length + ' 条命名建议');
    V.toast(status === 'approved' ? '已全部通过' : '已全部忽略',
      items.length + ' 条建议已更新，可点击「撤销上一步」回退。',
      status === 'approved' ? 'success' : 'info');
  }

  function undo() {
    var last = undoStack.pop();
    if (!last) return;
    var item = state.approvals.find(function (a) { return a.id === last.id; });
    if (item) item.status = last.previous;
    render();
    V.toast('已撤销', '刚才的操作已回退。', 'info');
  }

  function init() {
    if (!q('[data-approval-list]')) return;

    V.on(q('[data-approval-list]'), 'click', function (e) {
      var approve = e.target.closest('[data-approve]');
      var skip = e.target.closest('[data-skip]');
      if (approve) decide(approve.dataset.approve, 'approved');
      else if (skip) decide(skip.dataset.skip, 'skipped');
    });

    V.on(q('[data-approval-list]'), 'keydown', function (e) {
      if (e.key === 'Enter' && e.target.matches('[data-approval-name]')) {
        e.target.blur();
        V.toast('名称已更新', '记得点击「通过」应用这条命名。', 'info');
      }
    });

    V.on(q('[data-approve-all]'), 'click', function () { decideAll('approved'); });
    V.on(q('[data-skip-all]'), 'click', function () { decideAll('skipped'); });
    V.on(q('[data-approval-undo]'), 'click', undo);

    V.bus.on('approvals:update', render);
    render();
  }

  V.onReady(init);
})();
