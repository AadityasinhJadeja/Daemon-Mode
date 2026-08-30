#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import {
  CAPTURE_CONSENT_VERSION,
  LOCAL_API_VERSION,
  captureReadiness,
  evaluateServiceHealth
} from "../extension/src/readiness.js";
import { resolveCaptureProtection } from "../extension/src/protection.js";

const root = new URL("../", import.meta.url);
const manifest = JSON.parse(await readFile(new URL("extension/manifest.json", root), "utf8"));
const backgroundSource = await readFile(new URL("extension/src/background.js", root), "utf8");
const popupSource = await readFile(new URL("extension/popup/popup.js", root), "utf8");

assert.equal(manifest.version, "0.5.10");
assert.equal(manifest.incognito, "not_allowed");
assert.deepEqual(manifest.host_permissions, ["http://127.0.0.1:4317/*"]);
assert.deepEqual(manifest.optional_host_permissions, ["http://*/*", "https://*/*"]);
assert.ok(manifest.permissions.includes("scripting"));
assert.ok(!manifest.permissions.includes("activeTab"));
assert.equal(manifest.content_scripts, undefined, "content script must not be statically injected before consent");
assert.match(popupSource, /chrome\.permissions\.request/);
assert.match(
  popupSource,
  /const SETUP_URL = "https:\/\/github\.com\/AadityasinhJadeja\/Daemon-Mode#install";/,
  "offline setup action must open the README install section"
);
assert.doesNotMatch(backgroundSource, /chrome\.permissions\.request/);

assert.deepEqual(
  captureReadiness({
    consentVersion: 0,
    hostAccessGranted: true,
    serviceHealth: {
      ok: true,
      apiVersion: 1,
      minExtensionApiVersion: 1,
      maxExtensionApiVersion: 1
    }
  }),
  { ready: false, reason: "consent-required" }
);
assert.equal(CAPTURE_CONSENT_VERSION, 1);
assert.equal(LOCAL_API_VERSION, 1);
assert.equal(evaluateServiceHealth({ ok: true, apiVersion: 1 }).compatible, false, "missing compatibility range fails closed");
assert.equal(
  evaluateServiceHealth({
    ok: true,
    apiVersion: 1,
    serviceVersion: "0.5.10",
    minExtensionApiVersion: 1,
    maxExtensionApiVersion: 1
  }).compatible,
  true
);
assert.equal(
  captureReadiness({
    consentVersion: 1,
    hostAccessGranted: false,
    serviceHealth: {
      ok: true,
      apiVersion: 1,
      minExtensionApiVersion: 1,
      maxExtensionApiVersion: 1
    }
  }).reason,
  "host-access-withheld"
);

const protectionCalls = [];
const overlapResult = await resolveCaptureProtection(
  "https://mail.google.com/mail/u/0/",
  { blocked: true, reason: "blocked-domain:mail.google.com" },
  "http://127.0.0.1:4317/blocklist/check",
  "http://127.0.0.1:4317/default-protection/check",
  async (url) => {
    protectionCalls.push(url);
    if (url.includes("/blocklist/check")) {
      return {
        ok: true,
        json: async () => ({ ok: true, blocked: true, reason: "user-blocked-domain:mail.google.com" })
      };
    }
    return {
      ok: true,
      json: async () => ({ ok: true, disabled: true })
    };
  }
);
assert.equal(overlapResult.blocked, true);
assert.equal(overlapResult.reason, "user-blocked-domain:mail.google.com");
assert.equal(protectionCalls.length, 1, "explicit user block wins before a disabled default can allow text read");

const state = {};
const effects = {
  fetches: 0,
  registrations: 0,
  tabQueries: 0,
  storageWrites: []
};
let installedListener = null;
let tabUpdatedListener = null;
let runtimeMessageListener = null;
let pageAccessGranted = false;
let registered = false;
let serviceAvailable = false;

const event = (capture) => ({
  addListener(listener) {
    if (capture) capture(listener);
  }
});

globalThis.fetch = async () => {
  effects.fetches += 1;
  if (!serviceAvailable) {
    throw new Error("pre-consent code must not contact the page or local service");
  }
  return {
    ok: true,
    json: async () => ({
      ok: true,
      serviceVersion: "0.5.10",
      apiVersion: 1,
      minExtensionApiVersion: 1,
      maxExtensionApiVersion: 1
    })
  };
};
globalThis.chrome = {
  action: {
    setBadgeText: async () => {},
    setBadgeBackgroundColor: async () => {},
    setBadgeTextColor: async () => {},
    setTitle: async () => {}
  },
  permissions: {
    contains: async () => pageAccessGranted,
    onAdded: event(),
    onRemoved: event()
  },
  runtime: {
    OnInstalledReason: { INSTALL: "install" },
    getManifest: () => manifest,
    onInstalled: event((listener) => { installedListener = listener; }),
    onMessage: event((listener) => { runtimeMessageListener = listener; }),
    onStartup: event()
  },
  scripting: {
    getRegisteredContentScripts: async () => registered ? [{ id: "daemon-mode-page-capture" }] : [],
    registerContentScripts: async () => {
      effects.registrations += 1;
      registered = true;
    },
    unregisterContentScripts: async () => { registered = false; }
  },
  storage: {
    local: {
      async get(keys) {
        const selected = Array.isArray(keys) ? keys : [keys];
        return Object.fromEntries(selected.filter((key) => key in state).map((key) => [key, state[key]]));
      },
      async set(values) {
        effects.storageWrites.push({ ...values });
        Object.assign(state, values);
      }
    }
  },
  tabs: {
    async query() {
      effects.tabQueries += 1;
      return [];
    },
    create: async () => {},
    onActivated: event(),
    onUpdated: event((listener) => { tabUpdatedListener = listener; }),
    sendMessage() {}
  },
  webNavigation: {
    onCompleted: event(),
    onHistoryStateUpdated: event(),
    onReferenceFragmentUpdated: event()
  },
  windows: {
    WINDOW_ID_NONE: -1,
    onFocusChanged: event()
  }
};

const backgroundUrl = new URL("extension/src/background.js", root);
backgroundUrl.searchParams.set("proof", String(Date.now()));
await import(backgroundUrl.href);
await new Promise((resolve) => setTimeout(resolve, 25));
assert.equal(effects.registrations, 0, "no dynamic content script is registered before consent");
assert.equal(effects.tabQueries, 0, "no active tab URL or title is requested before consent");
assert.equal(effects.fetches, 0, "no capture, protection, event, or service request runs before consent");
assert.ok(tabUpdatedListener, "tab update listener is registered");
tabUpdatedListener(7, { url: "https://private.example/path", status: "complete" }, { active: true });
await new Promise((resolve) => setTimeout(resolve, 25));
assert.equal(effects.tabQueries, 0, "pre-consent tab updates do not query page metadata");
assert.equal(effects.fetches, 0, "pre-consent tab updates do not trigger service or protection calls");

assert.ok(installedListener, "install/update lifecycle listener is registered");
effects.storageWrites.length = 0;
installedListener({ reason: "update", previousVersion: "0.5.9" });
await new Promise((resolve) => setTimeout(resolve, 25));
assert.equal(state["daemonMode.captureConsentVersion"], undefined, "legacy update does not invent consent");
assert.equal(effects.registrations, 0, "legacy update remains off until the owner consents");
assert.equal(effects.storageWrites.length, 0, "legacy update does not rewrite consent or pause state");

assert.ok(runtimeMessageListener, "runtime message listener is registered");
pageAccessGranted = true;
serviceAvailable = true;
const startedStatus = await new Promise((resolve) => {
  const keepChannelOpen = runtimeMessageListener(
    { type: "DAEMON_MODE_START_REMEMBERING", permissionGranted: true },
    {},
    resolve
  );
  assert.equal(keepChannelOpen, true);
});
assert.equal(startedStatus.justStarted, true);
assert.equal(state["daemonMode.captureConsentVersion"], 1, "Start remembering stores versioned consent");
assert.equal(effects.registrations, 1, "content script is registered only after consent and optional host access");

state["daemonMode.capturePaused"] = true;
effects.storageWrites.length = 0;
installedListener({ reason: "update", previousVersion: "0.5.10" });
await new Promise((resolve) => setTimeout(resolve, 25));
assert.equal(state["daemonMode.captureConsentVersion"], 1, "update preserves consent");
assert.equal(state["daemonMode.capturePaused"], true, "update preserves pause state");
assert.equal(effects.storageWrites.length, 0, "update does not rewrite consent or pause state");

registered = false;
effects.registrations = 0;
installedListener({ reason: "update", previousVersion: "0.5.10" });
installedListener({ reason: "update", previousVersion: "0.5.10" });
await new Promise((resolve) => setTimeout(resolve, 25));
assert.equal(registered, true, "concurrent update sync leaves the content script registered");
assert.equal(effects.registrations, 1, "concurrent update sync serializes content script registration");

console.log("PASS extension store readiness proof");
