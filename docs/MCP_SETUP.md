# MCP Setup Helper

Daemon's setup helper prints machine-correct local MCP configuration without changing anything by default.

This is the agent-connection step, not the complete product installation. Daemon also needs the companion local service and Chrome extension. During the source-installed technical preview, load `extension/` unpacked through Chrome's Developer mode. The official Chrome Web Store URL will replace that step only after publication.

## Local service lifecycle

The dashboard is only a view into Daemon. Its browser tab does not keep capture running and does not need to stay open.

On macOS, `./tools/install-local-service-launch-agent.sh` installs a per-user LaunchAgent with `RunAtLoad` and `KeepAlive`. The local service starts after that macOS user logs in, returns after a normal reboot once the user logs in again, and is restarted by macOS if the process exits unexpectedly. Because it is a user LaunchAgent, it does not run before login or while that user is logged out.

The installer copies the service into `~/Library/Application Support/Daemon Mode/runtime/`. Moving or deleting the downloaded Git checkout after installation does not break the installed service. When Daemon is updated, run the installer from the new checkout once to atomically activate the verified runtime while preserving the SQLite memory file and the prior runtime release.

If the extension says **Daemon is offline**, run the same installer again. A repeat install is an idempotent repair: it verifies the managed files and LaunchAgent configuration, reloads an unloaded agent, and refuses to report success if the agent does not remain loaded. Then verify the local endpoint:

```bash
curl --fail --silent --show-error http://127.0.0.1:4317/health
```

Stopping Daemon is explicit and preserves memory:

```bash
./tools/uninstall-local-service-launch-agent.sh
```

This disables the LaunchAgent but keeps the managed runtime and `memory.sqlite3` so reinstalling can resume safely.

## Dry Run

From the repo root:

```bash
python3 tools/setup-agent-harness.py
```

The output shows:

- the detected Daemon repo path,
- the absolute local SQLite memory path,
- the detected absolute Python 3 executable,
- the local MCP server command,
- setup for Claude Code, Codex, Cursor, and Claude Desktop,
- the honest ChatGPT limitation.

The helper does not open or read the SQLite database. It only includes the path in generated configuration.

Show one client:

```bash
python3 tools/setup-agent-harness.py --client codex
```

## Explicit Apply

Writing config requires `--apply` for exactly one client.

```bash
python3 tools/setup-agent-harness.py --apply claude-code --scope user
python3 tools/setup-agent-harness.py --apply codex --scope user
python3 tools/setup-agent-harness.py --apply cursor --scope user
python3 tools/setup-agent-harness.py --apply claude-desktop --scope user
```

Supported helper scopes:

- Claude Code: `local`, `user`, or `project`.
- Codex: `user`.
- Cursor: `user` or `project`.
- Claude Desktop: `user`.

The default is `user`.

### Existing Daemon connections

The Store-ready local service runs from a managed runtime under `~/Library/Application Support/Daemon Mode/` instead of depending on a Git checkout. If a supported client already has an older Daemon entry that points into this repository, migrate it explicitly after installing the managed service:

```bash
python3 tools/setup-agent-harness.py --apply codex --scope user --migrate-existing-daemon
```

Replace `codex` with the client you use. Migration is deliberately narrow: the helper only replaces a recognized older Daemon command when it points at the same memory database. It backs up the existing config first and refuses ambiguous entries, a different database path, malformed config, or symlinked config files.

## Safety Behavior

- Existing config is parsed before any write.
- Unrelated settings and MCP servers are preserved.
- An existing config file is copied to a timestamped `.daemon-backup-*` file before modification.
- Writes use a temporary file plus atomic replacement.
- A repeat run is a no-op when the Daemon entry already matches.
- Idempotence is exact: an older entry using bare `python3` is treated as a conflict instead of being silently accepted or rewritten.
- Replacing a recognized repo-pinned Daemon entry requires the explicit `--migrate-existing-daemon` flag and the same memory database path.
- A conflicting Daemon entry, malformed JSON/TOML, symlinked config, or unsupported scope is refused instead of overwritten.
- Generated Codex TOML is parsed again before writing; non-extendable inline table shapes are refused.
- Agent data access remains non-destructive. This helper never adds export, delete, clear, or protection-rule tools. MCP calls write bounded local query metadata for the owner-visible activity view, but not returned evidence snippets.

To roll back, stop the client, move the timestamped backup back to the original config path, and restart the client.

## Client Notes

Daemon's dependency-free stdio server supports both the legacy `2025-06-18` initialization flow and the modern `2026-07-28` per-request metadata flow. Modern clients may discover capabilities with `server/discover`; legacy clients continue to use `initialize`. In both eras, Daemon exposes the same non-destructive tools and applies protection filtering before returning evidence.

Claude Code supports local stdio servers and `local`, `project`, and `user` scopes. Cursor reads custom MCP servers from `.cursor/mcp.json` or `~/.cursor/mcp.json`. Codex reads user MCP configuration from `~/.codex/config.toml`. Claude Desktop reads local MCP configuration from its platform-specific `claude_desktop_config.json`.

ChatGPT is not configured by this helper. ChatGPT's custom integration path is remote MCP/apps/connectors, not direct access to this local stdio process. A future ChatGPT bridge must preserve non-destructive tools, local protection filtering, citations, and explicit privacy boundaries.

Current client references:

- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [Cursor MCP](https://cursor.com/docs/context/mcp)
- [OpenAI Codex configuration](https://developers.openai.com/codex/config-reference)
- [Claude Desktop local MCP](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)
- [ChatGPT remote MCP/apps](https://developers.openai.com/api/docs/mcp)

The first verified browser target is Google Chrome on macOS. Brave, Edge, and other Chromium browsers are not yet supported claims. Multiple Chrome profiles enabled under one macOS user account connect to the same local service and merge allowed captures into that macOS user's SQLite vault.

## Repeatable Proof

```bash
python3 tools/setup-agent-harness-proof.py
```

The proof uses temporary config files only. It checks generated paths, absolute Python launch configuration, shell-safe Claude JSON, backups, idempotence, unrelated-setting preservation, malformed/conflicting/non-extendable config refusal, private-memory non-disclosure, and the ChatGPT limitation.
