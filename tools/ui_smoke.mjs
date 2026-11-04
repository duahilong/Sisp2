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
        step2extra: JSON.parse(await evaluate(`JSON.stringify({
          clicked: ${added ? 'true' : 'false'},
          rows: document.querySelectorAll('#wzBody .szrow.extra').length,
          selects: document.querySelectorAll('#wzBody select[data-fs="extra"]').length,
          segs: document.querySelectorAll('#wzBody .wseg').length,
          error: (document.querySelector('#wzBody .errmsg') || {}).textContent || '',
          canAddMore: Boolean(document.querySelector('#wzBody button[data-add]')),
        })`)),
      });
      const removed = await evaluate(`(function () {
        const rm = document.querySelector('#wzBody button[data-remove]');
        if (!rm) return false;
        rm.click();
        return true;
      })()`);
      await sleep(200);
      summary.steps.push({
        step2extraRemoved: JSON.parse(await evaluate(`JSON.stringify({
          clicked: ${removed ? 'true' : 'false'},
          rows: document.querySelectorAll('#wzBody .szrow.extra').length,
        })`)),
      });

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
            planExtra: text.indexOf('额外数据分区') >= 0,
            planUnalloc: text.indexOf('未分配') >= 0,
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
      if (extra.selects !== 1) problems.push(`额外分区缺少文件系统选择（${extra.selects}）`);
      if (extra.segs < 4) problems.push(`分区条只有 ${extra.segs} 段（期望 C/D/E/未分配 至少 4 段）`);
      if (extra.error) problems.push(`加了额外分区后第 2 步报错：${extra.error}`);
      if (extra.canAddMore) problems.push('已经有一个额外分区了，➕ 按钮还应该禁用/隐藏');
    }
    const removedStep = summary.steps.find((step) => step.step2extraRemoved);
    if (removedStep && removedStep.step2extraRemoved.rows !== 0) {
      problems.push('点 ✕ 之后额外分区没有被删掉');
    }
    const step4 = (summary.steps.find((step) => step.step4) || {}).step4;
    if (step4 && !step4.error) {
      if (!step4.planExtra) problems.push('第 4 步计划表里没有“额外数据分区”（partition_plan 没传到界面）');
      if (!step4.planLetters) problems.push('第 4 步没有显示计划盘符');
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
