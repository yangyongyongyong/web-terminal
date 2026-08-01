/**
 * 用本机 headless Chromium + CDP（真实时间，virtual-time 会截断 FileReader/createImageBitmap）
 * 跑 tests/fixtures/<fixture>.html（默认 keys-harness.html），把每个场景的结果
 * 按 `key=value` 打印给 Python 断言。用法: node tests/browser-keys.mjs [fixture.html]
 * 找不到浏览器时打印 SKIP 并以 0 退出（其它机器上不阻塞测试）。
 */
import { spawn } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = process.argv[2] || 'keys-harness.html';
const HARNESS = 'file://' + resolve(HERE, 'fixtures', FIXTURE);
const PORT = Number(process.env.WT_CDP_PORT || 9333);

function findChrome() {
  const envPath = process.env.WT_CHROME_BIN;
  if (envPath && existsSync(envPath)) return envPath;
  const cache = join(homedir(), 'Library', 'Caches', 'ms-playwright');
  if (existsSync(cache)) {
    for (const dir of readdirSync(cache)) {
      for (const rel of [
        ['chrome-headless-shell-mac-arm64', 'chrome-headless-shell'],
        ['chrome-headless-shell-mac-x64', 'chrome-headless-shell'],
        ['chrome-mac-arm64', 'Chromium.app', 'Contents', 'MacOS', 'Chromium'],
      ]) {
        const p = join(cache, dir, ...rel);
        if (existsSync(p)) return p;
      }
    }
  }
  for (const p of [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
  ]) if (existsSync(p)) return p;
  return null;
}

const bin = findChrome();
if (!bin) {
  console.log('SKIP=no headless chromium found');
  process.exit(0);
}

const chrome = spawn(bin, [
  '--headless', '--disable-gpu', '--no-first-run', '--allow-file-access-from-files',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=/tmp/wt-cdp-${process.pid}`, 'about:blank',
], { stdio: 'ignore' });

let ws = null;
try {
  let targets = [];
  for (let i = 0; i < 80; i++) {
    try {
      targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      if (targets.some(t => t.type === 'page')) break;
    } catch {}
    await sleep(150);
  }
  const page = targets.find(t => t.type === 'page');
  if (!page) throw new Error('CDP 没有可用 page target');

  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  let id = 0;
  const waiters = new Map();
  const events = [];
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); }
    else if (m.method) events.push(m);
  });
  const send = (method, params = {}) => new Promise((res) => {
    const i = ++id; waiters.set(i, res); ws.send(JSON.stringify({ id: i, method, params }));
  });

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: HARNESS });
  for (let i = 0; i < 100; i++) {
    if (events.some(e => e.method === 'Page.loadEventFired')) break;
    await sleep(50);
  }
  await sleep(200);
  const r = await send('Runtime.evaluate', { expression: 'window.__run()', awaitPromise: true, returnByValue: true });
  const exc = r.result?.exceptionDetails;
  if (exc) {
    console.log('ERR=' + JSON.stringify(exc).slice(0, 400));
    process.exitCode = 1;
  } else {
    for (const line of r.result?.result?.value || []) console.log(line);
  }
} catch (err) {
  console.log('ERR=' + String(err && err.message || err));
  process.exitCode = 1;
} finally {
  try { ws?.close(); } catch {}
  chrome.kill();
}
