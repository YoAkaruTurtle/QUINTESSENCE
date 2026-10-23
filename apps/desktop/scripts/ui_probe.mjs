/** Wait until the running app leaves the booting phase and print its state. */

const port = Number(process.argv[2] ?? 9227);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const deadline = Date.now() + 60_000;
  let page = null;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const pages = await response.json();
      page = pages.find((candidate) => candidate.type === "page");
      if (page) break;
    } catch {
      // not listening yet
    }
    await sleep(100);
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
        params: { expression, returnByValue: true },
      }),
    );
    const result = await promise;
    return result.result?.value;
  };
  while (Date.now() < deadline) {
    const state = await evaluate(
      `({ phase: document.getElementById('app')?.dataset.phase,
          ready: document.getElementById('app')?.dataset.hostReady,
          dpr: window.devicePixelRatio,
          width: window.innerWidth, height: window.innerHeight })`,
    );
    if (state && state.phase !== "booting") {
      console.log(JSON.stringify(state));
      socket.close();
      return;
    }
    await sleep(80);
  }
  throw new Error("app did not leave booting");
}

main().catch((error) => {
  console.error(error.stack ?? String(error));
  process.exit(1);
});
