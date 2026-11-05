/**
 * 设置页：小标题 + 卡片 + 行（左说明、右控件）。
 *
 * 后端 `GET /api/settings` 只给 `{settings, warnings, sections, pathKeys}`
 * ——**没有**每项的控件类型和中文说明，所以“标签/说明/控件类型”这张表在
 * 前端维护（就是下面的 `FIELDS`）。为此 `tests/test_ui_assets.py` 会拿后端
 * `settings.DEFAULTS` 的键逐个核对，后端新增设置而前端漏了会直接测试失败。
 *
 * 改动先写进 state.draft，点“保存设置”才 `PUT /api/settings`；
 * 保存后按三类生效时机提示：立即 / 下次任务 / 下次启动（见 Docs/配置说明.md）。
 */

import { api, ApiError } from './api.js';
import { $, $$, esc } from './dom.js';
import { state, draftValue, setDraft, dirtyCount, clearDraft } from './state.js';
import { toast, applyAppearance, pushLog } from './shell.js';

const SECTIONS = [
  ['general', '常规'],
  ['install', '安装默认值'],
  ['external', '外部程序'],
  ['system_files', '系统文件'],
  ['safety', '安全与提醒'],
  ['logging', '日志与诊断'],
];

const OPTIONS = {
  language: [['zh-CN', '简体中文'], ['en-US', 'English']],
  theme: [['light', '浅色'], ['dark', '深色'], ['system', '跟随系统']],
  density: [['comfortable', '舒适'], ['compact', '紧凑']],
  startView: [['main', '主页面'], ['install', '系统安装'], ['settings', '设置']],
  defaultFs: [['NTFS', 'NTFS'], ['exFAT', 'exFAT']],
  logLevel: [['debug', 'debug'], ['info', 'info'], ['warn', 'warn'], ['error', 'error']],
};

/* kind: bool | enum | int | auto-int | path | text */
const FIELDS = [
  { section: 'general', key: 'language', kind: 'enum', effect: '重启后生效', hint: '目前只切换语言标记，界面文案仍是中文。' },
  { section: 'general', key: 'theme', kind: 'enum', effect: '立即生效' },
  { section: 'general', key: 'density', kind: 'enum', effect: '立即生效' },
  { section: 'general', key: 'rememberWindow', kind: 'bool', effect: '下次启动生效', hint: '关掉后窗口每次都从默认大小开始。' },
  { section: 'general', key: 'autoRefresh', kind: 'bool', effect: '下次启动生效', hint: '打开窗口时自动扫描一次硬盘。' },
  { section: 'general', key: 'startView', kind: 'enum', effect: '下次启动生效' },

  { section: 'install', key: 'confirmBeforeStart', kind: 'bool', effect: '立即生效', hint: '真实写盘前再弹一次确认框。' },
  { section: 'install', key: 'typeToConfirm', kind: 'bool', effect: '立即生效', hint: '输入盘号或型号才允许开始，后端同样校验。' },
  { section: 'install', key: 'sysDefaultGb', kind: 'int', effect: '下次生成计划生效', hint: '向导第 2 步的初始值，可以在那一步改。' },
  { section: 'install', key: 'efiDefaultMb', kind: 'int', effect: '下次生成计划生效', hint: 'EFI（ESP）分区大小，最小 64 MB；向导第 2 步只显示不可改。' },
  { section: 'install', key: 'dataDefaultGb', kind: 'auto-int', effect: '下次生成计划生效', hint: '“自动”= 剩余空间全给数据分区。' },
  { section: 'install', key: 'defaultFs', kind: 'enum', effect: '下次生成计划生效', hint: '数据分区格式化用的文件系统（系统分区始终 NTFS）。' },
  { section: 'install', key: 'imageDefaultIndex', kind: 'int', effect: '下次生成计划生效', hint: '默认镜像里的第几个版本；0 = 用第一个。向导第 3 步可逐盘覆盖。' },

  { section: 'external', key: 'extEnabled', kind: 'bool', effect: '立即生效', hint: '关掉后一律用系统自带命令。' },
  { section: 'external', key: 'extConfirm', kind: 'bool', effect: '立即生效', hint: '调用外部程序前先确认路径。' },
  { section: 'external', key: 'extDiskpart', kind: 'path', pick: 'file', effect: '下次任务生效', hint: '空 = 用系统 diskpart。' },
  { section: 'external', key: 'extDism', kind: 'path', pick: 'file', effect: '下次任务生效', hint: '空 = 用系统 dism。' },
  { section: 'external', key: 'extWimlib', kind: 'path', pick: 'file', effect: '下次任务生效', hint: '可选，装了才用。' },
  { section: 'external', key: 'extVentoy', kind: 'path', pick: 'file', effect: '下次任务生效', hint: '可选，做启动盘才用。' },
  { section: 'external', key: 'ext7z', kind: 'path', pick: 'file', effect: '下次任务生效', hint: '可选，解压驱动包用。' },

  { section: 'system_files', key: 'sysIsoDir', kind: 'path', pick: 'dir', effect: '立即生效', hint: '安装向导第 3 步从这里列镜像。' },
  { section: 'system_files', key: 'sysWim', kind: 'path', pick: 'file', effect: '下次任务生效', hint: '默认系统镜像；向导里可以另选。' },
  { section: 'system_files', key: 'sysBoot', kind: 'path', pick: 'dir', effect: '下次任务生效', hint: '写引导时用的引导文件来源目录。' },
  { section: 'system_files', key: 'sysUnattend', kind: 'path', pick: 'file', effect: '下次任务生效', hint: '可选应答文件，会传给 dism。' },
  { section: 'system_files', key: 'sysDrivers', kind: 'path', pick: 'dir', effect: '下次任务生效', hint: '可选驱动目录，递归注入。' },
  { section: 'system_files', key: 'sysTemp', kind: 'path', pick: 'dir', effect: '下次任务生效', hint: '空 = 系统临时目录；diskpart 脚本也写这里。' },

  { section: 'safety', key: 'protectSystemDisk', kind: 'bool', effect: '立即生效', hint: '系统盘/引导盘一律拒绝写。' },
  { section: 'safety', key: 'dryRunByDefault', kind: 'bool', effect: '下次启动生效', hint: '打开后启动就是演练模式，重启程序才换回真实模式。' },
  { section: 'safety', key: 'blockUnverified', kind: 'bool', effect: '立即生效', hint: '拿不到 UniqueId/序列号就拒绝写盘。' },
  { section: 'safety', key: 'releaseEfiLetter', kind: 'bool', effect: '下次任务生效', hint: '任务结束后摘掉 EFI 分区的临时盘符；关掉会让它一直占着一个盘符。' },
  { section: 'safety', key: 'excludedDiskNames', kind: 'list', effect: '立即生效', hint: '一行一个硬盘型号（子串匹配，不区分大小写）；命中的盘禁止安装、分区和修复引导。' },
  { section: 'safety', key: 'logRetentionDays', kind: 'int', effect: '下次清理生效', hint: '超过天数的任务日志会被删。' },

  { section: 'logging', key: 'logDir', kind: 'path', pick: 'dir', effect: '重启后生效', hint: '空 = 用户目录下的 Sisp 日志目录。' },
  { section: 'logging', key: 'logLevel', kind: 'enum', effect: '立即生效' },
];

const FIELD_BY_KEY = new Map(FIELDS.map((field) => [field.key, field]));

const LABELS = {
  language: '界面语言',
  theme: '主题',
  density: '界面密度',
  rememberWindow: '记住窗口大小和位置',
  autoRefresh: '启动时自动刷新磁盘',
  startView: '启动时打开',
  confirmBeforeStart: '真实写盘前二次确认',
  typeToConfirm: '危险操作需输入确认',
  sysDefaultGb: '系统分区默认大小',
  efiDefaultMb: 'EFI 分区大小',
  imageDefaultIndex: '默认镜像版本',
  dataDefaultGb: '数据分区默认大小',
  defaultFs: '数据分区文件系统',
  extEnabled: '允许调用外部程序',
  extConfirm: '外部程序调用前确认',
  extDiskpart: 'diskpart 路径',
  extDism: 'dism 路径',
  extWimlib: 'wimlib 路径',
  extVentoy: 'Ventoy 路径',
  ext7z: '7-Zip 路径',
  sysIsoDir: '镜像目录',
  sysWim: '系统镜像文件',
  sysBoot: '引导文件目录',
  sysUnattend: '无人值守应答文件',
  sysDrivers: '驱动目录',
  sysTemp: '临时目录',
  protectSystemDisk: '保护系统盘',
  releaseEfiLetter: '任务结束后释放 EFI 盘符',
  dryRunByDefault: '默认演练模式',
  blockUnverified: '身份不明的磁盘禁止写入',
  excludedDiskNames: '禁止操作的硬盘型号',
  logRetentionDays: '日志保留天数',
  logDir: '日志目录',
  logLevel: '日志级别',
};

const PATH_KEYS = new Set(FIELDS.filter((field) => field.kind === 'path').map((field) => field.key));

export function renderSettings() {
  const body = $('#settingsBody');
  if (!body) return;
  const settings = state.settings;
  if (!settings) {
    body.innerHTML = '<div class="empty-hint">设置还没读出来</div>';
    return;
  }

  body.innerHTML = sourceBanner() + SECTIONS.map(([section, title]) => {
    const fields = FIELDS.filter((field) => sectionOf(field.key) === section);
    if (!fields.length) return '';
    const rows = fields.map((field) => renderRow(field, settings)).join('');
    return `<div class="set-sec"><h4>${esc(title)}</h4><div class="set-card">${rows}</div></div>`;
  }).join('');

  bindSettings();
  renderState();
}

/**
 * 顶部一行“当前生效来源”：界面设置优先，除非启动时显式指定了 -j/--config。
 * 没有这一行的话，用户改完设置发现计划没变，会怀疑是 bug。
 */
function sourceBanner() {
  const session = state.session || {};
  const configPath = session.config_path || '';
  const explicit = !!session.config_explicit;
  const text = explicit
    ? `界面设置 · 业务配置已显式指定（${configPath}），同名分区字段以业务配置为准`
    : (configPath
      ? `界面设置（业务配置 ${configPath} 作为可选预设，界面设置优先）`
      : '界面设置');
  return `<div class="set-src" id="settingsSource">当前生效：${esc(text)}</div>`;
}

function sectionOf(key) {
  const field = FIELD_BY_KEY.get(key);
  return field ? field.section : 'general';
}

function renderRow(field, settings) {
  const value = draftValue(field.key);
  const label = LABELS[field.key] || field.key;
  const effect = field.effect ? `<span class="tag">${esc(field.effect)}</span>` : '';
  const desc = field.hint || defaultDesc(field);
  const control = renderControl(field, value);
  const pathBox = field.kind === 'path' ? renderPathBox(field, value) : '';
  return `<div class="set-row${field.kind === 'path' ? ' path-row' : ''}" data-key="${esc(field.key)}">
    <div class="set-l">
      <div class="set-t">${esc(label)}${effect}</div>
      <div class="set-d">${esc(desc)}</div>
      ${pathBox}
    </div>
    <div class="set-c">${control}</div>
  </div>`;
}

function defaultDesc(field) {
  if (field.kind === 'bool') return '开关类设置，保存后立即按新值判断。';
  if (field.kind === 'path') return `路径类设置，可以点“检测”确认是否存在（${field.pick === 'dir' ? '目录' : '文件'}）。`;
  if (field.kind === 'list') return '列表类设置，一行一条；保存时自动去掉空行和重复项。';
  return '';
}

function renderControl(field, value) {
  const key = field.key;
  if (field.kind === 'bool') {
    return `<label class="switch"><input type="checkbox" data-key="${esc(key)}" ${value ? 'checked' : ''}><span class="sl"></span></label>`;
  }
  if (field.kind === 'enum') {
    const options = OPTIONS[key] || [];
    return `<span class="chips" data-key="${esc(key)}">${options.map(([v, text]) => `<button class="chip${String(value) === v ? ' on' : ''}" data-value="${esc(v)}">${esc(text)}</button>`).join('')}</span>`;
  }
  if (field.kind === 'int') {
    return `<input class="numin" type="number" data-key="${esc(key)}" value="${esc(value)}">`;
  }
  if (field.kind === 'auto-int') {
    const isAuto = String(value) === 'auto';
    return `<span class="chips" data-key="${esc(key)}">
        <button class="chip${isAuto ? ' on' : ''}" data-value="auto">自动</button></span>
      <input class="numin" type="number" data-key="${esc(key)}" value="${isAuto ? '' : esc(value)}"
        placeholder="GB" ${isAuto ? 'disabled' : ''}>`;
  }
  if (field.kind === 'path') {
    return `<button class="tbtn" data-act="pick" data-key="${esc(key)}">浏览…</button>
      <button class="tbtn" data-act="probe" data-key="${esc(key)}">检测</button>`;
  }
  if (field.kind === 'list') {
    const items = Array.isArray(value) ? value : [];
    return `<textarea class="txtin" rows="4" spellcheck="false" data-key="${esc(key)}"
      placeholder="一行一个型号，例如：Samsung SSD 980 PRO 1TB">${esc(items.join('\n'))}</textarea>`;
  }
  return `<input class="numin" style="width:200px;text-align:left" data-key="${esc(key)}" value="${esc(value)}">`;
}

function renderPathBox(field, value) {
  const probe = state.probes[field.key] || null;
  const text = value || '（未设置）';
  const iconClass = field.pick === 'dir' ? 'pico folder' : 'pico';
  if (!value) {
    return `<div class="pathbox"><span class="${iconClass}"></span><span class="p pemon">${esc(text)}</span>
      <span class="pstate na">空</span></div>`;
  }
  const stateClass = probe ? probe.state : 'na';
  const stateText = probe
    ? (probe.state === 'ok' ? '存在' : (probe.state === 'builtin' ? '系统自带' : '不存在'))
    : '未检测';
  return `<div class="pathbox"><span class="${iconClass}"></span><span class="p" title="${esc(text)}">${esc(text)}</span>
    <span class="pstate ${esc(stateClass)}">${esc(stateText)}</span></div>`;
}

function bindSettings() {
  $$('#settingsBody input[type="checkbox"][data-key]').forEach((input) => {
    input.addEventListener('change', () => {
      setDraft(input.dataset.key, input.checked);
      markDirty();
      if (['theme', 'density'].includes(input.dataset.key)) applyAppearance({ ...state.settings, ...state.draft });
    });
  });

  $$('#settingsBody .chips[data-key]').forEach((group) => {
    const key = group.dataset.key;
    group.addEventListener('click', (event) => {
      const chip = event.target.closest('.chip');
      if (!chip) return;
      $$('.chip', group).forEach((node) => node.classList.toggle('on', node === chip));
      if (key === 'dataDefaultGb' && chip.dataset.value === 'auto') {
        const input = $(`#settingsBody input[data-key="dataDefaultGb"]`);
        if (input) { input.value = ''; input.disabled = true; }
      } else if (key === 'dataDefaultGb') {
        const input = $(`#settingsBody input[data-key="dataDefaultGb"]`);
        if (input) input.disabled = false;
      }
      setDraft(key, key === 'dataDefaultGb' && chip.dataset.value === 'auto' ? 'auto' : chip.dataset.value);
      markDirty();
      if (key === 'theme' || key === 'density') applyAppearance({ ...state.settings, ...state.draft });
    });
  });

  $$('#settingsBody input.numin[data-key]').forEach((input) => {
    input.addEventListener('input', () => {
      const key = input.dataset.key;
      const field = FIELD_BY_KEY.get(key);
      let value = input.value === '' ? '' : Number(input.value);
      if (field && field.kind === 'int' && Number.isFinite(value)) value = Math.round(value);
      setDraft(key, value === '' && key === 'dataDefaultGb' ? 'auto' : value);
      markDirty();
    });
  });

  // 列表类（一行一条）：先拆行存成数组，清洗交给后端 coerce_value 做，
  // 这样“界面输入 → API → 落盘”只有一套归一化规则。
  $$('#settingsBody textarea[data-key]').forEach((area) => {
    area.addEventListener('input', () => {
      setDraft(
        area.dataset.key,
        area.value.split('\n').map((line) => line.trim()).filter(Boolean),
      );
      markDirty();
    });
  });

  $$('#settingsBody button[data-act]').forEach((button) => {
    button.addEventListener('click', () => {
      const key = button.dataset.key;
      if (button.dataset.act === 'probe') void probeRow(key);
      else void pickRow(key);
    });
  });
}

async function probeRow(key) {
  const field = FIELD_BY_KEY.get(key);
  try {
    const result = await api.probePath(key);
    state.probes[key] = result;
    const row = $(`#settingsBody .set-row[data-key="${key}"]`);
    if (row) {
      const box = row.querySelector('.pathbox');
      if (box) {
        const current = draftValue(key);
        box.outerHTML = renderPathBox(field, current);
      }
    }
    toast(`${LABELS[key] || key}：${result.state === 'ok' ? '路径存在' : (result.message || '不可用')}`,
      result.state === 'ok' ? '' : 'err');
  } catch (exc) {
    toast(`检测失败：${exc.message || exc}`, 'err');
  }
}

async function pickRow(key) {
  const field = FIELD_BY_KEY.get(key);
  try {
    const result = await api.pickPath(key, field && field.pick ? field.pick : 'file');
    if (result && result.cancelled) return;
    const chosen = (result && result.path) || '';
    if (!chosen) return;
    setDraft(key, chosen);
    markDirty();
    state.probes[key] = null;
    renderSettings();
    void probeRow(key);
  } catch (exc) {
    toast(`选择路径失败：${exc.message || exc}`, 'err');
  }
}

function markDirty() {
  renderState();
}

function renderState() {
  const count = dirtyCount();
  const node = $('#setState');
  if (node) node.textContent = count ? `未保存的改动 ${count} 项` : '';
  const save = $('#setSave');
  if (save) {
    save.disabled = count === 0;
    save.textContent = count ? `保存设置（${count}）` : '保存设置';
  }
}

export async function saveSettings() {
  if (!dirtyCount()) return;
  const patch = { ...state.draft };
  try {
    const data = await api.saveSettings(patch);
    state.settings = data.settings || state.settings;
    state.settingsMeta = data;
    state.draft = {};
    // 保存后清掉对应的探测结果，重新检测
    for (const key of Object.keys(patch)) {
      if (PATH_KEYS.has(key)) state.probes[key] = null;
    }
    applyAppearance(state.settings);
    renderSettings();
    pushLog(`设置已保存：${Object.keys(patch).join('、')}`, '', 'settings');
    toast('设置已保存');
    const effects = new Set(Object.keys(patch).map((key) => (FIELD_BY_KEY.get(key) || {}).effect).filter(Boolean));
    const restart = Array.from(effects).find((text) => text.includes('重启'));
    if (restart) toast(`${restart}：${Array.from(effects).join('、')}`);
    for (const warning of data.warnings || []) pushLog(`设置提醒：${warning}`, 'warn', 'settings');
  } catch (exc) {
    const message = exc instanceof ApiError ? exc.message : String(exc);
    toast(`保存失败：${message}`, 'err');
    pushLog(`保存设置失败：${message}`, 'err', 'settings');
  }
}

export async function resetSettings() {
  try {
    const data = await api.resetSettings();
    state.settings = data.settings || state.settings;
    clearDraft();
    state.probes = {};
    applyAppearance(state.settings);
    renderSettings();
    toast('已恢复默认设置');
    pushLog('设置已恢复默认值', '', 'settings');
  } catch (exc) {
    toast(`恢复默认失败：${exc.message || exc}`, 'err');
  }
}

export async function exportSettings() {
  let target = '';
  try {
    // 桌面窗口有原生保存框；浏览器模式（--serve）没有，后端会落到设置目录并回报路径
    const picked = await api.pickPath('', 'save').catch(() => null);
    if (picked && picked.cancelled) return;
    target = (picked && picked.path) || '';
  } catch (exc) {
    target = '';
  }
  try {
    const data = await api.exportSettings(target || undefined);
    toast(`设置已导出：${data.path}`);
    pushLog(`设置已导出：${data.path}`, '', 'settings');
  } catch (exc) {
    toast(`导出失败：${exc.message || exc}`, 'err');
  }
}

export async function importSettings() {
  try {
    const picked = await api.pickPath('', 'file').catch(() => null);
    if (!picked || picked.cancelled || !picked.path) {
      if (picked && picked.cancelled) return;
      toast('浏览器模式下请输入设置文件路径', 'err');
      return;
    }
    const data = await api.importSettings({ path: picked.path });
    state.settings = data.settings || state.settings;
    clearDraft();
    state.probes = {};
    applyAppearance(state.settings);
    renderSettings();
    toast('设置已导入');
    pushLog(`设置已从 ${picked.path} 导入`, '', 'settings');
  } catch (exc) {
    toast(`导入失败：${exc.message || exc}`, 'err');
  }
}

export async function probeAllSettings() {
  try {
    const result = await api.probeAllPaths();
    state.probes = result || {};
    renderSettings();
    const bad = Object.values(state.probes).filter((item) => item.state === 'miss');
    toast(bad.length ? `${bad.length} 个路径不存在` : '所有路径都可用', bad.length ? 'err' : '');
  } catch (exc) {
    toast(`批量检测失败：${exc.message || exc}`, 'err');
  }
}

export { FIELDS as SETTINGS_FIELDS, LABELS as SETTINGS_LABELS };
