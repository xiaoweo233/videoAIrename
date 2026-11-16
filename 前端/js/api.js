/* VAIR · 后端客户端
 *
 * 所有视图都只通过 `V.api` 访问后端，不各自 fetch —— 这样连接状态、超时、错误文案
 * 只有一个地方要维护。
 *
 * 两条运行路径：
 *   1. `live`  —— 由后端托管（http://127.0.0.1:8000），REST + SSE 全程实时。
 *   2. `demo`  —— 直接用 file:// 打开 index.html，连不上后端，各视图回落到内置模拟引擎。
 * 降级是刻意保留的：给别人看原型时不该先要求他装 Python。
 */
(function () {
  'use strict';

  var V = window.VAIR;
  if (!V) return;

  var state = V.state;
  var IS_FILE = location.protocol === 'file:';
  var LOG_LIMIT = 400;

  /* ---------------- 状态槽 ---------------- */

  state.conn = {
    mode: IS_FILE ? 'demo' : 'connecting',
    online: false,
    error: IS_FILE ? '当前以文件方式打开，未连接后端' : '',
    version: '',
    root: '',
    since: 0,
    retry: 0
  };
  state.services = {};
  state.config = null;
  state.defaults = null;
  state.bootstrap = null;
  state.runtime = null;
  state.server = null;
  state.progress = null;
  state.reviews = [];
  state.approvals = state.approvals || [];

  /* ---------------- HTTP ---------------- */

  function HttpError(status, message) {
    this.name = 'HttpError';
    this.status = status;
    this.message = message || '请求失败';
  }
  HttpError.prototype = Object.create(Error.prototype);
  HttpError.prototype.constructor = HttpError;

  function request(method, path, body, opts) {
    opts = opts || {};
    var timeout = opts.timeout === undefined ? 20000 : opts.timeout;   // 0 = 不超时
    var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timer = (ctrl && timeout > 0) ? window.setTimeout(function () { ctrl.abort(); }, timeout) : 0;

    var init = { method: method, headers: {} };
    if (body !== undefined && body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    if (ctrl) init.signal = ctrl.signal;

    return fetch(path, init).then(function (res) {
      if (timer) window.clearTimeout(timer);
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
        if (!res.ok) {
          var msg = (data && (data.detail || data.message)) || ('HTTP ' + res.status);
          throw new HttpError(res.status, typeof msg === 'string' ? msg : JSON.stringify(msg));
        }
        return data;
      });
    }).catch(function (err) {
      if (timer) window.clearTimeout(timer);
      if (err instanceof HttpError) throw err;
      throw new HttpError(0, err && err.name === 'AbortError' ? '请求超时' : '无法连接后端服务');
    });
  }

  /* ---------------- 数据规整：后端字段 → 前端字段 ---------------- */

  function toMB(bytes) {
    return bytes ? Math.round(bytes / 1024 / 1024) : 0;
  }

  function stageIndexOf(key) {
    var stages = V.STAGES || [];
    for (var i = 0; i < stages.length; i++) {
      if (stages[i].key === key) return i;
    }
    return -1;
  }

  function normItem(raw) {
    if (!raw) return null;
    return {
      id: raw.id,
      name: raw.name,
      path: raw.path,
      size: toMB(raw.size),
      bytes: raw.size || 0,
      duration: Math.round(raw.duration || 0),
      resolution: raw.resolution || '',
      hasAudio: !!raw.has_audio,
      title: raw.title || '',
      plot: raw.plot || '',
      tags: raw.tags || [],
      status: raw.status,
      stage: raw.stage || '',
      progress: raw.progress || 0,
      newName: raw.new_name || '',
      newPath: raw.new_path || '',
      frames: raw.frames || 0,
      chars: raw.chars || 0,
      source: raw.transcribe_source || '',
      model: raw.model || '',
      error: raw.error || '',
      notes: raw.notes || [],
      elapsed: raw.elapsed || 0,
      timings: raw.timings || {},
      raw: raw
    };
  }

  function normReview(raw) {
    if (!raw) return null;
    // 后端说 rejected，前端界面说「忽略」——在这里翻译，视图不用关心
    var status = raw.status === 'rejected' ? 'skipped' : raw.status;
    return {
      id: raw.id,
      original: raw.original,
      path: raw.path,
      name: raw.name,
      title: raw.title || '',
      plot: raw.plot || '',
      tags: raw.tags || [],
      confidence: typeof raw.confidence === 'number' ? raw.confidence : 0.9,
      duration: Math.round(raw.duration || 0),
      size: toMB(raw.size),
      status: status,
      error: raw.error || '',
      cost: raw.elapsed || 0,
      model: raw.model || '',
      frames: raw.frames || 0,
      chars: raw.chars || 0,
      raw: raw
    };
  }

  function applyQueue(items) {
    var prev = {};
    state.queue.forEach(function (f) { prev[f.id] = f; });
    state.queue = (items || []).map(normItem).filter(Boolean);
    // 保留用户正在查看的次要字段（翻页时不被重置）
    state.queue.forEach(function (f) {
      if (prev[f.id] && prev[f.id].uiState) f.uiState = prev[f.id].uiState;
    });
    V.bus.emit('queue:update');
  }

  function mergeItem(raw) {
    var item = normItem(raw);
    if (!item) return;
    var found = false;
    for (var i = 0; i < state.queue.length; i++) {
      if (state.queue[i].id === item.id) {
        if (state.queue[i].uiState) item.uiState = state.queue[i].uiState;
        state.queue[i] = item;
        found = true;
        break;
      }
    }
    if (!found) state.queue.push(item);
    V.bus.emit('queue:update');
    V.bus.emit('file:update', item);
    // 文件在 AI 分析或落盘时的细进度，不能只看 task.progress（它按完成文件计数）
    if (state.task.current && state.task.current.id === item.id) {
      state.task.current = item;
      state.task.currentFile = item.name;
      state.task.stage = stageIndexOf(item.stage);
      var total = Math.max(1, state.task.total || state.queue.length);
      state.task.progress = Math.min(100, ((state.task.done || 0) + item.progress / 100) / total * 100);
      V.bus.emit('task:update');
    }
  }

  function applyTask(task, stats) {
    if (!task) return;
    var running = task.status === 'running';
    var started = task.started_at ? task.started_at * 1000 : 0;
    var finished = task.finished_at ? task.finished_at * 1000 : 0;

    state.task.status = task.status;
    state.task.message = task.message || '';
    state.task.total = task.total || 0;
    state.task.done = task.done || 0;
    state.task.startedAt = started;
    state.task.elapsed = started
      ? ((running || !finished ? Date.now() : finished) - started) / 1000
      : 0;

    var current = normItem(task.current);
    state.task.current = current;
    state.task.currentFile = current ? current.name : null;
    state.task.currentIndex = current
      ? Math.max(0, state.queue.findIndex(function (f) { return f.id === current.id; }))
      : 0;
    state.task.stage = current && current.stage
      ? stageIndexOf(current.stage)
      : (task.status === 'done' ? (V.STAGES || []).length : -1);
    // 后端 task.progress 按「完成文件数」算；当前文件的细进度靠 file 事件。
    // 快照和 task 事件里也带 current，所以刷新/断线重连不会丢进度。
    var total = Math.max(1, state.task.total || state.queue.length);
    state.task.progress = current && running
      ? Math.min(100, ((state.task.done || 0) + (current.progress || 0) / 100) / total * 100)
      : (task.progress || 0);

    if (stats) {
      state.stats = stats;
      V.bus.emit('stats:update');
    }
    V.bus.emit('task:update');
    V.bus.emit('status:update', task.status);
  }

  function applyTimelines(list) {
    state.timelines = (list || []).map(function (t) {
      return {
        name: t.name,
        original: t.original || '',
        model: t.model || '-',
        frames: t.frames || 0,
        chars: t.chars || 0,
        cost: t.cost || 0
      };
    });
    V.bus.emit('timeline:update');
  }

  function applyApprovals(list) {
    state.reviews = (list || []).map(normReview).filter(Boolean);
    state.approvals = state.reviews;
    V.bus.emit('approvals:update');
  }

  function applySnapshot(snap) {
    if (!snap) return;
    applyQueue(snap.queue);
    applyApprovals(snap.reviews);
    applyTimelines(snap.timelines);
    applyTask(snap.task, snap.stats);
    if (snap.services) applyServices(snap.services);
    V.bus.emit('state:update');
  }

  /* ---------------- 日志 ---------------- */

  var seenLogs = {};

  function appendLog(entry, silent) {
    if (!entry || !entry.message) return;
    var key = entry.id === undefined ? (entry.time + '|' + entry.message) : 'id:' + entry.id;
    if (seenLogs[key]) return;
    seenLogs[key] = 1;
    state.logs.push({
      id: entry.id,
      time: entry.time || V.formatTime(new Date()),
      level: entry.level || 'info',
      message: entry.message,
      meta: entry.meta || '',
      source: entry.source || 'backend'
    });
    while (state.logs.length > LOG_LIMIT) {
      var dropped = state.logs.shift();
      if (dropped && dropped.id !== undefined) delete seenLogs['id:' + dropped.id];
    }
    if (!silent) V.bus.emit('logs:update', entry);
  }

  function clearLogs() {
    state.logs = [];
    seenLogs = {};
    V.bus.emit('logs:clear');
  }

  /* ---------------- 服务与依赖 ---------------- */

  function applyServices(map) {
    if (!map) return;
    state.services = map;
    V.bus.emit('services:update', map);
  }

  function applyService(svc) {
    if (!svc || !svc.name) return;
    state.services[svc.name] = svc;
    V.bus.emit('services:update', state.services);
    V.bus.emit('service:update', svc);
  }

  function applyRuntime(report) {
    if (!report) return;
    state.runtime = report;
    V.bus.emit('runtime:update', report);
  }

  /* ---------------- SSE ---------------- */

  var source = null;
  var retryTimer = 0;

  var HANDLERS = {
    log: function (data) { appendLog(data); },
    'logs:clear': function () { clearLogs(); },
    state: applySnapshot,
    task: function (data) { applyTask(data.task || data, data.stats); },
    queue: function (data) { applyQueue(Array.isArray(data) ? data : data.queue); },
    file: mergeItem,
    stats: function (data) { state.stats = data; V.bus.emit('stats:update'); },
    review: function (data) {
      var item = normReview(data);
      if (!item) return;
      var replaced = false;
      for (var i = 0; i < state.reviews.length; i++) {
        if (state.reviews[i].id === item.id) { state.reviews[i] = item; replaced = true; break; }
      }
      if (!replaced) state.reviews.unshift(item);
      state.approvals = state.reviews;
      V.bus.emit('approvals:update');
    },
    service: applyService,
    services: applyServices,
    runtime: applyRuntime,
    progress: function (data) {
      state.progress = data;
      V.bus.emit('progress:update', data);
    }
  };

  function setMode(mode, error) {
    if (state.conn.mode === mode && state.conn.error === (error || '')) return;
    state.conn.mode = mode;
    state.conn.online = mode === 'live';
    state.conn.error = error || '';
    V.bus.emit('conn:change', state.conn);
  }

  function connect() {
    if (IS_FILE || typeof EventSource === 'undefined') return;
    if (source) return;

    try {
      source = new EventSource('/api/events');
    } catch (e) {
      setMode('offline', '浏览器不支持实时事件流');
      return;
    }

    source.onopen = function () {
      state.conn.retry = 0;
      if (state.conn.mode !== 'live') state.conn.since = Date.now();
      setMode('live');
    };

    source.onmessage = function (ev) {
      var payload;
      try { payload = JSON.parse(ev.data); } catch (e) { return; }
      var handler = HANDLERS[payload && payload.type];
      if (!handler) return;
      try {
        handler(payload.data);
      } catch (err) {
        console.error('[VAIR] SSE handler error', payload.type, err);
      }
    };

    source.onerror = function () {
      // readyState=2 表示连接已被彻底关闭（比如后端进程没了），EventSource 不会再自动重连
      if (source && source.readyState === 2) {
        source.close();
        source = null;
        scheduleReconnect();
      }
    };
  }

  function scheduleReconnect() {
    if (retryTimer) return;
    state.conn.retry += 1;
    setMode('offline', '与后端的实时连接已断开，正在重试…');
    // 退避：1s、2s、4s… 最多 15s
    var delay = Math.min(15000, 1000 * Math.pow(2, Math.min(4, state.conn.retry - 1)));
    retryTimer = window.setTimeout(function () {
      retryTimer = 0;
      connect();
    }, delay);
  }

  function disconnect() {
    if (retryTimer) { window.clearTimeout(retryTimer); retryTimer = 0; }
    if (source) { source.close(); source = null; }
  }

  /* ---------------- 刷新入口 ---------------- */

  function refreshState() {
    return request('GET', '/api/state', null, { timeout: 15000 })
      .then(function (snap) { applySnapshot(snap); return snap; });
  }

  function refreshConfig() {
    return request('GET', '/api/config').then(function (data) {
      state.config = data && data.config ? data.config : null;
      state.defaults = data && data.defaults ? data.defaults : null;
      V.bus.emit('config:update', state.config);
      return data;
    });
  }

  function refreshServices() {
    return request('GET', '/api/services').then(function (data) {
      applyServices(data);
      return data;
    });
  }

  function refreshRuntime() {
    return request('GET', '/api/runtime').then(function (report) {
      applyRuntime(report);
      return report;
    });
  }

  function refreshServer() {
    return request('GET', '/api/server').then(function (info) {
      state.server = info;
      V.bus.emit('server:update', info);
      return info;
    });
  }

  /* ---------------- 对外接口 ---------------- */

  V.api = {
    HttpError: HttpError,
    isFileProtocol: IS_FILE,
    isLive: function () { return state.conn.mode === 'live'; },
    get: function (path, opts) { return request('GET', path, null, opts); },
    post: function (path, body, opts) { return request('POST', path, body === undefined ? {} : body, opts); },
    put: function (path, body, opts) { return request('PUT', path, body, opts); },
    del: function (path, opts) { return request('DELETE', path, null, opts); },

    connect: connect,
    disconnect: disconnect,

    refreshState: refreshState,
    refreshConfig: refreshConfig,
    refreshServices: refreshServices,
    refreshRuntime: refreshRuntime,
    refreshServer: refreshServer,

    selectFiles: function (dir) {
      // 系统对话框会一直挂着直到用户操作，所以不设超时
      return request('POST', '/api/dialog/files', { dir: dir || '' }, { timeout: 0 });
    },
    selectFolder: function (dir) {
      return request('POST', '/api/dialog/folder', { dir: dir || '' }, { timeout: 0 });
    },
    cancelDialog: function () {
      return request('POST', '/api/dialog/cancel', {}, { timeout: 5000 });
    },

    /* 队列与任务 */
    addPaths: function (paths, recursive) {
      var body = { paths: paths };
      if (recursive !== undefined) body.recursive = recursive;
      return request('POST', '/api/items', body, { timeout: 120000 });
    },
    removeItem: function (id) { return request('DELETE', '/api/items/' + encodeURIComponent(id)); },
    clearItems: function () { return request('POST', '/api/items/clear', {}); },
    startTask: function () { return request('POST', '/api/tasks', {}); },
    stopTask: function () { return request('POST', '/api/tasks/stop', {}); },

    /* 审批 */
    applyReviews: applyApprovals,
    resolveReview: function (id, action, name) {
      var mapped = action === 'skipped' ? 'reject' : (action === 'approved' ? 'approve' : action);
      return request('POST', '/api/reviews/' + encodeURIComponent(id),
        { action: mapped, name: name }, { timeout: 60000 });
    },

    /* 服务 */
    serviceAction: function (name, action) {
      return request('POST', '/api/services/' + name + '/' + action, {}, { timeout: 60000 });
    },
    asrRuntime: function () { return request('GET', '/api/asr/runtime', null, { timeout: 60000 }); },
    installAsrRuntime: function () {
      return request('POST', '/api/asr/runtime/install', {}, { timeout: 30000 });
    },

    /* 依赖与模型 */
    checkAssets: function () { return request('GET', '/api/bootstrap/check', null, { timeout: 60000 }); },
    fetchAssets: function (ids) { return request('POST', '/api/bootstrap/fetch', { ids: ids || null }); },
    installDeps: function (packages) { return request('POST', '/api/bootstrap/install', { packages: packages || null }); },

    /* AI 测试 */
    listModels: function () { return request('GET', '/api/ai/models', null, { timeout: 30000 }); },
    testAI: function (cfg) { return request('POST', '/api/ai/test', { config: cfg || null }, { timeout: 120000 }); },
    testWhisper: function (mode) {
      // 本地大模型冷启动可能几十秒，这里给足时间
      return request('POST', '/api/whisper/test', { mode: mode || 'real' }, { timeout: 300000 });
    },

    /* 配置 */
    saveConfig: function (patch) {
      return request('PUT', '/api/config', { config: patch }, { timeout: 30000 })
        .then(function (result) {
          if (result && result.config) {
            state.config = result.config;
            V.bus.emit('config:update', state.config);
          }
          return result;
        });
    },
    resetConfig: function () {
      return request('POST', '/api/config/reset', {}).then(function (result) {
        if (result && result.config) {
          state.config = result.config;
          V.bus.emit('config:update', state.config);
        }
        return result;
      });
    },
    openConfigFolder: function () { return request('POST', '/api/config/open', {}); },

    /* 日志 */
    loadLogs: function (limit) {
      return request('GET', '/api/logs?limit=' + (limit || 300)).then(function (data) {
        clearLogs();
        (data.logs || []).forEach(function (entry) { appendLog(entry, true); });
        V.bus.emit('logs:update');
        return data.logs || [];
      });
    },
    clearLogsOnServer: function () {
      return request('POST', '/api/logs/clear', {}).then(function () { clearLogs(); });
    },

    errorText: function (err) {
      if (!err) return '未知错误';
      if (err.status === 0) return err.message + '（后端可能没启动，或端口不对）';
      if (err.status === 409) return err.message;
      if (err.status === 404) return err.message || '接口不存在，后端版本可能不匹配';
      return err.message || ('请求失败（HTTP ' + err.status + '）');
    }
  };

  V.bus.emit('api:ready');

  /* ---------------- 握手 ---------------- */

  V.onReady(function () {
    if (IS_FILE) {
      setMode('demo');
      appendLog({
        level: 'info',
        message: '原型演示模式 · 未连接后端，页面数据为模拟内容',
        meta: 'file://'
      });
      return;
    }

    setMode('connecting');
    connect();

    // 健康检查决定徽标显示什么版本；失败就直接标成离线，不用等 SSE 超时
    request('GET', '/api/health', null, { timeout: 8000 }).then(function (info) {
      state.conn.version = (info && info.version) || '';
      state.conn.root = (info && info.root) || '';
      if (state.conn.mode !== 'live') setMode('live');
      else V.bus.emit('conn:change', state.conn);
    }).catch(function (err) {
      setMode('offline', err.message);
    });

    refreshState().catch(function () { /* SSE 的 state 快照会兜底 */ });
    refreshServer().catch(function () {});
    // 审批开关、预览模式这类行为判断要用到配置，不能等用户点进设置页才拉
    refreshConfig().catch(function () {});
  });
})();
