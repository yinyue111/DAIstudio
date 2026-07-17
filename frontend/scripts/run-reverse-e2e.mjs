import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const frontendDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const nextEnvPath = resolve(frontendDir, "next-env.d.ts");
const originalNextEnv = readFileSync(nextEnvPath);
const playwrightCli = require.resolve("@playwright/test/cli");
let child = null;
let restored = false;

function restoreNextEnv() {
  if (restored) return;
  writeFileSync(nextEnvPath, originalNextEnv);
  restored = true;
}

function forwardSignal(signal) {
  if (child && !child.killed) child.kill(signal);
}

const signalHandlers = new Map(
  ["SIGINT", "SIGTERM"].map((signal) => [signal, () => forwardSignal(signal)]),
);
for (const [signal, handler] of signalHandlers) process.on(signal, handler);
process.once("exit", restoreNextEnv);

try {
  child = spawn(
    process.execPath,
    [playwrightCli, "test", "e2e/reverse-operations.spec.ts", ...process.argv.slice(2)],
    { cwd: frontendDir, stdio: "inherit", env: process.env },
  );
  const outcome = await new Promise((resolveOutcome, reject) => {
    child.once("error", reject);
    child.once("exit", (code, signal) => resolveOutcome({ code, signal }));
  });
  process.exitCode = outcome.code ?? (outcome.signal ? 1 : 0);
} finally {
  restoreNextEnv();
  for (const [signal, handler] of signalHandlers) process.off(signal, handler);
}
