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
  protectionText, clamp, MIN_SYS_GB, MIN_DATA_GB, toGb, fromGb, RESERVE_GB,
} from './derive.js';
import {
  state, diskOf, targetOf, targetError, targetRest, addTarget, removeTarget,
  selectionPayload, resetWizard, STEP_LABELS, JOB_STATUS_TEXT,
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

/* 进度页里每块盘的内联日志：来自 GET /api/jobs/{id}/log 的尾部，轮询刷新。 */
const logCache = new Map();

/* ------------------------------------------------------------------ 渲染入口 */
export function renderInstall() {
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
  if (step === 2) return '拖动中间的分隔条也能调大小，截图里的“剩余可用”会跟着变';
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
function stepSizeHtml() {
  const cards = state.wizard.targets.map((target) => {
    const disk = diskOf(target.diskNumber);
    if (!disk) return '';
    const total = Number(disk.size_bytes) || GB;
    const sysBytes = fromGb(target.sysGb);
    const dataBytes = fromGb(target.dataGb);
    const rest = Math.max(0, total - sysBytes - dataBytes);
    const error = targetError(target);
    const restGb = Math.max(0, toGb(rest));
    const sysPct = (sysBytes / total) * 100;
    const dataPct = (dataBytes / total) * 100;
    const freePct = Math.max(0, 100 - sysPct - dataPct);
    return `<div class="tgt" data-disk="${target.diskNumber}">
      <div class="tgt-h">
        <span class="tn">磁盘 ${target.diskNumber}</span>
        <span class="tm">${esc(disk.model || '')} · ${esc(disk.partition_style || '')}</span>
        <span class="trest">总容量 <b>${esc(fmtSize(total))}</b> · 剩余 <b class="${error ? 'warn' : ''}">${esc(rest > 0 ? fmtSize(rest) : '不足')}</b></span>
      </div>
      <div class="szrow">
        <span class="szname"><span class="lbadge" style="background:${fsColor('NTFS')}">C</span>系统分区（Windows）</span>
        <input type="number" min="${MIN_SYS_GB}" step="1" value="${esc(target.sysGb)}" data-size="sys" data-disk="${target.diskNumber}">
        <span class="unit">GB</span>
        <span class="szsub">NTFS · 会装系统，至少 ${MIN_SYS_GB} GB</span>
      </div>
      <div class="szrow">
        <span class="szname"><span class="lbadge" style="background:${fsColor('NTFS')}">D</span>数据分区</span>
        <input type="number" min="${MIN_DATA_GB}" step="1" value="${esc(target.dataGb)}" data-size="data" data-disk="${target.diskNumber}">
        <span class="unit">GB</span>
        <span class="szsub">${esc(state.settings && state.settings.defaultFs ? state.settings.defaultFs : 'NTFS')} · 剩余空间都给它</span>
      </div>
      <div class="wzbar" data-disk="${target.diskNumber}" tabindex="0" title="拖动分隔条调整系统分区大小">
        <span class="wseg sys" style="width:${sysPct.toFixed(2)}%"><span>系统 ${esc(target.sysGb)} GB</span></span>
        <span class="wseg dat" style="width:${dataPct.toFixed(2)}%"><span>数据 ${esc(target.dataGb)} GB</span></span>
        <span class="wseg free" style="width:${freePct.toFixed(2)}%"><span>${restGb >= 1 ? `未用 ${restGb.toFixed(0)} GB` : ''}</span></span>
        <span class="wgrip" style="left:${sysPct.toFixed(2)}%"></span>
      </div>
      <div class="tgt-f">
        <span class="${error ? 'errmsg' : 'szsub'}" data-err="${target.diskNumber}">${esc(error || `${efiText()} + 系统 ${target.sysGb} GB + 数据 ${target.dataGb} GB`) }</span>
        <span class="presets" data-disk="${target.diskNumber}">
          ${[100, 120, 150, 200].map((gb) => `<button data-preset="${gb}">${gb}</button>`).join('')}
        </span>
      </div>
    </div>`;
  }).join('');

  return `<div class="wz-inner">
    <h3 class="wz-h">分区与格式化</h3>
    <p class="wz-p">每块盘会被重新分区：EFI 分区 + 系统分区 + 数据分区，GPT + UEFI。数字只影响三个分区的大小，盘上原有数据会全部丢失。</p>
    ${cards || '<div class="empty-hint">还没有选盘</div>'}
  </div>`;
}

function bindStepSize() {
  $$('#wzBody input[data-size]').forEach((input) => {
    input.addEventListener('input', () => {
      const target = targetOf(Number(input.dataset.disk));
      if (!target) return;
      const value = Number(input.value);
      if (input.dataset.size === 'sys') target.sysGb = Number.isFinite(value) ? value : 0;
      else target.dataGb = Number.isFinite(value) ? value : 0;
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
      renderInstall();
    });
  });

  $$('#wzBody .presets button').forEach((button) => {
    button.addEventListener('click', () => {
      const target = targetOf(Number(button.parentElement.dataset.disk));
      if (!target) return;
      const disk = diskOf(target.diskNumber);
      const totalGb = ((Number(disk && disk.size_bytes) || 0) / GB);
      target.sysGb = clamp(Number(button.dataset.preset), MIN_SYS_GB, Math.max(MIN_SYS_GB, totalGb - MIN_DATA_GB - RESERVE_GB));
      target.dataGb = Math.max(MIN_DATA_GB, Math.round((totalGb - target.sysGb - RESERVE_GB) * 10) / 10);
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
  const sysBytes = fromGb(target.sysGb);
  const dataBytes = fromGb(target.dataGb);
  const sysPct = clamp((sysBytes / total) * 100, 0, 100);
  const dataPct = clamp((dataBytes / total) * 100, 0, 100 - sysPct);
  const freePct = Math.max(0, 100 - sysPct - dataPct);
  const sysSeg = card.querySelector('.wseg.sys');
  const dataSeg = card.querySelector('.wseg.dat');
  const freeSeg = card.querySelector('.wseg.free');
  const grip = card.querySelector('.wgrip');
  if (sysSeg) { sysSeg.style.width = `${sysPct}%`; sysSeg.firstElementChild.textContent = `系统 ${Math.round(target.sysGb)} GB`; }
  if (dataSeg) { dataSeg.style.width = `${dataPct}%`; dataSeg.firstElementChild.textContent = `数据 ${Math.round(target.dataGb)} GB`; }
  if (freeSeg) {
    freeSeg.style.width = `${freePct}%`;
    freeSeg.firstElementChild.textContent = freePct > 6 ? `未用 ${Math.max(0, targetRest(target)) / GB >= 1 ? `${Math.round(targetRest(target) / GB)} GB` : ''}` : '';
  }
  if (grip) grip.style.left = `${sysPct}%`;
  const error = targetError(target);
  const box = card.querySelector(`[data-err="${target.diskNumber}"]`);
  if (box) {
    box.className = error ? 'errmsg' : 'szsub';
    box.textContent = error || `${efiText()} + 系统 ${Math.round(target.sysGb)} GB + 数据 ${Math.round(target.dataGb)} GB`;
  }
  const rest = card.querySelector('.trest b');
  if (rest) {
    const spare = targetRest(target);
    rest.textContent = spare > 0 ? fmtSize(spare) : '不足';
    rest.classList.toggle('warn', spare <= 0);
  }
}

function bindDrag(bar) {
  const target = targetOf(Number(bar.dataset.disk));
  const disk = diskOf(bar.dataset.disk);
  if (!target || !disk) return;
  const total = Number(disk.size_bytes) || GB;
  const drag = (clientX) => {
    const rect = bar.getBoundingClientRect();
    if (!rect.width) return;
    const ratio = clamp((clientX - rect.left) / rect.width, 0, 1);
    const maxSys = Math.max(MIN_SYS_GB, toGb(total) - MIN_DATA_GB - RESERVE_GB);
    const sysGb = clamp(Math.round(toGb(total) * ratio), MIN_SYS_GB, maxSys);
    target.sysGb = sysGb;
    target.dataGb = Math.max(MIN_DATA_GB, Math.round((toGb(total) - sysGb - RESERVE_GB) * 10) / 10);
    const input = bar.parentElement.querySelector('input[data-size="sys"]');
    const dataInput = bar.parentElement.querySelector('input[data-size="data"]');
    if (input) input.value = sysGb;
    if (dataInput) dataInput.value = target.dataGb;
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
    bar.classList.add('drag');
    drag(event.clientX);
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', stop);
    event.preventDefault();
  });
  bar.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const step = event.key === 'ArrowLeft' ? -1 : 1;
    target.sysGb = clamp(Math.round(target.sysGb) + step, MIN_SYS_GB, Math.max(MIN_SYS_GB, Math.round(toGb(total)) - MIN_DATA_GB));
    target.dataGb = Math.max(MIN_DATA_GB, Math.round((toGb(total) - target.sysGb) * 10) / 10);
    renderInstall();
    event.preventDefault();
  });
}

/* ---- 第 3 步 ---- */
function stepVersionHtml() {
  const rows = state.wizard.targets.map((target) => {
    const disk = diskOf(target.diskNumber);
    const fileOptions = (images.listing && images.listing.images) || [];
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
  return `<div class="wz-inner">
    <h3 class="wz-h">系统版本</h3>
    <p class="wz-p">版本列表是从镜像文件里用 dism 读出来的真实索引，不是写死的 Win10/Win11 两行。不选就沿用配置文件里的设置。</p>
    <div class="selbar">
      镜像目录 <b>${esc(dir || '（未设置）')}</b>
      <button class="lnk" id="imgReload">重新读取</button>
      ${images.loading ? '<span class="szsub">正在读取…</span>' : ''}
    </div>
    ${rows || '<div class="empty-hint">还没有选盘</div>'}
    ${images.listing && images.listing.images && images.listing.images.length === 0
      ? `<div class="warnbox"><span class="wi">!</span><div>镜像目录里没找到 WIM/ESD/SWM/ISO 文件。可以先去「设置 → 系统文件 → 镜像目录」指一个目录。</div></div>`
      : ''}
  </div>`;
}

function defaultImagePath() {
  const files = (images.listing && images.listing.images) || [];
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
    // 分区大小以执行计划里的真实值为准（界面输入只是入参，后端会再叠一层默认值）
    const info = (item.spec && item.spec.config_payload && item.spec.config_payload.partition_info) || {};
    const efiMb = Number(info.efi_size_mb) || 0;
    const cGb = Number(info.c_size_gb) || (targets ? Number(targets.sysGb) : 0);
    const dataGb = Number(info.data_size_gb) || (targets ? Number(targets.dataGb) : 0);
    const dataFs = String(info.data_fs || (state.settings && state.settings.defaultFs) || 'NTFS');
    const letterText = Object.keys(ROLE_TEXT)
      .filter((role) => letters[role])
      .map((role) => `${ROLE_TEXT[role]} ${letters[role]}:`)
      .join(' / ') || '由后端分配';
    return `<div class="sum" style="margin-bottom:10px">
      <table>
        <tr><td class="k">磁盘</td><td class="v">磁盘 ${esc(plan.disk_number)} · ${esc(plan.model || '')}</td></tr>
        <tr><td class="k">容量</td><td>${esc(plan.size_display || fmtSize(disk ? disk.size_bytes : 0))}</td></tr>
        <tr><td class="k">EFI 分区</td><td>${efiMb > 0 ? `${Math.round(efiMb)} MB` : '配置文件默认'} · FAT32</td></tr>
        <tr><td class="k">系统分区</td><td>${cGb > 0 ? `${cGb} GB` : '配置文件默认'} · NTFS</td></tr>
        <tr><td class="k">数据分区</td><td>${dataGb > 0 ? `${dataGb} GB` : '剩余空间'} · ${esc(dataFs)}</td></tr>
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
    const body = `<div class="codebox dark-code" style="max-height:340px">${esc((data.lines || []).join('\n'))}</div>
      <div class="hint" style="margin-top:6px">${esc(data.path || data.message || '')}</div>`;
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
