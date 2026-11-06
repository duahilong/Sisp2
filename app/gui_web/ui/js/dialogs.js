/**
 * 两个“诊断类”弹窗：修复引导、磁盘检查。
 *
 * 它们都不参与安装向导的主链路，但都是真的：
 *   - 修复引导 → GET /api/boot/report + POST /api/boot/repair
 *   - 磁盘检查 → 本次会话的 checks（来自 /api/session）
 */

import { api, ApiError } from './api.js';
import { $, $$, esc } from './dom.js';
import { fmtSize, isSelectable, protectionText } from './derive.js';
import { state, selectedDisk } from './state.js';
import { openModal, closeModal, toast, confirmDialog, pushLog, setStatus } from './shell.js';

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
      <div class="set-d" style="margin-top:10px">本程序直接调用 Windows 自带的 diskpart / bcdboot，以及你指定的 Ghost。真实写盘前请确认已备份数据，并以管理员身份运行。</div>`,
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
