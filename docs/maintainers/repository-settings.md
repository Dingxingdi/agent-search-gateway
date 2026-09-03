# Repository settings for maintainers

Several open-source safeguards live in GitHub settings rather than the Git tree. Apply and periodically review this checklist with repository administrator access.

## Open-source security and public-surface audit record

The open-source readiness audit performed and refreshed on 2026-09-02 used a snapshot taken immediately after pull request #68 merged (`main` at `a10228e`) and covered:

- the complete remote Git history with TruffleHog 3.97.1, plus a redacting structural scan of all 74 commit snapshots reachable in the local audit clone;
- all 68 available issue and pull-request records, 119 issue comments, 132 review comments, and 123 review summaries, with every paginated API page included;
- all 82 available GitHub Actions run logs, comprising 1,250 extracted log files;
- both unexpired Actions artifacts, comprising four top-level files and 314 recursively extracted package files; and
- the `v0.1.0` GitHub Release, its release text, and both attached assets, comprising 157 extracted package files.

No credential requiring remediation was found. The complete-remote-history scan produced 12 unverified URI-detector findings and zero verified findings; the redacting 74-snapshot scan found no credential-bearing HTTP URL outside reserved example domains. Across 46,511,907 bytes of refreshed public-surface material, TruffleHog examined 5,021 chunks and reported four unverified URI findings, zero verified findings, and zero non-URI findings. A separate structural scan examined 15,237 URL occurrences across 1,883 text files, including 387 URLs with user information, and confirmed that every one used a reserved example domain, an explicit scanner test sentinel, or a redacted CI placeholder.

The repository setting allowed a Wiki, but the corresponding Wiki Git repository did not exist, so there were no Wiki pages or revisions to inspect. The unused Wiki and Projects entry points were disabled on 2026-09-03.

A technical scan cannot prove that the project never handled customer-confidential material or real credentials. The repository owner must make that determination. If real secrets or confidential data ever entered a commit, issue, pull request, log, artifact, release, or Wiki revision, rotate credentials and prefer a new clean repository while archiving the old one privately.

## Provenance and licensing audit

Git history contains two author identities, `Dingxingdi-coder` and `Dingxingdi`, and no co-author or sign-off trailers. The repository owner must confirm that both identities are controlled by the owner and that the owner has the right to publish every historical contribution.

No Git submodules, Git LFS pointer files, or reachable Git blobs larger than 1 MiB were found. The Contributor Covenant attribution remains in `CODE_OF_CONDUCT.md`.

The runtime dependency chain uses BSD-3-Clause, MIT, and MPL-2.0 licenses. Those licenses are compatible with distributing this project under its existing MIT license; dependency notices and source obligations remain governed by each dependency's own distribution.

## Repository metadata

Configuration in this section was verified through the GitHub API on 2026-09-03.

- [x] Set the description to: `Local daemon and CLI for aggregated web and academic search plus admitted-URL fetching.`
- [x] Add the `agent-search`, `academic-search`, `cli`, `llm`, `python`, `search`, `unix-socket`, and `web-search` topics.
- [x] Set the website field to `https://dingxingdi.github.io/agent-search-gateway/`.
- [x] Keep Issues enabled.
- [x] Disable unused Projects and Wiki entry points.
- [x] Enable automatic deletion of head branches after merge.
- [x] Disable merge commits while retaining squash and rebase merges.

## Security and dependency settings

- [x] Enable private vulnerability reporting.
- [x] Enable the dependency graph, Dependabot alerts, and Dependabot security updates.
- [x] Enable GitHub secret scanning and push protection.
- [x] Enable CodeQL default setup for GitHub Actions workflows and Python, and verify its initial analysis succeeds.
- [x] Confirm that repository, Dependabot, Codespaces, and `github-pages` environment secret stores contain no secrets, and that the repository has no deploy keys or webhooks.
- [ ] Review installed GitHub Apps and personal fine-grained tokens in the GitHub account settings; the authenticated OAuth CLI token cannot enumerate those account-level grants.
- [x] Set the default `GITHUB_TOKEN` permission to read-only and prevent GitHub Actions from approving pull requests.
- [x] Restrict Actions to GitHub-owned actions plus `astral-sh/setup-uv` and `trufflesecurity/trufflehog`, and require every action reference to use a full commit SHA.

The checked-in Dependabot configuration handles routine uv and GitHub Actions version updates. Repository-level alerts and automated security updates are enabled. The CI workflow also audits the locked dependency graph.

The CI workflow runs TruffleHog for verified and unknown non-URI detections. Because the repository deliberately contains credential-shaped URLs on reserved example domains, a separate redacting scanner checks URI userinfo across affected commit snapshots and rejects every non-example hostname.

## Documentation publishing

- [x] Enable GitHub Pages and select **GitHub Actions** as the build and deployment source.
- [x] Run the `documentation` workflow and verify the landing page and generated Python reference.
- [x] Set `[project.urls].Documentation` and the README documentation link to the Pages URL.
- [x] Restrict the `github-pages` environment deployment branch policy to `main`.

The workflow probes whether Pages is enabled before attempting deployment. Documentation has a mandatory build check in CI, and successful default-branch runs deploy the generated site through the branch-restricted `github-pages` environment.

## Default-branch rules

The active `Protect main` ruleset has these controls:

- [x] Require changes through pull requests.
- [x] Require the `verify` status check.
- [x] Require the branch to be up to date before merge.
- [x] Block force pushes and branch deletion.
- [x] Require conversation resolution.
- [x] Require linear history and allow only squash or rebase merges.
- [x] Apply the ruleset to administrators with no permanent bypass actors. In an emergency, an administrator must explicitly disable or edit the ruleset, leaving an auditable settings change.

For a single-maintainer repository, the ruleset requires zero mandatory approving reviews so the maintainer is not deadlocked. Raise the requirement when another active maintainer can provide independent review. Keep the `verify` check name stable because the workflow deliberately exposes it as the branch-protection aggregation check.

## Releases

- [x] Merge the open-source readiness pull request and verify `main` CI.
- [x] Review `CHANGELOG.md` and create the first annotated `v0.1.0` tag.
- [x] Confirm that the release workflow attaches the wheel, source distribution, and build-provenance attestations.
- [x] Keep PyPI publishing disabled until a project and Trusted Publisher are configured; no PyPI token is stored in repository or environment secrets.

## Periodic review

Repeat the secret and public-surface audit before major publicity, ownership transfer, or a security-sensitive release. Review branch rules, collaborators, bots, tokens, environments, and release permissions at least twice a year.
