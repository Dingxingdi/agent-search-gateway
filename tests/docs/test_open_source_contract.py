import re
import tomllib
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_PATHS = (
    ".editorconfig",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/dependabot.yml",
    ".github/pull_request_template.md",
    ".github/workflows/ci.yml",
    ".github/workflows/pages.yml",
    ".github/workflows/release.yml",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "RELEASING.md",
    "SECURITY.md",
    "SUPPORT.md",
    "docs/maintainers/repository-settings.md",
    "docs/public-interface.md",
    "docs/site/index.html",
)
GOVERNANCE_MARKDOWN = tuple(
    ROOT / path for path in REQUIRED_PATHS if path.endswith(".md") and (ROOT / path).is_file()
)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
ACTION_REFERENCE = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
PINNED_ACTION = re.compile(r"[^@]+@[0-9a-f]{40}")


def test_required_open_source_files_exist() -> None:
    missing = [path for path in REQUIRED_PATHS if not (ROOT / path).is_file()]
    assert not missing


def test_governance_markdown_has_no_broken_local_links() -> None:
    broken: list[str] = []
    for document in GOVERNANCE_MARKDOWN:
        text = document.read_text(encoding="utf-8")
        for raw_target in MARKDOWN_LINK.findall(text):
            target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_text = unquote(target.split("#", 1)[0])
            if not path_text:
                continue
            resolved = (document.parent / path_text).resolve()
            if not resolved.exists():
                broken.append(f"{document.relative_to(ROOT)} -> {target}")
    assert not broken


def test_python_package_metadata_is_open_source_ready() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]

    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["readme"] == "README.md"
    assert project["requires-python"] == ">=3.11"
    assert project["urls"]["Documentation"] == (
        "https://dingxingdi.github.io/agent-search-gateway/"
    )
    assert project["urls"]["Repository"].endswith("agent-search-gateway.git")
    assert project["urls"]["Security"].endswith("/security/policy")


def test_readme_links_to_the_published_documentation() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "https://dingxingdi.github.io/agent-search-gateway/" in readme
    assert "After a repository administrator enables GitHub Pages" not in readme


def test_workflow_actions_are_pinned_and_checkout_does_not_persist_credentials() -> None:
    workflows = tuple(sorted((ROOT / ".github" / "workflows").glob("*.yml")))
    assert workflows

    checkout_count = 0
    disabled_credentials_count = 0
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        assert "permissions: {}" in text
        for reference in ACTION_REFERENCE.findall(text):
            if reference.startswith("./"):
                continue
            assert PINNED_ACTION.fullmatch(reference), (
                f"{workflow.relative_to(ROOT)} has mutable action reference {reference!r}"
            )
        checkout_count += text.count("uses: actions/checkout@")
        disabled_credentials_count += text.count("persist-credentials: false")

    assert checkout_count > 0
    assert disabled_credentials_count == checkout_count


def test_release_and_pages_workflows_preserve_privilege_separation() -> None:
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    pages = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")

    assert "needs: [build, provenance]" in release
    assert "actions/attest-build-provenance@" in release
    assert "contents: write" in release
    assert "enable-cache: false" in release
    assert "uv audit --locked --preview-features audit-command" in release
    assert "workflow_dispatch:" in release
    assert "tag_name:" in release
    assert "GH_REPO: ${{ github.repository }}" in release
    assert "ref: ${{ inputs.tag_name || github.ref }}" in release
    assert "Check whether Pages is enabled" in pages
    assert "GitHub Pages is not enabled; documentation deployment is skipped." in pages
