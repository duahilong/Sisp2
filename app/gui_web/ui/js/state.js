/**
 * 全局状态：一份可变对象 + 一个极简订阅表。
 *
 * 为什么不用别的框架：整个界面只有三个视图、一次只跑一个任务，
 * 手工 render() 比引入响应式框架更好排查问题，也避免打包体积膨胀。
 * 约定：**只有 views 里的 render*() 读 state，事件处理只改 state 然后 notify()**。
 */

import {
  clamp, freeBytes, isSelectable, checkLayout, fitLayout, reserveGb as reserveGbFor,
  DEFAULT_EFI_MB, MIN_DATA_GB, MIN_EXTRA_GB, MAX_EXTRAS, toGb,
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

/** 第 2 步能拿到的 EFI 分区大小（MB）。
 *
 * 优先用执行计划的真实值（第 4 步会回填 ``config_payload.partition_info``），
 * 其次用设置里的 ``efiDefaultMb``，都没有才用默认 100 MB。
 * 它决定给 EFI/对齐预留多少余量，所以值不准只会多一点或少一点“未分配”，
 * 不会让计划变得不合法（后端 `_check_partition_fit` 会再验一次）。
 */
export function reserveEfiMb() {
  const item = (state.plans || []).find((plan) => plan.spec && plan.spec.config_payload);
  const info = item && item.spec.config_payload.partition_info;
  const fromPlan = Number(info && info.efi_size_mb);
  if (fromPlan > 0) return fromPlan;
  const fromSettings = Number(state.settings && state.settings.efiDefaultMb);
  return fromSettings > 0 ? fromSettings : DEFAULT_EFI_MB;
}

/** 要预留的容量（GB）= EFI 分区 + 对齐开销；界面所有“分多少”的地方都用它。 */
export function reserveGb() {
  return reserveGbFor(reserveEfiMb());
}

export function defaultSizes(disk) {
  const total = Number(disk && disk.size_bytes) || 0;
  const totalGb = total / (1024 ** 3);
  const reserve = reserveGb();
  const wanted = Math.round(Number((state.settings && state.settings.sysDefaultGb) || 120));
  const sys = clamp(wanted, 60, Math.max(60, Math.floor(totalGb - 20)));
  const dataDefault = state.settings ? state.settings.dataDefaultGb : 'auto';
  // 留出 EFI 与对齐的余量，否则默认值会把盘算到最后一字节，后端直接判容量不足。
  let data = Math.round((totalGb - sys - reserve) * 10) / 10;
  if (dataDefault !== 'auto' && Number(dataDefault) > 0) {
    data = clamp(Number(dataDefault), 20, Math.max(20, Math.floor(totalGb - sys - reserve)));
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

/** 加一个数据分区（E）。平分剩余容量；放不下两张下限时返回 false。 */
export function addExtraPartition(diskNumber) {
  const target = targetOf(diskNumber);
  const disk = diskOf(diskNumber);
  if (!target || !disk) return false;
  if (!Array.isArray(target.extras)) target.extras = [];
  if (target.extras.length >= MAX_EXTRAS) return false;

  // 试算：把这一份当成“已经存在的最小 E”，让 fitLayout 平分一次。
  // 用同一个函数，所以“加 E 之后的平分”与“点快捷容量之后的平分”永远一致。
  const trial = [
    ...target.extras.map((extra) => Number(extra.sizeGb) || 0),
    MIN_EXTRA_GB,
  ];
  const fit = fitLayout(toGb(disk.size_bytes), Number(target.sysGb || 0), trial, {
    even: true,
    efiMb: reserveEfiMb(),
  });
  // fitLayout 只有真走了“平分”分支才会让所有数据分区一样大；
  // 盘太小被压回下限时不是平分的，这种情况直接放弃（提示用户没空间了）。
  const even = fit.extrasGb.length === trial.length
    && fit.extrasGb.every((gb) => gb === fit.dataGb)
    && fit.dataGb >= MIN_DATA_GB;
  if (!even) return false;

  target.extras.push({ sizeGb: MIN_EXTRA_GB, fs: dataFs() });
  applyFit(target, fit);
  state.wizard.ack = false;
  return true;
}

/** 把 ``derive.fitLayout()`` 的结果写回 target。
 *
 * 界面里有好几处“重算容量”（快捷容量、拖动分隔条、加/删 E），全都走这一个回写，
 * 避免某个入口只改了一半字段（曾经就是这样：删掉 E 之后 D 还停在“平分”时的半个盘）。
 */
export function applyFit(target, fit) {
  if (!target || !fit) return;
  target.sysGb = fit.sysGb;
  target.dataGb = fit.dataGb;
  const extras = Array.isArray(target.extras) ? target.extras : [];
  extras.forEach((extra, index) => {
    if (fit.extrasGb[index] !== undefined) extra.sizeGb = fit.extrasGb[index];
  });
}

/** 删掉一个额外分区（E）。
 *
 * 删完要把解出来的容量还给 D——回到“C 指定大小 + D 吃掉剩余”的两分区状态，
 * 否则界面会停在「D 只占一半、另一半变成未分配」的怪状态（总容量没少，但用户
 * 明明只想回到两个分区）。这是用户明确要求的行为：不想加 E 了，删掉就该恢复原样。
 */
export function removeExtraPartition(diskNumber, index) {
  const target = targetOf(diskNumber);
  if (!target || !Array.isArray(target.extras)) return;
  target.extras.splice(Number(index), 1);
  const disk = diskOf(diskNumber);
  const totalGb = disk ? toGb(disk.size_bytes) : 0;
  if (totalGb > 0) {
    applyFit(
      target,
      fitLayout(
        totalGb,
        Number(target.sysGb) || 0,
        target.extras.map((extra) => Number(extra.sizeGb) || 0),
        { efiMb: reserveEfiMb() },
      ),
    );
  }
  state.wizard.ack = false;
}

export function targetsValid() {
  return state.wizard.targets.length > 0
    && state.wizard.targets.every((target) => !targetError(target));
}

/** 每块盘的通知 payload：POST /api/prepare 的 selection 形态。
 *
 * **最后一个数据分区永远发 ``"auto"``**（= diskpart 不写 ``size=``，吃掉全部剩余空间）：
 *   - 界面上的 D/E 数字只是“预估”（``fitLayout`` 算出来的、用于显示与拖动），
 *     最后那个分区的输入框是只读的，用户改不了；
 *   - 尺寸由 diskpart 自己算，所以永远不会出现“界面显示刚好、diskpart 报空间不足”；
 *   - 有 E 时：D 显式、E 为 auto；没有 E 时：D 就是 auto，永远吃满整盘。
 */
export function selectionPayload() {
  const selection = {};
  const fs = dataFs();
  for (const target of state.wizard.targets) {
    const extras = Array.isArray(target.extras) ? target.extras : [];
    const entry = { c_size: Number(target.sysGb) };
    if (extras.length) {
      // 有额外分区时必须给出显式列表：第一个是 D（显式），最后一个是 auto。
      // 文件系统统一取 dataFs()，界面上的 E 没有选择器（与 D 一致）。
      const list = [
        { size_gb: Number(target.dataGb), file_system: fs },
        ...extras.map((extra) => ({
          size_gb: Number(extra.sizeGb),
          file_system: fs,
        })),
      ];
      list[list.length - 1].size_gb = 'auto';
      entry.data_partitions = list;
    } else {
      entry.data_size = 'auto';
    }
    if (target.imagePath) entry.win_gho = target.imagePath;
    if (target.imageIndex) entry.image_index = Number(target.imageIndex);
    selection[target.diskNumber] = entry;
  }
  return selection;
}

/** 最后一个数据分区（D 或 E）能分到多少字节 —— 它就是“剩余空间”。 */
export function autoPartitionBytes(target) {
  const disk = diskOf(target.diskNumber);
  const total = Number(disk && disk.size_bytes) || 0;
  const extras = Array.isArray(target.extras) ? target.extras : [];
  const others = (Number(target.sysGb) || 0)
    + (extras.length ? Number(target.dataGb) || 0 : 0)
    + reserveGb();
  return Math.max(0, total - others * (1024 ** 3));
}

/** 最后一个数据分区永远吃满剩余：按当前的 C（和显式 D）把它的预估值重算一遍。
 *
 * 以前 D 是显式分区，用户改 C 时只需重算“剩余”那个提示；现在 D/E 是 auto
 * （不带 `size=`），它的预估值就是“剩下多少”，**必须跟着 C / 显式 D 变**，
 * 否则输入框里的数字和进度条会和盘容量对不上（C 116 + D 276.6 ≠ 477）。
 *
 * 只改预估值、不动用户正在输入的那个值；越界时算出来可能是 0，交给
 * ``targetError()``（checkLayout 的“剩下的空间够不够下限”）去报错。
 */
export function syncAutoPartition(target) {
  if (!target) return;
  const disk = diskOf(target.diskNumber);
  const totalGb = toGb(Number(disk && disk.size_bytes) || 0);
  if (!(totalGb > 0)) return;
  const extras = Array.isArray(target.extras) ? target.extras : [];
  const autoRole = extras.length ? 'extra' : 'data';
  const explicitGb = (Number(target.sysGb) || 0)
    + (autoRole === 'extra' ? Number(target.dataGb) || 0 : 0);
  // 与 fitLayout 同口径：向下取整到 0.1 GB，再加 1e-9 吸收浮点误差
  const left = Math.max(0, Math.floor((totalGb - explicitGb - reserveGb()) * 10 + 1e-9) / 10);
  if (autoRole === 'extra') extras[extras.length - 1].sizeGb = left;
  else target.dataGb = left;
}

export function diskFreeGb(disk) {
  return freeBytes(disk) / (1024 ** 3);
}
