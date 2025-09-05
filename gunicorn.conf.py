# Gunicorn configuration for production deployment
import multiprocessing
import os

# Server socket
bind = f"0.0.0.0:{os.environ.get('PORT', 8080)}"
backlog = 2048

# Worker processes  
workers = 2
worker_class = "gthread"
threads = 4
worker_connections = 1000
max_requests = 1000
max_requests_jitter = 50

# Timeout settings
timeout = 120
keepalive = 2

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" in %(M)sms'

# Process naming
proc_name = "yahoo_losers_webapp"

# Server mechanics
preload_app = True
daemon = False
pidfile = "/tmp/gunicorn.pid"
tmp_upload_dir = None

# SSL (if needed in production)
# keyfile = None
# certfile = None

# Performance tuning
worker_tmp_dir = "/dev/shm"  # Use in-memory filesystem for better performance

def when_ready(server):
    """Called just after the server is started."""
    server.log.info("Server is ready. Spawning workers")

def worker_int(worker):
    """Called just after worker has been signalled by the master."""
    worker.log.info("worker received INT or QUIT signal")

def pre_fork(server, worker):
    """Called just before a worker is forked."""
    server.log.info("Worker spawned (pid: %s)", worker.pid)

def post_fork(server, worker):
    """Called just after a worker has been forked."""
    server.log.info("Worker spawned (pid: %s)", worker.pid)