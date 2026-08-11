const SETTLE_POLL_MS = 250;
const SETTLE_STABLE_POLLS = 3;
const SETTLE_TIMEOUT_MS = 3000;
const SCROLL_DEBOUNCE_MS = 1500;
const SCROLL_RECAPTURE_COOLDOWN_MS = 10000;
const MIN_SCROLL_TEXT_GROWTH = 800;
const MIN_SCROLL_TEXT_GROWTH_RATIO = 0.15;
const SENSITIVE_FIELD_SELECTORS = [
  'input[type="password" i]',
  'input[autocomplete~="current-password" i]',
  'input[autocomplete~="new-password" i]',
  'input[autocomplete~="one-time-code" i]',
  'input[autocomplete~="cc-number" i]',
  'input[autocomplete~="cc-csc" i]',
  'input[autocomplete~="cc-exp" i]',
  'input[name*="password" i]',
  'input[id*="password" i]',
  'input[name*="passcode" i]',
  'input[id*="passcode" i]',
  'input[name*="otp" i]',
  'input[id*="otp" i]',
  'input[name*="ssn" i]',
  'input[id*="ssn" i]',
  'input[name*="creditcard" i]',
  'input[id*="creditcard" i]',
  'input[name*="card-number" i]',
  'input[id*="card-number" i]',
  'textarea[name*="password" i]',
  'textarea[id*="password" i]'
];

let scrollWatchEnabled = false;
let scrollWatchBaselineLength = 0;
let scrollWatchTimer = null;
let lastScrollRecaptureAt = 0;

function getPageText() {
  return document.body?.innerText ?? "";
}

function getSensitiveFieldReason() {
  for (const selector of SENSITIVE_FIELD_SELECTORS) {
    if (document.querySelector(selector)) {
      return `sensitive-field:${selector}`;
    }
  }

  return null;
}

function getDomain(rawUrl) {
  try {
    return new URL(rawUrl).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function isInvalidatedExtensionContextError(error) {
  return error?.message?.includes("Extension context invalidated");
}

function sendRuntimeMessage(message) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(message, (response) => {
        const runtimeError = chrome.runtime.lastError;

        if (runtimeError) {
          resolve({
            ok: false,
            extensionContextInvalidated: runtimeError.message.includes("Extension context invalidated"),
            error: runtimeError.message
          });
          return;
        }

        resolve(response);
      });
    } catch (error) {
      resolve({
        ok: false,
        extensionContextInvalidated: isInvalidatedExtensionContextError(error),
        error: error.message
      });
    }
  });
}

async function waitForTextToSettle() {
  const startedAt = Date.now();
  let previousLength = -1;
  let stablePolls = 0;

  while (Date.now() - startedAt < SETTLE_TIMEOUT_MS) {
    const currentLength = getPageText().length;

    if (currentLength === previousLength) {
      stablePolls += 1;
    } else {
      stablePolls = 0;
      previousLength = currentLength;
    }

    if (stablePolls >= SETTLE_STABLE_POLLS) {
      break;
    }

    await new Promise((resolve) => setTimeout(resolve, SETTLE_POLL_MS));
  }
}

function shouldRecaptureAfterScroll(currentLength) {
  const growth = currentLength - scrollWatchBaselineLength;

  if (growth < MIN_SCROLL_TEXT_GROWTH) {
    return false;
  }

  if (scrollWatchBaselineLength === 0) {
    return true;
  }

  return growth / scrollWatchBaselineLength >= MIN_SCROLL_TEXT_GROWTH_RATIO;
}

async function maybeRecaptureAfterScroll() {
  if (!scrollWatchEnabled) {
    return;
  }

  const now = Date.now();

  if (now - lastScrollRecaptureAt < SCROLL_RECAPTURE_COOLDOWN_MS) {
    return;
  }

  const currentLength = getPageText().length;

  if (!shouldRecaptureAfterScroll(currentLength)) {
    return;
  }

  lastScrollRecaptureAt = now;

  console.log("[Daemon Mode] Meaningful scroll text growth detected", {
    domain: getDomain(window.location.href),
    previousLength: scrollWatchBaselineLength,
    currentLength
  });

  const response = await sendRuntimeMessage({
    type: "DAEMON_MODE_SCROLL_TEXT_GROWTH",
    url: window.location.href,
    previousLength: scrollWatchBaselineLength,
    currentLength
  });

  if (response?.extensionContextInvalidated) {
    disableScrollWatch("extension-context-invalidated");
    return;
  }

  if (response?.ok) {
    scrollWatchBaselineLength = currentLength;
  }
}

function onScroll() {
  if (!scrollWatchEnabled) {
    return;
  }

  if (scrollWatchTimer) {
    clearTimeout(scrollWatchTimer);
  }

  scrollWatchTimer = setTimeout(() => {
    scrollWatchTimer = null;
    void maybeRecaptureAfterScroll();
  }, SCROLL_DEBOUNCE_MS);
}

function enableScrollWatch(textLength) {
  scrollWatchEnabled = true;
  scrollWatchBaselineLength = textLength;
  lastScrollRecaptureAt = 0;
  window.addEventListener("scroll", onScroll, {
    passive: true
  });
}

function disableScrollWatch(reason) {
  scrollWatchEnabled = false;
  scrollWatchBaselineLength = 0;

  if (scrollWatchTimer) {
    clearTimeout(scrollWatchTimer);
    scrollWatchTimer = null;
  }

  window.removeEventListener("scroll", onScroll);

  console.log("[Daemon Mode] Disabled scroll recapture watch", {
    reason
  });
}

async function capturePage() {
  const sensitiveFieldReason = getSensitiveFieldReason();

  if (sensitiveFieldReason) {
    disableScrollWatch(sensitiveFieldReason);
    console.log("[Daemon Mode] Skipped page before reading text", {
      domain: getDomain(window.location.href),
      reason: sensitiveFieldReason
    });
    return {
      ok: false,
      skipped: true,
      reason: sensitiveFieldReason,
      url: window.location.href,
      title: document.title,
      domain: getDomain(window.location.href)
    };
  }

  await waitForTextToSettle();

  const settledSensitiveFieldReason = getSensitiveFieldReason();

  if (settledSensitiveFieldReason) {
    disableScrollWatch(settledSensitiveFieldReason);
    console.log("[Daemon Mode] Skipped page after settle check", {
      domain: getDomain(window.location.href),
      reason: settledSensitiveFieldReason
    });
    return {
      ok: false,
      skipped: true,
      reason: settledSensitiveFieldReason,
      url: window.location.href,
      title: document.title,
      domain: getDomain(window.location.href)
    };
  }

  const url = window.location.href;
  const text = getPageText();
  const finalUrl = window.location.href;

  if (url !== finalUrl) {
    disableScrollWatch("navigation-changed-before-read");
    return {
      ok: false,
      skipped: true,
      reason: "navigation-changed-before-read",
      url: finalUrl,
      title: document.title,
      domain: getDomain(finalUrl)
    };
  }

  const payload = {
    ok: true,
    url: finalUrl,
    title: document.title,
    domain: getDomain(url),
    capturedAt: new Date().toISOString(),
    text,
    textLength: text.length
  };

  enableScrollWatch(payload.textLength);

  return payload;
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "DAEMON_MODE_DISABLE_SCROLL_WATCH") {
    disableScrollWatch(message.reason);
    sendResponse({
      ok: true
    });
    return false;
  }

  if (message?.type !== "DAEMON_MODE_CAPTURE_PAGE") {
    return false;
  }

  capturePage()
    .then((payload) => {
      if (payload.skipped) {
        console.log("[Daemon Mode] Skipped page capture", {
          triggerDomain: getDomain(message.triggerUrl),
          navigationType: message.navigationType,
          domain: payload.domain,
          reason: payload.reason
        });
        sendResponse(payload);
        return;
      }

      console.log("[Daemon Mode] Captured page text", {
        triggerDomain: getDomain(message.triggerUrl),
        navigationType: message.navigationType,
        domain: payload.domain,
        textLength: payload.textLength
      });
      sendResponse(payload);
    })
    .catch((error) => {
      console.warn("[Daemon Mode] Capture page read failed", {
        triggerDomain: getDomain(message.triggerUrl),
        navigationType: message.navigationType,
        error: error.message
      });
      sendResponse({
        ok: false,
        error: error.message
      });
    });

  return true;
});
