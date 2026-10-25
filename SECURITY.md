# Security Policy

## Reporting a vulnerability
Please do not open a public issue for security problems. Report them privately through the
repository's "Report a vulnerability" (GitHub security advisories) feature, or by email to the maintainer.
Include steps to reproduce and the affected version. You can expect an initial response within a few days.

## Hardening guidance
- Use a Grafana service-account token with the **Viewer** role only.
- Keep TLS verification enabled; prefer `PMM_CA_BUNDLE` over `PMM_VERIFY=0`.
- Keep `~/.config/mcp/pmm.env` private (mode 600) and never commit it.
- Rotate the token if it may have been exposed.
