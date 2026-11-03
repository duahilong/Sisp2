/**
 * 三个“诊断类”弹窗：硬盘分区、修复引导、磁盘检查。
 *
 * 它们都不参与安装向导的主链路，但都是真的：
 *   - 硬盘分区 → POST /api/partition/plan|execute（plan 永远是演练）
 *   - 修复引导 → GET /api/boot/report + POST /api/boot/repair
 *   - 磁盘检查 → 本次会话的 checks（来自 /api/session）
 * 危险动作仍然要过设置里的 `confirmDelete`，确认文本由后端校验。
 */

import { api, ApiError } from './api.js';
import { $, $$, esc } from './dom.js';
import { fmtSize, fsColor, fsName, fsClass, partLabel, isSelectable, protectionText, suggestLetter } from './derive.js';
import { state, selectedDisk } from './state.js';
import { openModal, closeModal, toast, confirmDialog, pushLog, setStatus } from './shell.js';

const KINDS = [
  ['create', '新建分区'],
  ['format', '格式化'],
  ['delete', '删除分区'],
  ['assign', '分配盘符'],
];

/** 引导检查返回的是机器码（boot_ok / no_esp …），界面上换成短标签。 */
const BOOT_CODES = {
  boot_ok: '正常',
  boot_missing: '缺引导',
  no_esp: '无 ESP',
  no_windows: '无 Windows',
  orphan_esp: 'ESP 没系统',
  no_disk: '没扫到硬盘',
};

let actions = [];

/* ------------------------------------------------------------------ 硬盘分区 */
export function openPartitionDialog() {
  const disk = selectedDisk();
  if (!disk) {
    toast('先在主页面选一块磁盘', 'err');
    return;
  }
  actions = [];
  const modal = openModal({
    title: `硬盘分区 · 磁盘 ${disk.disk_number}`,
    className: 'wide',
    body: partitionBodyHtml(disk),
    footer: `<button class="tbtn" data-close>关闭</button>
      <button class="tbtn" id="planBtn">生成计划</button>
      <button class="tbtn danger" id="execBtn" disabled>执行</button>`,
  });

  $('#planBtn', modal.root).addEventListener('click', () => { void planPartition(disk); });
  $('#addAct', modal.root).addEventListener('click', () => {
    actions.push({ kind: 'create', size_gb: '', file_system: 'NTFS', letter: '', label: '', partition_number: 1 });
    renderActions(disk);
  });
  renderActions(disk);
}

function partitionBodyHtml(disk) {
  const parts = (disk.partitions || []).map((part) => `<tr>
      <td class="num">${esc(part.partition_number)}</td>
      <td>${esc(part.drive_letter || '—')}</td>
      <td>${esc(partLabel(part))}</td>
      <td>${esc(fsName(fsClass(part)))}</td>
      <td class="num">${esc(fmtSize(part.size_bytes))}</td>
    </tr>`).join('');
  const locked = !isSelectable(disk);
  return `<div class="opsum">
      <table>
        <thead><tr><th style="width:56px">分区</th><th style="width:56px">盘符</th><th>卷标</th><th style="width:76px">文件系统</th><th style="width:88px;text-align:right">容量</th></tr></thead>
        <tbody>${parts || '<tr><td colspan="5" class="dim">这块盘上没有分区</td></tr>'}</tbody>
      </table>
    </div>
    ${locked ? `<div class="warnbox"><span class="wi">!</span><div>${esc(protectionText(disk))}，不能在这里改动。</div></div>` : ''}
    <div class="set-d">按顺序执行：新建会先建分区再格式化并分配盘符；删除分区不可撤销。</div>
    <div class="actrows" id="actRows"></div>
    <button class="tbtn actrow add" id="addAct" ${locked ? 'disabled' : ''} style="width:100%">+ 添加一条操作</button>
    <div id="planOut"></div>`;
}

function renderActions(disk) {
  const box = $('#actRows');
  if (!box) return;
  box.innerHTML = actions.map((action, index) => {
    const isCreate = action.kind === 'create';
    const numberOptions = (disk.partitions || []).map((part) => `<option value="${esc(part.partition_number)}"${Number(action.partition_number) === Number(part.partition_number) ? ' selected' : ''}>分区 ${esc(part.partition_number)}${part.drive_letter ? ` (${esc(part.drive_letter)}:)` : ''}</option>`).join('');
    const fsOptions = ['NTFS', 'exFAT', 'FAT32'].map((fs) => `<option value="${fs}"${action.file_system === fs ? ' selected' : ''}>${fs}</option>`).join('');
    return `<div class="actrow" data-index="${index}">
      <select data-field="kind">${KINDS.map(([value, text]) => `<option value="${value}"${action.kind === value ? ' selected' : ''}>${text}</option>`).join('')}</select>
      ${isCreate
        ? `<input type="number" data-field="size_gb" min="1" placeholder="大小 GB" value="${esc(action.size_gb)}">`
        : `<select data-field="partition_number">${numberOptions || '<option value="1">分区 1</option>'}</select>`}
      ${isCreate || action.kind === 'format'
        ? `<select data-field="file_system">${fsOptions}</select>`
        : '<input data-field="letter" maxlength="1" placeholder="盘符" value="" disabled>'}
      ${isCreate
        ? `<input data-field="label" placeholder="卷标（可选）" value="${esc(action.label || '')}">`
        : `<input data-field="letter" maxlength="1" placeholder="盘符" value="${esc(action.letter || '')}">`}
      <button class="ax" data-rm="${index}" title="删除这条">&times;</button>
    </div>`;
  }).join('');

  $$('#actRows .actrow').forEach((row) => {
    const index = Number(row.dataset.index);
    $$('select, input', row).forEach((node) => {
      if (node.disabled) return;
      node.addEventListener('change', () => {
        const field = node.dataset.field;
        const value = node.value;
        actions[index][field] = field === 'partition_number' || field === 'size_gb' ? value : value;
        if (field === 'kind') {
          if (value === 'delete' || value === 'assign') actions[index].size_gb = '';
          renderActions(disk);
        }
      });
    });
    const remove = row.querySelector('[data-rm]');
    if (remove) {
      remove.addEventListener('click', () => {
        actions.splice(index, 1);
        renderActions(disk);
      });
    }
  });
}

function actionsPayload() {
  return actions.map((action) => {
    const item = { kind: action.kind, file_system: action.file_system || 'NTFS' };
    if (action.kind === 'create' || action.kind === 'format') {
      if (action.file_system) item.file_system = action.file_system;
    }
    if (action.size_gb !== '' && action.size_gb !== undefined) item.size_gb = Number(action.size_gb);
    if (action.partition_number && action.kind !== 'create') item.partition_number = Number(action.partition_number);
    if (action.letter) item.letter = String(action.letter).toUpperCase();
    if (action.label) item.label = String(action.label);
    return item;
  });
}

async function planPartition(disk) {
  const out = $('#planOut');
  if (!out) return;
  if (!actions.length) {
    out.innerHTML = '<div class="warnbox" style="margin-top:10px"><span class="wi">!</span><div>先添加一条操作。</div></div>';
    return;
  }
  out.innerHTML = '<div class="hint" style="margin-top:10px">正在校验…</div>';
  try {
    const result = await api.partitionPlan(disk.disk_number, actionsPayload());
    // 被保护的盘（系统盘 / 排除名单 / 离线 / 只读）只允许看演练脚本：真实执行一定
    // 过不了写盘闸门，不如在界面上先关掉按钮，避免白点一次。
    $('#execBtn').disabled = !isSelectable(disk);
    out.innerHTML = `<div class="codebox">${esc((result.script || []).join('\n'))}</div>
      <div class="hint" style="margin-top:6px">${esc(result.message || '演练：只生成脚本，没有执行')}</div>`;
  } catch (exc) {
    $('#execBtn').disabled = true;
    out.innerHTML = `<div class="warnbox" style="margin-top:10px"><span class="wi">!</span>
      <div>${esc(exc instanceof ApiError ? exc.message : String(exc))}</div></div>`;
  }
}

/* ------------------------------------------------------------------ 修复引导 */
export function openBootDialog() {
  const modal = openModal({
    title: '修复引导',
    className: 'wide',
    body: '<div class="empty-hint">正在扫描各硬盘的 Windows 目录与 EFI 分区…</div>',
    footer: '<button class="tbtn" id="rescanBtn">重新扫描</button><button class="tbtn" data-close>关闭</button>',
  });
  const rescan = () => { void loadBootReport(modal); };
  $('#rescanBtn', modal.root).addEventListener('click', rescan);
  void loadBootReport(modal);
}

async function loadBootReport(modal) {
  try {
    const report = await api.bootReport();
    state.boot = report;
    modal.body.innerHTML = bootBodyHtml(report);
    $$('[data-repair]', modal.root).forEach((button) => {
      button.addEventListener('click', () => { void repairBoot(Number(button.dataset.repair), modal); });
    });
  } catch (exc) {
    modal.body.innerHTML = `<div class="warnbox"><span class="wi">!</span>
      <div>${esc(exc instanceof ApiError ? exc.message : String(exc))}</div></div>`;
  }
}

function bootBodyHtml(report) {
  const findings = (report.findings || []).map((item) => `<div class="finding ${item.level === 'error' ? 'bad' : (item.level === 'warn' ? 'warn' : 'ok')}">
      <span class="fk">${esc(BOOT_CODES[item.code] || item.code || item.level)}</span>
      <span class="fb"><span class="ft">${esc(item.title || '')}</span><br>${esc(item.detail || '')}</span>
    </div>`).join('') || '<div class="finding ok"><span class="fk">ok</span><span class="fb">引导看起来正常</span></div>';

  const targets = (report.targets || []).map((target) => {
    const disk = (state.disks || []).find((item) => Number(item.disk_number) === Number(target.disk_number));
    // 被保护的盘（系统盘 / 排除名单 / 离线 / 只读）不给修复按钮：后端闸门会拒，
    // 这里先拦下来，省掉一次“点了才报错”。
    const locked = disk ? !isSelectable(disk) : false;
    let action = `<span class="dim">${esc(target.reason || '不可修复')}</span>`;
    if (locked) {
      action = `<span class="dim">受保护：${esc(protectionText(disk))}</span>`;
    } else if (target.repairable) {
      action = `<button class="tbtn" data-repair="${esc(target.disk_number)}">${target.boot_ok ? '重写引导' : '修复引导'}</button>`;
    }
    return `<tr>
      <td class="num">${esc(target.disk_number)}</td>
      <td>${esc(target.model || '')}</td>
      <td>${esc(target.windows_letter ? `${target.windows_letter}:` : '—')}</td>
      <td>${esc(target.esp_partition === null || target.esp_partition === undefined ? '—' : `分区 ${target.esp_partition}`)}</td>
      <td>${target.boot_ok ? '<span class="pstate ok">已就绪</span>' : '<span class="pstate miss">缺引导</span>'}</td>
      <td>${action}</td>
    </tr>`;
  }).join('');

  return `<div class="findings">${findings}</div>
    <div class="opsum" style="margin-top:12px">
      <table>
        <thead><tr><th style="width:56px">磁盘</th><th>型号</th><th style="width:56px">Windows</th><th style="width:84px">ESP</th><th style="width:76px">状态</th><th style="width:96px"></th></tr></thead>
        <tbody>${targets || '<tr><td colspan="6" class="dim">没有可用于修复引导的硬盘</td></tr>'}</tbody>
      </table>
    </div>
    <div class="set-d" style="margin-top:8px">修复会把临时盘符挂到 ESP 上执行 <span class="set-mono">bcdboot X:\\Windows /s Y: /f UEFI</span>，结束时无论成败都会释放临时盘符。</div>`;
}

async function repairBoot(diskNumber, modal) {
  if (!(state.info && state.info.dry_run)) {
    const ok = await confirmDialog({
      title: `修复磁盘 ${diskNumber} 的引导`,
      body: `<p>会在该盘的 ESP 分区上重写 UEFI 引导文件，让这块盘自己能启动。</p>
        <p style="margin-top:6px;color:var(--muted)">不改分区，也不动 Windows 本身。</p>`,
      okText: '修复引导',
    });
    if (!ok) return;
  }
  setStatus(`正在修复磁盘 ${diskNumber} 的引导…`);
  try {
    const result = await api.bootRepair({ disk_number: diskNumber });
    const steps = (result.steps || []).map((step) => `<tr>
        <td>${esc(step.title || step.key || '')}</td>
        <td class="num">${esc(step.status || '')}</td>
        <td class="dim">${esc((step.message || '').slice(0, 120))}</td>
      </tr>`).join('');
    modal.body.innerHTML = `<h3 class="wz-h">修复结果 · 磁盘 ${diskNumber}</h3>
      <div class="opsum"><table><thead><tr><th>步骤</th><th style="width:70px">状态</th><th>说明</th></tr></thead>
        <tbody>${steps || '<tr><td colspan="3" class="dim">没有步骤</td></tr>'}</tbody></table></div>
      ${result.log_path ? `<div class="set-mono" style="margin-top:8px">${esc(result.log_path)}</div>` : ''}
      ${result.dry_run ? '<div class="warnbox" style="margin-top:10px"><span class="wi">!</span><div>演练模式：只生成脚本，没有真的写引导。</div></div>' : ''}
      ${result.script ? `<div class="codebox" style="margin-top:10px">${esc(result.script.join('\n'))}</div>` : ''}`;
    pushLog(`修复引导 磁盘 ${diskNumber}：${result.exit === 0 ? '成功' : `失败（退出码 ${result.exit}）`}`, result.exit === 0 ? '' : 'err', 'boot');
    toast(result.exit === 0 ? '引导修复完成' : '引导修复失败，看日志', result.exit === 0 ? '' : 'err');
  } catch (exc) {
    modal.body.innerHTML = `<div class="warnbox"><span class="wi">!</span>
      <div>${esc(exc instanceof ApiError ? exc.message : String(exc))}</div></div>`;
    toast('引导修复失败', 'err');
  }
}

/* ------------------------------------------------------------------ 磁盘检查 */
export function openCheckDialog() {
  const session = state.session;
  const disk = selectedDisk();
  const checks = (session && session.checks) || [];
  const rows = checks.map((check) => `<div class="finding ${check.passed ? 'ok' : 'bad'}">
      <span class="fk">${check.passed ? 'pass' : 'fail'}</span>
      <span class="fb"><span class="ft">${esc(check.label || check.name || '')}</span><br>${esc(check.message || '')}</span>
    </div>`).join('') || '<div class="empty-hint">本次会话还没有检查结果</div>';

  const diskHtml = disk ? `<div class="opsum" style="margin-top:12px"><table>
      <tr><td class="k">磁盘</td><td>磁盘 ${esc(disk.disk_number)} · ${esc(disk.model || '')} · ${esc(disk.partition_style || '')}</td></tr>
      <tr><td class="k">容量</td><td>${esc(fmtSize(disk.size_bytes))}</td></tr>
      <tr><td class="k">分区</td><td>${(disk.partitions || []).length} 个</td></tr>
      <tr><td class="k">可写</td><td>${isSelectable(disk) ? '是' : `否 · ${esc(protectionText(disk))}`}</td></tr>
    </table></div>` : '';

  openModal({
    title: '磁盘检查',
    className: 'wide',
    body: `<div class="findings">${rows}</div>${diskHtml}
      <div class="set-d" style="margin-top:10px">这里是启动会话时跑的那套前置检查（工具、设置路径、权限、配置）。</div>`,
    footer: `<button class="tbtn" data-close>关闭</button>
      <button class="tbtn primary" id="recheckBtn">重新检查</button>`,
  });

  const recheck = $('#recheckBtn');
  if (recheck) {
    recheck.addEventListener('click', async () => {
      try {
        const sessionData = await api.startSession({});
        state.session = sessionData;
        closeModal();
        openCheckDialog();
        toast('检查完成');
      } catch (exc) {
        toast(`重新检查失败：${exc.message || exc}`, 'err');
      }
    });
  }
}

/* ------------------------------------------------------------------ 关于 / 快捷键 */
export function openAboutDialog() {
  const info = state.info || {};
  openModal({
    title: '关于硬盘工具箱',
    body: `<div class="opsum"><table>
        <tr><td class="k">版本</td><td>${esc(info.version || '—')}</td></tr>
        <tr><td class="k">Python</td><td>${esc(info.python || '—')}</td></tr>
        <tr><td class="k">模式</td><td>${info.dry_run ? '演练模式' : '真实写盘'}</td></tr>
        <tr><td class="k">配置文件</td><td class="set-mono">${esc((state.session && state.session.config_path) || '—')}</td></tr>
        <tr><td class="k">日志目录</td><td class="set-mono">${esc((state.settings && state.settings.logDir) || '用户目录下的 Sisp 日志目录')}</td></tr>
      </table></div>
      <div class="set-d" style="margin-top:10px">本程序直接调用 Windows 自带的 diskpart / dism / bcdboot。真实写盘前请确认已备份数据，并以管理员身份运行。</div>`,
    footer: '<button class="tbtn" data-close>确定</button>',
  });
}

export function openKeysDialog() {
  const rows = [
    ['F5', '重新扫描全部硬盘'],
    ['Ctrl + ,', '打开设置'],
    ['Esc', '关闭弹窗 / 菜单；安装向导里回到主页面'],
    ['← →', '在分区大小分隔条上微调 1 GB'],
  ].map(([key, text]) => `<tr><td><span class="code">${esc(key)}</span></td><td>${esc(text)}</td></tr>`).join('');
  openModal({
    title: '快捷键',
    body: `<div class="opsum"><table>${rows}</table></div>`,
    footer: '<button class="tbtn" data-close>确定</button>',
  });
}

/** 给“分配盘符”输入框一个建议值（后端 validate 会再检查）。 */
export function suggestAssignLetter() {
  return suggestLetter(state.disks);
}
