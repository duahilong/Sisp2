/**
 * 全局状态：一份可变对象 + 一个极简订阅表。
 *
 * 为什么不用别的框架：整个界面只有三个视图、一次只跑一个任务，
 * 手工 render() 比引入响应式框架更好排查问题，也避免打包体积膨胀。
 * 约定：**只有 views 里的 render*() 读 state，事件处理只改 state 然后 notify()**。
 */

import {
  clamp, freeBytes, isSelectable, checkLayout,
  MIN_DATA_GB, MIN_EXTRA_GB, MAX_EXTRAS, RESERVE_GB, toGb,
} from './derive.js';

export const STEP_LABELS = {
  efi: '清盘并重建分区表',
  windows: '创建系统分区 C:',
  data: '创建数据分区 D:',
  image: '应用系统镜像',
  boot: '写入 UEFI 引导',
  verify: '校验分区与引导',
};

export const JOB_STATUS_TEXT = {
  pending: '排队中',
  waiting_confirmation: '待确认',
  running: '进行中',
  ok: '已完成',
  failed: '失败',
  skipped: '已跳过',
  cancelled: '已取消',
};

const listeners = new Set();

export const state = {
  info: null,
  session: null,
  disks: [],
  selectedDisk: null,
  selectedPart: null,

  settings: null,
  settingsMeta: null,
  draft: {},          // 设置页里改过但还没保存的键
  probes: {},         // key → ProbeResult

  jobs: new Map(),
  plans: [],          // POST /api/prepare 的结果
  view: 'main',
  boot: null,         // /api/boot/report 的最近一次结果

  wizard: {
    step: 1,
    targets: [],      // {diskNumber, sysGb, dataGb, imagePath, imageIndex, imageLabel}
    ack: false,
    phase: 'form',    // form | running | done | failed
    jobIds: [],
    progress: new Map(),  // jobId → progress
    steps: new Map(),     // jobId → 已收到的 step 事件
    submittedAt: null,
    error: '',
  },
};

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function notify() {
  for (const fn of listeners) {
    try {
      fn();
    } catch (exc) {
      console.error('渲染失败', exc);
    }
  }
}

/* ------------------------------------------------------------------ 磁盘 */
export function setDisks(disks) {
  state.disks = Array.isArray(disks) ? disks : [];
  const numbers = state.disks.map((disk) => Number(disk.disk_number));
  if (!numbers.includes(state.selectedDisk)) {
    const preferred = state.disks.find(isSelectable) || state.disks[0];
    state.selectedDisk = preferred ? Number(preferred.disk_number) : null;
    state.selectedPart = null;
  }
  const disk = diskOf(state.selectedDisk);
  const partNumbers = ((disk && disk.partitions) || []).map((part) => Number(part.partition_number));
  if (!partNumbers.includes(Number(state.selectedPart))) state.selectedPart = null;
}

export function diskOf(number) {
  return state.disks.find((disk) => Number(disk.disk_number) === Number(number)) || null;
}

export function selectedDisk() {
  return diskOf(state.selectedDisk);
}

export function selectedPart() {
  const disk = selectedDisk();
  if (!disk) return null;
  return (disk.partitions || []).find((part) => Number(part.partition_number) === Number(state.selectedPart)) || null;
}

export function selectDisk(number) {
  state.selectedDisk = Number(number);
  state.selectedPart = null;
}

export function selectPart(diskNumber, partNumber) {
  state.selectedDisk = Number(diskNumber);
  state.selectedPart = partNumber === null || partNumber === undefined ? null : Number(partNumber);
}

export function totalPartitions() {
  return state.disks.reduce((sum, disk) => sum + ((disk.partitions || []).length), 0);
}

/* ------------------------------------------------------------------ 任务 */
export function setJobs(jobs) {
  state.jobs = new Map();
  for (const job of jobs || []) upsertJob(job, false);
}

export function upsertJob(job, emit = true) {
  if (!job || !job.id) return;
  const previous = state.jobs.get(job.id);
  const merged = previous ? { ...previous, ...job } : job;
  if (previous && previous.progress && !job.progress) merged.progress = previous.progress;
  state.jobs.set(job.id, merged);
  if (state.wizard.jobIds.includes(job.id) && merged.progress) {
    state.wizard.progress.set(job.id, merged.progress);
  }
  if (emit) notify();
}

export function jobsArray() {
  return Array.from(state.jobs.values());
}

export function activeJobs() {
  return jobsArray().filter((job) => job.status === 'running' || job.status === 'pending' || job.status === 'waiting_confirmation');
}

/* ------------------------------------------------------------------ 设置 */
export function draftValue(key) {
  if (Object.prototype.hasOwnProperty.call(state.draft, key)) return state.draft[key];
  return state.settings ? state.settings[key] : undefined;
}

export function setDraft(key, value) {
  state.draft[key] = value;
}

export function dirtyCount() {
  return Object.keys(state.draft).length;
}

export function clearDraft() {
  state.draft = {};
}

/* ------------------------------------------------------------------ 向导 */
export function resetWizard(keepStep = false) {
  state.wizard = {
    step: keepStep ? state.wizard.step : 1,
    targets: [],
    ack: false,
    phase: 'form',
    jobIds: [],
    progress: new Map(),
    steps: new Map(),
    submittedAt: null,
    error: '',
  };
}

export function targetOf(diskNumber) {
  return state.wizard.targets.find((target) => Number(target.diskNumber) === Number(diskNumber)) || null;
}

export function defaultSizes(disk) {
  const total = Number(disk && disk.size_bytes) || 0;
  const totalGb = total / (1024 ** 3);
  const wanted = Math.round(Number((state.settings && state.settings.sysDefaultGb) || 120));
  const sys = clamp(wanted, 60, Math.max(60, Math.floor(totalGb - 20)));
  const dataDefault = state.settings ? state.settings.dataDefaultGb : 'auto';
  // 留 1 GB 给 EFI 分区和对齐，否则默认值会把盘算到最后一字节，后端直接判容量不足。
  let data = Math.round((totalGb - sys - RESERVE_GB) * 10) / 10;
  if (dataDefault !== 'auto' && Number(dataDefault) > 0) {
    data = clamp(Number(dataDefault), 20, Math.max(20, Math.floor(totalGb - sys - RESERVE_GB)));
  }
  data = Math.max(20, data);
  return { sysGb: sys, dataGb: data };
}

export function addTarget(diskNumber) {
  const disk = diskOf(diskNumber);
  if (!disk || targetOf(diskNumber)) return;
  const sizes = defaultSizes(disk);
  state.wizard.targets.push({
    diskNumber: Number(diskNumber),
    sysGb: sizes.sysGb,
    dataGb: sizes.dataGb,
    extras: [],        // 额外数据分区（E）：[{sizeGb, fs}]，最多 MAX_EXTRAS 个
    imagePath: '',
    imageIndex: null,
    imageLabel: '',
  });
  state.wizard.targets.sort((a, b) => a.diskNumber - b.diskNumber);
  state.wizard.ack = false;
}

export function removeTarget(diskNumber) {
  state.wizard.targets = state.wizard.targets.filter((target) => Number(target.diskNumber) !== Number(diskNumber));
  state.wizard.ack = false;
}

export function targetError(target) {
  const disk = diskOf(target.diskNumber);
  if (!disk) return '硬盘已不在列表里';
  return checkLayout(disk.size_bytes, target.sysGb, target.dataGb, target.extras || []);
}

/** 数据分区（D 与 E）统一使用的文件系统。
 *
 * 跟随设置里的 `defaultFs`，但只允许 NTFS / exFAT（后端拒绝额外分区的 FAT32，
 * 大容量 FAT32 在 diskpart 里本来就格不了），其它值一律回落 NTFS。
 * 界面上不给 E 改文件系统，保证它和 D 一致——也避免用户把一个分区搞成 FAT32。
 */
export function dataFs() {
  const raw = String((state.settings && state.settings.defaultFs) || 'NTFS').trim().toUpperCase();
  return raw === 'EXFAT' ? 'exFAT' : 'NTFS';
}

/** 加一个数据分区（E）。默认与 D 平分剩余容量；放不下两张下限时返回 false。 */
export function addExtraPartition(diskNumber) {
  const target = targetOf(diskNumber);
  const disk = diskOf(diskNumber);
  if (!target || !disk) return false;
  if (!Array.isArray(target.extras)) target.extras = [];
  if (target.extras.length >= MAX_EXTRAS) return false;

  // 可分配给数据分区的总量 = 总容量 − 系统分区 − 给 EFI/对齐留的余量
  const totalGb = toGb(disk.size_bytes);
  const available = Math.round((totalGb - Number(target.sysGb || 0) - RESERVE_GB) * 10) / 10;
  if (!(available > 0)) return false;

  let dataGb = Math.floor((available / 2) * 10) / 10;
  if (dataGb < MIN_DATA_GB) dataGb = MIN_DATA_GB;
  let extraGb = Math.floor((available - dataGb) * 10) / 10;
  if (extraGb < MIN_EXTRA_GB) {
    extraGb = MIN_EXTRA_GB;
    dataGb = Math.round((available - extraGb) * 10) / 10;
  }
  // 两个分区都有下限（D ≥ 20 GB、E ≥ 1 GB），放不下就放弃
  if (dataGb < MIN_DATA_GB || extraGb < MIN_EXTRA_GB) return false;

  target.dataGb = dataGb;
  target.extras.push({ sizeGb: extraGb, fs: dataFs() });
  state.wizard.ack = false;
  return true;
}

export function removeExtraPartition(diskNumber, index) {
  const target = targetOf(diskNumber);
  if (!target || !Array.isArray(target.extras)) return;
  target.extras.splice(Number(index), 1);
  state.wizard.ack = false;
}

export function targetsValid() {
  return state.wizard.targets.length > 0
    && state.wizard.targets.every((target) => !targetError(target));
}

/** 每块盘的通知 payload：POST /api/prepare 的 selection 形态。 */
export function selectionPayload() {
  const selection = {};
  const fs = dataFs();
  for (const target of state.wizard.targets) {
    const entry = { c_size: Number(target.sysGb) };
    const extras = Array.isArray(target.extras) ? target.extras : [];
    if (extras.length) {
      // 有额外分区时必须给出显式列表：第一个是 D，其余是 E/F……
      // 文件系统统一取 dataFs()，界面上的 E 没有选择器（与 D 一致）。
      entry.data_partitions = [
        { size_gb: Number(target.dataGb), file_system: fs },
        ...extras.map((extra) => ({
          size_gb: Number(extra.sizeGb),
          file_system: fs,
        })),
      ];
    } else {
      entry.data_size = Number(target.dataGb);
    }
    if (target.imagePath) entry.win_gho = target.imagePath;
    if (target.imageIndex) entry.image_index = Number(target.imageIndex);
    selection[target.diskNumber] = entry;
  }
  return selection;
}

/** 剩余空间（用于第 2 步右上的“剩余”）。 */
export function targetRest(target) {
  const disk = diskOf(target.diskNumber);
  const total = Number(disk && disk.size_bytes) || 0;
  const extras = (Array.isArray(target.extras) ? target.extras : [])
    .reduce((sum, extra) => sum + (Number(extra.sizeGb) || 0), 0);
  const rest = total
    - (Number(target.sysGb) || 0) * (1024 ** 3)
    - (Number(target.dataGb) || 0) * (1024 ** 3)
    - extras * (1024 ** 3);
  return rest;
}

export function diskFreeGb(disk) {
  return freeBytes(disk) / (1024 ** 3);
}
