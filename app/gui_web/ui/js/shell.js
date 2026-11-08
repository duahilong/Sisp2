/**
 * 外壳：标题栏 chip、菜单栏、工具栏、日志坞、状态栏、弹窗、toast、快捷键。
 *
 * 视图模块只调用这里的函数，不直接摸这些区域的 DOM，
 * 这样“日志该出现在哪、状态栏显示什么”只有一处定义。
 */

import { $, $$, esc, stamp } from './dom.js';
import { state, jobsArray, JOB_STATUS_TEXT } from './state.js';

const MAX_LOG_LINES = 400;
let onChange = null;

/* ------------------------------------------------------------------ 初始化 */
export function initShell(handlers = {}) {
  onChange = handlers;

  // 菜单栏：点菜单名开合；点菜单项交给外部处理
  $$('#menubar .menu').forEach((menu) => {
    if (!menu.querySelector('.dropdown')) return;
    menu.addEventListener('click', (event) => {
      if (event.target.closest('.dd-item')) return;
      const wasOpen = menu.classList.contains('open');
      closeMenus();
      if (!wasOpen) menu.classList.add('open');
      event.stopPropagation();
    });
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.menu')) closeMenus();
  });
  $$('#menubar .dd-item').forEach((item) => {
    item.addEventListener('click', () => {
      closeMenus();
      closeModal();
      if (onChange && onChange.onAction) onChange.onAction(item.dataset.act);
    });
  });

  // 视图切换
  $('#navseg').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-nav]');
    if (button && onChange && onChange.onNavigate) onChange.onNavigate(button.dataset.nav);
  });

  // 刷新磁盘（F5 同效）
  $('#tbRefresh').addEventListener('click', () => {
    if (onChange && onChange.onRefresh) onChange.onRefresh();
  });

  // 日志坞
  $('#logClear').addEventListener('click', () => {
    const box = $('#logBody');
    if (box) box.innerHTML = '';
    setStatus('日志已清空');
  });
  $('#logFold').addEventListener('click', () => {
    const dock = $('#logDock');
    if (!dock) return;
    const collapsed = dock.classList.toggle('collapsed');
    $('#logFold').textContent = collapsed ? '展开' : '收起';
  });

  // 快捷键
  document.addEventListener('keydown', (event) => {
    const tag = (event.target && event.target.tagName) || '';
    const typing = tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA';
    if ((event.ctrlKey || event.metaKey) && event.key === ',') {
      event.preventDefault();
      if (onChange && onChange.onNavigate) onChange.onNavigate('settings');
      return;
    }
    if (typing) {
      if (event.key === 'Escape') event.target.blur();
      return;
    }
    if (event.key === 'F5') {
      event.preventDefault();
      if (onChange && onChange.onRefresh) onChange.onRefresh();
      return;
    }
    if (event.key === 'Escape') {
      if ($('#overlay').classList.contains('on')) {
        closeModal();
      } else {
        closeMenus();
        const { view } = state;
        if (view === 'install' && state.wizard.phase === 'form') {
          if (onChange && onChange.onNavigate) onChange.onNavigate('main');
        }
      }
    }
  });
}

function closeMenus() {
  $$('#menubar .menu').forEach((menu) => menu.classList.remove('open'));
}

/* ------------------------------------------------------------------ 视图/状态栏 */
export function setNav(view) {
  $$('#navseg button').forEach((button) => {
    button.classList.toggle('on', button.dataset.nav === view);
  });
  const installButton = $('#navseg [data-nav="install"]');
  if (installButton) {
    const pending = state.wizard.targets.length > 0 && state.wizard.phase === 'form' && view !== 'install';
    installButton.innerHTML = `系统安装${pending ? '<span class="dot"></span>' : ''}`;
    installButton.title = pending
      ? `上次填到第 ${state.wizard.step} 步，点一下接着填`
      : '选硬盘、分好区、装系统，四步完成';
  }
  const app = $('#app');
  const workspace = $('#workspace');
  if (app) app.classList.toggle('in-wizard', view === 'install');
  if (workspace) {
    workspace.classList.toggle('wizard-mode', view === 'install');
    workspace.classList.toggle('view-settings', view === 'settings');
  }
  $('#tbRefresh').disabled = view !== 'main';
  $('#tbHint').textContent = view === 'install'
    ? '安装向导写盘前会先过一轮预检，任何一项不通过都会拒绝写盘'
    : '只读查看 · 改动磁盘请用「系统安装」';
}

export function setStatus(text) {
  const node = $('#sbLeft');
  if (node) node.textContent = text == null ? '' : String(text);
}

export function setMidStatus(disk, part) {
  const node = $('#sbMid');
  if (!node) return;
  const active = jobsArray().filter((job) => job.status === 'running');
  const pieces = [];
  if (disk) {
    pieces.push(`<span class="mono">磁盘 ${esc(disk.disk_number)}</span>`);
    pieces.push('<span class="sb-sep"></span>');
    pieces.push(part
      ? `<span class="mono">${esc(part.drive_letter || '—')}</span> <span>${esc(part.label || '')}</span>`
      : '<span>未选中分区</span>');
  } else {
    pieces.push('<span>未检测到磁盘</span>');
  }
  if (active.length) {
    pieces.push('<span class="sb-sep"></span>');
    pieces.push(`<span class="mono">${active.length} 个任务进行中</span>`);
  }
  node.innerHTML = pieces.join('');
}

/* ------------------------------------------------------------------ 标题栏 chip */
export function renderChrome(info) {
  const version = $('#appVersion');
  if (version) {
    version.textContent = info && info.version ? `v${info.version}` : '—';
  }
  const chipMode = $('#chipMode');
  if (chipMode) {
    chipMode.classList.remove('dry');
    chipMode.classList.add('alert');
    chipMode.title = '真实模式：确认后会把选中的磁盘重新分区';
  }
  $('#chipModeText').textContent = '真实写盘';

  const partitionCount = state.disks.reduce((sum, disk) => sum + ((disk.partitions || []).length), 0);
  $('#chipDisksText').textContent = `${state.disks.length} 块磁盘`;
  $('#chipPartsText').textContent = `${partitionCount} 个分区`;
}

/* ------------------------------------------------------------------ 日志坞 */
export function pushLog(message, kind = '', source = '') {
  const box = $('#logBody');
  const text = String(message == null ? '' : message);
  if (box) {
    const line = document.createElement('div');
    if (kind) line.className = kind;
    const src = source ? `<span class="src">[${esc(source)}]</span>` : '';
    line.innerHTML = `<span class="t">${stamp()}</span>${src}<span>${esc(text)}</span>`;
    box.appendChild(line);
    while (box.children.length > MAX_LOG_LINES) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }
  setStatus(text);
}

export function setLogSource(text) {
  const node = $('#logSrc');
  if (node) node.textContent = text;
}

/** 把某个任务的日志行搬进日志坞（`GET /api/jobs/{id}/log` 的结果）。 */
/* ------------------------------------------------------------------ toast */
export function toast(message, kind = '') {
  const box = $('#toasts');
  if (!box) return;
  const node = document.createElement('div');
  node.className = `toast${kind ? ` ${kind}` : ''}`;
  node.textContent = String(message);
  box.appendChild(node);
  setTimeout(() => {
    node.style.opacity = '0';
    node.style.transition = 'opacity .25s';
  }, 2200);
  setTimeout(() => node.remove(), 2600);
}

/* ------------------------------------------------------------------ 弹窗 */
export function closeModal() {
  const overlay = $('#overlay');
  if (!overlay) return;
  overlay.classList.remove('on');
  overlay.innerHTML = '';
}

/**
 * 打开一个模态框。
 * @param {{title: string, body: string, footer?: string, width?: number, className?: string}} options
 * @returns {{root: HTMLElement, close: () => void, body: HTMLElement}}
 */
export function openModal({ title, body, footer = '', className = '', width = 0 }) {
  const overlay = $('#overlay');
  const style = width ? ` style="width:${width}px"` : '';
  overlay.innerHTML = `<div class="modal ${className}"${style}>
    <div class="mh">${esc(title)}<button class="x" data-close>&times;</button></div>
    <div class="mb">${body}</div>
    ${footer ? `<div class="mf">${footer}</div>` : ''}
  </div>`;
  overlay.classList.add('on');
  $$('#overlay [data-close]', overlay).forEach((button) => {
    button.addEventListener('click', closeModal);
  });
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) closeModal();
  });
  return {
    root: overlay.firstElementChild,
    body: overlay.querySelector('.mb'),
    close: closeModal,
  };
}

/** 通用确认框；返回 true/false 的 Promise。 */
export function confirmDialog({ title, body, okText = '确定', danger = true }) {
  return new Promise((resolve) => {
    const modal = openModal({
      title,
      body,
      footer: `<button class="tbtn" data-close>取消</button>
        <button class="tbtn ${danger ? 'danger' : 'primary'}" id="cdOk">${esc(okText)}</button>`,
    });
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      modal.close();
      resolve(value);
    };
    $('#cdOk', modal.root).addEventListener('click', () => finish(true));
    $$('[data-close]', modal.root).forEach((button) => {
      button.addEventListener('click', () => finish(false));
    });
  });
}

/* ------------------------------------------------------------------ 主题/密度 */
export function applyAppearance(settings) {
  const root = document.documentElement;
  const theme = (settings && settings.theme) || 'light';
  const resolved = theme === 'system'
    ? (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : theme;
  root.dataset.theme = resolved === 'dark' ? 'dark' : 'light';
  root.dataset.density = (settings && settings.density) === 'compact' ? 'compact' : 'comfortable';
}

/* ------------------------------------------------------------------ 致命错误 */
export function showFatal(message, detail = '') {
  const node = $('#fatal');
  if (!node) return;
  node.hidden = false;
  node.innerHTML = `<div class="fbox">
    <h2>界面没能加载出来</h2>
    <p>${esc(message)}</p>
    ${detail ? `<pre>${esc(detail)}</pre>` : ''}
    <p style="margin-top:12px;color:var(--muted)">可以关掉窗口重新打开；如果一直出现，请查看日志目录里的 app.log。</p>
  </div>`;
}

export function jobStatusText(status) {
  return JOB_STATUS_TEXT[status] || status || '未知';
}
