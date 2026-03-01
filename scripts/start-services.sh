#!/usr/bin/env bash
# Version: v1.0
# Antigravity AI Services Startup Script
# Brings up all MCP backend services: Nexus RAG + Code-Graph-RAG

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
MEMGRAPH_PORT=7688

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

    echo ""
    echo "Code-Graph-RAG Services:"
    check_port $MEMGRAPH_PORT "  Memgraph (Code Graph)" || true

    echo ""
    echo "Docker Containers:"
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" | grep -E "(turiya|memgraph|cgr)" || echo "  No matching containers found"
    echo ""
}

# ─────────────────────────────────────────────────────────────────────────────
# Start Services
# ─────────────────────────────────────────────────────────────────────────────

start_nexus_rag() {
    log_info "Starting MCP Nexus RAG services..."

    if [ ! -f "$NEXUS_RAG_DIR/docker-compose.yml" ]; then
        log_error "docker-compose.yml not found at $NEXUS_RAG_DIR"
        return 1
    fi

    cd "$NEXUS_RAG_DIR"
    docker-compose up -d

    # Wait for services
    wait_for_port $NEO4J_PORT "Neo4j" 90
    wait_for_port $QDRANT_PORT "Qdrant" 30
    wait_for_port $OLLAMA_PORT "Ollama" 60

    log_success "MCP Nexus RAG services started"
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

stop_services() {
    log_info "Stopping all Antigravity AI services..."

    # Stop Nexus RAG
    if [ -f "$NEXUS_RAG_DIR/docker-compose.yml" ]; then
        cd "$NEXUS_RAG_DIR"
        docker-compose down || true
    fi

    # Stop Memgraph
    docker stop memgraph-cgr 2>/dev/null || true

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
    else
        log_error "Ollama: unhealthy"
        all_healthy=false
    fi

    # Memgraph (check via port)
    if nc -z localhost $MEMGRAPH_PORT 2>/dev/null; then
        log_success "Memgraph: healthy"
    else
        log_error "Memgraph: unhealthy"
        all_healthy=false
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
            show_status
            ;;
        --reindex)
            reindex_antigravity
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
