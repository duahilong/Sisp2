/*
 * 内核兜底提示（普通脚本，不是 ES 模块）。
 *
 * 为什么单独一个文件、且必须排在 main.js 之前：整个界面只有一个入口
 * `<script type="module" src="/static/js/main.js">`，而 ES 模块在 IE11 上
 * 解析都不解析——一行都不会执行。所以“界面跑不起来”这件事只能由一份
 * **ES5 普通脚本**来报：它先于模块执行，能在最坏的情况下把原因显示出来。
 *
 * 刻意只用 ES5：var / function / 字符串拼接，没有箭头函数、const、模板串。
 * tests/test_ui_assets.py::test_engine_guard_is_plain_script 会盯住这一点。
 *
 * 两种兜底：
 *   1. 当前内核明显不可能跑模块（IE 的 document.documentMode，或缺 fetch/Promise）；
 *   2. 内核看起来正常但 10 秒内主模块没有置位 window.__sispBooted
 *      （模块 404、语法不被支持、被安全软件拦掉都属于这一类）。
 */
(function () {
  'use strict';

  var WARN_ID = 'engineWarn';
  var BOOTED = '__sispBooted';
  var TIMEOUT_MS = 10000;

  function show(text) {
    if (document.getElementById(WARN_ID)) return;
    var box = document.createElement('div');
    box.id = WARN_ID;
    box.className = 'engine-warn';
    // 内联样式是兜底：样式表本身也可能因为同样的问题没加载上
    box.setAttribute(
      'style',
      'position:fixed;left:0;right:0;top:0;z-index:99999;padding:12px 16px;' +
        'background:#c0392b;color:#fff;font:13px/1.7 "Microsoft YaHei",sans-serif;' +
        'white-space:pre-wrap;box-shadow:0 2px 8px rgba(0,0,0,.3)'
    );
    box.appendChild(document.createTextNode(text));
    if (document.body) {
      document.body.appendChild(box);
    } else {
      document.addEventListener('DOMContentLoaded', function () {
        if (document.body) document.body.appendChild(box);
      });
    }
  }

  function moduleCapable() {
    if (document.documentMode) return false; // IE（含 IE11 模式）
    if (typeof window.fetch !== 'function') return false;
    if (typeof window.Promise !== 'function') return false;
    if (!('content' in document.createElement('template'))) return false; // 无模板元素 = 很老
    return true;
  }

  if (!moduleCapable()) {
    show(
      '当前内核跑不了这个界面：需要 Chromium 内核（Edge WebView2 / Chrome / Edge），' +
        'IE 与旧内核不支持。\n' +
        '· 桌面版：安装 Microsoft Edge WebView2 运行时后重新打开本程序。\n' +
        '· 浏览器版：用 Edge 或 Chrome 打开 http://127.0.0.1:8765/ ，不要用 IE。'
    );
    return;
  }

  window.setTimeout(function () {
    if (window[BOOTED]) return;
    show(
      '界面脚本没有启动成功（主模块没有执行）。\n' +
        '常见原因：内核过旧、脚本被安全软件拦截、或本地服务没有提供 /static/js/main.js。\n' +
        '先看 %APPDATA%\\Sisp\\logs\\app.log ；仍不行就改用 Edge 或 Chrome 打开 http://127.0.0.1:8765/ 。'
    );
  }, TIMEOUT_MS);
})();
