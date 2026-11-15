(function () {
  'use strict';

  var V = window.VAIR;
  if (!V) return;

  var state = V.state;
  var STAGES = V.STAGES;
  var STAGE_NOTES = [
    'ffprobe 读取时长、音轨、creation_time，建立素材指纹。',
    'ffmpeg 并发抽关键帧，生成视觉摘要。',
    'Whisper 单路串行（占 GPU），输出带时间轴字幕。',
    '视觉摘要与字幕对齐，合成结构化上下文。',
    '多模态模型提炼主题、标题、简介与标签。',
    '写入元数据、重命名文件，并按规则输出 NFO / SRT。'
  ];

  var STAGE_DONE_LOGS = [
    'ffprobe 完成 · 1920×1080 · 29.97fps · AAC 48kHz',
    '抽帧完成 · 24 张关键帧已缓存',
    'Whisper 完成 · 检测语言 zh · 1,284 字',
    '合并完成 · 2.4k tokens 上下文',
    'AI 分析完成 · 标题 / 简介 / 标签已生成',
    '落盘完成 · 已写入 NFO 与 SRT'
  ];

  var els = {};
  var lastRendered = { progress: -1, stage: -2, status: '' };

  function q(sel) {
    return els[sel] || (els[sel] = V.$(sel));
  }

  function stageEls() {
    return V.$$('.stage', q('[data-pipeline]'));
  }

  function updateLive() {
    var t = state.task;
    var title = q('[data-live-title]');
    var sub = q('[data-live-sub]');
    var bar = q('[data-live-bar]');
    var percent = q('[data-live-percent]');
    var stage = q('[data-live-stage]');
    var elapsed = q('[data-live-elapsed]');
    var card = q('.live-card');

    if (!title) return;

    if (t.status === 'running') {
      title.textContent = t.currentFile || '处理中';
      sub.textContent = '正在执行 ' + (STAGES[t.stage] ? STAGES[t.stage].name : '收尾') + ' · 预计剩余 ' + estimateRemaining() + ' 秒';
      stage.textContent = STAGES[t.stage] ? STAGES[t.stage].name : '完成';
      card.classList.add('is-live');
    } else if (t.status === 'done') {
      title.textContent = '本轮处理完成';
      sub.textContent = '已生成命名建议 · 可前往命名审批确认';
      stage.textContent = '完成';
      card.classList.remove('is-live');
    } else if (t.status === 'stopped') {
      title.textContent = '任务已停止';
      sub.textContent = '进度已保留 · 可回到工作台重新开始';
      stage.textContent = '已停止';
      card.classList.remove('is-live');
    } else {
      title.textContent = '空闲中';
      sub.textContent = '等待任务开始 · 引擎与模型均已就绪';
      stage.textContent = '—';
      card.classList.remove('is-live');
    }

    bar.style.width = t.progress + '%';
    percent.textContent = Math.round(t.progress) + '%';
    elapsed.textContent = V.formatClock(t.elapsed);
  }

  function estimateRemaining() {
    var t = state.task;
    if (t.progress < 4) return '—';
    var total = t.elapsed / (t.progress / 100);
    return Math.max(0, Math.round(total - t.elapsed));
  }

  function updateStages(force) {
    var t = state.task;
    if (!force && t.progress === lastRendered.progress && t.stage === lastRendered.stage && t.status === lastRendered.status) return;
    lastRendered = { progress: t.progress, stage: t.stage, status: t.status };

    stageEls().forEach(function (el) {
      var index = Number(el.dataset.stage);
      var isDone = t.status === 'done' || (t.stage > index);
      var isActive = t.status === 'running' && t.stage === index;
      el.classList.toggle('is-done', isDone);
      el.classList.toggle('is-active', isActive);

      var stateEl = V.$('.stage__state span', el);
      if (stateEl) stateEl.textContent = isDone ? '完成' : (isActive ? '进行中' : (t.status === 'stopped' && t.stage === index ? '已停止' : '待机'));

      var logEl = V.$('[data-stage-log]', el);
      if (logEl) {
        if (isDone) logEl.textContent = STAGE_DONE_LOGS[index] || STAGE_NOTES[index];
        else if (isActive) logEl.textContent = STAGE_NOTES[index];
        else if (!logEl.dataset.filled) logEl.textContent = '等待进入该阶段。';
        if (isDone || isActive) logEl.dataset.filled = '1';
      }

      var metric = V.$('[data-stage-metric]', el);
      if (metric && isDone) {
        var metrics = ['03:06', '24 张', 'zh · 1,284 字', '2.4k tokens', 'qwen2.5-vl', 'MP4 + NFO + SRT'];
        metric.textContent = metrics[index] || '—';
      }
    });
  }

  function initStageToggles() {
    stageEls().forEach(function (el) {
      var head = V.$('.stage__head', el);
      V.on(head, 'click', function () {
        var open = el.classList.toggle('is-open');
        head.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
    });
  }

  function init() {
    if (!q('[data-pipeline]')) return;
    initStageToggles();

    V.bus.on('task:update', function () {
      updateLive();
      updateStages(false);
    });
    V.bus.on('status:update', function () {
      updateLive();
      updateStages(true);
    });

    updateLive();
    updateStages(true);
  }

  V.onReady(init);
})();
