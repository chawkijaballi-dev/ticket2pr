# Security policy

ticket2pr runs an AI agent on source code and handles Jira and forge (GitHub, GitLab...) credentials, so
security reports are taken seriously.

## Supported versions

Only the latest release receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting
(*Security → Report a vulnerability*) on this repository, with:

- the affected version and configuration (redact secrets),
- the steps to reproduce, ideally as a ticket text or repository that triggers the issue,
- the impact you observed (e.g. a credential reachable from the agent, a guard bypass).

You will get an acknowledgement within 5 working days. Guard bypasses that require the OS
sandbox to be disabled are still in scope.

The threat model and the controls in place are described in [docs/security.md](docs/security.md).
