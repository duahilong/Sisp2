/**
 * 与后端 REST 接口通信的唯一出口。
 *
 * 后端所有响应都是同一个信封：
 *   {ok: true, data: ...} / {ok: false, error: {code, message, detail}}
 * 这里把它拆成「成功返回 data / 失败抛 ApiError」，页面层只处理业务错误。
 *
 * 约定：本文件不碰 DOM，也不缓存数据（缓存在 state.js）。
 */

export class ApiError extends Error {
  constructor(code, message, detail = null, status = 0) {
    super(message || code || '请求失败');
    this.name = 'ApiError';
    this.code = code || 'E_HTTP';
    this.detail = detail;
    this.status = status;
  }
}

async function request(method, url, body) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch (exc) {
    throw new ApiError('E_NETWORK', '连不上本地服务（读不到接口）', String(exc));
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch (exc) {
    throw new ApiError('E_HTTP', `服务返回了非 JSON 响应（HTTP ${response.status}）`, String(exc), response.status);
  }
  if (!payload || payload.ok !== true) {
    const error = (payload && payload.error) || {};
    throw new ApiError(error.code, error.message, error.detail, response.status);
  }
  return payload.data;
}

const get = (url) => request('GET', url, undefined);
const post = (url, body) => request('POST', url, body === undefined ? {} : body);
const put = (url, body) => request('PUT', url, body === undefined ? {} : body);
const del = (url) => request('DELETE', url, undefined);

export const api = {
  // 基本信息
  info: () => get('/api/info'),
  health: () => get('/health'),

  // 会话与磁盘
  startSession: (options = {}) => post('/api/session', options),
  disks: () => get('/api/disks'),
  refreshDisks: () => get('/api/disks'),

  // 计划与任务
  prepare: (selection, dryRun) => post('/api/prepare', { selection, dry_run: dryRun }),
  submit: () => post('/api/submit'),
  confirm: (jobIds, confirmText) => post('/api/confirm', { job_ids: jobIds, confirm_text: confirmText }),
  cancel: (jobId) => post('/api/cancel', jobId ? { job_id: jobId } : {}),
  jobs: () => get('/api/jobs'),
  job: (jobId) => get(`/api/jobs/${encodeURIComponent(jobId)}`),
  jobLog: (jobId, tail = 200) => get(`/api/jobs/${encodeURIComponent(jobId)}/log?tail=${tail}`),

  // 分区脚本预览（只读：与执行器同源生成，不执行任何命令）
  partitionPreview: (diskNumber) => post('/api/partition/preview', { disk_number: diskNumber }),

  // 镜像
  images: (payload = {}) => post('/api/images', payload),

  // 引导
  bootReport: () => get('/api/boot/report'),
  bootRepair: (payload) => post('/api/boot/repair', payload),

  // 设置
  settings: () => get('/api/settings'),
  saveSettings: (patch) => put('/api/settings', patch),
  resetSettings: () => del('/api/settings'),
  exportSettings: (path) => post('/api/settings/export', { path }),
  importSettings: (payload) => post('/api/settings/import', payload),
  probePath: (key, path) => post('/api/settings/probe', path === undefined ? { key } : { key, path }),
  probeAllPaths: () => post('/api/settings/probe_all'),
  openSettingsFolder: () => post('/api/settings/open_folder'),
  pickPath: (key, kind) => post('/api/dialog/pick', { key, kind }),
};

/** SSE 事件流；返回一个带 close() 的句柄，浏览器会自动重连。 */
export function openEventStream(onFrame, onState) {
  let source = null;
  let closed = false;

  const connect = () => {
    if (closed) return;
    source = new EventSource('/api/events');
    source.onopen = () => onState && onState('open');
    source.onerror = () => onState && onState('reconnecting');
    source.onmessage = (event) => {
      let payload = null;
      try {
        payload = JSON.parse(event.data);
      } catch (exc) {
        return;
      }
      onFrame(payload);
    };
  };

  connect();
  return {
    close() {
      closed = true;
      if (source) source.close();
      source = null;
    },
  };
}
