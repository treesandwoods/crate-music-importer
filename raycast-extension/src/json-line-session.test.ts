import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import test from "node:test";
import { jsonLineSession } from "./json-line-session";

function checker(script: string, controller = new AbortController()) {
  const child = spawn(process.execPath, ["-e", script], { stdio: "pipe" });
  return { child, controller, session: jsonLineSession(child, controller.signal) };
}

test("checker session accepts split stdout lines and sequential replies", async () => {
  const { session } = checker(`
    process.stdout.write('{"rea'); process.stdout.write('dy":true}\\n');
    require('node:readline').createInterface({input:process.stdin}).on('line', line => {
      process.stdout.write(JSON.stringify({echo:JSON.parse(line)})+'\\n');
    });
  `);
  try {
    assert.deepEqual(await session.read(), { ready: true });
    assert.deepEqual(await session.request({ id: "one" }), { echo: { id: "one" } });
    assert.deepEqual(await session.request({ id: "two" }), { echo: { id: "two" } });
  } finally {
    session.close();
  }
});

test("aborting a checker rejects the pending check and terminates the process", async () => {
  const { child, controller, session } = checker(`process.stdout.write('{"ready":true}\\n'); process.stdin.resume();`);
  await session.read();
  const stopped = new Promise((resolve) => child.once("close", resolve));
  const response = session.request({ id: "one" });
  controller.abort();
  await assert.rejects(response, /cancelled/);
  await stopped;
  await assert.rejects(session.request({ id: "two" }), /cancelled/);
});

test("startup errors and malformed replies reject without hanging", async () => {
  const failed = checker(`process.stderr.write('ERROR: Refresh Library Health'); process.exit(2);`);
  await assert.rejects(failed.session.read(), /Refresh Library Health/);
  const invalid = checker(`process.stdout.write('not JSON\\n'); process.stdin.resume();`);
  await assert.rejects(invalid.session.read(), /invalid JSON/);
});
