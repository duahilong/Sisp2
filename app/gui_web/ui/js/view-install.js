/**
 * 系统安装：四步向导（选盘 → 分区 → 版本 → 确认）+ 进度页。
 *
 * 写盘链路只有一条，和 CLI 完全相同：
 *   POST /api/prepare  →  POST /api/submit  →  POST /api/confirm
 * 界面不自己拼 diskpart/Ghost 命令，也不自己算盘符：第 4 步显示的盘符来自
 * `plan.drive_letters`，第 2 步的大小只是给 `prepare` 的入参，后端
 * `_check_partition_fit` 还会再验一次。
 */

import { api, ApiError } from './api.js';
import { $, $$, esc, on } from './dom.js';
import {
  fmtSize, segments, fsColor, isSelectable,
  protectionText, clamp, MIN_SYS_GB, MIN_DATA_GB, MIN_EXTRA_GB, MAX_EXTRAS,
  toGb, fromGb, extraColor, extraLetter, fitLayout, autoPartitionRole,
  phaseGroups, progressByPhase, skippedByFailure, layoutVerdict, mergePlanNotes,
} from './derive.js';
import {
  state, diskOf, targetOf, targetError, autoPartitionBytes, syncAutoPartition, addTarget, removeTarget,
  addExtraPartition, removeExtraPartition, selectionPayload, resetWizard, applyFit,
  reserveGb, reserveEfiMb, dataFs, STEP_LABELS, JOB_STATUS_TEXT,
} from './state.js';
import { pushLog, toast, confirmDialog, openModal, jobStatusText } from './shell.js';
import { lostPartitionsText } from './view-main.js';

const GB = 1024 ** 3;
const images = {
  loading: false,
  error: '',
  versions: [],      // [{id, label, path, ok, reason, block_index, size_display}]
  defaultPath: '',   // 第一个可用版本，默认选中它
};

const ROLE_TEXT = { efi: 'EFI 分区', windows: '系统分区', data: '数据分区' };

/** 角色 → 界面名字：所有数据分区（D/E）都叫“数据分区”，靠盘符区分。 */
function roleLabel(role) {
  if (ROLE_TEXT[role]) return ROLE_TEXT[role];
  if (String(role).startsWith('data')) return '数据分区';
  return String(role || '分区');
}

/* 进度页里每块盘的内联日志：来自 GET /api/jobs/{id}/log 的尾部，轮询刷新。 */
const logCache = new Map();

/* ------------------------------------------------------------------ 渲染入口 */
export function renderInstall() {
  // 第 2 步的自定值（最后一个数据分区的预估）在渲染前对齐一次：从后端拿回计划、
  // 切盘、改设置（EFI 大小）之后，state 里的数字可能已经不是“剩下多少”了。
  if (state.wizard.step === 2 && state.wizard.phase === 'form') {
    state.wizard.targets.forEach((target) => syncAutoPartition(target));
  }
  renderStepper();
  renderBody();
  renderFoot();
  updateStepperHint();
}

function renderStepper() {
  const { step, phase } = state.wizard;
  $$('#stepper li').forEach((item) => {
    const number = Number(item.dataset.step);
    item.classList.toggle('on', phase === 'form' && number === step);
    const done = phase === 'running' || phase === 'done'
      ? number < 4
      : number < step;
    item.classList.toggle('done', done && !(phase === 'form' && number === step));
  });
  const stepper = $('#stepper');
  if (stepper) stepper.style.display = phase === 'form' ? '' : 'none';
}

function renderFoot() {
  const { step, phase } = state.wizard;
  const back = $('#wzBack');
  const next = $('#wzNext');
  const hint = $('#wzHint');
  const foot = $('.wz-foot');
  if (foot) foot.style.display = phase === 'form' ? '' : 'none';
  if (phase !== 'form') return;

  back.disabled = step <= 1;
  back.textContent = '上一步';
  next.textContent = step >= 4 ? '开始安装' : '下一步';
  next.classList.toggle('danger', step >= 4);
  next.classList.toggle('primary', step < 4);

  let reason = '';
  if (step === 1 && !state.wizard.targets.length) reason = '先勾选至少一块要装系统的硬盘';
  if (step === 2) {
    for (const target of state.wizard.targets) {
      const error = targetError(target);
      if (error) { reason = `磁盘 ${target.diskNumber}：${error}`; break; }
    }
  }
  if (step === 3) {
    for (const target of state.wizard.targets) {
      if (!target.imagePath) {
        reason = `磁盘 ${target.diskNumber}：还没选系统版本`;
        break;
      }
      const chosen = versionByPath(target.imagePath);
      if (chosen && !chosen.ok) {
        reason = `磁盘 ${target.diskNumber}：${chosen.reason || '镜像不可用'}`;
        break;
      }
    }
  }
  if (step === 4) {
    if (state.wizard.error) reason = reason || `生成计划失败：${state.wizard.error}`;
    else if (!state.plans.length) reason = reason || '正在生成执行计划…';
    else if (!state.plans.every((item) => item.plan.executable)) reason = reason || '有阻断项，先解决再开始';
  }
  if (step === 4 && !state.wizard.ack) reason = reason || '请先勾选“我明白…会被全部删除”的确认框';

  next.disabled = Boolean(reason);
  next.title = reason || '';
  hint.textContent = reason || hintText(step);
  hint.classList.toggle('warn', Boolean(reason));
}

function hintText(step) {
  if (step === 1) return `${state.disks.filter(isSelectable).length} 块盘可安装 · 系统盘和只读盘已置灰`;
  if (step === 2) return '拖分隔条调大小：最后一个数据分区永远吃满剩余，剩下多少就给它多少';
  if (step === 3) return images.loading ? '正在读取系统版本…' : '每块盘选一个系统版本，装完再写 UEFI 引导';
  if (step === 4) return state.info && state.info.dry_run
    ? '当前是演练模式：点开始只会走一遍计划，不会真的写盘'
    : '点开始后先跑一轮预检，任何一项不通过都会拒绝写盘';
  return '';
}

function updateStepperHint() {
  const { phase } = state.wizard;
  const sub = $('.wz-sub');
  if (!sub) return;
  if (phase === 'running') sub.textContent = '正在写盘 · 请勿断电或拔盘 · 可以切到主页面看盘，进度不会丢';
  else if (phase === 'done') sub.textContent = '全部完成 · 可以回到主页面确认分区和盘符';
  else if (phase === 'failed') sub.textContent = '执行失败 · 磁盘可能只分了一部分，看日志里的失败步骤';
  else sub.textContent = '四步完成 · 点左上角「主页面」随时回去看盘，进度不会丢';
}

/* ------------------------------------------------------------------ 正文 */
function renderBody() {
  const body = $('#wzBody');
  if (!body) return;
  const { step, phase } = state.wizard;
  if (phase === 'running' || phase === 'done' || phase === 'failed') {
    body.innerHTML = renderProgressHtml();
    bindProgress();
    return;
  }
  if (step === 1) { body.innerHTML = stepDiskHtml(); bindStepDisk(); }
  else if (step === 2) { body.innerHTML = stepSizeHtml(); bindStepSize(); }
  else if (step === 3) { body.innerHTML = stepVersionHtml(); bindStepVersion(); }
  else { body.innerHTML = stepConfirmHtml(); bindStepConfirm(); }
}

/* ---- 第 1 步 ---- */
function stepDiskHtml() {
  const rows = state.disks.map((disk) => {
    const number = Number(disk.disk_number);
    const selectable = isSelectable(disk);
    const picked = Boolean(targetOf(number));
    const cls = ['dsel'];
    if (picked) cls.push('on');
    if (!selectable) cls.push('off');
    const bars = segments(disk).map((seg) => `<i style="width:${(seg.ratio * 100).toFixed(2)}%;background:${seg.color}"></i>`).join('');
    const lockText = selectable ? '' : ` · ${esc(protectionText(disk) || '不可操作')}`;
    return `<button class="${cls.join(' ')}" data-disk="${number}" ${selectable ? '' : 'disabled'}>
      <span class="cb"></span>
      <span class="dmain">
        <span class="dt">磁盘 ${number} · ${esc(disk.model || '未知型号')} · ${esc(fmtSize(disk.size_bytes))}
          <span class="tag${selectable ? '' : ' no'}">${selectable ? '可选' : '已锁定'}</span>
        </span>
        <span class="dm">${esc(disk.partition_style || '')} · ${esc(disk.bus_type || '')} · ${(disk.partitions || []).length} 个分区${lockText}</span>
        <span class="dbar">${bars}</span>
      </span>
    </button>`;
  }).join('');

  return `<div class="wz-inner">
    <h3 class="wz-h">选择要装系统的硬盘</h3>
    <p class="wz-p">可以一次选多块盘。系统盘、只读盘会置灰；真实写盘时后端会重扫一次并复核磁盘身份（有 UniqueId / 序列号就比标识，没有就比盘号 + 容量 + 型号）。</p>
    <div class="selbar">已选 <b id="selCount">${state.wizard.targets.length}</b> 块
      <button class="lnk" id="selAll">全选可安装</button>
      <button class="lnk" id="selNone">清空</button>
    </div>
    ${rows || '<div class="empty-hint">没有扫描到磁盘。插好硬盘后按 F5 刷新。</div>'}
  </div>`;
}

function bindStepDisk() {
  $$('#wzBody .dsel').forEach((node) => {
    node.addEventListener('click', () => {
      const number = Number(node.dataset.disk);
      if (targetOf(number)) removeTarget(number);
      else addTarget(number);
      renderInstall();
    });
  });
  const selAll = $('#selAll');
  if (selAll) {
    selAll.addEventListener('click', () => {
      state.disks.filter(isSelectable).forEach((disk) => addTarget(disk.disk_number));
      renderInstall();
    });
  }
  const selNone = $('#selNone');
  if (selNone) {
    selNone.addEventListener('click', () => {
      resetWizard(true);
      state.wizard.step = 1;
      renderInstall();
    });
  }
}

/** 第 2 步的 EFI 提示：优先用执行计划里真实的 EFI 大小（来自设置页默认值）。 */
function efiText() {
  const item = state.plans && state.plans.find((plan) => plan.spec && plan.spec.task_payload);
  const info = item && item.spec.task_payload.partition_info;
  const mb = info && Number(info.efi_size_mb);
  return mb > 0 ? `EFI ${Math.round(mb)} MB` : 'EFI 分区';
}

/* ---- 第 2 步 ---- */
/** 一块盘的分区布局（字节 + 百分比），进度条和脚注共用。 */
function layoutOf(target, total) {
  const sysBytes = fromGb(Number(target.sysGb) || 0);
  const dataBytes = fromGb(Number(target.dataGb) || 0);
  const extraBytes = (Array.isArray(target.extras) ? target.extras : [])
    .map((extra) => fromGb(Number(extra.sizeGb) || 0));
  const pct = (bytes) => (total ? (bytes / total) * 100 : 0);
  return {
    sysBytes,
    dataBytes,
    extraBytes,
    sysPct: pct(sysBytes),
    dataPct: pct(dataBytes),
    extraPct: extraBytes.map(pct),
  };
}

function extraRowsHtml(target) {
  const extras = Array.isArray(target.extras) ? target.extras : [];
  return extras.map((extra, index) => `<div class="szrow extra">
        <span class="szname"><span class="lbadge" style="background:${extraColor(index)}">${extraLetter(index)}</span>数据分区</span>
        <input type="number" min="${MIN_EXTRA_GB}" step="1" value="${esc(extra.sizeGb)}"
          data-size="extra" data-index="${index}" data-disk="${target.diskNumber}" readonly>
        <span class="unit">GB</span>
        <span class="szsub">${esc(dataFs())} · 剩余自动占满</span>
        <button class="rm" data-remove="${index}" data-disk="${target.diskNumber}" title="删除这个分区">✕</button>
      </div>`).join('');
}

function stepSizeHtml() {
  const cards = state.wizard.targets.map((target) => {
    const disk = diskOf(target.diskNumber);
    if (!disk) return '';
    const total = Number(disk.size_bytes) || GB;
    const layout = layoutOf(target, total);
    const error = targetError(target);
    const extras = Array.isArray(target.extras) ? target.extras : [];
    const extraSegs = layout.extraBytes.map((bytes, index) => `<span class="wseg ext" data-extra="${index}" style="width:${layout.extraPct[index].toFixed(2)}%;background:${extraColor(index)}"><span>${extraLetter(index)} ${Math.round(Number(extras[index].sizeGb) || 0)} GB</span></span>`).join('');
    const addButton = extras.length < MAX_EXTRAS
      ? `<button class="addpart" data-add="${target.diskNumber}">➕ 添加数据分区</button>`
      : '<div class="szsub">已添加数据分区（最多 1 个）</div>';
    return `<div class="tgt" data-disk="${target.diskNumber}">
      <div class="tgt-h">
        <span class="tn">磁盘 ${target.diskNumber}</span>
        <span class="tm">${esc(disk.model || '')} · ${esc(disk.partition_style || '')}</span>
        <span class="trest">总容量 <b data-total>${esc(fmtSize(total))}</b></span>
      </div>
      <div class="szrow">
        <span class="szname"><span class="lbadge" style="background:${fsColor('NTFS')}">C</span>系统分区（Windows）</span>
        <input type="number" min="${MIN_SYS_GB}" step="1" value="${esc(target.sysGb)}" data-size="sys" data-disk="${target.diskNumber}">
        <span class="unit">GB</span>
        <span class="szsub">NTFS · 会装系统，至少 ${MIN_SYS_GB} GB</span>
      </div>
      <div class="szrow">
        <span class="szname"><span class="lbadge" style="background:${fsColor('NTFS')}">D</span>数据分区</span>
        <input type="number" min="${MIN_DATA_GB}" step="1" value="${esc(target.dataGb)}"
          data-size="data" data-disk="${target.diskNumber}"${autoRole(target) === 'data' ? ' readonly' : ''}>
        <span class="unit">GB</span>
        <span class="szsub">${esc(dataFs())} · ${autoRole(target) === 'data' ? '剩余容量自动占满' : '建完即可用'}</span>
      </div>
      ${extraRowsHtml(target)}
      ${addButton}
      <div class="wzbar" data-disk="${target.diskNumber}" tabindex="0" title="拖动分隔条调容量：C|D 改系统分区，D|E 改两个数据分区的比例">
        <span class="wseg sys" style="width:${layout.sysPct.toFixed(2)}%"><span>系统 ${esc(target.sysGb)} GB</span></span>
        <span class="wseg dat" style="width:${layout.dataPct.toFixed(2)}%"><span>数据 ${esc(target.dataGb)} GB</span></span>
        ${extraSegs}
        <span class="wgrip" data-grip="sys" style="left:${layout.sysPct.toFixed(2)}%" title="拖动调整系统分区大小"></span>
        ${extras.length ? `<span class="wgrip" data-grip="data" style="left:${(layout.sysPct + layout.dataPct).toFixed(2)}%" title="拖动调整 D / E 容量"></span>` : ''}
      </div>
      <div class="tgt-f">
        <span class="${error ? 'errmsg' : 'szsub'}" data-err="${target.diskNumber}">${esc(error || layoutText(target))}</span>
        <span class="presets" data-disk="${target.diskNumber}">
          ${[100, 120, 150, 200].map((gb) => `<button data-preset="${gb}">${gb}</button>`).join('')}
        </span>
      </div>
    </div>`;
  }).join('');

  return `<div class="wz-inner">
    <h3 class="wz-h">分区与格式化</h3>
    <p class="wz-p">每块盘会被重新分区：EFI 分区 + 系统分区 + 数据分区，GPT + UEFI。需要的话可以再加一个数据分区（E）。数字只影响分区大小：<b>最后一个数据分区永远占满剩余容量</b>（它的输入框是只读的，尺寸由 diskpart 决定，不会出现“算多了建不出来”），盘上原有数据会全部丢失。</p>
    ${cards || '<div class="empty-hint">还没有选盘</div>'}
  </div>`;
}

/** 这块盘“吃剩余空间”的那个分区：有 E 就是 E，否则是 D。 */
function autoRole(target) {
  return autoPartitionRole(Array.isArray(target.extras) ? target.extras : []);
}

/** 脚注：各分区大小；最后那个自动分区显示预估大小（真实尺寸由 diskpart 决定）。 */
function layoutText(target) {
  const disk = diskOf(target.diskNumber);
  const extras = Array.isArray(target.extras) ? target.extras : [];
  const isAutoExtra = autoRole(target) === 'extra';
  const parts = [`系统 ${Math.round(Number(target.sysGb) || 0)} GB`];
  if (isAutoExtra) parts.push(`数据(D) ${Math.round(Number(target.dataGb) || 0)} GB`);
  const autoBytes = autoPartitionBytes(target);
  const autoLabel = isAutoExtra ? `数据(${extraLetter(0)})` : '数据(D)';
  parts.push(`${autoLabel} 剩余（约 ${fmtSize(autoBytes)}）`);
  return `${efiText()} + ${parts.join(' + ')}`;
}

function bindStepSize() {
  $$('#wzBody input[data-size]').forEach((input) => {
    input.addEventListener('input', () => {
      const target = targetOf(Number(input.dataset.disk));
      if (!target) return;
      const value = Number(input.value);
      if (input.dataset.size === 'sys') target.sysGb = Number.isFinite(value) ? value : 0;
      else target.dataGb = Number.isFinite(value) ? value : 0;
      // 最后一个数据分区是 auto（不带 size=），它的预估值必须跟着手填的数字走，
      // 否则输入框和进度条加起来不等于盘容量（用户实测：C 116 + D 276.6 ≠ 477）
      syncAutoPartition(target);
      state.wizard.ack = false;
      updateSizeVisual(target);
      renderFoot();
    });
    input.addEventListener('blur', () => {
      const target = targetOf(Number(input.dataset.disk));
      if (!target) return;
      // 失焦时把非法值拉回边界，避免界面一直停在“输入错误”的状态
      const key = input.dataset.size === 'sys' ? 'sysGb' : 'dataGb';
      const min = input.dataset.size === 'sys' ? MIN_SYS_GB : MIN_DATA_GB;
      if (!(target[key] >= min)) target[key] = min;
      syncAutoPartition(target);
      renderInstall();
    });
  });

  $$('#wzBody input[data-size="extra"]').forEach((input) => {
    input.addEventListener('input', () => {
      const target = targetOf(Number(input.dataset.disk));
      if (!target) return;
      const extra = (target.extras || [])[Number(input.dataset.index)];
      if (!extra) return;
      const value = Number(input.value);
      extra.sizeGb = Number.isFinite(value) ? value : 0;
      state.wizard.ack = false;
      updateSizeVisual(target);
      renderFoot();
    });
    input.addEventListener('blur', () => {
      const target = targetOf(Number(input.dataset.disk));
      if (!target) return;
      const extra = (target.extras || [])[Number(input.dataset.index)];
      if (extra && !(Number(extra.sizeGb) >= MIN_EXTRA_GB)) extra.sizeGb = MIN_EXTRA_GB;
      renderInstall();
    });
  });

  $$('#wzBody button[data-add]').forEach((button) => {
    button.addEventListener('click', () => {
      if (!addExtraPartition(Number(button.dataset.add))) {
        toast('这块盘没有空间再加一个分区了', 'err');
        return;
      }
      renderInstall();
    });
  });

  $$('#wzBody button[data-remove]').forEach((button) => {
    button.addEventListener('click', () => {
      removeExtraPartition(Number(button.dataset.disk), Number(button.dataset.remove));
      renderInstall();
    });
  });

  $$('#wzBody .presets button').forEach((button) => {
    button.addEventListener('click', () => {
      const target = targetOf(Number(button.parentElement.dataset.disk));
      if (!target) return;
      const disk = diskOf(target.diskNumber);
      const totalGb = toGb(Number(disk && disk.size_bytes) || 0);
      // 必须走 fitLayout：它会把 E 也一起算进去。以前这里只算 C、D，
      // E 保持原值，于是“C200 + D275.9 + E71.7”超过了 477 GB 的盘。
      // even: 选「200」的预期是“系统 200，剩下的 D/E 平分”，不是 D 独吞剩余。
      const fit = fitLayout(
        totalGb,
        Number(button.dataset.preset),
        (Array.isArray(target.extras) ? target.extras : []).map((extra) => Number(extra.sizeGb) || 0),
        { even: true, efiMb: reserveEfiMb() },
      );
      applyFit(target, fit);
      state.wizard.ack = false;
      renderInstall();
    });
  });

  $$('#wzBody .wzbar').forEach((bar) => bindDrag(bar));
}

/** 只更新一块盘的条与错误提示，保持输入框焦点（不整页重绘）。 */
function updateSizeVisual(target) {
  const card = $(`#wzBody .tgt[data-disk="${target.diskNumber}"]`);
  if (!card) return;
  const disk = diskOf(target.diskNumber);
  const total = Number(disk && disk.size_bytes) || GB;
  const layout = layoutOf(target, total);
  const extras = Array.isArray(target.extras) ? target.extras : [];
  const sysPct = clamp(layout.sysPct, 0, 100);
  const dataPct = clamp(layout.dataPct, 0, 100 - sysPct);
  const extraPct = layout.extraPct.map((pct) => clamp(pct, 0, 100));

  const sysSeg = card.querySelector('.wseg.sys');
  const dataSeg = card.querySelector('.wseg.dat');
  if (sysSeg) { sysSeg.style.width = `${sysPct}%`; sysSeg.firstElementChild.textContent = `系统 ${Math.round(target.sysGb)} GB`; }
  // 吃满剩余的那个分区是只读输入框：改 C / 改显式 D 时它自己不触发 input 事件，
  // 所以这里要把重算出来的预估值写回输入框——否则用户看到的数字停在旧值，
  // C + D 加起来也不等于盘容量。
  const autoInput = card.querySelector(
    autoRole(target) === 'extra'
      ? `input[data-size="extra"][data-index="${extras.length - 1}"]`
      : 'input[data-size="data"]',
  );
  if (autoInput && autoInput.readOnly) autoInput.value = autoRole(target) === 'extra'
    ? (extras[extras.length - 1] && extras[extras.length - 1].sizeGb)
    : target.dataGb;
  if (dataSeg) { dataSeg.style.width = `${dataPct}%`; dataSeg.firstElementChild.textContent = `数据 ${Math.round(target.dataGb)} GB`; }
  card.querySelectorAll('.wseg.ext').forEach((seg, index) => {
    if (!(index in extraPct)) return;
    seg.style.width = `${extraPct[index]}%`;
    seg.firstElementChild.textContent = `${extraLetter(index)} ${Math.round(Number(extras[index] && extras[index].sizeGb) || 0)} GB`;
  });
  card.querySelectorAll('.wgrip').forEach((grip) => {
    if (grip.dataset.grip === 'data') grip.style.left = `${sysPct + dataPct}%`;
    else grip.style.left = `${sysPct}%`;
  });
  const error = targetError(target);
  const box = card.querySelector(`[data-err="${target.diskNumber}"]`);
  if (box) {
    box.className = error ? 'errmsg' : 'szsub';
    box.textContent = error || layoutText(target);
  }
}

function bindDrag(bar) {
  const target = targetOf(Number(bar.dataset.disk));
  const disk = diskOf(bar.dataset.disk);
  if (!target || !disk) return;
  const total = Number(disk.size_bytes) || GB;
  const totalGb = toGb(total);
  const extras = () => (Array.isArray(target.extras) ? target.extras : []);
  const extrasGbList = () => extras().map((extra) => Number(extra.sizeGb) || 0);
  // 向下取整：吸收变化的那一侧永远不吃掉最后 0.1 GB，保证总和不超过真实容量。
  const floor1 = (value) => Math.floor(value * 10) / 10;
  const round1 = (value) => Math.round(value * 10) / 10;
  let mode = 'sys';

  /** 把 C 设成 newSysGb，D/E 一起重新分配（fitLayout 保证不超容量）。 */
  const applySys = (newSysGb) => {
    applyFit(target, fitLayout(totalGb, newSysGb, extrasGbList(), { efiMb: reserveEfiMb() }));
  };

  /** D|E 分界：两者总量不变，只在中间挪——所以永远不会超过盘容量。 */
  const applySplit = (ratio) => {
    const list = extras();
    if (!list.length) return;
    const available = totalGb - Number(target.sysGb || 0) - reserveGb();
    const dataGb = clamp(
      round1(totalGb * ratio - Number(target.sysGb || 0)),
      MIN_DATA_GB,
      Math.max(MIN_DATA_GB, floor1(available - MIN_EXTRA_GB)),
    );
    target.dataGb = dataGb;
    list[0].sizeGb = Math.max(MIN_EXTRA_GB, floor1(available - dataGb));
  };

  const syncInputs = () => {
    const card = bar.parentElement;
    const sysInput = card.querySelector('input[data-size="sys"]');
    const dataInput = card.querySelector('input[data-size="data"]');
    const extraInput = card.querySelector('input[data-size="extra"]');
    if (sysInput) sysInput.value = target.sysGb;
    if (dataInput) dataInput.value = target.dataGb;
    const extra = extras()[0];
    if (extraInput && extra) extraInput.value = extra.sizeGb;
  };

  const drag = (clientX) => {
    const rect = bar.getBoundingClientRect();
    if (!rect.width) return;
    const ratio = clamp((clientX - rect.left) / rect.width, 0, 1);
    if (mode === 'data') applySplit(ratio);
    else applySys(round1(totalGb * ratio));
    syncInputs();
    updateSizeVisual(target);
  };

  const onMove = (event) => {
    if (!bar.classList.contains('drag')) return;
    drag(event.clientX);
    event.preventDefault();
  };
  const stop = () => {
    if (!bar.classList.contains('drag')) return;
    bar.classList.remove('drag');
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', stop);
    renderFoot();
  };
  bar.addEventListener('pointerdown', (event) => {
    const grip = event.target && event.target.closest ? event.target.closest('.wgrip') : null;
    // 拖到哪根分隔条就调哪一段；点条的空白处仍然按老习惯调系统分区
    mode = grip && grip.dataset.grip === 'data' ? 'data' : 'sys';
    bar.classList.add('drag');
    drag(event.clientX);
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', stop);
    event.preventDefault();
  });
  bar.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const step = event.key === 'ArrowLeft' ? -1 : 1;
    if (extras().length && event.shiftKey) {
      // Shift + 左右：调 D|E 分界
      const available = totalGb - Number(target.sysGb || 0) - reserveGb();
      const dataGb = clamp(
        round1(Number(target.dataGb || 0) + step),
        MIN_DATA_GB,
        Math.max(MIN_DATA_GB, floor1(available - MIN_EXTRA_GB)),
      );
      target.dataGb = dataGb;
      extras()[0].sizeGb = Math.max(MIN_EXTRA_GB, floor1(available - dataGb));
    } else {
      applySys(Math.round(Number(target.sysGb) || 0) + step);
    }
    renderInstall();
    event.preventDefault();
  });
}

/* ---- 第 3 步 ---- */
function versionByPath(path) {
  return (images.versions || []).find((version) => version.path === path) || null;
}

/** 默认选中的版本：后端给的第一个可用版本，没有就用第一个配置了的。 */
function defaultImagePath() {
  if (images.defaultPath) return images.defaultPath;
  const first = (images.versions || [])[0];
  return first ? first.path : '';
}

function stepVersionHtml() {
  const versions = images.versions || [];
  const rows = state.wizard.targets.map((target) => {
    const current = target.imagePath || defaultImagePath();
    const chips = versions.map((version) => `<button class="vchip${version.path === current ? ' on' : ''}"
        data-disk="${target.diskNumber}" data-path="${esc(version.path)}"
        title="${esc(version.path)}">${esc(version.label)}${version.ok ? '' : '<em>不可用</em>'}</button>`).join('');
    const chosen = versionByPath(current);
    let hint = '';
    if (images.error) {
      hint = `<span class="errmsg">${esc(images.error)}</span>`;
    } else if (chosen) {
      // 只显示 Ghost 自己的判定：文件头对不对、分区块几号。
      hint = chosen.ok
        ? `<span class="szsub ok">Ghost 分区块 #${esc(String(chosen.block_index || 1))} · ${esc(chosen.size_display || '大小未知')}</span>`
        : `<span class="errmsg">${esc(chosen.reason || '不是有效的 Ghost 镜像')}</span>`;
    } else if (current) {
      hint = `<span class="szsub">${esc(current)}</span>`;
    }
    return `<div class="vrow">
      <span class="vrow-n">磁盘 ${target.diskNumber}</span>
      <span class="vchips">${chips || '<span class="szsub">设置里还没有配系统镜像</span>'}</span>
      <span class="vrow-hint">${hint}</span>
    </div>`;
  }).join('');

  return `<div class="wz-inner">
    <h3 class="wz-h">系统版本</h3>
    <p class="wz-p">每块盘单独选一个系统版本，互不影响。版本来自「设置 → 系统文件」里的系统 A / 系统 B 两个槽位，每个槽位存一个 GHO。</p>
    <div class="selbar">
      已配置 <b>${esc(String(versions.length))}</b> 个版本
      <button class="lnk" id="imgReload">重新读取</button>
      ${images.loading ? '<span class="szsub">正在读取…</span>' : ''}
    </div>
    ${rows || '<div class="empty-hint">还没有选盘</div>'}
    ${versions.length === 0
      ? `<div class="warnbox"><span class="wi">!</span><div>设置里还没配系统镜像。先去「设置 → 系统文件」把系统 A / 系统 B 的 GHO 指上。</div></div>`
      : ''}
  </div>`;
}

function bindStepVersion() {
  $$('#wzBody .vchip[data-disk]').forEach((chip) => {
    chip.addEventListener('click', () => {
      const target = targetOf(Number(chip.dataset.disk));
      if (!target || target.imagePath === chip.dataset.path) return;
      target.imagePath = chip.dataset.path;
      target.imageIndex = null;
      const version = versionByPath(target.imagePath);
      target.imageLabel = version ? version.label : '';
      state.wizard.ack = false;
      renderInstall();
    });
  });
  const reload = $('#imgReload');
  if (reload) reload.addEventListener('click', () => { void loadImages(); });
}

/**
 * 读取设置里的两个系统版本（顺带读文件头）。
 *
 * ``POST /api/images`` 不带参数：后端把设置里的 A/B 槽位逐个 sniff 一遍，返回
 * ``{versions, default}``。「重新读取」按钮与进入第 3 步时都会调它，所以必须 export。
 */
export async function loadImages() {
  if (images.loading) return;
  images.loading = true;
  images.error = '';
  if (state.view === 'install' && state.wizard.step === 3) renderInstall();
  try {
    const data = await api.images({});
    images.versions = (data && data.versions) || [];
    images.defaultPath = (data && data.default) || '';
  } catch (exc) {
    images.versions = [];
    images.defaultPath = '';
    images.error = exc instanceof ApiError ? exc.message : String(exc);
  } finally {
    images.loading = false;
  }
  // 还没选过版本的盘默认用第一个可用版本，避免用户必须挨个点。
  const fallback = defaultImagePath();
  if (fallback) {
    for (const target of state.wizard.targets) {
      if (!target.imagePath) target.imagePath = fallback;
      const version = versionByPath(target.imagePath);
      if (version) target.imageLabel = version.label;
    }
  }
  if (state.view === 'install') renderInstall();
}

/* ---- 第 4 步 ---- */
/* 不再要求手输盘号：界面上只有一处勾选，后端 confirm() 也不再校验文本。 */

/** 一行分区：盘符徒标 + 角色名 + 尺寸 / 文件系统。 */
function partRowHtml(role, letter, size, fileSystem) {
  const text = String(letter || '').trim();
  const color = fsColor(fileSystem || (role === 'efi' ? 'FAT32' : 'NTFS'));
  const badge = text
    ? `<span class="lbadge" style="background:${color}">${esc(text)}</span>`
    : '<span class="lbadge ghost">·</span>';
  return `<div class="pline">${badge}<span class="pln">${esc(roleLabel(role))}</span>
    <span class="pls">${esc(size || '')}${fileSystem ? ` · ${esc(fileSystem)}` : ''}</span></div>`;
}

/**
 * 每块盘一张卡：磁盘 / 型号 / 容量一行，分区几行，镜像一行，脚本默认折叠。
 *
 * 只用一次的信息不再重复：盘符并在各自分区行，模式提到页脚，MSR 行删掉
 * （`convert gpt` 的临时产物，执行器会删，日志里有）。
 */
function planCardsHtml() {
  const mode = state.info && state.info.dry_run ? '演练（不写盘）' : '真实写盘';
  return state.plans.map((item) => {
    const plan = item.plan;
    const disk = item.disk || diskOf(plan.disk_number);
    const letters = plan.drive_letters || {};
    const targets = state.wizard.targets.find((t) => Number(t.diskNumber) === Number(plan.disk_number));
    // 分区行以执行计划里的 partition_plan 为准（后端解析后的真实大小 + 盘符）；
    // 只有老后端没有这个字段时才退回按配置估算。
    const planned = Array.isArray(plan.partition_plan) ? plan.partition_plan : [];
    const info = (item.spec && item.spec.task_payload && item.spec.task_payload.partition_info) || {};
    const efiMb = Number(info.efi_size_mb) || 0;
    const cGb = Number(info.c_size_gb) || (targets ? Number(targets.sysGb) : 0);
    const dataGb = Number(info.data_size_gb) || (targets ? Number(targets.dataGb) : 0);
    const rows = planned.length
      ? planned.map((part) => partRowHtml(part.role, part.letter, part.size_display, part.file_system)).join('')
      : [
        partRowHtml('efi', letters.efi, efiMb > 0 ? `${Math.round(efiMb)} MB` : '设置默认', 'FAT32'),
        partRowHtml('windows', letters.windows, cGb > 0 ? `${cGb} GB` : '设置默认', 'NTFS'),
        partRowHtml('data', letters.data || letters.data0, dataGb > 0 ? `${dataGb} GB` : '剩余空间', String(info.data_fs || 'NTFS')),
      ].join('');
    return `<div class="pcard" data-disk="${esc(plan.disk_number)}">
      <div class="pcard-h">
        <span class="pn">磁盘 ${esc(plan.disk_number)}</span>
        <span class="pm">${esc(plan.model || (disk && disk.model) || '未知型号')} · ${esc(plan.size_display || fmtSize(disk ? disk.size_bytes : 0))}</span>
      </div>
      <div class="ptable">${rows}</div>
      <div class="pcard-f"><span>镜像 ${esc(versionText(targets))}</span><span class="pmode">${esc(mode)}</span></div>
      <details class="scriptbox" data-script-disk="${esc(plan.disk_number)}">
        <summary class="lnk" data-load-script="${esc(plan.disk_number)}">查看 diskpart 脚本</summary>
        <div class="scriptnote">只读预览：与真实执行器同一套生成器。分区验收不通过时，镜像与引导都不会执行。</div>
        <pre class="mono scriptpre" hidden></pre>
      </details>
    </div>`;
  }).join('');
}

/** 行尾的磁盘标记；单盘时写成“磁盘 2”，多盘时“磁盘 1 / 2”。 */
function diskTag(disks) {
  return `<span class="ntag">磁盘 ${disks.map((number) => esc(number)).join(' / ')}</span>`;
}

function stepConfirmHtml() {
  if (state.wizard.error) {
    return `<div class="wz-inner">
      <h3 class="wz-h">没能生成执行计划</h3>
      <div class="warnbox"><span class="wi">!</span><div>${esc(state.wizard.error)}</div></div>
      <button class="tbtn" id="planRetry">重新生成计划</button>
    </div>`;
  }
  if (!state.plans.length) {
    return '<div class="wz-inner"><div class="empty-hint">正在生成执行计划…</div></div>';
  }

  const lost = state.plans.map((item) => {
    const disk = item.disk || diskOf(item.plan.disk_number);
    return `<li><b>磁盘 ${esc(item.plan.disk_number)}</b>
      <span class="da-lost">${esc(lostPartitionsText(disk))}</span></li>`;
  }).join('');

  // 阻断项必须放在这里：按钮为什么禁用要写明，否则用户只看到一块禁用的按钮。
  const notes = [
    ...mergePlanNotes(state.plans, 'blockers').map((row) => `<div><b>阻断项</b>：${esc(row.body)}${diskTag(row.disks)}</div>`),
    ...mergePlanNotes(state.plans, 'warnings').map((row) => `<div>${esc(row.body)}${diskTag(row.disks)}</div>`),
  ].join('');
  const numbers = state.plans.map((item) => item.plan.disk_number).join(' / ');

  return `<div class="wz-inner wz-wide">
    <h3 class="wz-h">确认并开始</h3>
    <p class="wz-p">这些来自后端生成的执行计划，盘符是真实分配结果。</p>
    <div class="diskgrid">${planCardsHtml()}</div>
    <div class="warnbox warnline">
      <span class="wi">!</span>
      <span>磁盘 <b>${esc(numbers)}</b> 会被重新分区，盘上原有数据全部丢失，无法撤销</span>
      <details class="lossbox"><summary>查看将丢失的分区</summary><ul>${lost}</ul></details>
    </div>
    ${notes ? `<div class="warnbox"><span class="wi">!</span><div>${notes}</div></div>` : ''}
    <label class="ackbar${state.wizard.ack ? ' on' : ''}" id="ackBar">
      <input type="checkbox" id="ackBox" ${state.wizard.ack ? 'checked' : ''}>
      <span>我明白磁盘 <strong>${esc(numbers)}</strong> 上的分区会被全部删除，并且已经备份了需要的数据。</span>
    </label>
  </div>`;
}

function versionText(target) {
  if (!target) return '设置默认';
  if (target.imageLabel) return target.imageLabel;
  if (target.imageIndex) return `GHO 分区块 #${target.imageIndex}`;
  if (target.imagePath) return target.imagePath;
  return '设置默认';
}

function canStart() {
  if (!state.plans.length) return false;
  if (!state.plans.every((item) => item.plan.executable)) return false;
  return state.wizard.ack;
}

function bindStepConfirm() {
  const retry = $('#planRetry');
  if (retry) {
    retry.addEventListener('click', () => {
      state.wizard.error = '';
      void preparePlans();
    });
  }
  const ack = $('#ackBox');
  if (ack) {
    ack.addEventListener('change', () => {
      state.wizard.ack = ack.checked;
      const bar = $('#ackBar');
      if (bar) bar.classList.toggle('on', ack.checked);
      renderFoot();
    });
  }
  // 脚本默认折叠：展开时才去请求（首次成功后在 box.dataset.loaded 上记一笔）。
  for (const box of $$('#wzBody details.scriptbox')) {
    box.addEventListener('toggle', () => {
      if (box.open) void loadPartitionScript(box.querySelector('summary'));
    });
  }
}

async function loadPartitionScript(summary) {
  const diskNumber = Number(summary.dataset.loadScript);
  const box = summary.closest('.scriptbox');
  const pre = box ? box.querySelector('.scriptpre') : null;
  if (!pre || box.dataset.loaded) return;
  box.dataset.loaded = '1';
  pre.hidden = false;
  pre.textContent = '正在读取脚本…';
  try {
    const items = await api.partitionPreview(diskNumber);
    const item = (items || []).find((entry) => Number(entry.disk_number) === diskNumber) || (items || [])[0];
    pre.textContent = item ? item.script : '没有拿到脚本文本';
  } catch (exc) {
    // 失败时清掉标记：收起再展开可以重试。
    box.dataset.loaded = '';
    pre.textContent = exc instanceof ApiError ? exc.message : String(exc);
  }
}

/* ------------------------------------------------------------------ 计划与执行 */
export async function preparePlans() {
  state.wizard.step = 4;
  state.wizard.error = '';
  state.plans = [];
  renderInstall();
  try {
    const prepared = await api.prepare(selectionPayload());
    state.plans = prepared;
    pushLog(`执行计划已生成：${prepared.map((item) => `磁盘 ${item.plan.disk_number} ${item.plan.executable ? '可执行' : '有阻断项'}`).join('；')}`, '', 'prepare');
  } catch (exc) {
    state.wizard.error = exc instanceof ApiError ? exc.message : String(exc);
    pushLog(`生成计划失败：${state.wizard.error}`, 'err', 'prepare');
  }
  renderInstall();
}

async function startInstall() {
  if (!canStart()) return;
  const askConfirm = state.settings ? state.settings.confirmBeforeStart !== false : true;
  if (!(state.info && state.info.dry_run) && askConfirm) {
    const ok = await confirmDialog({
      title: '开始真实写盘',
      body: `<p>即将对磁盘 <b>${state.plans.map((item) => item.plan.disk_number).join(' / ')}</b> 重新分区并安装系统。</p>
        <p style="color:var(--danger);margin-top:8px">开始后无法撤销，请确认这些盘里没有需要保留的数据。</p>`,
      okText: '开始写盘',
    });
    if (!ok) return;
  }

  state.wizard.phase = 'running';
  state.wizard.submittedAt = new Date().toISOString();
  renderInstall();
  pushLog('提交安装任务…', '', 'install');
  let jobs;
  try {
    jobs = await api.submit();
  } catch (exc) {
    state.wizard.phase = 'failed';
    state.wizard.error = exc instanceof ApiError ? exc.message : String(exc);
    renderInstall();
    toast(`提交失败：${state.wizard.error}`, 'err');
    return;
  }
  state.wizard.jobIds = jobs.map((job) => job.id);
  for (const job of jobs) pushLog(`任务 ${job.id} 已排队（磁盘 ${job.spec ? job.spec.disk_number : '?'}）`, '', 'install');
  renderInstall();

  try {
    const confirmed = await api.confirm(state.wizard.jobIds);
    for (const job of confirmed) pushLog(`任务 ${job.id}：${jobStatusText(job.status)}`, '', 'install');
    toast('已开始安装');
  } catch (exc) {
    state.wizard.phase = 'failed';
    state.wizard.error = exc instanceof ApiError ? exc.message : String(exc);
    pushLog(`确认失败：${state.wizard.error}`, 'err', 'install');
    toast(`确认失败：${state.wizard.error}`, 'err');
  }
  renderInstall();
}

/* ------------------------------------------------------------------ 进度页 */
function planOfDisk(diskNumber) {
  const item = (state.plans || []).find((entry) => Number(entry.plan && entry.plan.disk_number) === Number(diskNumber));
  return item ? item.plan : null;
}

/** 从任务结果里取「分区验收」这一步（跑完才有；跑的过程中是 null）。 */
function layoutDetailsOf(job) {
  const steps = (job && job.result && job.result.steps) || [];
  const step = steps.find((item) => item && item.key === 'layout');
  if (!step) return null;
  return { status: step.status, message: step.message || '', verdict: layoutVerdict(step.details) };
}

function layoutPanel(info) {
  const verdict = info.verdict;
  const ok = info.status === 'ok' && verdict.ok;
  const rows = verdict.rows.map((row) => `<tr><td class="k">#${row.number} ${esc(row.label)}</td>
      <td>${esc(fmtSize(row.size))}${row.letter ? ` · ${esc(row.letter)}:` : ''}${row.fileSystem ? ` · ${esc(row.fileSystem)}` : ''}</td></tr>`).join('');
  return `<li class="verdict ${ok ? 'ok' : 'bad'}">
    <div class="vd-h">分区验收${ok ? '通过' : `未通过（${verdict.problems.length} 个问题）`}</div>
    <div class="vd-m">${esc(info.message)}</div>
    ${rows ? `<table class="vd-rows">${rows}</table>` : ''}
    ${verdict.problems.length ? `<div class="vd-p">${verdict.problems.map(esc).join('<br>')}</div>` : ''}
    ${verdict.warnings.length ? `<div class="vd-w">${verdict.warnings.map(esc).join('<br>')}</div>` : ''}
  </li>`;
}

function jobOf(jobId) {
  return state.jobs.get(jobId) || null;
}

function renderProgressHtml() {
  const { jobIds, phase } = state.wizard;
  if (!jobIds.length) {
    return `<div class="wz-inner"><div class="empty-hint">没有要显示的任务</div></div>`;
  }

  const blocks = jobIds.map((jobId) => {
    const job = jobOf(jobId);
    const diskNumber = job && job.spec ? job.spec.disk_number : '?';
    const progress = (job && job.progress) || state.wizard.progress.get(jobId) || {};
    const percent = Number(progress.percent) || 0;
    const completed = progress.completed || [];
    const current = progress.current;
    const stepEvents = state.wizard.steps.get(jobId) || new Map();
    // 阶段分组来自后端 plan.phases（老后端回退内置表），分区与装机在界面上就是两段。
    const plan = (job && job.plan) || planOfDisk(diskNumber);
    const groups = phaseGroups(plan);
    const stepStates = {};
    stepEvents.forEach((event, key) => { stepStates[key] = event.status; });
    const perPhase = progressByPhase(progress, groups, stepStates);
    const failedKey = Object.keys(stepStates).find((key) => stepStates[key] === 'failed') || null;
    const skippedGroups = new Set(failedKey ? skippedByFailure(failedKey, groups) : []);
    const layoutInfo = layoutDetailsOf(job);

    const stages = perPhase.map((phase, index) => {
      const head = `<li class="phase-head">
        <span class="ph-no">${index + 1}</span>
        <span class="ph-title">${esc(phase.title)}</span>
        <span class="ph-eta">${phase.completed.length}/${phase.total}${phase.status === 'done' ? ' ✓' : (phase.status === 'failed' ? ' ×' : '')}</span>
      </li>`;
      if (skippedGroups.has(phase.key)) {
        return head + `<li class="skip"><span class="sk">⊘</span>未执行（前一段没有通过验收）</li>`;
      }
      const items = phase.steps.map((key) => {
        const event = stepEvents.get(key);
        const done = completed.includes(key);
        const isCurrent = current === key || (event && event.status === 'running');
        const failed = event && event.status === 'failed';
        let cls = '';
        if (done) cls = 'done';
        else if (failed) cls = 'on';
        else if (isCurrent) cls = 'on';
        const mark = done ? '✓' : (failed ? '×' : (isCurrent ? '●' : ''));
        const status = event ? (event.status === 'running' ? '进行中' : (JOB_STATUS_TEXT[event.status] || event.status)) : '';
        return `<li class="sub ${cls}"><span class="sk">${mark}</span>${esc(STEP_LABELS[key] || key)}
          <span class="stt">${esc(status)}</span></li>`;
      }).join('');
      const panel = (phase.key === 'partition' && layoutInfo) ? layoutPanel(layoutInfo) : '';
      return head + items + panel;
    }).join('');

    const status = job ? job.status : 'pending';
    const badge = `<span class="badge ${esc(status)}">${esc(jobStatusText(status))}</span>`;
    const logLines = logCache.get(jobId) || [];

    return `<div class="prog" data-job="${esc(jobId)}">
      <div class="prow">
        <span class="pname">磁盘 ${esc(diskNumber)} 安装中</span>
        ${badge}
        <span class="peta">${percent}% · ${esc(progress.message || '')}</span>
      </div>
      <div class="track"><i style="width:${percent}%"></i></div>
      <ul class="stages">${stages}</ul>
      ${job && job.error ? `<div class="warnbox" style="margin-top:10px"><span class="wi">!</span><div>${esc(job.error)}</div></div>` : ''}
      <div class="plog" data-log="${esc(jobId)}">${logLines.map((line) => `<div><span class="t">·</span><span>${esc(line)}</span></div>`).join('')}</div>
    </div>`;
  }).join('');

  return `<div class="wz-inner wp">
    ${blocks}
    <div class="wz-startrow">
      ${phase === 'running' ? '<button class="tbtn" id="cancelBtn">取消剩余任务</button>' : ''}
      ${phase === 'done' ? '<button class="tbtn primary" id="finishBtn">完成，回到主页面</button>' : ''}
      ${phase === 'failed' ? '<button class="tbtn" id="retryBtn">重新开始</button>' : ''}
      <button class="tbtn" id="openLogBtn">查看任务日志</button>
      <span class="wz-startnote" id="progNote"></span>
    </div>
  </div>`;
}

function bindProgress() {
  const cancel = $('#cancelBtn');
  if (cancel) {
    cancel.addEventListener('click', async () => {
      const ok = await confirmDialog({
        title: '取消任务',
        body: '<p>已开始的写盘步骤会尽量收尾，未开始的步骤会跳过。已经分好的分区不会自动还原。</p>',
        okText: '取消任务',
      });
      if (!ok) return;
      for (const jobId of state.wizard.jobIds) {
        try {
          await api.cancel(jobId);
        } catch (exc) {
          pushLog(`取消失败：${exc.message || exc}`, 'err', 'install');
        }
      }
    });
  }
  const finish = $('#finishBtn');
  if (finish) {
    finish.addEventListener('click', () => {
      resetWizard();
      state.plans = [];
      if (typeof window.__sispGoMain === 'function') window.__sispGoMain();
    });
  }
  const retry = $('#retryBtn');
  if (retry) {
    retry.addEventListener('click', () => {
      resetWizard();
      state.plans = [];
      renderInstall();
    });
  }
  const openLog = $('#openLogBtn');
  if (openLog) openLog.addEventListener('click', () => { void showJobLogs(); });
}

async function showJobLogs() {
  const jobId = state.wizard.jobIds[state.wizard.jobIds.length - 1];
  if (!jobId) return;
  try {
    const data = await api.jobLog(jobId, 300);
    const lines = data.lines || [];
    // 没配 logDir 时后端不会有执行器日志，这里给一句人话，别弹一个空黑框。
    const text = lines.length ? lines.join('\n') : (data.message || '这个任务没有输出日志');
    const body = `<div class="codebox dark-code" style="max-height:340px">${esc(text)}</div>
      <div class="hint" style="margin-top:6px">${esc(data.path || '')}</div>`;
    openModal({ title: `任务日志 · ${jobId.slice(0, 8)}`, body, width: 720 });
  } catch (exc) {
    toast(`读日志失败：${exc.message || exc}`, 'err');
  }
}

/* ------------------------------------------------------------------ 事件与刷新 */
/** SSE 的 job.step 事件 → 记到向导状态里。 */
export function onStepEvent(payload) {
  const jobId = payload.job_id;
  if (!jobId || !state.wizard.jobIds.includes(jobId)) return false;
  const map = state.wizard.steps.get(jobId) || new Map();
  const key = payload.key;
  if (key) map.set(key, payload);
  state.wizard.steps.set(jobId, map);
  if (payload.progress) state.wizard.progress.set(jobId, payload.progress);
  return true;
}

/**
 * 轮询在跑的任务日志尾部，填进进度页的内联日志区。
 * 只改 .plog 的 DOM，避免整页重绘打断用户滚动。
 */
export async function pollJobLogs() {
  if (state.wizard.phase !== 'running') return;
  for (const jobId of state.wizard.jobIds) {
    const job = jobOf(jobId);
    if (!job || !['running', 'ok', 'failed'].includes(job.status)) continue;
    try {
      const data = await api.jobLog(jobId, 60);
      const lines = data.lines || [];
      const before = (logCache.get(jobId) || []).length;
      logCache.set(jobId, lines);
      if (lines.length !== before) {
        const box = $(`#wzBody .plog[data-log="${jobId}"]`);
        if (box) {
          box.innerHTML = lines.map((line) => `<div><span class="t">·</span><span>${esc(line)}</span></div>`).join('');
          box.scrollTop = box.scrollHeight;
        }
      }
    } catch (exc) {
      // 日志读不到不影响安装；下一轮再试
    }
  }
}

/** 任务状态推进到终态时，把向导切到“完成/失败”。 */
export function refreshPhase() {
  const jobs = state.wizard.jobIds.map(jobOf).filter(Boolean);
  if (!jobs.length) return false;
  const allDone = jobs.every((job) => ['ok', 'failed', 'skipped', 'cancelled'].includes(job.status));
  const anyFailed = jobs.some((job) => ['failed', 'cancelled'].includes(job.status));
  const previous = state.wizard.phase;
  if (allDone) state.wizard.phase = anyFailed ? 'failed' : 'done';
  else if (jobs.some((job) => job.status === 'running')) state.wizard.phase = 'running';
  return previous !== state.wizard.phase;
}

export function goStep(step) {
  const target = clamp(step, 1, 4);
  state.wizard.step = target;
  state.wizard.ack = false;
  if (target === 4) {
    void preparePlans();
    return;
  }
  if (target === 3) void loadImages();
  renderInstall();
}

export function next() {
  const { step, phase } = state.wizard;
  if (phase !== 'form') return;
  if (step >= 4) {
    void startInstall();
    return;
  }
  if (step === 1 && !state.wizard.targets.length) {
    toast('先勾选至少一块硬盘', 'err');
    return;
  }
  if (step === 2) {
    const bad = state.wizard.targets.find((target) => targetError(target));
    if (bad) {
      toast(`磁盘 ${bad.diskNumber}：${targetError(bad)}`, 'err');
      return;
    }
  }
  goStep(step + 1);
}

export function back() {
  if (state.wizard.phase !== 'form') return;
  if (state.wizard.step <= 1) return;
  state.wizard.step -= 1;
  renderInstall();
}

export function resetAll() {
  resetWizard();
  logCache.clear();
  state.plans = [];
  images.versions = [];
  images.defaultPath = '';
  images.error = '';
  renderInstall();
}
