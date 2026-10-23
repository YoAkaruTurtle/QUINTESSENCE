/**
 * Compile the DOM-free UI modules to a temporary CommonJS tree and run the
 * Node test files against them.  Nothing is written inside the checkout.
 */

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const app = path.resolve(here, "..");
const tsc = path.join(app, "node_modules", "typescript", "bin", "tsc");
const tests = path.join(app, "tests", "frontend");
const build = fs.mkdtempSync(path.join(os.tmpdir(), "quintessence-frontend-tests-"));

function run(command, args, env) {
  const result = spawnSync(command, args, {
    cwd: app,
    env: { ...process.env, ...env },
    stdio: "inherit",
  });
  return result.status ?? 1;
}

let status = 0;
try {
  status = run(process.execPath, [
    tsc,
    "-p",
    "tsconfig.test.json",
    "--outDir",
    build,
  ]);
  if (status !== 0) {
    process.exitCode = status;
  } else {
    const testFiles = fs
      .readdirSync(tests)
      .filter((name) => name.endsWith(".test.mjs"))
      .map((name) => path.join(tests, name));
    status = run(
      process.execPath,
      ["--test", ...testFiles],
      { QUINTESSENCE_FRONTEND_BUILD: build },
    );
    process.exitCode = status;
  }
} finally {
  fs.rmSync(build, { recursive: true, force: true });
}
