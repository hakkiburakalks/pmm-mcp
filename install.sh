#!/usr/bin/env bash
# PMM MCP installer: copies the code to ~/mcp/pmm, writes a launcher, registers it with Claude Code.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"

command -v uv >/dev/null || { echo "uv not found: install it from https://docs.astral.sh/uv/" >&2; exit 1; }

mkdir -p "$HOME/mcp/pmm" "$HOME/.config/mcp" "$HOME/.local/bin"
rm -rf "$HOME/mcp/pmm/pmm_mcp"
cp -r "$SRC/pmm_mcp" "$HOME/mcp/pmm/"
cp "$SRC/server.py" "$SRC/pyproject.toml" "$SRC/uv.lock" "$SRC/README.md" "$SRC/LICENSE" "$HOME/mcp/pmm/"

ENV="$HOME/.config/mcp/pmm.env"
if [ ! -f "$ENV" ]; then
  read -rp "PMM URL (e.g. https://pmm.example.com): " URL
  read -rsp "PMM token (Grafana service account, Viewer role): " TOKEN; echo
  [ -n "$URL" ] && [ -n "$TOKEN" ] || { echo "URL and token are required." >&2; exit 1; }
  # Create the file private from the start; %q quotes values safely for sourcing.
  ( umask 077
    {
      printf 'PMM_URL=%q\n' "$URL"
      printf 'PMM_TOKEN=%q\n' "$TOKEN"
      printf 'PMM_VERIFY=1\n'
    } > "$ENV" )
  chmod 600 "$ENV"
fi

cat > "$HOME/.local/bin/mcp-pmm" <<'L'
#!/usr/bin/env bash
set -euo pipefail
set -a; . "$HOME/.config/mcp/pmm.env"; set +a
exec uv run --quiet --directory "$HOME/mcp/pmm" server.py
L
chmod +x "$HOME/.local/bin/mcp-pmm"

(cd "$HOME/mcp/pmm" && uv sync --quiet)

if command -v claude >/dev/null; then
  claude mcp remove pmm -s user >/dev/null 2>&1 || true
  claude mcp add pmm -s user -- "$HOME/.local/bin/mcp-pmm"
  claude mcp get pmm
else
  echo "Claude Code not found. For Claude Desktop, add this to claude_desktop_config.json:"
  echo "  \"pmm\": {\"command\": \"$HOME/.local/bin/mcp-pmm\"}"
fi
