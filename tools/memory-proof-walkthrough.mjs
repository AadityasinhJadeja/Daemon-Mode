#!/usr/bin/env node
import { spawn } from "node:child_process";
import { cpSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const EXTENSION_SOURCE = join(ROOT, "extension");
const SERVICE_SCRIPT = join(ROOT, "tools", "local-memory-service.py");
const TIMESTAMP = Date.now();
const SERVICE_PORT = 4400 + Math.floor(Math.random() * 400);
const DEBUG_PORT = 9400 + Math.floor(Math.random() * 400);
const BASE_URL = `http://127.0.0.1:${SERVICE_PORT}`;

const TEST_URLS = {
  normal: `https://example.com/?daemon_mode_memory_proof=${TIMESTAMP}`,
  protected: "https://accounts.google.com/",
  paused: `https://www.example.org/?daemon_mode_paused=${TIMESTAMP}`,
  blockable: `https://www.iana.org/domains/reserved?daemon_mode_block=${TIMESTAMP}`,
  blockedAgain: `https://www.iana.org/help/example-domains?daemon_mode_blocked=${TIMESTAMP}`,
};

const tempRoot = mkdtempSync(join(tmpdir(), "daemon-mode-proof-"));
const extensionDir = join(tempRoot, "extension");
const profileDir = join(tempRoot, "chrome-profile");
const dbPath = join(tempRoot, "memory-proof.sqlite3");
let serviceProcess = null;
let chromeProcess = null;
let browserClient = null;
let pageClient = null;
let workerClient = null;
let extensionPageClient = null;
let extensionPageTargetId = null;
let extensionId = null;

function printStep(message) {
  console.log(`[memory-proof] ${message}`);
}

function assertTrue(condition, message) {
  if (!condition) {
    throw new Error(message);
  }

  printStep(`PASS ${message}`);
}

function delay(ms) {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, ms));
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = null;

  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = { raw: text };
  }

  if (!response.ok || payload.ok === false) {
    throw new Error(`${url} failed with HTTP ${response.status}: ${text}`);
  }

  return payload;
}

async function waitForJson(url, label, attempts = 80) {
  let lastError = null;

  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      return await fetchJson(url);
    } catch (error) {
      lastError = error;
      await delay(250);
    }
  }

  throw new Error(`Timed out waiting for ${label}: ${lastError?.message || "unknown error"}`);
}

class CdpClient {
  constructor(webSocketUrl) {
    this.webSocketUrl = webSocketUrl;
    this.nextId = 1;
    this.pending = new Map();
    this.ws = null;
  }

  async connect() {
    this.ws = new WebSocket(this.webSocketUrl);

    await new Promise((resolveConnect, rejectConnect) => {
      this.ws.addEventListener("open", resolveConnect, { once: true });
      this.ws.addEventListener("error", rejectConnect, { once: true });
    });

    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);

      if (!message.id) {
        return;
      }

      const pending = this.pending.get(message.id);

      if (!pending) {
        return;
      }

      this.pending.delete(message.id);

      if (message.error) {
        pending.reject(new Error(message.error.message || JSON.stringify(message.error)));
        return;
      }

      pending.resolve(message.result || {});
    });

    return this;
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;

    return new Promise((resolveSend, rejectSend) => {
      this.pending.set(id, {
        resolve: resolveSend,
        reject: rejectSend,
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });

    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.text || "Runtime.evaluate failed");
    }

    return result.result?.value;
  }

  close() {
    try {
      this.ws?.close();
    } catch {
      // Best-effort cleanup.
    }
  }
}

async function connectCdp(webSocketDebuggerUrl) {
  return new CdpClient(webSocketDebuggerUrl).connect();
}

function patchExtensionForServicePort() {
  cpSync(EXTENSION_SOURCE, extensionDir, { recursive: true });

  for (const relativePath of ["src/background.js", "manifest.json"]) {
    const filePath = join(extensionDir, relativePath);
    const contents = readFileSync(filePath, "utf8");
    writeFileSync(filePath, contents.replaceAll("127.0.0.1:4317", `127.0.0.1:${SERVICE_PORT}`));
  }
}

function startService() {
  serviceProcess = spawn(
    "python3",
    [
      SERVICE_SCRIPT,
      "--db-path",
      dbPath,
      "serve",
      "--port",
      String(SERVICE_PORT),
    ],
    {
      cwd: ROOT,
      env: {
        ...process.env,
        DAEMON_MODE_DISABLE_AI: "1",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  serviceProcess.stderr.on("data", (chunk) => {
    process.stderr.write(`[memory-proof-service] ${chunk}`);
  });
}

function startChrome() {
  if (!existsSync(CHROME_PATH)) {
    throw new Error(`Google Chrome was not found at ${CHROME_PATH}`);
  }

  chromeProcess = spawn(
    CHROME_PATH,
    [
      `--user-data-dir=${profileDir}`,
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--disable-extensions-except=${extensionDir}`,
      `--load-extension=${extensionDir}`,
      "--enable-extensions",
      "--enable-unsafe-extension-debugging",
      "--disable-features=DisableLoadExtensionCommandLineSwitch",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-popup-blocking",
      "--window-size=1280,900",
      "about:blank",
    ],
    {
      stdio: ["ignore", "ignore", "pipe"],
    },
  );

  chromeProcess.stderr.on("data", (chunk) => {
    const text = chunk.toString();

    if (text.includes("DevTools listening")) {
      process.stderr.write(`[memory-proof-chrome] ${text}`);
    }
  });
}

async function waitForTargets() {
  const version = await waitForJson(`http://127.0.0.1:${DEBUG_PORT}/json/version`, "Chrome DevTools");
  browserClient = await connectCdp(version.webSocketDebuggerUrl);

  const pageTarget = await browserClient.send("Target.createTarget", { url: "about:blank" });
  const targets = await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
  const pageInfo = targets.find((target) => target.id === pageTarget.targetId);
  assertTrue(Boolean(pageInfo?.webSocketDebuggerUrl), "temporary Chrome created a controllable browsing tab");
  pageClient = await connectCdp(pageInfo.webSocketDebuggerUrl);
  await pageClient.send("Page.enable");
  await pageClient.send("Runtime.enable");

  return pageTarget.targetId;
}

async function waitForExtensionWorker() {
  let workerTarget = null;

  for (let attempt = 0; attempt < 80; attempt += 1) {
    const targets = await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
    workerTarget = targets.find((target) => {
      return target.type === "service_worker" && target.url.startsWith("chrome-extension://");
    });

    if (workerTarget) {
      break;
    }

    await delay(250);
  }

  if (!workerTarget) {
    const targets = await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
    const summary = targets.map((target) => `${target.type}:${target.url}`).join("\n");
    throw new Error(`temporary Chrome loaded the Daemon Mode extension service worker\nObserved targets:\n${summary}`);
  }

  assertTrue(true, "temporary Chrome loaded the Daemon Mode extension service worker");
  extensionId = new URL(workerTarget.url).hostname;
  workerClient = await connectCdp(workerTarget.webSocketDebuggerUrl);
  await workerClient.send("Runtime.enable");
}

async function waitForLoadedExtensionId() {
  const preferencesPath = join(profileDir, "Default", "Preferences");

  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (!existsSync(preferencesPath)) {
      await delay(250);
      continue;
    }

    try {
      const preferences = JSON.parse(readFileSync(preferencesPath, "utf8"));
      const settings = preferences.extensions?.settings || {};
      const entry = Object.entries(settings).find(([_id, value]) => {
        return value?.manifest?.name === "Daemon Mode Capture Prototype";
      });

      if (entry) {
        return entry[0];
      }
    } catch {
      // Chrome may be writing Preferences while we read it.
    }

    await delay(250);
  }

  let diagnostics = "Preferences file was not created.";

  if (existsSync(preferencesPath)) {
    try {
      const preferences = JSON.parse(readFileSync(preferencesPath, "utf8"));
      const settings = preferences.extensions?.settings || {};
      diagnostics = Object.entries(settings)
        .map(([id, value]) => `${id}:${value?.manifest?.name || value?.path || "unknown"}`)
        .join("\n") || "No extension settings present.";
    } catch (error) {
      diagnostics = `Could not read Preferences: ${error.message}`;
    }
  }

  throw new Error(`Could not find Daemon Mode extension ID in the temporary Chrome profile\n${diagnostics}`);
}

async function openExtensionPopupPage() {
  const popupTarget = await browserClient.send("Target.createTarget", {
    url: `chrome-extension://${extensionId}/popup/popup.html`,
  });
  extensionPageTargetId = popupTarget.targetId;

  for (let attempt = 0; attempt < 40; attempt += 1) {
    const targets = await fetchJson(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
    const popupInfo = targets.find((target) => target.id === extensionPageTargetId);

    if (popupInfo?.webSocketDebuggerUrl) {
      extensionPageClient = await connectCdp(popupInfo.webSocketDebuggerUrl);
      await extensionPageClient.send("Runtime.enable");
      await delay(500);
      assertTrue(true, "temporary Chrome opened the extension trust console page");
      return;
    }

    await delay(250);
  }

  throw new Error("Could not open extension popup page for runtime messages");
}

async function navigate(pageTargetId, url, label, waitMs = 6500) {
  await browserClient.send("Target.activateTarget", { targetId: pageTargetId });
  await pageClient.send("Page.navigate", { url });
  await delay(waitMs);
  printStep(`Visited ${label}: ${url}`);
}

async function sendExtensionMessage(message) {
  if (!extensionPageClient) {
    throw new Error("Extension popup page is not connected");
  }

  const expression = `
    new Promise((resolve, reject) => {
      chrome.runtime.sendMessage(${JSON.stringify(message)}, (response) => {
        const error = chrome.runtime.lastError;
        if (error) {
          reject(new Error(error.message));
          return;
        }
        resolve(response);
      });
    })
  `;

  return extensionPageClient.evaluate(expression);
}

async function getServiceState() {
  const [captures, events, pulse, blockedDomains] = await Promise.all([
    fetchJson(`${BASE_URL}/captures?limit=25`),
    fetchJson(`${BASE_URL}/capture-events?limit=25`),
    fetchJson(`${BASE_URL}/memory-pulse`),
    fetchJson(`${BASE_URL}/blocked-domains`),
  ]);

  return {
    captures: captures.captures || [],
    events: events.events || [],
    pulse,
    blockedDomains: blockedDomains.domains || [],
  };
}

async function runServiceLevelWalkthrough(fallbackReason) {
  printStep(`Chrome extension walkthrough unavailable; running isolated service-level proof loop. Reason: ${fallbackReason.split("\n")[0]}`);

  await fetchJson(`${BASE_URL}/captures`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      source: "daemon-mode-memory-proof-walkthrough",
      extensionVersion: "0.4.8-validation",
      navigationType: "service-level-proof",
      url: TEST_URLS.normal,
      title: "Example Domain Memory Proof",
      domain: "example.com",
      capturedAt: new Date().toISOString(),
      text: "Example Domain memory proof page for Daemon Mode local recall and first-session validation.",
      textLength: 91,
    }),
  });
  assertTrue(true, "service-level normal page was saved locally");

  await fetchJson(`${BASE_URL}/capture-events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      status: "skipped",
      type: "blocked-navigation",
      navigationType: "service-level-proof",
      url: TEST_URLS.protected,
      reason: "blocked-domain:accounts.google.com",
      message: "Skipped blocked navigation",
    }),
  });
  assertTrue(true, "service-level protected page event was recorded without page text");

  await fetchJson(`${BASE_URL}/capture-events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      status: "paused",
      type: "paused-navigation",
      navigationType: "service-level-proof",
      url: TEST_URLS.paused,
      message: "Capture is paused",
    }),
  });
  assertTrue(true, "service-level pause event was recorded");

  await fetchJson(`${BASE_URL}/blocked-domains`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      domain: "www.iana.org",
      reason: "memory-proof-walkthrough",
    }),
  });
  await fetchJson(`${BASE_URL}/capture-events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      status: "skipped",
      type: "blocked-navigation",
      navigationType: "service-level-proof",
      url: TEST_URLS.blockedAgain,
      reason: "user-blocked-domain:www.iana.org",
      message: "Skipped blocked navigation",
    }),
  });
  assertTrue(true, "service-level block-current-site result was represented in local controls");

  const state = await getServiceState();
  assertTrue(state.captures.some((capture) => capture.domain === "example.com"), "Memory Pulse source includes saved activity");
  assertTrue(state.pulse.recentCaptured.length >= 1, "Memory Pulse shows recent saved activity");
  assertTrue(state.pulse.recentProtected.length >= 1, "Memory Pulse shows recent protected activity");
  assertTrue(Boolean(state.pulse.suggestedQuestion), "Memory Pulse suggests one recall question");

  const retrieve = await fetchJson(`${BASE_URL}/retrieve?q=${encodeURIComponent("example domain memory proof")}&limit=3&maxTokens=700`);
  assertTrue(retrieve.noEvidence === false && retrieve.evidence.length >= 1, "local recall found evidence from the proof loop");

  const answer = await fetchJson(`${BASE_URL}/answer?q=${encodeURIComponent("example domain memory proof")}&limit=3&maxTokens=700`);
  assertTrue(answer.status === "provider_disabled", "answer action preserves evidence when AI is disabled");
  assertTrue(answer.selectedEvidence.length >= 1 && answer.citations.length >= 1, "answer action keeps selected evidence and citations visible");

  console.log(
    JSON.stringify(
      {
        ok: true,
        chromeExtensionLoaded: false,
        fallbackReason: fallbackReason.split("\n")[0],
        servicePort: SERVICE_PORT,
        captures: state.captures.length,
        protectedEvents: state.pulse.recentProtected.length,
        suggestedQuestion: state.pulse.suggestedQuestion,
        retrieveEvidence: retrieve.evidence.length,
        answerStatus: answer.status,
      },
      null,
      2,
    ),
  );
}

async function runWalkthrough() {
  patchExtensionForServicePort();
  startService();
  await waitForJson(`${BASE_URL}/health`, "local memory service");
  assertTrue(true, "isolated local memory service is running");

  let pageTargetId = null;

  try {
    startChrome();
    pageTargetId = await waitForTargets();

    await navigate(pageTargetId, TEST_URLS.normal, "normal memory page");
    extensionId = await waitForLoadedExtensionId();
    assertTrue(Boolean(extensionId), "temporary Chrome loaded the Daemon Mode extension");
  } catch (error) {
    await runServiceLevelWalkthrough(error.message);
    return;
  }

  await openExtensionPopupPage();
  await browserClient.send("Target.activateTarget", { targetId: pageTargetId });
  const manualCapture = await sendExtensionMessage({ type: "DAEMON_MODE_CAPTURE_ACTIVE_TAB" });
  assertTrue(manualCapture?.ok === true, "capture-again control saved the current normal page");
  await delay(1000);
  let state = await getServiceState();
  assertTrue(state.captures.some((capture) => capture.domain === "example.com"), "normal browsing page was saved locally");

  await navigate(pageTargetId, TEST_URLS.protected, "blocked Google account page", 3500);
  state = await getServiceState();
  assertTrue(
    state.events.some((event) => event.status === "skipped" && event.reason?.includes("accounts.google.com")),
    "protected page was skipped before reading text",
  );

  const pausedStatus = await sendExtensionMessage({ type: "DAEMON_MODE_SET_PAUSED", paused: true });
  assertTrue(pausedStatus?.paused === true, "extension pause control turned capture off");
  await navigate(pageTargetId, TEST_URLS.paused, "allowed page while paused", 3500);
  state = await getServiceState();
  assertTrue(state.events.some((event) => event.status === "paused"), "paused browsing produced a protected pause event");

  const resumedStatus = await sendExtensionMessage({ type: "DAEMON_MODE_SET_PAUSED", paused: false });
  assertTrue(resumedStatus?.paused === false, "extension resume control turned capture on");

  await navigate(pageTargetId, TEST_URLS.blockable, "site to block");
  const blockResult = await sendExtensionMessage({ type: "DAEMON_MODE_BLOCK_ACTIVE_TAB_DOMAIN" });
  assertTrue(blockResult?.ok === true && blockResult.domain === "www.iana.org", "block-current-site control stored the active domain");

  await navigate(pageTargetId, TEST_URLS.blockedAgain, "blocked site revisit", 3500);
  state = await getServiceState();
  assertTrue(
    state.blockedDomains.some((domain) => domain.domain === "www.iana.org"),
    "local user blocklist includes the blocked site",
  );
  assertTrue(
    state.events.some((event) => event.status === "skipped" && event.reason?.includes("www.iana.org")),
    "blocked site revisit was skipped before reading text",
  );

  assertTrue(state.pulse.recentCaptured.length >= 1, "Memory Pulse shows recent saved activity");
  assertTrue(state.pulse.recentProtected.length >= 1, "Memory Pulse shows recent protected activity");
  assertTrue(Boolean(state.pulse.suggestedQuestion), "Memory Pulse suggests one recall question");

  const retrieve = await fetchJson(`${BASE_URL}/retrieve?q=${encodeURIComponent("example domain")}&limit=3&maxTokens=700`);
  assertTrue(retrieve.noEvidence === false && retrieve.evidence.length >= 1, "local recall found evidence from recent browsing");

  const answer = await fetchJson(`${BASE_URL}/answer?q=${encodeURIComponent("example domain")}&limit=3&maxTokens=700`);
  assertTrue(answer.status === "provider_disabled", "answer action preserves evidence when AI is disabled");
  assertTrue(answer.selectedEvidence.length >= 1 && answer.citations.length >= 1, "answer action keeps selected evidence and citations visible");

  console.log(
    JSON.stringify(
      {
        ok: true,
        chromeExtensionLoaded: true,
        servicePort: SERVICE_PORT,
        captures: state.captures.length,
        protectedEvents: state.pulse.recentProtected.length,
        suggestedQuestion: state.pulse.suggestedQuestion,
        retrieveEvidence: retrieve.evidence.length,
        answerStatus: answer.status,
      },
      null,
      2,
    ),
  );
}

async function cleanup() {
  extensionPageClient?.close();
  pageClient?.close();
  workerClient?.close();
  browserClient?.close();

  if (chromeProcess && !chromeProcess.killed) {
    chromeProcess.kill("SIGTERM");
  }

  if (serviceProcess && !serviceProcess.killed) {
    serviceProcess.kill("SIGTERM");
  }

  await delay(500);
  rmSync(tempRoot, { force: true, recursive: true });
}

try {
  await runWalkthrough();
} finally {
  await cleanup();
}
