import { isBlockedUrl } from "./blocklist.js";
import { resolveCaptureProtection } from "./protection.js";
import {
  CAPTURE_CONSENT_VERSION,
  OPTIONAL_PAGE_ORIGINS,
  captureReadiness,
  evaluateServiceHealth,
  hasCurrentCaptureConsent
} from "./readiness.js";

const INGEST_ENDPOINT = "http://127.0.0.1:4317/captures";
const PORTAL_URL = "http://127.0.0.1:4317/";
const HEALTH_ENDPOINT = "http://127.0.0.1:4317/health";
const MEMORY_PULSE_ENDPOINT = "http://127.0.0.1:4317/memory-pulse";
const CAPTURE_EVENTS_ENDPOINT = "http://127.0.0.1:4317/capture-events";
const BLOCKED_DOMAINS_ENDPOINT = "http://127.0.0.1:4317/blocked-domains";
const USER_BLOCKLIST_CHECK_ENDPOINT = "http://127.0.0.1:4317/blocklist/check";
const DEFAULT_PROTECTION_CHECK_ENDPOINT = "http://127.0.0.1:4317/default-protection/check";
const CAPTURE_DEBOUNCE_MS = 750;
const CONTENT_SCRIPT_ID = "daemon-mode-page-capture";
const STATE_KEYS = {
  paused: "daemonMode.capturePaused",
  lastEvent: "daemonMode.lastEvent",
  consentVersion: "daemonMode.captureConsentVersion",
  permissionWithheld: "daemonMode.permissionWithheld"
};

const pendingCaptures = new Map();
let captureContentScriptSync = Promise.resolve(false);

function log(message, details = {}) {
  const safeDetails = {};
  for (const [key, value] of Object.entries(details)) {
    if (key.toLowerCase().includes("url")) {
      safeDetails[`${key}Domain`] = getDomain(value);
    } else {
      safeDetails[key] = value;
    }
  }
  console.log(`[Daemon Mode] ${message}`, safeDetails);
}

function storageGet(keys) {
  return chrome.storage.local.get(keys);
}

function storageSet(values) {
  return chrome.storage.local.set(values);
}

async function reconcilePermissionWithheld(permissionWithheld) {
  const state = await storageGet(STATE_KEYS.permissionWithheld);
  if (Boolean(state[STATE_KEYS.permissionWithheld]) === permissionWithheld) {
    return;
  }

  await storageSet({
    [STATE_KEYS.permissionWithheld]: permissionWithheld
  });
}

function getDomain(rawUrl) {
  try {
    return new URL(rawUrl).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function urlsMatch(firstUrl, secondUrl) {
  try {
    const first = new URL(firstUrl);
    const second = new URL(secondUrl);
    first.hash = "";
    second.hash = "";
    return first.toString() === second.toString();
  } catch {
    return firstUrl === secondUrl;
  }
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({
    active: true,
    currentWindow: true
  });

  return tab ?? null;
}

async function checkServiceHealth() {
  try {
    const response = await fetch(HEALTH_ENDPOINT);

    if (!response.ok) {
      return {
        connected: false,
        status: "service-unavailable",
        message: `Local service returned HTTP ${response.status}`
      };
    }

    const payload = await response.json();
    const compatibility = evaluateServiceHealth(payload);
    return {
      connected: Boolean(payload.ok),
      compatible: compatibility.compatible,
      status: compatibility.compatible ? "connected" : compatibility.reason,
      message: compatibility.compatible
        ? "Memory service connected"
        : "Update Daemon's local service before capture can continue",
      dbPath: payload.dbPath,
      apiVersion: compatibility.apiVersion,
      serviceVersion: compatibility.serviceVersion,
      minExtensionApiVersion: Number(payload.minExtensionApiVersion),
      maxExtensionApiVersion: Number(payload.maxExtensionApiVersion)
    };
  } catch (error) {
    return {
      connected: false,
      status: "service-off",
      message: "Local service not running",
      error: error.message
    };
  }
}

async function hasCaptureConsent() {
  const state = await storageGet(STATE_KEYS.consentVersion);
  return hasCurrentCaptureConsent(state[STATE_KEYS.consentVersion]);
}

function containsOptionalPageAccess() {
  return chrome.permissions.contains({
    origins: OPTIONAL_PAGE_ORIGINS
  });
}

async function unregisterCaptureContentScript() {
  try {
    await chrome.scripting.unregisterContentScripts({ ids: [CONTENT_SCRIPT_ID] });
  } catch {
    // The script may not be registered yet.
  }
}

async function performCaptureContentScriptSync() {
  const consented = await hasCaptureConsent();
  const hostAccessGranted = consented ? await containsOptionalPageAccess() : false;

  if (!consented) {
    await unregisterCaptureContentScript();
    return false;
  }

  if (!hostAccessGranted) {
    await reconcilePermissionWithheld(true);
    await unregisterCaptureContentScript();
    return false;
  }

  // Chrome's permission state is authoritative. A previously stored
  // permission-withheld flag can become stale after browser/profile changes.
  await reconcilePermissionWithheld(false);

  const registrations = await chrome.scripting.getRegisteredContentScripts({ ids: [CONTENT_SCRIPT_ID] });
  if (!registrations.length) {
    try {
      await chrome.scripting.registerContentScripts([
        {
          id: CONTENT_SCRIPT_ID,
          matches: OPTIONAL_PAGE_ORIGINS,
          js: ["src/content.js"],
          runAt: "document_idle",
          persistAcrossSessions: true
        }
      ]);
    } catch (error) {
      // Startup and onInstalled can run close together after an unpacked
      // extension reload. If another sync won the registration race, keep it.
      const registrationsAfterError = await chrome.scripting.getRegisteredContentScripts({ ids: [CONTENT_SCRIPT_ID] });
      if (registrationsAfterError.length) {
        return true;
      }
      log("Content script registration failed", { error: error.message });
      return false;
    }
  }

  return true;
}

function syncCaptureContentScript() {
  const nextSync = captureContentScriptSync
    .catch(() => false)
    .then(performCaptureContentScriptSync);
  captureContentScriptSync = nextSync;
  return nextSync;
}

async function getCaptureGate() {
  const state = await storageGet([STATE_KEYS.consentVersion, STATE_KEYS.permissionWithheld]);
  const consentVersion = state[STATE_KEYS.consentVersion];
  if (!hasCurrentCaptureConsent(consentVersion)) {
    return {
      ready: false,
      reason: "consent-required",
      consentVersion,
      hostAccessGranted: false,
      service: null
    };
  }

  const hostAccessGranted = await containsOptionalPageAccess();
  await reconcilePermissionWithheld(!hostAccessGranted);
  if (!hostAccessGranted) {
    return {
      ready: false,
      reason: "host-access-withheld",
      consentVersion,
      hostAccessGranted,
      service: null
    };
  }

  const service = await checkServiceHealth();
  const readiness = captureReadiness({
    consentVersion,
    hostAccessGranted,
    serviceHealth: {
      ok: service.connected,
      apiVersion: service.apiVersion,
      serviceVersion: service.serviceVersion,
      minExtensionApiVersion: service.minExtensionApiVersion,
      maxExtensionApiVersion: service.maxExtensionApiVersion
    }
  });
  return {
    ...readiness,
    consentVersion,
    hostAccessGranted,
    service
  };
}

async function getTodayCaptureCount() {
  try {
    const response = await fetch(MEMORY_PULSE_ENDPOINT);

    if (!response.ok) {
      return 0;
    }

    const payload = await response.json();
    return Number(payload?.stats?.today_capture_count || 0);
  } catch {
    return 0;
  }
}

function formatBadgeCount(count) {
  if (!count) {
    return "";
  }

  if (count > 99) {
    return "99+";
  }

  return String(count);
}

async function reportCaptureEvent(event) {
  if (event.status === "captured" || event.status === "scheduled" || event.status === "active") {
    return;
  }

  try {
    await fetch(CAPTURE_EVENTS_ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(event)
    });
  } catch (error) {
    log("Could not report capture event to local service", {
      status: event.status,
      error: error.message
    });
  }
}

async function updateBadge(status, options = {}) {
  const count = Number(options.todayCaptureCount || 0);
  const countText = formatBadgeCount(count);
  const countLabel = count === 1 ? "1 page remembered today" : `${count} pages remembered today`;
  const titleByStatus = {
    active: count ? `Daemon is ready · ${countLabel}` : "Daemon is ready to save allowed pages",
    captured: count ? `Daemon saved this page locally · ${countLabel}` : "Daemon saved this page locally",
    scheduled: count ? `Daemon is checking this page · ${countLabel}` : "Daemon is checking this page",
    paused: "Daemon capture is paused",
    "consent-required": "Daemon is off until you choose Start remembering",
    "host-access-withheld": "Chrome page access is required before Daemon can remember pages",
    "service-incompatible": "Update the Daemon local service before capture can continue",
    "service-unavailable": "Daemon local service is unavailable",
    "service-off": "Daemon local service is offline",
    skipped: "Daemon protected this page before reading",
    blocked: "Daemon protected this page before reading",
    failed: "Daemon needs attention"
  };
  const title = titleByStatus[status] ?? titleByStatus.active;

  try {
    await chrome.action.setBadgeText({
      text: ["active", "captured", "scheduled"].includes(status) ? countText : ""
    });
    await chrome.action.setBadgeBackgroundColor({ color: "#27616a" });
    await chrome.action.setBadgeTextColor({ color: "#fffefa" });
    await chrome.action.setTitle({ title });
  } catch {
    // Badge updates are best-effort UI polish.
  }
}

async function isCapturePaused() {
  const state = await storageGet(STATE_KEYS.paused);
  return Boolean(state[STATE_KEYS.paused]);
}

async function setCapturePaused(paused) {
  await storageSet({
    [STATE_KEYS.paused]: paused
  });

  await setLastEvent({
    status: paused ? "paused" : "active",
    type: paused ? "capture-paused" : "capture-resumed",
    message: paused ? "Capture paused" : "Capture resumed"
  });
}

async function setLastEvent(event) {
  if (!(await hasCaptureConsent())) {
    return null;
  }

  const lastEvent = {
    at: new Date().toISOString(),
    ...event
  };

  await storageSet({
    [STATE_KEYS.lastEvent]: lastEvent
  });

  await updateBadge(lastEvent.status, {
    todayCaptureCount: await getTodayCaptureCount()
  });
  await reportCaptureEvent(lastEvent);

  return lastEvent;
}

async function getStatus() {
  const state = await storageGet([
    STATE_KEYS.paused,
    STATE_KEYS.lastEvent,
    STATE_KEYS.consentVersion,
    STATE_KEYS.permissionWithheld
  ]);
  const consentVersion = state[STATE_KEYS.consentVersion];
  const consented = hasCurrentCaptureConsent(consentVersion);
  const hostAccessGranted = consented ? await containsOptionalPageAccess() : false;
  const permissionWithheld = consented && !hostAccessGranted;
  await reconcilePermissionWithheld(permissionWithheld);
  const service = await checkServiceHealth();
  const activeTab = consented && hostAccessGranted && service.compatible ? await getActiveTab() : null;
  const activeUrl = activeTab?.url ?? "";
  const blockResult = activeUrl ? await getBlockResult(activeUrl) : { blocked: false, reason: null };
  const lastEvent = consented ? state[STATE_KEYS.lastEvent] ?? null : null;
  const currentPageEvent = activeUrl && lastEvent?.url && urlsMatch(activeUrl, lastEvent.url)
    ? lastEvent
    : null;

  return {
    ok: true,
    endpoint: PORTAL_URL,
    consentVersion: Number(consentVersion || 0),
    consentRequired: !consented,
    hostAccessGranted,
    permissionWithheld,
    captureEnabled: consented && hostAccessGranted && service.compatible && !Boolean(state[STATE_KEYS.paused]),
    paused: Boolean(state[STATE_KEYS.paused]),
    lastEvent,
    currentPageEvent,
    service,
    activeTab: activeTab
      ? {
          url: activeTab.url,
          title: activeTab.title,
          domain: getDomain(activeTab.url)
        }
      : null,
    blockResult
  };
}

async function refreshBadgeFromCurrentTab() {
  const gate = await getCaptureGate();
  if (!gate.ready) {
    await updateBadge(gate.reason);
    return;
  }

  const paused = await isCapturePaused();

  if (paused) {
    await updateBadge("paused");
    return;
  }

  const service = gate.service;

  if (!service.connected) {
    await updateBadge("service-off");
    return;
  }

  const todayCaptureCount = await getTodayCaptureCount();
  const activeTab = await getActiveTab();
  const blockResult = activeTab?.url ? await getBlockResult(activeTab.url) : { blocked: false };

  if (blockResult.blocked) {
    await updateBadge("blocked", { todayCaptureCount });
    return;
  }

  const state = await storageGet(STATE_KEYS.lastEvent);
  const lastEvent = state[STATE_KEYS.lastEvent];

  const status = activeTab?.url && lastEvent?.status === "captured" && urlsMatch(activeTab.url, lastEvent.url)
    ? "captured"
    : "active";
  await updateBadge(status, { todayCaptureCount });
}

function refreshActiveBadgeSoon() {
  setTimeout(() => {
    void refreshBadgeFromCurrentTab();
  }, 0);
}

function postCapture(payload) {
  return fetch(INGEST_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify(payload)
  });
}

function postBlockedDomain(domain) {
  return fetch(BLOCKED_DOMAINS_ENDPOINT, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      domain,
      reason: "blocked-from-popup"
    })
  });
}

async function getBlockResult(rawUrl) {
  const staticBlockResult = isBlockedUrl(rawUrl);
  const blockResult = await resolveCaptureProtection(
    rawUrl,
    staticBlockResult,
    USER_BLOCKLIST_CHECK_ENDPOINT,
    DEFAULT_PROTECTION_CHECK_ENDPOINT
  );

  if (blockResult.reason === "protection-check-unavailable") {
    log("Protection check unavailable; page remains unread", {
      domain: getDomain(rawUrl)
    });
  }

  return blockResult;
}

function sendMessageToTab(tabId, message) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, (response) => {
      const runtimeError = chrome.runtime.lastError;

      if (runtimeError) {
        reject(new Error(runtimeError.message));
        return;
      }

      resolve(response);
    });
  });
}

async function disableScrollWatch(tabId, reason) {
  try {
    await sendMessageToTab(tabId, {
      type: "DAEMON_MODE_DISABLE_SCROLL_WATCH",
      reason
    });
  } catch {
    // The page may not have a content script yet, which is fine.
  }
}

function sendCaptureMessage(tabId, triggerUrl, navigationType) {
  return sendMessageToTab(tabId, {
    type: "DAEMON_MODE_CAPTURE_PAGE",
    triggerUrl,
    navigationType
  });
}

async function captureTab(tabId, triggerUrl, navigationType) {
  const gate = await getCaptureGate();
  if (!gate.ready) {
    await updateBadge(gate.reason);
    return {
      ok: false,
      skipped: true,
      reason: gate.reason
    };
  }

  const blockResult = await getBlockResult(triggerUrl);

  if (blockResult.blocked) {
    await disableScrollWatch(tabId, blockResult.reason);
    await setLastEvent({
      status: "skipped",
      type: "blocked-before-read",
      navigationType,
      url: triggerUrl,
      reason: blockResult.reason,
      message: "Skipped blocked page before reading text"
    });
    log("Skipped blocked page before reading text", {
      tabId,
      triggerUrl,
      navigationType,
      reason: blockResult.reason
    });
    return {
      ok: false,
      skipped: true,
      reason: blockResult.reason
    };
  }

  if (await isCapturePaused()) {
    await disableScrollWatch(tabId, "capture-paused");
    await setLastEvent({
      status: "paused",
      type: "paused-before-read",
      navigationType,
      url: triggerUrl,
      message: "Skipped capture because Daemon Mode is paused"
    });
    log("Skipped capture because paused", {
      tabId,
      triggerUrl,
      navigationType
    });
    return {
      ok: false,
      skipped: true,
      reason: "capture-paused"
    };
  }

  try {
    const capture = await sendCaptureMessage(tabId, triggerUrl, navigationType);

    if (capture?.skipped) {
      await disableScrollWatch(tabId, capture.reason ?? "content-script-skipped");
      await setLastEvent({
        status: "skipped",
        type: "content-script-skipped-before-read",
        navigationType,
        url: triggerUrl,
        reason: capture.reason ?? "content-script-skipped",
        message: "Skipped page before reading text"
      });
      log("Content script skipped page before reading text", {
        tabId,
        triggerUrl,
        navigationType,
        reason: capture.reason
      });
      return {
        ok: false,
        skipped: true,
        reason: capture.reason
      };
    }

    if (!capture?.ok) {
      const error = capture?.error ?? "missing-response";
      await setLastEvent({
        status: "failed",
        type: "content-script-failed",
        navigationType,
        url: triggerUrl,
        error,
        message: "Content script did not return a capture"
      });
      log("Content script did not return a capture", {
        tabId,
        triggerUrl,
        navigationType,
        error
      });
      return {
        ok: false,
        error
      };
    }

    const finalBlockResult = await getBlockResult(capture.url);

    if (finalBlockResult.blocked) {
      await disableScrollWatch(tabId, finalBlockResult.reason);
      await setLastEvent({
        status: "skipped",
        type: "blocked-after-content-settle",
        navigationType,
        url: capture.url,
        reason: finalBlockResult.reason,
        message: "Skipped page after its URL changed"
      });
      log("Skipped page after final protection check", {
        tabId,
        url: capture.url,
        navigationType,
        reason: finalBlockResult.reason
      });
      return {
        ok: false,
        skipped: true,
        reason: finalBlockResult.reason
      };
    }

    const payload = {
      source: "daemon-mode-chromium-extension",
      extensionVersion: chrome.runtime.getManifest().version,
      navigationType,
      url: capture.url,
      title: capture.title,
      domain: capture.domain,
      capturedAt: capture.capturedAt,
      text: capture.text,
      textLength: capture.textLength
    };

    const response = await postCapture(payload);

    if (!response.ok) {
      throw new Error(`Receiver returned HTTP ${response.status}`);
    }

    const lastEvent = await setLastEvent({
      status: "captured",
      type: "payload-posted",
      navigationType,
      url: payload.url,
      title: payload.title,
      domain: payload.domain,
      textLength: payload.textLength,
      message: "Capture payload posted"
    });

    log("Posted capture payload", {
      tabId,
      navigationType,
      url: payload.url,
      textLength: payload.textLength
    });

    return {
      ok: true,
      lastEvent
    };
  } catch (error) {
    await setLastEvent({
      status: "failed",
      type: "capture-failed",
      navigationType,
      url: triggerUrl,
      error: error.message,
      message: "Capture failed"
    });
    log("Capture failed", {
      tabId,
      triggerUrl,
      navigationType,
      error: error.message
    });
    return {
      ok: false,
      error: error.message
    };
  }
}

async function scheduleCapture(details, navigationType) {
  const gate = await getCaptureGate();
  if (!gate.ready) {
    await updateBadge(gate.reason);
    return;
  }

  if (details.frameId !== 0 || !details.tabId || !details.url) {
    return;
  }

  const existingTimer = pendingCaptures.get(details.tabId);

  if (existingTimer) {
    clearTimeout(existingTimer);
    pendingCaptures.delete(details.tabId);
  }

  const blockResult = await getBlockResult(details.url);

  if (blockResult.blocked) {
    await disableScrollWatch(details.tabId, blockResult.reason);
    await setLastEvent({
      status: "skipped",
      type: "blocked-navigation",
      navigationType,
      url: details.url,
      reason: blockResult.reason,
      message: "Skipped blocked navigation"
    });
    log("Skipped blocked navigation", {
      tabId: details.tabId,
      url: details.url,
      navigationType,
      reason: blockResult.reason
    });
    return;
  }

  if (await isCapturePaused()) {
    await disableScrollWatch(details.tabId, "capture-paused");
    await setLastEvent({
      status: "paused",
      type: "paused-navigation",
      navigationType,
      url: details.url,
      message: "Capture is paused"
    });
    log("Skipped scheduled capture because paused", {
      tabId: details.tabId,
      url: details.url,
      navigationType
    });
    return;
  }

  const timer = setTimeout(() => {
    pendingCaptures.delete(details.tabId);
    void captureTab(details.tabId, details.url, navigationType);
  }, CAPTURE_DEBOUNCE_MS);

  pendingCaptures.set(details.tabId, timer);

  await setLastEvent({
    status: "scheduled",
    type: "capture-scheduled",
    navigationType,
    url: details.url,
    message: "Capture scheduled"
  });

  log("Scheduled capture", {
    tabId: details.tabId,
    url: details.url,
    navigationType
  });
}

async function captureActiveTab() {
  const gate = await getCaptureGate();
  if (!gate.ready) {
    return {
      ok: false,
      error: gate.reason
    };
  }

  const tab = await getActiveTab();

  if (!tab?.id || !tab.url) {
    return {
      ok: false,
      error: "No active tab to capture"
    };
  }

  return captureTab(tab.id, tab.url, "manual-recapture");
}

async function blockActiveTabDomain() {
  const gate = await getCaptureGate();
  if (!gate.ready) {
    return {
      ok: false,
      error: gate.reason
    };
  }

  const tab = await getActiveTab();
  const domain = getDomain(tab?.url);

  if (!domain) {
    return {
      ok: false,
      error: "No current site to block"
    };
  }

  try {
    const response = await postBlockedDomain(domain);
    const payload = await response.json();

    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `Local service returned HTTP ${response.status}`);
    }

    await setLastEvent({
      status: "blocked",
      type: "blocked-current-site",
      url: tab.url,
      title: tab.title,
      domain,
      reason: "blocked-from-popup",
      message: "Blocked this site"
    });

    return {
      ok: true,
      domain
    };
  } catch (error) {
    await setLastEvent({
      status: "failed",
      type: "block-current-site-failed",
      url: tab?.url,
      title: tab?.title,
      domain,
      error: error.message,
      message: "Could not block this site"
    });

    return {
      ok: false,
      error: error.message
    };
  }
}

async function openMemoryPortal() {
  await chrome.tabs.create({
    url: PORTAL_URL
  });

  return {
    ok: true,
    url: PORTAL_URL
  };
}

async function startRemembering(permissionGranted) {
  const hostAccessGranted = Boolean(permissionGranted) && await containsOptionalPageAccess();
  if (!hostAccessGranted) {
    await storageSet({
      [STATE_KEYS.permissionWithheld]: true
    });
    await unregisterCaptureContentScript();
    await updateBadge("host-access-withheld");
    return getStatus();
  }

  const alreadyConsented = await hasCaptureConsent();
  const values = {
    [STATE_KEYS.consentVersion]: CAPTURE_CONSENT_VERSION,
    [STATE_KEYS.permissionWithheld]: false
  };
  if (!alreadyConsented) {
    values[STATE_KEYS.paused] = false;
  }
  await storageSet(values);
  await syncCaptureContentScript();
  await refreshBadgeFromCurrentTab();
  return {
    ...(await getStatus()),
    justStarted: !alreadyConsented
  };
}

async function handleScrollTextGrowth(message, sender) {
  const gate = await getCaptureGate();
  if (!gate.ready) {
    return {
      ok: false,
      error: gate.reason
    };
  }

  if (!sender.tab?.id || !message.url) {
    return {
      ok: false,
      error: "Missing sender tab or URL"
    };
  }

  return captureTab(sender.tab.id, message.url, "scroll-text-growth");
}

chrome.webNavigation.onCompleted.addListener((details) => {
  void scheduleCapture(details, "completed");
});

chrome.webNavigation.onHistoryStateUpdated.addListener((details) => {
  void scheduleCapture(details, "history-state-updated");
});

chrome.webNavigation.onReferenceFragmentUpdated.addListener((details) => {
  void scheduleCapture(details, "reference-fragment-updated");
});

chrome.tabs.onActivated.addListener(() => {
  refreshActiveBadgeSoon();
});

chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  void hasCaptureConsent().then((consented) => {
    if (!consented || !tab.active) {
      return;
    }

    if (changeInfo.url || changeInfo.status === "complete") {
      refreshActiveBadgeSoon();
    }
  });
});

chrome.windows.onFocusChanged.addListener((windowId) => {
  if (windowId === chrome.windows.WINDOW_ID_NONE) {
    return;
  }

  refreshActiveBadgeSoon();
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "DAEMON_MODE_GET_STATUS") {
    getStatus().then(sendResponse);
    return true;
  }

  if (message?.type === "DAEMON_MODE_SET_PAUSED") {
    setCapturePaused(Boolean(message.paused))
      .then(getStatus)
      .then(sendResponse);
    return true;
  }

  if (message?.type === "DAEMON_MODE_START_REMEMBERING") {
    startRemembering(message.permissionGranted === true).then(sendResponse);
    return true;
  }

  if (message?.type === "DAEMON_MODE_CAPTURE_ACTIVE_TAB") {
    captureActiveTab().then(sendResponse);
    return true;
  }

  if (message?.type === "DAEMON_MODE_BLOCK_ACTIVE_TAB_DOMAIN") {
    blockActiveTabDomain().then(sendResponse);
    return true;
  }

  if (message?.type === "DAEMON_MODE_OPEN_PORTAL") {
    openMemoryPortal().then(sendResponse);
    return true;
  }

  if (message?.type === "DAEMON_MODE_SCROLL_TEXT_GROWTH") {
    handleScrollTextGrowth(message, sender).then(sendResponse);
    return true;
  }

  return false;
});

chrome.runtime.onInstalled.addListener((details) => {
  void (async () => {
    if (details.reason === chrome.runtime.OnInstalledReason.INSTALL) {
      await storageSet({
        [STATE_KEYS.consentVersion]: 0,
        [STATE_KEYS.permissionWithheld]: false,
        [STATE_KEYS.paused]: false
      });
      await unregisterCaptureContentScript();
      await updateBadge("consent-required");
      log("Installed with capture off until the owner opts in");
      return;
    }

    // Updates preserve both consent and pause state.
    await syncCaptureContentScript();
    await refreshBadgeFromCurrentTab();
    log("Updated Daemon Mode", {
      previousVersion: details.previousVersion || "unknown"
    });
  })();
});

chrome.runtime.onStartup.addListener(() => {
  void syncCaptureContentScript().then(refreshActiveBadgeSoon);
});

chrome.permissions.onRemoved.addListener((permissions) => {
  if (!(permissions.origins || []).length) {
    return;
  }

  void containsOptionalPageAccess().then(async (hostAccessGranted) => {
    await reconcilePermissionWithheld(!hostAccessGranted);

    if (hostAccessGranted) {
      await syncCaptureContentScript();
      refreshActiveBadgeSoon();
      return;
    }

    await unregisterCaptureContentScript();
    await updateBadge("host-access-withheld");
  });
});

chrome.permissions.onAdded.addListener((permissions) => {
  if (!(permissions.origins || []).length) {
    return;
  }

  void storageSet({
    [STATE_KEYS.permissionWithheld]: false
  }).then(syncCaptureContentScript).then(refreshActiveBadgeSoon);
});

void syncCaptureContentScript().then(refreshActiveBadgeSoon);
