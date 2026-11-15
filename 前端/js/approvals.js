/* VAIR · 命名审批
 *
 * 「通过」在后端是**真的执行重命名**，不是标记一下。所以这里所有操作都走接口，
 * 本地状态只是后端返回结果的镜像 —— 用户看到的每一行都必须对应磁盘上的真实状态。
 */
(function () {
  'use strict';

  var V = window.VAIR;
  if (!V) return;

  var state = V.state;
  var els = {};
  var undoStack = [];
  var busy = false;

  function q(sel) {
    return els[sel] || (els[sel] = V.$(sel));
  }

  function live() {
    return !!(V.api && V.api.isLive());
  }

  function find(id) {
    for (var i = 0; i < state.approvals.length; i++) {
      if (state.approvals[i].id === id) return state.approvals[i];
    }
    return null;
  }

  function pending() {
    return state.approvals.filter(function (a) { return a.status === 'pending'; });
  }

  function renderSummary() {
    var list = state.approvals;
    var p = list.filter(function (a) { return a.status === 'pending'; }).length;
    var ok = list.filter(function (a) { return a.status === 'approved'; }).length;
    var skip = list.filter(function (a) {
      return a.status !== 'pending' && a.status !== 'approved';
    }).length;

    var map = {
      '[data-approval-pending]': p,
      '[data-approval-approved]': ok,
      '[data-approval-skipped]': skip
    };
    Object.keys(map).forEach(function (sel) {
      var el = V.$(sel);
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
    var showEmpty = list.length === 0 || p === 0;

    if (empty) {
      empty.hidden = !showEmpty;
      var title = V.$('h3', empty);
      var desc = V.$('p', empty);
      if (list.length === 0) {
        if (title) title.textContent = '还没有命名建议';
        if (desc) {
          desc.textContent = live()
            ? '在工作台添加素材并开始处理后，AI 给出的命名会出现在这里等你确认。'
            : '当前是演示模式，未连接后端。';
        }
      } else {
        if (title) title.textContent = '全部处理完了';
        if (desc) desc.textContent = '命名建议已应用。可以回到工作台继续添加素材，或在日志里查看完整记录。';
      }
    }
    if (listEl) listEl.hidden = showEmpty;
  }

  function rowHTML(item) {
    var tags = (item.tags || []).map(function (t, i) {
      return '<span class="tag' + (i === 0 ? ' tag--violet' : '') + '">' + V.escapeHTML(t) + '</span>';
    }).join('');
    var conf = Math.round((item.confidence || 0) * 100);
    var minutes = Math.floor((item.duration || 0) / 60);
    var seconds = (item.duration || 0) % 60;
    var fact = [];
    if (item.cost) fact.push(item.cost.toFixed(1) + 's');
    if (item.model) fact.push(item.model);
    if (item.frames) fact.push(item.frames + ' 帧');
    if (item.chars) fact.push(item.chars + ' 字');

    return '<article class="approval-row" data-approval="' + V.escapeHTML(item.id) + '">' +
      '<div class="approval-thumb">' +
        '<span class="approval-thumb__play"><svg class="icon"><use href="#i-play"/></svg></span>' +
        '<span class="approval-thumb__time">' + V.pad(minutes) + ':' + V.pad(seconds) + '</span>' +
      '</div>' +
      '<div class="approval-main">' +
        '<p class="approval-original"><svg class="icon"><use href="#i-file-video"/></svg>' +
          V.escapeHTML(item.original) + '</p>' +
        '<div class="approval-name">' +
          '<input type="text" value="' + V.escapeHTML(item.name) + '" aria-label="编辑新文件名" ' +
            'data-approval-name="' + V.escapeHTML(item.id) + '" spellcheck="false">' +
        '</div>' +
        '<div class="approval-meta">' + tags +
          '<span class="confidence"><span class="confidence__track"><span style="width:' + conf +
            '%"></span></span>置信度 ' + conf + '%</span>' +
          (fact.length ? '<span class="muted mono">' + V.escapeHTML(fact.join(' · ')) + '</span>' : '') +
        '</div>' +
        (item.error ? '<p class="approval-original muted">' + V.escapeHTML(item.error) + '</p>' : '') +
      '</div>' +
      '<div class="approval-actions">' +
        '<button class="btn btn--ghost" type="button" data-skip="' + V.escapeHTML(item.id) +
          '"><svg class="icon"><use href="#i-skip"/></svg>忽略</button>' +
        '<button class="btn btn--primary" type="button" data-approve="' + V.escapeHTML(item.id) +
          '"><svg class="icon"><use href="#i-check"/></svg>通过并改名</button>' +
      '</div>' +
    '</article>';
  }

  function renderList() {
    var listEl = q('[data-approval-list]');
    if (!listEl) return;
    listEl.innerHTML = pending().map(rowHTML).join('');
  }

  function renderApprovalMode() {
    var button = q('[data-approval-mode]');
    if (!button) return;
    var cfg = state.config || {};
    var enabled = !(cfg.output && cfg.output.require_approval === false);
    button.setAttribute('aria-checked', enabled ? 'true' : 'false');
    button.disabled = !live();
  }

  function render() {
    renderList();
    renderSummary();
    renderApprovalMode();
  }

  /* ---------------- 操作 ---------------- */

  function applyServerReviews(reviews) {
    if (reviews) V.api.applyReviews(reviews);
    else render();
  }

  function decide(id, status) {
    var item = find(id);
    if (!item || item.status !== 'pending' || busy) return;

    var nameInput = V.$('[data-approval-name="' + id + '"]');
    var name = nameInput ? nameInput.value.trim() : '';
    var row = V.$('[data-approval="' + id + '"]');

    if (!live()) {
      // 演示模式：只改本地状态，不假装改文件
      undoStack.push({ id: id, previous: item.status });
      if (undoStack.length > 12) undoStack.shift();
      if (name) item.name = name;
      item.status = status;
      render();
      return;
    }

    busy = true;
    if (row) row.classList.add('is-leaving');

    V.api.resolveReview(id, status, name || item.name).then(function (result) {
      applyServerReviews(result.reviews);
      if (!result.ok) {
        V.toast('没能应用', result.message || '后端拒绝了这次操作', 'warn');
        return;
      }
      if (status !== 'approved') {
        undoStack.push({ id: id, previous: 'pending' });
        if (undoStack.length > 12) undoStack.shift();
      }
      if (status === 'approved') {
        V.log('info', '命名已通过 · ' + (name || item.name));
      } else {
        V.log('warn', '命名建议已忽略 · ' + item.original);
      }
    }).catch(function (err) {
      if (row) row.classList.remove('is-leaving');
      V.toast('操作失败', V.api.errorText(err), 'error');
    }).then(function () {
      busy = false;
    });
  }

  function decideAll(status) {
    var items = pending();
    if (!items.length) {
      V.toast('没有待处理的建议', '运行一次任务后会生成新的命名建议。', 'info');
      return;
    }

    if (!live()) {
      items.forEach(function (a) {
        undoStack.push({ id: a.id, previous: a.status });
        a.status = status;
      });
      if (undoStack.length > 24) undoStack.splice(0, undoStack.length - 24);
      render();
      return;
    }

    busy = true;
    V.toast('正在批量处理', items.length + ' 条建议逐条应用到磁盘…', 'info');

    var chain = Promise.resolve();
    var done = 0;
    var failed = 0;

    items.forEach(function (a) {
      chain = chain.then(function () {
        var input = V.$('[data-approval-name="' + a.id + '"]');
        var name = input ? input.value.trim() : '';
        return V.api.resolveReview(a.id, status, name || a.name)
          .then(function (result) {
            applyServerReviews(result.reviews);
            if (result.ok) done += 1; else failed += 1;
          })
          .catch(function () { failed += 1; });
      });
    });

    chain.then(function () {
      busy = false;
      undoStack = [];
      render();
      V.log(status === 'approved' ? 'info' : 'warn',
        (status === 'approved' ? '批量通过 ' : '批量忽略 ') + done + ' 条命名建议'
        + (failed ? '，失败 ' + failed + ' 条' : ''));
      V.toast(failed ? '部分完成' : (status === 'approved' ? '已全部通过' : '已全部忽略'),
        done + ' 条已更新' + (failed ? '，' + failed + ' 条失败，详情见日志' : ''),
        failed ? 'warn' : 'success');
    });
  }

  function undo() {
    var last = undoStack.pop();
    if (!last) return;
    if (!live()) {
      var item = find(last.id);
      if (item) item.status = last.previous;
      render();
      V.toast('已撤销', '刚才的操作已回退。', 'info');
      return;
    }
    // 后端只支持把状态退回「待审批」，这也正是撤销想要的语义
    var item = find(last.id);
    if (item && item.status === 'approved') {
      V.toast('无法撤销已落盘的改名', '文件已经在磁盘上改名，不能只改审批状态。请手动改回原名。', 'warn');
      return;
    }
    V.api.resolveReview(last.id, 'pending', null).then(function (result) {
      applyServerReviews(result.reviews);
      V.toast('已撤销', '这条建议回到待审批状态。', 'info');
    }).catch(function (err) {
      V.toast('撤销失败', V.api.errorText(err), 'error');
    });
  }

  /* ---------------- 接线 ---------------- */

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
        V.toast('名称已修改', '点「通过并改名」才会真正应用到磁盘。', 'info');
      }
    });

    V.on(q('[data-approval-mode]'), 'click', function () {
      if (!live()) return;
      var current = !((state.config || {}).output && state.config.output.require_approval === false);
      var button = q('[data-approval-mode]');
      button.disabled = true;
      V.api.saveConfig({ output: { require_approval: !current } }).then(function () {
        renderApprovalMode();
        V.toast('命名审批设置已保存', current ? '下次任务处理完成后将直接落盘。' :
          '下次任务将等待审批通过后再落盘。', 'success');
      }).catch(function (err) {
        V.toast('设置保存失败', V.api.errorText(err), 'error');
      }).then(function () { renderApprovalMode(); });
    });

    V.on(q('[data-approve-all]'), 'click', function () { decideAll('approved'); });
    V.on(q('[data-skip-all]'), 'click', function () { decideAll('skipped'); });
    V.on(q('[data-approval-undo]'), 'click', undo);

    V.bus.on('approvals:update', render);
    V.bus.on('config:update', renderApprovalMode);
    V.bus.on('conn:change', render);
    render();
  }

  V.onReady(init);
})();
