# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Because the project is pre-1.0, minor releases may contain breaking changes when they are called out in the release notes.

The first public release is `v0.1.0`.

## [Unreleased]

## [0.1.0] - 2026-09-02

### Added

- Open-source contribution, security, support, conduct, interface-stability, and release documentation.
- Hardened continuous integration with enforced Ruff formatting, least-privilege permissions, immutable action references, Linux/Python and macOS compatibility coverage, packaging checks, documentation builds, and layered secret scanning.
- A GitHub Pages documentation site with a generated pdoc reference and safe deployment gating until Pages is enabled.
- Automated dependency update configuration and a tag-driven, privilege-separated GitHub Release workflow with build provenance attestations.

### Security

- Audited the complete Git history, fully paginated issue and pull-request text, review comments, Actions logs, artifacts, releases, and Wiki state before the open-source readiness change.
- Raised the pytest floor to 9.0.3, upgraded the locked test stack, and added locked dependency auditing to CI and release verification to remediate CVE-2025-71176.
