#!/bin/bash
# Auto-scaling management script for Yahoo Losers WebApp
# Supports Docker Compose and Kubernetes scaling

set -euo pipefail

# Configuration
APP_NAME="yahoo-losers-webapp"
MIN_REPLICAS=1
MAX_REPLICAS=10
SCALE_UP_THRESHOLD=70    # CPU percentage
SCALE_DOWN_THRESHOLD=30  # CPU percentage
CHECK_INTERVAL=30        # seconds

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

warn() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

# Check if Docker Compose is available
check_docker_compose() {
    if ! command -v docker-compose &> /dev/null; then
        error "Docker Compose is not installed"
        exit 1
    fi
}

# Get current number of replicas
get_current_replicas() {
    docker-compose ps -q app | wc -l
}

# Scale up the application
scale_up() {
    local current_replicas=$1
    local new_replicas=$((current_replicas + 1))
    
    if [ $new_replicas -le $MAX_REPLICAS ]; then
        log "Scaling UP: $current_replicas -> $new_replicas replicas"
        docker-compose up --scale app=$new_replicas -d
        
        # Update NGINX configuration to include new instances
        update_nginx_config $new_replicas
        docker-compose exec nginx nginx -s reload
        
        log "✅ Scaled up successfully to $new_replicas instances"
    else
        warn "Maximum replicas ($MAX_REPLICAS) reached. Cannot scale up further."
    fi
}

# Scale down the application  
scale_down() {
    local current_replicas=$1
    local new_replicas=$((current_replicas - 1))
    
    if [ $new_replicas -ge $MIN_REPLICAS ]; then
        log "Scaling DOWN: $current_replicas -> $new_replicas replicas"
        docker-compose up --scale app=$new_replicas -d
        
        # Update NGINX configuration
        update_nginx_config $new_replicas
        docker-compose exec nginx nginx -s reload
        
        log "✅ Scaled down successfully to $new_replicas instances"
    else
        warn "Minimum replicas ($MIN_REPLICAS) reached. Cannot scale down further."
    fi
}

# Update NGINX configuration for new number of replicas
update_nginx_config() {
    local replicas=$1
    local nginx_conf="nginx.scale.conf"
    
    # Generate upstream configuration
    cat > $nginx_conf << EOF
# Auto-generated NGINX configuration for $replicas replicas
worker_processes auto;
error_log /var/log/nginx/error.log warn;
pid /var/run/nginx.pid;

events {
    worker_connections 1024;
    use epoll;
    multi_accept on;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    
    # Upstream app servers
    upstream app_backend {
        least_conn;
EOF
    
    # Add server entries for each replica
    for i in $(seq 1 $replicas); do
        echo "        server ${APP_NAME}_app_${i}:8080 max_fails=3 fail_timeout=30s;" >> $nginx_conf
    done
    
    cat >> $nginx_conf << 'EOF'
        keepalive 32;
    }
    
    # Main server configuration (same as nginx.conf)
    server {
        listen 80;
        server_name localhost;
        
        location /health {
            proxy_pass http://app_backend;
            access_log off;
        }
        
        location / {
            proxy_pass http://app_backend;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        }
    }
}
EOF
    
    info "Updated NGINX configuration for $replicas replicas"
}

# Get average CPU usage across all app containers
get_cpu_usage() {
    local total_cpu=0
    local container_count=0
    
    for container in $(docker-compose ps -q app); do
        local cpu=$(docker stats --no-stream --format "{{.CPUPerc}}" $container | sed 's/%//')
        total_cpu=$(echo "$total_cpu + $cpu" | bc -l)
        container_count=$((container_count + 1))
    done
    
    if [ $container_count -gt 0 ]; then
        echo "scale=2; $total_cpu / $container_count" | bc -l
    else
        echo "0"
    fi
}

# Auto-scaling monitoring loop
auto_scale() {
    log "🚀 Starting auto-scaling monitor for $APP_NAME"
    log "📊 Thresholds: Scale UP > ${SCALE_UP_THRESHOLD}%, Scale DOWN < ${SCALE_DOWN_THRESHOLD}%"
    log "📈 Min replicas: $MIN_REPLICAS, Max replicas: $MAX_REPLICAS"
    log "⏱️  Check interval: ${CHECK_INTERVAL}s"
    
    while true; do
        local current_replicas=$(get_current_replicas)
        local cpu_usage=$(get_cpu_usage)
        local cpu_int=$(echo "$cpu_usage/1" | bc)
        
        info "Current: ${current_replicas} replicas, CPU: ${cpu_usage}%"
        
        if [ $cpu_int -gt $SCALE_UP_THRESHOLD ] && [ $current_replicas -lt $MAX_REPLICAS ]; then
            warn "High CPU usage detected (${cpu_usage}% > ${SCALE_UP_THRESHOLD}%)"
            scale_up $current_replicas
        elif [ $cpu_int -lt $SCALE_DOWN_THRESHOLD ] && [ $current_replicas -gt $MIN_REPLICAS ]; then
            info "Low CPU usage detected (${cpu_usage}% < ${SCALE_DOWN_THRESHOLD}%)"
            scale_down $current_replicas
        fi
        
        sleep $CHECK_INTERVAL
    done
}

# Manual scaling commands
case "${1:-auto}" in
    "up")
        current=$(get_current_replicas)
        scale_up $current
        ;;
    "down")  
        current=$(get_current_replicas)
        scale_down $current
        ;;
    "status")
        current=$(get_current_replicas)
        cpu=$(get_cpu_usage)
        log "📊 Current Status:"
        log "   Replicas: $current"
        log "   Average CPU: ${cpu}%"
        log "   Min/Max: $MIN_REPLICAS/$MAX_REPLICAS"
        ;;
    "auto")
        check_docker_compose
        auto_scale
        ;;
    *)
        echo "Usage: $0 {up|down|status|auto}"
        echo ""
        echo "Commands:"
        echo "  up     - Scale up by 1 replica"
        echo "  down   - Scale down by 1 replica" 
        echo "  status - Show current scaling status"
        echo "  auto   - Start auto-scaling monitor (default)"
        exit 1
        ;;
esac