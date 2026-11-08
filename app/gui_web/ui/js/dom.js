/** DOM 小工具：选择器、转义、事件委托、可见性。 */

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const ESCAPES = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
};

/** 所有拼进 innerHTML 的外部文本都必须过这里。 */
export const esc = (value) => String(value === null || value === undefined ? '' : value)
  .replace(/[&<>"']/g, (ch) => ESCAPES[ch]);

export function on(root, type, selector, handler) {
  root.addEventListener(type, (event) => {
    const target = event.target.closest(selector);
    if (target && root.contains(target)) handler(event, target);
  });
}

export function toggle(node, visible) {
  if (node) node.style.display = visible ? '' : 'none';
}

/** 时钟字符串，日志行和进度页共用。 */
export function stamp(date = new Date()) {
  return [date.getHours(), date.getMinutes(), date.getSeconds()]
    .map((n) => String(n).padStart(2, '0'))
    .join(':');
}
