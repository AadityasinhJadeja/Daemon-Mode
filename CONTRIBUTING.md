# Contributing

Daemon Mode welcomes focused fixes that improve local ownership, protection, recoverability, citation quality, or agent usefulness.

## Before opening a change

1. Open an issue for a material behavior or architecture change.
2. Keep agent data access non-destructive. Do not add agent tools for export, deletion, reset, or protection changes.
3. Keep protection ahead of capture and retrieval.
4. Do not add cloud storage, accounts, analytics, email, productivity scoring, or new capture surfaces without an accepted product decision.
5. Use synthetic fixtures. Never commit real browsing data, database files, credentials, agent config, or raw private queries.

## Development setup

Requirements are Python 3.11+, Node.js for syntax checks, and a Chromium browser for manual extension verification. Follow the source install in [README.md](README.md).

To run the companion service in the foreground while developing:

```bash
python3 tools/local-memory-service.py serve
```

## Chrome Web Store package

The extension source in `extension/` is canonical. Maintainers build the Store upload from an explicit runtime allowlist:

```bash
python3 tools/package-extension.py
python3 tools/package-extension-proof.py
```

The packager writes an ignored ZIP plus inventory and SHA-256 sidecars under `dist/`. It places `manifest.json` at the ZIP root and refuses symlinks, secret or private-data files, personal absolute paths, private-repository references, common credential patterns, and runtime references missing from the allowlist. Source artwork and unused assets are excluded.

Only upload packages produced from the public release source. Store privacy-policy and support URLs must use verified public production pages.

Before submitting a pull request, run:

```bash
python3 -m py_compile tools/*.py
find extension site tools -type f \( -name '*.js' -o -name '*.mjs' \) -exec node --check {} \;
bash -n tools/install-local-service-launch-agent.sh tools/uninstall-local-service-launch-agent.sh
python3 tools/answer-eval.py
python3 tools/mcp-retrieval-eval.py
python3 tools/mcp-loop-proof.py
python3 tools/first-run-agent-proof.py
python3 tools/setup-agent-harness-proof.py
python3 tools/package-extension-proof.py
python3 tools/smoke-test.py
```

Proofs must use temporary databases and must not read or modify the owner's real memory.

## Pull requests

- Explain the user problem and why the change is worth shipping.
- Keep the diff scoped; separate refactors from behavior changes.
- Add repeatable proof for trust, retrieval, setup, or storage behavior.
- Describe privacy/security impact and manual verification.
- Do not include screenshots with real URLs, queries, or browsing content.
- Expect contributions to use pull requests and review after the initial launch foundation is on `main`.

By contributing, you agree that your contribution is licensed under the repository's MIT License. Bundled font files remain under their original licenses.
