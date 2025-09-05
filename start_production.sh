#!/bin/bash
# Production startup script for Yahoo Losers WebApp
# Optimized Gunicorn configuration with monitoring

echo "🚀 Starting Yahoo Losers WebApp in Production Mode"
echo "📊 Configuration: 2 workers, 4 threads each"
echo "🔒 Rate Limiting: 30/min general, 10/min AI endpoints"
echo "💾 Memory Monitoring: Active via /metrics endpoint"
echo ""

# Set default port if not provided
export PORT=${PORT:-8080}

# Kill any existing instances
pkill -f "gunicorn.*app:app" 2>/dev/null || true
sleep 2

# Start Gunicorn with optimized configuration
echo "Starting Gunicorn on port $PORT..."
python3 -m gunicorn -c gunicorn.conf.py app:app

echo "✅ Production server started!"
echo "📊 Monitor performance at: http://localhost:$PORT/metrics"
echo "🔍 Dashboard available at: http://localhost:$PORT/"