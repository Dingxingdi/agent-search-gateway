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

The repository setting allowed a Wiki, but the corresponding Wiki Git repository still did not exist, so there were no Wiki pages or revisions to inspect. Disable the unused Wiki entry point.

A technical scan cannot prove that the project never handled customer-confidential material or real credentials. The repository owner must make that determination. If real secrets or confidential data ever entered a commit, issue, pull request, log, artifact, release, or Wiki revision, rotate credentials and prefer a new clean repository while archiving the old one privately.

## Provenance and licensing audit

Git history contains two author identities, `Dingxingdi-coder` and `Dingxingdi`, and no co-author or sign-off trailers. The repository owner must confirm that both identities are controlled by the owner and that the owner has the right to publish every historical contribution.

No Git submodules, Git LFS pointer files, or reachable Git blobs larger than 1 MiB were found. The Contributor Covenant attribution remains in `CODE_OF_CONDUCT.md`.

The runtime dependency chain uses BSD-3-Clause, MIT, and MPL-2.0 licenses. Those licenses are compatible with distributing this project under its existing MIT license; dependency notices and source obligations remain governed by each dependency's own distribution.

## Repository metadata

- [ ] Set the description to: `Local daemon and CLI for aggregated web and academic search plus admitted-URL fetching.`
- [ ] Add relevant topics such as `python`, `cli`, `search`, `web-search`, `academic-search`, `llm`, and `unix-socket`.
- [ ] Set the website field to `https://dingxingdi.github.io/agent-search-gateway/` after the first successful Pages deployment.
- [x] Keep Issues enabled.
- [ ] Disable Projects and Wiki unless they have an intentional maintainer workflow and reviewed content.
- [ ] Enable automatic deletion of head branches after merge.

## Security and dependency settings

- [ ] Enable private vulnerability reporting.
- [ ] Enable the dependency graph, Dependabot alerts, and Dependabot security updates.
- [ ] Enable GitHub secret scanning and push protection where the repository plan supports them.
- [ ] Review the repository's Actions secrets, environments, deploy keys, webhooks, installed GitHub Apps, and fine-grained tokens. Remove anything unused and minimize scopes.
- [ ] Set the default `GITHUB_TOKEN` permission to read-only and prevent GitHub Actions from creating or approving pull requests unless a reviewed workflow requires it.

The checked-in Dependabot configuration handles routine uv and GitHub Actions version updates. Security updates still depend on the repository-level security features above.

The CI workflow also runs TruffleHog for verified and unknown non-URI detections. Because the repository deliberately contains credential-shaped URLs on reserved example domains, a separate redacting scanner checks URI userinfo across affected commit snapshots and rejects every non-example hostname.

## Documentation publishing

- [ ] Enable GitHub Pages and select **GitHub Actions** as the build and deployment source.
- [ ] Run the `documentation` workflow once and verify the landing page and generated Python reference.
- [ ] Change `[project.urls].Documentation` in `pyproject.toml` to the Pages URL and update the README wording after the first successful deployment.
- [ ] Confirm the Pages environment allows deployment only from the default branch.

The workflow probes whether Pages is enabled before attempting deployment. Until an administrator enables it, default-branch runs succeed with deployment safely skipped; documentation still has a mandatory build check in CI.

## Default-branch rules

Create a ruleset for `main` with these initial controls:

- [ ] Require changes through pull requests.
- [ ] Require the `verify` status check.
- [ ] Require the branch to be up to date before merge.
- [ ] Block force pushes and branch deletion.
- [ ] Require conversation resolution.
- [ ] Apply the rule to administrators, with an explicit emergency bypass role rather than a broad permanent bypass.

For a single-maintainer repository, start with zero mandatory approving reviews so the maintainer is not deadlocked. Raise the requirement when another active maintainer can provide independent review.

Prefer squash or rebase merges and disable merge commits if linear history is desired. Keep the `verify` check name stable because the workflow deliberately exposes it as the branch-protection aggregation check.

## Releases

- [x] Merge the open-source readiness pull request and verify `main` CI.
- [x] Review `CHANGELOG.md` and create the first annotated `v0.1.0` tag.
- [x] Confirm that the release workflow attaches the wheel, source distribution, and build-provenance attestations.
- [ ] Do not enable PyPI publishing until a project and Trusted Publisher are configured. Do not store a PyPI API token in repository secrets.

## Periodic review

Repeat the secret and public-surface audit before major publicity, ownership transfer, or a security-sensitive release. Review branch rules, collaborators, bots, tokens, environments, and release permissions at least twice a year.
