from pathlib import Path

_ROOT = Path(__file__).parents[2]


def test_readme_separates_end_user_tool_install_from_locked_development() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")

    assert "uv tool install ." in readme
    assert "agent-search-gateway doctor" in readme
    assert "agent-search-gateway start" in readme
    assert "agent-search-gateway start --debug" in readme

    assert "uv sync --locked --all-groups --extra mcp" in readme
    assert "uv run --extra mcp ruff format --check src tests scripts" in readme
    assert "uv run --extra mcp ruff check ." in readme
    assert "uv run --extra mcp mypy src tests scripts" in readme
    assert "uv run --extra mcp pytest -v" in readme
    assert "uv run --group docs --extra mcp python scripts/build_docs.py" in readme

    tool_install_index = readme.index("uv tool install .")
    development_index = readme.index("uv sync --locked")
    assert tool_install_index < development_index


def test_optional_mcp_install_and_client_setup() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "uv tool install '.[mcp]'" in readme
    assert "`agent-search-gateway-mcp`" in readme
    assert "STDIO (default)" in readme
    assert "agent-search-gateway-mcp --transport streamable-http" in readme
    assert "http://127.0.0.1:8765/mcp" in readme

    setup = readme.partition("### MCP client setup")[2].partition("\n## ")[0]
    for term in (
        "agent-search-gateway start",
        "foreground",
        "independent",
        "discovery",
        "single trust domain",
        "authentication",
        "TLS",
    ):
        assert term in setup


def test_public_mcp_tool_signatures_and_result_contract() -> None:
    policy = (_ROOT / "docs/public-interface.md").read_text(encoding="utf-8")
    contract = policy.partition("### MCP server")[2].partition("\n### ")[0]
    signatures = {line.split("`")[1] for line in contract.splitlines() if line.startswith("| `")}
    assert signatures == {
        "keyword_search(query, provider=null)",
        "paper_search(query, provider=null)",
        'llm_search(prompt, scope="web", provider=null, model=null)',
        'agent_search(prompt, scope="web", provider=null, model=null)',
        "url_fetch(url, focus=null)",
    }
    for term in (
        "`web`",
        "`paper`",
        "`all`",
        "complete JSONL text",
        "empty string",
        "literal text",
        "URL unavailable: it may have failed content validation or been flagged as unsafe.",
        "TextContent",
        "isError",
        "INVALID_PARAMS",
    ):
        assert term in contract


def test_pages_builds_api_docs_with_the_mcp_extra() -> None:
    workflow = (_ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    assert "run: uv sync --locked --all-groups --extra mcp\n" in workflow
    assert "run: uv run --group docs --extra mcp python scripts/build_docs.py\n" in workflow


def test_readme_documents_debug_and_doctor_operational_contract() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")

    assert "~/.cache/agent-search-gateway-cli/logs/debug.log" in readme
    assert "5 MiB" in readme
    assert "3 backups" in readme
    assert "Target URL path/query/fragment values" in readme
    assert "URI userinfo is stripped" in readme
    assert "HTTP transport endpoint fields" in readme
    assert "sensitive local artifacts" in readme
    assert "upstream_body_preview" in readme
    assert "request_input_truncated" in readme
    assert "response_preview_truncated" in readme
    assert "authentication values" in readme
    assert "final-output-only" in readme
    assert "no network requests when `open_websearch` is absent" in readme
    assert "both Search and Fetch are disabled" in readme
    assert "either capability enabled" in readme
    assert "one credential-free `GET /health`" in readme
    assert "failed probe gives exit code 1" in readme
    assert "daemon liveness only" in readme
    assert "daemon not running" in readme
    assert "informational" in readme


def test_ci_preserves_locked_verification_and_smokes_built_wheel() -> None:
    workflow = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "run: uv sync --locked --group dev --extra mcp\n" in workflow
    assert "run: uv audit --locked --preview-features audit-command\n" in workflow
    assert "uv run --extra mcp ruff format --check src tests scripts" in workflow
    assert "uv run --extra mcp ruff check ." in workflow
    assert "uv run --extra mcp mypy src tests scripts" in workflow

    assert 'uv sync --locked --group dev --extra mcp --python "$PYTHON_VERSION"' in workflow
    assert "uv run --extra mcp pytest -v" in workflow
    tests_job = workflow.split("\n  tests:\n")[1].split("\n  docs:\n")[0]
    assert 'WEB_SEARCH_RUN_INTEGRATION: "0"' in tests_job
    for version in ("3.11", "3.12", "3.13", "3.14"):
        assert f'- os: ubuntu-latest\n            python-version: "{version}"' in tests_job
    assert '- os: macos-latest\n            python-version: "3.12"' in tests_job

    assert "uv sync --locked --group docs --extra mcp" in workflow
    assert "uv run --group docs --extra mcp python scripts/build_docs.py" in workflow
    assert "test -f site/index.html" in workflow
    assert "test -f site/api/agent_search_gateway.html" in workflow

    assert "Build distributions" in workflow
    assert "Smoke-test the built wheel" in workflow
    base, extra = workflow.split("- name: Smoke-test the built wheel with the MCP extra")
    tool_python = '"$UV_TOOL_DIR/agent-search-gateway/bin/python"'
    wheel_smoke = f"{tool_python} tests/mcp/wheel_smoke.py"
    assert "UV_TOOL_DIR: ${{ runner.temp }}/agent-search-gateway-tools" in base
    assert "UV_TOOL_BIN_DIR: ${{ runner.temp }}/agent-search-gateway-bin" in base
    assert "uv tool install --force dist/*.whl" in base
    assert '"$UV_TOOL_BIN_DIR/agent-search-gateway" --help' in base
    assert '"$UV_TOOL_BIN_DIR/agent-search-gateway" start --help' in base
    assert f'{wheel_smoke} --mode base --bin-dir "$UV_TOOL_BIN_DIR"' in base
    assert "UV_TOOL_DIR: ${{ runner.temp }}/agent-search-gateway-mcp-tools" in extra
    assert "UV_TOOL_BIN_DIR: ${{ runner.temp }}/agent-search-gateway-mcp-bin" in extra
    assert "agent-search-gateway[mcp] @ " in extra
    assert 'uv tool install --force "$mcp_wheel_requirement"' in extra
    assert f'{wheel_smoke} --mode mcp --bin-dir "$UV_TOOL_BIN_DIR"' in extra

    assert "secret-scan:" in workflow
    assert "python scripts/check_sensitive_urls.py" in workflow
    assert "uses: trufflesecurity/trufflehog@" in workflow
    assert "needs: [quality, tests, docs, package, secret-scan]" in workflow
    assert "permissions: {}" in workflow
    assert "persist-credentials: false" in workflow
    assert "uses: actions/checkout@v" not in workflow
    assert "uses: astral-sh/setup-uv@v" not in workflow
