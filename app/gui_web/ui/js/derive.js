/**
 * 纯函数：把后端契约里的字节数/分区结构换算成界面要的东西。
 *
 * 这里**只做换算**，不读 DOM、不发请求、不缓存。好处是可以用 node 直接跑
 * （见 build/ui_check.py 里的 node --check 与 tests/test_ui_assets.py），
 * 也可以被三个视图共用，不会出现“主页面一套颜色、安装向导另一套”的情况。
 *
 * 数据来源（不要凭直觉改字段名）：
 *   disk   = DiskInfo.to_dict()       ← GET /api/disks
 *   part   = PartitionInfo.to_dict()
 *   plan   = ExecutionPlan.to_dict()   ← POST /api/prepare
 */

export const FS_COLOR = {
  NTFS: '#2F6FDC',
  exFAT: '#C0841C',
  ESP: '#7A5BC7',
  REC: '#6E8496',
  FREE: '#C6D2DF',
  OTHER: '#9AA9B8',
};

export const FS_NAME = {
  NTFS: 'NTFS',
  exFAT: 'exFAT',
  ESP: 'EFI',
  REC: '恢复',
  FREE: '未分配',
  OTHER: '未知',
};

/* GPT 分区类型 GUID 的小写前缀；后端下发时可能带花括号。 */
const GPT_ESP = 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b';
const GPT_RECOVERY = 'de94bba4-06d1-4d40-a16a-bfd50179d6ac';
const GPT_MSR = 'e3c9e316-0b5c-4db8-817d-f92df00215ae';

export const MIN_SYS_GB = 60;
export const MIN_DATA_GB = 20;
const MB = 1024 * 1024;
const GB = 1024 * 1024 * 1024;
const TB = 1024 * 1024 * 1024 * 1024;

export function clamp(value, low, high) {
  const num = Number(value);
  if (!Number.isFinite(num)) return low;
  return Math.min(high, Math.max(low, num));
}

/** 按原型的口径显示容量：≥1 TB 用 TB，≥10 GB 取整，≥1 GB 一位小数，其余 MB。 */
export function fmtSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return '未知';
  if (value >= TB) return `${(value / TB).toFixed(2)} TB`;
  if (value >= 10 * GB) return `${Math.round(value / GB)} GB`;
  if (value >= GB) return `${(value / GB).toFixed(1)} GB`;
  if (value >= MB) return `${Math.max(1, Math.round(value / MB))} MB`;
  return `${Math.round(value)} B`;
}

/** 精确到两位小数的容量，用在表格与日志里。 */
export function fmtExact(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value < 0) return '未知';
  if (value >= GB) return `${(value / GB).toFixed(2)} GB`;
  if (value >= MB) return `${(value / MB).toFixed(2)} MB`;
  return `${Math.round(value)} B`;
}

export const toGb = (bytes) => (Number(bytes) || 0) / GB;
export const fromGb = (value) => Math.round((Number(value) || 0) * GB);

/** 分区 → 配色分类（NTFS / exFAT / ESP / REC / OTHER）。 */
export function fsClass(part) {
  if (!part) return 'OTHER';
  const gpt = String(part.gpt_type || '').toLowerCase().replace(/[{}]/g, '');
  if (gpt === GPT_ESP) return 'ESP';
  if (gpt === GPT_RECOVERY) return 'REC';
  if (gpt === GPT_MSR) return 'OTHER';

  const fs = String(part.file_system || '').trim().toUpperCase();
  if (fs === 'NTFS') return 'NTFS';
  if (fs === 'EXFAT') return 'exFAT';
  if (fs === 'FAT32' || fs === 'FAT' || fs === 'FAT16') {
    // FAT32 的 ESP 与普通 FAT32 U 盘要分开：有 esp/boot 标记或卷标像 EFI 才算 ESP。
    const label = String(part.label || '').toUpperCase();
    const looksEsp = gpt === GPT_ESP || label.includes('EFI') || label.includes('ESP')
      || Boolean(part.is_system && !part.drive_letter);
    return looksEsp ? 'ESP' : 'OTHER';
  }
  return 'OTHER';
}

export const fsColor = (cls) => FS_COLOR[cls] || FS_COLOR.OTHER;
export const fsName = (cls) => FS_NAME[cls] || FS_NAME.OTHER;

/** 分区的“已用”比例；后端没有 remaining 时返回 null（界面画满色）。 */
export function usedRatio(part) {
  const size = Number(part && part.size_bytes);
  const used = Number(part && part.used_bytes);
  if (!Number.isFinite(size) || size <= 0 || !Number.isFinite(used)) return null;
  return clamp(used / size, 0, 1);
}

export function diskTotal(disk) {
  const size = Number(disk && disk.size_bytes);
  return Number.isFinite(size) && size > 0 ? size : 0;
}

/** 分区按起始偏移排序；没有偏移时退回分区号。 */
export function sortedParts(disk) {
  const parts = (disk && disk.partitions) || [];
  return parts.slice().sort((a, b) => {
    const ao = Number(a.offset_bytes);
    const bo = Number(b.offset_bytes);
    if (Number.isFinite(ao) && Number.isFinite(bo) && ao !== bo) return ao - bo;
    return Number(a.partition_number || 0) - Number(b.partition_number || 0);
  });
}

/** 未被任何分区占用的字节（按后端给出的分区大小求和）。 */
export function freeBytes(disk) {
  const total = diskTotal(disk);
  const used = sortedParts(disk).reduce((sum, part) => sum + (Number(part.size_bytes) || 0), 0);
  return Math.max(0, total - used);
}

/**
 * 分区地图/概览条用的片段数组。
 * 每段：{key, cls, color, ratio, sizeBytes, letter, name, free}
 */
export function segments(disk) {
  const total = diskTotal(disk);
  if (!total) return [];
  const out = [];
  for (const part of sortedParts(disk)) {
    const size = Number(part.size_bytes) || 0;
    if (size <= 0) continue;
    const cls = fsClass(part);
    out.push({
      key: `p${part.partition_number}`,
      cls,
      color: fsColor(cls),
      ratio: clamp(size / total, 0, 1),
      sizeBytes: size,
      letter: part.drive_letter || '',
      name: partLabel(part),
      free: false,
      part,
    });
  }
  const rest = freeBytes(disk);
  if (rest > MB) {
    out.push({
      key: 'free',
      cls: 'FREE',
      color: FS_COLOR.FREE,
      ratio: clamp(rest / total, 0, 1),
      sizeBytes: rest,
      letter: '',
      name: '未分配',
      free: true,
      part: null,
    });
  }
  return out;
}

/** 卷标显示口径：有卷标用卷标，否则用「C: Windows」式回退。 */
export function partLabel(part) {
  if (!part) return '未分配';
  const label = String(part.label || '').trim();
  if (label) return label;
  const cls = fsClass(part);
  if (cls === 'ESP') return 'EFI 系统分区';
  if (cls === 'REC') return '恢复分区';
  return fsName(cls);
}

/** 树/表格里的短名：C: 系统 / （无卷标）。 */
export function partShortName(part) {
  if (!part) return '未分配空间';
  const letter = part.drive_letter ? `${part.drive_letter}: ` : '';
  return `${letter}${partLabel(part)}`;
}

export function partBadge(part) {
  if (!part) return '·';
  return part.drive_letter || '—';
}

/** 表格“类型”列：GPT 类型优先显示中文角色，MBR 显示主分区/扩展。 */
export function partTypeText(part) {
  if (!part) return '未分配';
  const cls = fsClass(part);
  if (cls === 'ESP') return 'EFI 系统分区';
  if (cls === 'REC') return '恢复分区';
  const gpt = String(part.gpt_type || '').toLowerCase().replace(/[{}]/g, '');
  if (gpt === GPT_MSR) return 'MSR 保留';
  if (part.type) return String(part.type);
  return part.is_system ? '系统分区' : '主分区';
}

/** 分区状态标签：系统 / 引导 / 隐藏 / 正常。 */
export function partStatusText(part) {
  if (!part) return '未分配';
  const flags = [];
  if (part.is_system) flags.push('系统');
  if (part.is_boot) flags.push('引导');
  if (part.is_hidden) flags.push('隐藏');
  return flags.length ? flags.join(' · ') : '正常';
}

/** 磁盘是否可被写（可安装/可分区的候选）。 */
export function isSelectable(disk) {
  if (!disk) return false;
  if (disk.is_selectable === false) return false;
  if (disk.is_system || disk.is_boot || disk.is_read_only || disk.is_offline) return false;
  return true;
}

/** 受保护原因 → 一句中文；没有原因时按标记兜底。 */
export function protectionText(disk) {
  const reasons = (disk && disk.protection_reasons) || [];
  if (reasons.length) return reasons.join('；');
  if (!disk) return '';
  if (disk.is_system || disk.is_boot) return '正在使用的系统盘，不能写入';
  if (disk.is_read_only) return '只读磁盘，不能写入';
  if (disk.is_offline) return '磁盘处于脱机状态';
  return '';
}

/** 当前所有已用盘符（不带冒号）。 */
export function usedLetters(disks) {
  const used = new Set(['A', 'B', 'C']);
  for (const disk of disks || []) {
    for (const part of disk.partitions || []) {
      const letter = String(part.drive_letter || '').trim().toUpperCase();
      if (letter) used.add(letter);
    }
  }
  return used;
}

/**
 * 给分区操作建议空闲盘符：从 Z 往 D 降序取第一个没被占用的字母。
 * 只用于输入框默认值，后端 validate() 会再检查一遍。
 */
export function suggestLetter(disks, exclude = []) {
  const used = usedLetters(disks);
  for (const letter of exclude) used.add(String(letter || '').toUpperCase());
  for (let code = 'Z'.charCodeAt(0); code >= 'D'.charCodeAt(0); code -= 1) {
    const letter = String.fromCharCode(code);
    if (!used.has(letter)) return letter;
  }
  return '';
}

/** 默认分区大小里给 EFI / 对齐预留的余量（GB）。 */
export const RESERVE_GB = 1;

/** 分区地图标尺刻度：0 / 25% / 50% / 75% / 100%，最后一个是总容量。 */
export function rulerTicks(totalBytes) {
  return [0, 25, 50, 75, 100].map((percent) => ({
    percent,
    label: percent === 0 ? '0' : fmtSize((Number(totalBytes) || 0) * (percent / 100)),
  }));
}

/** 额外数据分区（E）的限制：最多 1 个，最小 1 GB。 */
export const MAX_EXTRAS = 1;
export const MIN_EXTRA_GB = 1;

/** 额外分区的段颜色（与分区地图里的数据卷同色系，但更深一点以免混淆）。 */
export function extraColor(index) {
  return ["#1F8A70", "#3E7CB1", "#9C6644"][Number(index) % 3];
}

/** 额外数据分区的角色/徽标：索引 0 是 E，1 是 F……（C=系统、D=第一个数据分区） */
export function extraLetter(index) {
  return String.fromCharCode("E".charCodeAt(0) + Number(index));
}

/** 分区大小校验（与后端 plan_partitions/check_layout 同口径，错误提示给界面用）。
 *
 * ``efiMb`` 是 EFI 分区大小：它来自配置文件（``partition_info.efi_size_mb``，
 * 常见 100/300 MB），界面拿不到，所以按 100 MB 估一个下限；真实的
 * EFI 大小在第 4 步的执行计划里能读到，超了后端也会直接把阻断项写回来。
 *
 * ``extras`` 是额外数据分区数组（``[{sizeGb}]``），每一项都必须显式给大小——
 * 只有最后一个有大小，剩下的是“未分配”，界面会单独提示。
 */
export function checkLayout(totalBytes, sysGb, dataGb, extras = [], efiMb = 100) {
  const total = Number(totalBytes) || 0;
  const sys = Number(sysGb) || 0;
  const data = Number(dataGb) || 0;
  const efi = Math.max(0, Number(efiMb) || 0);
  if (!(sys > 0) || !(data > 0)) return '两个分区都要填大小';
  if (sys < MIN_SYS_GB) return `系统分区至少 ${MIN_SYS_GB} GB`;
  if (data < MIN_DATA_GB) return `第一个数据分区至少 ${MIN_DATA_GB} GB`;
  let need = Math.round(efi * MB) + fromGb(sys) + fromGb(data) + MB;
  for (const extra of extras || []) {
    const gb = Number((extra && extra.sizeGb) || 0);
    if (!(gb > 0)) return '额外分区要填大小';
    if (gb < MIN_EXTRA_GB) return `额外分区至少 ${MIN_EXTRA_GB} GB`;
    need += fromGb(gb);
  }
  if (need > total) return `超过磁盘容量 ${fmtSize(total)}`;
  return '';
}

/** 兼容老调用方：没有额外分区时就是原来的两分区校验。 */
export function checkSizes(totalBytes, sysGb, dataGb, efiMb = 100) {
  return checkLayout(totalBytes, sysGb, dataGb, [], efiMb);
}

/** 按计划建完分区后剩下的未分配字节数（0 表示正好用完）。 */
export function unallocatedBytes(totalBytes, sysGb, dataGb, extras = [], efiMb = 100) {
  const total = Number(totalBytes) || 0;
  let used = Math.round((Number(efiMb) || 0) * MB) + fromGb(Number(sysGb) || 0) + fromGb(Number(dataGb) || 0) + MB;
  for (const extra of extras || []) used += fromGb(Number((extra && extra.sizeGb) || 0));
  return Math.max(0, total - used);
}
