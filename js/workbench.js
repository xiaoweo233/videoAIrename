(function () {
  'use strict';

  var V = window.VAIR;
  if (!V) return;

  var $ = V.$;
  var $$ = V.$$;
  var state = V.state;
  var STAGES = V.STAGES;

  var els = {};
  var sampleCursor = 0;
  var tickTimer = 0;
  var logTimer = 0;
  var elapsedTimer = 0;
  var queueSeq = 0;

  var STAGE_LOGS = [
    'ffprobe 读取容器信息 · duration / fps / creation_time',
    'ffmpeg 抽帧完成 · 关键帧已写入缓存',
    'Whisper 转写完成 · 已生成带时间轴字幕',
    '视觉摘要与字幕对齐 · 上下文构建完成',
    '多模态模型返回标题与标签 · 置信度 0.94',
    '元数据写入完成 · 文件重命名成功'
  ];

  function q(sel) {
    return els[sel] || (els[sel] = $(sel));
  }

  function uid() {
    queueSeq += 1;
    return 'f' + Date.now().toString(36) + queueSeq;
  }

  function addFile(file) {
    state.queue.push({
      id: uid(),
      name: file.name,
      size: file.size || 0,
      duration: file.duration || 0,
      title: file.title || '',
      tags: file.tags || []
    });
    renderQueue();
  }

  function addSamples(count) {
    var n = count || 3;
    var added = 0;
    for (var i = 0; i < n; i++) {
      var sample = V.SAMPLES[sampleCursor % V.SAMPLES.length];
      sampleCursor += 1;
      if (state.queue.some(function (f) { return f.name === sample.name; })) continue;
      addFile(sample);
      added += 1;
    }
    if (added) {
      V.toast('已加入 ' + added + ' 个素材', '可以继续添加，或直接开始处理。', 'success');
    } else {
      V.toast('示例素材已在队列中', '点击「开始处理」即可体验完整流水线。', 'info');
    }
    return added;
  }

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

    var total = state.queue.reduce(function (sum, f) { return sum + (f.size || 0); }, 0);
    if (count) count.textContent = state.queue.length + ' 个文件 · ' + V.formatBytes(total * 1024 * 1024);

    list.innerHTML = state.queue.map(function (f, i) {
      return '<span class="queue-chip" style="animation-delay:' + (i * 40) + 'ms">' +
        '<svg class="icon"><use href="#i-file-video"/></svg>' +
        '<span title="' + V.escapeHTML(f.name) + '">' + V.escapeHTML(f.name) + '</span>' +
        '<small>' + (f.size ? f.size + ' MB' : '—') + '</small>' +
        '<button class="queue-chip__remove" type="button" data-remove="' + f.id + '" aria-label="移除 ' + V.escapeHTML(f.name) + '">' +
        '<svg class="icon"><use href="#i-x"/></svg></button></span>';
    }).join('');
  }

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

  function setStatus(status) {
    state.task.status = status;
    V.bus.emit('task:update');
    V.bus.emit('status:update', status);
  }

  function updateProgress(progress) {
    state.task.progress = V.clamp(progress, 0, 100);
    state.task.stage = stageIndexFor(state.task.progress);
    if (state.task.status === 'running') {
      state.task.elapsed = (Date.now() - state.task.startedAt) / 1000;
    }
    V.bus.emit('task:update');
  }

  function currentFile() {
    if (!state.queue.length) return null;
    return state.queue[Math.min(state.task.currentIndex, state.queue.length - 1)];
  }

  function start() {
    if (state.task.status === 'running') return;

    if (!state.queue.length) {
      addSamples(3);
      V.toast('已载入示例素材', '再次点击「开始处理」即可运行完整流水线。', 'info');
      return;
    }

    var file = currentFile();
    state.task.status = 'running';
    state.task.progress = 0;
    state.task.stage = 0;
    state.task.startedAt = Date.now();
    state.task.elapsed = 0;
    state.task.currentFile = file ? file.name : null;

    setStatus('running');
    updateProgress(0);
    V.log('info', '任务启动 · 队列 ' + state.queue.length + ' 个文件' + (state.preview ? ' · 预览模式' : ''));
    if (file) V.log('debug', '开始处理 ' + file.name);
    V.toast('任务已开始', '正在处理 ' + (file ? file.name : '队列'), 'success');

    window.clearInterval(tickTimer);
    window.clearInterval(logTimer);
    window.clearInterval(elapsedTimer);

    tickTimer = window.setInterval(function () {
      var remaining = 100 - state.task.progress;
      var step = Math.max(0.35, remaining * 0.028) * (0.7 + Math.random() * 0.6);
      var next = state.task.progress + step;
      if (next >= 100) {
        updateProgress(100);
        finish();
        return;
      }
      updateProgress(next);
    }, 260);

    logTimer = window.setInterval(function () {
      var idx = Math.min(STAGES.length - 1, state.task.stage);
      var msg = STAGE_LOGS[idx] || STAGE_LOGS[0];
      V.log(idx === 4 ? 'debug' : 'info', msg, (0.2 + Math.random() * 1.4).toFixed(2) + 's');
    }, 2600);

    elapsedTimer = window.setInterval(function () {
      state.task.elapsed = (Date.now() - state.task.startedAt) / 1000;
      V.bus.emit('task:update');
    }, 1000);
  }

  function stop(silent) {
    if (state.task.status !== 'running') return;
    window.clearInterval(tickTimer);
    window.clearInterval(logTimer);
    window.clearInterval(elapsedTimer);
    tickTimer = logTimer = elapsedTimer = 0;
    setStatus('stopped');
    state.stats.canceled += 1;
    V.bus.emit('stats:update');
    V.log('warn', '任务被手动停止 · 已完成 ' + Math.round(state.task.progress) + '%');
    if (!silent) V.toast('任务已停止', '进度已保留，可随时继续。', 'warn');
  }

  function addApproval(file, elapsed) {
    var base = (file.title || file.name.replace(/\.[^.]+$/, '')).trim();
    var date = new Date();
    var stamp = date.getFullYear() + '-' + V.pad(date.getMonth() + 1) + '-' + V.pad(date.getDate());
    var name = stamp + '_' + base + '.mp4';
    if (state.approvals.some(function (a) { return a.original === file.name; })) return;
    state.approvals.unshift({
      id: 'a' + Date.now().toString(36),
      original: file.name,
      name: name,
      tags: (file.tags && file.tags.length ? file.tags : ['待归类']).slice(0, 3),
      confidence: 0.88 + Math.random() * 0.1,
      duration: file.duration || 120,
      size: file.size || 0,
      status: 'pending',
      cost: elapsed
    });
    V.bus.emit('approvals:update');
  }

  function finish() {
    window.clearInterval(tickTimer);
    window.clearInterval(logTimer);
    window.clearInterval(elapsedTimer);
    tickTimer = logTimer = elapsedTimer = 0;

    var elapsed = Math.max(1, (Date.now() - state.task.startedAt) / 1000);
    state.task.elapsed = elapsed;
    state.task.status = 'done';
    state.task.progress = 100;
    state.task.stage = STAGES.length;
    state.stats.success += 1;
    state.stats.completed += 1;
    state.stats.seconds += elapsed;
    setStatus('done');
    V.bus.emit('stats:update');

    var file = currentFile();
    if (file) {
      V.log('info', '处理完成 · ' + file.name + ' → ' + (file.title || '未命名'), elapsed.toFixed(1) + 's');
      state.timelines.unshift({
        name: file.title ? file.title + '.mp4' : file.name,
        model: 'qwen2.5-vl-72b',
        frames: 24,
        chars: 860 + Math.floor(Math.random() * 400),
        cost: elapsed
      });
      V.bus.emit('timeline:update');
      addApproval(file, elapsed);
    }

    V.toast('处理完成', '已生成命名建议，前往「命名审批」确认。', 'success');
  }

  function resetTask() {
    window.clearInterval(tickTimer);
    window.clearInterval(logTimer);
    window.clearInterval(elapsedTimer);
    tickTimer = logTimer = elapsedTimer = 0;
    state.task.status = 'idle';
    state.task.progress = 0;
    state.task.stage = -1;
    state.task.currentFile = null;
    state.task.elapsed = 0;
    setStatus('idle');
    updateProgress(0);
  }

  V.engine = {
    start: start,
    stop: stop,
    reset: resetTask,
    addSamples: addSamples,
    addFile: addFile
  };

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
    var labels = { idle: '待机', running: '处理中', stopped: '已停止', done: '已完成' };
    var pill = q('[data-task-pill]');
    var ppill = q('[data-progress-pill]');
    var startBtn = q('[data-start]');
    var stopBtn = q('[data-stop]');

    [pill, ppill].forEach(function (p) {
      if (!p) return;
      p.classList.remove('is-running', 'is-done', 'is-stopped');
      if (status === 'running') p.classList.add('is-running');
      if (status === 'done') p.classList.add('is-done');
      if (status === 'stopped') p.classList.add('is-stopped');
      var span = p.querySelector('span');
      if (span) span.textContent = labels[status] || '待机';
    });

    if (startBtn) {
      startBtn.disabled = status === 'running';
      var label = $('[data-start-label]', startBtn);
      if (label) label.textContent = status === 'running' ? '处理中…' : (status === 'done' ? '再次处理' : '开始处理');
    }
    if (stopBtn) stopBtn.disabled = status !== 'running';

    var gpu = q('[data-gpu-label]');
    if (gpu) gpu.textContent = status === 'running' ? 'GPU 占用 78%' : 'CUDA 加速可用';
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

    if (fill) fill.style.width = progress + '%';
    if (track) track.setAttribute('aria-valuenow', String(rounded));
    if (percent) percent.innerHTML = rounded + '<span>%</span>';
    if (card) card.classList.toggle('is-running', state.task.status === 'running');

    var stage = state.task.stage;
    if (title) {
      if (state.task.status === 'running') title.textContent = stageName(stage);
      else if (state.task.status === 'done') title.textContent = '全部完成';
      else if (state.task.status === 'stopped') title.textContent = '已停止';
      else title.textContent = '空闲';
    }
    if (file) {
      file.textContent = state.task.status === 'running' && state.task.currentFile
        ? state.task.currentFile
        : (state.task.status === 'done' ? '结果已写入' : '等待素材');
    }
    if (caption) {
      caption.textContent = (state.task.status === 'done' ? 1 : 0) + ' / ' + (state.queue.length || 0) + ' 个文件';
    }
    if (eta) {
      if (state.task.status === 'running' && progress > 4) {
        var total = state.task.elapsed / (progress / 100);
        eta.textContent = '预计剩余 ' + V.formatClock(Math.max(0, total - state.task.elapsed));
      } else if (state.task.status === 'done') {
        eta.textContent = '总耗时 ' + state.task.elapsed.toFixed(1) + ' 秒';
      } else {
        eta.textContent = '预计剩余 —';
      }
    }
  }

  function renderPreview() {
    var sw = q('[data-preview-toggle]');
    if (sw) sw.setAttribute('aria-checked', state.preview ? 'true' : 'false');
  }

  function init() {
    var zone = q('[data-dropzone]');
    var dragDepth = 0;

    document.addEventListener('dragenter', function (e) {
      if (!e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') < 0) return;
      e.preventDefault();
      dragDepth += 1;
      zone.classList.add('is-drag');
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
      if (dragDepth === 0) zone.classList.remove('is-drag');
    });

    document.addEventListener('drop', function (e) {
      e.preventDefault();
      dragDepth = 0;
      zone.classList.remove('is-drag');
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) {
        Array.prototype.slice.call(files).slice(0, 20).forEach(function (f) {
          addFile({ name: f.name, size: Math.round(f.size / 1024 / 1024) });
        });
        V.toast('已添加 ' + Math.min(files.length, 20) + ' 个文件', '前端演示不会读取真实文件内容。', 'success');
      }
    });

    $$('[data-add-sample]').forEach(function (btn) {
      V.on(btn, 'click', function () { addSamples(3); });
    });

    V.on(q('[data-clear-queue]'), 'click', function () {
      if (!state.queue.length) {
        V.toast('队列已经是空的', '拖入素材或点击选择视频开始。', 'info');
        return;
      }
      state.queue = [];
      renderQueue();
      resetTask();
      V.toast('队列已清空', '可以重新添加素材。', 'success');
    });

    V.on(q('[data-queue-list]'), 'click', function (e) {
      var btn = e.target.closest('[data-remove]');
      if (!btn) return;
      state.queue = state.queue.filter(function (f) { return f.id !== btn.dataset.remove; });
      renderQueue();
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
      var segments = value.split(/[\\/]/).filter(Boolean);
      var name = segments[segments.length - 1] || '素材文件夹';
      addFile({ name: name + '（文件夹）', size: 0 });
      input.value = '';
      V.toast('路径已加入队列', value, 'success');
    });

    V.on(q('[data-start]'), 'click', start);
    V.on(q('[data-stop]'), 'click', function () { stop(false); });
    V.on(q('[data-preview-toggle]'), 'click', function () {
      state.preview = !state.preview;
      renderPreview();
      V.toast(state.preview ? '预览模式已开启' : '预览模式已关闭',
        state.preview ? '只计算不落盘，适合先确认命名效果。' : '处理结果会直接写入磁盘。', 'info');
    });

    V.bus.on('action:add-sample', function () { addSamples(3); });
    V.bus.on('action:start', function () { start(); });
    V.bus.on('task:update', function () { renderProgress(); renderStatus(); });
    V.bus.on('stats:update', renderStats);
    V.bus.on('status:update', renderStatus);

    renderQueue();
    renderStats();
    renderProgress();
    renderStatus();
    renderPreview();
  }

  V.onReady(init);
})();
