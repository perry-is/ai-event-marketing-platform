import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

const root = fileURLToPath(new URL("../../", import.meta.url));
const python = resolve(
  root,
  process.platform === "win32"
    ? ".venv/Scripts/python.exe"
    : ".venv/bin/python",
);
const child = spawn(
  python,
  [
    "-m",
    "uvicorn",
    "tests.browser_server:app",
    "--host",
    "127.0.0.1",
    "--port",
    process.env.EMP_E2E_BACKEND_PORT ?? "8011",
    "--no-access-log",
  ],
  {
    cwd: resolve(root, "backend"),
    stdio: "inherit",
    env: { ...process.env, EMP_ENVIRONMENT: "test" },
  },
);
child.on("exit", (code) => process.exit(code ?? 1));
for (const signal of ["SIGINT", "SIGTERM"])
  process.on(signal, () => {
    child.kill(signal);
  });
