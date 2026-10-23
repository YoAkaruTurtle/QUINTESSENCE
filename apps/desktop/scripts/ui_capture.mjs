/**
 * Drive the running desktop Consult window over the WebView2 DevTools
 * protocol, capture the requested state and write 880x560, 1280x800 and
 * 1600x1000 screenshots.
 *
 * The remote-debugging port is enabled only by the verification environment
 * (`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS`); it is not a production feature of
 * the application and no test hook lives in the packaged binary.
 */

import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const result = {
    port: 9223,
    outDir: "",
    scenario: "answered",
    sizes: "880x560,1280x800,1600x1000",
    question: "合成问题：请根据当前可披露材料给出三条建议。",
    scale: null,
    aiName: null,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === "--port") result.port = Number(value);
    else if (key === "--out-dir") result.outDir = value;
    else if (key === "--scenario") result.scenario = value;
    else if (key === "--sizes") result.sizes = value;
    else if (key === "--question") result.question = value;
    else if (key === "--scale") result.scale = Number(value);
    else if (key === "--ai-name") result.aiName = value;
    else continue;
    index += 1;
  }
  return result;
}

const args = parseArgs(process.argv.slice(2));
if (!args.outDir) {
  console.error("--out-dir is required");
  process.exit(2);
}
fs.mkdirSync(args.outDir, { recursive: true });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function findPage(port) {
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const pages = await response.json();
      const page = pages.find(
        (candidate) =>
          candidate.type === "page" &&
          typeof candidate.webSocketDebuggerUrl === "string",
      );
      if (page) {
        return page;
      }
    } catch {
      // The WebView2 debug endpoint is not listening yet.
    }
    await sleep(300);
  }
  throw new Error("WebView2 debug endpoint did not expose a page");
}

function connect(url) {
  const socket = new WebSocket(url);
  const pending = new Map();
  let nextId = 1;
  const events = [];
  const ready = new Promise((resolve, reject) => {
    socket.addEventListener("open", () => resolve());
    socket.addEventListener("error", () => reject(new Error("CDP socket error")));
  });
  socket.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) {
        reject(new Error(JSON.stringify(message.error)));
      } else {
        resolve(message.result);
      }
      return;
    }
    if (message.method) {
      events.push(message);
    }
  });
  const send = async (method, params = {}) => {
    const id = nextId++;
    const promise = new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
    });
    socket.send(JSON.stringify({ id, method, params }));
    return promise;
  };
  return { socket, send, events, ready };
}

async function evaluate(client, expression) {
  const result = await client.send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  });
  if (result.exceptionDetails) {
    throw new Error(
      `evaluation failed: ${result.exceptionDetails.text ?? "unknown"}`,
    );
  }
  return result.result?.value;
}

async function waitFor(client, expression, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await evaluate(client, expression);
    if (value) {
      return value;
    }
    await sleep(150);
  }
  throw new Error(`timed out waiting for ${expression}`);
}

async function resize(width, height, scale) {
  const physicalWidth = Math.round(width * scale);
  const physicalHeight = Math.round(height * scale);
  await new Promise((resolve, reject) => {
    const child = spawn(
      process.env.SystemRoot
        ? path.join(
            process.env.SystemRoot,
            "System32",
            "WindowsPowerShell",
            "v1.0",
            "powershell.exe",
          )
        : "powershell.exe",
      [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        path.join(here, "resize_window.ps1"),
        "-Width",
        String(physicalWidth),
        "-Height",
        String(physicalHeight),
      ],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
    let stderr = "";
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`resize failed (${code}): ${stderr}`));
    });
  });
  await sleep(450);
}

async function capture(client, file) {
  const result = await client.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
    captureBeyondViewport: false,
  });
  fs.writeFileSync(file, Buffer.from(result.data, "base64"));
}

function snapshotExpression() {
  return `(() => {
    const phase = document.getElementById('app')?.dataset.phase ?? null;
    const body = document.getElementById('main')?.innerText ?? '';
    return {
      phase,
      workspace: document.getElementById('app')?.dataset.workspace ?? null,
      hostReady: document.getElementById('app')?.dataset.hostReady ?? null,
      confirmed: document.getElementById('app')?.dataset.confirmed ?? null,
      title: document.title,
      body: body.slice(0, 4000),
      destination: document.getElementById('destination-chip')?.innerText ?? null,
      host: document.getElementById('host-indicator')?.innerText ?? null,
      source: document.getElementById('source-summary')?.innerText ?? null,
      reviewed: document.getElementById('open-drawer')?.innerText ?? null,
      actions: [...document.querySelectorAll('#actions button')].map((button) => button.innerText),
      drawerOpen: !document.getElementById('drawer')?.classList.contains('hidden'),
      drawer: document.getElementById('drawer-body')?.innerText?.slice(0, 3000) ?? '',
      viewport: { width: window.innerWidth, height: window.innerHeight, dpr: window.devicePixelRatio },
      phaseText: document.getElementById('status-announcer')?.innerText ?? null,
      imeGuard: document.body.dataset.imeGuard ?? null,
    };
  })()`;
}

async function setQuestion(client, question) {
  await evaluate(
    client,
    `(() => {
      const textarea = document.getElementById('question');
      if (!textarea) return false;
      textarea.focus();
      textarea.value = ${JSON.stringify(question)};
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()`,
  );
  await sleep(120);
}

async function click(client, selector) {
  await evaluate(
    client,
    `(() => {
      const node = document.querySelector(${JSON.stringify(selector)});
      if (!node) return false;
      node.click();
      return true;
    })()`,
  );
}

async function clickWhenPresent(client, selector, timeoutMs = 20_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const clicked = await evaluate(
      client,
      `(() => {
        const node = document.querySelector(${JSON.stringify(selector)});
        if (!node) return false;
        node.click();
        return true;
      })()`,
    );
    if (clicked) {
      return true;
    }
    await sleep(120);
  }
  return false;
}

async function runScenario(client) {
  if (args.scenario === "settings") {
    await click(client, "#settings-button");
    await waitFor(client, `!document.getElementById('drawer')?.classList.contains('hidden')`);
    await waitFor(client, `document.getElementById('ai-name') !== null`);
    return;
  }
  if (args.scenario === "review") {
    await evaluate(client, `document.querySelectorAll('.workspace-tab')[1]?.click()`);
    await waitFor(client, `document.getElementById('app')?.dataset.workspace === 'review'`);
    return;
  }
  if (args.scenario === "not-ready") {
    await waitFor(client, `document.getElementById('app')?.dataset.phase === 'not-ready'`);
    return;
  }
  if (args.scenario === "input") {
    await waitFor(client, `document.getElementById('app')?.dataset.phase === 'input'`);
    return;
  }
  if (args.scenario === "ime") {
    await waitFor(client, `document.getElementById('app')?.dataset.phase === 'input'`);
    const guard = await evaluate(
      client,
      `(() => {
        const textarea = document.getElementById('question');
        if (!textarea) return false;
        textarea.focus();
        textarea.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }));
        textarea.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: 'zhongwen' }));
        textarea.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Enter', ctrlKey: true, bubbles: true, cancelable: true,
        }));
        const during = document.getElementById('app')?.dataset.phase;
        textarea.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '中文输入' }));
        textarea.value = '中文输入法组合输入测试：请根据当前可披露材料给出三条建议。';
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        document.body.dataset.imeGuard = during === 'input' ? 'true' : 'false';
        return during === 'input';
      })()`,
    );
    if (!guard) {
      throw new Error("Enter during IME composition started a consult");
    }
    return;
  }
  await waitFor(client, `document.getElementById('app')?.dataset.phase === 'input'`);
  await setQuestion(client, args.question);
  await click(client, "#action-send");
  await sleep(200);
  if (args.scenario === "confirm") {
    await waitFor(
      client,
      `!document.getElementById('confirm-banner')?.classList.contains('hidden')`,
    );
    return;
  }
  await clickWhenPresent(client, "#confirm-send", 5_000);
  const expected =
    args.scenario === "answered"
      ? "answered"
      : args.scenario === "waiting"
        ? "waiting"
        : args.scenario === "failed"
          ? "failed"
          : args.scenario === "incomplete"
            ? "incomplete"
            : args.scenario;
  await waitFor(
    client,
    `document.getElementById('app')?.dataset.phase === ${JSON.stringify(expected)}`,
    90_000,
  );
}

async function applyAiName(client, name) {
  await click(client, "#settings-button");
  await waitFor(client, `document.getElementById('ai-name') !== null`);
  await evaluate(
    client,
    `(() => {
      const input = document.getElementById('ai-name');
      input.value = ${JSON.stringify(name)};
      input.dispatchEvent(new Event('input', { bubbles: true }));
      document.getElementById('save-ai-name').click();
      return true;
    })()`,
  );
  await waitFor(
    client,
    `(() => {
       const toast = document.getElementById('toast');
       return toast && !toast.classList.contains('hidden') &&
         toast.innerText.includes(${JSON.stringify(name)});
     })()`,
    20_000,
  );
  await sleep(250);
}

async function main() {
  const page = await findPage(args.port);
  const client = connect(page.webSocketDebuggerUrl);
  await client.ready;
  await client.send("Page.enable");
  await client.send("Runtime.enable");
  await client.send("Log.enable");
  if (args.aiName) {
    await waitFor(
      client,
      `document.getElementById('app')?.dataset.phase !== 'booting'`,
    );
    await applyAiName(client, args.aiName);
    if (args.scenario !== "settings") {
      await click(client, "#drawer-close");
      await sleep(200);
    }
  }
  await runScenario(client);

  const scale =
    args.scale ??
    (await evaluate(client, "window.devicePixelRatio")) ??
    1;
  const sizes = args.sizes.split(",").map((entry) => {
    const [width, height] = entry.split("x").map(Number);
    return { width, height };
  });

  const manifest = {
    scenario: args.scenario,
    page: { title: page.title, url: page.url },
    scale,
    screenshots: [],
    state: null,
    console: [],
  };

  for (const size of sizes) {
    await resize(size.width, size.height, scale);
    const file = path.join(
      args.outDir,
      `${args.scenario}-${size.width}x${size.height}.png`,
    );
    await capture(client, file);
    const state = await evaluate(client, snapshotExpression());
    manifest.screenshots.push({
      file: path.basename(file),
      requested: `${size.width}x${size.height}`,
      viewport: state.viewport,
      phase: state.phase,
    });
    manifest.state = state;
    if (args.scenario === "answered" || args.scenario === "waiting") {
      // Also capture the source drawer for the source-summary evidence.
      await click(client, "#open-drawer");
      await sleep(250);
      const drawerFile = path.join(
        args.outDir,
        `${args.scenario}-${size.width}x${size.height}-drawer.png`,
      );
      await capture(client, drawerFile);
      manifest.screenshots.push({
        file: path.basename(drawerFile),
        requested: `${size.width}x${size.height}`,
        drawer: true,
      });
      await click(client, "#drawer-close");
      await sleep(150);
    }
  }

  for (const event of client.events) {
    if (event.method === "Runtime.consoleAPICalled") {
      manifest.console.push({
        type: event.params.type,
        text: (event.params.args ?? [])
          .map((arg) => arg.value ?? arg.description ?? "")
          .join(" ")
          .slice(0, 500),
      });
    }
    if (event.method === "Log.entryAdded") {
      manifest.console.push({
        level: event.params.entry.level,
        text: String(event.params.entry.text ?? "").slice(0, 500),
      });
    }
  }

  fs.writeFileSync(
    path.join(args.outDir, `${args.scenario}-state.json`),
    JSON.stringify(manifest, null, 2),
    "utf8",
  );
  console.log(JSON.stringify(manifest.state, null, 2));
  if (args.scenario === "settings") {
    await click(client, "#drawer-close");
  }
  client.socket.close();
}

main().catch((error) => {
  console.error(error.stack ?? String(error));
  process.exit(1);
});
