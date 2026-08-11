const statusPill = document.querySelector("#status-pill");
const endpoint = document.querySelector("#endpoint");
const pageTitle = document.querySelector("#page-title");
const pageMessage = document.querySelector("#page-message");
const pageDetail = document.querySelector("#page-detail");
const eventMessage = document.querySelector("#event-message");
const eventDetail = document.querySelector("#event-detail");
const pauseToggle = document.querySelector("#pause-toggle");
const captureAgain = document.querySelector("#capture-again");
const openMemory = document.querySelector("#open-memory");
const blockSite = document.querySelector("#block-site");
const trustFooter = document.querySelector("#trust-footer");
const todayLine = document.querySelector("#today-line");
const todayLineText = document.querySelector("#today-line-text");
const latestActivity = document.querySelector("#latest-activity");
const moreActivity = document.querySelector("#more-activity");
const activityList = document.querySelector("#activity-list");
const consentDisclosure = document.querySelector("#consent-disclosure");

const MEMORY_PULSE_URL = "http://127.0.0.1:4317/memory-pulse";
const SETUP_URL = "https://github.com/AadityasinhJadeja/Daemon-Mode#quick-start";
const OPTIONAL_PAGE_ORIGINS = ["http://*/*", "https://*/*"];
let activityExpanded = false;

function getOriginCoverDiameter(width, height, x, y) {
  return Math.ceil(
    2 * Math.max(
      Math.hypot(x, y),
      Math.hypot(width - x, y),
      Math.hypot(x, height - y),
      Math.hypot(width - x, height - y)
    )
  );
}

function updateOriginFill(button, x, y) {
  const rect = button.getBoundingClientRect();
  const safeX = Math.max(0, Math.min(x, rect.width));
  const safeY = Math.max(0, Math.min(y, rect.height));
  const size = getOriginCoverDiameter(rect.width, rect.height, safeX, safeY);
  button.style.setProperty("--origin-x", `${safeX}px`);
  button.style.setProperty("--origin-y", `${safeY}px`);
  button.style.setProperty("--origin-size", `${size}px`);
}

function updateOriginFillFromCenter(button) {
  const rect = button.getBoundingClientRect();
  updateOriginFill(button, rect.width / 2, rect.height / 2);
}

function updateOriginFillFromPointer(button, event) {
  const rect = button.getBoundingClientRect();
  updateOriginFill(button, event.clientX - rect.left, event.clientY - rect.top);
}

function setOriginActive(button, active) {
  button.dataset.originActive = String(Boolean(active && !button.disabled));
}

function setOriginPressed(button, pressed) {
  button.dataset.originPressed = String(Boolean(pressed && !button.disabled));
}

function setOriginButtonLabel(button, text) {
  let label = button.querySelector(".origin-label");
  if (!label) {
    label = document.createElement("span");
    label.className = "origin-label";
    button.replaceChildren(label);
  }
  label.textContent = text;
  button.setAttribute("aria-label", text);
}

function initOriginButtons() {
  const originButtons = document.querySelectorAll("[data-origin-button]");
  originButtons.forEach((button) => {
    if (button.dataset.originReady === "true") return;
    button.dataset.originReady = "true";
    const label = button.querySelector(".origin-label");
    if (label && !button.getAttribute("aria-label")) {
      button.setAttribute("aria-label", label.textContent.trim());
    }

    button.addEventListener("pointerenter", (event) => {
      if (button.disabled) return;
      updateOriginFillFromPointer(button, event);
      setOriginActive(button, true);
    });

    button.addEventListener("pointermove", (event) => {
      if (button.disabled || button.dataset.originActive !== "true") return;
      updateOriginFillFromPointer(button, event);
    });

    button.addEventListener("pointerleave", () => {
      setOriginActive(button, false);
      setOriginPressed(button, false);
    });

    button.addEventListener("pointerdown", (event) => {
      if (button.disabled || event.button !== 0) return;
      updateOriginFillFromPointer(button, event);
      setOriginActive(button, true);
      setOriginPressed(button, true);
    });

    button.addEventListener("pointerup", () => {
      setOriginPressed(button, false);
    });

    button.addEventListener("pointercancel", () => {
      setOriginPressed(button, false);
    });

    button.addEventListener("focus", () => {
      if (button.disabled || !button.matches(":focus-visible")) return;
      updateOriginFillFromCenter(button);
      setOriginActive(button, true);
    });

    button.addEventListener("blur", () => {
      setOriginActive(button, false);
      setOriginPressed(button, false);
    });

    button.addEventListener("keydown", (event) => {
      if (button.disabled || event.repeat || (event.key !== " " && event.key !== "Enter")) return;
      if (event.key === " ") {
        event.preventDefault();
      }
      updateOriginFillFromCenter(button);
      setOriginActive(button, true);
      setOriginPressed(button, true);
    });

    button.addEventListener("keyup", (event) => {
      if (event.key !== " " && event.key !== "Enter") return;
      setOriginPressed(button, false);
      if (!button.matches(":focus-visible")) {
        setOriginActive(button, false);
      }
    });
  });
}

function sendMessage(message) {
  return chrome.runtime.sendMessage(message);
}

function formatUrl(url) {
  if (!url) {
    return "";
  }

  try {
    const parsedUrl = new URL(url);
    return parsedUrl.hostname + parsedUrl.pathname;
  } catch {
    return url;
  }
}

function formatDomain(status) {
  return status.activeTab?.domain || formatUrl(status.activeTab?.url) || "this page";
}

function formatActiveTitle(status) {
  return status.activeTab?.title || formatDomain(status) || "This page";
}

function safeDetail(parts) {
  return parts.filter(Boolean).join(" · ");
}

function pluralize(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

function todayCaptureCount(pulse) {
  return Number(pulse?.stats?.today_capture_count || 0);
}

function todayMemoryLabel(count) {
  if (!count) {
    return "Local memory";
  }

  return `${pluralize(count, "remembered page")} today`;
}

function isUserBlocked(status) {
  return Boolean(status.blockResult?.blocked && status.blockResult.reason?.startsWith("user-blocked-domain"));
}

function isProtectedByDefault(status) {
  return Boolean(status.blockResult?.blocked && !isUserBlocked(status));
}

function explainReason(reason) {
  if (!reason) {
    return null;
  }

  if (reason.startsWith("user-blocked-domain")) {
    return "your rule";
  }

  if (reason.startsWith("blocked-domain")) {
    return "default privacy list";
  }

  if (reason.startsWith("blocked-url-pattern")) {
    return "sensitive URL pattern";
  }

  if (reason.startsWith("sensitive-field")) {
    return "sensitive form field";
  }

  if (reason === "blocked-from-popup") {
    return "blocked from the popup";
  }

  return reason;
}

function relativeTime(value) {
  if (!value) {
    return "";
  }

  const then = new Date(value).getTime();
  if (Number.isNaN(then)) {
    return "";
  }

  const diffSeconds = Math.max(0, Math.round((Date.now() - then) / 1000));

  if (diffSeconds < 45) {
    return "just now";
  }

  const diffMinutes = Math.round(diffSeconds / 60);
  if (diffMinutes < 60) {
    return `${diffMinutes}m ago`;
  }

  const diffHours = Math.round(diffMinutes / 60);
  if (diffHours < 24) {
    return `${diffHours}h ago`;
  }

  const diffDays = Math.round(diffHours / 24);
  return `${diffDays}d ago`;
}

function eventLabel(event) {
  if (!event) {
    return "No capture activity yet.";
  }

  if (event.status === "captured") {
    return "Saved locally";
  }

  if (event.reason?.startsWith("user-blocked-domain") || event.status === "blocked") {
    return "Blocked by your rules";
  }

  if (event.type?.includes("blocked") || event.reason?.startsWith("sensitive-field")) {
    return "Protected before reading";
  }

  if (event.status === "paused") {
    return "Not recording";
  }

  if (event.status === "failed") {
    return "Needs attention";
  }

  if (event.status === "scheduled") {
    return "Checking this page";
  }

  return event.message ?? event.type ?? "Status updated";
}

function eventMeta(event) {
  if (!event) {
    return "";
  }

  const time = relativeTime(event.at || event.occurredAt || event.capturedAt);
  const protectedReason = event.type?.includes("blocked") || event.reason?.startsWith("sensitive-field") || event.status === "blocked";

  return safeDetail([
    event.status === "captured" && event.textLength ? `${event.textLength} chars` : null,
    protectedReason ? "no text read" : null,
    time
  ]);
}

function pulseEventMeta(item) {
  const protectedReason = item.type?.includes("blocked") || item.reason?.startsWith("sensitive-field") || item.status === "blocked";

  return safeDetail([
    item.status === "captured" || item.textLength ? "saved" : protectedReason ? "protected" : item.status || item.type,
    relativeTime(item.receivedAt || item.occurredAt || item.at || item.capturedAt)
  ]);
}

function pulseEventTitle(item) {
  if (item.title) {
    return item.title;
  }

  return eventLabel(item);
}

function recentPulseItems(pulse) {
  if (!pulse) {
    return [];
  }

  const captured = (pulse.recentCaptured || []).map((item) => ({
    ...item,
    status: "captured",
    sortAt: item.receivedAt || item.capturedAt || item.at
  }));
  const protectedItems = (pulse.recentProtected || []).map((item) => ({
    ...item,
    sortAt: item.occurredAt || item.at
  }));

  return [...captured, ...protectedItems]
    .sort((left, right) => new Date(right.sortAt || 0) - new Date(left.sortAt || 0))
    .slice(0, 3);
}

async function fetchMemoryPulse() {
  try {
    const response = await fetch(MEMORY_PULSE_URL);
    if (!response.ok) {
      return null;
    }

    const payload = await response.json();
    return payload?.ok === false ? null : payload;
  } catch {
    return null;
  }
}

function pageState(status) {
  const currentEvent = status.currentPageEvent;

  if (status.consentRequired) {
    return {
      kind: "consent-required",
      title: "Start when you are ready",
      message: "Choose Start remembering to let Daemon read allowed web pages and save them on this computer.",
      meta: "Sensitive and blocked pages stay protected · consent v1"
    };
  }

  if (!status.hostAccessGranted || status.permissionWithheld) {
    return {
      kind: "host-access-withheld",
      title: "Chrome access is off",
      message: "Daemon cannot read page URLs, titles, or text until you grant access again.",
      meta: "No page content is being handled"
    };
  }

  if (status.service?.connected && !status.service?.compatible) {
    return {
      kind: "service-incompatible",
      title: "Local service needs an update",
      message: "This extension will stay off until the Daemon service supports local API v1.",
      meta: "No page content is being read"
    };
  }

  if (!status.service?.connected) {
    return {
      kind: "offline",
      title: "Daemon is offline",
      message: "The background service normally starts when you log in. Run setup again to repair it.",
      meta: "The dashboard tab does not need to stay open."
    };
  }

  if (status.justStarted) {
    return {
      kind: "active",
      title: "Ready to remember",
      message: "Reload this page once, or open another allowed page, to begin.",
      meta: "Capture stays local"
    };
  }

  const domain = formatDomain(status);

  if (status.paused) {
    return {
      kind: "paused",
      title: "Not recording",
      message: "Daemon is paused until you resume it.",
      meta: "No new pages are being saved."
    };
  }

  if (isUserBlocked(status)) {
    return {
      kind: "blocked",
      title: "This site is blocked",
      message: "Daemon will not read or save this page.",
      meta: "Your rule"
    };
  }

  if (isProtectedByDefault(status)) {
    return {
      kind: "protected",
      title: "Kept private",
      message: "Daemon did not read this page.",
      meta: "Default protection"
    };
  }

  if (currentEvent?.status === "captured") {
    return {
      kind: "saved",
      title: currentEvent.title || formatActiveTitle(status),
      message: "Saved to memory.",
      meta: safeDetail([
        currentEvent.textLength ? `${currentEvent.textLength} chars` : null,
        relativeTime(currentEvent.at || currentEvent.capturedAt)
      ])
    };
  }

  if (currentEvent?.status === "failed") {
    return {
      kind: "failed",
      title: formatActiveTitle(status),
      message: "Could not save this page.",
      meta: currentEvent.error || "Try again from this popup."
    };
  }

  if (currentEvent?.status === "scheduled") {
    return {
      kind: "active",
      title: formatActiveTitle(status),
      message: "Checking whether this page can be saved.",
      meta: ""
    };
  }

  return {
    kind: "active",
    title: formatActiveTitle(status),
    message: "Ready to save this page if it is allowed.",
    meta: ""
  };
}

function statusCopy(status, state) {
  if (state.kind === "consent-required") {
    return { label: "Off", className: "paused" };
  }

  if (state.kind === "host-access-withheld") {
    return { label: "Access off", className: "paused" };
  }

  if (state.kind === "service-incompatible") {
    return { label: "Update needed", className: "failed" };
  }

  if (state.kind === "offline") {
    return { label: "Offline", className: "offline" };
  }

  if (state.kind === "paused") {
    return { label: "Paused", className: "paused" };
  }

  if (state.kind === "blocked") {
    return { label: "Blocked", className: "blocked" };
  }

  if (state.kind === "protected") {
    return { label: "Protected", className: "protected" };
  }

  if (state.kind === "saved") {
    return { label: "Saved", className: "saved" };
  }

  if (state.kind === "failed") {
    return { label: "Needs attention", className: "failed" };
  }

  return { label: "Local", className: "active" };
}

function setButtonState(button, { label, disabled = false, title = "" }) {
  setOriginButtonLabel(button, label);
  button.disabled = disabled;
  button.title = title;
  button.dataset.locked = disabled ? "true" : "false";
}

function renderActions(status, state) {
  const consentRequired = state.kind === "consent-required";
  const hostAccessWithheld = state.kind === "host-access-withheld";
  const setupRequired = consentRequired || hostAccessWithheld;
  const serviceOff = state.kind === "offline";
  const serviceIncompatible = state.kind === "service-incompatible";
  const paused = state.kind === "paused";
  const userBlocked = isUserBlocked(status);
  const protectedByDefault = isProtectedByDefault(status);
  const protectedPage = userBlocked || protectedByDefault;
  const hasDomain = Boolean(status.activeTab?.domain);

  setButtonState(openMemory, {
    label: consentRequired
      ? "Start remembering"
      : hostAccessWithheld
        ? "Grant Chrome access"
        : serviceIncompatible
          ? "Update local service"
          : serviceOff
            ? "Repair local service"
            : "Open memory",
    disabled: false,
    title: consentRequired
      ? "Chrome will ask before Daemon receives access to normal web pages."
      : hostAccessWithheld
        ? "Restore optional access to normal web pages."
        : serviceIncompatible
          ? "Open Daemon's local service update instructions."
          : serviceOff
            ? "Open Daemon's local service repair instructions."
            : ""
  });

  document.querySelector(".secondary-actions").hidden = setupRequired;

  setButtonState(captureAgain, {
    label: protectedPage ? "Protected page" : "Save again",
    disabled: paused || serviceOff || protectedPage,
    title: protectedPage
      ? "Daemon Mode is not reading this page."
      : paused
        ? "Resume capture first."
        : serviceOff
          ? "Start the local memory service first."
          : ""
  });

  setButtonState(pauseToggle, {
    label: paused ? "Resume" : "Pause",
    disabled: false,
    title: paused ? "Resume automatic capture." : "Pause automatic capture."
  });

  setButtonState(blockSite, {
    label: userBlocked ? "Site blocked" : protectedByDefault ? "Protected already" : "Block site",
    disabled: !hasDomain || serviceOff || protectedPage,
    title: userBlocked
      ? "This site is already blocked by your rules."
      : protectedByDefault
        ? "This site is already protected by the default privacy list."
        : serviceOff
          ? "Start the local memory service first."
          : ""
  });
}

function renderTodayLine(pulse) {
  const savedToday = todayCaptureCount(pulse);
  const protectedToday = Number(pulse?.today?.protectedCount || 0);

  if (savedToday < 5) {
    todayLine.hidden = true;
    todayLineText.textContent = "";
    return;
  }

  todayLineText.textContent = protectedToday
    ? `Today Daemon remembered ${pluralize(savedToday, "page")} and quietly protected ${pluralize(protectedToday, "moment")}.`
    : `Today Daemon remembered ${pluralize(savedToday, "page")}.`;
  todayLine.hidden = false;
}

function renderActivityList(pulse) {
  const items = recentPulseItems(pulse);
  activityList.replaceChildren();

  if (items.length < 2) {
    activityExpanded = false;
    moreActivity.hidden = true;
    activityList.hidden = true;
    return;
  }

  moreActivity.hidden = false;
  setOriginButtonLabel(moreActivity, activityExpanded ? "Less activity" : "More activity");
  activityList.hidden = !activityExpanded;

  for (const item of items) {
    const row = document.createElement("div");
    row.className = "activity-item";

    const title = document.createElement("strong");
    title.textContent = pulseEventTitle(item);

    const meta = document.createElement("span");
    meta.textContent = pulseEventMeta(item);

    row.append(title, meta);
    activityList.append(row);
  }
}

function renderStatus(status, pulse = null) {
  const state = pageState(status);
  const pill = statusCopy(status, state);
  const lastEvent = status.lastEvent;

  consentDisclosure.hidden = !status.consentRequired;

  endpoint.textContent = status.consentRequired
    ? "Capture is off"
    : status.permissionWithheld || !status.hostAccessGranted
      ? "Chrome access is off"
      : status.service?.compatible
        ? todayMemoryLabel(todayCaptureCount(pulse))
        : "Waiting for daemon";
  statusPill.textContent = pill.label;
  statusPill.className = `status-pill ${pill.className}`;

  pageTitle.textContent = state.title;
  pageMessage.textContent = state.message;
  pageDetail.textContent = state.meta;

  const currentEventIsRecent = Boolean(
    lastEvent &&
      status.currentPageEvent &&
      lastEvent.status === status.currentPageEvent.status &&
      lastEvent.url === status.currentPageEvent.url &&
      lastEvent.at === status.currentPageEvent.at
  );
  const recentAddsValue = Boolean(lastEvent) && !currentEventIsRecent;
  latestActivity.hidden = !recentAddsValue;

  if (!recentAddsValue) {
    eventMessage.textContent = "No capture activity yet.";
    eventDetail.textContent = "";
  } else {
    eventMessage.textContent = eventLabel(lastEvent);
    eventDetail.textContent = eventMeta(lastEvent);
  }

  trustFooter.textContent = status.service?.connected
    ? status.service?.compatible
      ? "Local only · no cloud sync"
      : "Local only · capture is safely off"
    : "Local only · waiting for daemon";

  renderTodayLine(pulse);
  renderActivityList(pulse);
  renderActions(status, state);
}

async function refreshStatus() {
  const status = await sendMessage({
    type: "DAEMON_MODE_GET_STATUS"
  });

  const pulse = !status.consentRequired && status.hostAccessGranted && status.service?.compatible
    ? await fetchMemoryPulse()
    : null;
  renderStatus(status, pulse);
  return status;
}

async function withBusyState(button, action) {
  if (button.disabled) {
    return;
  }

  button.disabled = true;
  button.dataset.busy = "true";

  try {
    await action();
  } finally {
    delete button.dataset.busy;
    if (button.dataset.locked !== "true") {
      button.disabled = false;
    }
  }
}

pauseToggle.addEventListener("click", () => {
  void withBusyState(pauseToggle, async () => {
    const status = await refreshStatus();
    const nextPaused = !status.paused;
    const updatedStatus = await sendMessage({
      type: "DAEMON_MODE_SET_PAUSED",
      paused: nextPaused
    });
    renderStatus(updatedStatus);
  });
});

captureAgain.addEventListener("click", () => {
  void withBusyState(captureAgain, async () => {
    await sendMessage({
      type: "DAEMON_MODE_CAPTURE_ACTIVE_TAB"
    });
    await refreshStatus();
  });
});

openMemory.addEventListener("click", () => {
  void withBusyState(openMemory, async () => {
    const status = await refreshStatus();

    if (status.consentRequired || !status.hostAccessGranted || status.permissionWithheld) {
      let permissionGranted = false;
      try {
        permissionGranted = await chrome.permissions.request({
          origins: OPTIONAL_PAGE_ORIGINS
        });
      } catch {
        permissionGranted = false;
      }

      const updatedStatus = await sendMessage({
        type: "DAEMON_MODE_START_REMEMBERING",
        permissionGranted
      });
      renderStatus(updatedStatus);
      return;
    }

    if (!status.service?.connected || !status.service?.compatible) {
      await chrome.tabs.create({ url: SETUP_URL });
      return;
    }

    await sendMessage({
      type: "DAEMON_MODE_OPEN_PORTAL"
    });
  });
});

blockSite.addEventListener("click", () => {
  void withBusyState(blockSite, async () => {
    await sendMessage({
      type: "DAEMON_MODE_BLOCK_ACTIVE_TAB_DOMAIN"
    });
    await refreshStatus();
  });
});

moreActivity.addEventListener("click", () => {
  activityExpanded = !activityExpanded;
  void refreshStatus();
});

initOriginButtons();
void refreshStatus();
