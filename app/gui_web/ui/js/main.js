/**
 * 启动入口：把 api / state / shell / 三个视图串起来，并订阅后端事件。
 *
 * 渲染策略是“事件改 state → render()”，但做了两点防抖：
 *   1) 安装向导只在 step/phase/选盘/计划 这些关键量变化时整页重绘，
 *      否则每来一个进度事件都会把输入框（分区大小、确认文本）冲掉；
 *   2) 设置页只在数据重新加载后绘一次，因为页面上全是未保存的草稿。
 */

import { api, ApiError, openEventStream } from './api.js';
import {
  state, setDisks, setJobs, upsertJob, subscribe, notify,
} from './state.js';
import {
  initShell, setNav, renderChrome, pushLog, toast, setStatus, applyAppearance,
  showFatal, setLogSource, jobStatusText,
} from './shell.js';
import { renderMain } from './view-main.js';
import {
  renderInstall, goStep, next, back, resetAll, onStepEvent, refreshPhase,
  pollJobLogs, loadImages,
} from './view-install.js';
import { renderSettings, saveSettings, resetSettings, exportSettings, importSettings } from './view-settings.js';
import {
  openBootDialog, openCheckDialog, openAboutDialog, openKeysDialog,
} from './dialogs.js';

let installSig = '';
let settingsPainted = false;
let eventStream = null;
let pollTimer = null;

/* ------------------------------------------------------------------ 渲染 */
function render() {
  renderChrome(state.info);
  setNav(state.view);
  if (state.view === 'main') {
    renderMain();
    return;
  }
  if (state.view === 'install') {
    const wiz = state.wizard;
    const sig = [
      wiz.step,
      wiz.phase,
      wiz.ack,
      wiz.error,
      state.plans.length,
      wiz.targets.map((target) => `${target.diskNumber}:${target.sysGb}:${target.dataGb}:${target.imageIndex}`).join(','),
    ].join('|');
    const busy = ['running', 'done', 'failed'].includes(wiz.phase);
    if (busy || sig !== installSig) {
      installSig = sig;
      renderInstall();
    }
    return;
  }
  if (!settingsPainted && state.settings) {
    settingsPainted = true;
    renderSettings();
  }
}

function invalidateSettings() {
  settingsPainted = false;
}

/* ------------------------------------------------------------------ 导航 */
function goTo(view) {
  state.view = view;
  if (view === 'settings') invalidateSettings();
  if (view === 'install') {
    installSig = '';
    if (state.wizard.step === 3) void loadImages();
  }
  notify();
  if (view === 'main') setStatus('就绪');
}

window.__sispGoMain = () => goTo('main');

/* ------------------------------------------------------------------ 数据加载 */
async function loadAll() {
  try {
    state.info = await api.info();
  } catch (exc) {
    showFatal('读不到本地服务的信息接口。', exc instanceof ApiError ? `${exc.code}: ${exc.message}` : String(exc));
    return false;
  }

  try {
    const data = await api.settings();
    state.settings = data.settings || {};
    state.settingsMeta = data;
    applyAppearance(state.settings);
    for (const warning of data.warnings || []) pushLog(`设置提醒：${warning}`, 'warn', 'settings');
  } catch (exc) {
    state.settings = state.settings || {};
    pushLog(`读取设置失败：${exc instanceof ApiError ? exc.message : exc}`, 'err', 'settings');
  }

  await startSession();

  const startView = (state.settings && state.settings.startView) || 'main';
  state.view = ['main', 'install', 'settings'].includes(startView) ? startView : 'main';
  if (state.view === 'install') invalidateSettings();

  const autoRefresh = state.settings ? state.settings.autoRefresh !== false : true;
  if (autoRefresh) await refreshDisks(true);

  try {
    setJobs(await api.jobs());
  } catch (exc) {
    // 还没有任务时接口本来就是空列表；真出错了也只是少了历史任务
  }

  return true;
}

async function startSession() {
  try {
    state.session = await api.startSession({});
    const session = state.session;
    pushLog(`会话已启动：检查 ${(session.checks || []).length} 项，磁盘 ${(session.disks || []).length} 块`,
      session.blockers && session.blockers.length ? 'warn' : '', 'session');
    if (session.disks) applyDisks(session.disks);
  } catch (exc) {
    const message = exc instanceof ApiError ? exc.message : String(exc);
    pushLog(`会话启动失败：${message}`, 'err', 'session');
    toast('启动检查没通过，看下方日志', 'err');
  }
}

function applyDisks(disks) {
  setDisks(disks);
  setLogSource(`本次会话 · ${disks.length} 块盘`);
}

async function refreshDisks(quiet = false) {
  try {
    const data = await api.disks();
    applyDisks(Array.isArray(data) ? data : (data.disks || []));
    if (!quiet) {
      pushLog(`刷新磁盘：${state.disks.length} 块`, '', 'scan');
      toast(`已刷新：${state.disks.length} 块磁盘`);
    }
    notify();
  } catch (exc) {
    const message = exc instanceof ApiError ? exc.message : String(exc);
    pushLog(`刷新失败：${message}`, 'err', 'scan');
    if (!quiet) toast(`刷新失败：${message}`, 'err');
  }
}

/* ------------------------------------------------------------------ 事件 */
function handleFrame(frame) {
  if (!frame) return;
  if (frame.type === 'snapshot') {
    if (Array.isArray(frame.jobs)) setJobs(frame.jobs);
    for (const event of frame.events || []) handleEvent({ ...event, type: event.type });
    return;
  }
  if (frame.jobs) {
    for (const job of frame.jobs) upsertJob(job, false);
  }
  if (frame.event) handleEvent(frame.event);
  notify();
}

function handleEvent(event) {
  const type = event.type || event.event;
  const jobId = event.job_id;
  const payload = event.payload || {};

  if (type === 'job.step' && jobId) {
    onStepEvent({ job_id: jobId, ...payload });
    const job = state.jobs.get(jobId);
    if (job && payload.progress) job.progress = payload.progress;
    if (state.view === 'install' && state.wizard.jobIds.includes(jobId)) notify();
    return;
  }

  if (type === 'job.created' && jobId) {
    const disk = payload.disk_number !== undefined ? `磁盘 ${payload.disk_number}` : '';
    pushLog(`任务 ${jobId.slice(0, 6)} 已创建${disk ? ` · ${disk}` : ''}（${jobStatusText(payload.status || 'pending')}）`, '', 'job');
    return;
  }

  if (type === 'job.started' && jobId) {
    pushLog(`任务 ${jobId.slice(0, 6)} 开始执行`, '', 'job');
    if (state.wizard.jobIds.includes(jobId) && state.wizard.phase === 'running' && state.view !== 'install') {
      toast('安装任务已开始');
    }
    return;
  }

  if (type === 'job.finished' && jobId) {
    const status = payload.status || 'ok';
    const ok = status === 'ok';
    pushLog(`任务 ${jobId.slice(0, 6)} 结束：${jobStatusText(status)}${payload.message ? ` · ${payload.message}` : ''}`, ok ? '' : 'err', 'job');
    if (state.wizard.jobIds.includes(jobId)) {
      if (refreshPhase()) notify();
      toast(ok ? '安装完成' : `安装${jobStatusText(status)}`, ok ? '' : 'err');
    }
    return;
  }

  if (type === 'job.cancelled' && jobId) {
    pushLog(`任务 ${jobId.slice(0, 6)} 已取消：${payload.reason || ''}`, 'warn', 'job');
  }
}

/* ------------------------------------------------------------------ 轮询兜底 */
function startPolling() {
  pollTimer = setInterval(async () => {
    const busy = state.wizard.phase === 'running'
      || Array.from(state.jobs.values()).some((job) => job.status === 'running' || job.status === 'pending');
    if (!busy) return;
    try {
      setJobs(await api.jobs());
      refreshPhase();
      await pollJobLogs();
      notify();
    } catch (exc) {
      // 服务短暂不可用时下一轮再试
    }
  }, 1500);
}

/* ------------------------------------------------------------------ 动作 */
const ACTIONS = {
  install: () => goTo('install'),
  boot: () => openBootDialog(),
  check: () => openCheckDialog(),
  settings: () => goTo('settings'),
  about: () => openAboutDialog(),
  keys: () => openKeysDialog(),
  openlog: async () => {
    try {
      await api.openSettingsFolder();
      toast('已打开日志目录');
    } catch (exc) {
      toast(`打不开日志目录：${exc.message || exc}`, 'err');
    }
  },
};

function bindToolbar() {
  const navseg = document.querySelector('#navseg');
  if (navseg) navseg.setAttribute('data-bound', '1');

  const wzBack = document.querySelector('#wzBack');
  const wzNext = document.querySelector('#wzNext');
  const wzReset = document.querySelector('#wzReset');
  const wzExit = document.querySelector('#wzExit');
  const setSave = document.querySelector('#setSave');
  const setDefaults = document.querySelector('#setDefaults');
  const setExport = document.querySelector('#setExport');
  const setImport = document.querySelector('#setImport');

  if (wzBack) wzBack.addEventListener('click', () => back());
  if (wzNext) wzNext.addEventListener('click', () => next());
  if (wzReset) {
    wzReset.addEventListener('click', () => {
      resetAll();
      toast('安装向导已重置');
    });
  }
  if (wzExit) wzExit.addEventListener('click', () => goTo('main'));
  if (setSave) setSave.addEventListener('click', () => { void saveSettings(); });
  if (setDefaults) setDefaults.addEventListener('click', () => { void resetSettings(); });
  if (setExport) setExport.addEventListener('click', () => { void exportSettings(); });
  if (setImport) setImport.addEventListener('click', () => { void importSettings(); });
}

/* ------------------------------------------------------------------ 启动 */
async function boot() {
  bindToolbar();
  initShell({
    onNavigate: goTo,
    onRefresh: () => { void refreshDisks(false); },
    onAction: (action) => {
      const handler = ACTIONS[action];
      if (handler) handler();
    },
  });

  subscribe(render);

  const ready = await loadAll();
  if (!ready) return;

  render();
  setStatus('就绪');
  pushLog('界面已就绪', '', 'ui');

  eventStream = openEventStream(handleFrame, (status) => {
    if (status === 'open') setStatus('已连接本地服务');
    else if (status === 'reconnecting') setStatus('事件流断开，正在重连…');
  });
  startPolling();
}

window.addEventListener('error', (event) => {
  // 界面脚本出错时至少把原因写进日志坞，别只剩一个白屏
  pushLog(`界面脚本错误：${event.message}`, 'err', 'ui');
});

boot().catch((exc) => {
  showFatal('界面启动失败。', exc && exc.stack ? exc.stack : String(exc));
});
