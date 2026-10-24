#!/usr/bin/env bash
# Optional: token-saving tools for a developer's own Claude Code (not for ticket2pr's agent,
# which loads no plugins on purpose: docs/security.md). Idempotent: what is there is skipped.
#   make dev-tools      or      bash scripts/dev-tools.sh
# Installs: Claude Code plugins claude-mem, caveman, ponytail; rtk (+ its Claude Code hook);
# pxpipe; graphify (+ its skill). Every change is to your user config (~/.claude, ~/.local).
set -euo pipefail

RTK_INSTALL_REF="${RTK_INSTALL_REF:-v0.42.4}"  # pinned: the installer is run from the network
PXPIPE_VERSION="0.10.0"
PLUGINS=(  # marketplace repository  plugin@marketplace
    "thedotmack/claude-mem claude-mem@thedotmack"
    "JuliusBrussee/caveman caveman@caveman"
    "DietrichGebert/ponytail ponytail@ponytail"
)

say() { printf '\033[1m%s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

if ! have claude; then
    echo "Claude Code (claude) is not on PATH: install it first, then re-run." >&2
    exit 1
fi

enabled_plugins() {
    claude plugin list --json 2>/dev/null |
        python3 -c 'import json,sys; print("\n".join(p["id"] for p in json.load(sys.stdin) if p.get("enabled")))'
}

say "Claude Code plugins"
# The first explicit enable can disable plugins that were only enabled implicitly: restore them.
before=$(enabled_plugins)
for entry in "${PLUGINS[@]}"; do
    read -r repo plugin <<<"$entry"
    marketplace=${plugin#*@}
    if ! claude plugin marketplace list 2>/dev/null | grep -q "^  ❯ ${marketplace}$"; then
        claude plugin marketplace add "$repo" || echo "  $marketplace: could not add (already there?)"
    fi
    if claude plugin list --json | python3 -c "import json,sys; sys.exit(0 if any(p['id']=='$plugin' for p in json.load(sys.stdin)) else 1)"; then
        echo "  $plugin: already installed"
    else
        claude plugin install "$plugin"
    fi
    grep -qx "$plugin" <<<"$(enabled_plugins)" || claude plugin enable "$plugin"
done
after=$(enabled_plugins)
while read -r plugin; do
    [[ -n $plugin ]] && ! grep -qx "$plugin" <<<"$after" && claude plugin enable "$plugin"
done <<<"$before"

say "rtk"
if have rtk; then
    echo "  rtk: already installed ($(rtk --version))"
else
    curl -fsSL "https://raw.githubusercontent.com/rtk-ai/rtk/${RTK_INSTALL_REF}/install.sh" | sh
fi
if rtk init --show 2>&1 | grep -q '^\[ok\] Hook'; then  # it prints to stderr
    echo "  rtk hook: already installed"
else
    rtk init -g --auto-patch
fi

say "pxpipe"
if have pxpipe; then
    echo "  pxpipe: already installed ($(pxpipe --version))"
elif have npm; then
    npm install -g "pxpipe-proxy@${PXPIPE_VERSION}"
else
    echo "  pxpipe: skipped (npm not found; install Node.js, then re-run)"
fi

say "graphify"
if have graphify; then
    echo "  graphify: already installed ($(graphify --version))"
else
    uv tool install graphifyy  # the PyPI package is graphifyy; the command is graphify
fi
if [[ -f ~/.claude/skills/graphify/SKILL.md ]]; then
    echo "  graphify skill: already registered"
else
    graphify install --platform claude
fi

say "Done. Restart Claude Code to load new plugins and hooks."
