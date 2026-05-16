#!/bin/bash
set -e

echo ""
echo "============================================"
echo "  Memory MCP Server - One-Line Installer"
echo "============================================"
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Check for uv
if ! command -v uv &> /dev/null; then
    echo -e "${YELLOW}uv not found. Installing...${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    echo -e "${GREEN}uv installed.${NC}"
fi

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing dependencies..."
cd "$SCRIPT_DIR"
uv sync

echo ""
echo "Running auto-setup..."
uv run memory-mcp-setup

echo ""
echo -e "${GREEN}Installation complete!${NC}"
echo ""
echo "The memory daemon runs in the background (launchd) and serves both the"
echo "MCP endpoint and the management UI."
echo ""
echo "If setup printed an /etc/hosts command, run it once so the UI URL works:"
echo -e "  ${YELLOW}echo \"127.0.0.1 claude-memory-mcp\" | sudo tee -a /etc/hosts${NC}"
echo ""
echo "Then:"
echo "  - Restart Claude Code (it now connects to the HTTP daemon)"
echo "  - Open the UI at http://claude-memory-mcp:8765/"
echo ""
echo "Quick start inside Claude Code:"
echo "  memory_init_project('my-project', 'My Project')"
echo "  memory_session_start('my-project')"
echo ""
