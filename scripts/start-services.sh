#!/usr/bin/env bash
# Version: v2.0
# Antigravity AI Services Startup Script
# Brings up all MCP backend services: Nexus RAG + Code-Graph-RAG + MCP SSE

set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEXUS_RAG_DIR="$(dirname "$SCRIPT_DIR")"
CODE_GRAPH_RAG_DIR="${HOME}/code-graph-rag"
ANTIGRAVITY_DIR="${HOME}/antigravity"

# Service ports
NEO4J_PORT=7687
QDRANT_PORT=6333
OLLAMA_PORT=11434
REDIS_PORT=6379
MEMGRAPH_PORT=7688
SEARXNG_PORT="${SEARXNG_PORT:-8888}"
MCP_SSE_PORT="${MCP_SSE_PORT:-8765}"
HTTP_API_PORT="${HTTP_API_PORT:-8766}"
RERANKER_PORT="${RERANKER_PORT:-8767}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────────────────────

log_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

check_port() {
    local port=$1
    local service=$2
    if nc -z localhost "$port" 2>/dev/null; then
        log_success "$service is running on port $port"
        return 0
    else
        log_warn "$service is NOT running on port $port"
        return 1
    fi
}

wait_for_port() {
    local port=$1
    local service=$2
    local max_wait=${3:-60}
    local waited=0

    log_info "Waiting for $service on port $port..."
    while ! nc -z localhost "$port" 2>/dev/null; do
        sleep 2
        waited=$((waited + 2))
        if [ $waited -ge $max_wait ]; then
            log_error "$service failed to start within ${max_wait}s"
            return 1
        fi
    done
    log_success "$service is ready on port $port"
}

confirm_process_stable() {
    local pid=$1
    local stable_seconds=${2:-5}
    local waited=0
    while [ $waited -lt $stable_seconds ]; do
        if ! kill -0 "$pid" 2>/dev/null; then
            return 1
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 0
}

# ─────────────────────────────────────────────────────────────────────────────
# Status Check
# ─────────────────────────────────────────────────────────────────────────────

show_status() {
    echo ""
    echo "═══════════════════════════════════════════════════════════════════"
    echo "                  Antigravity AI Services Status"
    echo "═══════════════════════════════════════════════════════════════════"
    echo ""

    echo "MCP Nexus RAG Services:"
    check_port $NEO4J_PORT "  Neo4j (GraphRAG)" || true
    check_port $QDRANT_PORT "  Qdrant (VectorRAG)" || true
    check_port $OLLAMA_PORT "  Ollama (LLM)" || true
    check_port $REDIS_PORT "  Redis (Cache)" || true

    echo ""
    echo "Code-Graph-RAG Services:"
    check_port $MEMGRAPH_PORT "  Memgraph (Code Graph)" || true

    echo ""
    echo "Search Services:"
    check_port $SEARXNG_PORT "  SearXNG (Web Search)" || true

    echo ""
    echo "Reranker Service:"
    check_port $RERANKER_PORT "  Reranker Service (shared cross-encoder)" || true

    echo ""
    echo "MCP SSE Server:"
    check_port $MCP_SSE_PORT "  Nexus SSE (for Docker consumers)" || true
    check_port $HTTP_API_PORT "  Nexus HTTP API (for mission-control)" || true

    echo ""
    echo "Docker Containers:"
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "(turiya|memgraph|cgr)" || echo "  No matching containers found"
    echo ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Start Services
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_OLLAMA_MODELS=("nomic-embed-text" "qllama/bge-reranker-v2-m3" "qwen2.5:3b")

verify_ollama_models() {
    log_info "Verifying required Ollama models..."
    local missing=()
    local installed
    installed=$(curl -sf http://localhost:$OLLAMA_PORT/api/tags 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for m in data.get('models', []):
        print(m['name'].split(':')[0] if ':' not in m['name'] or m['name'].endswith(':latest') else m['name'])
except: pass
" 2>/dev/null || echo "")

    for model in "${REQUIRED_OLLAMA_MODELS[@]}"; do
        # Check exact match or prefix match (e.g., "nomic-embed-text" matches "nomic-embed-text:latest")
        if echo "$installed" | grep -qF "$model"; then
            log_success "  Model present: $model"
        else
            log_warn "  Model missing: $model"
            missing+=("$model")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        log_info "Pulling ${#missing[@]} missing model(s)..."
        for model in "${missing[@]}"; do
            log_info "  Pulling $model (this may take a while)..."
            ollama pull "$model" 2>&1 | tail -1
            log_success "  Pulled: $model"
        done
    fi
}

start_nexus_rag() {
    log_info "Starting MCP Nexus RAG services..."

    if [ ! -f "$NEXUS_RAG_DIR/docker-compose.yml" ]; then
        log_error "docker-compose.yml not found at $NEXUS_RAG_DIR"
        return 1
    fi

    cd "$NEXUS_RAG_DIR"

    # Detect which services are already running natively (not via Docker)
    local compose_services=""
    local native_services=""

    for svc_port in "redis:$REDIS_PORT:Redis" "ollama:$OLLAMA_PORT:Ollama"; do
        local svc="${svc_port%%:*}"
        local rest="${svc_port#*:}"
        local port="${rest%%:*}"
        local label="${rest#*:}"

        if nc -z localhost "$port" 2>/dev/null; then
            # Check if it's a Docker container or native process
            local container_name="turiya-${svc}"
            if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${container_name}$"; then
                compose_services="$compose_services $svc"
            else
                log_info "$label already running natively on port $port — skipping Docker"
                native_services="$native_services $svc"
            fi
        else
            compose_services="$compose_services $svc"
        fi
    done

    # Always start these via compose (no native alternative)
    compose_services="postgres neo4j qdrant $compose_services"

    # shellcheck disable=SC2086
    docker compose up -d $compose_services

    # Wait for services
    wait_for_port $NEO4J_PORT "Neo4j" 90
    wait_for_port $QDRANT_PORT "Qdrant" 30
    wait_for_port $OLLAMA_PORT "Ollama" 60
    wait_for_port $REDIS_PORT "Redis" 15

    # Verify required Ollama models are pulled
    verify_ollama_models

    log_success "MCP Nexus RAG services started"
}

start_searxng() {
    log_info "Starting SearXNG on port $SEARXNG_PORT..."

    if docker ps --format '{{.Names}}' | grep -q '^searxng$'; then
        log_info "SearXNG container already running"
        return 0
    fi

    if docker ps -a --format '{{.Names}}' | grep -q '^searxng$'; then
        log_info "Starting existing SearXNG container..."
        docker start searxng
    else
        log_info "Creating new SearXNG container..."
        docker run -d --name searxng \
            -p ${SEARXNG_PORT}:8080 \
            -e SEARXNG_SECRET="antigravity-searxng-$(openssl rand -hex 8)" \
            --restart unless-stopped \
            searxng/searxng:latest

        # Enable JSON output format
        sleep 3
        docker exec searxng sh -c "sed -i 's/^  formats:/  formats:\n    - json/' /etc/searxng/settings.yml" 2>/dev/null || true
        docker restart searxng
    fi

    wait_for_port $SEARXNG_PORT "SearXNG" 30
    log_success "SearXNG started on port $SEARXNG_PORT"
}

start_code_graph_rag() {
    log_info "Starting Code-Graph-RAG services..."

    # Check if memgraph-cgr container exists
    if docker ps -a --format '{{.Names}}' | grep -q '^memgraph-cgr$'; then
        # Container exists, start it
        if docker ps --format '{{.Names}}' | grep -q '^memgraph-cgr$'; then
            log_info "Memgraph container already running"
        else
            log_info "Starting existing Memgraph container..."
            docker start memgraph-cgr
        fi
    else
        # Create new container
        log_info "Creating new Memgraph container..."
        docker run -d --name memgraph-cgr \
            -p ${MEMGRAPH_PORT}:7687 \
            -p 7445:7444 \
            --restart unless-stopped \
            memgraph/memgraph-mage
    fi

    wait_for_port $MEMGRAPH_PORT "Memgraph" 30
    log_success "Code-Graph-RAG services started"
}

start_reranker_service() {
    log_info "Starting Reranker Service on port $RERANKER_PORT..."

    # Check if already running
    if nc -z localhost "$RERANKER_PORT" 2>/dev/null; then
        log_success "Reranker service already running on port $RERANKER_PORT"
        return 0
    fi

    local VENV="$NEXUS_RAG_DIR/.venv/bin/python"
    local LOG="/tmp/nexus-reranker-service.log"

    if [ ! -f "$VENV" ]; then
        log_error "venv not found at $VENV"
        return 1
    fi

    cd "$NEXUS_RAG_DIR"
    nohup "$VENV" -m uvicorn reranker_service:app --host 0.0.0.0 --port "$RERANKER_PORT" --loop asyncio > "$LOG" 2>&1 &

    local pid=$!
    log_info "Reranker Service PID: $pid (log: $LOG)"

    wait_for_port "$RERANKER_PORT" "Reranker Service" 30
}

start_mcp_sse() {
    log_info "Starting MCP Nexus RAG SSE server on port $MCP_SSE_PORT..."

    # Check if already running
    if nc -z localhost "$MCP_SSE_PORT" 2>/dev/null; then
        log_success "MCP SSE server already running on port $MCP_SSE_PORT"
        return 0
    fi

    local VENV="$NEXUS_RAG_DIR/.venv/bin/python"
    local LOG="/tmp/mcp-nexus-rag-sse.log"

    if [ ! -f "$VENV" ]; then
        log_error "venv not found at $VENV"
        return 1
    fi

    cd "$NEXUS_RAG_DIR"
    nohup "$VENV" -c "
from mcp.server.transport_security import TransportSecuritySettings
from server import mcp, validate_config, logger
for w in validate_config():
    logger.warning(f'[CONFIG] {w}')
mcp.settings.host = '0.0.0.0'
mcp.settings.port = $MCP_SSE_PORT
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp.run(transport='sse', mount_path='/')
" > "$LOG" 2>&1 &

    local pid=$!
    log_info "MCP SSE PID: $pid (log: $LOG)"

    wait_for_port "$MCP_SSE_PORT" "MCP SSE" 15
}

start_http_api() {
    log_info "Starting Nexus RAG HTTP API server on port $HTTP_API_PORT..."

    # Check if already running
    if nc -z localhost "$HTTP_API_PORT" 2>/dev/null; then
        log_success "HTTP API server already running on port $HTTP_API_PORT"
        return 0
    fi

    local VENV="$NEXUS_RAG_DIR/.venv/bin/python"
    local LOG="/tmp/nexus-rag-http-api.log"

    if [ ! -f "$VENV" ]; then
        log_error "venv not found at $VENV"
        return 1
    fi

    cd "$NEXUS_RAG_DIR"
    nohup "$VENV" -m uvicorn http_server:app --host 0.0.0.0 --port "$HTTP_API_PORT" --loop asyncio > "$LOG" 2>&1 &

    local pid=$!
    log_info "HTTP API PID: $pid (log: $LOG)"

    wait_for_port "$HTTP_API_PORT" "HTTP API" 15
}

# ─────────────────────────────────────────────────────────────────────────────
# Reindex Antigravity
# ─────────────────────────────────────────────────────────────────────────────

reindex_antigravity() {
    log_info "Re-indexing Antigravity codebase into Code-Graph-RAG..."

    if [ ! -d "$CODE_GRAPH_RAG_DIR" ]; then
        log_error "Code-Graph-RAG not found at $CODE_GRAPH_RAG_DIR"
        return 1
    fi

    cd "$CODE_GRAPH_RAG_DIR"
    MEMGRAPH_PORT=$MEMGRAPH_PORT uv run cgr start \
        --repo-path "$ANTIGRAVITY_DIR" \
        --update-graph --clean

    # Verify indexing
    local result
    result=$(MEMGRAPH_PORT=$MEMGRAPH_PORT uv run cgr export -o /tmp/graph_check.json 2>&1 | grep -o '[0-9]* nodes' | head -1)
    log_success "Indexing complete: $result indexed"
}

# ─────────────────────────────────────────────────────────────────────────────
# Stop Services
# ─────────────────────────────────────────────────────────────────────────────

start_watcher() {
    log_info "Starting Code-Graph-RAG realtime watcher..."

    if [ ! -d "$CODE_GRAPH_RAG_DIR" ]; then
        log_error "Code-Graph-RAG not found at $CODE_GRAPH_RAG_DIR"
        return 1
    fi

    # Kill any existing watcher
    pkill -f "realtime_updater.py" 2>/dev/null || true
    sleep 1

    cd "$CODE_GRAPH_RAG_DIR"
    # nohup & already detaches; setsid can fail if venv python symlinks to
    # a root-owned binary (e.g., uv-managed python under /root/).
    nohup .venv/bin/python realtime_updater.py "$ANTIGRAVITY_DIR" \
        --host localhost --port $MEMGRAPH_PORT \
        < /dev/null > /tmp/cgr-watcher.log 2>&1 &

    local watcher_pid=$!
    sleep 2

    if confirm_process_stable "$watcher_pid" 5; then
        log_success "Watcher started (PID: $watcher_pid). Log: /tmp/cgr-watcher.log"
    else
        log_error "Watcher failed to start. Check /tmp/cgr-watcher.log"
        return 1
    fi
}

start_rag_sync_watcher() {
    log_info "Starting Nexus RAG sync watcher..."

    # Kill any existing rag sync watcher
    pkill -f "nexus.watcher" 2>/dev/null || true
    sleep 1

    cd "$NEXUS_RAG_DIR"
    nohup .venv/bin/python -m nexus.watcher \
        --workspace "$ANTIGRAVITY_DIR" \
        < /dev/null > /tmp/rag-sync-watcher.log 2>&1 &

    local pid=$!
    sleep 2

    if confirm_process_stable "$pid" 5; then
        log_success "RAG sync watcher started (PID: $pid). Log: /tmp/rag-sync-watcher.log"
    else
        log_error "RAG sync watcher failed to start. Check /tmp/rag-sync-watcher.log"
        return 1
    fi
}

stop_services() {
    log_info "Stopping all Antigravity AI services..."

    # Stop Nexus RAG
    if [ -f "$NEXUS_RAG_DIR/docker-compose.yml" ]; then
        cd "$NEXUS_RAG_DIR"
        docker-compose down || true
    fi

    # Stop Memgraph
    docker stop memgraph-cgr 2>/dev/null || true

    # Stop SearXNG
    docker stop searxng 2>/dev/null || true

    # Stop RAG sync watcher
    pkill -f "nexus.watcher" 2>/dev/null || true

    # Stop MCP SSE server
    if ss -tlnp 2>/dev/null | grep -q ":${MCP_SSE_PORT} "; then
        local sse_pid
        sse_pid=$(ss -tlnp 2>/dev/null | grep ":${MCP_SSE_PORT} " | grep -oP 'pid=\K[0-9]+' | head -1)
        if [ -n "$sse_pid" ]; then
            kill "$sse_pid" 2>/dev/null || true
            log_info "Stopped MCP SSE server (PID: $sse_pid)"
        fi
    fi

    # Stop HTTP API server
    if ss -tlnp 2>/dev/null | grep -q ":${HTTP_API_PORT} "; then
        local api_pid
        api_pid=$(ss -tlnp 2>/dev/null | grep ":${HTTP_API_PORT} " | grep -oP 'pid=\K[0-9]+' | head -1)
        if [ -n "$api_pid" ]; then
            kill "$api_pid" 2>/dev/null || true
            log_info "Stopped HTTP API server (PID: $api_pid)"
        fi
    fi

    # Stop Reranker service
    if ss -tlnp 2>/dev/null | grep -q ":${RERANKER_PORT} "; then
        local reranker_pid
        reranker_pid=$(ss -tlnp 2>/dev/null | grep ":${RERANKER_PORT} " | grep -oP 'pid=\K[0-9]+' | head -1)
        if [ -n "$reranker_pid" ]; then
            kill "$reranker_pid" 2>/dev/null || true
            log_info "Stopped Reranker service (PID: $reranker_pid)"
        fi
    fi

    log_success "All services stopped"
}

# ─────────────────────────────────────────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────────────────────────────────────────

health_check() {
    local all_healthy=true

    echo ""
    log_info "Running health checks..."
    echo ""

    # Neo4j
    if curl -sf http://localhost:7474 > /dev/null 2>&1; then
        log_success "Neo4j: healthy"
    else
        log_error "Neo4j: unhealthy"
        all_healthy=false
    fi

    # Qdrant
    if curl -sf http://localhost:$QDRANT_PORT/collections > /dev/null 2>&1; then
        log_success "Qdrant: healthy"
    else
        log_error "Qdrant: unhealthy"
        all_healthy=false
    fi

    # Ollama
    if curl -sf http://localhost:$OLLAMA_PORT/api/tags > /dev/null 2>&1; then
        log_success "Ollama: healthy"
        # Check required models
        local model_count
        model_count=$(curl -sf http://localhost:$OLLAMA_PORT/api/tags 2>/dev/null | python3 -c "
import sys, json
try:
    models = [m['name'] for m in json.load(sys.stdin).get('models', [])]
    required = ['nomic-embed-text', 'qllama/bge-reranker-v2-m3', 'qwen2.5:3b']
    found = sum(1 for r in required if any(r in m for m in models))
    print(f'{found}/{len(required)}')
except: print('?/?')
" 2>/dev/null || echo "?/?")
        if [[ "$model_count" == "3/3" ]]; then
            log_success "  Ollama models: $model_count required models present"
        else
            log_warn "  Ollama models: $model_count required models present (run with --start to auto-pull)"
            all_healthy=false
        fi
    else
        log_error "Ollama: unhealthy"
        all_healthy=false
    fi

    # Redis
    if redis-cli -p $REDIS_PORT ping > /dev/null 2>&1; then
        log_success "Redis: healthy"
    else
        log_error "Redis: unhealthy"
        all_healthy=false
    fi

    # Memgraph (check via port)
    if nc -z localhost $MEMGRAPH_PORT 2>/dev/null; then
        log_success "Memgraph: healthy"
    else
        log_error "Memgraph: unhealthy"
        all_healthy=false
    fi

    # SearXNG
    if curl -sf "http://localhost:$SEARXNG_PORT/search?q=test&format=json" > /dev/null 2>&1; then
        log_success "SearXNG: healthy (port $SEARXNG_PORT)"
    else
        log_warn "SearXNG: not running (optional — web search via MCP)"
    fi

    # MCP SSE
    if curl -sf http://localhost:$MCP_SSE_PORT/sse > /dev/null 2>&1 || nc -z localhost $MCP_SSE_PORT 2>/dev/null; then
        log_success "MCP SSE: healthy (port $MCP_SSE_PORT)"
    else
        log_warn "MCP SSE: not running (optional — needed for Docker consumers)"
    fi

    # HTTP API (for mission-control)
    if curl -sf http://localhost:$HTTP_API_PORT/health > /dev/null 2>&1; then
        log_success "HTTP API: healthy (port $HTTP_API_PORT)"
    else
        log_warn "HTTP API: not running (optional — needed for mission-control Nexus query)"
    fi

    # Reranker Service
    if curl -sf http://localhost:$RERANKER_PORT/health > /dev/null 2>&1; then
        log_success "Reranker: healthy (port $RERANKER_PORT)"
    else
        log_warn "Reranker: not running (optional — shared cross-encoder for VRAM savings)"
    fi

    echo ""
    if $all_healthy; then
        log_success "All services are healthy"
        return 0
    else
        log_error "Some services are unhealthy"
        return 1
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

usage() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Antigravity AI Services Manager"
    echo ""
    echo "Options:"
    echo "  (no args)     Start all services"
    echo "  --status      Show service status"
    echo "  --health      Run health checks"
    echo "  --stop        Stop all services"
    echo "  --restart     Restart all services"
    echo "  --reindex     Re-index antigravity codebase"
    echo "  --watcher     Start/restart Code-Graph-RAG realtime watcher"
    echo "  --rag-sync    Start/restart Nexus RAG sync watcher (auto-ingests core docs)"
    echo "  --mcp-sse     Start MCP SSE server on port $MCP_SSE_PORT (for Docker consumers)"
    echo "  --http-api    Start HTTP API server on port $HTTP_API_PORT (for mission-control)"
    echo "  --reranker    Start shared reranker service on port $RERANKER_PORT"
    echo "  --help        Show this help"
    echo ""
    echo "Examples:"
    echo "  $0                    # Start all services"
    echo "  $0 --status           # Check what's running"
    echo "  $0 --reindex          # Re-index after code changes"
    echo ""
}

main() {
    case "${1:-}" in
        --status)
            show_status
            ;;
        --health)
            health_check
            ;;
        --stop)
            stop_services
            ;;
        --restart)
            stop_services
            sleep 3
            start_nexus_rag
            start_code_graph_rag
            start_searxng || log_warn "SearXNG failed to start (non-fatal)"
            start_reranker_service || log_warn "Reranker service failed to start (non-fatal)"
            start_mcp_sse || log_warn "MCP SSE server failed to start (non-fatal)"
            start_http_api || log_warn "HTTP API server failed to start (non-fatal)"
            show_status
            ;;
        --reindex)
            reindex_antigravity
            ;;
        --watcher)
            start_watcher
            ;;
        --rag-sync)
            start_rag_sync_watcher
            ;;
        --mcp-sse)
            start_mcp_sse
            ;;
        --http-api)
            start_http_api
            ;;
        --reranker)
            start_reranker_service
            ;;
        --help|-h)
            usage
            ;;
        "")
            echo ""
            echo "═══════════════════════════════════════════════════════════════════"
            echo "           Starting Antigravity AI Services"
            echo "═══════════════════════════════════════════════════════════════════"
            echo ""
            start_nexus_rag
            start_code_graph_rag
            start_searxng || log_warn "SearXNG failed to start (non-fatal — web search via MCP)"
            echo ""
            log_info "Starting file watchers..."
            start_watcher || log_warn "Code-Graph-RAG watcher failed to start (non-fatal)"
            start_rag_sync_watcher || log_warn "Nexus RAG sync watcher failed to start (non-fatal)"
            echo ""
            log_info "Starting reranker service..."
            start_reranker_service || log_warn "Reranker service failed to start (non-fatal — shared cross-encoder for VRAM savings)"
            echo ""
            log_info "Starting MCP SSE server..."
            start_mcp_sse || log_warn "MCP SSE server failed to start (non-fatal — needed for Docker consumers)"
            start_http_api || log_warn "HTTP API server failed to start (non-fatal — needed for mission-control)"
            echo ""
            show_status
            ;;
        *)
            log_error "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
}

main "$@"
