# Security policy

## Supported versions

`agent-search-gateway` is an alpha project. The current `main` branch and the latest `2.x` release receive security fixes on a best-effort basis.

| Version | Supported |
| --- | --- |
| `main` / latest `2.x` | Yes, best effort |
| Older commits or releases | No |

## Reporting a vulnerability

Do not open a public issue, pull request, discussion, or comment for a suspected vulnerability.

Use GitHub's private **Report a vulnerability** flow in the repository's Security tab. Include:

- the affected command, provider, configuration, or version;
- a minimal reproduction using fake credentials and non-sensitive data;
- the security impact and realistic attack conditions;
- any proposed mitigation or patch;
- whether the issue is already public elsewhere.

If private vulnerability reporting is temporarily unavailable, contact the repository owner through a private contact method listed on the owner's GitHub profile. If no private method is listed, open a minimal public issue asking the maintainer to establish a private channel, without including vulnerability details.

Reports are handled as maintainer capacity permits; this project does not promise a response-time or remediation-time service-level agreement. The maintainer will coordinate disclosure and credit with the reporter when practical.

## Sensitive data and credentials

Provider API keys belong only in the local process environment. Configuration files should contain environment-variable names, never credential values.

Debug logs can contain target URLs, paths, queries, and fragments. Treat debug logs and generated result files as potentially sensitive, review them before sharing, and remove signed URLs, personal data, tokens, and proprietary content.

If a credential may have been exposed, rotate or revoke it immediately. Removing it from the latest commit is not sufficient because Git history, forks, caches, CI logs, artifacts, issues, and pull requests may retain copies.

## Scope notes

Useful reports include credential disclosure, authentication-value logging, unsafe URL handling, local socket authorization issues, command injection, dependency compromise, denial-of-service paths, or provider-response handling that crosses a documented trust boundary.

Ordinary bugs and feature requests belong in the public issue tracker. Provider account, billing, quota, and service-availability questions generally belong with the relevant provider.
