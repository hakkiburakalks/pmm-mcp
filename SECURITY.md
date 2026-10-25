# Security Policy

## Reporting a vulnerability
Please do not open a public issue for security problems. Report them privately through the
repository's "Report a vulnerability" (GitHub security advisories) feature, or by email to the maintainer.
Include steps to reproduce and the affected version. You can expect an initial response within a few days.

## Hardening guidance
- Use a Grafana service-account token with the **Viewer** role. That is all the read tools need.
- Leave `PMM_ALLOW_WRITE` unset unless you really want the model to change things. Write tools (silences,
  annotations) need an Editor/Admin token **and** `PMM_ALLOW_WRITE=1`; each one only previews its change until
  called with `confirm=true`. Keep your MCP client's per-tool approval enabled for them and use a dedicated
  token with the least role that works.
- A token with write rights is a bigger blast radius if leaked: prefer a separate token for write mode and
  keep the read-only token for everyday use.
- Keep TLS verification enabled; prefer `PMM_CA_BUNDLE` over `PMM_VERIFY=0`.
- Keep `~/.config/mcp/pmm.env` private (mode 600) and never commit it.
- Rotate the token if it may have been exposed.
