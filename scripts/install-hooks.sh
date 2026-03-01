#!/usr/bin/env bash
# Version: v1.0
# Install git hooks for Antigravity workspace
# Ensures Code-Graph-RAG stays in sync with code changes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ANTIGRAVITY_DIR="${HOME}/antigravity"
CODE_GRAPH_RAG_DIR="${HOME}/code-graph-rag"
MEMGRAPH_PORT=7688

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }

# ─────────────────────────────────────────────────────────────────────────────
# Pre-commit hook for antigravity repo
# ─────────────────────────────────────────────────────────────────────────────

install_antigravity_hook() {
    local hook_path="$ANTIGRAVITY_DIR/.git/hooks/pre-commit"

    log_info "Installing pre-commit hook for antigravity..."

    cat > "$hook_path" << 'HOOK'
#!/usr/bin/env bash
# Antigravity pre-commit hook
# Updates Code-Graph-RAG index for changed Python files

set -e

MEMGRAPH_PORT=7688
CODE_GRAPH_RAG_DIR="${HOME}/code-graph-rag"

# Get list of staged Python files
STAGED_PY=$(git diff --cached --name-only --diff-filter=ACM | grep '\.py$' || true)

if [ -n "$STAGED_PY" ]; then
    echo "[pre-commit] Detected Python file changes, checking Code-Graph-RAG sync..."

    # Check if memgraph is running
    if ! nc -z localhost $MEMGRAPH_PORT 2>/dev/null; then
        echo "[pre-commit] WARNING: Memgraph not running on port $MEMGRAPH_PORT"
        echo "[pre-commit] Graph will not be updated. Run: start-services.sh"
        exit 0
    fi

    # Quick incremental update (non-blocking)
    # The realtime watcher should handle this, but this is a safety net
    echo "[pre-commit] Graph sync delegated to realtime watcher"
fi

exit 0
HOOK

    chmod +x "$hook_path"
    log_success "Installed: $hook_path"
}

# ─────────────────────────────────────────────────────────────────────────────
# Pre-commit hook for mcp-nexus-rag submodule
# ─────────────────────────────────────────────────────────────────────────────

install_nexus_hook() {
    local submodule_dir="$ANTIGRAVITY_DIR/projects/mcp-nexus-rag"
    local hook_path="$submodule_dir/.git/hooks/pre-commit"

    # Check if this is a submodule (has .git file, not directory)
    if [ -f "$submodule_dir/.git" ]; then
        # Get the actual git dir from the .git file
        local git_dir
        git_dir=$(cat "$submodule_dir/.git" | sed 's/gitdir: //')
        hook_path="$submodule_dir/$git_dir/hooks/pre-commit"
    fi

    log_info "Installing pre-commit hook for mcp-nexus-rag..."

    mkdir -p "$(dirname "$hook_path")"

    cat > "$hook_path" << 'HOOK'
#!/usr/bin/env bash
# MCP Nexus RAG pre-commit hook
# Runs basic validation before commit

set -e

echo "[pre-commit] Running pre-commit checks..."

# Check for debug prints
if git diff --cached --name-only | xargs grep -l 'print(' 2>/dev/null | head -1; then
    echo "[pre-commit] WARNING: Found print() statements in staged files"
fi

# Ensure no secrets in .env files are staged
if git diff --cached --name-only | grep -q '\.env$'; then
    echo "[pre-commit] ERROR: .env file staged for commit!"
    echo "[pre-commit] Remove with: git reset HEAD .env"
    exit 1
fi

echo "[pre-commit] Checks passed"
exit 0
HOOK

    chmod +x "$hook_path"
    log_success "Installed: $hook_path"
}

# ─────────────────────────────────────────────────────────────────────────────
# Post-commit hook to update parent submodule pointer
# ─────────────────────────────────────────────────────────────────────────────

install_post_commit_hook() {
    local submodule_dir="$ANTIGRAVITY_DIR/projects/mcp-nexus-rag"
    local hook_path

    if [ -f "$submodule_dir/.git" ]; then
        local git_dir
        git_dir=$(cat "$submodule_dir/.git" | sed 's/gitdir: //')
        hook_path="$submodule_dir/$git_dir/hooks/post-commit"
    else
        hook_path="$submodule_dir/.git/hooks/post-commit"
    fi

    log_info "Installing post-commit hook for mcp-nexus-rag..."

    cat > "$hook_path" << 'HOOK'
#!/usr/bin/env bash
# MCP Nexus RAG post-commit hook
# Reminds to update parent submodule pointer

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Committed to mcp-nexus-rag submodule                        ║"
echo "║                                                              ║"
echo "║  To update parent repo pointer:                              ║"
echo "║    cd ~/antigravity                                          ║"
echo "║    git add projects/mcp-nexus-rag                            ║"
echo "║    git commit -m 'chore(submodule): update mcp-nexus-rag'    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
HOOK

    chmod +x "$hook_path"
    log_success "Installed: $hook_path"
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

main() {
    echo ""
    echo "Installing git hooks for Antigravity workspace..."
    echo ""

    install_antigravity_hook
    install_nexus_hook
    install_post_commit_hook

    echo ""
    log_success "All hooks installed successfully"
    echo ""
}

main "$@"
