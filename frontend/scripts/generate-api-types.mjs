import { spawnSync } from "node:child_process";
import { access, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import openapiTS, { astToString } from "openapi-typescript";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const root = resolve(frontend, "..");
const candidates =
  process.platform === "win32"
    ? [resolve(root, ".venv", "Scripts", "python.exe"), "python"]
    : [resolve(root, ".venv", "bin", "python"), "python3", "python"];
let python = candidates.at(-1);
for (const candidate of candidates) {
  try {
    await access(candidate, constants.X_OK);
    python = candidate;
    break;
  } catch {
    // Try the next interpreter; the venv is optional for tooling discovery.
  }
}

const source =
  "import json,sys; from app.config import Settings; from app.main import create_app; " +
  "s=Settings(environment='development', app_origin='http://localhost:5173', " +
  "database_url='postgresql+psycopg://emp_local:local-only-password@127.0.0.1:5433/emp_local'); " +
  "json.dump(create_app(s).openapi(),sys.stdout)";
const generated = spawnSync(python, ["-c", source], {
  cwd: resolve(root, "backend"),
  encoding: "utf8",
  windowsHide: true,
});
if (generated.error || generated.status !== 0) {
  process.stderr.write(generated.stderr || String(generated.error));
  process.exit(generated.status || 1);
}

const output = await openapiTS(JSON.parse(generated.stdout));
await writeFile(
  resolve(frontend, "src", "api.generated.ts"),
  astToString(output),
);
console.log("Updated src/api.generated.ts from the local FastAPI application.");
