# Security Policy

## Supported version

Security fixes currently target the latest commit on `main` during the technical preview. There is not yet a stable release support matrix.

## Report a vulnerability privately

Please use GitHub's **Private vulnerability reporting** for this repository when available. Include the affected commit, impact, a minimal reproduction using synthetic data, and any suggested mitigation.

If private reporting is unavailable, open a public issue containing no exploit details or private data and ask the maintainer to establish a private channel. Do not publish browsing data, SQLite files, credentials, or a working exploit.

## Security boundaries

- The local HTTP service refuses non-loopback binds and validates the request host, browser origin, and extension origin.
- The official Chrome Web Store package and the local service must share one verified extension identity. The service rejects other extension origins.
- The public `extension/` directory remains the canonical source. Store ZIPs are deterministic allowlisted release artifacts with published file inventories and SHA-256 checksums; they are not a second source tree.
- Protection checks fail closed immediately before page text is read, again at local-service ingestion, and again before agent access.
- Authentication callbacks and credential-bearing URLs are rejected at ingestion and redacted from local list/detail responses.
- MCP tools are non-destructive; destructive controls are not exposed to agents. Calls retain bounded local query metadata, not returned evidence snippets.
- An agent with separate shell or filesystem access is outside the MCP-only boundary.
- Config writes require an explicit setup-helper flag, back up existing files, preserve unrelated settings, and refuse unsafe conflicts.
- Memory and service logs use owner-only permissions in the managed macOS data directory.
- Provider prompts treat saved page content as untrusted quoted data.

## Not security bugs by themselves

- A person with access to your local user account can access your local files.
- Chrome profiles enabled under the same macOS user account share the same local Daemon service and SQLite vault.
- An owner can deliberately export or inspect their own SQLite database.
- Uninstalling the service preserves the database by design.
- The current technical preview loads the extension unpacked in Developer mode. The official Chrome Web Store package becomes the normal path only after it is published.
- Brave, Edge, and other Chromium browsers are not security-compatibility claims until separately verified.

## Safe testing

Use the included isolated smoke and MCP proof scripts. They create temporary databases and must not be pointed at real private memory. Do not test destructive behavior against another person's machine or data.

Before uploading an extension package, run `python3 tools/package-extension-proof.py` and compare the generated checksum with the intended artifact. Never upload a ZIP assembled manually from the private owner repository.
