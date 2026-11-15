/* VAIR · 实时进度
 *
 * 六段流水线的每个阶段说明与「当前值」都由后端真实数据拼出来：
 * 分辨率、帧数、字幕字数、模型名、各阶段耗时 —— 都不是写死的文案。
 * 后端没连上时，这些格子显示「等待数据」，而不是编一组好看的假数字。
 */
(function () {
  'use strict';

  var V = window.VAIR;
  if (!V) return;

  var state = V.state;
  var STAGES = V.STAGES;

  var STAGE_NOTES = [
    'ffprobe 读取时长、音轨与录制时间，建立素材指纹。',
    'ffmpeg 并发抽关键帧，分片 MP4 会先无损重封装。',
    '语音转写：远程接口优先，不可用自动回退本地模型。',
    '抽帧与转写并行完成，合成结构化上下文。',
    '多模态模型提炼主题、标题与标签。',
    '写元数据、重命名文件，并按配置输出 NFO / SRT。'
  ];

  var els = {};
  var lastRendered = { progress: -1, stage: -2, status: '', id: '' };

  function q(sel) {
    return els[sel] || (els[sel] = V.$(sel));
  }

  function live() {
    return !!(V.api && V.api.isLive());
  }

  function stageEls() {
    return V.$$('.stage', q('[data-pipeline]'));
  }

  function currentItem() {
    if (state.task.current) return state.task.current;
    for (var i = 0; i < state.queue.length; i++) {
      if (state.queue[i].status === 'running') return state.queue[i];
    }
    return null;
  }

  function ms(value) {
    if (!value) return '';
    return value >= 1000 ? (value / 1000).toFixed(1) + 's' : value + 'ms';
  }

  /** 每个阶段完成后显示的真实产出。拿不到就留空，不编。 */
  function stageMetric(index, item) {
    if (!item) return '—';
    var timings = item.timings || {};
    switch (index) {
      case 0:
        return [item.resolution, item.duration ? V.formatClock(item.duration) : '',
          ms(timings.probe)].filter(Boolean).join(' · ') || '—';
      case 1:
        return [item.frames ? item.frames + ' 帧' : '', ms(timings.frames)].filter(Boolean).join(' · ') || '—';
      case 2:
        return [item.source || '', item.chars ? item.chars + ' 字' : '',
          ms(timings.transcribe)].filter(Boolean).join(' · ') || '—';
      case 3:
        return item.hasAudio ? '画面 + 音频上下文' : '纯画面上下文';
      case 4:
        return [item.model || '', item.tags && item.tags.length ? item.tags.length + ' 个标签' : '',
          ms(timings.analyze)].filter(Boolean).join(' · ') || '—';
      default:
        return item.newName ? item.newName : (item.notes && item.notes[0]) || '—';
    }
  }

  function stageDetail(index, item) {
    if (!item) return STAGE_NOTES[index];
    var notes = item.notes || [];
    if (index === 5 && notes.length) return notes.join('；');
    if (index === 0 && item.resolution) return STAGE_NOTES[index];
    return STAGE_NOTES[index];
  }

  function estimateRemaining() {
    var t = state.task;
    if (t.status !== 'running' || t.progress < 4 || !t.elapsed) return '—';
    var total = t.elapsed / (t.progress / 100);
    return V.formatClock(Math.max(0, total - t.elapsed));
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
      sub.textContent = '正在执行 ' + (STAGES[t.stage] ? STAGES[t.stage].name : '收尾')
        + ' · 预计剩余 ' + estimateRemaining();
      stage.textContent = STAGES[t.stage] ? STAGES[t.stage].name : '完成';
      if (card) card.classList.add('is-live');
    } else if (t.status === 'done') {
      title.textContent = '本轮处理完成';
      sub.textContent = '共 ' + (t.done || 0) + ' 个文件 · 可前往「命名审批」确认命名';
      stage.textContent = '完成';
      if (card) card.classList.remove('is-live');
    } else if (t.status === 'stopped') {
      title.textContent = '任务已停止';
      sub.textContent = '已处理 ' + (t.done || 0) + ' / ' + (t.total || state.queue.length) + ' 个文件';
      stage.textContent = '已停止';
      if (card) card.classList.remove('is-live');
    } else if (t.status === 'error') {
      title.textContent = '任务中断';
      sub.textContent = t.message || '连续失败过多，已自动停止，请查看运行日志';
      stage.textContent = '出错';
      if (card) card.classList.remove('is-live');
    } else {
      title.textContent = live() ? '空闲中' : '空闲中（演示模式）';
      sub.textContent = live()
        ? '等待任务开始 · 在「工作台」添加素材后点「开始处理」'
        : '未连接后端，页面数据为模拟内容';
      stage.textContent = '—';
      if (card) card.classList.remove('is-live');
    }

    if (bar) bar.style.width = Math.min(100, t.progress) + '%';
    if (percent) percent.textContent = Math.round(t.progress) + '%';
    if (elapsed) elapsed.textContent = V.formatClock(t.elapsed);
  }

  function updateStages(force) {
    var t = state.task;
    var item = currentItem();
    var id = item ? item.id : '';
    if (!force && t.progress === lastRendered.progress && t.stage === lastRendered.stage
        && t.status === lastRendered.status && id === lastRendered.id) {
      return;
    }
    lastRendered = { progress: t.progress, stage: t.stage, status: t.status, id: id };

    stageEls().forEach(function (el) {
      var index = Number(el.dataset.stage);
      var isDone = t.status === 'done' || (t.stage > index);
      var isActive = t.status === 'running' && t.stage === index;
      el.classList.toggle('is-done', isDone);
      el.classList.toggle('is-active', isActive);

      var stateEl = V.$('.stage__state span', el);
      if (stateEl) {
        stateEl.textContent = isDone ? '完成'
          : (isActive ? '进行中'
            : (t.status === 'stopped' && t.stage === index ? '已停止' : '待机'));
      }

      var logEl = V.$('[data-stage-log]', el);
      if (logEl) {
        if (isDone || isActive) logEl.textContent = stageDetail(index, item);
        if (isDone || isActive) logEl.dataset.filled = '1';
        else if (!logEl.dataset.filled) logEl.textContent = '等待进入该阶段。';
      }

      var metric = V.$('[data-stage-metric]', el);
      if (metric) metric.textContent = isDone ? stageMetric(index, item) : '—';
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
    V.bus.on('file:update', function () { updateStages(true); });
    V.bus.on('conn:change', function () {
      updateLive();
      updateStages(true);
    });

    updateLive();
    updateStages(true);
  }

  V.onReady(init);
})();
