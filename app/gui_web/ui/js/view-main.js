/**
 * 主页面：左侧磁盘/分区树 + 中间概览条、盘头、分区地图、分区明细。
 *
 * 只读视图：这里不做任何写盘动作，按钮最多跳去安装向导或打开诊断弹窗。
 * 所有数字都来自 `GET /api/disks`（DiskInfo + PartitionInfo），
 * 未分配空间 = 磁盘容量 − Σ 分区容量，由 derive.segments() 算出来。
 */

import { $, $$, esc, on } from './dom.js';
import {
  fmtSize, fmtExact, segments, fsColor, fsName, fsClass, partLabel, partShortName,
  partTypeText, partStatusText, protectionText, isSelectable, rulerTicks, diskTotal,
  usedRatio, partBadge,
} from './derive.js';
import { state, diskOf, selectDisk, selectPart, notify, jobsArray } from './state.js';
import { setStatus, setMidStatus, jobStatusText } from './shell.js';

export function renderMain() {
  renderBanners();
  renderTree();
  renderOverview();
  renderCenter();
  const disk = state.disks.find((item) => Number(item.disk_number) === Number(state.selectedDisk)) || null;
  const part = (disk && (disk.partitions || []).find((item) => Number(item.partition_number) === Number(state.selectedPart))) || null;
  setMidStatus(disk, part);
}

/* ------------------------------------------------------------------ 提示条 */
function renderBanners() {
  const box = $('#banners');
  if (!box) return;
  const banners = [];
  const session = state.session;
  if (state.info && state.info.dry_run) {
    banners.push(['ok', '演练模式：界面可以完整走一遍，但不会真的写盘。要真写请在设置里关掉“默认演练”。']);
  }
  for (const blocker of (session && session.blockers) || []) {
    banners.push(['danger', blocker]);
  }
  for (const warning of (session && session.warnings) || []) {
    banners.push(['warn', warning]);
  }
  if (!state.disks.length) {
    banners.push(['warn', '没有扫描到磁盘。插好硬盘后点「刷新磁盘」，或用管理员身份重新打开。']);
  }
  const failed = jobsArray().filter((job) => job.status === 'failed');
  if (failed.length) {
    banners.push(['danger', `${failed.length} 个任务失败：${failed.map((job) => job.error || job.id).join('；')}`]);
  }
  box.innerHTML = banners.map(([kind, text]) => `<div class="banner ${kind}">
      <span class="bi">${kind === 'ok' ? '✓' : '!'}</span><span>${esc(text)}</span>
    </div>`).join('');
}

/* ------------------------------------------------------------------ 左树 */
function renderTree() {
  const tree = $('#tree');
  if (!tree) return;

  if (!state.disks.length) {
    tree.innerHTML = '<div class="empty-hint">还没有磁盘数据<br>点上方「刷新磁盘」</div>';
    $('#treeHint').textContent = '';
    return;
  }

  const html = state.disks.map((disk) => {
    const number = Number(disk.disk_number);
    const locked = !isSelectable(disk);
    const onDisk = number === Number(state.selectedDisk) ? ' on' : '';
    const flag = disk.is_system || disk.is_boot
      ? '<span class="tdot sys" title="系统盘"></span>'
      : (disk.is_read_only ? '<span class="tdot ro" title="只读"></span>' : '');
    const name = locked
      ? (disk.is_system || disk.is_boot ? '系统盘' : (disk.is_read_only ? '只读磁盘' : '受保护'))
      : (disk.model || '未知型号');
    let body = '';
    for (const part of disk.partitions || []) {
      const cls = fsClass(part);
      const selected = number === Number(state.selectedDisk)
        && Number(part.partition_number) === Number(state.selectedPart) ? ' on' : '';
      const lockedPart = part.is_system || part.is_boot ? ' locked' : '';
      body += `<div class="tnode part${selected}${lockedPart}" data-disk="${number}" data-part="${esc(part.partition_number)}"
          title="${esc(`${partShortName(part)} · ${fsName(cls)} · ${fmtExact(part.size_bytes)}`)}">
        <span class="lbadge" style="background:${fsColor(cls)}">${esc(partBadge(part))}</span>
        <span class="tname">${esc(partShortName(part))}</span>
        ${part.is_system || part.is_boot ? '<span class="tflag">系统</span>' : ''}
        <span class="tsize">${esc(fmtSize(part.size_bytes))}</span>
      </div>`;
    }
    const segs = segments(disk);
    const freeSeg = segs.find((seg) => seg.free);
    if (freeSeg) {
      body += `<div class="tnode part" data-disk="${number}" data-part="free"
          title="未分配空间 · ${esc(fmtExact(freeSeg.sizeBytes))}">
        <span class="lbadge free">·</span>
        <span class="tname" style="color:var(--faint)">未分配空间</span>
        <span class="tsize">${esc(fmtSize(freeSeg.sizeBytes))}</span>
      </div>`;
    }

    return `<div class="tnode disk${onDisk}" data-disk="${number}" title="${esc(disk.model || '')}">
        ${flag}<span class="tname">磁盘 ${number} · ${esc(name)}</span>
        <span class="tsize">${esc(disk.size_display || fmtSize(disk.size_bytes))}</span>
      </div>${body}`;
  }).join('');

  tree.innerHTML = html;
  const partCount = state.disks.reduce((sum, disk) => sum + ((disk.partitions || []).length), 0);
  $('#treeHint').textContent = `${state.disks.length} 块 / ${partCount} 分区`;
}

on(document, 'click', '#tree .tnode', (event, node) => {
  const diskNumber = Number(node.dataset.disk);
  if (node.dataset.part === undefined) {
    selectDisk(diskNumber);
    const disk = diskOf(diskNumber);
    setStatus(disk ? `选择磁盘 ${diskNumber} · ${disk.model || ''}` : '');
  } else {
    const part = node.dataset.part === 'free' ? null : Number(node.dataset.part);
    selectPart(diskNumber, part);
    const disk = diskOf(diskNumber);
    const target = part === null
      ? null
      : (disk.partitions || []).find((item) => Number(item.partition_number) === part);
    if (target) {
      setStatus(`选中 ${partShortName(target)} · ${fsName(fsClass(target))} · ${fmtExact(target.size_bytes)}`
        + (protectionText(disk) ? ` · ${protectionText(disk)}` : ''));
    } else {
      setStatus(`选中磁盘 ${diskNumber} 的未分配空间`);
    }
  }
  notify();
});

/* ------------------------------------------------------------------ 概览条 */
function renderOverview() {
  const box = $('#overview');
  if (!box) return;
  if (!state.disks.length) {
    box.innerHTML = '';
    return;
  }
  box.innerHTML = state.disks.map((disk) => {
    const number = Number(disk.disk_number);
    const active = number === Number(state.selectedDisk) ? ' on' : '';
    const locked = isSelectable(disk) ? '' : ' locked';
    const bars = segments(disk).map((seg) => `<i style="width:${(seg.ratio * 100).toFixed(2)}%;background:${seg.color}"
        title="${esc(`${seg.name} ${fmtSize(seg.sizeBytes)}`)}"></i>`).join('');
    return `<button class="ocard${active}${locked}" data-disk="${number}" title="${esc(disk.model || '')}">
      <span class="orow"><span class="oname">磁盘 ${number}</span><span class="ocap">${esc(disk.size_display || fmtSize(disk.size_bytes))}</span></span>
      <span class="obar">${bars}</span>
      <span class="omodel">${esc(disk.model || '未知型号')}${lockSuffix(disk)}</span>
    </button>`;
  }).join('');

  $$('#overview .ocard').forEach((card) => {
    card.addEventListener('click', () => {
      selectDisk(Number(card.dataset.disk));
      notify();
    });
  });
}

function lockSuffix(disk) {
  if (disk.is_system || disk.is_boot) return ' · 系统盘';
  if (disk.is_read_only) return ' · 只读';
  if (disk.is_offline) return ' · 脱机';
  return '';
}

/* ------------------------------------------------------------------ 中间：盘头 + 地图 + 表格 */
function renderCenter() {
  const disk = diskOf(state.selectedDisk);
  renderHead(disk);
  renderMap(disk);
  renderGrid(disk);
}

function renderHead(disk) {
  const head = $('#diskHead');
  if (!head) return;
  if (!disk) {
    head.innerHTML = '<span class="hname">未选择磁盘</span>';
    return;
  }
  const partitions = disk.partitions || [];
  const reasons = protectionText(disk);
  const flags = [];
  if (disk.is_system || disk.is_boot) flags.push('<span class="flag warn">系统盘 · 已锁定</span>');
  if (disk.is_read_only) flags.push('<span class="flag warn">只读磁盘</span>');
  if (disk.is_offline) flags.push('<span class="flag warn">脱机</span>');
  if (isSelectable(disk)) flags.push('<span class="flag acc">可操作</span>');
  if (state.info && state.info.dry_run) flags.push('<span class="flag">演练模式</span>');

  head.innerHTML = `
    <span class="hname">磁盘 ${esc(disk.disk_number)}</span>
    <span class="hmeta">
      <span>${esc(disk.model || '未知型号')}</span><span class="dotsep"></span>
      <span class="mono">${esc(disk.size_display || fmtSize(disk.size_bytes))}</span><span class="dotsep"></span>
      <span>${esc(disk.partition_style || '未知')}</span><span class="dotsep"></span>
      <span>${esc(disk.bus_type || '未知总线')}</span><span class="dotsep"></span>
      <span>${partitions.length} 个分区</span>
      ${reasons ? `<span class="dotsep"></span><span title="${esc(reasons)}">${esc(reasons)}</span>` : ''}
    </span>
    <span class="hr">${flags.join('')}</span>`;
}

function renderMap(disk) {
  const map = $('#map');
  const legend = $('#legend');
  if (!map || !legend) return;

  if (!disk) {
    map.innerHTML = '<div class="mapempty">选择左侧或上方的磁盘后，这里显示分区布局。</div>';
    legend.innerHTML = '';
    return;
  }

  const total = diskTotal(disk);
  const segs = segments(disk);
  if (!segs.length) {
    map.innerHTML = '<div class="mapempty">这块磁盘没有可显示的分区信息。</div>';
    legend.innerHTML = '';
    return;
  }

  const ticks = rulerTicks(total);
  const ruler = ticks.map((tick) => `<i style="left:${tick.percent}%"></i>
      <span style="left:${tick.percent}%">${esc(tick.label)}</span>`).join('');

  const pieces = segs.map((seg) => {
    const percent = seg.ratio * 100;
    const classes = ['seg-piece'];
    if (seg.free) classes.push('free');
    else if (seg.cls === 'OTHER') classes.push('unknown');
    const selected = seg.part && Number(seg.part.partition_number) === Number(state.selectedPart) ? ' sel' : '';
    const style = `width:${percent.toFixed(3)}%${seg.free ? '' : `;background:${seg.color}`}`;
    const letter = seg.free ? '空' : (seg.letter || '—');
    const inner = percent > 9
      ? `<span class="sl">${esc(letter)}</span><span class="sz">${esc(fmtSize(seg.sizeBytes))}</span>`
      : (percent > 4.5 ? `<span class="sl">${esc(letter)}</span>` : '');
    const title = `${seg.name} · ${seg.free ? '未分配' : fsName(seg.cls)} · ${fmtExact(seg.sizeBytes)}`
      + (seg.part && usedRatio(seg.part) !== null ? ` · 已用 ${(usedRatio(seg.part) * 100).toFixed(0)}%` : '');
    return `<div class="${classes.join('')}${selected}" style="${style}"
        data-part="${seg.part ? esc(seg.part.partition_number) : 'free'}" title="${esc(title)}">${inner}</div>`;
  }).join('');

  map.innerHTML = `<div class="ruler">${ruler}</div><div class="bar">${pieces}</div>`;

  const groups = new Map();
  for (const seg of segs) {
    const group = groups.get(seg.cls) || { count: 0, size: 0 };
    group.count += 1;
    group.size += seg.sizeBytes;
    groups.set(seg.cls, group);
  }
  legend.innerHTML = Array.from(groups.entries()).map(([cls, group]) => `<span class="lg">
      <i style="background:${fsColor(cls)}"></i>${esc(fsName(cls))} <b>${esc(fmtSize(group.size))}</b>
    </span>`).join('')
    + `<span class="lg" style="margin-left:auto;color:var(--faint)">共 ${segs.length} 项</span>`;

  $$('#map .seg-piece').forEach((piece) => {
    piece.addEventListener('click', () => {
      if (piece.dataset.part === 'free') {
        selectPart(disk.disk_number, null);
      } else {
        selectPart(disk.disk_number, Number(piece.dataset.part));
      }
      notify();
    });
  });
}

function renderGrid(disk) {
  const body = $('#gridBody');
  if (!body) return;
  if (!disk) {
    body.innerHTML = '<tr class="emptyrow"><td colspan="6">还没有选择磁盘</td></tr>';
    return;
  }
  const parts = (disk.partitions || []).slice().sort(
    (a, b) => Number(a.partition_number || 0) - Number(b.partition_number || 0),
  );
  if (!parts.length) {
    body.innerHTML = '<tr class="emptyrow"><td colspan="6">这块磁盘上没有分区</td></tr>';
    return;
  }
  body.innerHTML = parts.map((part) => {
    const cls = fsClass(part);
    const selected = Number(part.partition_number) === Number(state.selectedPart) ? ' class="sel"' : '';
    const label = String(part.label || '').trim();
    return `<tr${selected} data-part="${esc(part.partition_number)}">
      <td><span class="lt"><span class="lbadge" style="background:${fsColor(cls)}">${esc(partBadge(part))}</span>
        <span>${esc(part.drive_letter || '—')}</span></span></td>
      <td class="${label ? '' : 'dim'}" title="${esc(label)}">${esc(label || '（无卷标）')}</td>
      <td class="${cls === 'OTHER' ? 'dim' : ''}">${esc(fsName(cls))}${part.file_system && fsName(cls) !== part.file_system ? ` <span class="dim">${esc(part.file_system)}</span>` : ''}</td>
      <td class="num">${esc(fmtSize(part.size_bytes))}</td>
      <td class="dim">${esc(partTypeText(part))}</td>
      <td class="dim">${esc(partStatusText(part))}</td>
    </tr>`;
  }).join('');

  $$('#gridBody tr[data-part]').forEach((row) => {
    row.addEventListener('click', () => {
      selectPart(disk.disk_number, Number(row.dataset.part));
      notify();
    });
  });
}

/* 供安装向导第 4 步复用：某块盘会被清掉的分区说明。 */
export function lostPartitionsText(disk) {
  const parts = (disk && disk.partitions) || [];
  if (!parts.length) return '这块盘上本来就没有分区';
  return parts.map((part) => `${part.drive_letter ? `${part.drive_letter}: ` : ''}${partLabel(part)}（${fmtSize(part.size_bytes)}）`).join('、');
}

export function jobProgressText(job) {
  const progress = job && job.progress;
  if (!progress) return '';
  const percent = Number(progress.percent) || 0;
  return `${percent}% · ${progress.message || jobStatusText(job.status)}`;
}
