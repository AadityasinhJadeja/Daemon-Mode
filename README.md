# Daemon Mode

[![Verify](https://github.com/AadityasinhJadeja/Daemon-Mode/actions/workflows/ci.yml/badge.svg)](https://github.com/AadityasinhJadeja/Daemon-Mode/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-173f43.svg)](LICENSE)

Local, private, citation-backed browsing memory for AI agents.

[Website](https://daemonmode-ai.vercel.app) · [Documentation](https://daemonmode-ai.vercel.app/docs/)

Daemon saves useful content from allowed Chrome pages to a SQLite file on your Mac. Connect it to Codex, Claude Code, Claude Desktop, or Cursor over MCP, and your agent can find past research with the original links and capture times.

- No account, cloud service, or AI provider key is required.
- Sensitive pages are protected before their text is read.
- Agents cannot delete, clear, export, or change protection rules.

Daemon currently supports Google Chrome on macOS and is installed from this repository.

## When it helps

Daemon is useful when research is spread across tabs and days: comparing jobs or products, collecting material for writing, or following a technical topic over time. It is not a replacement for bookmarks, project files, or complete browser history.

## How it works

1. The Chrome extension reads allowed pages.
2. A background service stores them locally in SQLite.
3. A connected agent searches that memory through MCP.
4. Results include the saved source and capture time.

The dashboard is only for viewing and managing memory. It does not need to stay open.

## Install

You need macOS, Python 3.11 or newer, Git, Google Chrome, and an MCP-compatible agent.

### 1. Start the local service

```bash
git clone https://github.com/AadityasinhJadeja/Daemon-Mode.git
cd Daemon-Mode
./tools/install-local-service-launch-agent.sh
```

The service starts at login and runs in the background. Running the installer again repairs or updates it without deleting your memory.

### 2. Load the Chrome extension

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Select **Load unpacked**.
4. Choose the repository's `extension` folder.
5. Open the extension and grant access to the sites you want Daemon to remember.

### 3. Connect an agent

Preview the available setup commands:

```bash
python3 tools/setup-agent-harness.py
```

Connect Codex:

```bash
python3 tools/setup-agent-harness.py --apply codex --scope user
```

You can replace `codex` with `claude-code`, `cursor`, or `claude-desktop`. See [MCP setup](docs/MCP_SETUP.md) for client-specific details and rollback instructions.

### 4. Try it

Open a new agent conversation and ask:

```text
Use Daemon MCP to summarize the topics in my recent saved pages.
Cite the source URLs and say when there is not enough evidence.
```

## Privacy

- Memory stays in `~/Library/Application Support/Daemon Mode/memory.sqlite3`.
- The local service listens only on `127.0.0.1`.
- Banking, email, authentication pages, and your blocked sites are protected before reading, storage, and retrieval.
- Export, deletion, and protection settings stay under human control.
- Daemon has no analytics, advertising, account system, or cloud sync.

Read [Privacy](PRIVACY.md) and [Security](SECURITY.md) before using real browsing data.

## Current limits

- Chrome on macOS is the only verified browser and platform combination.
- The extension and local service are both required.
- MCP access is non-destructive, but calls keep bounded local diagnostics such as tool name, outcome, timing, and evidence count.
- An agent with separate shell or filesystem access is outside Daemon's MCP boundary.

## Development

Run the isolated verification suite:

```bash
python3 tools/smoke-test.py
```

The tests use temporary databases and do not read your normal Daemon memory. See [Contributing](CONTRIBUTING.md) for development guidance.

Daemon Mode is available under the [MIT License](LICENSE).
