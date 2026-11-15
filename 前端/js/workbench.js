/* VAIR · 工作台
 *
 * 职责边界：这里只负责「把意图发给后端」和「把后端状态画出来」。
 * 进度不靠前端计时器模拟 —— 进度来自 SSE，后端说多少就是多少。
 * 连不上后端（file:// 打开原型）时才回落到内置模拟，保证演示可用。
 */
(function () {
  'use strict';

  var V = window.VAIR;
  if (!V) return;

  var $ = V.$;
  var $$ = V.$$;
  var state = V.state;
  var STAGES = V.STAGES;

  var els = {};
  var elapsedTimer = 0;
  var sampleCursor = 0;
  var dragDepth = 0;

  function q(sel) {
    return els[sel] || (els[sel] = $(sel));
  }

  function live() {
    return !!(V.api && V.api.isLive());
  }

  function offlineToast() {
    V.toast('后端未连接', '当前是原型演示模式，只会展示模拟数据。请通过后端启动页面访问。', 'warn');
  }

  /* ---------------- 队列 ---------------- */

  var STATUS_TEXT = {
    pending: '等待处理',
    running: '处理中',
    review: '待审批',
    success: '已完成',
    skipped: '已跳过',
    failed: '失败',
    canceled: '已取消'
  };

  function renderQueue() {
    var strip = q('[data-queue-strip]');
    var list = q('[data-queue-list]');
    var count = q('[data-queue-count]');
    if (!strip || !list) return;

    var has = state.queue.length > 0;
    strip.hidden = !has;
    if (!has) {
      list.innerHTML = '';
      if (count) count.textContent = '0 个文件';
      return;
    }

    var total = state.queue.reduce(function (sum, f) { return sum + (f.bytes || 0); }, 0);
    if (count) count.textContent = state.queue.length + ' 个文件 · ' + V.formatBytes(total);

    list.innerHTML = state.queue.map(function (f, i) {
      var status = f.status || 'pending';
      var tip = f.name + '（' + (STATUS_TEXT[status] || status) + '）'
        + (f.error ? '\n' + f.error : '')
        + (f.newName ? '\n→ ' + f.newName : '');
      return '<span class="queue-chip" data-status="' + status + '" style="animation-delay:' + (i * 40) + 'ms">' +
        '<svg class="icon"><use href="#i-file-video"/></svg>' +
        '<span title="' + V.escapeHTML(tip) + '">' + V.escapeHTML(f.name) + '</span>' +
        '<small>' + (f.bytes ? V.formatBytes(f.bytes) + (f.duration ? ' · ' + V.formatClock(f.duration) : '') : '—') + '</small>' +
        '<button class="queue-chip__remove" type="button" data-remove="' + V.escapeHTML(f.id) + '" aria-label="移除 ' + V.escapeHTML(f.name) + '">' +
        '<svg class="icon"><use href="#i-x"/></svg></button></span>';
    }).join('');
  }

  function addPaths(paths) {
    if (!live()) {
      // 演示模式：只把名字挂上去，不假装读了文件
      paths.slice(0, 20).forEach(function (p) {
        var name = String(p).split(/[\\/]/).filter(Boolean).pop() || '素材';
        if (state.queue.some(function (f) { return f.name === name; })) return;
        state.queue.push({ id: 'demo-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6),
          name: name, bytes: 0, size: 0, duration: 0, status: 'pending' });
      });
      renderQueue();
      offlineToast();
      return;
    }

    V.api.addPaths(paths).then(function (result) {
      if (result.added) {
        V.toast('已加入 ' + result.added + ' 个素材',
          result.skipped ? '跳过重复 ' + result.skipped + ' 个。' : '可以开始处理了。', 'success');
      } else if (result.errors && result.errors.length) {
        V.toast('没能加入素材', result.errors[0], 'warn');
      } else {
        V.toast('没有新增素材', '这些文件已经在队列里了。', 'info');
      }
    }).catch(function (err) {
      V.toast('加入失败', V.api.errorText(err), 'error');
    });
  }

  /** 上次成功添加的目录：下次开对话框直接定位过去，省得每次从仓库根翻。 */
  function lastDir() {
    return V.store.get('lastDir', '') || '';
  }

  function rememberDirs(paths) {
    var dir = '';
    (paths || []).forEach(function (p) {
      var parts = String(p).split(/[\\/]/);
      parts.pop();
      var candidate = parts.join('\\');
      if (candidate && (!dir || candidate.length < dir.length)) dir = candidate;
    });
    if (dir) V.store.set('lastDir', dir);
  }

  function pickFiles() {
    if (!live()) { offlineToast(); return; }
    V.api.selectFiles(lastDir()).then(function (result) {
      if (!result.ok) {
        V.toast('无法打开对话框', result.error || '', 'warn');
        return;
      }
      if (result.cancelled || !result.paths || !result.paths.length) return;
      rememberDirs(result.paths);
      addPaths(result.paths);
    }).catch(function (err) {
      V.toast('打开文件对话框失败', V.api.errorText(err), 'error');
    });
  }

  function pickFolder() {
    if (!live()) { offlineToast(); return; }
    V.api.selectFolder(lastDir()).then(function (result) {
      if (!result.ok) {
        V.toast('无法打开对话框', result.error || '', 'warn');
        return;
      }
      if (result.cancelled || !result.paths || !result.paths.length) return;
      rememberDirs(result.paths);
      addPaths(result.paths);
    }).catch(function (err) {
      V.toast('打开文件夹对话框失败', V.api.errorText(err), 'error');
    });
  }

  /* ---------------- 示例素材（仅演示模式有意义） ---------------- */

  function addSamples(count) {
    if (live()) {
      V.toast('不需要示例素材', '后端已连上，点「选择视频」或「选择文件夹」添加真实素材即可。', 'info');
      return 0;
    }
    var n = count || 3;
    var added = 0;
    for (var i = 0; i < n; i++) {
      var sample = V.SAMPLES[sampleCursor % V.SAMPLES.length];
      sampleCursor += 1;
      if (state.queue.some(function (f) { return f.name === sample.name; })) continue;
      state.queue.push({
        id: 'demo-' + Date.now() + '-' + sampleCursor,
        name: sample.name,
        bytes: sample.size * 1024 * 1024,
        size: sample.size,
        duration: sample.duration,
        status: 'pending'
      });
      added += 1;
    }
    renderQueue();
    if (added) V.toast('已加入 ' + added + ' 个示例素材', '演示数据，不会真的处理文件。', 'success');
    else V.toast('示例素材已在队列中', '可以直接点「开始处理」看模拟流程。', 'info');
    return added;
  }

  /* ---------------- 任务 ---------------- */

  function start() {
    if (state.task.status === 'running') return;

    if (!live()) { demoStart(); return; }

    if (!state.queue.length) {
      V.toast('队列是空的', '先点「选择视频」或「选择文件夹」添加素材。', 'warn');
      return;
    }

    V.api.startTask().then(function (result) {
      V.toast('任务已开始', result.message || '正在处理队列。', 'success');
      startElapsedTimer();
    }).catch(function (err) {
      V.toast('无法开始', V.api.errorText(err), 'error');
    });
  }

  function stop(silent) {
    if (!live()) { demoStop(silent); return; }
    V.api.stopTask().then(function (result) {
      if (!result.ok) { V.toast('没有可停止的任务', result.message || '', 'info'); return; }
      if (!silent) V.toast('已请求停止', '正在跑的文件会跑完，之后不再取新文件。', 'warn');
    }).catch(function (err) {
      V.toast('停止失败', V.api.errorText(err), 'error');
    });
  }

  function startElapsedTimer() {
    if (elapsedTimer) return;
    elapsedTimer = window.setInterval(function () {
      if (state.task.status !== 'running') {
        window.clearInterval(elapsedTimer);
        elapsedTimer = 0;
        return;
      }
      var started = state.task.startedAt;
      state.task.elapsed = started ? (Date.now() - started) / 1000 : 0;
      V.bus.emit('task:update');
    }, 1000);
  }

  /* ---------------- 演示模式（file:// 打开时的模拟流水线） ---------------- */

  var demoTick = 0;
  var demoLog = 0;
  var DEMO_LOGS = [
    'ffprobe 读取容器信息 · duration / fps / creation_time',
    '关键帧抽取完成 · 已写入缓存',
    '语音转写完成 · 已生成带时间轴字幕',
    '视觉摘要与字幕对齐 · 上下文构建完成',
    '多模态模型返回标题与标签',
    '元数据写入完成 · 文件重命名成功'
  ];

  function demoStart() {
    if (!state.queue.length) {
      addSamples(3);
      V.toast('已载入示例素材', '再次点击「开始处理」即可跑一遍模拟流程。', 'info');
      return;
    }
    state.task.status = 'running';
    state.task.progress = 0;
    state.task.stage = 0;
    state.task.startedAt = Date.now();
    state.task.elapsed = 0;
    state.task.total = state.queue.length;
    state.task.done = 0;
    state.task.currentFile = state.queue[0].name;
    V.bus.emit('status:update', 'running');
    V.bus.emit('task:update');
    V.log('info', '模拟任务启动 · 队列 ' + state.queue.length + ' 个文件');

    window.clearInterval(demoTick);
    window.clearInterval(demoLog);
    demoTick = window.setInterval(function () {
      var next = state.task.progress + Math.max(0.4, (100 - state.task.progress) * 0.03);
      state.task.progress = V.clamp(next, 0, 100);
      state.task.stage = stageIndexFor(state.task.progress);
      state.task.elapsed = (Date.now() - state.task.startedAt) / 1000;
      if (state.task.progress >= 100) {
        window.clearInterval(demoTick);
        window.clearInterval(demoLog);
        state.task.status = 'done';
        state.task.done = state.task.total;
        state.stats.success += 1;
        state.stats.completed += 1;
        state.stats.seconds += state.task.elapsed;
        V.bus.emit('status:update', 'done');
        V.log('info', '模拟流程结束（演示模式不会真的改文件）');
      }
      V.bus.emit('task:update');
    }, 300);

    demoLog = window.setInterval(function () {
      var idx = Math.min(DEMO_LOGS.length - 1, state.task.stage);
      V.log('debug', DEMO_LOGS[idx]);
    }, 2400);
  }

  function demoStop(silent) {
    if (state.task.status !== 'running') return;
    window.clearInterval(demoTick);
    window.clearInterval(demoLog);
    demoTick = demoLog = 0;
    state.task.status = 'stopped';
    state.stats.canceled += 1;
    V.bus.emit('status:update', 'stopped');
    V.bus.emit('task:update');
    if (!silent) V.toast('已停止', '模拟进度已保留。', 'warn');
  }

  /* ---------------- 渲染 ---------------- */

  function stageIndexFor(progress) {
    if (progress >= 100) return STAGES.length;
    for (var i = 0; i < STAGES.length; i++) {
      if (progress < STAGES[i].to) {
        if (progress < STAGES[i].from) return Math.max(0, i - 1);
        return i;
      }
    }
    return STAGES.length - 1;
  }

  function stageName(index) {
    if (index < 0 || index >= STAGES.length) return '—';
    return STAGES[index].name;
  }

  function renderStats() {
    var s = state.stats;
    var avg = s.completed ? (s.seconds / s.completed) : 0;
    var values = {
      success: s.success,
      skipped: s.skipped,
      failed: s.failed,
      avg: s.completed ? avg.toFixed(1) : '—'
    };

    Object.keys(values).forEach(function (key) {
      var card = $('[data-stat="' + key + '"]');
      if (!card) return;
      var valueEl = $('[data-stat-value]', card);
      var bar = $('.stat__bar span', card);
      var next = values[key];
      if (valueEl && valueEl.textContent !== String(next)) {
        valueEl.textContent = next;
        valueEl.classList.remove('is-bump');
        void valueEl.offsetWidth;
        valueEl.classList.add('is-bump');
      }
      if (bar) {
        var ratio = key === 'avg' ? Math.min(1, avg / 120)
          : Math.min(1, Number(next) / Math.max(4, state.queue.length || 4));
        bar.style.width = (ratio * 100) + '%';
      }
    });
  }

  function renderStatus() {
    var status = state.task.status;
    var labels = { idle: '待机', running: '处理中', stopped: '已停止', done: '已完成', error: '出错了' };
    var pill = q('[data-task-pill]');
    var ppill = q('[data-progress-pill]');
    var startBtn = q('[data-start]');
    var stopBtn = q('[data-stop]');

    [pill, ppill].forEach(function (p) {
      if (!p) return;
      p.classList.remove('is-running', 'is-done', 'is-stopped');
      if (status === 'running') p.classList.add('is-running');
      if (status === 'done') p.classList.add('is-done');
      if (status === 'stopped' || status === 'error') p.classList.add('is-stopped');
      var span = p.querySelector('span');
      if (span) span.textContent = labels[status] || '待机';
    });

    if (startBtn) {
      startBtn.disabled = status === 'running';
      var label = $('[data-start-label]', startBtn);
      if (label) label.textContent = status === 'running' ? '处理中…' : (status === 'done' ? '再次处理' : '开始处理');
    }
    if (stopBtn) stopBtn.disabled = status !== 'running';

    if (status === 'running') startElapsedTimer();

    var gpu = q('[data-gpu-label]');
    if (gpu) {
      var rt = state.runtime;
      if (status === 'running') gpu.textContent = '正在推理';
      else if (rt && rt.cuda && rt.cuda.available) gpu.textContent = 'CUDA 加速可用';
      else if (rt) gpu.textContent = '未检测到显卡';
      else gpu.textContent = 'CUDA 加速可用';
    }
    var bar = $('[data-engine-bar]');
    if (bar) bar.style.width = status === 'running' ? (30 + state.task.progress * 0.6).toFixed(0) + '%' : '8%';
  }

  function renderProgress() {
    var progress = state.task.progress;
    var rounded = Math.round(progress);
    var fill = q('[data-progress-fill]');
    var track = q('[data-progress-track]');
    var percent = q('[data-progress-percent]');
    var title = q('[data-progress-title]');
    var file = q('[data-progress-file]');
    var caption = q('[data-progress-caption]');
    var eta = q('[data-progress-eta]');
    var card = q('.progress-card');
    var status = state.task.status;

    if (fill) fill.style.width = progress + '%';
    if (track) track.setAttribute('aria-valuenow', String(rounded));
    if (percent) percent.innerHTML = rounded + '<span>%</span>';
    if (card) card.classList.toggle('is-running', status === 'running');

    if (title) {
      if (status === 'running') title.textContent = stageName(state.task.stage);
      else if (status === 'done') title.textContent = '全部完成';
      else if (status === 'stopped') title.textContent = '已停止';
      else if (status === 'error') title.textContent = '出错了';
      else title.textContent = '空闲';
    }
    if (file) {
      if (status === 'running' && state.task.currentFile) file.textContent = state.task.currentFile;
      else if (status === 'done') file.textContent = '结果已写入磁盘';
      else if (status === 'error') file.textContent = state.task.message || '查看运行日志';
      else file.textContent = '等待素材';
    }
    if (caption) {
      var total = state.task.total || state.queue.length || 0;
      var done = state.task.done || 0;
      caption.textContent = done + ' / ' + total + ' 个文件';
    }
    if (eta) {
      if (status === 'running' && progress > 4 && state.task.elapsed) {
        var totalSeconds = state.task.elapsed / (progress / 100);
        eta.textContent = '预计剩余 ' + V.formatClock(Math.max(0, totalSeconds - state.task.elapsed));
      } else if (status === 'done') {
        eta.textContent = '总耗时 ' + state.task.elapsed.toFixed(1) + ' 秒';
      } else {
        eta.textContent = '预计剩余 —';
      }
    }
  }

  function renderPreview() {
    var sw = q('[data-preview-toggle]');
    if (!sw) return;
    sw.setAttribute('aria-checked', state.preview ? 'true' : 'false');
  }

  function togglePreview() {
    var next = !state.preview;
    if (!live()) {
      state.preview = next;
      renderPreview();
      offlineToast();
      return;
    }
    V.api.saveConfig({ output: { dry_run: next } }).then(function () {
      state.preview = next;
      renderPreview();
      V.toast(next ? '预览模式已开启' : '预览模式已关闭',
        next ? '只演算命名结果并推送到「命名审批」，不改文件也不写元数据。'
             : '处理结果会直接写入磁盘。', next ? 'info' : 'warn');
    }).catch(function (err) {
      V.toast('切换失败', V.api.errorText(err), 'error');
    });
  }

  /* ---------------- 接线 ---------------- */

  function init() {
    var zone = q('[data-dropzone]');

    document.addEventListener('dragenter', function (e) {
      if (!e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') < 0) return;
      e.preventDefault();
      dragDepth += 1;
      if (zone) zone.classList.add('is-drag');
    });

    document.addEventListener('dragover', function (e) {
      if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') > -1) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
      }
    });

    document.addEventListener('dragleave', function (e) {
      if (!e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') < 0) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0 && zone) zone.classList.remove('is-drag');
    });

    document.addEventListener('drop', function (e) {
      e.preventDefault();
      dragDepth = 0;
      if (zone) zone.classList.remove('is-drag');
      var files = e.dataTransfer && e.dataTransfer.files;
      if (!files || !files.length) return;

      if (live()) {
        // 出于安全，浏览器不会把拖入文件的绝对路径告诉网页；后端需要的是真实路径。
        V.toast('请用「选择视频」按钮', '浏览器拿不到拖入文件的真实路径，'
          + '系统文件对话框才能把路径交给后端。', 'warn');
        return;
      }
      addSamples(3);
    });

    V.on(q('[data-pick-files]'), 'click', pickFiles);
    V.on(q('[data-pick-folder]'), 'click', pickFolder);

    V.on(q('[data-clear-queue]'), 'click', function () {
      if (!state.queue.length) {
        V.toast('队列已经是空的', '用「选择视频」或「选择文件夹」添加素材。', 'info');
        return;
      }
      if (!live()) {
        state.queue = [];
        renderQueue();
        V.toast('队列已清空', '演示数据已清掉。', 'success');
        return;
      }
      V.api.clearItems().then(function () {
        V.toast('队列已清空', '可以重新添加素材。', 'success');
      }).catch(function (err) {
        V.toast('清空失败', V.api.errorText(err), 'error');
      });
    });

    V.on(q('[data-queue-list]'), 'click', function (e) {
      var btn = e.target.closest('[data-remove]');
      if (!btn) return;
      var id = btn.dataset.remove;
      if (!live()) {
        state.queue = state.queue.filter(function (f) { return f.id !== id; });
        renderQueue();
        return;
      }
      V.api.removeItem(id).catch(function (err) {
        V.toast('移除失败', V.api.errorText(err), 'error');
      });
    });

    V.on($('#pathForm'), 'submit', function (e) {
      e.preventDefault();
      var input = $('#pathInput');
      var value = (input.value || '').trim();
      if (!value) {
        input.focus();
        V.toast('请输入素材路径', '例如 D:\\素材\\产品视频', 'warn');
        return;
      }
      input.value = '';
      addPaths([value]);
    });

    V.on(q('[data-start]'), 'click', start);
    V.on(q('[data-stop]'), 'click', function () { stop(false); });
    V.on(q('[data-preview-toggle]'), 'click', togglePreview);

    V.bus.on('action:add-sample', function () { addSamples(3); });
    V.bus.on('action:start', start);
    V.bus.on('task:update', function () { renderProgress(); renderStatus(); });
    V.bus.on('status:update', renderStatus);
    V.bus.on('stats:update', renderStats);
    V.bus.on('queue:update', renderQueue);
    V.bus.on('runtime:update', renderStatus);
    V.bus.on('config:update', function (cfg) {
      if (!cfg || !cfg.output) return;
      state.preview = !!cfg.output.dry_run;
      renderPreview();
    });

    renderQueue();
    renderStats();
    renderProgress();
    renderStatus();
    renderPreview();
  }

  // 握手是异步的，不能在这里同步判断连接状态 —— 等连接结果出来再说
  var toldDemo = false;
  V.bus.on('conn:change', function (conn) {
    if (conn.mode === 'live' || toldDemo) return;
    if (conn.mode === 'demo') {
      toldDemo = true;
      V.log('info', '未连接后端 · 工作台处于模拟模式，拖入或粘贴的路径都不会真的处理');
    }
  });

  V.onReady(init);
})();
