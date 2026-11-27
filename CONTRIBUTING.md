# Contributing to agent-search-gateway

Thank you for considering a contribution. Bug reports, documentation improvements, provider fixes, tests, and focused feature proposals are welcome.

## Before you start

- Use a public issue to discuss substantial behavior changes before investing in an implementation.
- Use the repository's private vulnerability reporting flow for security issues; do not disclose vulnerabilities or real credentials in a public issue.
- Keep pull requests focused. Unrelated refactors make review and rollback harder.
- The supported end-user interface is documented in [docs/public-interface.md](docs/public-interface.md). Treat other Python modules and the daemon socket protocol as internal unless a change explicitly promotes them to a supported interface.

## Development environment

The project targets Unix-like systems and Python 3.11 or newer. Install [uv](https://docs.astral.sh/uv/), then create the locked development environment:

```bash
git clone https://github.com/Dingxingdi/agent-search-gateway.git
cd agent-search-gateway
uv sync --locked --all-groups --extra mcp
```

The full development suite includes MCP coverage, so it requires the `mcp` extra. The normal test suite uses fakes, local Unix sockets, and mock transports. It does not require provider credentials or network access.

## Required checks

Run the same checks used by continuous integration:

```bash
uv run --extra mcp ruff format --check src tests scripts
uv run --extra mcp ruff check .
uv run --extra mcp mypy src tests scripts
uv run --extra mcp pytest -v
uv run --group docs --extra mcp python scripts/build_docs.py
```

For packaging changes, also run:

```bash
uv build
```

Live provider checks are opt-in and are not required for ordinary contributions. Never paste credentials into configuration files, test fixtures, command output, screenshots, issues, pull requests, or CI logs.

## Contribution guidelines

When changing behavior:

1. Add or update tests that fail without the change.
2. Update the README, example configuration, or public-interface documentation when users must act differently.
3. Add a concise entry under `Unreleased` in `CHANGELOG.md` for user-visible changes.
4. Preserve the no-network default test suite. Put live checks behind the existing explicit integration-test opt-in.
5. For provider adapters, validate malformed and partial provider responses. Never log authentication values or unredacted request/response bodies; use the shared DEBUG-only, redacted and bounded body-preview path when body diagnostics are needed.

Conventional-style commit subjects such as `fix:`, `feat:`, `docs:`, and `chore:` are encouraged but not mechanically required.

## Pull requests

A pull request should explain the problem, the chosen approach, compatibility implications, and how it was verified. CI must pass before merge. Maintainers may request smaller commits, additional tests, documentation, or a design discussion for broad changes.

By submitting a contribution, you agree that it may be distributed under this repository's [MIT License](LICENSE).

All participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
