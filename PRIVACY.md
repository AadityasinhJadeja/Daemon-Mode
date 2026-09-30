# Privacy

Daemon Mode is designed to keep browsing memory under the owner's control.

## Chrome Web Store limited use

Daemon uses browsing activity and website content only to provide its disclosed single purpose: a local, owner-controlled memory that the user's chosen AI agents can search through non-destructive tools. Daemon does not sell this data, use it for advertising or credit decisions, or send it to the developer or other people. Access to the local files follows the owner's macOS permissions.

Daemon Mode's use of information received from Chrome extension APIs adheres to the [Chrome Web Store User Data Policy](https://developer.chrome.com/docs/webstore/program-policies/policies), including its Limited Use requirements.

## What is stored

For pages that pass protection checks, Daemon can store the page URL, title, domain, capture timestamp, extracted page text, text length, and local capture metadata in SQLite. It also stores bounded agent-tool diagnostics such as tool name, status, timing, evidence count, and a bounded query for owner-visible activity review.

Daemon does not intentionally store browser cookies, passwords, form values, or account credentials. Page text can still contain sensitive information, so review protection settings before browsing private material.

## Where it is stored

Memory is stored locally. The default macOS path is:

```text
~/Library/Application Support/Daemon Mode/memory.sqlite3
```

The extension sends allowed page data only to the companion service on the same computer. The service binds to loopback only. Daemon has no account system, cloud database, analytics service, advertising SDK, or remote sync.

On macOS, one local service and SQLite vault are shared by the Chrome profiles enabled under the same macOS user account. If Daemon is enabled in multiple Chrome profiles for that macOS account, their allowed captures are merged into that one vault. Separate macOS user accounts have separate default vaults.

## Protection

The extension checks the current page immediately before reading its text. Built-in sensitive categories, site-specific preferences, password/private-page checks, authentication callbacks, credential-bearing URLs, and the owner's blocklist are enforced again by the local service before storage and again before agent retrieval.

If the protection check is unavailable, non-successful, or malformed, the extension fails closed and does not read page text.

Protected events may retain bounded diagnostics such as domain, reason, status, and timestamp so the owner can verify that protection happened. They do not retain the protected page text.

## Agent access

Daemon's MCP data surface is non-destructive. Agents can search memory, retrieve citation-ready evidence, create an evidence-grounded answer, request an exact agent-safe activity summary, and check bounded protection status. Agents cannot export, delete, clear, or modify protection rules through MCP.

Query logs do not store retrieved evidence snippets. The default diagnostics report redacts query text and the absolute database path.

An agent that separately has general shell or filesystem access to the Mac is outside this MCP-only boundary.

The connected agent client and its model can process the evidence returned through MCP under that client's own privacy terms.

## Optional AI provider

No provider is needed for local capture, search, citations, protection, or exact summaries. If the owner explicitly enables an AI provider, selected evidence and the question are sent to that provider to produce prose. The provider receives only the evidence selected for that request; its own privacy terms then apply.

## Owner controls

The local dashboard provides human-only controls to:

- export the SQLite database;
- permanently delete one capture;
- clear all captures and local agent-query history while preserving protection rules;
- add, remove, or change protection rules.

SQLite secure deletion, physical row/FTS deletion, database compaction, and WAL truncation reduce recoverable remnants after confirmed deletion. Filesystem backups, snapshots, or copies made outside Daemon are outside that guarantee.

Uninstalling the background service does not delete memory. This prevents accidental data loss and lets the owner export or clear it separately.

## Browser permissions and network behavior

Daemon asks for access to normal HTTP and HTTPS pages because remembering allowed page content is its single browser purpose. `storage` keeps extension consent and preferences, and `webNavigation` observes page navigation and single-page-app changes. Broad website access is not used for advertising, analytics, or developer-owned servers.

Daemon shows an in-extension disclosure and requires an explicit owner action before capture begins. Incognito use is not allowed. Removing the extension stops future capture but does not delete the separately stored SQLite vault.

The extension sends captures to the loopback service only. The local service makes no analytics or telemetry calls. A network call to an AI provider happens only when the owner configures a provider and explicitly uses provider-backed answer generation; no provider is called for capture, retrieval, citations, protection, or exact summaries.

Protection reduces exposure but cannot guarantee classification of every sensitive sentence rendered on an otherwise allowed page. A malicious local administrator or process with access to the owner's files is outside Daemon's current protection boundary.

Google Chrome on macOS is the verified browser target for the first Store release. Brave, Edge, and other Chromium browsers have not been verified end to end and are not currently support promises.

## Reporting a privacy issue

Do not include real browsing text, URLs, database files, API keys, or other private data in a public issue. Follow the private reporting path in [SECURITY.md](SECURITY.md).
