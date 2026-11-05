/**
 * 无依赖的界面冒烟测试：用 Chrome DevTools Protocol 打开界面、点几下、截图。
 *
 * 用法（先起服务）：
 *   python -m app.gui_web.main --serve --port 8901 --dry-run
 *   node build/ui_smoke.mjs --url http://127.0.0.1:8901/ --out build/shots
 *
 * 它会：
 *   1. 用 --headless=new 起一个临时 Chrome（独立 user-data-dir），跑完自动关掉；
 *   2. 打开页面，等首屏渲染完（标题栏磁盘数 / 分区树出现）；
 *   3. 依次切到 系统安装 → 选盘 → 分区大小 → 版本 → 确认，再切到 设置；
 *   4. 每屏截一张 png，最后把结果（含 JS 报错）打成一坨 JSON。
 *
 * 有 JS 异常 / 致命错误屏 / 关键节点为空 → 退出码 1。
 */

import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const args = process.argv.slice(2);
const argOf = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};

const URL_ = argOf('url', 'http://127.0.0.1:8901/');
const OUT = argOf('out', 'build/shots');
// 端口默认随机抽一个：固定端口一旦有残留的 headless Chrome 占着，就会连到上一轮的浏览器上。
const PORT = Number(argOf('port', String(9300 + Math.floor(Math.random() * 600))));
// 自动找一个 Chromium 内核的浏览器：开发机上通常是 Chrome，干净的测试机可能只有 Edge。
const BROWSER_CANDIDATES = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
];
const CHROME = argOf('chrome', BROWSER_CANDIDATES.find((path) => existsSync(path)) || BROWSER_CANDIDATES[0]);
const WIDTH = Number(argOf('width', '1440'));
const HEIGHT = Number(argOf('height', '900'));

mkdirSync(OUT, { recursive: true });
const profile = mkdtempSync(join(tmpdir(), 'sisp-ui-'));

class CDP {
  constructor(url) {
    this.nextId = 0;
    this.pending = new Map();
    this.listeners = [];
    this.ws = new WebSocket(url);
    this.ready = new Promise((resolve, reject) => {
      this.ws.addEventListener('open', () => resolve());
      this.ws.addEventListener('error', (event) => reject(new Error(`CDP 连接失败: ${event.message || ''}`)));
    });
    this.ws.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.id && this.pending.has(message.id)) {
        const { resolve, reject } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) reject(new Error(`${message.error.message} (${message.error.code})`));
        else resolve(message.result);
        return;
      }
      for (const listener of this.listeners) listener(message);
    });
  }

  send(method, params = {}, sessionId) {
    const id = ++this.nextId;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    this.ws.send(JSON.stringify(payload));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, 20000);
    });
  }

  on(listener) {
    this.listeners.push(listener);
  }

  close() {
    try {
      this.ws.close();
    } catch (ignored) {
      /* 关闭失败无所谓 */
    }
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForHttp(url, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch (ignored) {
      /* 还没起来 */
    }
    await sleep(200);
  }
  throw new Error(`等不到 ${url}`);
}

/**
 * 每块盘的容量（GB，1 位小数），直接问后端的 ``/api/disks``。
 *
 * 界面上的“总容量”是 ``fmtSize`` 的展示串（``1.82 TB`` / ``200.00 GB``），
 * ``parseFloat('1.82 TB')`` 只会得到 1.82 —— 在 ≥1 TB 的盘上，所有容量断言
 * 都会变成误报（VM 上那块 2 TB 测试盘就是这么把冒烟跑红的）。这里改用真实字节数。
 */
async function diskCapacityGb(baseUrl) {
  const response = await fetch(new URL('api/disks', baseUrl));
  if (!response.ok) throw new Error(`/api/disks 返回 ${response.status}`);
  const payload = await response.json();
  const map = new Map();
  for (const disk of (payload && payload.data) || []) {
    const bytes = Number(disk && disk.size_bytes) || 0;
    if (bytes > 0) map.set(Number(disk.disk_number), Math.round((bytes / (1024 ** 3)) * 10) / 10);
  }
  return map;
}

async function main() {
  const chrome = spawn(
    CHROME,
    [
      '--headless=new',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${profile}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--hide-scrollbars',
      `--window-size=${WIDTH},${HEIGHT}`,
      'about:blank',
    ],
    { stdio: 'ignore' },
  );

  const problems = [];
  // 容量一律按后端给的真实字节数算，不解析界面上的“1.82 TB”（见 diskCapacityGb）
  let capacities = new Map();
  try {
    capacities = await diskCapacityGb(URL_);
  } catch (error) {
    console.error(`读 /api/disks 失败：${error && error.message}`);
  }
  const capacityOf = (step) => {
    const gb = capacities.get(Number(step && step.diskNumber)) || 0;
    if (!(gb > 0)) {
      problems.push(`读不到磁盘 ${step && step.diskNumber} 的容量（/api/disks 里没有这块盘）`);
    }
    return gb;
  };
  // 浏览器起不来（路径不对/没装）时直接给一句人话，别甩一棵 spawn 堆栈。
  chrome.on('error', (error) => {
    console.error(JSON.stringify({
      ok: false,
      problems: [`起不来浏览器：${CHROME}（${error.message}）`],
    }, null, 2));
    process.exit(1);
  });
  let client;
  try {
    await waitForHttp(`http://127.0.0.1:${PORT}/json/version`);
    const target = await (
      await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent('about:blank')}`, { method: 'PUT' })
    ).json();
    client = new CDP(target.webSocketDebuggerUrl);
    await client.ready;

    client.on((message) => {
      if (message.method === 'Runtime.exceptionThrown') {
        const details = message.params.exceptionDetails;
        problems.push(`JS 异常: ${details.exception?.description || details.text}`);
      }
      if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
        problems.push(`console.error: ${message.params.args.map((item) => item.value ?? item.description).join(' ')}`);
      }
      if (message.method === 'Log.entryAdded' && message.params.entry.level === 'error') {
        problems.push(`浏览器日志: ${message.params.entry.text}`);
      }
    });

    await client.send('Page.enable');
    await client.send('Runtime.enable');
    await client.send('Log.enable');
    await client.send('Emulation.setDeviceMetricsOverride', {
      width: WIDTH, height: HEIGHT, deviceScaleFactor: 1, mobile: false,
    });

    const evaluate = async (expression) => {
      const result = await client.send('Runtime.evaluate', {
        expression, returnByValue: true, awaitPromise: true,
      });
      if (result.exceptionDetails) {
        throw new Error(`求值失败: ${expression}\n${result.exceptionDetails.text}`);
      }
      return result.result.value;
    };

    const shoot = async (name) => {
      const result = await client.send('Page.captureScreenshot', { format: 'png' });
      const bytes = Buffer.from(result.data, 'base64');
      writeFileSync(join(OUT, `${name}.png`), bytes);
      // 纯背景色的空白页只有几 KB；界面真画出来了才有几十上百 KB。
      if (bytes.length < 20000) problems.push(`截图疑似空白：${name}.png 只有 ${bytes.length} 字节`);
      return `${name}.png`;
    };

    await client.send('Page.navigate', { url: URL_ });

    // 等首屏：分区树里有节点，或者明确提示“没有磁盘”
    const deadline = Date.now() + 25000;
    let firstPaint = '';
    while (Date.now() < deadline) {
      firstPaint = await evaluate(`(function () {
        const tree = document.querySelector('#tree');
        const fatal = document.querySelector('#fatal');
        return JSON.stringify({
          ready: document.readyState,
          tree: tree ? tree.querySelectorAll('.tnode').length : -1,
          fatal: fatal && !fatal.hidden ? (fatal.innerText || '').slice(0, 200) : '',
          chipDisks: (document.querySelector('#chipDisks') || {}).textContent || '',
          rows: document.querySelectorAll('#gridBody tr').length,
          segments: document.querySelectorAll('#map .seg-piece').length,
          logs: document.querySelectorAll('#logBody div').length,
        });
      })()`);
      const state = JSON.parse(firstPaint);
      if (state.fatal) {
        problems.push(`致命错误屏: ${state.fatal}`);
        break;
      }
      if (state.ready === 'complete' && (state.tree > 0 || state.rows > 0)) break;
      await sleep(250);
    }

    const summary = { firstPaint: JSON.parse(firstPaint), shots: [], steps: [] };
    summary.shots.push(await shoot('01-main'));

    // 磁盘概览 → 点第一块可选磁盘，看主页面选中态
    const picked = await evaluate(`(function () {
      const card = document.querySelector('#overview .ocard[data-disk]');
      if (card) card.click();
      const disk = document.querySelector('#tree .tnode[data-disk]');
      if (disk) disk.click();
      return JSON.stringify({ card: !!card, disk: !!disk });
    })()`);
    summary.steps.push({ picked: JSON.parse(picked) });
    await sleep(200);
    summary.shots.push(await shoot('02-main-selected'));

    // 系统安装 → 第 1 步
    await evaluate(`document.querySelector('#navseg [data-nav="install"]').click(); true`);
    await sleep(400);
    summary.shots.push(await shoot('03-install-step1'));
    summary.steps.push({
      step1: JSON.parse(await evaluate(`JSON.stringify({
        targets: document.querySelectorAll('#wzBody .dsel').length,
        disabled: document.querySelectorAll('#wzBody .dsel[disabled]').length,
        next: (document.querySelector('#wzNext') || {}).textContent || '',
      })`)),
    });

    // 选中第一块可用磁盘 → 下一步
    const chose = await evaluate(`(function () {
      const pick = document.querySelector('#wzBody .dsel:not([disabled])');
      if (!pick) return false;
      pick.click();
      return true;
    })()`);
    if (chose) {
      await sleep(300);
      summary.shots.push(await shoot('04-install-step1-picked'));
      await evaluate(`document.querySelector('#wzNext').click(); true`);
      await sleep(400);
      summary.shots.push(await shoot('05-install-step2'));
      summary.steps.push({
        step2: JSON.parse(await evaluate(`JSON.stringify({
          cards: document.querySelectorAll('#wzBody .tgt').length,
          inputs: document.querySelectorAll('#wzBody .szrow input').length,
          next: (document.querySelector('#wzNext') || {}).textContent || '',
        })`)),
      });

      // 手动改 C：最后一个数据分区是 auto，它的预估必须跟着变，
      // 否则输入框里的 D 还是旧值、进度条右侧空一块（C + D ≠ 盘容量）。
      const manual = await evaluate(`(function () {
        const sysInput = document.querySelector('#wzBody .szrow input[data-size="sys"]');
        if (!sysInput) return JSON.stringify({ reason: 'no sys input' });
        sysInput.value = '116';
        sysInput.dispatchEvent(new Event('input', { bubbles: true }));
        const dataInput = document.querySelector('#wzBody .szrow input[data-size="data"]');
        const freeSeg = document.querySelector('#wzBody .wseg.free');
        const result = {
          diskNumber: Number((document.querySelector('#wzBody .tgt') || { dataset: {} }).dataset.disk) || 0,
          sysGb: Number(sysInput.value) || 0,
          dataGb: dataInput ? Number(dataInput.value) || 0 : 0,
          dataReadonly: Boolean(dataInput && dataInput.readOnly),
          hasFreeSeg: Boolean(freeSeg),
          err: String((document.querySelector('#wzBody .errmsg') || {}).textContent || ''),
        };
        return JSON.stringify(result);
      })()`);
      await sleep(200);
      // 截图留在“C=116”那一刻，再恢复成 120 给后面的加 E 断言用
      summary.shots.push(await shoot('05a-install-step2-manual'));
      summary.steps.push({ step2manual: JSON.parse(manual) });
      await evaluate(`(function () {
        const sysInput = document.querySelector('#wzBody .szrow input[data-size="sys"]');
        if (!sysInput) return false;
        sysInput.value = '120';
        sysInput.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
      })()`);
      await sleep(150);

      // 加一个额外数据分区（E）：➏ → 多一行 + 进度条多一段 + 能选文件系统，
      // 然后删掉，确认回到 C/D 两行——两个方向都要通，否则用户会卡在“加错了删不掉”。
      const added = await evaluate(`(function () {
        const add = document.querySelector('#wzBody button[data-add]');
        if (!add) return false;
        add.click();
        return true;
      })()`);
      await sleep(300);
      summary.shots.push(await shoot('05b-install-step2-extra'));
      summary.steps.push({
        step2extra: JSON.parse(await evaluate(`(function () {
          const row = document.querySelector('#wzBody .szrow.extra');
          const freeSeg = document.querySelector('#wzBody .wseg.free');
          const footText = String((document.querySelector('#wzBody [data-err]') || {}).textContent || '');
          const nameEl = row && row.querySelector('.szname');
          const inputs = Array.from(document.querySelectorAll('#wzBody .szrow input[data-size]'));
          const dataInput = inputs.find((el) => el.dataset.size === 'data');
          const extraInput = inputs.find((el) => el.dataset.size === 'extra');
          const dataGb = dataInput ? Number(dataInput.value) : 0;
          const extraGb = extraInput ? Number(extraInput.value) : 0;
          return JSON.stringify({
            clicked: ${added ? 'true' : 'false'},
            rows: document.querySelectorAll('#wzBody .szrow.extra').length,
            selects: document.querySelectorAll('#wzBody .szrow.extra select').length,
            segs: document.querySelectorAll('#wzBody .wseg').length,
            label: nameEl ? String(nameEl.textContent || '') : '',
            rowText: row ? String(row.innerText || '') : '',
            dataGb: dataGb,
            extraGb: extraGb,
            splitOk: dataGb > 0 && extraGb > 0 && Math.abs(dataGb - extraGb) <= 1.5,
            error: (document.querySelector('#wzBody .errmsg') || {}).textContent || '',
            canAddMore: Boolean(document.querySelector('#wzBody button[data-add]')),
            // 最后一个数据分区永远 auto：输入框只读、没有“剩余/未分配”段
            extraReadonly: Boolean(extraInput && extraInput.readOnly),
            dataReadonly: Boolean(dataInput && dataInput.readOnly),
            hasFreeSeg: Boolean(freeSeg),
            footText: footText,
          });
        })()`)),
      });
      // 有 E 时 D 是显式分区：改 D，E（auto）必须跟着变（只读框要被写回新值）
      const splitAuto = await evaluate(`(function () {
        const dataInput = document.querySelector('#wzBody .szrow input[data-size="data"]');
        const extraInput = document.querySelector('#wzBody .szrow.extra input[data-size="extra"]');
        if (!dataInput || !extraInput) return JSON.stringify({ reason: 'no inputs' });
        const beforeData = Number(dataInput.value) || 0;
        const beforeExtra = Number(extraInput.value) || 0;
        dataInput.value = '100';
        dataInput.dispatchEvent(new Event('input', { bubbles: true }));
        const sysGb = Number((document.querySelector('#wzBody .szrow input[data-size="sys"]') || {}).value) || 0;
        const result = {
          beforeData,
          beforeExtra,
          dataGb: Number(dataInput.value) || 0,
          extraGb: Number(extraInput.value) || 0,
          sysGb,
          diskNumber: Number((document.querySelector('#wzBody .tgt') || { dataset: {} }).dataset.disk) || 0,
          err: String((document.querySelector('#wzBody .errmsg') || {}).textContent || ''),
        };
        // 恢复，后面的断言按平分后的值走
        dataInput.value = String(beforeData);
        dataInput.dispatchEvent(new Event('input', { bubbles: true }));
        return JSON.stringify(result);
      })()`);
      await sleep(200);
      summary.steps.push({ step2splitAuto: JSON.parse(splitAuto) });

      // 快捷容量（100/120/150/200）：以前只算 C、D，E 保持原值，于是 C+D+E 会
      // 超过盘容量、界面报「超过磁盘容量」。现在走 derive.fitLayout，E 跟着重算。
      const preset = await evaluate(`(function () {
        const readValues = () => {
          const out = {};
          document.querySelectorAll('#wzBody .szrow input[data-size]').forEach((el) => {
            out[el.dataset.size] = Number(el.value);
          });
          return out;
        };
        const sum = (v) => Math.round(((v.sys || 0) + (v.data || 0) + (v.extra || 0)) * 10) / 10;
        const before = readValues();
        const button = document.querySelector('#wzBody .presets button[data-preset="200"]');
        if (button) button.click();
        const after = readValues();
        return JSON.stringify({
          clicked: Boolean(button),
          sysBefore: before.sys,
          sysAfter: after.sys,
          dataBefore: before.data,
          dataAfter: after.data,
          extraBefore: before.extra,
          extraAfter: after.extra,
          sumAfter: sum(after),
          diskNumber: Number((document.querySelector('#wzBody .tgt') || { dataset: {} }).dataset.disk) || 0,
          // “平分”允许 0.2 GB 的取整差：D 向下取整到 0.1，E 被写成精确的剩余量，
          // 两者本来就可能差 0.1（831.3 / 831.4），卡在 0.1 会被浮点误差判死。
          evenSplit: after.extra !== undefined && Math.abs(after.data - after.extra) <= 0.2,
          error: String((document.querySelector('#wzBody .errmsg') || {}).textContent || ''),
          nextDisabled: Boolean((document.querySelector('#wzNext') || {}).disabled),
        });
      })()`);
      await sleep(250);
      summary.shots.push(await shoot('05b1-install-step2-preset'));
      const presetResult = JSON.parse(preset);
      summary.steps.push({ step2preset: presetResult });
      if (!presetResult.clicked) problems.push('第 2 步找不到快捷容量按钮 200');
      const presetCapacityGb = capacityOf(presetResult);
      if (presetResult.sumAfter > presetCapacityGb + 0.5) {
        problems.push(`快捷容量 200 之后 C+D+E（${presetResult.sumAfter} GB）超过了盘容量 ${presetCapacityGb} GB`);
      }
      if (!presetResult.evenSplit) {
        problems.push(`快捷容量 200 之后 D(${presetResult.dataAfter}) 与 E(${presetResult.extraAfter}) 没有平分剩余容量`);
      }
      if (presetResult.error) problems.push(`快捷容量 200 之后出现错误提示: ${presetResult.error}`);
      if (presetResult.nextDisabled) problems.push('快捷容量 200 之后“下一步”被禁用（说明布局不合法）');

      const splitStep = (summary.steps.find((step) => step.step2splitAuto) || {}).step2splitAuto;
      if (splitStep && !splitStep.reason) {
        if (splitStep.dataGb !== 100) problems.push(`把 D 改成 100 没生效（${splitStep.dataGb}）`);
        if (Math.abs(splitStep.extraGb - splitStep.beforeExtra) < 0.5) {
          problems.push(`改 D 之后 E 没有跟着变（还是 ${splitStep.extraGb} GB，应等于剩余空间）`);
        }
        const sum = splitStep.sysGb + splitStep.dataGb + splitStep.extraGb + 0.3;
        const capacityGb = capacityOf(splitStep);
        if (Math.abs(sum - capacityGb) > 1) {
          problems.push(`改 D=100 之后 C+D+E 与总容量 ${capacityGb} GB 对不上（和=${sum.toFixed(1)}）`);
        }
        if (splitStep.err) problems.push(`改 D 之后出现错误提示: ${splitStep.err}`);
      }

      const manualStep = (summary.steps.find((step) => step.step2manual) || {}).step2manual;
      if (!manualStep) {
        problems.push('没采集到“手动改 C”之后的状态');
      } else if (!manualStep.reason) {
        if (!manualStep.dataReadonly) problems.push('没有 E 时 D 是吃满剩余的分区，输入框必须只读');
        if (manualStep.hasFreeSeg) problems.push('手动改 C 之后进度条不该出现“未分配”段');
        // C + D + 预留余量(0.3 GB) 应该就是总容量，允许 1 GB 的取整误差
        const sum = manualStep.sysGb + manualStep.dataGb + 0.3;
        const capacityGb = capacityOf(manualStep);
        if (Math.abs(sum - capacityGb) > 1) {
          problems.push(`手动把 C 改成 116 之后 C(${manualStep.sysGb}) + D(${manualStep.dataGb}) 与总容量 ${capacityGb} GB 对不上`);
        }
        if (manualStep.err) problems.push(`手动改 C 之后出现错误提示: ${manualStep.err}`);
      }

      // 最后一个数据分区永远 auto（不带 size=，由 diskpart 吃满），所以：
      //   - 有 E 时 E 的输入框只读，D 可编辑；
      //   - 进度条没有“未分配”段；
      //   - 脚注把最后那个分区写成“剩余（约 X）”。
      const step2Extra = summary.steps.find((item) => item.step2extra)?.step2extra;
      if (!step2Extra) {
        problems.push('没采集到第 2 步加 E 之后的状态');
      } else {
        if (!step2Extra.extraReadonly) problems.push('加 E 之后 E 的输入框必须是只读（它永远吃满剩余）');
        if (step2Extra.dataReadonly) problems.push('加 E 之后 D 必须是可编辑的（它是显式分区）');
        if (step2Extra.hasFreeSeg) problems.push('第 2 步进度条不该再有“未分配”段');
        if (!/剩余（约 /.test(step2Extra.footText)) {
          problems.push(`脚注应把最后一个分区写成“剩余（约 X）”，实际：${step2Extra.footText}`);
        }
      }

      // 两条分隔条：C|D 与 D|E。拖 D|E 只挪比例（总量不变），拖 C|D 到最右也不能超盘。
      const dragged = await evaluate(`(function () {
        const bar = document.querySelector('#wzBody .wzbar');
        if (!bar) return JSON.stringify({ reason: 'no bar' });
        const readValues = () => {
          const out = {};
          document.querySelectorAll('#wzBody .szrow input[data-size]').forEach((el) => {
            out[el.dataset.size] = Number(el.value);
          });
          return out;
        };
        const total = (values) => Math.round(((values.sys || 0) + (values.data || 0) + (values.extra || 0)) * 10) / 10;
        const rect = bar.getBoundingClientRect();
        const fire = (element, type, x) => element.dispatchEvent(
          new PointerEvent(type, { clientX: x, bubbles: true, cancelable: true }),
        );
        const dragTo = (grip, ratio) => {
          const x0 = rect.left + rect.width * ((parseFloat(grip.style.left) || 0) / 100);
          const x1 = rect.left + rect.width * ratio;
          fire(grip, 'pointerdown', x0);
          fire(document, 'pointermove', x1);
          fire(document, 'pointerup', x1);
        };
        const grips = Array.from(bar.querySelectorAll('.wgrip'));
        const sysGrip = grips.find((el) => el.dataset.grip === 'sys');
        const dataGrip = grips.find((el) => el.dataset.grip === 'data');
        const before = readValues();
        if (dataGrip) dragTo(dataGrip, 0.25);
        const afterSplit = readValues();
        // 再把 C|D 分隔条拖到最右：系统分区不能把盘撑爆
        let afterSys = afterSplit;
        if (sysGrip) {
          dragTo(sysGrip, 0.999);
          afterSys = readValues();
        }
        // 盘容量不在页面里解析：“1.82 TB”这种展示串换算会丢精度，只带回盘号，
        // 由 Node 侧按 /api/disks 的真实字节数算。
        const caption = String((document.querySelector('#wzBody .trest [data-total]') || {}).textContent || '');
        return JSON.stringify({
          grips: grips.length,
          hasSysGrip: Boolean(sysGrip),
          hasDataGrip: Boolean(dataGrip),
          before: before,
          afterSplit: afterSplit,
          afterSys: afterSys,
          splitChanged: before.data !== afterSplit.data || before.extra !== afterSplit.extra,
          splitTotalBefore: total(before),
          splitTotalAfter: total(afterSplit),
          sysTotalAfter: total(afterSys),
          diskNumber: Number((document.querySelector('#wzBody .tgt') || { dataset: {} }).dataset.disk) || 0,
          caption: caption,
        });
      })()`);
      summary.shots.push(await shoot('05b2-install-step2-drag'));
      summary.steps.push({ step2drag: JSON.parse(dragged) });

      const removed = await evaluate(`(function () {
        const rm = document.querySelector('#wzBody button[data-remove]');
        if (!rm) return false;
        rm.click();
        return true;
      })()`);
      await sleep(200);
      summary.steps.push({
        step2extraRemoved: JSON.parse(await evaluate(`(function () {
          const readValues = () => {
            const out = {};
            document.querySelectorAll('#wzBody .szrow input[data-size]').forEach((el) => {
              out[el.dataset.size] = Number(el.value);
            });
            return out;
          };
          const values = readValues();
          return JSON.stringify({
            clicked: ${removed ? 'true' : 'false'},
            rows: document.querySelectorAll('#wzBody .szrow.extra').length,
            sysGb: values.sys,
            dataGb: values.data,
            diskNumber: Number((document.querySelector('#wzBody .tgt') || { dataset: {} }).dataset.disk) || 0,
            // 删掉 E 之后 D 变回“吃满剩余”的 auto，输入框只读、数字是预估
            dataReadonly: Boolean(document.querySelector('#wzBody .szrow input[data-size="data"]')?.readOnly),
            error: String((document.querySelector('#wzBody .errmsg') || {}).textContent || ''),
          });
        })()`)),
      });
      const removedResult = summary.steps[summary.steps.length - 1].step2extraRemoved;
      if (removedResult.rows !== 0) problems.push('删掉 E 之后还留着 E 的输入行');
      if (!removedResult.dataReadonly) {
        problems.push('删掉 E 之后 D 变回“吃满剩余”，输入框必须是只读');
      }
      if (removedResult.error) problems.push(`删掉 E 之后报错: ${removedResult.error}`);
      summary.shots.push(await shoot('05b3-install-step2-extra-removed'));

      // 再加回去，让后面第 3、4 步和真实写盘都是“四个分区”的路径
      await evaluate(`(function () {
        const add = document.querySelector('#wzBody button[data-add]');
        if (add) add.click();
        return true;
      })()`);
      await sleep(250);
      summary.shots.push(await shoot('05c-install-step2-extra-again'));

      await evaluate(`document.querySelector('#wzNext').click(); true`);
      await sleep(600);
      summary.shots.push(await shoot('06-install-step3'));
      summary.steps.push({
        step3: JSON.parse(await evaluate(`JSON.stringify({
          selects: document.querySelectorAll('#wzBody select[data-image]').length,
          options: document.querySelectorAll('#wzBody select[data-image] option').length,
        })`)),
      });

      await evaluate(`document.querySelector('#wzNext').click(); true`);
      // 第 4 步要等 /api/prepare 回来（真实扫描 + 计划校验，可能要好几秒）
      let planReady = false;
      for (let i = 0; i < 100 && !planReady; i += 1) {
        await sleep(200);
        planReady = await evaluate(`Boolean(document.querySelector('#startBtn')) || Boolean(document.querySelector('#wzBody .warnbox'))`);
      }
      if (!planReady) problems.push('第 4 步没等到执行计划（20 秒内既没有 #startBtn 也没有错误提示）');
      await sleep(300);
      summary.shots.push(await shoot('07-install-step4'));
      summary.steps.push({
        step4: JSON.parse(await evaluate(`(function () {
          const body = document.querySelector('#wzBody') || { innerText: '' };
          const text = String(body.innerText || '');
          return JSON.stringify({
            rows: document.querySelectorAll('#wzBody .sum table tr').length,
            start: (document.querySelector('#startBtn') || {}).disabled,
            error: (document.querySelector('#wzBody .warnbox') || {}).innerText || '',
            planDataRows: (text.match(/数据分区/g) || []).length,
            planUnalloc: text.indexOf('未分配') >= 0,  // 永远是 false：最后一项吃满
            planAuto: text.indexOf('剩余空间（约') >= 0,
            planLetters: text.indexOf('盘符') >= 0,
            text: text.slice(0, 400),
          });
        })()`)),
      });
    }

    // 设置
    await evaluate(`document.querySelector('#navseg [data-nav="settings"]').click(); true`);
    await sleep(500);
    summary.shots.push(await shoot('08-settings'));
    summary.steps.push({
      settings: JSON.parse(await evaluate(`JSON.stringify({
        rows: document.querySelectorAll('#settingsBody .set-row').length,
        saves: (document.querySelector('#setSave') || {}).disabled,
        state: (document.querySelector('#setState') || {}).textContent || '',
      })`)),
    });

    // 操作菜单 → 修复引导弹窗
    await evaluate(`document.querySelector('#navseg [data-nav="main"]').click(); true`);
    await sleep(200);
    await evaluate(`(function () {
      const menu = document.querySelector('.menubar [data-menu="ops"]');
      if (menu) menu.click();
      return true;
    })()`);
    await sleep(150);
    summary.shots.push(await shoot('09-menu'));
    await evaluate(`(function () {
      const item = document.querySelector('.dd-item[data-act="boot"]');
      if (item) item.click();
      return true;
    })()`);
    await sleep(2500);
    summary.shots.push(await shoot('10-boot-dialog'));
    summary.steps.push({
      boot: JSON.parse(await evaluate(`JSON.stringify({
        modal: !!document.querySelector('#overlay .modal'),
        findings: document.querySelectorAll('#overlay .finding').length,
      })`)),
    });
    await evaluate(`(function () {
      const closer = document.querySelector('#overlay .modal [data-close]');
      if (closer) closer.click();
      return true;
    })()`);

    if (summary.firstPaint.segments === 0 && summary.firstPaint.tree === 0) {
      problems.push('主页面既没有分区块也没有磁盘节点');
    }
    if (summary.steps.some((step) => step.settings && step.settings.rows < 20)) {
      problems.push('设置页渲染的行数过少');
    }
    const extraStep = summary.steps.find((step) => step.step2extra);
    if (extraStep) {
      const extra = extraStep.step2extra;
      if (!extra.clicked) problems.push('第 2 步没有“➕ 添加数据分区”按钮');
      if (extra.rows !== 1) problems.push(`点 ➕ 之后额外分区行数为 ${extra.rows}（期望 1）`);
      if (extra.selects !== 0) problems.push('数据分区不应该再有文件系统选择器（固定跟 D 一致）');
      // 最后一个数据分区永远吃满，所以进度条只有 C / D / E 三段，没有“未分配”段
      if (extra.segs !== 3) problems.push(`分区条是 ${extra.segs} 段（期望 3：C/D/E，不再有未分配段）`);
      if (extra.error) problems.push(`加了数据分区后第 2 步报错：${extra.error}`);
      if (extra.canAddMore) problems.push('已经有一个数据分区了，➕ 按钮还应该禁用/隐藏');
      if (!String(extra.rowText).includes('数据分区')) problems.push('E 行的名字应该就是“数据分区”');
      if (String(extra.rowText).includes('额外')) problems.push('界面文案里还残留“额外”两个字');
      if (!extra.splitOk) {
        problems.push(`D/E 没有平分容量（D=${extra.dataGb} GB，E=${extra.extraGb} GB）`);
      }
    }
    const dragStep = (summary.steps.find((step) => step.step2drag) || {}).step2drag;
    if (dragStep) {
      if (dragStep.grips !== 2) problems.push(`加了数据分区后应该有 2 条可拖分隔条（实际 ${dragStep.grips}）`);
      if (!dragStep.hasDataGrip) problems.push('没有 D/E 之间的分隔条');
      if (!dragStep.splitChanged) problems.push('拖动 D/E 分隔条没有改变 D 与 E 的大小');
      if (Math.abs(dragStep.splitTotalBefore - dragStep.splitTotalAfter) > 1) {
        problems.push(`拖 D/E 分隔条改变了总容量（${dragStep.splitTotalBefore} → ${dragStep.splitTotalAfter} GB）`);
      }
      const dragCapacityGb = capacityOf(dragStep);
      if (dragCapacityGb > 0 && dragStep.sysTotalAfter > dragCapacityGb + 0.5) {
        problems.push(`拖动后总量 ${dragStep.sysTotalAfter} GB 超过了盘容量 ${dragCapacityGb} GB`);
      }
    }
    const removedStep = summary.steps.find((step) => step.step2extraRemoved);
    if (removedStep && removedStep.step2extraRemoved.rows !== 0) {
      problems.push('点 ✕ 之后额外分区没有被删掉');
    }
    const step4 = (summary.steps.find((step) => step.step4) || {}).step4;
    if (step4 && !step4.error) {
      if ((step4.planDataRows || 0) < 2) {
        problems.push(`第 4 步计划表里只有一个数据分区（${step4.planDataRows}，期望 2：D 和 E）`);
      }
      if (!step4.planLetters) problems.push('第 4 步没有显示计划盘符');
      if (step4.planUnalloc) problems.push('第 4 步不该出现“未分配”（最后一个分区永远吃满）');
      if (!step4.planAuto) problems.push('第 4 步应把最后一个分区显示为“剩余空间（约 X GB）”');
    }

    console.log(JSON.stringify({ ok: problems.length === 0, problems, ...summary }, null, 2));
    process.exitCode = problems.length === 0 ? 0 : 1;
  } finally {
    if (client) client.close();
    // Chrome 会拉一串子进程，只 kill 根进程会漏；Windows 上用 taskkill /T 收整棵树。
    try {
      if (process.platform === 'win32') spawnSync('taskkill', ['/PID', String(chrome.pid), '/T', '/F'], { stdio: 'ignore' });
      else chrome.kill('SIGKILL');
    } catch (ignored) {
      chrome.kill();
    }
    await sleep(300);
    try {
      rmSync(profile, { recursive: true, force: true });
    } catch (ignored) {
      /* Windows 上偶尔删不掉，留临时目录无所谓 */
    }
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, problems: [String(error && error.stack ? error.stack : error)] }));
  process.exitCode = 1;
});
