# Release process

The project uses Semantic Versioning and publishes GitHub Releases containing a source distribution and wheel. PyPI publishing is intentionally not configured yet; it should be added only through PyPI Trusted Publishing, not a long-lived API token.

## Prepare a release

1. Choose the version and update it in both `pyproject.toml` and `src/agent_search_gateway/__init__.py`.
2. Move the relevant `CHANGELOG.md` entries from `Unreleased` into a dated release section.
3. Refresh the lockfile if dependencies or dependency metadata changed:

   ```bash
   uv lock
   ```

4. Run the complete local gate:

   ```bash
   uv sync --locked --all-groups --extra mcp
   uv run --extra mcp ruff format --check src tests scripts
   uv run --extra mcp ruff check .
   uv run --extra mcp mypy src tests scripts
   uv run --extra mcp pytest -v
   uv run --group docs --extra mcp python scripts/build_docs.py
   uv build
   ```

5. Merge the release preparation pull request and verify that the required `verify` check passes on `main`.

## Publish

Create and push an annotated tag whose value exactly matches the package version:

```bash
git switch main
git pull --ff-only
git tag -a v0.1.0 -m "agent-search-gateway v0.1.0"
git push upstream v0.1.0
```

Replace `0.1.0` with the prepared version. Release candidates may use `vX.Y.Z-rc.N` with a Python version of `X.Y.ZrcN`; early prereleases may use `vX.Y.Z-pre.N` with a Python version of `X.Y.ZaN`. The release workflow rejects unsupported tags and tags that do not match both version declarations.

The workflow reruns formatting, linting, type checking, tests, and the documentation build in a read-only job. It then passes immutable distribution artifacts to a separate OIDC provenance job and finally to a minimal `contents: write` job that creates or updates the GitHub Release. Prerelease tags are marked as prereleases automatically.

## After publishing

- Verify that the release assets and provenance attestations are present.
- Install the released wheel in a clean temporary tool directory and run `agent-search-gateway --help`.
- Confirm the release notes accurately call out breaking changes and security fixes.
- Open a follow-up pull request that restores a fresh `Unreleased` section when necessary.

Do not force-move or reuse a published release tag. Publish a new patch version instead.
