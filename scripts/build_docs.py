#!/usr/bin/env python3
"""Build the static documentation site and generated Python reference."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE_DIR = ROOT / "site"
API_DIR = SITE_DIR / "api"
LANDING_TEMPLATE = ROOT / "docs" / "site" / "index.html"


def project_version() -> str:
    """Read the canonical package version from pyproject.toml."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def build() -> None:
    """Create a self-contained Pages artifact under ``site/``."""
    shutil.rmtree(SITE_DIR, ignore_errors=True)
    API_DIR.mkdir(parents=True)
    version = project_version()

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pdoc",
            "agent_search_gateway",
            "--output-directory",
            str(API_DIR),
            "--docformat",
            "markdown",
            "--footer-text",
            f"agent-search-gateway {version} internal API reference",
            "--edit-url",
            (
                "agent_search_gateway="
                "https://github.com/Dingxingdi/agent-search-gateway/blob/main/"
                "src/agent_search_gateway/"
            ),
        ],
        cwd=ROOT,
        check=True,
    )

    landing = LANDING_TEMPLATE.read_text(encoding="utf-8").replace("{{PROJECT_VERSION}}", version)
    (SITE_DIR / "index.html").write_text(landing, encoding="utf-8")
    (SITE_DIR / ".nojekyll").write_text("", encoding="utf-8")

    expected_reference = API_DIR / "agent_search_gateway.html"
    if not expected_reference.is_file():
        raise RuntimeError(f"pdoc did not create {expected_reference.relative_to(ROOT)}")


if __name__ == "__main__":
    build()
