from pathlib import Path

_ROOT = Path(__file__).parents[2]


def test_readme_separates_end_user_tool_install_from_locked_development() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")

    assert "uv tool install ." in readme
    assert "agent-search-gateway doctor" in readme
    assert "agent-search-gateway start" in readme
    assert "agent-search-gateway start --debug" in readme

    assert "uv sync --locked" in readme
    assert "uv run ruff check ." in readme
    assert "uv run mypy src tests" in readme
    assert "uv run pytest -v" in readme

    tool_install_index = readme.index("uv tool install .")
    development_index = readme.index("uv sync --locked")
    assert tool_install_index < development_index


def test_readme_documents_debug_and_doctor_operational_contract() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")

    assert "~/.cache/agent-search-gateway-cli/logs/debug.log" in readme
    assert "5 MiB" in readme
    assert "3 backups" in readme
    assert "Target URL path/query/fragment values" in readme
    assert "URI userinfo is stripped" in readme
    assert "HTTP transport endpoint fields" in readme
    assert "sensitive local artifacts" in readme
    assert "query/prompt/page/model-response bodies" in readme
    assert "authentication values" in readme
    assert "final-output-only" in readme
    assert "no network" in readme
    assert "daemon not running" in readme
    assert "informational" in readme


def test_ci_preserves_locked_verification_and_smokes_built_wheel() -> None:
    workflow = (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv sync --locked" in workflow
    assert "uv audit --locked --preview-features audit-command" in workflow
    assert "uv run ruff check ." in workflow
    assert "uv run mypy src tests" in workflow
    assert "uv run pytest -v" in workflow
    assert "Build distributions" in workflow
    assert "Smoke-test the built wheel" in workflow
    assert "UV_TOOL_DIR: ${{ runner.temp }}/agent-search-gateway-tools" in workflow
    assert "UV_TOOL_BIN_DIR: ${{ runner.temp }}/agent-search-gateway-bin" in workflow
    assert "uv tool install --force dist/*.whl" in workflow
    assert '"$UV_TOOL_BIN_DIR/agent-search-gateway" --help' in workflow
    assert '"$UV_TOOL_BIN_DIR/agent-search-gateway" start --help' in workflow
    assert "permissions: {}" in workflow
    assert "persist-credentials: false" in workflow
    assert "uses: actions/checkout@v" not in workflow
    assert "uses: astral-sh/setup-uv@v" not in workflow
