/**
 * 系统安装：四步向导（选盘 → 分区 → 版本 → 确认）+ 进度页。
 *
 * 写盘链路只有一条，和 CLI 完全相同：
 *   POST /api/prepare  →  POST /api/submit  →  POST /api/confirm
 * 界面不自己拼 diskpart/dism 命令，也不自己算盘符：第 4 步显示的盘符来自
 * `plan.drive_letters`，第 2 步的大小只是给 `prepare` 的入参，后端
 * `_check_partition_fit` 还会再验一次。
 */

import { api, ApiError } from './api.js';
import { $, $$, esc, on } from './dom.js';
import {
  fmtSize, segments, fsColor, isSelectable,
  protectionText, clamp, MIN_SYS_GB, MIN_DATA_GB, MIN_EXTRA_GB, MAX_EXTRAS,
  toGb, fromGb, extraColor, extraLetter, fitLayout, lastPartitionIsAuto,
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
  listing: null,     // {directory, images[], exists}
  probe: null,       // {path, name, kind, indexes[]}
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
  if (step === 4) {
    if (state.wizard.error) reason = reason || `生成计划失败：${state.wizard.error}`;
    else if (!state.plans.length) reason = reason || '正在生成执行计划…';
    else if (!state.plans.every((item) => item.plan.executable)) reason = reason || '有阻断项，先解决再开始';
  }
  if (step === 4 && !state.wizard.ack) reason = reason || '请勾选“我明白这些分区会被删除”';
  if (step === 4 && needsTypedConfirm() && !confirmTextOk()) reason = reason || '请按提示输入确认文本';

  next.disabled = Boolean(reason);
  next.title = reason || '';
  hint.textContent = reason || hintText(step);
  hint.classList.toggle('warn', Boolean(reason));
}

function hintText(step) {
  if (step === 1) return `${state.disks.filter(isSelectable).length} 块盘可安装 · 系统盘和只读盘已置灰`;
  if (step === 2) return '拖分隔条调大小：最后一个数据分区永远吃满剩余，剩下多少就给它多少';
  if (step === 3) return images.loading ? '正在读取镜像索引…' : '版本来自镜像文件本身，不是写死的列表';
  if (step === 4) return state.info && state.info.dry_run
    ? '当前是演练模式：点开始只会走一遍计划，不会真的写盘'
    : '点开始后先做五道安全检查，任何一道不通过都会拒绝写盘';
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
    const right = selectable
      ? `<span>可安装</span><b>${esc(fmtSize(disk.size_bytes))}</b>`
      : `<span>${esc(protectionText(disk) || '不可操作')}</span><b>${esc(fmtSize(disk.size_bytes))}</b>`;
    return `<button class="${cls.join(' ')}" data-disk="${number}" ${selectable ? '' : 'disabled'}>
      <span class="cb"></span>
      <span class="dmain">
        <span class="dt">磁盘 ${number} · ${esc(disk.model || '未知型号')}
          <span class="tag${selectable ? '' : ' no'}">${selectable ? '可选' : '已锁定'}</span>
        </span>
        <span class="dm">${esc(disk.partition_style || '')} · ${esc(disk.bus_type || '')} · ${(disk.partitions || []).length} 个分区</span>
        <span class="dbar">${bars}</span>
      </span>
      <span class="dright">${right}</span>
    </button>`;
  }).join('');

  return `<div class="wz-inner">
    <h3 class="wz-h">选择要装系统的硬盘</h3>
    <p class="wz-p">可以一次选多块盘。系统盘、只读盘、配置里排除的盘会置灰；真实写盘时后端还会再核一次磁盘身份（UniqueId / 序列号）。</p>
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

/** 第 2 步的 EFI 提示：优先用执行计划里真实的 EFI 大小（来自配置文件）。 */
function efiText() {
  const item = state.plans && state.plans.find((plan) => plan.spec && plan.spec.config_payload);
  const info = item && item.spec.config_payload.partition_info;
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
        <span class="szsub">${esc(dataFs())} · 剩余容量自动占满</span>
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
  return lastPartitionIsAuto(Array.isArray(target.extras) ? target.extras : []);
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
function stepVersionHtml() {
  const rows = state.wizard.targets.map((target) => {
    const disk = diskOf(target.diskNumber);
    // 装机只接受 dism 能 /Apply-Image 的格式；ISO 需要先取出 sources\install.wim。
    const fileOptions = ((images.listing && images.listing.images) || [])
      .filter((file) => file.kind !== 'iso');
    const options = ['<option value="">配置文件里的镜像（不改）</option>']
      .concat(fileOptions.map((file) => `<option value="${esc(file.path)}"${file.path === (target.imagePath || defaultImagePath()) ? ' selected' : ''}>${esc(file.name)} · ${esc(file.size_display || fmtSize(file.size_bytes))}</option>`))
      .join('');
    const probing = images.probe && images.probe.path === (target.imagePath || defaultImagePath());
    const chips = probing && images.probe.indexes
      ? images.probe.indexes.map((entry) => `<button class="vchip${Number(target.imageIndex) === Number(entry.index) ? ' on' : ''}"
          data-disk="${target.diskNumber}" data-index="${entry.index}" title="${esc(entry.description || entry.name || '')}">
          ${esc(entry.name || `索引 ${entry.index}`)}<em>#${entry.index}</em></button>`).join('')
      : '';
    const hint = images.error
      ? `<span class="errmsg">${esc(images.error)}</span>`
      : (probing ? '' : `<span class="szsub">${images.loading ? '读取中…' : '点“检测版本”读取'}</span>`);
    return `<div class="vrow">
      <span class="vrow-n">磁盘 ${target.diskNumber}</span>
      <select class="sel" data-image="${target.diskNumber}">${options}</select>
      <span class="vchips">${chips || hint}</span>
    </div>`;
  }).join('');

  const dir = images.listing ? images.listing.directory : (state.settings ? state.settings.sysIsoDir : '');
  const isoCount = ((images.listing && images.listing.images) || [])
    .filter((file) => file.kind === 'iso').length;
  return `<div class="wz-inner">
    <h3 class="wz-h">系统版本</h3>
    <p class="wz-p">版本列表是从镜像文件里用 dism 读出来的真实索引，不是写死的 Win10/Win11 两行。不选就沿用配置文件里的设置。</p>
    <div class="selbar">
      镜像目录 <b>${esc(dir || '（未设置）')}</b>
      <button class="lnk" id="imgReload">重新读取</button>
      ${images.loading ? '<span class="szsub">正在读取…</span>' : ''}
    </div>
    ${isoCount ? `<div class="szsub" style="margin:-2px 0 6px">目录里还有 ${isoCount} 个 ISO 没有列出：ISO 要先取出 sources\\install.wim 才能装机。</div>` : ''}
    ${rows || '<div class="empty-hint">还没有选盘</div>'}
    ${images.listing && images.listing.images && images.listing.images.length === 0
      ? `<div class="warnbox"><span class="wi">!</span><div>镜像目录里没找到 WIM/ESD/SWM/ISO 文件。可以先去「设置 → 系统文件 → 镜像目录」指一个目录。</div></div>`
      : ''}
  </div>`;
}

function defaultImagePath() {
  const files = ((images.listing && images.listing.images) || []).filter((file) => file.kind !== 'iso');
  const preferred = (state.settings && state.settings.sysWim) || '';
  const hit = files.find((file) => file.path === preferred);
  return hit ? hit.path : (files.length ? files[0].path : '');
}

function bindStepVersion() {
  $$('#wzBody select[data-image]').forEach((select) => {
    select.addEventListener('change', () => {
      const target = targetOf(Number(select.dataset.image));
      if (!target) return;
      target.imagePath = select.value;
      target.imageIndex = null;
      target.imageLabel = '';
      state.wizard.ack = false;
      void probeImage(select.value);
    });
  });
  $$('#wzBody .vchip').forEach((chip) => {
    chip.addEventListener('click', () => {
      const target = targetOf(Number(chip.dataset.disk));
      if (!target) return;
      target.imageIndex = Number(chip.dataset.index);
      target.imageLabel = chip.textContent.trim();
      state.wizard.ack = false;
      renderInstall();
    });
  });
  const reload = $('#imgReload');
  if (reload) {
    reload.addEventListener('click', () => {
      images.listing = null;
      images.probe = null;
      void loadImages();
    });
  }
}

/* ------------------------------------------------------------------ 镜像探测 */
export async function loadImages() {
  if (images.loading) return;
  images.loading = true;
  images.error = '';
  if (state.view === 'install' && state.wizard.step === 3) renderInstall();
  try {
    images.listing = await api.images({});
  } catch (exc) {
    images.listing = { directory: '', images: [], exists: false };
    images.error = exc instanceof ApiError ? exc.message : String(exc);
  } finally {
    images.loading = false;
  }
  const path = defaultImagePath();
  if (path) await probeImage(path);
  else if (state.view === 'install') renderInstall();
}

async function probeImage(path) {
  if (!path) {
    images.probe = null;
    if (state.view === 'install') renderInstall();
    return;
  }
  images.loading = true;
  images.error = '';
  if (state.view === 'install') renderInstall();
  try {
    images.probe = await api.images({ path });
  } catch (exc) {
    images.probe = null;
    images.error = exc instanceof ApiError ? exc.message : String(exc);
  } finally {
    images.loading = false;
  }
  for (const target of state.wizard.targets) {
    if (!target.imagePath) target.imagePath = path;
  }
  if (state.view === 'install') renderInstall();
}

/* ---- 第 4 步 ---- */
function needsTypedConfirm() {
  return Boolean(state.settings && state.settings.typeToConfirm) && !(state.info && state.info.dry_run);
}

function acceptedTexts(plan) {
  const number = Number(plan.disk_number);
  const model = String((plan.identity && plan.identity.model) || '').trim();
  const words = [String(number), `disk ${number}`, `disk${number}`, `硬盘${number}`, `硬盘 ${number}`];
  if (model) words.push(model);
  return words.map((word) => word.replace(/\s+/g, ' ').trim().toLowerCase());
}

function confirmTextOk() {
  const text = ($('#confirmText') && $('#confirmText').value ? $('#confirmText').value : '')
    .replace(/\s+/g, ' ').trim().toLowerCase();
  if (!text) return false;
  return state.plans.every((item) => acceptedTexts(item.plan).includes(text));
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

  const summaries = state.plans.map((item) => {
    const plan = item.plan;
    const disk = item.disk || diskOf(plan.disk_number);
    const letters = plan.drive_letters || {};
    const targets = state.wizard.targets.find((t) => Number(t.diskNumber) === Number(plan.disk_number));
    // 分区行以执行计划里的 partition_plan 为准（后端解析后的真实大小 + 盘符）；
    // 只有老后端没有这个字段时才退回按配置估算。
    const planned = Array.isArray(plan.partition_plan) ? plan.partition_plan : [];
    const info = (item.spec && item.spec.config_payload && item.spec.config_payload.partition_info) || {};
    const efiMb = Number(info.efi_size_mb) || 0;
    const cGb = Number(info.c_size_gb) || (targets ? Number(targets.sysGb) : 0);
    const dataGb = Number(info.data_size_gb) || (targets ? Number(targets.dataGb) : 0);
    const dataFs = String(info.data_fs || (state.settings && state.settings.defaultFs) || 'NTFS');
    const partRows = planned.length
      ? planned.map((part) => `<tr><td class="k">${esc(roleLabel(part.role))}</td><td>${esc(part.size_display || '')} · ${esc(part.file_system || '')}${part.letter ? ` · 盘符 ${esc(part.letter)}:` : ''}</td></tr>`).join('')
      : [
        `<tr><td class="k">EFI 分区</td><td>${efiMb > 0 ? `${Math.round(efiMb)} MB` : '配置文件默认'} · FAT32</td></tr>`,
        `<tr><td class="k">系统分区</td><td>${cGb > 0 ? `${cGb} GB` : '配置文件默认'} · NTFS</td></tr>`,
        `<tr><td class="k">数据分区</td><td>${dataGb > 0 ? `${dataGb} GB` : '剩余空间'} · ${esc(dataFs)}</td></tr>`,
      ].join('');
    // convert gpt 会在盘首自动塞一个约 16 MB 的 MSR（Windows 的行为，不是我们的命令）。
    // 执行器建分区前会按 GPT 类型找到它并删掉，这里如实说明，免得用户看 diskpart 日志时困惑。
    const msrRow = '<tr><td class="k">MSR 分区</td><td>约 16 MB · 由 <code>convert gpt</code> 自动生成，执行时删除，最终布局不留</td></tr>';
    const letterText = Object.keys(letters)
      .filter((role) => letters[role])
      .map((role) => `${roleLabel(role)} ${letters[role]}:`)
      .join(' / ') || '由后端分配';
    return `<div class="sum" style="margin-bottom:10px">
      <table>
        <tr><td class="k">磁盘</td><td class="v">磁盘 ${esc(plan.disk_number)} · ${esc(plan.model || '')}</td></tr>
        <tr><td class="k">容量</td><td>${esc(plan.size_display || fmtSize(disk ? disk.size_bytes : 0))}</td></tr>
        ${partRows}
        ${msrRow}
        <tr><td class="k">计划盘符</td><td>${esc(letterText)}</td></tr>
        <tr><td class="k">版本</td><td>${esc(versionText(targets))}</td></tr>
        <tr><td class="k">模式</td><td>${plan.dry_run ? '演练（不写盘）' : '<b style="color:var(--danger)">真实写盘</b>'}</td></tr>
      </table>
    </div>`;
  }).join('');

  const lost = state.plans.map((item) => {
    const disk = item.disk || diskOf(item.plan.disk_number);
    return `<li><b>磁盘 ${esc(item.plan.disk_number)}</b>
      <span class="da-lost">${esc(lostPartitionsText(disk))}</span></li>`;
  }).join('');

  const blockers = state.plans.flatMap((item) => (item.plan.blockers || [])
    .map((text) => `磁盘 ${item.plan.disk_number}：${text}`));
  const warnings = state.plans.flatMap((item) => (item.plan.warnings || [])
    .map((text) => `磁盘 ${item.plan.disk_number}：${text}`));

  const checks = state.plans.length ? (state.plans[0].plan.checks || []) : [];

  return `<div class="wz-inner">
    <h3 class="wz-h">确认并开始</h3>
    <p class="wz-p">下面这些都来自后端生成的执行计划，盘符是真实分配结果。点“开始安装”后，会先把任务排队，再做确认。</p>
    ${summaries}
    <div class="dangeralert">
      <span class="da-ico">!</span>
      <div class="da-body">
        <div class="da-t">这 ${state.plans.length} 块盘上的分区会被<em>全部删除</em>并重新分区</div>
        <div class="da-s">分区表、卷标、盘上的全部文件都会消失，<strong>无法撤销</strong>。确认这些盘里没有还要用的数据。</div>
        <ul class="da-list">${lost}</ul>
      </div>
    </div>
    ${blockers.length ? `<div class="warnbox" style="margin-top:12px"><span class="wi">!</span>
      <div><b>阻断项（必须先解决）</b><br>${blockers.map(esc).join('<br>')}</div></div>` : ''}
    ${warnings.length ? `<div class="warnbox" style="margin-top:12px"><span class="wi">!</span>
      <div>${warnings.map(esc).join('<br>')}</div></div>` : ''}
    ${checks.length ? `<div class="sum" style="margin-top:12px"><table>
      <tr><td class="k">计划检查</td><td>${checks.map((check) => `${check.passed ? '✓' : '×'} ${esc(check.label || check.name || '')}`).join(' · ')}</td></tr>
    </table></div>` : ''}
    <label class="ackbar${state.wizard.ack ? ' on' : ''}" id="ackBar">
      <input type="checkbox" id="ackBox" ${state.wizard.ack ? 'checked' : ''}>
      <span>我明白磁盘 <strong>${state.plans.map((item) => item.plan.disk_number).join(' / ')}</strong> 上的分区会被全部删除，并且已经备份了需要的数据。</span>
    </label>
    ${needsTypedConfirm() ? `<div class="fieldrow">
      <label>输入确认：</label>
      <input class="confirm" id="confirmText" placeholder="盘号或型号" autocomplete="off">
      <span class="hint">设置里开了“危险操作需输入确认”</span>
    </div>` : ''}
    <div class="wz-startrow">
      <button class="tbtn danger startbtn" id="startBtn" ${canStart() ? '' : 'disabled'}>开始安装</button>
      <span class="wz-startnote">${state.info && state.info.dry_run ? '演练模式：不会真的写盘' : '真实写盘 · 中途断电会留下半成品'}</span>
    </div>
  </div>`;
}

function versionText(target) {
  if (!target) return '配置文件默认';
  if (target.imageLabel) return target.imageLabel;
  if (target.imageIndex) return `索引 ${target.imageIndex}`;
  if (target.imagePath) return target.imagePath;
  return '配置文件默认';
}

function canStart() {
  if (!state.plans.length) return false;
  if (!state.plans.every((item) => item.plan.executable)) return false;
  if (!state.wizard.ack) return false;
  if (needsTypedConfirm() && !confirmTextOk()) return false;
  return true;
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
      const start = $('#startBtn');
      if (start) start.disabled = !canStart();
      renderFoot();
    });
  }
  const text = $('#confirmText');
  if (text) {
    text.addEventListener('input', () => {
      const start = $('#startBtn');
      if (start) start.disabled = !canStart();
      renderFoot();
    });
  }
  const start = $('#startBtn');
  if (start) start.addEventListener('click', () => { void startInstall(); });
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
  if (!(state.info && state.info.dry_run)) {
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
    const text = ($('#confirmText') && $('#confirmText').value) || undefined;
    const confirmed = await api.confirm(state.wizard.jobIds, text);
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
    const keys = Object.keys(STEP_LABELS);
    const stages = keys.map((key) => {
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
      return `<li class="${cls}"><span class="sk">${mark}</span>${esc(STEP_LABELS[key])}
        <span class="stt">${esc(status)}</span></li>`;
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
  images.probe = null;
  renderInstall();
}
