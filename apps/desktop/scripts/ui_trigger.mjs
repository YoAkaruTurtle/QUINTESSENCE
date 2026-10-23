/** Send one Consult through the real UI and wait for a terminal state. */

const port = Number(process.argv[2] ?? 9227);
const question = process.argv[3] ?? "测量用的合成问题：请给出三条建议。";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const deadline = Date.now() + 30_000;
  let page = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      page = (await response.json()).find((candidate) => candidate.type === "page");
      if (page) break;
    } catch {
      // the debug endpoint can be briefly unavailable between app restarts
    }
    await sleep(150);
  }
  if (!page) throw new Error("no page");
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  const pending = new Map();
  let nextId = 1;
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve);
    socket.addEventListener("error", reject);
  });
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const { resolve } = pending.get(message.id);
    pending.delete(message.id);
    resolve(message.result);
  });
  const evaluate = async (expression) => {
    const id = nextId++;
    const promise = new Promise((resolve) => pending.set(id, { resolve }));
    socket.send(
      JSON.stringify({
        id,
        method: "Runtime.evaluate",
        params: { expression, returnByValue: true, userGesture: true },
      }),
    );
    const result = await promise;
    if (result.exceptionDetails) throw new Error("evaluation failed");
    return result.result?.value;
  };
  const waitFor = async (expression, timeout = 90_000) => {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (await evaluate(expression)) return;
      await sleep(100);
    }
    throw new Error(`timeout: ${expression}`);
  };

  await waitFor(`document.getElementById('app')?.dataset.phase === 'input'`);
  await evaluate(
    `(() => {
      const textarea = document.getElementById('question');
      textarea.focus();
      textarea.value = ${JSON.stringify(question)};
      textarea.dispatchEvent(new Event('input', { bubbles: true }));
      document.getElementById('action-send').click();
      return true;
    })()`,
  );
  await sleep(200);
  await evaluate(
    `(() => {
      const node = document.getElementById('confirm-send');
      if (node) node.click();
      return true;
    })()`,
  );
  await waitFor(
    `['answered','waiting','failed','incomplete','stale'].includes(
       document.getElementById('app')?.dataset.phase
     )`,
  );
  console.log(
    JSON.stringify({
      phase: await evaluate(`document.getElementById('app')?.dataset.phase`),
      text: await evaluate(`document.getElementById('main')?.innerText?.slice(0, 400)`),
    }),
  );
  socket.close();
}

main().catch((error) => {
  console.error(error.stack ?? String(error));
  process.exit(1);
});
