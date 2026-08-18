from flask import Flask, render_template_string, render_template, request, jsonify, g, make_response
from flask_compress import Compress
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import pandas as pd
import os
import json
import ssl
import logging
import pickle
import time
from collections import deque
from functools import wraps
import gc
import psutil
import threading
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta, date
import pytz
import redis
import structlog
from celery import Celery
import hashlib
from sophisticated_timeframe import SophisticatedTimeframePredictor
from provenance import Sourced, safe_ratio, UNAVAILABLE_DISPLAY
import market_data
import recommendation
import econ_calendar
import social
import timeframes
import tracking

app = Flask(__name__)

# Initialize sophisticated timeframe predictor
sophisticated_predictor = SophisticatedTimeframePredictor()

# Start background warming at import so it runs under gunicorn, which never
# executes the __main__ block below.
def _current_universe():
    """Symbols the warmer should keep fresh: today's losers."""
    losers, status = scrape_yahoo_losers()
    if not status.get("success"):
        return []
    return [s["Symbol"] for s in losers if s.get("Symbol") and s["Symbol"] != "ERROR"]


market_data.set_symbol_source(_current_universe)


@app.before_request
def _ensure_warmer_running():
    """Start the background warmer in this worker, once.

    It cannot be started at import: gunicorn runs with preload_app, so import
    happens in the master and threads are not inherited across fork. Starting
    from a request handler guarantees it runs in a process that serves traffic.
    """
    if not market_data._warmer_started:
        market_data.start_background_warmer()

# =============================================================================
# PRODUCTION OPTIMIZATIONS SETUP
# =============================================================================

# #2 HTTP Response Caching & Compression
Compress(app)
app.config['COMPRESS_MIMETYPES'] = ['text/html', 'text/css', 'text/xml', 
                                   'application/json', 'application/javascript']
app.config['COMPRESS_LEVEL'] = 6
app.config['COMPRESS_MIN_SIZE'] = 500

# #6 Security Headers & CORS
# No cookies or auth are used, so credentials must stay off. Pairing a wildcard
# origin with supports_credentials makes Flask-CORS reflect whatever Origin the
# caller sends, which defeats the point of an origin check entirely.
CORS(app, origins=os.environ.get('CORS_ORIGINS', '*').split(','), supports_credentials=False)
app.config['CORS_HEADERS'] = 'Content-Type'

# #5 Structured Logging & APM
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

# Get structured logger
logger = structlog.get_logger(__name__)

# #5 Enhanced APM - Request Logging Middleware
@app.before_request
def log_request_info():
    """Log request start with structured data"""
    g.start_time = time.time()
    logger.info("Request started", 
                method=request.method,
                path=request.path,
                remote_addr=request.remote_addr,
                user_agent=request.user_agent.string if request.user_agent else None)

@app.after_request
def log_request_end(response):
    """Log request completion with structured data"""
    try:
        duration = time.time() - g.start_time if hasattr(g, 'start_time') else 0
        logger.info("Request completed",
                    method=request.method,
                    path=request.path,
                    status_code=response.status_code,
                    duration_ms=round(duration * 1000, 2),
                    content_length=response.content_length)
    except Exception:
        pass  # Don't break response if logging fails
    return response

# #6 Additional Security Headers (beyond NGINX)
@app.after_request  
def add_security_headers(response):
    """Add comprehensive security headers"""
    # Content Security Policy
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
        "font-src 'self' https://cdnjs.cloudflare.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https:; "
        "frame-src https://www.tradingview.com; "
        "frame-ancestors 'none';"
    )
    # Strict Transport Security (if HTTPS)
    if request.is_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    
    # Additional security headers
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'  
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    
    # ULTRA-AGGRESSIVE CACHE-BUSTING V3.0 - FORCE BROWSER TO NEVER CACHE HTML PAGES
    if request.endpoint == 'index':  # Main HTML page
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        response.headers['Last-Modified'] = datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT')
        response.headers['Vary'] = '*'
        # Add random header to ensure complete cache invalidation
        response.headers['X-Cache-Bust-V3'] = f'{int(time.time())}_{hash(time.time())}'
    
    return response

# #1 Redis Caching Layer
REDIS_URL = os.environ.get('REDIS_URL') or 'redis://localhost:6379/0'
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
    # Test connection
    redis_client.ping()
    logger.info("Redis connection established", redis_url=REDIS_URL)
    USE_REDIS = True
except (redis.RedisError, ConnectionError, ValueError) as e:
    # ValueError covers a malformed REDIS_URL. Previously an empty or invalid
    # value raised at import and took the whole app down, when the correct
    # behaviour is the same graceful degradation as an unreachable server.
    logger.warning("Redis unavailable, falling back to file cache", error=str(e))
    redis_client = None
    USE_REDIS = False

# #7 Background Task Queue (Celery)
def make_celery(app):
    celery = Celery(
        app.import_name,
        backend=REDIS_URL if USE_REDIS else 'rpc://',
        broker=REDIS_URL if USE_REDIS else 'redis://localhost:6379/0'
    )
    celery.conf.update(app.config)
    return celery

celery_app = make_celery(app)

# #7 Background Tasks for AI Processing
@celery_app.task(bind=True, name='predict_recovery_task')
def predict_recovery_task(self, symbol):
    """Background task for AI recovery prediction"""
    try:
        logger.info("Starting background recovery prediction", symbol=symbol, task_id=self.request.id)
        # This will be implemented when we convert the AI functions
        result = {"status": "pending", "symbol": symbol, "task_id": self.request.id}
        logger.info("Recovery prediction task queued", symbol=symbol, task_id=self.request.id)
        return result
    except Exception as e:
        logger.error("Recovery prediction task failed", symbol=symbol, error=str(e))
        raise

@celery_app.task(bind=True, name='analyze_sentiment_task')
def analyze_sentiment_task(self, symbol):
    """Background task for sentiment analysis"""
    try:
        logger.info("Starting background sentiment analysis", symbol=symbol, task_id=self.request.id)
        result = {"status": "pending", "symbol": symbol, "task_id": self.request.id}
        logger.info("Sentiment analysis task queued", symbol=symbol, task_id=self.request.id)
        return result
    except Exception as e:
        logger.error("Sentiment analysis task failed", symbol=symbol, error=str(e))
        raise

@celery_app.task(bind=True, name='bulk_analysis_task')
def bulk_analysis_task(self, symbols):
    """Background task for bulk stock analysis"""
    try:
        logger.info("Starting bulk analysis", symbol_count=len(symbols), task_id=self.request.id)
        result = {"status": "pending", "symbols": symbols, "task_id": self.request.id}
        logger.info("Bulk analysis task queued", symbol_count=len(symbols), task_id=self.request.id)
        return result
    except Exception as e:
        logger.error("Bulk analysis task failed", error=str(e))
        raise

# Request tracking for rate limiting
request_counts = {}
request_lock = threading.Lock()

# SSL configuration - Use default secure context
# Removed ssl._create_unverified_context for security
# Individual requests can handle SSL issues case-by-case if needed

# Cache configuration
CACHE_FILE = '/tmp/yahoo_finance_cache.pkl'  # Use /tmp for Render compatibility
CACHE_DURATION_HOURS = 24  # ceiling; page_cache_hours() is what callers use


def page_cache_hours():
    """How long a rendered loser list stays servable.

    A flat 24 hours meant a live session could be served yesterday's losers.
    The list genuinely churns while the market is open and cannot change at all
    once it closes, so the lifetime follows the session rather than the clock.
    """
    if market_data.market_is_open():
        return float(os.environ.get('CACHE_MINUTES_MARKET_OPEN', 10)) / 60
    return float(os.environ.get('CACHE_HOURS_MARKET_CLOSED', 12))

# Rate limiting configuration
MAX_REQUESTS_PER_MINUTE = 30
MAX_AI_REQUESTS_PER_MINUTE = 10

# Minimum upside required for a stock to be listed as high potential.
# Overridable so it can be recalibrated once real analyst targets are wired in.
MIN_UPSIDE_PERCENT = float(os.environ.get('MIN_UPSIDE_PERCENT', 65))

# Minimum rebound score for a stock to appear in the recommendations panel.
# 70 is the "Strong rebound setup" boundary in the scoring model. Set at the
# Constructive boundary (58) this surfaced 17 of 25 losers, which is not a
# shortlist -- beaten-down names routinely show large consensus upside.
MIN_REBOUND_SCORE = float(os.environ.get('MIN_REBOUND_SCORE', 70))

# Concurrency for the per-symbol quote fetch.
QUOTE_WORKERS = int(os.environ.get('QUOTE_WORKERS', 8))

# Forward window for scheduled macro events. The old code filtered on 30 days
# while the UI text claimed 14; one number now drives both.
ECON_HORIZON_DAYS = int(os.environ.get('ECON_HORIZON_DAYS', 30))

def rate_limit(max_per_minute=MAX_REQUESTS_PER_MINUTE):
    """Rate limiting decorator"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            client_ip = request.remote_addr
            current_time = time.time()
            
            with request_lock:
                # Clean old entries
                cutoff_time = current_time - 60  # 1 minute ago
                request_counts[client_ip] = [req_time for req_time in request_counts.get(client_ip, []) if req_time > cutoff_time]
                
                # Check rate limit
                if len(request_counts.get(client_ip, [])) >= max_per_minute:
                    return jsonify({"error": "Rate limit exceeded. Please try again later."}), 429
                
                # Add current request
                request_counts.setdefault(client_ip, []).append(current_time)
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def get_memory_usage():
    """Get current memory usage"""
    try:
        process = psutil.Process()
        memory_info = process.memory_info()
        return {
            'rss': memory_info.rss / 1024 / 1024,  # MB
            'vms': memory_info.vms / 1024 / 1024,  # MB
            'percent': process.memory_percent()
        }
    except (psutil.Error, OSError) as e:
        # Monitoring only. Zeroes here are a reading of the process, not of
        # market data, so they cannot be mistaken for a financial figure.
        logger.debug(f"Memory probe unavailable: {type(e).__name__}")
        return {'rss': 0, 'vms': 0, 'percent': 0}

def cleanup_resources():
    """Clean up resources to prevent memory leaks"""
    gc.collect()
    
def log_performance(func_name, start_time, memory_before):
    """Log performance metrics"""
    end_time = time.time()
    memory_after = get_memory_usage()
    
    logger.info(f"{func_name} - Duration: {(end_time - start_time):.2f}s, "
                f"Memory: {memory_before['rss']:.1f}MB -> {memory_after['rss']:.1f}MB "
                f"({memory_after['rss'] - memory_before['rss']:+.1f}MB)")
    return memory_after

def save_cache(data):
    """Save analysis results to cache with timestamp (Redis + file fallback)"""
    try:
        cache_data = {
            'timestamp': datetime.now(),
            'data': data
        }
        
        # Try Redis first
        try:
            redis_data = {
                'timestamp': cache_data['timestamp'].isoformat(),
                'data': data
            }
            redis_client.setex('yahoo_losers_cache', int(page_cache_hours() * 3600), json.dumps(redis_data, default=str))
            logger.info(f"Cache saved to Redis successfully at {cache_data['timestamp']}")
        except Exception as redis_error:
            logger.warning(f"Redis cache save failed: {redis_error}, falling back to file")
            # Fallback to file cache
            with open(CACHE_FILE, 'wb') as f:
                pickle.dump(cache_data, f)
            logger.info(f"Cache saved to file successfully at {cache_data['timestamp']}")
            
    except Exception as e:
        logger.error(f"Failed to save cache: {str(e)}")

def load_cache():
    """Load cached results if within 24 hours (Redis + file fallback)"""
    try:
        # Try Redis first
        try:
            redis_data = redis_client.get('yahoo_losers_cache')
            if redis_data:
                cache_data = json.loads(redis_data)
                cache_time = datetime.fromisoformat(cache_data['timestamp'])
                current_time = datetime.now()
                time_diff = current_time - cache_time
                
                cache_data_formatted = {
                    'timestamp': cache_time,
                    'data': cache_data['data']
                }
                logger.info(f"Valid cache found from Redis from {cache_time} ({time_diff.total_seconds()/3600:.1f} hours ago)")
                return cache_data_formatted
        except Exception as redis_error:
            logger.warning(f"Redis cache load failed: {redis_error}, trying file fallback")
        
        # Fallback to file cache
        if not os.path.exists(CACHE_FILE):
            logger.info("No cache file found")
            return None
        
        with open(CACHE_FILE, 'rb') as f:
            cache_data = pickle.load(f)
        
        # Check if cache is still valid (within 24 hours)
        cache_time = cache_data['timestamp']
        current_time = datetime.now()
        time_diff = current_time - cache_time
        
        if time_diff.total_seconds() / 3600 < page_cache_hours():
            logger.info(f"Valid cache found from file from {cache_time} ({time_diff.total_seconds()/3600:.1f} hours ago)")
            return cache_data
        else:
            logger.info(f"Cache expired ({time_diff.total_seconds()/3600:.1f} hours old), will refresh")
            return None
            
    except Exception as e:
        logger.error(f"Failed to load cache: {str(e)}")
        return None

# #2 HTTP Response Caching & ETag helpers
def generate_etag(data):
    """Generate ETag for HTTP caching based on data content"""
    if isinstance(data, dict) or isinstance(data, list):
        content = json.dumps(data, sort_keys=True, default=str)
    else:
        content = str(data)
    return hashlib.md5(content.encode()).hexdigest()

def add_cache_headers(response, max_age=60):
    """Add cache control headers to response"""
    response.headers['Cache-Control'] = f'public, max-age={max_age}'
    response.headers['Vary'] = 'Accept-Encoding'
    return response

def get_cache_status():
    """Get cache status for display in UI"""
    try:
        if not os.path.exists(CACHE_FILE):
            return {"exists": False, "message": "No cache available"}
        
        with open(CACHE_FILE, 'rb') as f:
            cache_data = pickle.load(f)
        
        cache_time = cache_data['timestamp']
        current_time = datetime.now()
        time_diff = current_time - cache_time
        hours_old = time_diff.total_seconds() / 3600
        
        if hours_old < page_cache_hours():
            return {
                "exists": True,
                "valid": True,
                "hours_old": round(hours_old, 1),
                "cache_time": cache_time,
                "message": f"Using cached data from {hours_old:.1f} hours ago"
            }
        else:
            return {
                "exists": True,
                "valid": False,
                "hours_old": round(hours_old, 1),
                "cache_time": cache_time,
                "message": f"Cache expired ({hours_old:.1f} hours old)"
            }
    except Exception as e:
        return {"exists": False, "message": f"Cache error: {str(e)}"}

def scrape_yahoo_losers():
    """Step 1: Get DAILY LOSERS from Yahoo Finance screener API - same as original website"""
    start_time = time.time()
    memory_before = get_memory_usage()
    
    status = {"success": False, "data_source": "unknown", "message": ""}
    try:
        # Use the ACTUAL Yahoo Finance day losers screener API
        losers_url = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=true&lang=en-US&region=US&scrIds=day_losers"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        session = requests.Session()
        response = session.get(losers_url, headers=headers, timeout=15)
        response.raise_for_status()
        
        data = response.json()
        
        if 'finance' in data and 'result' in data['finance'] and data['finance']['result']:
            result = data['finance']['result'][0]
            quotes = result.get('quotes', [])
            
            stocks_data = []
            
            for quote in quotes:
                try:
                    symbol = quote.get('symbol', 'N/A')
                    long_name = quote.get('longName', quote.get('shortName', symbol))
                    current_price = quote.get('regularMarketPrice', {}).get('raw', 0)
                    change = quote.get('regularMarketChange', {}).get('raw', 0) 
                    percent_change = quote.get('regularMarketChangePercent', {}).get('raw', 0)
                    market_cap = quote.get('marketCap', {}).get('raw', 0)
                    
                    stocks_data.append({
                        'Symbol': symbol,
                        'Name': long_name,
                        'Price': f"${current_price:.2f}" if current_price else 'N/A',
                        'Change': f"${change:.2f}" if change else 'N/A',
                        'Percent Change': f"{percent_change:.2f}%" if percent_change else 'N/A',
                        'Market Cap': format_market_cap(market_cap)
                    })
                    
                except Exception as e:
                    logger.warning(f"Error processing quote: {str(e)}")
                    continue
            
            if stocks_data:
                status["data_source"] = "live"
                status["message"] = f"Successfully scraped {len(stocks_data)} DAILY LOSERS from Yahoo Finance screener"
                status["success"] = True
                logger.info(status["message"])
                log_performance("scrape_yahoo_losers", start_time, memory_before)
                cleanup_resources()
                return stocks_data, status
            else:
                raise Exception("No quotes found in screener response")
        else:
            raise Exception("Invalid screener API response")
            
    except Exception as e:
        logger.error(f"Error getting Yahoo Finance daily losers: {str(e)}")
        status["data_source"] = "error" 
        status["message"] = f"Daily losers API failed: {str(e)}"
        
        # Fallback 
        return [
            {'Symbol': 'ERROR', 'Name': 'Could not fetch daily losers', 'Price': '$0.00', 'Change': '$0.00', 'Percent Change': '0.00%', 'Market Cap': 'N/A'}
        ], status

def format_market_cap(market_cap):
    """Format market cap in human readable format"""
    if not market_cap:
        return 'N/A'
    
    if market_cap >= 1e12:
        return f"{market_cap/1e12:.2f}T"
    elif market_cap >= 1e9:
        return f"{market_cap/1e9:.2f}B"
    elif market_cap >= 1e6:
        return f"{market_cap/1e6:.2f}M"
    else:
        return f"{market_cap:,.0f}"

def get_stock_details(symbols):
    """Step 2: Get additional real stock details using Yahoo Finance API.

    The per-symbol loop below stays sequential and readable, but every provider
    call it makes is pre-warmed concurrently first. Warming turns ~4 sequential
    round trips per symbol into a parallel batch, which is the difference
    between a page render taking ~45s and a few seconds on a cold cache.
    """
    stock_details = []

    # Queue for the background warmer and move on. Fetching here put provider
    # calls in the request path, so Render's periodic HEAD / health check was
    # triggering a full refresh each time.
    try:
        market_data.request_warm(symbols)
    except Exception as e:
        logger.warning(f"Queueing warm failed: {type(e).__name__}: {e}")

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    def fetch_one(symbol):
        # A session per worker: requests.Session is not documented as
        # thread-safe, and sharing one across the pool risks connection reuse
        # races for no real benefit at this batch size.
        session = requests.Session()
        try:
            # Get detailed quote data
            quote_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            quote_response = session.get(quote_url, headers=headers, timeout=10)
            quote_response.raise_for_status()
            quote_data = quote_response.json()
            
            if 'chart' in quote_data and quote_data['chart']['result']:
                result = quote_data['chart']['result'][0]
                meta = result['meta']
                
                current_price = meta.get('regularMarketPrice', 0)
                prev_close = meta.get('previousClose', 0)
                volume = meta.get('regularMarketVolume', 0)
                
                # Analyst consensus target, via yfinance because the raw
                # quoteSummary endpoint now returns 401 to unauthenticated callers.
                # A consensus drawn from fewer than three estimates is rejected
                # upstream in market_data rather than presented as a consensus.
                targets = market_data.analyst_target(symbol, allow_fetch=False)
                target = targets['mean']
                analysts = targets['analysts']

                return {
                    'Symbol': symbol,
                    'Current Price': f"${current_price:.2f}" if current_price else UNAVAILABLE_DISPLAY,
                    'Previous Close': f"${prev_close:.2f}" if prev_close else UNAVAILABLE_DISPLAY,
                    'Volume': format_volume(volume),
                    'Price Target': target.format('.2f', prefix='$'),
                    'Target Low': targets['low'].format('.2f', prefix='$'),
                    'Target High': targets['high'].format('.2f', prefix='$'),
                    'Analyst Count': analysts.value if analysts.ok else None,
                    'Price Target Source': target.source if target.ok else f'unavailable: {target.reason}'
                }

            raise ValueError('chart response contained no result')

        except Exception as e:
            logger.warning(f"Failed to get details for {symbol}: {str(e)}")
            # Nothing was retrieved for this symbol. Every field stays empty
            # rather than being filled with a placeholder that reads as data.
            return {
                'Symbol': symbol,
                'Current Price': UNAVAILABLE_DISPLAY,
                'Previous Close': UNAVAILABLE_DISPLAY,
                'Volume': UNAVAILABLE_DISPLAY,
                'Price Target': UNAVAILABLE_DISPLAY,
                'Target Low': UNAVAILABLE_DISPLAY,
                'Target High': UNAVAILABLE_DISPLAY,
                'Analyst Count': None,
                'Price Target Source': f'unavailable: quote fetch failed ({type(e).__name__})'
            }
        finally:
            session.close()

    # Quotes are independent per symbol and purely network-bound, so they run
    # concurrently. Order is preserved to keep the rendered table stable.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=QUOTE_WORKERS) as pool:
        stock_details = list(pool.map(fetch_one, symbols))

    logger.info(f"Retrieved details for {len(stock_details)} stocks from Yahoo Finance API")
    return stock_details

def format_volume(volume):
    """Format volume in human readable format"""
    if not volume:
        return 'N/A'
    
    if volume >= 1e9:
        return f"{volume/1e9:.1f}B"
    elif volume >= 1e6:
        return f"{volume/1e6:.1f}M"
    elif volume >= 1e3:
        return f"{volume/1e3:.1f}K"
    else:
        return f"{volume:,.0f}"

def parse_money(raw):
    """Parse a display string like '$12.34' into a float.

    Returns None for anything that is not a number, including the em dash used
    for unavailable values. Callers must treat None as 'no data' and must not
    substitute a default.
    """
    if raw is None:
        return None
    text = str(raw).replace('$', '').replace(',', '').strip()
    try:
        return float(text)
    except ValueError:
        return None


def calculate_all_investment_analysis(losers_data, details_data):
    """Calculate investment analysis for ALL stocks (no filtering)"""
    all_analysis = []
    
    # Create lookup dictionary for details
    details_dict = {item['Symbol']: item for item in details_data}
    
    for stock in losers_data:
        symbol = stock['Symbol']
        if symbol in details_dict:
            details = details_dict[symbol]
            
            try:
                current_price = parse_money(details['Current Price'])
                target_price = parse_money(details['Price Target'])

                # Upside is only meaningful when both sides are real. Without a
                # genuine analyst target this stays empty instead of resolving to
                # the flat 15% that the old fabricated fallback produced.
                potential_return = None
                if current_price and target_price:
                    potential_return = ((target_price - current_price) / current_price) * 100

                all_analysis.append({
                    'Symbol': symbol,
                    'Name': stock['Name'],
                    'Current Price': current_price if current_price else UNAVAILABLE_DISPLAY,
                    'Target Price': target_price if target_price else UNAVAILABLE_DISPLAY,
                    'Potential Return %': round(potential_return, 2) if potential_return is not None else UNAVAILABLE_DISPLAY,
                    'Analyst Count': details.get('Analyst Count'),
                    'Target Low': details.get('Target Low', UNAVAILABLE_DISPLAY),
                    'Target High': details.get('Target High', UNAVAILABLE_DISPLAY),
                    'Target Source': details.get('Price Target Source', 'unknown'),
                    'Volume': details['Volume'],
                    'Change Today': stock['Change'],
                    'Percent Change Today': stock['Percent Change'],
                    'Market Cap': stock.get('Market Cap', 'N/A')
                })
                
            except (ValueError, TypeError) as e:
                logger.error(f"Error calculating potential for {symbol}: {str(e)}")
                # Still list the stock, but with the price fields left empty.
                all_analysis.append({
                    'Symbol': symbol,
                    'Name': stock['Name'],
                    'Current Price': UNAVAILABLE_DISPLAY,
                    'Target Price': UNAVAILABLE_DISPLAY,
                    'Potential Return %': UNAVAILABLE_DISPLAY,
                    'Target Source': f'unavailable: {type(e).__name__}',
                    'Volume': details.get('Volume', UNAVAILABLE_DISPLAY),
                    'Change Today': stock['Change'],
                    'Percent Change Today': stock['Percent Change'],
                    'Market Cap': stock.get('Market Cap', 'N/A')
                })
                continue
    
    return all_analysis

def calculate_investment_potential(all_analysis):
    """Step 3: Filter high-potential investments from all analysis.

    The threshold is configurable because the previous hard-coded 65 was tuned
    against fabricated targets that made every stock read as exactly 15% upside,
    so it was never calibrated against real analyst consensus.
    """
    min_upside = MIN_UPSIDE_PERCENT

    high_potential = []
    for analysis in all_analysis:
        upside = analysis['Potential Return %']
        if isinstance(upside, (int, float)) and upside > min_upside:
            high_potential.append(analysis)

    return high_potential

def format_results_as_html(losers_data, details_data, all_analysis, recommendations, status):
    """Format all results as HTML"""
    
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta name="theme-color" content="#0d1117">
        <link rel="manifest" href="/static/manifest.json">
        <link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
        <link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png">
        <title>Yahoo Finance Daily Losers Analysis</title>
        <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
        <meta http-equiv="Pragma" content="no-cache">
        <meta http-equiv="Expires" content="0">
        <style>
            /* DARK MODE DEFAULT - Professional Theme */
            :root {
                --bg-primary: #0d1117;
                --bg-secondary: #161b22;
                --bg-tertiary: #21262d;
                --text-primary: #f0f6fc;
                --text-secondary: #8b949e;
                --border-color: #30363d;
                --header-bg: #21262d;
                --positive-color: #3fb950;
                --negative-color: #f85149;
                --highlight-bg: #1f2328;
                --summary-bg: #0d419d;
                --shadow: rgba(0,0,0,0.4);
                --accent-blue: #1f6feb;
                --accent-purple: #8b5cf6;
                --modal-bg: rgba(22,27,34,0.95);
            }
            
            /* Light Mode Override */
            [data-theme="light"] {
                --bg-primary: #ffffff;
                --bg-secondary: #f6f8fa;
                --bg-tertiary: #ffffff;
                --text-primary: #24292f;
                --text-secondary: #656d76;
                --border-color: #d0d7de;
                --header-bg: #f6f8fa;
                --positive-color: #1a7f37;
                --negative-color: #cf222e;
                --highlight-bg: #fff8c5;
                --summary-bg: #dbeafe;
                --shadow: rgba(0,0,0,0.08);
                --accent-blue: #0969da;
                --accent-purple: #8250df;
                --modal-bg: rgba(255,255,255,0.95);
            }
            
            * { 
                transition: all 0.2s ease; 
                box-sizing: border-box;
            }
            
            body { 
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; 
                margin: 0; 
                padding: 16px;
                background: linear-gradient(135deg, var(--bg-primary) 0%, var(--bg-tertiary) 100%);
                color: var(--text-primary);
                line-height: 1.5;
                min-height: 100vh;
                font-size: 15px;
            }
            
            .container { 
                max-width: 1600px; 
                margin: 0 auto; 
                padding: 0 24px;
            }
            
            /* Improved Typography */
            h1, h2, h3, h4, h5, h6 { 
                margin: 0 0 16px 0; 
                font-weight: 600;
                letter-spacing: -0.01em;
            }
            
            h1 { font-size: 28px; line-height: 1.2; }
            h2 { font-size: 22px; line-height: 1.3; }
            h3 { font-size: 18px; line-height: 1.4; }
            h4 { font-size: 16px; line-height: 1.4; }
            
            /* Status Badges */
            .status-badge {
                background: var(--bg-tertiary);
                border: 1px solid var(--border-color);
                border-radius: 6px;
                padding: 4px 8px;
                font-size: 12px;
                font-weight: 500;
                white-space: nowrap;
            }
            
            /* Improved Section Styling */
            .section {
                background: var(--bg-secondary);
                border: 1px solid var(--border-color);
                border-radius: 8px;
                padding: 20px;
                margin-bottom: 20px;
            }
            
            /* Widescreen Grid Layout */
            .grid-layout {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 24px;
                margin: 20px 0;
            }
            
            .grid-full {
                grid-column: 1 / -1;
            }
            
            @media (max-width: 1200px) {
                .grid-layout {
                    grid-template-columns: 1fr;
                    gap: 16px;
                }
            }
            
            h1 { 
                color: var(--text-primary); 
                text-align: center; 
                font-size: 2.5rem;
                font-weight: 700;
                margin: 24px 0;
                background: linear-gradient(135deg, var(--accent-blue), var(--accent-purple));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                background-clip: text;
            }
            
            h2 { 
                color: var(--text-primary); 
                text-align: center; 
                font-size: 1.8rem;
                font-weight: 600;
                margin: 24px 0 16px 0;
                padding: 12px 0;
                border-bottom: 2px solid var(--border-color);
            }
            
            h3 { 
                color: var(--text-primary); 
                font-size: 1.3rem;
                font-weight: 600;
                margin: 20px 0 12px 0;
            }
            
            .section { 
                background: var(--bg-secondary); 
                margin: 16px 0; 
                padding: 24px; 
                border-radius: 12px; 
                box-shadow: 0 4px 12px var(--shadow);
                border: 1px solid var(--border-color);
                backdrop-filter: blur(10px);
            }
            
            /* Widescreen Optimized Tables */
            table { 
                width: 100%; 
                border-collapse: collapse; 
                margin: 12px 0;
                border-radius: 8px;
                overflow: hidden;
                box-shadow: 0 2px 8px var(--shadow);
            }
            
            th, td { 
                padding: 12px 16px; 
                text-align: left; 
                border-bottom: 1px solid var(--border-color); 
                color: var(--text-primary);
                font-size: 0.9rem;
            }
            
            th { 
                background: linear-gradient(135deg, var(--header-bg), var(--bg-tertiary)); 
                font-weight: 600;
                font-size: 0.85rem;
                text-transform: uppercase;
                letter-spacing: 0.5px;
                color: var(--text-secondary);
            }
            
            tr:hover {
                background-color: var(--highlight-bg);
                transform: translateY(-1px);
            }
            
            .positive { color: var(--positive-color); font-weight: 600; }
            .negative { color: var(--negative-color); font-weight: 600; }
            .highlight { background-color: var(--highlight-bg); }
            
            .theme-toggle { 
                position: fixed; 
                top: 24px; 
                right: 24px; 
                z-index: 1000; 
                background: var(--bg-secondary); 
                border: 2px solid var(--border-color); 
                border-radius: 50px; 
                padding: 12px 18px; 
                cursor: pointer; 
                font-size: 14px; 
                font-weight: 600;
                box-shadow: 0 4px 12px var(--shadow);
                color: var(--text-primary);
                backdrop-filter: blur(10px);
            }
            .theme-toggle:hover { 
                transform: scale(1.05); 
                box-shadow: 0 6px 20px var(--shadow);
                border-color: var(--accent-blue);
            }
            
            /* Professional Status Indicators */
            .status-live { 
                background: linear-gradient(135deg, var(--positive-color), #4ade80); 
                color: white; 
                padding: 16px 20px; 
                border-radius: 10px; 
                margin: 16px 0; 
                font-weight: 600;
                box-shadow: 0 4px 12px rgba(63, 185, 80, 0.3);
            }
            .status-cached { 
                background: linear-gradient(135deg, var(--accent-blue), #3b82f6); 
                color: white; 
                padding: 16px 20px; 
                border-radius: 10px; 
                margin: 16px 0; 
                font-weight: 600;
                box-shadow: 0 4px 12px rgba(31, 111, 235, 0.3);
            }
            .status-sample { 
                background: linear-gradient(135deg, #f59e0b, #fbbf24); 
                color: white; 
                padding: 16px 20px; 
                border-radius: 10px; 
                margin: 16px 0; 
                font-weight: 600;
                box-shadow: 0 4px 12px rgba(245, 158, 11, 0.3);
            }
            .status-error { 
                background: linear-gradient(135deg, var(--negative-color), #f87171); 
                color: white; 
                padding: 16px 20px; 
                border-radius: 10px; 
                margin: 16px 0; 
                font-weight: 600;
                box-shadow: 0 4px 12px rgba(248, 81, 73, 0.3);
            }
            .status-icon { font-weight: bold; margin-right: 12px; font-size: 1.1rem; }
            
            /* Enhanced AI Button */
            .ai-button {
                background: linear-gradient(135deg, var(--accent-purple), #a855f7);
                color: white;
                border: none;
                padding: 8px 12px;
                border-radius: 8px;
                font-size: 12px;
                font-weight: 600;
                cursor: pointer;
                margin-left: 12px;
                box-shadow: 0 2px 8px rgba(139, 92, 246, 0.3);
                backdrop-filter: blur(10px);
            }
            
            .ai-button:hover {
                transform: translateY(-2px);
                box-shadow: 0 6px 16px rgba(139, 92, 246, 0.4);
                background: linear-gradient(135deg, #a855f7, var(--accent-purple));
            }
            
            /* Summary and Info Boxes */
            .summary { 
                background: linear-gradient(135deg, var(--summary-bg), #1e40af); 
                color: white; 
                padding: 24px; 
                border-radius: 12px; 
                margin: 20px 0;
                box-shadow: 0 4px 16px rgba(13, 65, 157, 0.3);
            }
            
            .timestamp { 
                text-align: center; 
                color: var(--text-secondary); 
                font-size: 14px; 
                margin: 20px 0;
                padding: 12px;
                background: var(--bg-tertiary);
                border-radius: 8px;
                border: 1px solid var(--border-color);
            }
            
            /* Stock Symbol Styling */
            .stock-symbol {
                color: var(--accent-blue);
                font-weight: 700;
                font-size: 1rem;
                display: inline-block;
                padding: 4px 0;
                border-bottom: 2px solid transparent;
            }
            .stock-symbol:hover {
                color: var(--accent-purple);
                border-bottom-color: var(--accent-purple);
            }
            
            /* Table Sorting */
            .sortable { 
                cursor: pointer; 
                user-select: none; 
                position: relative;
                transition: all 0.2s ease;
            }
            .sortable:hover { 
                background-color: var(--highlight-bg);
                color: var(--accent-blue);
            }
            .sortable::after { content: ' ↕️'; font-size: 12px; opacity: 0.5; }
            .sort-asc::after { content: ' ↑'; opacity: 1; color: var(--positive-color); }
            .sort-desc::after { content: ' ↓'; opacity: 1; color: var(--negative-color); }
            
            /* Chart Container */
            .chart-container {
                margin: 16px 0;
                text-align: center;
                background: var(--bg-tertiary);
                border-radius: 12px;
                padding: 20px;
                box-shadow: 0 4px 12px var(--shadow);
                border: 1px solid var(--border-color);
            }
            
            /* Animations */
            @keyframes pulse {
                0% { opacity: 1; }
                50% { opacity: 0.7; }
                100% { opacity: 1; }
            }
            
            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
            
            /* Mobile stock cards. Desktop keeps the dense table; below
               768px each stock becomes a tappable card, which beats swiping a
               nine-column table sideways on a phone. */
            .stock-cards { display: none; }
            .card-sorter { display: none; }
            @media (max-width: 768px) {
                .table-wrap { display: none; }
                .stock-cards { display: grid; gap: 10px; }
                .card-sorter {
                    display: flex; align-items: center; gap: 8px;
                    margin: 4px 0 12px; font-size: 12px; color: var(--text-secondary);
                }
                .sort-chip {
                    background: var(--bg-tertiary); color: var(--text-secondary);
                    border: 1px solid var(--border-color); border-radius: 999px;
                    padding: 6px 12px; font-size: 12px; cursor: pointer;
                }
                .sort-chip.active { background: #6c5ce7; color: #fff; border-color: #6c5ce7; }
            }
            .stock-card {
                background: var(--bg-secondary); border: 1px solid var(--border-color);
                border-radius: 12px; padding: 12px 14px; cursor: pointer;
                box-shadow: var(--shadow);
            }
            .stock-card:active { transform: scale(0.985); }
            .card-top { display: flex; align-items: baseline; gap: 10px; }
            .card-symbol { font-size: 18px; font-weight: 700; color: #6c8dff; }
            .card-price { color: var(--text-primary); font-weight: 600; }
            .card-change { margin-left: auto; color: #e74c3c; font-weight: 700; }
            .card-score-row { display: flex; align-items: baseline; gap: 6px; margin: 6px 0 8px; flex-wrap: wrap; }
            .card-score { font-size: 22px; font-weight: 800; color: var(--text-primary); }
            .card-score-sub { font-size: 11px; color: var(--text-secondary); }
            .card-sentiment { width: 100%; font-size: 12px; color: var(--text-secondary); }
            .card-chips { display: flex; flex-wrap: wrap; gap: 6px; }
            .chip {
                background: var(--bg-tertiary); border: 1px solid var(--border-color);
                border-radius: 999px; padding: 4px 10px; font-size: 12px;
                color: var(--text-secondary);
            }
            .chip strong { color: var(--text-primary); }
            .chip-upside { border-color: #2ecc71; color: #2ecc71; }
            .chip-upside em { font-style: normal; font-size: 10px; opacity: 0.8; }

            /* The tables scroll sideways inside this wrapper on narrow
               screens; without it nine columns crush into one-word-per-line
               cells and every row grows to several hundred pixels tall. */
            .table-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; }
            .table-wrap table { min-width: 760px; }
            .table-wrap th:first-child, .table-wrap td:first-child {
                position: sticky; left: 0; z-index: 2;
                background: var(--bg-secondary);
            }

            /* Responsive Design */
            @media (max-width: 768px) {
                .container { padding: 0 8px; }
                .section { padding: 12px; margin: 10px 0; }
                h1 { font-size: 1.5rem; }
                h2 { font-size: 1.15rem; }
                th, td { padding: 8px 10px; font-size: 0.8rem; }
                th { white-space: nowrap; }
                .ai-button { font-size: 11px; padding: 8px 10px; }

                /* The toggle floated over the header controls and covered the
                   Force Refresh button; on small screens it joins the flow. */
                .theme-toggle { position: static; margin: 8px auto; display: block; }

                /* Status pills: tighter so they wrap into tidy rows. */
                .status-badge { font-size: 11px !important; padding: 4px 8px; }

                /* Market overview cards stack full-width instead of leaving an
                   orphaned half-width card in a two-column grid. */
                .metrics-grid { grid-template-columns: 1fr !important; }

                /* The analysis modal becomes a full-screen sheet: the floating
                   card wasted a third of a phone screen on backdrop. */
                .modal-overlay > div {
                    width: 100% !important; max-width: 100% !important;
                    height: 100% !important; max-height: 100% !important;
                    border-radius: 0 !important; padding: 14px !important;
                }
                .ultimate-tab-btn { padding: 8px 8px !important; font-size: 11px !important; margin: 0 !important; }
            }
        </style>
        <script>
        
        function sortTable(table, column, direction) {
            const tbody = table.querySelector('tbody');
            const rows = Array.from(tbody.querySelectorAll('tr'));
            
            const sortedRows = rows.sort((a, b) => {
                const aVal = a.children[column].textContent.trim();
                const bVal = b.children[column].textContent.trim();
                
                // Try to parse as numbers (remove $ and % signs)
                const aNum = parseFloat(aVal.replace(/[$%,]/g, ''));
                const bNum = parseFloat(bVal.replace(/[$%,]/g, ''));
                
                if (!isNaN(aNum) && !isNaN(bNum)) {
                    return direction === 'asc' ? aNum - bNum : bNum - aNum;
                }
                
                // String comparison
                return direction === 'asc' ? 
                    aVal.localeCompare(bVal) : 
                    bVal.localeCompare(aVal);
            });
            
            // Clear tbody and append sorted rows
            tbody.innerHTML = '';
            sortedRows.forEach(row => tbody.appendChild(row));
        }
        
        function makeTablesSortable() {
            document.querySelectorAll('table').forEach(table => {
                const headers = table.querySelectorAll('th');
                headers.forEach((header, index) => {
                    header.classList.add('sortable');
                    header.addEventListener('click', () => {
                        // Reset other headers
                        headers.forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
                        
                        // Determine sort direction
                        const currentSort = header.getAttribute('data-sort') || 'none';
                        const newSort = currentSort === 'asc' ? 'desc' : 'asc';
                        
                        // Update header
                        header.setAttribute('data-sort', newSort);
                        header.classList.add(newSort === 'asc' ? 'sort-asc' : 'sort-desc');
                        
                        // Sort table
                        sortTable(table, index, newSort);
                    });
                });
            });
        }
        
        // Dark mode functionality
        function initTheme() {
            const savedTheme = localStorage.getItem('theme') || 'light';
            document.documentElement.setAttribute('data-theme', savedTheme);
            updateThemeToggle(savedTheme);
        }
        
        function toggleTheme() {
            const currentTheme = document.documentElement.getAttribute('data-theme') || 'light';
            const newTheme = currentTheme === 'dark' ? 'light' : 'dark';
            
            document.documentElement.setAttribute('data-theme', newTheme);
            localStorage.setItem('theme', newTheme);
            updateThemeToggle(newTheme);
        }
        
        function updateThemeToggle(theme) {
            const toggle = document.getElementById('theme-toggle');
            if (toggle) {
                toggle.textContent = theme === 'dark' ? '☀️ Light' : '🌙 Dark';
            }
        }
        
        /* ========================================================================
         * DATA SOURCES AND THEIR CURRENT STATUS
         * ========================================================================
         *
         * WORKING:
         *    - Daily losers:  query1.finance.yahoo.com/v1/finance/screener (day_losers)
         *    - Stock quotes:  query1.finance.yahoo.com/v8/finance/chart/{symbol}
         *    - StockTwits:    api.stocktwits.com/api/2/streams/symbol/{symbol}.json
         *    - Technicals:    RSI, MACD, Bollinger, MFI, VIX and SPY via yfinance
         *
         * CURRENTLY UNAVAILABLE (these now render as an em dash, never a
         * substituted value -- see provenance.py):
         *    - Analyst targets:  quoteSummary/financialData returns 401
         *    - Options chain:    v7/finance/options returns 401
         *    - Earnings dates:   quoteSummary/calendarEvents returns 401
         *    - Reddit mentions:  search.json returns 403 without OAuth
         *
         * The banner that used to sit here claimed no fabricated data was in
         * use. That was untrue: several fields were invented on failure. Those
         * fallbacks are gone. Anything still computed rather than reported is
         * tagged `estimated` in its payload.
         * ======================================================================== */
        
        // Auto-refresh functionality
        let autoRefreshInterval;
        let lastUpdateTime = Date.now();
        

        // Normalise the social payload for display. Missing data must render as
        // an em dash, never as a default number -- the previous code fell back
        // to "5/10", so an API failure looked like a real, calm reading.

        // Render a measured probability with the evidence behind it. When the
        // measurement could not be made, show an em dash and the reason --
        // never a number.

        // All API reads bypass the browser HTTP cache. The payload shapes
        // evolve, and a cached hour-old response made every backend fix
        // invisible -- the page kept replaying the old data. Server-side
        // caching keeps these requests cheap.

        // Client-side column sort for the loser tables. Numeric cells carry
        // their sortable value in data-val so display strings stay free-form.
        // Uncaught browser errors land server-side; swallowed-catch bugs
        // spent hours invisible tonight, so silence is no longer an option.
        function reportClientError(msg, src, line) {
            try { navigator.sendBeacon('/api/client-error',
                new Blob([JSON.stringify({msg, src, line})], {type: 'application/json'})); } catch (e) {}
        }
        window.addEventListener('error', (e) => reportClientError(e.message, e.filename, e.lineno));
        window.addEventListener('unhandledrejection', (e) => reportClientError('unhandledrejection: ' + (e.reason && e.reason.message || e.reason), '', 0));

        if ('serviceWorker' in navigator) {
            window.addEventListener('load', () => {
                navigator.serviceWorker.register('/sw.js').catch(() => {});
            });
        }

        // Re-sort the mobile cards by a data attribute. Ties keep DOM order.
        function sortCards(containerId, key, chip) {
            const box = document.getElementById(containerId);
            if (!box) return;
            chip.closest('.card-sorter').querySelectorAll('.sort-chip')
                .forEach(c => c.classList.remove('active'));
            chip.classList.add('active');
            [...box.querySelectorAll('.stock-card')]
                .sort((a, b) => (parseFloat(b.dataset[key]) || -Infinity) -
                                (parseFloat(a.dataset[key]) || -Infinity))
                .forEach(card => box.appendChild(card));
        }

        // Swipe left/right moves through the analysis tabs on touch screens.
        // Mostly-horizontal swipes only, so vertical scrolling stays untouched.
        const ULTIMATE_TABS = [
            ['sentiment-tab', '🤖📱'], ['recovery-tab', '🔮'],
            ['mediumterm-tab', '⏰'], ['longterm-tab', '📊']];
        let _swipeX = null, _swipeY = null;
        document.addEventListener('touchstart', (e) => {
            if (!e.target.closest('.modal-overlay')) { _swipeX = null; return; }
            _swipeX = e.touches[0].clientX; _swipeY = e.touches[0].clientY;
        }, {passive: true});
        document.addEventListener('touchend', (e) => {
            if (_swipeX === null) return;
            const dx = e.changedTouches[0].clientX - _swipeX;
            const dy = e.changedTouches[0].clientY - _swipeY;
            _swipeX = null;
            if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 2) return;
            const current = ULTIMATE_TABS.findIndex(([id]) => {
                const el = document.getElementById(id);
                return el && el.style.display !== 'none';
            });
            if (current === -1) return;
            const next = current + (dx < 0 ? 1 : -1);
            if (next < 0 || next >= ULTIMATE_TABS.length) return;
            try { switchUltimateTab(ULTIMATE_TABS[next][0], ULTIMATE_TABS[next][1]); } catch (err) {}
        }, {passive: true});

        function sortLoserTable(th, col, kind) {
            const table = th.closest('table');
            const body = table.querySelector('tbody');
            const dir = th.dataset.dir === 'desc' ? 'asc' : 'desc';
            table.querySelectorAll('th').forEach(h => delete h.dataset.dir);
            th.dataset.dir = dir;
            const rows = [...body.querySelectorAll('tr')];
            rows.sort((a, b) => {
                const ca = a.cells[col], cb = b.cells[col];
                let va, vb;
                if (kind === 'num') {
                    va = parseFloat(ca?.dataset.val ?? ca?.innerText) || -Infinity;
                    vb = parseFloat(cb?.dataset.val ?? cb?.innerText) || -Infinity;
                } else {
                    va = (ca?.innerText || '').trim(); vb = (cb?.innerText || '').trim();
                    return dir === 'desc' ? vb.localeCompare(va) : va.localeCompare(vb);
                }
                return dir === 'desc' ? vb - va : va - vb;
            });
            rows.forEach(r => body.appendChild(r));
        }

        function fetch2(url, opts) { return fetch(url, Object.assign({cache: 'no-store'}, opts || {})); }

        function probabilityBadge(target) {
            if (!target || target.probability_available !== true) {
                const why = (target && target.probability_reason) || 'not measured';
                return `<span style="color:#e9ecef; font-weight:600;">\u2014</span>
                        <span style="color:#ced4da; font-size:11px;"> (${why})</span>`;
            }
            const pct = Number(target.probability).toFixed(1);
            const tone = target.probability >= 50 ? '#7bed9f'
                       : target.probability >= 25 ? '#ffd97d' : '#ff9f9f';
            const ci = (target.ci_low !== undefined && target.ci_high !== undefined)
                ? ` <span style="color:#dee2e6; font-size:11px;">(&plusmn; CI ${Number(target.ci_low).toFixed(0)}\u2013${Number(target.ci_high).toFixed(0)}%)</span>` : '';
            return `<span style="color:${tone}; font-weight:700;">${pct}%</span>${ci}
                    <span style="color:#e9ecef; font-size:11px;"> ${target.evidence || ''}</span>`;
        }

        function socialDisplay(sentiment) {
            const s = (sentiment && sentiment.sentiment) || {};
            const has = s.bearish_ratio !== undefined && s.bearish_ratio !== null;
            return {
                label: s.label || 'Unavailable',
                color: s.color || '#6c757d',
                bearishText: has ? Math.round(s.bearish_ratio * 100) + '% bearish' : '\u2014',
                basis: has ? ('of ' + s.tagged_messages + ' tagged messages') : (s.reason || 'no tagged messages'),
                phrases: (sentiment && sentiment.trending_phrases) || []
            };
        }

        function isMarketHoliday(date) {
            const year = date.getFullYear();
            const month = date.getMonth() + 1; // JS months are 0-based
            const day = date.getDate();
            
            // Fixed holidays
            if ((month === 1 && day === 1) ||   // New Year's Day
                (month === 7 && day === 4) ||   // Independence Day  
                (month === 12 && day === 25) || // Christmas
                (month === 12 && day === 24) || // Christmas Eve
                (month === 12 && day === 31)) { // New Year's Eve
                return true;
            }
            
            // Basic holiday approximations (could be more precise)
            // MLK Day (3rd Monday in January), Presidents Day (3rd Monday in February)
            // Memorial Day (last Monday in May), Labor Day (1st Monday in September)
            // Thanksgiving (4th Thursday in November), Black Friday
            
            return false; // Simplified - server-side check is more accurate
        }
        
        function startAutoRefresh() {
            if (autoRefreshInterval) return; // Already running
            
            autoRefreshInterval = setInterval(() => {
                // Only refresh during market hours
                const now = new Date();
                const day = now.getDay(); // 0 = Sunday, 6 = Saturday
                const hour = now.getHours();
                
                // Skip weekends, holidays, and non-market hours (9:30 AM - 4:00 PM EST)
                if (day === 0 || day === 6 || hour < 9 || hour > 16 || isMarketHoliday(now)) {
                    return;
                }
                
                // Refresh every 3 hours during market hours
                if (Date.now() - lastUpdateTime > 10800000) { // 3 hours in milliseconds
                    updateLastRefreshTime();
                    showRefreshIndicator();
                    setTimeout(() => location.reload(), 1000);
                }
            }, 3600000); // Check every hour
        }
        
        function stopAutoRefresh() {
            if (autoRefreshInterval) {
                clearInterval(autoRefreshInterval);
                autoRefreshInterval = null;
            }
        }
        
        function updateLastRefreshTime() {
            lastUpdateTime = Date.now();
            const indicator = document.getElementById('live-indicator');
            if (indicator) {
                indicator.textContent = '🔴 Refreshing...';
            }
        }
        
        function showRefreshIndicator() {
            const indicator = document.getElementById('live-indicator');
            if (indicator) {
                indicator.innerHTML = '🟢 Live Updates Active';
                indicator.style.animation = 'pulse 1s infinite';
            }
        }
        
        // TradingView chart functionality
        function showTradingViewChart(symbol) {
            const existingChart = document.getElementById('chart-modal');
            if (existingChart) {
                existingChart.remove();
            }
            
            const modal = document.createElement('div');
            modal.id = 'chart-modal';
            modal.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 10000; display: flex; justify-content: center; align-items: center;';
            
            const chartContainer = document.createElement('div');
            chartContainer.style.cssText = 'background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 10px; padding: 20px; width: 95%; max-width: 1200px; height: 90%; position: relative;';
            
            // Create close button
            const closeBtn = document.createElement('button');
            closeBtn.innerHTML = '×';
            closeBtn.style.cssText = 'position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;';
            closeBtn.onclick = () => modal.remove();
            
            // Create title with exchange indicator
            const title = document.createElement('h3');
            title.textContent = symbol + ' - Live Chart (Auto-detect)';
            title.style.cssText = 'margin-top: 0; text-align: left; color: var(--text-primary); font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;';
            
            // Update title when switching exchanges
            const updateTitle = (exchange) => {
                title.textContent = symbol + ' - Live Chart (' + exchange + ')';
            };
            
            // Create chart with fallback options
            const chartFrame = document.createElement('iframe');
            chartFrame.style.cssText = 'width: 100%; height: calc(100% - 60px); border: none; border-radius: 5px;';
            
            // Start with Auto-detect (no prefix - most universal)
            chartFrame.src = 'https://www.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=' + symbol + '&interval=D&hideideas=1&hidetoptoolbar=1&hidecontrols=0&theme=light&style=1&timezone=Etc%2FUTC&studies=%5B%5D&overrides=%7B%7D&enabled_features=%5B%5D&disabled_features=%5B%5D&locale=en';
            
            // Add a button to try different exchanges
            const switchBtn = document.createElement('button');
            switchBtn.textContent = 'Try NASDAQ';
            switchBtn.style.cssText = 'position: absolute; top: 50px; right: 15px; background: #007bff; color: white; border: none; border-radius: 3px; padding: 5px 10px; cursor: pointer; font-size: 12px;';
            switchBtn.onclick = function() {
                if (this.textContent === 'Try NASDAQ') {
                    // Try NASDAQ prefix
                    chartFrame.src = 'https://www.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=NASDAQ:' + symbol + '&interval=D&hideideas=1&hidetoptoolbar=1&hidecontrols=0&theme=light&style=1&timezone=Etc%2FUTC&studies=%5B%5D&overrides=%7B%7D&enabled_features=%5B%5D&disabled_features=%5B%5D&locale=en';
                    updateTitle('NASDAQ');
                    this.textContent = 'Try NYSE';
                } else if (this.textContent === 'Try NYSE') {
                    // Try NYSE prefix
                    chartFrame.src = 'https://www.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=NYSE:' + symbol + '&interval=D&hideideas=1&hidetoptoolbar=1&hidecontrols=0&theme=light&style=1&timezone=Etc%2FUTC&studies=%5B%5D&overrides=%7B%7D&enabled_features=%5B%5D&disabled_features=%5B%5D&locale=en';
                    updateTitle('NYSE');
                    this.textContent = 'Try AMEX';
                } else if (this.textContent === 'Try AMEX') {
                    // Try AMEX prefix
                    chartFrame.src = 'https://www.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=AMEX:' + symbol + '&interval=D&hideideas=1&hidetoptoolbar=1&hidecontrols=0&theme=light&style=1&timezone=Etc%2FUTC&studies=%5B%5D&overrides=%7B%7D&enabled_features=%5B%5D&disabled_features=%5B%5D&locale=en';
                    updateTitle('AMEX');
                    this.textContent = 'Back to Auto-detect';
                } else {
                    // Back to Auto-detect (default)
                    chartFrame.src = 'https://www.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=' + symbol + '&interval=D&hideideas=1&hidetoptoolbar=1&hidecontrols=0&theme=light&style=1&timezone=Etc%2FUTC&studies=%5B%5D&overrides=%7B%7D&enabled_features=%5B%5D&disabled_features=%5B%5D&locale=en';
                    updateTitle('Auto-detect');
                    this.textContent = 'Try NASDAQ';
                }
            };
            
            chartContainer.appendChild(closeBtn);
            chartContainer.appendChild(switchBtn);
            chartContainer.appendChild(title);
            chartContainer.appendChild(chartFrame);
            modal.appendChild(chartContainer);
            document.body.appendChild(modal);
        }
        
        function makeSymbolsClickable() {
            document.querySelectorAll('.stock-symbol').forEach(symbol => {
                symbol.addEventListener('click', function() {
                    const ticker = this.textContent.trim();
                    showTradingViewChart(ticker);
                });
            });
        }
        
        // AI News Analysis functionality
        let analysisCache = {};
        
        function showAIAnalysis(symbol) {
            // Check cache first
            if (analysisCache[symbol]) {
                displayAnalysisModal(symbol, analysisCache[symbol]);
                return;
            }
            
            // Show loading modal first
            showAnalysisLoading(symbol);
            
            // Fetch AI analysis powered by REAL Yahoo Finance analyst recommendation data
            // Data Source: Yahoo Finance API - recommendation trends, earnings history, analyst downgrades
            fetch2('/api/news-analysis/' + symbol)
                .then(response => response.json())
                .then(data => {
                    analysisCache[symbol] = data.analysis;
                    displayAnalysisModal(symbol, data.analysis);
                })
                .catch(error => {
                    console.error('Analysis error:', error);
                    displayAnalysisModal(symbol, {
                        sentiment: 'unknown',
                        headlines: { available: false, count: 0, items: [], reason: 'request failed' },
                        analyst_posture: { available: false, reason: 'request failed' }
                    });
                });
        }
        
        function showAnalysisLoading(symbol) {
            const modal = createModal('ai-analysis-modal');
            const container = createModalContainer();
            
            container.innerHTML = `
                <button onclick="document.getElementById('ai-analysis-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: left; color: var(--text-primary); margin-top: 0; font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;">🤖 AI News Detective</h3>
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; animation: spin 1s linear infinite;">🔍</div>
                    <h4>Analyzing ${symbol}...</h4>
                    <p style="color: #666;">Scanning recent news and social media sentiment...</p>
                    <div class="loading-dots" style="margin: 20px 0;">
                        <span style="animation: pulse 1.5s ease-in-out infinite;">.</span>
                        <span style="animation: pulse 1.5s ease-in-out 0.5s infinite;">.</span>
                        <span style="animation: pulse 1.5s ease-in-out 1s infinite;">.</span>
                    </div>
                </div>
            `;
            
            modal.appendChild(container);
            document.body.appendChild(modal);
        }
        
        function displayAnalysisModal(symbol, analysis) {
            // Remove loading modal
            const existingModal = document.getElementById('ai-analysis-modal');
            if (existingModal) existingModal.remove();
            
            const modal = createModal('ai-analysis-modal');
            const container = createModalContainer();
            
            // Determine sentiment color and style
            const sentimentStyles = {
                'very_negative': { color: '#dc3545', bg: '#f8d7da', label: 'Very Negative' },
                'negative': { color: '#fd7e14', bg: '#fff3cd', label: 'Negative' },
                'neutral': { color: '#6c757d', bg: '#e2e3e5', label: 'Neutral' },
                'unknown': { color: '#6c757d', bg: '#e2e3e5', label: 'Unknown' },
                'error': { color: '#dc3545', bg: '#f8d7da', label: 'Error' }
            };
            
            const style = sentimentStyles[analysis.sentiment] || sentimentStyles['unknown'];
            
            container.innerHTML = `
                <button onclick="document.getElementById('ai-analysis-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: left; color: var(--text-primary); margin-top: 0; font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;">🤖 AI News Analysis: ${symbol}</h3>
                
                <div style="background: ${style.bg}; border: 1px solid ${style.color}; border-radius: 8px; padding: 20px; margin: 20px 0;">
                    <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); padding: 15px; border-radius: 5px; border-left: 4px solid ${style.color}; margin-bottom: 15px;">
                        <h4 style="margin: 0 0 10px 0; color: var(--text-primary);">Analyst posture</h4>
                        <p style="margin: 0; font-size: 15px; color: var(--text-primary);">
                            ${(analysis.analyst_posture && analysis.analyst_posture.available)
                                ? analysis.analyst_posture.summary
                                : '— <span style="color:#888; font-size:13px;">(' + ((analysis.analyst_posture && analysis.analyst_posture.reason) || 'unavailable') + ')</span>'}
                        </p>
                    </div>

                    <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); padding: 15px; border-radius: 5px;">
                        <h4 style="margin: 0 0 10px 0; color: var(--text-primary);">
                            Recent headlines${(analysis.headlines && analysis.headlines.count) ? ' (' + analysis.headlines.count + ')' : ''}
                        </h4>
                        ${analysis.fall_reason ? `<div style="margin: 0 0 10px 0;">
                            <span title="${analysis.fall_reason.basis}" style="background: rgba(108,92,231,0.18); border: 1px solid #6c5ce7; border-radius: 999px; padding: 3px 10px; font-size: 12px; color: var(--text-primary);">
                                Likely reason: <strong>${analysis.fall_reason.label}</strong>
                            </span></div>` : ''}
                        ${(analysis.headlines && analysis.headlines.available && analysis.headlines.items.length)
                            ? analysis.headlines.items.map(h => `
                                <div style="padding: 8px 0; border-bottom: 1px solid var(--border-color);">
                                    <a href="${h.url || '#'}" target="_blank" rel="noopener noreferrer"
                                       style="color: var(--text-primary); text-decoration: none; font-size: 15px; line-height: 1.4;">
                                        ${h.title}
                                    </a>
                                    <div style="font-size: 12px; color: #888; margin-top: 3px;">${h.publisher || ''}</div>
                                </div>`).join('')
                            : `<p style="margin:0; color:#888;">— no headlines available
                                 ${(analysis.headlines && analysis.headlines.reason) ? '(' + analysis.headlines.reason + ')' : ''}</p>`}
                        <div style="font-size: 11px; color: #888; margin-top: 10px;">
                            Source: ${(analysis.headlines && analysis.headlines.source) || 'unknown'}
                        </div>
                    </div>
                </div>
                
                <div style="text-align: center; margin-top: 20px;">
                    <button onclick="showTradingViewChart('${symbol}')" 
                            style="background: #007bff; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 10px; cursor: pointer;">
                        📈 View Chart
                    </button>
                    <button onclick="window.open('https://finance.yahoo.com/quote/${symbol}/news', '_blank')" 
                            style="background: #28a745; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 10px; cursor: pointer;">
                        📰 Read News
                    </button>
                </div>
            `;
            
            modal.appendChild(container);
            document.body.appendChild(modal);
        }
        
        function createModal(id) {
            const modal = document.createElement('div');
            modal.className = 'modal-overlay';
            modal.id = id;
            modal.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 10000; display: flex; justify-content: center; align-items: center;';
            return modal;
        }
        
        function createModalContainer() {
            const container = document.createElement('div');
            container.style.cssText = 'background: var(--bg-primary); border: 1px solid var(--border-color); border-radius: 10px; padding: 20px; width: 95%; max-width: 900px; max-height: 90%; overflow-y: auto; position: relative;';
            return container;
        }
        
        // Recovery Prediction functionality
        let recoveryCache = {};
        
        function showRecoveryPrediction(symbol) {
            if (recoveryCache[symbol]) {
                displayRecoveryModal(symbol, recoveryCache[symbol]);
                return;
            }
            
            showRecoveryLoading(symbol);
            
            // Fetch SOPHISTICATED recovery prediction using REAL multi-target analysis
            // Data Sources: Yahoo Finance (prices, volumes, analyst targets), yfinance (technical indicators)
            fetch2('/api/recovery-prediction/' + symbol)
                .then(response => response.json())
                .then(data => {
                    const recoveryData = data.prediction;
                    recoveryCache[symbol] = recoveryData;
                    displayRecoveryModal(symbol, recoveryData);
                })
                .catch(error => {
                    console.error('Recovery prediction error:', error);
                    displayRecoveryModal(symbol, {
                        recovery_score: 0,
                        recommendation: 'Unable to analyze recovery potential',
                        confidence: 'low',
                        risk_level: 'high'
                    });
                });
        }
        
        function showRecoveryLoading(symbol) {
            const modal = createModal('recovery-modal');
            const container = createModalContainer();
            
            container.innerHTML = `
                <button onclick="document.getElementById('recovery-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: left; color: var(--text-primary); margin-top: 0; font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;">🔮 Recovery Predictor</h3>
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; animation: spin 1s linear infinite;">🔮</div>
                    <h4>Analyzing ${symbol} Recovery Potential...</h4>
                    <p style="color: #666;">Analyzing technical indicators, historical patterns, and fundamentals...</p>
                </div>
            `;
            
            modal.appendChild(container);
            document.body.appendChild(modal);
        }
        
        function displayRecoveryModal(symbol, prediction) {
            const existingModal = document.getElementById('recovery-modal');
            if (existingModal) existingModal.remove();
            
            const modal = createModal('recovery-modal');
            const container = createModalContainer();
            
            const riskColors = {
                'low': '#28a745',
                'moderate': '#ffc107', 
                'high': '#dc3545'
            };
            
            const riskColor = riskColors[prediction.risk_level] || '#6c757d';
            
            let factorsHtml = '';
            if (prediction.factors) {
                factorsHtml = `
                    <div style="margin-top: 20px;">
                        <h5>📊 Analysis Factors:</h5>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-top: 10px;">
                            <div>
                                <strong>Technical:</strong>
                                <ul style="margin: 5px 0; font-size: 12px;">
                                    ${prediction.factors.technical.map(f => `<li>${f}</li>`).join('')}
                                </ul>
                            </div>
                            <div>
                                <strong>Historical:</strong>
                                <ul style="margin: 5px 0; font-size: 12px;">
                                    ${prediction.factors.historical.map(f => `<li>${f}</li>`).join('')}
                                </ul>
                            </div>
                            <div>
                                <strong>Fundamental:</strong>
                                <ul style="margin: 5px 0; font-size: 12px;">
                                    ${prediction.factors.fundamental.map(f => `<li>${f}</li>`).join('')}
                                </ul>
                            </div>
                            <div>
                                <strong>News Impact:</strong>
                                <ul style="margin: 5px 0; font-size: 12px;">
                                    ${prediction.factors.news.map(f => `<li>${f}</li>`).join('')}
                                </ul>
                            </div>
                        </div>
                    </div>
                `;
            }
            
            container.innerHTML = `
                <button onclick="document.getElementById('recovery-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: left; color: var(--text-primary); margin-top: 0; font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;">🔮 Recovery Prediction: ${symbol}</h3>
                
                <div style="text-align: center; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border-radius: 10px; margin: 15px 0;">
                    <div style="font-size: 48px; margin-bottom: 10px;">
                        ${prediction.recovery_score >= 75 ? '🚀' : prediction.recovery_score >= 60 ? '📈' : prediction.recovery_score >= 40 ? '⚖️' : '⚠️'}
                    </div>
                    <div style="font-size: 36px; font-weight: bold; margin-bottom: 5px;">
                        ${Math.round(prediction.recovery_score * 10) / 10}% Recovery Score
                    </div>
                    <div style="font-size: 18px; opacity: 0.9;">
                        Expected timeframe: ${prediction.timeframe}
                    </div>
                </div>
                
                <div style="background: ${riskColor}; color: white; padding: 15px; border-radius: 8px; margin: 15px 0; text-align: center;">
                    <div style="font-size: 18px; font-weight: bold; margin-bottom: 5px;">
                        ${prediction.recommendation}
                    </div>
                    <div style="font-size: 14px; opacity: 0.9;">
                        Risk Level: ${prediction.risk_level.toUpperCase()} • Confidence: ${prediction.confidence.replace('_', ' ').toUpperCase()}
                    </div>
                </div>
                
                ${factorsHtml}
                
                <div style="text-align: center; margin-top: 20px;">
                    <button onclick="showTradingViewChart('${symbol}')" 
                            style="background: #007bff; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 5px; cursor: pointer;">
                        📈 View Chart
                    </button>
                    <button onclick="showUltimateAnalysis('${symbol}')" 
                            style="background: linear-gradient(45deg, #007bff, #28a745, #fd7e14); color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 5px; cursor: pointer;">
                        🤖📱🔮 Complete Analysis
                    </button>
                </div>
            `;
            
            modal.appendChild(container);
            document.body.appendChild(modal);
        }
        
        // Social Sentiment functionality  
        let sentimentCache = {};
        
        function showSocialSentiment(symbol) {
            if (sentimentCache[symbol]) {
                displaySentimentModal(symbol, sentimentCache[symbol]);
                return;
            }
            
            showSentimentLoading(symbol);
            
            // Fetch REAL social sentiment from Reddit API and StockTwits API
            // Data Sources: Reddit search API, StockTwits streaming API, real mention counts and sentiment
            fetch2('/api/social-sentiment/' + symbol)
                .then(response => response.json())
                .then(data => {
                    sentimentCache[symbol] = data.sentiment;
                    displaySentimentModal(symbol, data.sentiment);
                })
                .catch(error => {
                    console.error('Social sentiment error:', error);
                    displaySentimentModal(symbol, {
                        sentiment: { label: 'Unavailable', color: '#6c757d', reason: 'request failed' },
                        overall_sentiment: 'unknown'
                    });
                });
        }
        
        function showSentimentLoading(symbol) {
            const modal = createModal('sentiment-modal');
            const container = createModalContainer();
            
            container.innerHTML = `
                <button onclick="document.getElementById('sentiment-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: left; color: var(--text-primary); margin-top: 0; font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;">📱 Social Sentiment Radar</h3>
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; animation: pulse 1.5s ease-in-out infinite;">📊</div>
                    <h4>Scanning Social Media for ${symbol}...</h4>
                    <p style="color: #666;">Analyzing Reddit, Twitter, StockTwits, and news sentiment...</p>
                </div>
            `;
            
            modal.appendChild(container);
            document.body.appendChild(modal);
        }
        
        function displaySentimentModal(symbol, sentiment) {
            const existingModal = document.getElementById('sentiment-modal');
            if (existingModal) existingModal.remove();
            
            const modal = createModal('sentiment-modal');
            const container = createModalContainer();
            
            // Handle both new real data format and old simulated format
            const isNewFormat = sentiment.sentiment_label !== undefined;
            
            // Get color based on panic level or format
            const getColorByPanic = (level) => {
                if (level <= 3) return '#28a745'; // Green for low panic
                if (level <= 6) return '#ffc107'; // Yellow for medium panic  
                return '#dc3545'; // Red for high panic
            };
            
            const panicColor = socialDisplay(sentiment).color;
            
            // Get display values based on format
            const sentimentDisplay = isNewFormat ? 
                (sentiment.sentiment_label || '😐 Neutral') : 
                socialDisplay(sentiment).label;
            const volumeDisplay = isNewFormat ? 
                (sentiment.volume_interest || '📊 Standard interest') : 
                (sentiment.social_volume || 'Standard');
            
            container.innerHTML = `
                <button onclick="document.getElementById('sentiment-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: left; color: var(--text-primary); margin-top: 0; font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;">📱 Social Sentiment: ${symbol}</h3>
                
                <div style="text-align: center; padding: 25px; background: ${panicColor}; color: white; border-radius: 10px; margin: 15px 0;">
                    <div style="font-size: 36px; font-weight: bold; margin-bottom: 10px;">
                        ${sentimentDisplay}
                    </div>
                    <div style="font-size: 18px; opacity: 0.9;">
                        ${socialDisplay(sentiment).bearishText} ${socialDisplay(sentiment).basis}
                    </div>
                    ${isNewFormat ? `<div style="font-size: 16px; margin-top: 10px;">
                        ${volumeDisplay}
                    </div>` : ''}
                </div>
                
                ${!isNewFormat ? `<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin: 20px 0; text-align: center;">
                    <div style="background: #f8f9fa; padding: 15px; border-radius: 8px;">
                        <div style="font-size: 24px; font-weight: bold; color: #ff4757;">${sentiment.reddit_mentions || 0}</div>
                        <div style="font-size: 12px; color: #666;">Reddit Mentions</div>
                    </div>
                    <div style="background: #f8f9fa; padding: 15px; border-radius: 8px;">
                        <div style="font-size: 24px; font-weight: bold; color: #2ecc71;">${sentiment.stocktwits_mentions || 0}</div>
                        <div style="font-size: 12px; color: #666;">StockTwits Posts</div>
                    </div>
                </div>` : `<div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin: 20px 0; text-align: center;">
                    <div style="background: #f8f9fa; padding: 15px; border-radius: 8px;">
                        <div style="font-size: 24px; font-weight: bold; color: #ff4757;">📈</div>
                        <div style="font-size: 14px; color: #666;">Market Sentiment</div>
                        <div style="font-size: 16px; font-weight: bold; color: #333;">${sentimentDisplay}</div>
                    </div>
                    <div style="background: #f8f9fa; padding: 15px; border-radius: 8px;">
                        <div style="font-size: 24px; font-weight: bold; color: #1da1f2;">🔥</div>
                        <div style="font-size: 14px; color: #666;">Interest Level</div>
                        <div style="font-size: 16px; font-weight: bold; color: #333;">${volumeDisplay}</div>
                    </div>
                </div>`}
                
                <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 15px 0;">
                    <h5 style="margin: 0 0 10px 0;">🔥 ${isNewFormat ? 'Key Indicators:' : 'Trending Phrases:'}</h5>
                    <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                        ${(sentiment.trending_phrases || ['Standard sentiment', 'Market tracking']).map(phrase => 
                            `<span style="background: ${panicColor}; color: white; padding: 4px 8px; border-radius: 12px; font-size: 12px;">"${phrase}"</span>`
                        ).join('')}
                    </div>
                </div>
                
                <div style="text-align: center; margin-top: 20px;">
                    <button onclick="showRecoveryPrediction('${symbol}')" 
                            style="background: #28a745; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 5px; cursor: pointer;">
                        🔮 Recovery Analysis
                    </button>
                    <button onclick="window.open('https://www.reddit.com/search/?q=${symbol}', '_blank')" 
                            style="background: #ff4757; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 5px; cursor: pointer;">
                        🔍 View Reddit Discussion
                    </button>
                </div>
            `;
            
            modal.appendChild(container);
            document.body.appendChild(modal);
        }
        
        // Combined Analysis functionality (Social Sentiment + Recovery)
        function showComprehensiveAnalysis(symbol) {
            // Create loading modal first
            showComprehensiveLoading(symbol);
            
            // Fetch REAL social sentiment and sophisticated recovery analysis in parallel
            // Data Sources: Reddit API + StockTwits API + Yahoo Finance sophisticated multi-target analysis
            Promise.all([
                fetch2('/api/social-sentiment/' + symbol).then(response => response.json()),
                // FORCE BROWSER RELOAD - VERSION 2.1 - CACHE_BUSTER_20250906
            fetch2('/api/sophisticated-timeframe/' + symbol).then(response => response.json())
            ]).then(([sentimentData, recoveryData]) => {
                // Cache the results
                sentimentCache[symbol] = sentimentData.sentiment;
                recoveryCache[symbol] = recoveryData.prediction;
                
                displayComprehensiveModal(symbol, sentimentData.sentiment, recoveryData.prediction);
            }).catch(error => {
                console.error('Comprehensive analysis error:', error);
                displayComprehensiveModal(symbol, null, null);
            });
        }
        
        function showComprehensiveLoading(symbol) {
            const modal = createModal('comprehensive-modal');
            const container = createModalContainer();
            
            container.innerHTML = `
                <button onclick="document.getElementById('comprehensive-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: left; color: var(--text-primary); margin-top: 0; font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;">🔮📱 Complete Analysis: ${symbol}</h3>
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; animation: spin 1s linear infinite;">🔮</div>
                    <div style="margin-top: 20px; font-size: 16px; color: #666;">
                        Analyzing social sentiment and recovery potential...
                    </div>
                </div>
            `;
            
            modal.appendChild(container);
            document.body.appendChild(modal);
        }
        
        function displayComprehensiveModal(symbol, sentiment, recovery) {
            const existingModal = document.getElementById('comprehensive-modal');
            if (existingModal) existingModal.remove();
            
            const modal = createModal('comprehensive-modal');
            const container = createModalContainer();
            
            // Handle missing data
            if (!sentiment) sentiment = { sentiment: { label: 'Unavailable', color: '#6c757d', reason: 'request failed' }, trending_phrases: [] };
            if (!recovery) recovery = { recovery_score: 0, recommendation: 'Analysis unavailable', confidence: 'low' };
            
            // Handle both data formats for sentiment
            const isNewFormat = sentiment.sentiment_label !== undefined;
            const getColorByPanic = (level) => {
                if (level <= 3) return '#28a745';
                if (level <= 6) return '#ffc107';
                return '#dc3545';
            };
            
            const panicColor = socialDisplay(sentiment).color;
            const sentimentDisplay = isNewFormat ? 
                (sentiment.sentiment_label || '😐 Neutral') : 
                socialDisplay(sentiment).label;
            
            // Recovery color based on score
            const getRecoveryColor = (score) => {
                if (score >= 75) return '#28a745';
                if (score >= 60) return '#ffc107';
                if (score >= 40) return '#fd7e14';
                return '#dc3545';
            };
            
            const recoveryColor = getRecoveryColor(recovery.recovery_score || 0);
            
            container.innerHTML = `
                <button onclick="document.getElementById('comprehensive-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: left; color: var(--text-primary); margin-top: 0; font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;">🔮📱 Complete Analysis: ${symbol}</h3>
                
                <!-- Social Sentiment Section -->
                <div style="background: ${panicColor}; color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                    <h4 style="margin: 0 0 15px 0; text-align: center;">📱 Social Sentiment</h4>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; text-align: center;">
                        <div>
                            <div style="font-size: 28px; font-weight: bold;">${sentimentDisplay}</div>
                            <div style="font-size: 14px; opacity: 0.9;">Current Mood</div>
                        </div>
                        <div>
                            <div style="font-size: 28px; font-weight: bold;">${socialDisplay(sentiment).bearishText}</div>
                            <div style="font-size: 14px; opacity: 0.9;">${socialDisplay(sentiment).basis}</div>
                        </div>
                    </div>
                    ${isNewFormat ? `<div style="text-align: center; margin-top: 10px; font-size: 16px;">
                        ${sentiment.volume_interest || '📊 Standard interest'}
                    </div>` : ''}
                </div>
                
                <!-- Recovery Analysis Section -->
                <div style="background: ${recoveryColor}; color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                    <h4 style="margin: 0 0 15px 0; text-align: center;">🔮 Recovery Potential</h4>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; text-align: center;">
                        <div>
                            <div style="font-size: 28px; font-weight: bold;">${Math.round((recovery.recovery_score || 0) * 10) / 10}%</div>
                            <div style="font-size: 14px; opacity: 0.9;">Recovery Score</div>
                        </div>
                        <div>
                            <div style="font-size: 16px; font-weight: bold;">${recovery.confidence || 'Low'}</div>
                            <div style="font-size: 14px; opacity: 0.9;">Confidence</div>
                        </div>
                    </div>
                    <div style="text-align: center; margin-top: 15px; font-size: 16px; font-weight: bold;">
                        ${recovery.recommendation || 'Analysis unavailable'}
                    </div>
                    ${recovery.timeframe ? `<div style="text-align: center; margin-top: 5px; font-size: 14px; opacity: 0.9;">
                        Expected timeframe: ${recovery.timeframe}
                    </div>` : ''}
                </div>
                
                <!-- Key Indicators -->
                ${(socialDisplay(sentiment).phrases.length > 0) ? `
                <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 15px 0;">
                    <h5 style="margin: 0 0 10px 0; color: #333;">🔥 Key Market Indicators</h5>
                    <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                        ${socialDisplay(sentiment).phrases.map(p =>
                            `<span style="background: ${panicColor}; color: white; padding: 4px 8px; border-radius: 12px; font-size: 12px;">${p.phrase} (${p.count})</span>`
                        ).join('')}
                    </div>
                </div>` : ''}
                
                <!-- Action Buttons -->
                <div style="text-align: center; margin-top: 20px;">
                    <button onclick="showTradingViewChart('${symbol}')" 
                            style="background: #007bff; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 5px; cursor: pointer;">
                        📈 View Chart
                    </button>
                    <button onclick="window.open('https://finance.yahoo.com/quote/${symbol}/news', '_blank')" 
                            style="background: #28a745; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 5px; cursor: pointer;">
                        📰 Latest News
                    </button>
                    <button onclick="window.open('https://www.reddit.com/search/?q=${symbol}', '_blank')" 
                            style="background: #ff4757; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 5px; cursor: pointer;">
                        🔍 Social Discussion
                    </button>
                </div>
            `;
            
            modal.appendChild(container);
            document.body.appendChild(modal);
        }
        
        // Ultimate Complete Analysis functionality (AI + Social + Recovery)
        function showUltimateAnalysis(symbol, companyName = '') {
            // Create loading modal first
            showUltimateLoading(symbol, companyName);
            
            // Fetch ALL THREE REAL analysis types in parallel for comprehensive stock analysis
            // Data Sources: Yahoo Finance analyst data + Reddit/StockTwits APIs + Sophisticated multi-target recovery
            Promise.all([
                fetch2('/api/news-analysis/' + symbol).then(response => response.json()),
                fetch2('/api/social-sentiment/' + symbol).then(response => response.json()),
                // FORCE BROWSER RELOAD - VERSION 2.1 - CACHE_BUSTER_20250906
            fetch2('/api/sophisticated-timeframe/' + symbol).then(response => response.json())
            ]).then(([aiData, sentimentData, recoveryData]) => {
                // Cache all results
                analysisCache[symbol] = aiData.analysis;
                sentimentCache[symbol] = sentimentData.sentiment;
                recoveryCache[symbol] = recoveryData.prediction;
                
                // Store recovery data globally for medium/long-term breakdowns
                window.currentRecoveryData = recoveryData;
                
                displayUltimateModal(symbol, aiData.analysis, sentimentData.sentiment, recoveryData.prediction, companyName);
            }).catch(error => {
                console.error('Ultimate analysis error:', error);
                displayUltimateModal(symbol, null, null, null, companyName);
            });
        }
        
        function showUltimateLoading(symbol, companyName = '') {
            const modal = createModal('ultimate-modal');
            const container = createModalContainer();
            
            const displayName = companyName ? `${symbol} - ${companyName}` : symbol;
            
            container.innerHTML = `
                <button onclick="document.getElementById('ultimate-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: left; color: var(--text-primary); margin-top: 0; font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;">📊 Complete Analysis: ${displayName}</h3>
                <div style="text-align: center; padding: 40px;">
                    <div style="font-size: 48px; animation: spin 1s linear infinite;">🤖</div>
                    <div style="margin-top: 20px; font-size: 16px; color: #666;">
                        Running comprehensive AI + Social + Recovery analysis...
                    </div>
                    <div style="margin-top: 10px; font-size: 14px; color: #999;">
                        This may take a few moments as we gather insights from multiple sources
                    </div>
                </div>
            `;
            
            modal.appendChild(container);
            document.body.appendChild(modal);
        }
        
        function displayUltimateModal(symbol, aiAnalysis, sentiment, recovery, companyName = '') {
            const existingModal = document.getElementById('ultimate-modal');
            if (existingModal) existingModal.remove();
            
            const modal = createModal('ultimate-modal');
            const container = createModalContainer();
            
            // Handle missing data
            if (!aiAnalysis) aiAnalysis = { headlines: { available: false, items: [], reason: 'request failed' },
                                            analyst_posture: { available: false } };
            if (!sentiment) sentiment = { sentiment: { label: 'Unavailable', color: '#6c757d', reason: 'request failed' }, trending_phrases: [] };
            if (!recovery) recovery = { recovery_score: 0, recommendation: 'Analysis unavailable', confidence: 'low' };
            
            // Data format handling
            const isNewFormat = sentiment.sentiment_label !== undefined;
            const getColorByPanic = (level) => {
                if (level <= 3) return '#28a745';
                if (level <= 6) return '#ffc107';
                return '#dc3545';
            };
            const getRecoveryColor = (score) => {
                if (score >= 75) return '#28a745';
                if (score >= 60) return '#ffc107';
                if (score >= 40) return '#fd7e14';
                return '#dc3545';
            };
            
            const panicColor = socialDisplay(sentiment).color;
            const recoveryColor = getRecoveryColor(recovery.recovery_score || 0);
            const sentimentDisplay = isNewFormat ? (sentiment.sentiment_label || '😐 Neutral') : socialDisplay(sentiment).label;
            
            // Map AI sentiment to meaningful category
            const getCategoryFromSentiment = (sentiment) => {
                if (!sentiment) return '📊 News Analysis';
                switch(sentiment.toLowerCase()) {
                    case 'positive': return '📈 Positive News';
                    case 'negative': return '📉 Negative News';  
                    case 'neutral': return '📊 Neutral News';
                    case 'bullish': return '🐂 Bullish Outlook';
                    case 'bearish': return '🐻 Bearish Outlook';
                    default: return '📰 Market News';
                }
            };
            const aiCategory = getCategoryFromSentiment(aiAnalysis.sentiment);
            const displayName = companyName ? `${symbol} - ${companyName}` : symbol;
            
            container.innerHTML = `
                <button onclick="document.getElementById('ultimate-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: left; color: var(--text-primary); margin-top: 0; font-size: 20px; font-weight: bold; padding: 15px 0; border-bottom: 2px solid var(--border-color); margin-bottom: 20px;">📊 Complete Analysis: ${displayName}</h3>
                
                <!-- Tab Navigation -->
                <div style="display: flex; justify-content: center; margin: 20px 0; border-bottom: 2px solid #eee; flex-wrap: wrap;">
                    <button onclick="switchUltimateTab('sentiment-tab', '🤖📱')" id="sentiment-tab-btn" class="ultimate-tab-btn ultimate-tab-active" style="background: none; border: none; padding: 10px 16px; margin: 0 3px; cursor: pointer; border-bottom: 3px solid #007bff; font-weight: bold; color: #007bff; font-size: 13px;">🤖📱 Market Sentiment</button>
                    <button onclick="switchUltimateTab('recovery-tab', '🔮')" id="recovery-tab-btn" class="ultimate-tab-btn" style="background: none; border: none; padding: 10px 16px; margin: 0 3px; cursor: pointer; border-bottom: 3px solid transparent; color: #666; font-size: 13px;">🔮 Short Term</button>
                    <button onclick="switchUltimateTab('mediumterm-tab', '⏰')" id="mediumterm-tab-btn" class="ultimate-tab-btn" style="background: none; border: none; padding: 10px 16px; margin: 0 3px; cursor: pointer; border-bottom: 3px solid transparent; color: #666; font-size: 13px;">⏰ Medium Term</button>
                    <button onclick="switchUltimateTab('longterm-tab', '📊')" id="longterm-tab-btn" class="ultimate-tab-btn" style="background: none; border: none; padding: 10px 16px; margin: 0 3px; cursor: pointer; border-bottom: 3px solid transparent; color: #666; font-size: 13px;">📊 Long Term</button>
                </div>
                
                <!-- Combined Market Sentiment Tab (AI News + Social) -->
                <div id="sentiment-tab" class="ultimate-tab-content" style="display: block;">
                    <!-- AI News Analysis Section -->
                    <div style="background: linear-gradient(45deg, #007bff, #6610f2); color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                        <h4 style="margin: 0 0 15px 0; text-align: center;">📰 Recent Headlines</h4>
                        <div style="font-size: 15px; line-height: 1.6; margin-bottom: 15px;">
                            ${(aiAnalysis.headlines && aiAnalysis.headlines.available && aiAnalysis.headlines.items.length)
                                ? aiAnalysis.headlines.items.slice(0, 4).map(h => `
                                    <div style="padding: 7px 0; border-bottom: 1px solid rgba(255,255,255,0.25);">
                                        <a href="${h.url || '#'}" target="_blank" rel="noopener noreferrer"
                                           style="color: white; text-decoration: none;">${h.title}</a>
                                        <div style="font-size: 12px; opacity: 0.75;">${h.publisher || ''}</div>
                                    </div>`).join('')
                                : `<div style="text-align:center; opacity:0.85;">\u2014 no headlines available${
                                    (aiAnalysis.headlines && aiAnalysis.headlines.reason) ? ' (' + aiAnalysis.headlines.reason + ')' : ''}</div>`}
                        </div>
                        <div style="text-align: center; font-size: 15px; padding-top: 10px; border-top: 1px solid rgba(255,255,255,0.25);">
                            ${(aiAnalysis.analyst_posture && aiAnalysis.analyst_posture.available)
                                ? aiAnalysis.analyst_posture.summary
                                : '\u2014 analyst ratings unavailable'}
                            <div style="font-size: 12px; opacity: 0.75; margin-top: 4px;">Analyst posture</div>
                        </div>
                    </div>
                    
                    <!-- Social Sentiment Section -->
                    <div style="background: ${panicColor}; color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                        <h4 style="margin: 0 0 15px 0; text-align: center;">📱 Social Media Sentiment</h4>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; text-align: center;">
                            <div>
                                <div style="font-size: 24px; font-weight: bold;">${sentimentDisplay}</div>
                                <div style="font-size: 14px; opacity: 0.9;">Current Mood</div>
                            </div>
                            <div>
                                <div style="font-size: 24px; font-weight: bold;">${socialDisplay(sentiment).bearishText}</div>
                                <div style="font-size: 14px; opacity: 0.9;">${socialDisplay(sentiment).basis}</div>
                            </div>
                        </div>
                        ${isNewFormat ? `<div style="text-align: center; margin-top: 15px; font-size: 16px;">
                            ${sentiment.volume_interest || '📊 Standard interest'}
                        </div>` : ''}
                    </div>
                    
                    <!-- Trending Phrases -->
                    ${(socialDisplay(sentiment).phrases.length > 0) ? `
                    <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 15px 0;">
                        <h5 style="margin: 0 0 10px 0; color: #333;">🔥 Trending Phrases & Market Indicators</h5>
                        <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                            ${socialDisplay(sentiment).phrases.map(p =>
                                `<span style="background: ${panicColor}; color: white; padding: 4px 8px; border-radius: 12px; font-size: 12px;">${p.phrase} (${p.count})</span>`
                            ).join('')}
                        </div>
                    </div>` : ''}
                </div>
                
                <!-- Recovery Analysis Tab -->
                <div id="recovery-tab" class="ultimate-tab-content" style="display: none;">
                    <div style="background: linear-gradient(135deg, #fd7e14, #f39c12); color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                        <h4 style="margin: 0 0 15px 0; text-align: center;">🔮 Short Term Recovery (1-7 days)</h4>
                        <div id="recovery-data">
                            <div style="text-align: center; color: rgba(255,255,255,0.8);">
                                <div style="font-size: 16px; margin: 20px 0;">⏳ Loading short-term analysis...</div>
                            </div>
                        </div>
                        
                        <!-- Mathematical Breakdown Section for Short Term -->
                        <div id="recovery-breakdown" style="background: rgba(0,0,0,0.3); border-radius: 8px; padding: 15px; margin: 15px 0; color: white; border: 1px solid rgba(255,255,255,0.2); display: none;">
                            <h5 style="margin: 0 0 15px 0; text-align: center; color: #ffffff; text-shadow: 1px 1px 2px rgba(0,0,0,0.5);">🧮 Mathematical Breakdown</h5>
                            <div id="recovery-breakdown-content" style="font-size: 14px; line-height: 1.6; color: #ffffff;">
                                <!-- Content will be populated by JavaScript -->
                            </div>
                        </div>
                    </div>
                </div>
                
                <!-- Medium Term Recovery Tab -->
                <div id="mediumterm-tab" class="ultimate-tab-content" style="display: none;">
                    <div style="background: linear-gradient(135deg, #6610f2, #e83e8c); color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                        <h4 style="margin: 0 0 15px 0; text-align: center;">⏰ Medium Term Recovery (1-4 weeks)</h4>
                        <div id="mediumterm-data">
                            <div style="text-align: center; color: rgba(255,255,255,0.8);">
                                <div style="font-size: 16px; margin: 20px 0;">⏳ Loading medium-term analysis...</div>
                            </div>
                        </div>
                        
                        <!-- Mathematical Breakdown Section for Medium Term -->
                        <div id="mediumterm-breakdown" style="background: rgba(0,0,0,0.3); border-radius: 8px; padding: 15px; margin: 15px 0; color: white; border: 1px solid rgba(255,255,255,0.2); display: none;">
                            <h5 style="margin: 0 0 15px 0; text-align: center; color: #ffffff; text-shadow: 1px 1px 2px rgba(0,0,0,0.5);">🧮 Mathematical Breakdown</h5>
                            <div id="mediumterm-breakdown-content" style="font-size: 14px; line-height: 1.6; color: #ffffff;">
                                <!-- Content will be populated by JavaScript -->
                            </div>
                        </div>
                    </div>
                </div>
                
                <!-- Long Term Projection Tab -->
                <div id="longterm-tab" class="ultimate-tab-content" style="display: none;">
                    <div style="background: linear-gradient(135deg, #28a745, #20c997); color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                        <h4 style="margin: 0 0 15px 0; text-align: center;">📊 Long Term Analyst Projection</h4>
                        <div id="longterm-data">
                            <div style="text-align: center; color: rgba(255,255,255,0.8);">
                                <div style="font-size: 16px; margin: 20px 0;">⏳ Loading analyst data...</div>
                            </div>
                        </div>
                        
                        <!-- Mathematical Breakdown Section for Long Term -->
                        <div id="longterm-breakdown" style="background: rgba(0,0,0,0.3); border-radius: 8px; padding: 15px; margin: 15px 0; color: white; border: 1px solid rgba(255,255,255,0.2); display: none;">
                            <h5 style="margin: 0 0 15px 0; text-align: center; color: #ffffff; text-shadow: 1px 1px 2px rgba(0,0,0,0.5);">🧮 Mathematical Breakdown</h5>
                            <div id="longterm-breakdown-content" style="font-size: 14px; line-height: 1.6; color: #ffffff;">
                                <!-- Content will be populated by JavaScript -->
                            </div>
                        </div>
                    </div>
                </div>
                
                <!-- Action Buttons -->
                <div style="text-align: center; margin-top: 20px; border-top: 2px solid #eee; padding-top: 20px;">
                    <button onclick="showTradingViewChart('${symbol}')" 
                            style="background: #007bff; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 5px; cursor: pointer;">
                        📈 View Chart
                    </button>
                    <button onclick="window.open('https://finance.yahoo.com/quote/${symbol}/news', '_blank')" 
                            style="background: #28a745; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 5px; cursor: pointer;">
                        📰 Latest News
                    </button>
                    <button onclick="window.open('https://www.reddit.com/search/?q=${symbol}', '_blank')" 
                            style="background: #ff4757; color: white; border: none; padding: 10px 20px; border-radius: 5px; margin: 0 5px; cursor: pointer;">
                        🔍 Social Discussion
                    </button>
                </div>
            `;
            
            modal.appendChild(container);
            document.body.appendChild(modal);
        }
        
        // Tab switching functionality for ultimate modal
        function switchUltimateTab(tabId, emoji) {
            // Hide all tabs
            const tabs = document.querySelectorAll('.ultimate-tab-content');
            tabs.forEach(tab => tab.style.display = 'none');
            
            // Remove active class from all buttons
            const buttons = document.querySelectorAll('.ultimate-tab-btn');
            buttons.forEach(btn => {
                btn.style.borderBottom = '3px solid transparent';
                btn.style.color = '#666';
                btn.style.fontWeight = 'normal';
            });
            
            // Show selected tab
            document.getElementById(tabId).style.display = 'block';
            
            // Activate selected button
            const activeBtn = document.getElementById(tabId + '-btn');
            activeBtn.style.borderBottom = '3px solid #007bff';
            activeBtn.style.color = '#007bff';
            activeBtn.style.fontWeight = 'bold';
            
            // Load data for specific tabs when clicked
            if (tabId === 'recovery-tab') {
                console.log('DEBUG: Recovery tab clicked, calling loadRecoveryData()');
                loadRecoveryData();
            } else if (tabId === 'mediumterm-tab') {
                console.log('DEBUG: Medium-term tab clicked, calling loadMediumTermData()');
                loadMediumTermData();
            } else if (tabId === 'longterm-tab') {
                console.log('DEBUG: Long-term tab clicked, calling loadLongTermData()');
                loadLongTermData();
            }
        }
        
        // Load short-term recovery data
        function loadRecoveryData() {
            const recoveryData = document.getElementById('recovery-data');
            // Try multiple ways to get the symbol
            const h3Element = document.querySelector('#ultimate-modal h3');
            console.log('DEBUG: h3 element:', h3Element);
            console.log('DEBUG: h3 textContent:', h3Element?.textContent);
            
            let symbol = null;
            if (h3Element && h3Element.textContent) {
                // Try different regex patterns
                const patterns = [
                    /: ([A-Z]+)(?:\s|$)/,  // Standard pattern with word boundary
                    /Analysis:\s*([A-Z]+)/,  // "Analysis: SYMBOL"
                    /([A-Z]{2,5})$/,        // 2-5 uppercase letters at end
                    /([A-Z]+)/              // Any uppercase letters
                ];
                
                for (let pattern of patterns) {
                    const match = h3Element.textContent.match(pattern);
                    if (match && match[1]) {
                        symbol = match[1];
                        console.log('DEBUG: Found symbol using pattern:', pattern, 'Symbol:', symbol);
                        break;
                    }
                }
            }
            
            console.log('DEBUG: Final extracted symbol:', symbol);
            
            if (!symbol) {
                console.error('No symbol found in h3 element for recovery');
                recoveryData.innerHTML = `<div style="text-align: center; color: rgba(255,255,255,0.8);"><div style="font-size: 16px; margin: 20px 0;">❌ No symbol found</div></div>`;
                return;
            }
            
            // Show loading state
            recoveryData.innerHTML = `
                <div style="text-align: center; color: rgba(255,255,255,0.8);">
                    <div style="font-size: 16px; margin: 20px 0;">⏳ Loading recovery analysis...</div>
                </div>
            `;
            
            fetch2('/api/sophisticated-timeframe/' + symbol)
                .then(response => response.json())
                .then(data => {
                    const recovery = data;
                    
                    if (!recovery || !recovery.prediction || !recovery.prediction.recovery_score) {
                        recoveryData.innerHTML = `
                            <div style="text-align: center; color: rgba(255,255,255,0.8);">
                                <div style="font-size: 16px; margin: 20px 0;">📊 No recovery data available</div>
                                <div style="font-size: 14px; opacity: 0.7;">Unable to calculate recovery targets for this stock</div>
                            </div>
                        `;
                        return;
                    }
                    
                    // Get short-term targets from the sophisticated data
                    const shortTermData = recovery.sophisticated_analysis?.timeframe_predictions?.short_term || {};
                    const targets = Object.values(shortTermData);
                    const avgConfidence = targets.some(t => t.confidence === 'Very High' || t.confidence === 'High') ? 'High' : 
                                         targets.some(t => t.confidence === 'Medium') ? 'Medium' : 'Low';
                    
                    // Calculate overall score from short-term targets (consistent with medium/long-term)
                    const avgProbability = targets.length > 0 ? targets.reduce((sum, t) => sum + (t.probability || 0), 0) / targets.length : 0;
                    const heuristicNote = `<div style="background: rgba(255,255,255,0.14); border-left: 3px solid #ffffff; padding: 9px 12px; margin: 12px 0; font-size: 12px; color: #f8f9fa; line-height:1.5;">Each probability is the share of historical windows in which this stock actually reached that target within the horizon, measured from its own price history and shown with its sample size. Past frequency is not a forecast.</div>`;
                    
                    // Calculate final score with signal multipliers for header (matching medium/long-term pattern)
                    const enhancedSignals = recovery.sophisticated_analysis?.enhanced_signals || {};
                    let shortTermSignalMultiplier = 1.0;
                    
                    if (enhancedSignals.volume_surge?.surge_detected) {
                        shortTermSignalMultiplier *= enhancedSignals.volume_surge.surge_multiplier;
                    }
                    if (enhancedSignals.rsi_reversion?.oversold_detected) {
                        shortTermSignalMultiplier *= enhancedSignals.rsi_reversion.reversion_multiplier;
                    }
                    if (enhancedSignals.economic_regime?.regime) {
                        const regimeBoost = enhancedSignals.economic_regime.short_term_multiplier || 1.0;
                        shortTermSignalMultiplier *= regimeBoost;
                    }
                    if (enhancedSignals.money_flow_index?.oversold_detected) {
                        shortTermSignalMultiplier *= enhancedSignals.money_flow_index.recovery_multiplier;
                    }
                    if (enhancedSignals.macd_histogram?.momentum_shift) {
                        shortTermSignalMultiplier *= enhancedSignals.macd_histogram.recovery_multiplier;
                    }
                    if (enhancedSignals.bollinger_squeeze && enhancedSignals.bollinger_squeeze.signal_type !== 'neutral') {
                        shortTermSignalMultiplier *= enhancedSignals.bollinger_squeeze.recovery_multiplier;
                    }
                    if (enhancedSignals.put_call_ratio?.extreme_sentiment) {
                        shortTermSignalMultiplier *= enhancedSignals.put_call_ratio.recovery_multiplier;
                    }
                    if (enhancedSignals.short_interest?.squeeze_potential) {
                        shortTermSignalMultiplier *= enhancedSignals.short_interest.recovery_multiplier;
                    }
                    
                    const shortTermFinalScoreSummary = (function(list){
                        const measured = list.filter(t => t && t.probability_available === true);
                        if (!measured.length) return { text: '\u2014', sub: 'no measurable targets' };
                        const best = measured.reduce((a, b) => (b.probability > a.probability ? b : a));
                        return { text: Number(best.probability).toFixed(0) + '%',
                                 sub: 'best measured target (' + measured.length + ' measured)' };
                    })(Object.values(targets || {}));
                    
                    // Header with confidence levels matching other sections
                    recoveryData.innerHTML = `
                        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; text-align: center;">
                            <div>
                                <div style="font-size: 28px; font-weight: bold;">${shortTermFinalScoreSummary.text}</div>
                                <div style="font-size: 13px; opacity: 0.95;">${shortTermFinalScoreSummary.sub}</div>
                            </div>
                            <div>
                                <div style="font-size: 18px; font-weight: bold;">${avgConfidence}</div>
                                <div style="font-size: 14px; opacity: 0.9;">Confidence</div>
                            </div>
                            <div>
                                <div style="font-size: 18px; font-weight: bold;">1-7 days</div>
                                <div style="font-size: 14px; opacity: 0.9;">Time Frame</div>
                            </div>
                        </div>
                        <div style="text-align: center; margin-top: 15px; font-size: 16px; line-height: 1.6;">
                            Short-term recovery analysis based on technical indicators and market momentum
                        </div>
                        ${heuristicNote}
                        <div style="display: grid; gap: 15px; margin-top: 20px;">
                    `;
                    
                    // Add individual targets like med/long-term sections
                    if (Object.keys(shortTermData).length > 0) {
                        let shortTermTargets = '';
                        Object.entries(shortTermData).forEach(([targetName, target]) => {
                            const confidence = target.confidence || 'Low';
                            const probability = Math.round(target.probability || 0);
                            const confidenceColor = confidence === 'Very High' || confidence === 'High' ? '#28a745' : 
                                                  confidence === 'Medium' ? '#ffc107' : '#dc3545';
                            
                            shortTermTargets += `
                                <div style="background: rgba(0,0,0,0.30); border-radius: 8px; padding: 15px; border-left: 4px solid ${confidenceColor}; box-shadow: 0 1px 3px rgba(0,0,0,0.25);">
                                    <div style="display: flex; justify-content: between; align-items: center; margin-bottom: 8px;">
                                        <div style="font-size: 16px; font-weight: bold; color: #ffffff;">
                                            ${target.description || targetName}
                                        </div>
                                        <div style="font-size: 13px; text-align: right;">
                                            ${probabilityBadge(target)}
                                        </div>
                                    </div>
                                    <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-top: 10px; text-align: center;">
                                        <div>
                                            <div style="font-size: 18px; font-weight: bold; color: #ffffff;">$${target.target_price || 'N/A'}</div>
                                            <div style="font-size: 12px; color: #f1f3f5;">Target Price</div>
                                        </div>
                                        <div>
                                            <div style="font-size: 18px; font-weight: bold; color: #eaffef; text-shadow: 0 1px 2px rgba(0,0,0,0.45);">+${target.upside_percent || 0}%</div>
                                            <div style="font-size: 12px; color: #f1f3f5;">Upside</div>
                                        </div>
                                        <div>
                                            <div style="font-size: 18px; font-weight: bold; color: #fff6d8; text-shadow: 0 1px 2px rgba(0,0,0,0.45);">${target.timeframe || '1-7 days'}</div>
                                            <div style="font-size: 12px; color: #f1f3f5;">Timeframe</div>
                                        </div>
                                    </div>
                                </div>
                            `;
                        });
                        
                        recoveryData.innerHTML += shortTermTargets + '</div>';
                    } else {
                        recoveryData.innerHTML += '</div>';
                    }
                    
                    // Add mathematical breakdown
                    const breakdownElement = document.getElementById('recovery-breakdown');
                    const breakdownContent = document.getElementById('recovery-breakdown-content');
                    if (breakdownElement && breakdownContent && Object.keys(shortTermData).length > 0) {
                        
                        // Check for enhanced signals
                        const enhancedSignals = recovery.sophisticated_analysis?.enhanced_signals || {};
                        let signalsDisplay = '';
                        
                        // Volume Surge Signal
                        if (enhancedSignals.volume_surge?.surge_detected) {
                            const volumeData = enhancedSignals.volume_surge;
                            signalsDisplay += `
                                <div style="margin-bottom: 8px; padding: 8px; background: rgba(0,255,0,0.1); border-radius: 4px; border-left: 3px solid #28a745;">
                                    <strong style="color: #28a745;">📈 Volume Surge:</strong> ${volumeData.volume_ratio}x average
                                    <span style="color: rgba(255,255,255,0.8); font-size: 12px;">(volume surge raises this score by ${Math.round((volumeData.surge_multiplier - 1) * 100)}%)</span>
                                </div>`;
                        }
                        
                        // RSI Oversold Signal
                        if (enhancedSignals.rsi_reversion?.oversold) {
                            const rsiData = enhancedSignals.rsi_reversion;
                            signalsDisplay += `
                                <div style="margin-bottom: 8px; padding: 8px; background: rgba(255,193,7,0.1); border-radius: 4px; border-left: 3px solid #ffc107;">
                                    <strong style="color: #ffc107;">🎯 RSI Oversold:</strong> ${rsiData.rsi} (${rsiData.signal_strength})
                                    <span style="color: rgba(255,255,255,0.8); font-size: 12px;">(mean reversion likely)</span>
                                </div>`;
                        }
                        
                        // Economic Regime Signal  
                        if (enhancedSignals.economic_regime?.regime) {
                            const regimeData = enhancedSignals.economic_regime;
                            const regimeBoost = regimeData.regime_multipliers?.short || 1.0;
                            const regimeColor = regimeBoost > 1.2 ? '#28a745' : regimeBoost > 1.0 ? '#ffc107' : '#dc3545';
                            signalsDisplay += `
                                <div style="margin-bottom: 8px; padding: 8px; background: rgba(128,128,128,0.1); border-radius: 4px; border-left: 3px solid ${regimeColor};">
                                    <strong style="color: ${regimeColor};">🌡️ Market Regime:</strong> VIX ${regimeData.vix_level} (${regimeData.regime.replace('_', ' ')})
                                    <span style="color: rgba(255,255,255,0.8); font-size: 12px;">(${regimeData.recovery_environment})</span>
                                </div>`;
                        }

                        // Build detailed mathematical breakdown for short-term
                        const shortTermData = recovery.sophisticated_analysis?.timeframe_predictions?.short_term || {};
                        let targetsBreakdown = '';
                        let totalWeightedScore = 0;
                        let totalWeight = 0;
                        
                        // Show each target's calculation
                        Object.entries(shortTermData).forEach(([targetName, target]) => {
                            const displayName = targetName.replace('_', ' ').replace(/\b\w/g, l => l.toUpperCase());
                            const weight = targetName === 'previous_close' ? 1.0 : 
                                          targetName === '5day_high' ? 0.9 : 
                                          targetName === '10day_ma' ? 0.8 : 0.7;
                            
                            totalWeightedScore += (target.probability || 0) * weight;
                            totalWeight += weight;
                            
                            targetsBreakdown += `
                                <div style="margin: 6px 0; padding: 6px; background: rgba(0,0,0,0.26); border-radius: 4px;">
                                    <div style="display: flex; justify-content: space-between; align-items: center;">
                                        <span style="color: #ffffff; font-weight: 600;">${displayName}:</span>
                                        <span>${probabilityBadge(target)}</span>
                                    </div>
                                    <div style="font-size: 11px; color: #dee2e6;">
                                        Target: $${target.target_price} (+${Math.round((target.upside_percent || 0) * 10) / 10}%)${target.median_days_to_hit ? ' • median ' + target.median_days_to_hit + 'd to reach' : ''}
                                    </div>
                                </div>`;
                        });
                        
                        const baseScore = totalWeight > 0 ? totalWeightedScore / totalWeight : 0;
                        
                        // Calculate signal effects for short-term
                        let signalEffects = '';
                        let signalMultiplier = 1.0;
                        
                        if (enhancedSignals.volume_surge?.surge_detected) {
                            const volumeBoost = enhancedSignals.volume_surge.surge_multiplier;
                            signalMultiplier *= volumeBoost;
                            signalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #28a745;">
                                    ✓ Volume Surge: ${volumeBoost.toFixed(2)}x multiplier (${Math.round((volumeBoost - 1) * 100)}% boost)
                                </div>`;
                        }
                        
                        if (enhancedSignals.rsi_reversion?.oversold_detected) {
                            const rsiBoost = enhancedSignals.rsi_reversion.reversion_multiplier;
                            signalMultiplier *= rsiBoost;
                            signalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #ffc107;">
                                    ✓ RSI Oversold: ${rsiBoost.toFixed(2)}x multiplier (RSI ${enhancedSignals.rsi_reversion.rsi_value})
                                </div>`;
                        }
                        
                        if (enhancedSignals.economic_regime?.regime) {
                            const regimeBoost = enhancedSignals.economic_regime.short_term_multiplier || 1.0;
                            signalMultiplier *= regimeBoost;
                            const regimeColor = regimeBoost > 1.1 ? '#28a745' : regimeBoost > 1.0 ? '#ffc107' : '#dc3545';
                            signalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: ${regimeColor};">
                                    ✓ Market Regime: ${regimeBoost.toFixed(2)}x multiplier (VIX ${enhancedSignals.economic_regime.vix_level})
                                </div>`;
                        }
                        
                        // NEW HIGH-ACCURACY SIGNALS
                        if (enhancedSignals.money_flow_index?.oversold_detected) {
                            const mfiBoost = enhancedSignals.money_flow_index.recovery_multiplier;
                            signalMultiplier *= mfiBoost;
                            signalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #17a2b8;">
                                    ✓ Money Flow Index: ${mfiBoost.toFixed(2)}x multiplier (MFI ${enhancedSignals.money_flow_index.mfi_value} - volume confirmed)
                                </div>`;
                        }
                        
                        if (enhancedSignals.macd_histogram?.momentum_shift) {
                            const macdBoost = enhancedSignals.macd_histogram.recovery_multiplier;
                            signalMultiplier *= macdBoost;
                            signalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #dc3545;">
                                    ✓ MACD Histogram: ${macdBoost.toFixed(2)}x multiplier (${enhancedSignals.macd_histogram.signal_type.replace('_', ' ')})
                                </div>`;
                        }
                        
                        if (enhancedSignals.bollinger_squeeze && enhancedSignals.bollinger_squeeze.signal_type !== 'neutral') {
                            const bbBoost = enhancedSignals.bollinger_squeeze.recovery_multiplier;
                            signalMultiplier *= bbBoost;
                            signalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #6610f2;">
                                    ✓ Bollinger Bands: ${bbBoost.toFixed(2)}x multiplier (${enhancedSignals.bollinger_squeeze.signal_type.replace('_', ' ')})
                                </div>`;
                        }
                        
                        if (enhancedSignals.put_call_ratio?.extreme_sentiment) {
                            const pcBoost = enhancedSignals.put_call_ratio.recovery_multiplier;
                            signalMultiplier *= pcBoost;
                            signalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #fd7e14;">
                                    ✓ Put/Call Ratio: ${pcBoost.toFixed(2)}x multiplier (P/C ${enhancedSignals.put_call_ratio.pc_ratio} - contrarian)
                                </div>`;
                        }
                        
                        if (enhancedSignals.short_interest?.squeeze_potential) {
                            const siBoost = enhancedSignals.short_interest.recovery_multiplier;
                            signalMultiplier *= siBoost;
                            signalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #e83e8c;">
                                    ✓ Short Squeeze: ${siBoost.toFixed(2)}x multiplier (${enhancedSignals.short_interest.short_percent}% short interest)
                                </div>`;
                        }
                        
                        const finalScore = Math.min(95, baseScore * signalMultiplier);
                        
                        breakdownContent.innerHTML = `
                            <div style="margin-bottom: 12px;">
                                <strong style="color: #ffffff; font-size: 14px;">Measured hit rates</strong>
                                <div style="font-size: 12px; color: #f1f3f5; margin-top: 6px; line-height: 1.55;">
                                    Each figure is how often this stock actually reached that target
                                    within the horizon, counted over its own price history.
                                </div>
                                ${targetsBreakdown}
                            </div>

                            <div style="padding: 10px; background: rgba(0,0,0,0.28); border-radius: 6px; margin: 10px 0;">
                                <div style="color: #ffffff; font-weight: 600; margin-bottom: 4px;">Why there is no combined score</div>
                                <div style="font-size: 12px; color: #f1f3f5; line-height: 1.55;">
                                    Targets differ in size, so their hit rates are not comparable and
                                    averaging them would not describe anything real. This panel
                                    previously showed a weighted score multiplied by a "signal
                                    multiplier" and capped at 95, which is why every target displayed
                                    an identical 95% and the arithmetic read 95% &times; 1.88 = 95%.
                                    Each target now stands on its own measured frequency.
                                </div>
                            </div>
                        `;
                        breakdownElement.style.display = 'block';
                    }
                })
                .catch(error => {
                    console.error('Recovery data error:', error);
                    recoveryData.innerHTML = `
                        <div style="text-align: center; color: rgba(255,255,255,0.8);">
                            <div style="font-size: 16px; margin: 20px 0;">⚠️ Error loading recovery data</div>
                        </div>
                    `;
                });
        }
        
        // Load medium-term recovery data
        function loadMediumTermData() {
            const mediumtermData = document.getElementById('mediumterm-data');
            
            // Get the recovery data that was already loaded in the ultimate modal
            const h3Element = document.querySelector('#ultimate-modal h3');
            let symbol = null;
            if (h3Element && h3Element.textContent) {
                const patterns = [
                    /: ([A-Z]+)(?:\s|$)/,
                    /Analysis:\s*([A-Z]+)/,
                    /([A-Z]{2,5})$/,
                    /([A-Z]+)/
                ];
                
                for (let pattern of patterns) {
                    const match = h3Element.textContent.match(pattern);
                    if (match && match[1]) {
                        symbol = match[1];
                        break;
                    }
                }
            }
            
            if (!symbol) {
                mediumtermData.innerHTML = `<div style="text-align: center; color: rgba(255,255,255,0.8);"><div style="font-size: 16px; margin: 20px 0;">❌ No symbol found</div></div>`;
                return;
            }
            
            // Show loading state
            mediumtermData.innerHTML = `
                <div style="text-align: center; color: rgba(255,255,255,0.8);">
                    <div style="font-size: 16px; margin: 20px 0;">⏳ Loading medium-term analysis...</div>
                </div>
            `;
            
            // Use the recovery data already loaded or fetch it
            const recovery = window.currentRecoveryData;
            let dataPromise;
            
            if (recovery && recovery.sophisticated_analysis) {
                // Use already loaded data
                dataPromise = Promise.resolve(recovery.sophisticated_analysis);
            } else {
                // Fetch the data
                dataPromise = fetch2('/api/sophisticated-timeframe/' + symbol)
                    .then(response => response.json())
                    .then(data => data.sophisticated_analysis);
            }
            
            dataPromise.then(sophisticatedAnalysis => {
                const mediumTermPredictions = sophisticatedAnalysis?.timeframe_predictions?.medium_term || {};
                const mediumTargets = sophisticatedAnalysis?.medium_targets || {};
                    
                    if (Object.keys(mediumTermPredictions).length === 0) {
                        mediumtermData.innerHTML = `
                            <div style="text-align: center; color: rgba(255,255,255,0.8);">
                                <div style="font-size: 16px; margin: 20px 0;">📊 No medium-term targets available</div>
                                <div style="font-size: 14px; opacity: 0.7;">Stock may not have suitable 1-4 week recovery targets</div>
                            </div>
                        `;
                        return;
                    }
                    
                    // Calculate overall confidence and score for header
                    const predictions = Object.values(mediumTermPredictions);
                    const avgProbability = predictions.reduce((sum, p) => sum + (p.probability || 0), 0) / predictions.length;
                    const heuristicNote = `<div style="background: rgba(255,255,255,0.14); border-left: 3px solid #ffffff; padding: 9px 12px; margin: 12px 0; font-size: 12px; color: #f8f9fa; line-height:1.5;">Each probability is the share of historical windows in which this stock actually reached that target within the horizon, measured from its own price history and shown with its sample size. Past frequency is not a forecast.</div>`;
                    const avgConfidence = predictions.some(p => p.confidence === 'Very High' || p.confidence === 'High') ? 'High' : 
                                         predictions.some(p => p.confidence === 'Medium') ? 'Medium' : 'Low';
                    
                    // Calculate final score with signal multipliers for header (matching mathematical breakdown)
                    const recoveryData = window.currentRecoveryData || {};
                    const enhancedSignals = recoveryData.sophisticated_analysis?.enhanced_signals || {};
                    let mediumSignalMultiplier = 1.0;
                    
                    if (enhancedSignals.volume_surge?.surge_detected) {
                        const volumeBoost = 1.0 + ((enhancedSignals.volume_surge.surge_multiplier - 1.0) * 0.5);
                        mediumSignalMultiplier *= volumeBoost;
                    }
                    if (enhancedSignals.rsi_reversion?.oversold_detected) {
                        const rsiBoost = 1.0 + ((enhancedSignals.rsi_reversion.reversion_multiplier - 1.0) * 0.7);
                        mediumSignalMultiplier *= rsiBoost;
                    }
                    if (enhancedSignals.economic_regime?.regime) {
                        const regimeBoost = enhancedSignals.economic_regime.medium_term_multiplier || 1.0;
                        mediumSignalMultiplier *= regimeBoost;
                    }
                    if (enhancedSignals.money_flow_index?.oversold_detected) {
                        const mfiBoost = 1.0 + ((enhancedSignals.money_flow_index.recovery_multiplier - 1.0) * 0.5);
                        mediumSignalMultiplier *= mfiBoost;
                    }
                    if (enhancedSignals.macd_histogram?.momentum_shift) {
                        const macdBoost = 1.0 + ((enhancedSignals.macd_histogram.recovery_multiplier - 1.0) * 0.7);
                        mediumSignalMultiplier *= macdBoost;
                    }
                    if (enhancedSignals.bollinger_squeeze && enhancedSignals.bollinger_squeeze.signal_type !== 'neutral') {
                        const bbBoost = 1.0 + ((enhancedSignals.bollinger_squeeze.recovery_multiplier - 1.0) * 0.6);
                        mediumSignalMultiplier *= bbBoost;
                    }
                    if (enhancedSignals.put_call_ratio?.extreme_sentiment) {
                        const pcBoost = 1.0 + ((enhancedSignals.put_call_ratio.recovery_multiplier - 1.0) * 0.4);
                        mediumSignalMultiplier *= pcBoost;
                    }
                    if (enhancedSignals.short_interest?.squeeze_potential) {
                        const siBoost = 1.0 + ((enhancedSignals.short_interest.recovery_multiplier - 1.0) * 0.3);
                        mediumSignalMultiplier *= siBoost;
                    }
                    
                    const mediumFinalScoreSummary = (function(list){
                        const measured = list.filter(t => t && t.probability_available === true);
                        if (!measured.length) return { text: '\u2014', sub: 'no measurable targets' };
                        const best = measured.reduce((a, b) => (b.probability > a.probability ? b : a));
                        return { text: Number(best.probability).toFixed(0) + '%',
                                 sub: 'best measured target (' + measured.length + ' measured)' };
                    })(Object.values(predictions || {}));
                    
                    // Header with confidence levels matching other sections
                    mediumtermData.innerHTML = `
                        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; text-align: center;">
                            <div>
                                <div style="font-size: 28px; font-weight: bold;">${mediumFinalScoreSummary.text}</div>
                                <div style="font-size: 13px; opacity: 0.95;">${mediumFinalScoreSummary.sub}</div>
                            </div>
                            <div>
                                <div style="font-size: 18px; font-weight: bold;">${avgConfidence}</div>
                                <div style="font-size: 14px; opacity: 0.9;">Confidence</div>
                            </div>
                            <div>
                                <div style="font-size: 18px; font-weight: bold;">1-4 weeks</div>
                                <div style="font-size: 14px; opacity: 0.9;">Time Frame</div>
                            </div>
                        </div>
                        <div style="text-align: center; margin-top: 15px; font-size: 16px; line-height: 1.6;">
                            Medium-term recovery analysis based on technical patterns and market conditions
                        </div>
                        ${heuristicNote}
                        <div style="display: grid; gap: 15px; margin-top: 20px;">
                    `;
                    
                    let mediumTermTargets = '';
                    Object.entries(mediumTermPredictions).forEach(([targetName, prediction]) => {
                        const confidence = prediction.confidence || 'Low';
                        const probability = Math.round(prediction.probability || 0);
                        const confidenceColor = confidence === 'High' ? '#28a745' : 
                                              confidence === 'Medium' ? '#ffc107' : '#dc3545';
                        
                        mediumTermTargets += `
                            <div style="background: rgba(0,0,0,0.30); border-radius: 8px; padding: 15px; border-left: 4px solid ${confidenceColor}; box-shadow: 0 1px 3px rgba(0,0,0,0.25);">
                                <div style="display: flex; justify-content: between; align-items: center; margin-bottom: 8px;">
                                    <div style="font-size: 16px; font-weight: bold; color: #ffffff;">
                                        ${prediction.description || targetName}
                                    </div>
                                    <div style="font-size: 13px; text-align: right;">
                                        ${probabilityBadge(prediction)}
                                    </div>
                                </div>
                                <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-top: 10px; text-align: center;">
                                    <div>
                                        <div style="font-size: 18px; font-weight: bold; color: #ffffff;">$${prediction.target_price}</div>
                                        <div style="font-size: 12px; color: #f1f3f5;">Target Price</div>
                                    </div>
                                    <div>
                                        <div style="font-size: 18px; font-weight: bold; color: #28a745;">+${prediction.upside_percent}%</div>
                                        <div style="font-size: 12px; color: #f1f3f5;">Upside</div>
                                    </div>
                                    <div>
                                        <div style="font-size: 18px; font-weight: bold; color: #ffc107;">${prediction.timeframe}</div>
                                        <div style="font-size: 12px; color: #f1f3f5;">Timeframe</div>
                                    </div>
                                </div>
                            </div>
                        `;
                    });
                    
                    mediumtermData.innerHTML += mediumTermTargets + '</div>';
                    
                    // Add mathematical breakdown
                    const breakdownElement = document.getElementById('mediumterm-breakdown');
                    const breakdownContent = document.getElementById('mediumterm-breakdown-content');
                    if (breakdownElement && breakdownContent) {
                        const totalTargets = predictions.length;
                        const highConfTargets = predictions.filter(p => p.confidence === 'Very High' || p.confidence === 'High').length;
                        const avgUpside = predictions.reduce((sum, p) => sum + parseFloat(p.upside_percent || 0), 0) / totalTargets;
                        
                        // Build detailed mathematical breakdown for medium-term using window data
                        const recoveryData = window.currentRecoveryData || {};
                        const mediumTermData = recoveryData.sophisticated_analysis?.timeframe_predictions?.medium_term || {};
                        const enhancedSignals = recoveryData.sophisticated_analysis?.enhanced_signals || {};
                        let mediumTargetsBreakdown = '';
                        let mediumTotalWeightedScore = 0;
                        let mediumTotalWeight = 0;
                        
                        // Show each medium-term target's calculation
                        Object.entries(mediumTermData).forEach(([targetName, target]) => {
                            const displayName = targetName.replace('_', ' ').replace(/\b\w/g, l => l.toUpperCase());
                            const weight = targetName === '20day_ma' ? 1.0 : 
                                          targetName === 'support_bounce' ? 0.9 : 
                                          targetName === 'fair_value' ? 0.8 : 0.7;
                            
                            mediumTotalWeightedScore += (target.probability || 0) * weight;
                            mediumTotalWeight += weight;
                            
                            mediumTargetsBreakdown += `
                                <div style="margin: 6px 0; padding: 6px; background: rgba(0,0,0,0.26); border-radius: 4px;">
                                    <div style="display: flex; justify-content: space-between; align-items: center;">
                                        <span style="color: #ffffff; font-weight: 500;">${displayName}:</span>
                                        <span>${probabilityBadge(target)}</span>
                                    </div>
                                    <div style="font-size: 11px; color: rgba(255,255,255,0.7);">
                                        Target: $${target.target_price} (+${Math.round((target.upside_percent || 0) * 10) / 10}%)${target.median_days_to_hit ? ' • median ' + target.median_days_to_hit + 'd to reach' : ''}
                                    </div>
                                </div>`;
                        });
                        
                        const mediumBaseScore = mediumTotalWeight > 0 ? mediumTotalWeightedScore / mediumTotalWeight : avgProbability;
                        
                        // Calculate medium-term signal effects (reduced impact)
                        let mediumSignalEffects = '';
                        let mediumSignalMultiplier = 1.0;
                        
                        if (enhancedSignals.volume_surge?.surge_detected) {
                            const volumeBoost = 1.0 + ((enhancedSignals.volume_surge.surge_multiplier - 1.0) * 0.5); // 50% impact for medium-term
                            mediumSignalMultiplier *= volumeBoost;
                            mediumSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #28a745;">
                                    ✓ Volume Surge: ${volumeBoost.toFixed(2)}x multiplier (50% medium-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.rsi_reversion?.oversold_detected) {
                            const rsiBoost = 1.0 + ((enhancedSignals.rsi_reversion.reversion_multiplier - 1.0) * 0.7); // 70% impact for medium-term
                            mediumSignalMultiplier *= rsiBoost;
                            mediumSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #ffc107;">
                                    ✓ RSI Oversold: ${rsiBoost.toFixed(2)}x multiplier (70% medium-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.economic_regime?.regime) {
                            const regimeBoost = enhancedSignals.economic_regime.medium_term_multiplier || 1.0;
                            mediumSignalMultiplier *= regimeBoost;
                            const regimeColor = regimeBoost > 1.1 ? '#28a745' : regimeBoost > 1.0 ? '#ffc107' : '#dc3545';
                            mediumSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: ${regimeColor};">
                                    ✓ Market Regime: ${regimeBoost.toFixed(2)}x multiplier (VIX ${enhancedSignals.economic_regime.vix_level})
                                </div>`;
                        }
                        
                        // NEW HIGH-ACCURACY INDICATORS (Medium-term with reduced impact)
                        if (enhancedSignals.money_flow_index?.oversold_detected) {
                            const mfiBoost = 1.0 + ((enhancedSignals.money_flow_index.recovery_multiplier - 1.0) * 0.5); // 50% impact
                            mediumSignalMultiplier *= mfiBoost;
                            mediumSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #17a2b8;">
                                    ✓ Money Flow Index: ${mfiBoost.toFixed(2)}x multiplier (50% medium-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.macd_histogram?.momentum_shift) {
                            const macdBoost = 1.0 + ((enhancedSignals.macd_histogram.recovery_multiplier - 1.0) * 0.7); // 70% impact
                            mediumSignalMultiplier *= macdBoost;
                            mediumSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #dc3545;">
                                    ✓ MACD Histogram: ${macdBoost.toFixed(2)}x multiplier (70% medium-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.bollinger_squeeze && enhancedSignals.bollinger_squeeze.signal_type !== 'neutral') {
                            const bbBoost = 1.0 + ((enhancedSignals.bollinger_squeeze.recovery_multiplier - 1.0) * 0.6); // 60% impact
                            mediumSignalMultiplier *= bbBoost;
                            mediumSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #6610f2;">
                                    ✓ Bollinger Bands: ${bbBoost.toFixed(2)}x multiplier (60% medium-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.put_call_ratio?.extreme_sentiment) {
                            const pcBoost = 1.0 + ((enhancedSignals.put_call_ratio.recovery_multiplier - 1.0) * 0.4); // 40% impact
                            mediumSignalMultiplier *= pcBoost;
                            mediumSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #fd7e14;">
                                    ✓ Put/Call Ratio: ${pcBoost.toFixed(2)}x multiplier (40% medium-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.short_interest?.squeeze_potential) {
                            const siBoost = 1.0 + ((enhancedSignals.short_interest.recovery_multiplier - 1.0) * 0.3); // 30% impact
                            mediumSignalMultiplier *= siBoost;
                            mediumSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #e83e8c;">
                                    ✓ Short Squeeze: ${siBoost.toFixed(2)}x multiplier (30% medium-term impact)
                                </div>`;
                        }
                        
                        const mediumFinalScore = Math.min(90, mediumBaseScore * mediumSignalMultiplier);
                        
                        breakdownContent.innerHTML = `
                            <div style="margin-bottom: 12px;">
                                <strong style="color: #ffffff; font-size: 14px;">Measured hit rates</strong>
                                <div style="font-size: 12px; color: #f1f3f5; margin-top: 6px; line-height: 1.55;">
                                    Each figure is how often this stock actually reached that target
                                    within the horizon, counted over its own price history.
                                </div>
                                ${mediumTargetsBreakdown}
                            </div>

                            <div style="padding: 10px; background: rgba(0,0,0,0.28); border-radius: 6px; margin: 10px 0;">
                                <div style="color: #ffffff; font-weight: 600; margin-bottom: 4px;">Why there is no combined score</div>
                                <div style="font-size: 12px; color: #f1f3f5; line-height: 1.55;">
                                    Targets differ in size, so their hit rates are not comparable and
                                    averaging them would not describe anything real. This panel
                                    previously showed a weighted score multiplied by a "signal
                                    multiplier" and capped at 95, which is why every target displayed
                                    an identical 95% and the arithmetic read 95% &times; 1.88 = 95%.
                                    Each target now stands on its own measured frequency.
                                </div>
                            </div>
                        `;
                        breakdownElement.style.display = 'block';
                    }
                })
                .catch(error => {
                    console.error('Medium-term data error:', error);
                    mediumtermData.innerHTML = `
                        <div style="text-align: center; color: rgba(255,255,255,0.8);">
                            <div style="font-size: 16px; margin: 20px 0;">⚠️ Error loading medium-term data</div>
                        </div>
                    `;
                });
        }
        
        // Load long-term analyst data
        function loadLongTermData() {
            const longtermData = document.getElementById('longterm-data');
            
            // Get the recovery data that was already loaded in the ultimate modal
            const h3Element = document.querySelector('#ultimate-modal h3');
            let symbol = null;
            if (h3Element && h3Element.textContent) {
                const patterns = [
                    /: ([A-Z]+)(?:\s|$)/,
                    /Analysis:\s*([A-Z]+)/,
                    /([A-Z]{2,5})$/,
                    /([A-Z]+)/
                ];
                
                for (let pattern of patterns) {
                    const match = h3Element.textContent.match(pattern);
                    if (match && match[1]) {
                        symbol = match[1];
                        break;
                    }
                }
            }
            
            if (!symbol) {
                longtermData.innerHTML = `<div style="text-align: center; color: rgba(255,255,255,0.8);"><div style="font-size: 16px; margin: 20px 0;">❌ No symbol found</div></div>`;
                return;
            }
            
            // Show loading state
            longtermData.innerHTML = `
                <div style="text-align: center; color: rgba(255,255,255,0.8);">
                    <div style="font-size: 16px; margin: 20px 0;">⏳ Loading analyst projections...</div>
                </div>
            `;
            
            // Use the recovery data already loaded or fetch it
            const recovery = window.currentRecoveryData;
            let dataPromise;
            
            if (recovery && recovery.sophisticated_analysis) {
                // Use already loaded data
                dataPromise = Promise.resolve(recovery.sophisticated_analysis);
            } else {
                // Fetch the data
                dataPromise = fetch2('/api/sophisticated-timeframe/' + symbol)
                    .then(response => response.json())
                    .then(data => data.sophisticated_analysis);
            }
            
            dataPromise.then(sophisticatedAnalysis => {
                const longTermPredictions = sophisticatedAnalysis?.timeframe_predictions?.long_term || {};
                    
                    if (Object.keys(longTermPredictions).length === 0) {
                        longtermData.innerHTML = `
                            <div style="text-align: center; color: rgba(255,255,255,0.8);">
                                <div style="font-size: 16px; margin: 20px 0;">📊 No long-term analyst targets available</div>
                                <div style="font-size: 14px; opacity: 0.7;">Limited analyst coverage for this stock</div>
                            </div>
                        `;
                        return;
                    }
                    
                    // Calculate overall confidence and score for header
                    const predictions = Object.values(longTermPredictions);
                    const avgProbability = predictions.reduce((sum, p) => sum + (p.probability || 0), 0) / predictions.length;
                    const heuristicNote = `<div style="background: rgba(255,255,255,0.14); border-left: 3px solid #ffffff; padding: 9px 12px; margin: 12px 0; font-size: 12px; color: #f8f9fa; line-height:1.5;">Each probability is the share of historical windows in which this stock actually reached that target within the horizon, measured from its own price history and shown with its sample size. Past frequency is not a forecast.</div>`;
                    const avgConfidence = predictions.some(p => p.confidence === 'Very High' || p.confidence === 'High') ? 'High' : 
                                         predictions.some(p => p.confidence === 'Medium') ? 'Medium' : 'Low';
                    
                    // Calculate final score with signal multipliers for header (matching mathematical breakdown)
                    const recoveryData = window.currentRecoveryData || {};
                    const enhancedSignals = recoveryData.sophisticated_analysis?.enhanced_signals || {};
                    let longTermSignalMultiplier = 1.0;
                    
                    if (enhancedSignals.volume_surge?.surge_detected) {
                        const volumeBoost = 1.0 + ((enhancedSignals.volume_surge.surge_multiplier - 1.0) * 0.25);
                        longTermSignalMultiplier *= volumeBoost;
                    }
                    if (enhancedSignals.rsi_reversion?.oversold_detected) {
                        const rsiBoost = 1.0 + ((enhancedSignals.rsi_reversion.reversion_multiplier - 1.0) * 0.4);
                        longTermSignalMultiplier *= rsiBoost;
                    }
                    if (enhancedSignals.economic_regime?.regime) {
                        const regimeBoost = enhancedSignals.economic_regime.long_term_multiplier || 1.0;
                        longTermSignalMultiplier *= regimeBoost;
                    }
                    if (enhancedSignals.money_flow_index?.oversold_detected) {
                        const mfiBoost = 1.0 + ((enhancedSignals.money_flow_index.recovery_multiplier - 1.0) * 0.25);
                        longTermSignalMultiplier *= mfiBoost;
                    }
                    if (enhancedSignals.macd_histogram?.momentum_shift) {
                        const macdBoost = 1.0 + ((enhancedSignals.macd_histogram.recovery_multiplier - 1.0) * 0.4);
                        longTermSignalMultiplier *= macdBoost;
                    }
                    if (enhancedSignals.bollinger_squeeze && enhancedSignals.bollinger_squeeze.signal_type !== 'neutral') {
                        const bbBoost = 1.0 + ((enhancedSignals.bollinger_squeeze.recovery_multiplier - 1.0) * 0.3);
                        longTermSignalMultiplier *= bbBoost;
                    }
                    if (enhancedSignals.put_call_ratio?.extreme_sentiment) {
                        const pcBoost = 1.0 + ((enhancedSignals.put_call_ratio.recovery_multiplier - 1.0) * 0.2);
                        longTermSignalMultiplier *= pcBoost;
                    }
                    if (enhancedSignals.short_interest?.squeeze_potential) {
                        const siBoost = 1.0 + ((enhancedSignals.short_interest.recovery_multiplier - 1.0) * 0.15);
                        longTermSignalMultiplier *= siBoost;
                    }
                    
                    const longTermFinalScoreSummary = (function(list){
                        const measured = list.filter(t => t && t.probability_available === true);
                        if (!measured.length) return { text: '\u2014', sub: 'no measurable targets' };
                        const best = measured.reduce((a, b) => (b.probability > a.probability ? b : a));
                        return { text: Number(best.probability).toFixed(0) + '%',
                                 sub: 'best measured target (' + measured.length + ' measured)' };
                    })(Object.values(predictions || {}));
                    
                    // Header with confidence levels matching other sections
                    longtermData.innerHTML = `
                        <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; text-align: center;">
                            <div>
                                <div style="font-size: 28px; font-weight: bold;">${longTermFinalScoreSummary.text}</div>
                                <div style="font-size: 13px; opacity: 0.95;">${longTermFinalScoreSummary.sub}</div>
                            </div>
                            <div>
                                <div style="font-size: 18px; font-weight: bold;">${avgConfidence}</div>
                                <div style="font-size: 14px; opacity: 0.9;">Confidence</div>
                            </div>
                            <div>
                                <div style="font-size: 18px; font-weight: bold;">6-12 months</div>
                                <div style="font-size: 14px; opacity: 0.9;">Time Frame</div>
                            </div>
                        </div>
                        <div style="text-align: center; margin-top: 15px; font-size: 16px; line-height: 1.6;">
                            Long-term analyst projections and fundamental analysis targets
                        </div>
                        ${heuristicNote}
                        <div style="display: grid; gap: 15px; margin-top: 20px;">
                    `;
                    
                    let longTermTargets = '';
                    Object.entries(longTermPredictions).forEach(([targetName, prediction]) => {
                        const confidence = prediction.confidence || 'Low';
                        const probability = Math.round(prediction.probability || 0);
                        const confidenceColor = confidence === 'High' ? '#28a745' : 
                                              confidence === 'Medium' ? '#ffc107' : '#dc3545';
                        
                        longTermTargets += `
                            <div style="background: rgba(0,0,0,0.30); border-radius: 8px; padding: 15px; border-left: 4px solid ${confidenceColor}; box-shadow: 0 1px 3px rgba(0,0,0,0.25);">
                                <div style="display: flex; justify-content: between; align-items: center; margin-bottom: 8px;">
                                    <div style="font-size: 16px; font-weight: bold; color: #ffffff;">
                                        ${prediction.description || targetName}
                                    </div>
                                    <div style="font-size: 13px; text-align: right;">
                                        ${probabilityBadge(prediction)}
                                    </div>
                                </div>
                                <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-top: 10px; text-align: center;">
                                    <div>
                                        <div style="font-size: 18px; font-weight: bold; color: #ffffff;">$${prediction.target_price || 'N/A'}</div>
                                        <div style="font-size: 12px; color: #f1f3f5;">Target Price</div>
                                    </div>
                                    <div>
                                        <div style="font-size: 18px; font-weight: bold; color: #28a745;">+${prediction.upside_percent || 0}%</div>
                                        <div style="font-size: 12px; color: #f1f3f5;">Upside</div>
                                    </div>
                                    <div>
                                        <div style="font-size: 18px; font-weight: bold; color: #ffc107;">${prediction.timeframe || 'N/A'}</div>
                                        <div style="font-size: 12px; color: #f1f3f5;">Timeframe</div>
                                    </div>
                                </div>
                            </div>
                        `;
                    });
                    
                    longtermData.innerHTML += longTermTargets + '</div>';
                    
                    // Add mathematical breakdown
                    const breakdownElement = document.getElementById('longterm-breakdown');
                    const breakdownContent = document.getElementById('longterm-breakdown-content');
                    if (breakdownElement && breakdownContent) {
                        const totalTargets = predictions.length;
                        const highConfTargets = predictions.filter(p => p.confidence === 'Very High' || p.confidence === 'High').length;
                        const avgUpside = predictions.reduce((sum, p) => sum + parseFloat(p.upside_percent || 0), 0) / totalTargets;
                        
                        // Build detailed mathematical breakdown for long-term (analyst targets)
                        const recoveryData = window.currentRecoveryData || {};
                        const enhancedSignals = recoveryData.sophisticated_analysis?.enhanced_signals || {};
                        let longTermTargetsBreakdown = '';
                        let longTermTotalWeightedScore = 0;
                        let longTermTotalWeight = 0;
                        
                        // Show each analyst target's calculation
                        predictions.forEach((target, index) => {
                            const weight = target.confidence === 'Very High' ? 1.0 :
                                          target.confidence === 'High' ? 1.0 : 
                                          target.confidence === 'Medium' ? 0.8 : 0.6;
                            const probability = parseFloat(target.probability || 0);
                            
                            longTermTotalWeightedScore += probability * weight;
                            longTermTotalWeight += weight;
                            
                            longTermTargetsBreakdown += `
                                <div style="margin: 6px 0; padding: 6px; background: rgba(0,0,0,0.26); border-radius: 4px;">
                                    <div style="display: flex; justify-content: space-between; align-items: center;">
                                        <span style="color: #ffffff; font-weight: 500;">${target.target_type || 'Analyst Target'}:</span>
                                        <span>${probabilityBadge(target)}</span>
                                    </div>
                                    <div style="font-size: 11px; color: rgba(255,255,255,0.7);">
                                        Target: $${target.target_price} (+${Math.round((parseFloat(target.upside_percent) || 0) * 10) / 10}%)${target.median_days_to_hit ? ' • median ' + target.median_days_to_hit + 'd to reach' : ''}
                                    </div>
                                </div>`;
                        });
                        
                        const longTermBaseScore = longTermTotalWeight > 0 ? longTermTotalWeightedScore / longTermTotalWeight : avgProbability;
                        
                        // Calculate long-term signal effects (minimal impact)
                        let longTermSignalEffects = '';
                        let longTermSignalMultiplier = 1.0;
                        
                        if (enhancedSignals.volume_surge?.surge_detected) {
                            const volumeBoost = 1.0 + ((enhancedSignals.volume_surge.surge_multiplier - 1.0) * 0.25); // 25% impact for long-term
                            longTermSignalMultiplier *= volumeBoost;
                            longTermSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #28a745;">
                                    ✓ Volume Surge: ${volumeBoost.toFixed(2)}x multiplier (25% long-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.rsi_reversion?.oversold_detected) {
                            const rsiBoost = 1.0 + ((enhancedSignals.rsi_reversion.reversion_multiplier - 1.0) * 0.4); // 40% impact for long-term
                            longTermSignalMultiplier *= rsiBoost;
                            longTermSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #ffc107;">
                                    ✓ RSI Oversold: ${rsiBoost.toFixed(2)}x multiplier (40% long-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.economic_regime?.regime) {
                            const regimeBoost = enhancedSignals.economic_regime.long_term_multiplier || 1.0;
                            longTermSignalMultiplier *= regimeBoost;
                            const regimeColor = regimeBoost > 1.1 ? '#28a745' : regimeBoost > 1.0 ? '#ffc107' : '#dc3545';
                            longTermSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: ${regimeColor};">
                                    ✓ Market Regime: ${regimeBoost.toFixed(2)}x multiplier (VIX ${enhancedSignals.economic_regime.vix_level})
                                </div>`;
                        }
                        
                        // NEW HIGH-ACCURACY INDICATORS (Long-term with minimal impact)
                        if (enhancedSignals.money_flow_index?.oversold_detected) {
                            const mfiBoost = 1.0 + ((enhancedSignals.money_flow_index.recovery_multiplier - 1.0) * 0.25); // 25% impact
                            longTermSignalMultiplier *= mfiBoost;
                            longTermSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #17a2b8;">
                                    ✓ Money Flow Index: ${mfiBoost.toFixed(2)}x multiplier (25% long-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.macd_histogram?.momentum_shift) {
                            const macdBoost = 1.0 + ((enhancedSignals.macd_histogram.recovery_multiplier - 1.0) * 0.4); // 40% impact
                            longTermSignalMultiplier *= macdBoost;
                            longTermSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #dc3545;">
                                    ✓ MACD Histogram: ${macdBoost.toFixed(2)}x multiplier (40% long-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.bollinger_squeeze && enhancedSignals.bollinger_squeeze.signal_type !== 'neutral') {
                            const bbBoost = 1.0 + ((enhancedSignals.bollinger_squeeze.recovery_multiplier - 1.0) * 0.3); // 30% impact
                            longTermSignalMultiplier *= bbBoost;
                            longTermSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #6610f2;">
                                    ✓ Bollinger Bands: ${bbBoost.toFixed(2)}x multiplier (30% long-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.put_call_ratio?.extreme_sentiment) {
                            const pcBoost = 1.0 + ((enhancedSignals.put_call_ratio.recovery_multiplier - 1.0) * 0.2); // 20% impact
                            longTermSignalMultiplier *= pcBoost;
                            longTermSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #fd7e14;">
                                    ✓ Put/Call Ratio: ${pcBoost.toFixed(2)}x multiplier (20% long-term impact)
                                </div>`;
                        }
                        
                        if (enhancedSignals.short_interest?.squeeze_potential) {
                            const siBoost = 1.0 + ((enhancedSignals.short_interest.recovery_multiplier - 1.0) * 0.15); // 15% impact
                            longTermSignalMultiplier *= siBoost;
                            longTermSignalEffects += `
                                <div style="margin: 4px 0; font-size: 12px; color: #e83e8c;">
                                    ✓ Short Squeeze: ${siBoost.toFixed(2)}x multiplier (15% long-term impact)
                                </div>`;
                        }
                        
                        const longTermFinalScore = Math.min(85, longTermBaseScore * longTermSignalMultiplier);
                        
                        breakdownContent.innerHTML = `
                            <div style="margin-bottom: 12px;">
                                <strong style="color: #ffffff; font-size: 14px;">Measured hit rates</strong>
                                <div style="font-size: 12px; color: #f1f3f5; margin-top: 6px; line-height: 1.55;">
                                    Each figure is how often this stock actually reached that target
                                    within the horizon, counted over its own price history.
                                </div>
                                ${longTermTargetsBreakdown}
                            </div>

                            <div style="padding: 10px; background: rgba(0,0,0,0.28); border-radius: 6px; margin: 10px 0;">
                                <div style="color: #ffffff; font-weight: 600; margin-bottom: 4px;">Why there is no combined score</div>
                                <div style="font-size: 12px; color: #f1f3f5; line-height: 1.55;">
                                    Targets differ in size, so their hit rates are not comparable and
                                    averaging them would not describe anything real. This panel
                                    previously showed a weighted score multiplied by a "signal
                                    multiplier" and capped at 95, which is why every target displayed
                                    an identical 95% and the arithmetic read 95% &times; 1.88 = 95%.
                                    Each target now stands on its own measured frequency.
                                </div>
                            </div>
                        `;
                        breakdownElement.style.display = 'block';
                    }
                })
                .catch(error => {
                    console.error('Long-term data error:', error);
                    longtermData.innerHTML = `
                        <div style="text-align: center; color: rgba(255,255,255,0.8);">
                            <div style="font-size: 16px; margin: 20px 0;">⚠️ Error loading analyst data</div>
                        </div>
                    `;
                });
        }
        
        // Theme Toggle Functionality
        function toggleTheme() {
            const body = document.body;
            const themeToggle = document.getElementById('theme-toggle');
            const currentTheme = body.getAttribute('data-theme');
            
            if (currentTheme === 'dark') {
                body.setAttribute('data-theme', 'light');
                themeToggle.innerHTML = '🌙 Dark Mode';
                localStorage.setItem('theme', 'light');
            } else {
                body.setAttribute('data-theme', 'dark');
                themeToggle.innerHTML = '☀️ Light Mode';
                localStorage.setItem('theme', 'dark');
            }
        }
        
        function initTheme() {
            const body = document.body;
            const themeToggle = document.getElementById('theme-toggle');
            const savedTheme = localStorage.getItem('theme');
            
            // Default to dark mode if no preference saved
            const theme = savedTheme || 'dark';
            body.setAttribute('data-theme', theme);
            
            if (theme === 'dark') {
                themeToggle.innerHTML = '☀️ Light Mode';
            } else {
                themeToggle.innerHTML = '🌙 Dark Mode';
            }
        }
        
        // Initialize everything when page loads
        document.addEventListener('DOMContentLoaded', function() {
            initTheme();
            makeTablesSortable();
            makeSymbolsClickable();
            startAutoRefresh();
        });
        </script>
    </head>
    <body data-theme="dark">
        <!-- Theme Toggle Button -->
        <button id="theme-toggle" class="theme-toggle" onclick="toggleTheme()">☀️ Light Mode</button>
        
        <div class="container">
            <!-- Clean, Consolidated Header -->
            <div class="main-header" style="text-align: center; margin-bottom: 32px;">
                <h1 style="margin: 0 0 8px 0; font-size: 28px; font-weight: 700;">📉 Daily Losers Analysis</h1>
                <div style="display: flex; justify-content: center; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 12px;">
                    <span class="status-badge" style="font-size: 13px; font-weight: 500;">
                        🕐 {{ market_status.message }}
                        {% if market_status.time_to_close %} • {{ market_status.time_to_close }}{% endif %}
                    </span>
                    <span class="status-badge" style="font-size: 13px; font-weight: 500;">
                        {% if status.data_source == 'cached' %}📁 Cached
                        {% elif status.data_source == 'live' %}✅ Live
                        {% elif status.data_source == 'sample' %}⚠️ Sample
                        {% elif status.data_source == 'error' %}❌ Error
                        {% endif %}
                    </span>
                    <span class="status-badge" style="font-size: 13px; font-weight: 500;">
                        ⚡ Auto-refresh: 3hrs
                    </span>
                    <span class="status-badge" style="font-size: 13px; font-weight: 500;">
                        📊 {{ total_losers }} Stocks Analyzed
                    </span>
                    <a href="/refresh" style="background-color: #007bff; color: white; padding: 6px 12px; text-decoration: none; border-radius: 4px; font-weight: bold; font-size: 11px; margin-left: 8px;">
                        🔄 Force Refresh
                    </a>
                    <a href="/export/csv" style="background-color: #28a745; color: white; padding: 6px 12px; text-decoration: none; border-radius: 4px; font-weight: bold; font-size: 11px; margin-left: 5px;">
                        📊 Export CSV
                    </a>
                    <a href="/track-record" style="background-color: #6c5ce7; color: white; padding: 6px 12px; text-decoration: none; border-radius: 4px; font-weight: bold; font-size: 11px; margin-left: 5px;">
                        📜 Track Record
                    </a>
                    <a href="/methodology" style="background-color: #444c56; color: white; padding: 6px 12px; text-decoration: none; border-radius: 4px; font-weight: bold; font-size: 11px; margin-left: 5px;">
                        📖 Methodology
                    </a>
                    <span style="font-size: 12px; color: var(--text-secondary); margin-left: 15px;">{{ timestamp.split(' (')[0] }}</span>
                </div>
            </div>
            
                

            <!-- Clean Market Overview -->
            <div class="section">
                <h2 style="margin-bottom: 24px; text-align: left;">📈 Market Overview</h2>
                <div class="metrics-grid" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; margin-bottom: 8px;">
                    <!-- VIX Volatility Card -->
                    <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; text-align: center; box-shadow: var(--shadow);">
                        <div style="font-size: 14px; color: var(--text-secondary); font-weight: 600; margin-bottom: 8px;">📊 VOLATILITY INDEX</div>
                        <div style="font-size: 32px; font-weight: 700; color: {{ market_analysis.vix_analysis.color }}; margin-bottom: 4px;">{{ market_analysis.vix_analysis.current_vix }}</div>
                        <div style="font-size: 13px; color: var(--text-secondary); font-weight: 500;">VIX • {{ market_analysis.vix_analysis.regime }}</div>
                        <div style="font-size: 12px; color: var(--text-secondary); margin-top: 8px; opacity: 0.8;">{{ market_analysis.vix_analysis.recovery_impact }}</div>
                    </div>
                    
                    <!-- SPY Market Trend Card -->
                    <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; text-align: center; box-shadow: var(--shadow);">
                        <div style="font-size: 14px; color: var(--text-secondary); font-weight: 600; margin-bottom: 8px;">📈 S&P 500 TRACKER</div>
                        <div style="font-size: 32px; font-weight: 700; color: {{ market_analysis.market_trend.color }}; margin-bottom: 4px;">${{ market_analysis.market_trend.current_price if market_analysis.market_trend.current_price != 'N/A' else 'N/A' }}</div>
                        <div style="font-size: 13px; color: var(--text-secondary); font-weight: 500;">SPY • {{ market_analysis.market_trend.trend }}</div>
                        <div style="font-size: 12px; color: var(--text-secondary); margin-top: 8px; opacity: 0.8;">
                            {% if market_analysis.market_trend.week_change != 'N/A' %}
                                {{ market_analysis.market_trend.week_change|round(2) }}% this week
                            {% else %}
                                Market data loading...
                            {% endif %}
                        </div>
                    </div>
                    
                    <!-- AI Recommendations Card -->
                    <div style="background: var(--bg-secondary); border: 1px solid var(--border-color); border-radius: 12px; padding: 20px; text-align: center; box-shadow: var(--shadow);">
                        <div style="font-size: 14px; color: var(--text-secondary); font-weight: 600; margin-bottom: 8px;">🤖 AI SIGNALS</div>
                        <div style="font-size: 32px; font-weight: 700; color: {% if recommendations_count > 0 %}var(--positive-color){% else %}var(--text-secondary){% endif %}; margin-bottom: 4px;">{{ recommendations_count }}</div>
                        <div style="font-size: 13px; color: var(--text-secondary); font-weight: 500;">Strong Buy Signals</div>
                        <div style="font-size: 12px; color: var(--text-secondary); margin-top: 8px; opacity: 0.8;">
                            {% if recommendations_count > 0 %}
                                High-conviction opportunities detected
                            {% else %}
                                Conservative mode active
                            {% endif %}
                        </div>
                    </div>
                </div>
            </div>
                

            {% macro stock_cards(rows, section_id) %}
            <div class="stock-cards" id="cards-{{ section_id }}">
                {% for stock in rows %}
                <div class="stock-card" role="button" tabindex="0"
                     aria-label="Open full analysis for {{ stock.Symbol }}"
                     onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}"
                     data-score="{{ stock.get('Rebound Score') if stock.get('Rebound Score') is not none else -1 }}"
                     data-upside="{{ stock['Potential Return %'] if stock['Potential Return %'] != '\u2014' else -999 }}"
                     data-p7="{{ stock['P Short']['sort'] }}"
                     data-sym="{{ stock.Symbol }}" data-name="{{ stock.Name }}"
                     onclick="showUltimateAnalysis(this.dataset.sym, this.dataset.name)">
                    <div class="card-top">
                        <span class="card-symbol">{{ stock.Symbol }}</span>
                        <span class="card-price">{% if stock['Current Price'] not in ('N/A', '\u2014') %}${{ "%.2f"|format(stock['Current Price']) }}{% else %}&#8212;{% endif %}</span>
                        <span class="card-change">{{ stock['Percent Change Today'] }}</span>
                    </div>
                    <div class="card-score-row">
                        {% if stock.get('Rebound Score') is not none %}
                            <span class="card-score">{{ stock.get('Rebound Score') }}</span>
                            <span class="card-score-sub">/100 · {{ stock.get('Confidence','') }} conf · {{ stock.get('Factors Used','?') }}/{{ stock.get('Factors Total', 6) }} inputs</span>
                        {% else %}
                            <span class="card-score">&#8212;</span>
                            <span class="card-score-sub">{{ stock.get('Score Reason', 'insufficient data') }}</span>
                        {% endif %}
                        <span class="card-sentiment">{{ stock.get('AI Sentiment','') }}</span>
                    </div>
                    <div class="card-chips">
                        <span class="chip" title="{{ stock['P Short'].get('detail','') }}">7d <strong>{{ stock['P Short']['display'] }}</strong></span>
                        <span class="chip" title="{{ stock['P Medium'].get('detail','') }}">21d <strong>{{ stock['P Medium']['display'] }}</strong></span>
                        <span class="chip" title="{{ stock['P Long'].get('detail','') }}">6mo <strong>{{ stock['P Long']['display'] }}</strong></span>
                        <span class="chip chip-upside">{% if stock['Potential Return %'] != '\u2014' %}▲ {{ stock['Potential Return %'] }}% <em>{{ stock.get('Analyst Count') or '?' }} an.</em>{% else %}▲ &#8212;{% endif %}</span>
                    </div>
                </div>
                {% endfor %}
            </div>
            {% endmacro %}

            <div class="section">
                <h2 style="text-align: left;">🔍 Short Term Recovery Recommendations</h2>
                {% if recommendations %}
                    <div class="card-sorter" data-target="cards-recs">
                        <span>Sort:</span>
                        <button class="sort-chip active" aria-pressed="true" onclick="sortCards('cards-recs', 'score', this)">Score</button>
                        <button class="sort-chip" onclick="sortCards('cards-recs', 'upside', this)">Upside</button>
                        <button class="sort-chip" onclick="sortCards('cards-recs', 'p7', this)">7d odds</button>
                    </div>
                    {{ stock_cards(recommendations, 'recs') }}
                    <div class="table-wrap"><table>
                        <thead>
                            <tr>
                                <th onclick="sortLoserTable(this, 0, 'text')">Symbol</th>
                                <th onclick="sortLoserTable(this, 1, 'num')" title="Backtested rebound score, 0-100, with confidence from input coverage. Click to sort.">Score</th>
                                <th onclick="sortLoserTable(this, 2, 'num')" title="Analyst consensus upside. Click to sort.">Upside</th>
                                <th onclick="sortLoserTable(this, 3, 'num')" title="Measured frequency of reaching yesterday's close within 7 trading days, over this stock's own history. Click to sort.">P(prev close, 7d)</th>
                                <th onclick="sortLoserTable(this, 4, 'num')" title="Measured frequency of reaching the 20-day mean within 21 trading days. Click to sort.">P(20d MA, 21d)</th>
                                <th onclick="sortLoserTable(this, 5, 'num')" title="Measured frequency of reaching the analyst consensus within ~6 months. Click to sort.">P(target, 6mo)</th>
                                <th onclick="sortLoserTable(this, 6, 'num')">Current Price</th>
                                <th onclick="sortLoserTable(this, 7, 'num')">Today's %</th>
                                <th>Analysis</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for stock in recommendations %}
                            <tr class="highlight">
                                <td>
                                    <strong class="stock-symbol">{{ stock.Symbol }}</strong>
                                </td>
                                <td data-val="{{ stock.get('Rebound Score') if stock.get('Rebound Score') is not none else -1 }}">
                                    {% if stock.get('Rebound Score') is not none %}
                                        <strong>{{ stock.get('Rebound Score') }}</strong><span style="color: var(--text-secondary); font-size: 11px;"> /100 · {{ stock.get('Confidence','') }} conf · {{ stock.get('Factors Used', '?') }}/{{ stock.get('Factors Total', 6) }} inputs</span>
                                        <div style="font-size: 11px; color: var(--text-secondary);">{{ stock.get('AI Sentiment', '') }}</div>
                                    {% else %}
                                        <span title="{{ stock.get('Score Reason', 'insufficient data') }}">&#8212;</span>
                                        <div style="font-size: 11px; color: var(--text-secondary);">{{ stock.get('AI Sentiment', '') }}</div>
                                    {% endif %}
                                </td>
                                <td data-val="{{ stock['Potential Return %'] if stock['Potential Return %'] != '\u2014' else -999 }}">
                                    {% if stock['Potential Return %'] != '\u2014' %}+{{ stock['Potential Return %'] }}%<div style="font-size: 10px; color: var(--text-secondary);">{{ stock.get('Analyst Count') or '?' }} analysts</div>{% else %}&#8212;{% endif %}
                                </td>
                                <td data-val="{{ stock['P Short']['sort'] }}" title="{{ stock['P Short'].get('detail','') }}">{{ stock['P Short']['display'] }}</td>
                                <td data-val="{{ stock['P Medium']['sort'] }}" title="{{ stock['P Medium'].get('detail','') }}">{{ stock['P Medium']['display'] }}</td>
                                <td data-val="{{ stock['P Long']['sort'] }}" title="{{ stock['P Long'].get('detail','') }}">{{ stock['P Long']['display'] }}</td>
                                <td data-val="{{ stock['Current Price'] }}">${{ "%.2f"|format(stock['Current Price']) }}</td>
                                <td class="negative" data-val="{{ stock['Percent Change Today']|replace('%','') }}">{{ stock['Percent Change Today'] }}</td>
                                <td>
                                    <button class="ai-button" onclick="showUltimateAnalysis('{{ stock.Symbol }}', '{{ stock.Name }}')" 
                                            style="background: linear-gradient(45deg, #007bff, #28a745, #fd7e14); color: white; font-weight: bold; font-size: 11px; padding: 4px 8px;">
                                        🤖📱🔮 Analysis
                                    </button>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table></div>
                {% else %}
                    <div style="background: var(--bg-secondary); border: 2px solid var(--border-color); border-radius: 10px; padding: 25px; text-align: center;">
                        <div style="font-size: 48px; margin-bottom: 15px;">🤖</div>
                        <h3 style="color: var(--text-primary); margin-bottom: 15px; text-align: left;">No Strong Buy Recommendations Today</h3>
                        <p style="font-size: 16px; color: var(--text-secondary); margin-bottom: 20px;">
                            This is actually <strong>good news</strong> - our AI is being appropriately conservative in current market conditions.
                        </p>
                        
                        <div style="background: var(--bg-tertiary); border: 1px solid var(--border-color); border-radius: 8px; padding: 20px; margin: 15px 0; text-align: left;">
                            <h4 style="color: var(--text-primary); margin-bottom: 10px;">📊 Current Market Snapshot:</h4>
                            <ul style="margin: 0; color: var(--text-secondary);">
                                <li><strong>VIX Level:</strong> {{ market_analysis.vix_analysis.current_vix }} ({{ market_analysis.vix_analysis.regime }})</li>
                                <li><strong>Market Trend:</strong> {{ market_analysis.market_trend.trend }} (SPY {{ market_analysis.market_trend.week_change|round(2) if market_analysis.market_trend.week_change != 'N/A' else 'N/A' }}%)</li>
                                <li><strong>Recovery Environment:</strong> {{ market_analysis.vix_analysis.recovery_impact }}</li>
                            </ul>
                        </div>
                        
                        <div style="background: var(--bg-tertiary); border: 1px solid var(--accent-blue); border-radius: 8px; padding: 15px; margin: 15px 0; text-align: left;">
                            <h4 style="color: var(--accent-blue); margin-bottom: 10px;">🎯 What We're Looking For:</h4>
                            <div style="color: var(--text-secondary); font-size: 14px;">
                                <strong>Strong Buy Signals:</strong> Recovery scores ≥75% with "STRONG BUY" recommendations<br>
                                <strong>Market Catalyst:</strong> VIX >25 or significant market corrections (SPY -3%+)<br>
                                <strong>Technical Oversold:</strong> Multiple stocks showing extreme oversold conditions simultaneously
                            </div>
                        </div>
                        
                        <div style="background: var(--bg-tertiary); border: 1px solid #ffc107; border-radius: 8px; padding: 15px; margin: 15px 0; text-align: left;">
                            <h4 style="color: #ffc107; margin-bottom: 10px;">💡 Why This Approach Works:</h4>
                            <p style="color: var(--text-secondary); font-size: 14px; margin: 0;">
                                By waiting for high-conviction opportunities, we avoid the trap of mediocre recommendations during uncertain periods. 
                                Quality over quantity means better risk-adjusted returns when opportunities do arise.
                            </p>
                        </div>
                        
                        <p style="font-style: italic; color: var(--text-secondary); margin-top: 20px;">
                            Check back during periods of market stress or volatility for potential opportunities!
                        </p>
                    </div>
                {% endif %}
            </div>

            <div class="section">
                <h2 style="text-align: left;">📊 Complete Analysis (All Daily Losers)</h2>
                <p><em>Comprehensive analysis of all daily losers with AI recovery predictions and market insights.</em></p>
                {% if all_analysis %}
                    <div class="card-sorter" data-target="cards-all">
                        <span>Sort:</span>
                        <button class="sort-chip active" aria-pressed="true" onclick="sortCards('cards-all', 'score', this)">Score</button>
                        <button class="sort-chip" onclick="sortCards('cards-all', 'upside', this)">Upside</button>
                        <button class="sort-chip" onclick="sortCards('cards-all', 'p7', this)">7d odds</button>
                    </div>
                    {{ stock_cards(all_analysis, 'all') }}
                    <div class="table-wrap"><table>
                        <thead>
                            <tr>
                                <th onclick="sortLoserTable(this, 0, 'text')">Symbol</th>
                                <th onclick="sortLoserTable(this, 1, 'num')" title="Backtested rebound score, 0-100, with confidence from input coverage. Click to sort.">Score</th>
                                <th onclick="sortLoserTable(this, 2, 'num')" title="Analyst consensus upside. Click to sort.">Upside</th>
                                <th onclick="sortLoserTable(this, 3, 'num')" title="Measured frequency of reaching yesterday's close within 7 trading days, over this stock's own history. Click to sort.">P(prev close, 7d)</th>
                                <th onclick="sortLoserTable(this, 4, 'num')" title="Measured frequency of reaching the 20-day mean within 21 trading days. Click to sort.">P(20d MA, 21d)</th>
                                <th onclick="sortLoserTable(this, 5, 'num')" title="Measured frequency of reaching the analyst consensus within ~6 months. Click to sort.">P(target, 6mo)</th>
                                <th onclick="sortLoserTable(this, 6, 'num')">Current Price</th>
                                <th onclick="sortLoserTable(this, 7, 'num')">Today's %</th>
                                <th>Analysis</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for stock in all_analysis %}
                            <tr>
                                <td>
                                    <strong class="stock-symbol">{{ stock.Symbol }}</strong>
                                </td>
                                <td data-val="{{ stock.get('Rebound Score') if stock.get('Rebound Score') is not none else -1 }}">
                                    {% if stock.get('Rebound Score') is not none %}
                                        <strong>{{ stock.get('Rebound Score') }}</strong><span style="color: var(--text-secondary); font-size: 11px;"> /100 · {{ stock.get('Confidence','') }} conf · {{ stock.get('Factors Used', '?') }}/{{ stock.get('Factors Total', 6) }} inputs</span>
                                        <div style="font-size: 11px; color: var(--text-secondary);">{{ stock.get('AI Sentiment', '') }}</div>
                                    {% else %}
                                        <span title="{{ stock.get('Score Reason', 'insufficient data') }}">&#8212;</span>
                                        <div style="font-size: 11px; color: var(--text-secondary);">{{ stock.get('AI Sentiment', '') }}</div>
                                    {% endif %}
                                </td>
                                <td data-val="{{ stock['Potential Return %'] if stock['Potential Return %'] != '\u2014' else -999 }}">
                                    {% if stock['Potential Return %'] != '\u2014' %}+{{ stock['Potential Return %'] }}%<div style="font-size: 10px; color: var(--text-secondary);">{{ stock.get('Analyst Count') or '?' }} analysts</div>{% else %}&#8212;{% endif %}
                                </td>
                                <td data-val="{{ stock['P Short']['sort'] }}" title="{{ stock['P Short'].get('detail','') }}">{{ stock['P Short']['display'] }}</td>
                                <td data-val="{{ stock['P Medium']['sort'] }}" title="{{ stock['P Medium'].get('detail','') }}">{{ stock['P Medium']['display'] }}</td>
                                <td data-val="{{ stock['P Long']['sort'] }}" title="{{ stock['P Long'].get('detail','') }}">{{ stock['P Long']['display'] }}</td>
                                <td data-val="{{ stock['Current Price'] if stock['Current Price'] not in ('N/A', '\u2014') else -1 }}">
                                    {% if stock['Current Price'] in ('N/A', '\u2014') %}&#8212;{% else %}${{ "%.2f"|format(stock['Current Price']) }}{% endif %}
                                </td>
                                <td class="negative" data-val="{{ stock['Percent Change Today']|replace('%','') }}">{{ stock['Percent Change Today'] }}</td>
                                <td>
                                    <button class="ai-button" onclick="showUltimateAnalysis('{{ stock.Symbol }}', '{{ stock.Name }}')" style="background: linear-gradient(45deg, #007bff, #28a745, #fd7e14); color: white; font-weight: bold; font-size: 11px; padding: 4px 8px;">🤖📱🔮 Analysis</button>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table></div>
                {% else %}
                    <p>No investment analysis data available.</p>
                {% endif %}
            </div>






            <!-- Footer -->
            <footer style="background-color: #343a40; color: white; padding: 30px 20px; margin-top: 40px; text-align: center; border-radius: 8px;">
                <div style="max-width: 800px; margin: 0 auto;">
                    <h4 style="margin-bottom: 15px; color: #fff;">📊 Yahoo Finance Daily Losers Analyzer</h4>
                    <p style="margin-bottom: 20px; line-height: 1.6;">
                        A next-generation web platform that analyzes Yahoo Finance daily losers with Ultimate Analysis buttons, interactive TradingView charts, EST time display, and AI-powered investment insights.
                    </p>
                    
                    <div style="display: flex; justify-content: center; gap: 30px; flex-wrap: wrap; margin-bottom: 20px;">
                        <div>
                            <strong>👨‍💻 Created by:</strong><br>
                            <span style="color: #17a2b8;">Damien Adams</span>
                        </div>
                        <div>
                            <strong>🔧 Tech Stack:</strong><br>
                            Python • Flask • BeautifulSoup • Pandas
                        </div>
                        <div>
                            <strong>☁️ Deployed on:</strong><br>
                            <span style="color: #28a745;">Render Cloud</span>
                        </div>
                    </div>
                    
                    <div style="border-top: 1px solid #495057; padding-top: 20px;">
                        <a href="https://github.com/repbyrepdev/yahoo_losers_webapp" 
                           style="background-color: #6c757d; color: white; padding: 8px 16px; text-decoration: none; border-radius: 4px; font-weight: bold; display: inline-block; margin: 0 10px;">
                            📂 View Source Code
                        </a>
                        <a href="https://github.com/repbyrepdev" 
                           style="background-color: #17a2b8; color: white; padding: 8px 16px; text-decoration: none; border-radius: 4px; font-weight: bold; display: inline-block; margin: 0 10px;">
                            🔗 My GitHub Profile
                        </a>
                    </div>
                    
                    <!-- Disclaimer -->
                    <div style="background: rgba(255,193,7,0.1); border: 1px solid #ffc107; border-radius: 5px; padding: 15px; margin: 20px 0;">
                        <p style="margin: 0; font-size: 13px; color: #fff8dc; line-height: 1.5;">
                            <strong>⚠️ Disclaimer:</strong> This is a technical demonstration, not financial advice.
                            The rebound score measures how closely a stock matches conditions that have historically
                            preceded mean reversion. It is <strong>not a prediction of future returns</strong>.
                            <br><br>
                            <strong>What the evidence actually shows:</strong> a backtest over 9,126 point-in-time
                            observations found the technical factors carry a <em>weak</em> positive signal
                            (rank correlation +0.04 at a 20-day horizon), with roughly a 2.4 percentage point spread
                            between the highest- and lowest-scoring buckets. The majority of the model's weight comes
                            from analyst data that <strong>cannot be backtested</strong> with this data source and is
                            therefore unvalidated. The backtest also excludes trading costs, and its universe carries
                            survivorship bias.
                            <br><br>
                            Stock investments carry risk and past performance does not guarantee future results.
                            Consult a qualified financial advisor before making investment decisions.
                        </p>
                    </div>
                    
                    <p style="margin-top: 15px; font-size: 12px; color: #adb5bd;">
                        © 2024 Damien Adams. Open source project. Data provided by Yahoo Finance.
                    </p>
                </div>
            </footer>
        </div>
    </body>
    </html>
    """
    
    return html_template

@app.route('/')
@rate_limit(MAX_REQUESTS_PER_MINUTE)
def index():
    """Main route that runs the Yahoo Finance losers analysis"""
    try:
        # Check cache first
        logger.info("Checking cache...")
        cache_data = load_cache()
        cache_status = get_cache_status()
        
        if cache_data:
            # Use cached data
            logger.info("Using cached data")
            cached_results = cache_data['data']
            
            # Add cache information to status
            cached_results['status']['message'] = f"📁 CACHED: {cache_status['message']}"
            cached_results['status']['data_source'] = 'cached'
            cached_results['cache_info'] = cache_status
            
            # Update timestamp to show cache time
            cached_results['timestamp'] = f"{cache_data['timestamp'].astimezone(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %I:%M:%S %p EST')} (cached)"
            
            # Add current market status (always fresh)
            cached_results['market_status'] = get_market_status()
            
            html_template = format_results_as_html(
                cached_results['losers_data'], 
                cached_results['details_data'], 
                cached_results['all_analysis'], 
                cached_results['recommendations'], 
                cached_results['status']
            )
            
            # Generate ETag and add cache headers
            etag = generate_etag(cached_results)
            
            # Check if client has current version (ETag)
            if request.headers.get('If-None-Match') == etag:
                response = make_response('', 304)
                response.headers['ETag'] = etag
                return response
            
            response = make_response(render_template_string(html_template, **cached_results))
            response.headers['ETag'] = etag
            return add_cache_headers(response, max_age=60)
        
        # No valid cache, perform fresh analysis
        logger.info("No valid cache, performing fresh analysis...")
        
        # Step 1: Scrape today's losers
        logger.info("Step 1: Scraping Yahoo Finance losers...")
        losers_data, losers_status = scrape_yahoo_losers()
        
        # Step 2: Get detailed information for top stocks
        logger.info("Step 2: Getting detailed stock information...")
        symbols = [stock['Symbol'] for stock in losers_data]
        details_data = get_stock_details(symbols)
        
        # Step 3: Calculate AI-enhanced investment analysis for ALL stocks
        logger.info("Step 3: Calculating AI-enhanced investment analysis...")
        all_analysis = calculate_enhanced_investment_analysis(losers_data, details_data)
        
        # Step 4: Filter AI recovery potential (replaces 65% analyst filter)
        logger.info("Step 4: Filtering AI recovery potential...")
        recommendations = filter_ai_recovery_potential(all_analysis)
        
        # Get market status
        market_status = get_market_status()
        market_analysis = get_comprehensive_market_analysis()
        
        # Prepare template variables
        template_vars = {
            'timestamp': datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %I:%M:%S %p EST'),
            'total_losers': len(losers_data),
            'detailed_count': len(details_data),
            'all_analysis_count': len(all_analysis),
            'recommendations_count': len(recommendations),
            'losers_data': losers_data,
            'details_data': details_data,
            'all_analysis': all_analysis,
            'recommendations': recommendations,
            'status': losers_status,
            'cache_info': cache_status,
            'market_status': market_status,
            'market_analysis': market_analysis
        }
        
        # Save results to cache
        logger.info("Saving results to cache...")
        save_cache(template_vars)
        
        # Step 5: Format as HTML
        logger.info("Step 5: Formatting results...")
        html_template = format_results_as_html(losers_data, details_data, all_analysis, recommendations, losers_status)
        
        # Generate ETag and add cache headers for fresh data
        etag = generate_etag(template_vars)
        
        # Check if client has current version (ETag) 
        if request.headers.get('If-None-Match') == etag:
            response = make_response('', 304)
            response.headers['ETag'] = etag
            return response
            
        response = make_response(render_template_string(html_template, **template_vars))
        response.headers['ETag'] = etag
        return add_cache_headers(response, max_age=60)
        
    except Exception as e:
        logger.error(f"Error in main analysis: {str(e)}")
        return f"<h1>Error occurred during analysis: {str(e)}</h1><p>Please try refreshing the page.</p>"


@app.route('/api/snapshot')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def api_snapshot():
    """One day's full model output, for the nightly recorder to commit.

    Runs the complete scoring (all six factors, fetches allowed) for today's
    losers, and quotes every symbol picked in recent snapshots so forward
    returns stay computable after names drop off the losers list. Called once
    a day by the GitHub Action; the throttle keeps the burst legal.
    """
    losers_data, status = scrape_yahoo_losers()
    if not status.get("success"):
        return jsonify({"error": "losers list unavailable", "detail": status.get("message")}), 503

    symbols = [s["Symbol"] for s in losers_data if s.get("Symbol") and s["Symbol"] != "ERROR"]
    market_data.batch_history(symbols)

    universe = []
    for stock in losers_data:
        symbol = stock.get("Symbol")
        if not symbol or symbol == "ERROR":
            continue
        try:
            result = score_stock(symbol, full=True)
        except Exception as e:
            logger.warning(f"snapshot scoring failed for {symbol}: {type(e).__name__}")
            result = {"scored": False, "reason": type(e).__name__}
        tech = market_data.technicals(symbol, allow_fetch=False)
        price = (tech.value or {}).get("close") if tech.ok else None
        targets = market_data.analyst_target(symbol, allow_fetch=False)
        row = {
            "symbol": symbol,
            "price": price,
            "score": result.get("score") if result.get("scored") else None,
            "confidence": result.get("confidence"),
            "coverage": result.get("coverage"),
            "not_scored_reason": None if result.get("scored") else result.get("reason"),
            "factors": {f["key"]: {"score": f["score"], "detail": f["detail"]}
                        for f in result.get("factors", []) if f.get("key")} if result.get("scored") else {},
            "analyst_target_mean": targets["mean"].value if targets["mean"].ok else None,
            "analyst_count": targets["analysts"].value if targets["analysts"].ok else None,
        }
        universe.append(row)

    prior = [s for s in tracking.tracked_symbols() if s not in set(symbols)]
    tracked_prices = {}
    if prior:
        market_data.batch_history(prior)
        for symbol in prior:
            history = market_data.price_history(symbol, allow_fetch=False)
            if history.ok and history.value:
                tracked_prices[symbol] = history.value[-1]

    return jsonify(tracking.build_snapshot(universe, tracked_prices))


@app.route('/track-record')
def track_record_page():
    """Realized forward returns of every logged pick, computed from git history."""
    record = tracking.compute_track_record()
    rows = ""
    for pick in record["picks"][:60]:
        rets = pick.get("returns", {})
        def cell(h):
            r = rets.get(str(h))
            if not r:
                return '<td class="pending">pending</td>'
            tone = '#2ecc71' if r['pct'] > 0 else '#e74c3c'
            return f'<td style="color:{tone}">{r["pct"]:+.1f}%</td>'
        rows += (f'<tr><td>{pick["date"]}</td><td class="sym">{pick["symbol"]}</td>'
                 f'<td>{pick["score"]:.1f}</td><td>${pick["entry"]:.2f}</td>'
                 f'{cell(7)}{cell(30)}</tr>')

    def agg(h):
        e = record["horizons"].get(str(h), {})
        if not e.get("n_picks"):
            return f"<p>No picks have reached the {h}-day horizon yet.</p>"
        parts = [f"<strong>{e['n_picks']}</strong> resolved picks, mean <strong>{e.get('picks_mean', 0):+.2f}%</strong>, "
                 f"win rate <strong>{e.get('picks_win_rate', 0):.0%}</strong>"]
        if e.get("baseline_mean") is not None:
            parts.append(f" vs unpicked-losers baseline {e['baseline_mean']:+.2f}% "
                         f"(excess <strong>{e.get('excess', 0):+.2f}%</strong>, n={e['n_baseline']})")
        return f"<p>{''.join(parts)}</p>"

    body = f"""
    <h1>📜 Track Record</h1>
    <p class="sub">Every recommendation this app has logged, joined with the prices its own later
    snapshots recorded. Snapshots are committed to
    <a href="https://github.com/repbyrepdev/yahoo_losers_webapp/tree/main/data/snapshots">git</a>,
    so this history is tamper-evident &mdash; rewriting it would leave a diff.</p>
    <p class="sub">Recording began <strong>{record['first_date'] or 'today'}</strong> &middot;
    {record['snapshot_days']} snapshot day(s) &middot; model v{record['model_version']} &middot;
    picks are scores &ge; {record['pick_threshold']:.0f} &middot; {record['pending']} pick(s) awaiting a forward price.</p>
    <h2>~7 calendar days</h2>{agg(7)}
    <h2>~30 calendar days</h2>{agg(30)}
    <h2>Individual picks</h2>
    <table><thead><tr><th>Date</th><th>Symbol</th><th>Score</th><th>Entry</th>
    <th>~7d</th><th>~30d</th></tr></thead><tbody>{rows or '<tr><td colspan=6>No picks logged yet &mdash; the first snapshot lands tonight.</td></tr>'}</tbody></table>
    <p class="sub">Horizons match to the nearest snapshot (&plusmn;40%), so "7d" is calendar-approximate.
    Returns ignore dividends and costs. A young record proves little either way; that is why it is dated.</p>
    """
    return _simple_page("Track Record", body)


@app.route('/methodology')
def methodology_page():
    """The README, rendered in-app, so the methodology ships with the product."""
    try:
        import markdown as _md
        with open(os.path.join(os.path.dirname(__file__), "README.md"), encoding="utf-8") as fh:
            html = _md.markdown(fh.read(), extensions=["tables", "fenced_code"])
    except Exception as e:
        logger.warning(f"methodology render failed: {type(e).__name__}")
        html = "<p>Methodology temporarily unavailable.</p>"
    return _simple_page("Methodology", html)


_CLIENT_ERRORS = deque(maxlen=50)


@app.route('/api/client-error', methods=['POST'])
@rate_limit(10)
def client_error():
    """Browser-side failures, logged server-side.

    Tonight's tab-blanking bugs lived inside swallowed catch handlers and were
    invisible from the server. This gives them somewhere to land.
    """
    try:
        payload = request.get_json(silent=True) or {}
        entry = {
            "at": datetime.now().isoformat(),
            "msg": str(payload.get("msg", ""))[:300],
            "src": str(payload.get("src", ""))[:200],
            "line": payload.get("line"),
        }
        _CLIENT_ERRORS.append(entry)
        logger.error(f"client-error: {entry['msg']} @ {entry['src']}:{entry['line']}")
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route('/api/client-errors')
def client_errors():
    return jsonify({"errors": list(_CLIENT_ERRORS)})


def _simple_page(title, body_html):
    """Shared shell for the static-ish pages, matching the app's dark theme."""
    return f"""<!DOCTYPE html><html><head>
    <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#0d1117"><title>{title} — Daily Losers</title>
    <style>
      body {{ background: #0d1117; color: #e6edf3; font-family: -apple-system, system-ui, sans-serif;
             max-width: 900px; margin: 0 auto; padding: 20px; line-height: 1.6; }}
      a {{ color: #6c8dff; }}
      h1, h2, h3 {{ color: #fff; }} code, pre {{ background: #161b22; border-radius: 6px; padding: 2px 6px; }}
      pre {{ padding: 12px; overflow-x: auto; }}
      table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
      th, td {{ border: 1px solid #30363d; padding: 8px 10px; text-align: left; font-size: 14px; }}
      th {{ background: #161b22; }}
      .sub {{ color: #8b949e; font-size: 14px; }} .sym {{ color: #6c8dff; font-weight: 700; }}
      .pending {{ color: #8b949e; font-style: italic; }}
      .nav {{ margin-bottom: 18px; }}
    </style></head><body>
    <div class="nav"><a href="/">&larr; Dashboard</a> &middot; <a href="/track-record">Track record</a>
    &middot; <a href="/methodology">Methodology</a></div>
    {body_html}
    </body></html>"""


@app.route('/sw.js')
def service_worker():
    # Served from the root so its scope covers the whole app; a worker under
    # /static/ could only control /static/.
    return app.send_static_file('sw.js')


@app.route('/health')
def health_check():
    """Enhanced health check endpoint for auto-scaling"""
    try:
        # Basic health checks
        health_status = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "version": "1.0.0",
            "instance_id": os.environ.get('HOSTNAME', 'unknown')
        }
        
        # Check cache availability
        cache_info = get_cache_status()
        health_status["cache"] = {
            "status": "available" if cache_info.get("exists") else "unavailable",
            "age_hours": cache_info.get("age_hours", 0),
            # Which backend each layer actually resolved to. Without this, a
            # misconfigured REDIS_URL is invisible: the app degrades silently to
            # per-worker in-memory caches, which halves the useful request
            # budget and cannot be diagnosed from outside.
            "page_cache_backend": "redis" if USE_REDIS else "file",
            "market_data_backend": "redis" if market_data._cache._redis is not None else "memory",
            "market_data_entries": market_data.cache_size(),
        }
        
        # Check memory usage for scaling decisions
        memory = get_memory_usage()
        health_status["resources"] = {
            "memory_mb": round(memory['rss'], 1),
            "memory_percent": round(memory['percent'], 1),
            "healthy": memory['percent'] < 90  # Unhealthy if using >90% memory
        }
        
        # Liveness, not readiness. An empty cache is the normal state of a
        # freshly started instance, so treating it as unhealthy made every new
        # deploy fail its health check and hang in update_in_progress forever.
        # Cache state is still reported, just not as a failure condition.
        overall_healthy = health_status["resources"]["healthy"]
        
        status_code = 200 if overall_healthy else 503
        if not overall_healthy:
            health_status["status"] = "unhealthy"
            
        return health_status, status_code
        
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }, 503

# Upstream providers this app depends on. Checked by /health/sources so that a
# provider going dark is visible immediately instead of silently degrading the
# data behind a fallback.
UPSTREAM_SOURCES = {
    "yahoo_screener": "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_losers&count=1",
    "yahoo_chart": "https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
    "yahoo_quote_summary": "https://query1.finance.yahoo.com/v10/finance/quoteSummary/AAPL?modules=financialData",
    "yahoo_options": "https://query1.finance.yahoo.com/v7/finance/options/AAPL",
    "reddit_search": "https://www.reddit.com/search.json?q=%24AAPL&limit=1",
    "stocktwits": "https://api.stocktwits.com/api/2/streams/symbol/AAPL.json",
}


@app.route('/health/sources')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def health_sources():
    """Report which upstream providers are actually reachable right now.

    Every provider that fails here corresponds to a field the UI will render as
    an em dash. This endpoint exists because three providers began returning
    401/403 and nothing surfaced it -- the failures were absorbed by fallbacks
    that produced invented values.
    """
    headers = {'User-Agent': 'Mozilla/5.0 (compatible; StockAnalyzer/1.0)'}
    results = {}

    for name, url in UPSTREAM_SOURCES.items():
        try:
            response = requests.get(url, headers=headers, timeout=10)
            results[name] = {
                "reachable": response.ok,
                "http_status": response.status_code,
            }
        except requests.RequestException as e:
            results[name] = {"reachable": False, "error": type(e).__name__}

    # yfinance is the path that actually serves analyst targets, options and
    # history, and Yahoo throttles per IP. A block here starves the score while
    # every raw endpoint above looks unchanged, so it is probed explicitly.
    try:
        probe = market_data.analyst_target('AAPL')
        rate_limited = 'ratelimit' in (probe['mean'].reason or '').lower() if not probe['mean'].ok else False
        results['yfinance'] = {
            'reachable': probe['mean'].ok or not rate_limited,
            'rate_limited': rate_limited,
            'detail': 'ok' if probe['mean'].ok else (probe['mean'].reason or 'unavailable'),
        }
    except Exception as e:
        results['yfinance'] = {'reachable': False, 'error': type(e).__name__}

    degraded = sorted(name for name, r in results.items() if not r["reachable"])

    return {
        "status": "degraded" if degraded else "ok",
        "degraded_sources": degraded,
        "sources": results,
        "timestamp": datetime.now().isoformat(),
    }, (200 if not degraded else 207)


@app.route('/metrics')
@rate_limit(MAX_REQUESTS_PER_MINUTE)
def metrics():
    """Resource monitoring endpoint"""
    memory = get_memory_usage()
    cache_info = get_cache_status()
    
    return jsonify({
        "memory": {
            "rss_mb": round(memory['rss'], 1),
            "vms_mb": round(memory['vms'], 1), 
            "percent": round(memory['percent'], 1)
        },
        "cache": {
            "exists": cache_info.get("exists", False),
            "age_hours": cache_info.get("age_hours", 0) if cache_info.get("exists") else None,
            "size_mb": cache_info.get("size_mb", 0) if cache_info.get("exists") else None
        },
        "rate_limiting": {
            "active_ips": len(request_counts),
            "max_per_minute": MAX_REQUESTS_PER_MINUTE,
            "ai_max_per_minute": MAX_AI_REQUESTS_PER_MINUTE
        },
        "timestamp": datetime.now().isoformat()
    })

@app.route('/refresh')
def refresh_cache():
    """Clear the rendered page so the next load rebuilds it from warm data.

    Earlier behaviour cleared the Redis page cache and then reported "No Cache
    Found -- data is already fresh!" because it only checked the legacy cache
    file, which does not exist in deployment. The message said nothing was
    refreshed while the refresh was in fact happening. One code path now, one
    truthful message, and ?deep=1 still clears the provider cache for the rare
    case the upstream data itself is suspect.
    """
    try:
        cleared = []

        deep = request.args.get('deep') in ('1', 'true', 'yes')
        if deep:
            entries = market_data.clear_cache()
            cleared.append(f"provider cache ({entries} entries)")

        if USE_REDIS and redis_client is not None:
            try:
                if redis_client.delete('yahoo_losers_cache'):
                    cleared.append("rendered page (redis)")
            except Exception as e:
                logger.warning(f"Redis page cache clear failed: {type(e).__name__}")

        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
            cleared.append("rendered page (file)")

        message = ", ".join(cleared) if cleared else "nothing was cached"
        logger.info(f"Manual refresh cleared: {message}")
        detail = ("The next page load rebuilds from the warm provider cache "
                  "(a few seconds). Provider data refreshes on its own schedule; "
                  "add <code>?deep=1</code> to force that too.")
        return f"""
        <html>
            <head><title>Refreshed</title><meta name="viewport" content="width=device-width, initial-scale=1"></head>
            <body style="font-family: -apple-system, system-ui, sans-serif; text-align: center; margin: 50px auto; max-width: 520px; background: #0d1117; color: #e6edf3;">
                <div style="background: #161b22; border: 1px solid #30363d; padding: 36px; border-radius: 10px;">
                    <h1 style="color: #2ecc71; font-size: 1.4rem;">&#10227; Refresh done</h1>
                    <p style="color: #8b949e;">Cleared: {message}.</p>
                    <p style="color: #8b949e; font-size: 14px;">{detail}</p>
                    <a href='/' style="background-color: #6c5ce7; color: white; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block; margin-top: 14px;">
                        Reload dashboard
                    </a>
                </div>
            </body>
        </html>
        """
    except Exception as e:
        logger.error(f"Refresh failed: {type(e).__name__}: {e}")
        return f"Refresh failed: {type(e).__name__}", 500


def is_market_holiday(check_date):
    """Check if a given date is a US stock market holiday"""
    year = check_date.year
    month = check_date.month
    day = check_date.day
    
    # Fixed holidays
    fixed_holidays = [
        (1, 1),   # New Year's Day
        (7, 4),   # Independence Day
        (12, 25), # Christmas Day
    ]
    
    if (month, day) in fixed_holidays:
        return True
    
    # MLK Day - 3rd Monday in January
    if month == 1:
        third_monday = 15 + (7 - date(year, 1, 15).weekday()) % 7
        if day == third_monday:
            return True
    
    # Presidents Day - 3rd Monday in February
    if month == 2:
        third_monday = 15 + (7 - date(year, 2, 15).weekday()) % 7
        if day == third_monday:
            return True
    
    # Memorial Day - Last Monday in May
    if month == 5:
        last_monday = 31
        while date(year, 5, last_monday).weekday() != 0:
            last_monday -= 1
        if day == last_monday:
            return True
    
    # Labor Day - 1st Monday in September
    if month == 9:
        first_monday = 1
        while date(year, 9, first_monday).weekday() != 0:
            first_monday += 1
        if day == first_monday:
            return True
    
    # Thanksgiving - 4th Thursday in November
    if month == 11:
        fourth_thursday = 22 + (3 - date(year, 11, 22).weekday()) % 7
        if day == fourth_thursday:
            return True
    
    # Black Friday - Day after Thanksgiving (half day, treat as closed)
    if month == 11:
        fourth_thursday = 22 + (3 - date(year, 11, 22).weekday()) % 7
        if day == fourth_thursday + 1:
            return True
    
    # Christmas Eve - Dec 24 (often early close, treat as closed)
    if month == 12 and day == 24:
        return True
    
    # New Year's Eve - Dec 31 (often early close, treat as closed)  
    if month == 12 and day == 31:
        return True
    
    # Good Friday (complex calculation - approximate)
    # Easter Sunday calculation then subtract 2 days
    if month == 3 or month == 4:
        # Simplified Good Friday check (falls between March 20 - April 23)
        # This is a basic approximation - could be made more precise
        if month == 3 and day >= 20:
            # Could be Good Friday in late March
            pass
        elif month == 4 and day <= 23:
            # Could be Good Friday in April  
            pass
    
    return False

def get_market_status():
    """Get current market status (open/closed) including holidays"""
    import pytz
    try:
        # Get current time in EST (NYSE timezone)
        est = pytz.timezone('America/New_York')
        now_est = datetime.now(est)
        
        # Check if it's a weekday (Monday = 0, Sunday = 6)
        if now_est.weekday() >= 5:  # Weekend
            return {
                "status": "closed",
                "message": "🔴 Markets Closed (Weekend)",
                "next_open": "Next trading day: Monday 9:30 AM EST"
            }
        
        # Check if it's a market holiday
        if is_market_holiday(now_est.date()):
            return {
                "status": "closed",
                "message": "🔴 Markets Closed (Holiday)",
                "next_open": "Next trading day: Check market calendar"
            }
        
        # Check market hours (9:30 AM - 4:00 PM EST)
        market_open = now_est.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now_est.replace(hour=16, minute=0, second=0, microsecond=0)
        
        if now_est < market_open:
            return {
                "status": "closed", 
                "message": "🔴 Markets Closed (Pre-Market)",
                "next_open": f"Opens today at 9:30 AM EST"
            }
        elif now_est > market_close:
            return {
                "status": "closed",
                "message": "🔴 Markets Closed (After-Hours)", 
                "next_open": "Opens tomorrow at 9:30 AM EST"
            }
        else:
            seconds_to_close = int((market_close - now_est).total_seconds())
            
            # Smart time unit selection
            if seconds_to_close >= 3600:  # 1+ hours
                hours = seconds_to_close // 3600
                minutes = (seconds_to_close % 3600) // 60
                if hours == 1:
                    time_display = f"Closes in 1 hour {minutes} minutes" if minutes > 0 else "Closes in 1 hour"
                else:
                    time_display = f"Closes in {hours} hours {minutes} minutes" if minutes > 0 else f"Closes in {hours} hours"
            elif seconds_to_close >= 60:  # 1+ minutes
                minutes = seconds_to_close // 60
                time_display = f"Closes in {minutes} minute{'s' if minutes != 1 else ''}"
            else:  # Less than 1 minute
                time_display = f"Closes in {seconds_to_close} second{'s' if seconds_to_close != 1 else ''}"
            
            return {
                "status": "open",
                "message": f"🟢 Markets Open",
                "time_to_close": time_display
            }
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        logger.warning(f"Market status unavailable: {type(e).__name__}: {e}")
        return {
            "status": "unknown",
            "message": "❓ Market Status Unknown", 
            "next_open": "Check market hours manually"
        }

def get_comprehensive_market_analysis():
    """Get comprehensive market analysis with detailed insights and explanations"""
    try:
        analysis = {
            'vix_analysis': {},
            'market_trend': {},
            'volatility_regime': {},
            'sector_rotation': {},
            'recommendation_logic': {},
            'ai_insights': {}
        }
        
        # 1. VIX ANALYSIS
        #
        # Read from the warmer-maintained cache; this was the last direct
        # yfinance call left on the render path, and under the per-IP limiter
        # it failed into "N/A / Unable to fetch VIX data" while every other
        # number on the page worked.
        try:
            vix_hist_sourced = market_data.price_history("^VIX", allow_fetch=False)
            if not vix_hist_sourced.ok or len(vix_hist_sourced.value) < 2:
                raise LookupError(vix_hist_sourced.reason or "VIX closes not cached yet")
            vix_closes = vix_hist_sourced.value
            current_vix = float(vix_closes[-1])
            prev_vix = float(vix_closes[-2])
            vix_change = current_vix - prev_vix
            
            # VIX interpretation
            if current_vix < 15:
                vix_regime = "Ultra-Low Volatility"
                vix_description = "Market complacency at extreme levels. Limited opportunities for sharp reversals."
                vix_color = "#28a745"
                recovery_impact = "Very Limited - Stocks tend to grind rather than snap back sharply."
            elif current_vix < 20:
                vix_regime = "Low Volatility"  
                vix_description = "Calm market conditions with steady, predictable price action."
                vix_color = "#6c757d"
                recovery_impact = "Limited - Few dramatic recovery opportunities in calm conditions."
            elif current_vix < 25:
                vix_regime = "Normal Volatility"
                vix_description = "Healthy market volatility providing good trading opportunities."
                vix_color = "#ffc107" 
                recovery_impact = "Moderate - Normal reversal patterns and recovery opportunities."
            elif current_vix < 35:
                vix_regime = "Elevated Volatility"
                vix_description = "Market concern creating increased reversal opportunities."
                vix_color = "#fd7e14"
                recovery_impact = "High - Fear-driven selloffs often create strong bounce-back potential."
            else:
                vix_regime = "Extreme Volatility"
                vix_description = "Panic conditions creating exceptional reversal opportunities."
                vix_color = "#dc3545"
                recovery_impact = "Very High - Panic selling often followed by sharp recoveries."
            
            analysis['vix_analysis'] = {
                'current_vix': round(current_vix, 2),
                'previous_vix': round(prev_vix, 2),
                'change': round(vix_change, 2),
                'regime': vix_regime,
                'description': vix_description,
                'color': vix_color,
                'recovery_impact': recovery_impact,
                'interpretation': f"VIX at {current_vix:.1f} indicates {vix_regime.lower()} market conditions."
            }
        except Exception as e:
            logger.warning(f"VIX analysis unavailable: {type(e).__name__}: {e}")
            analysis['vix_analysis'] = {
                'current_vix': 'N/A',
                'regime': 'Unknown',
                'description': 'VIX cache still warming — refresh in about a minute',
                'color': '#6c757d',
                'recovery_impact': 'Unable to determine',
                'interpretation': 'VIX data unavailable'
            }
        
        # 2. MARKET TREND ANALYSIS (cache-only, same reasoning as the VIX block)
        try:
            spy_sourced = market_data.price_history("SPY", allow_fetch=False)
            spy_closes = spy_sourced.value if spy_sourced.ok else []

            if len(spy_closes) > 5:
                current_spy = float(spy_closes[-1])
                week_ago_spy = float(spy_closes[-5])
                month_start = float(spy_closes[-21]) if len(spy_closes) >= 21 else float(spy_closes[0])
                month_change = ((current_spy - month_start) / month_start) * 100
                week_change = ((current_spy - week_ago_spy) / week_ago_spy) * 100
                
                if week_change > 2:
                    trend = "Strong Bullish"
                    trend_description = "Market showing strong upward momentum, reducing oversold recovery potential."
                    trend_color = "#28a745"
                elif week_change > 0.5:
                    trend = "Moderately Bullish"
                    trend_description = "Positive market trend with some recovery opportunities in laggards."
                    trend_color = "#20c997"
                elif week_change > -0.5:
                    trend = "Neutral/Sideways"
                    trend_description = "Range-bound market creating stock-specific opportunities."
                    trend_color = "#6c757d"
                elif week_change > -2:
                    trend = "Moderately Bearish"
                    trend_description = "Market weakness creating selective recovery opportunities."
                    trend_color = "#fd7e14"
                else:
                    trend = "Strong Bearish"
                    trend_description = "Broad market decline creating significant oversold conditions."
                    trend_color = "#dc3545"
                
                analysis['market_trend'] = {
                    'current_price': round(current_spy, 2),
                    'week_change': round(week_change, 2),
                    'month_change': round(month_change, 2),
                    'trend': trend,
                    'description': trend_description,
                    'color': trend_color,
                    'interpretation': f"SPY {week_change:+.2f}% this week indicates {trend.lower()} conditions."
                }
            else:
                raise Exception("Insufficient SPY data")
        except Exception as e:
            logger.warning(f"SPY trend unavailable: {type(e).__name__}: {e}")
            analysis['market_trend'] = {
                'trend': 'Unknown',
                'description': 'Unable to analyze market trend',
                'color': '#6c757d',
                'interpretation': 'Market trend data unavailable'
            }
        
        # 3. AI RECOMMENDATION LOGIC EXPLANATION
        analysis['recommendation_logic'] = {
            'title': 'Why No Strong Recommendations Today?',
            'criteria': [
                {
                    'requirement': 'STRONG BUY Signals',
                    'threshold': 'Contains "STRONG BUY" in AI recommendation',
                    'current_status': 'Most stocks showing "WAIT & WATCH" or "AVOID"',
                    'explanation': 'AI requires high conviction signals, not moderate opportunities'
                },
                {
                    'requirement': 'Market Volatility',
                    'threshold': 'VIX > 25 for elevated opportunities', 
                    'current_status': f'VIX = {analysis["vix_analysis"]["current_vix"]} (low volatility)',
                    'explanation': 'Low volatility limits dramatic recovery potential'
                }
            ],
            'summary': f'In current {analysis["vix_analysis"]["regime"].lower()} conditions, the AI correctly avoids recommending mediocre opportunities. This conservative approach protects against false positives during uncertain market periods.'
        }
        
        # 4. DETAILED INSIGHTS
        analysis['ai_insights'] = {
            'market_regime_impact': f'Current {analysis["vix_analysis"]["regime"]} regime means fewer stocks meet our strict quality criteria.',
            'opportunity_outlook': 'Look for recommendations during periods of VIX > 25 or strong market corrections.',
            'quality_over_quantity': 'Zero recommendations is better than poor recommendations. The system prioritizes high-conviction opportunities.',
            'when_to_expect_signals': 'Strong buy signals typically emerge during: Market corrections (SPY -3%+), Elevated VIX (25+), Earnings surprises, or Sector rotation events.'
        }
        
        return analysis
        
    except Exception as e:
        logger.error(f"Error in comprehensive market analysis: {e}")
        return {
            'vix_analysis': {'regime': 'Unknown', 'description': 'Analysis unavailable', 'color': '#6c757d'},
            'market_trend': {'trend': 'Unknown', 'description': 'Analysis unavailable', 'color': '#6c757d'},
            'recommendation_logic': {'summary': 'Analysis unavailable due to data issues'},
            'ai_insights': {'market_regime_impact': 'Unable to analyze current conditions'}
        }

@app.route('/export/csv')
def export_csv():
    """Export current data to CSV format"""
    try:
        # Try to get data from cache first
        cache_data = load_cache()
        if cache_data:
            data = cache_data['data']
            losers_data = data['losers_data']
            all_analysis = data['all_analysis']
        else:
            # If no cache, get fresh data
            losers_data, _ = scrape_yahoo_losers()
            symbols = [stock['Symbol'] for stock in losers_data]
            details_data = get_stock_details(symbols)
            all_analysis = calculate_enhanced_investment_analysis(losers_data, details_data)
        
        # Create CSV data
        import io
        from flask import Response
        
        output = io.StringIO()
        
        # Write header
        output.write("Symbol,Company Name,Rebound Score,Confidence,AI Technical Sentiment,Current Price,Target Price,Potential Return %,P Prev Close 7d,P MA20 21d,P Target 6mo,Target Source,Change Today,Percent Change Today,Volume,Market Cap\n")
        
        # Write data rows.
        #
        # These cells were previously built by calling .replace() directly on the
        # values, which raised AttributeError as soon as one was a float -- and
        # 'Current Price' is a float, so the whole export returned an error page.
        # cell() coerces first, so a numeric value can no longer break the export.
        def cell(analysis, key, strip=''):
            value = analysis.get(key, '')
            text = '' if value is None else str(value)
            for char in strip:
                text = text.replace(char, '')
            return text.replace(',', ';')

        for analysis in all_analysis:
            row = [
                cell(analysis, 'Symbol'),
                cell(analysis, 'Name'),
                cell(analysis, 'Rebound Score'),
                cell(analysis, 'Confidence'),
                cell(analysis, 'AI Sentiment'),
                cell(analysis, 'Current Price', '$'),
                cell(analysis, 'Target Price', '$'),
                cell(analysis, 'Potential Return %', '%'),
                str((analysis.get('P Short') or {}).get('display', '')).replace(',', ';'),
                str((analysis.get('P Medium') or {}).get('display', '')).replace(',', ';'),
                str((analysis.get('P Long') or {}).get('display', '')).replace(',', ';'),
                cell(analysis, 'Target Source'),
                cell(analysis, 'Change Today', '$'),
                cell(analysis, 'Percent Change Today', '%'),
                cell(analysis, 'Volume'),
                cell(analysis, 'Market Cap'),
            ]
            output.write(','.join(row) + '\n')
        
        # Prepare response
        csv_data = output.getvalue()
        output.close()
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'yahoo_losers_{timestamp}.csv'
        
        return Response(
            csv_data,
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
        
    except Exception as e:
        logger.error(f"Error exporting CSV: {str(e)}")
        return f"""
        <html>
            <head><title>Export Error</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; margin: 50px; background-color: #f5f5f5;">
                <div style="background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 500px; margin: 0 auto;">
                    <h1 style="color: #dc3545;">❌ Export Error</h1>
                    <p>Error exporting data: {str(e)}</p>
                    <a href='/' style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block; margin-top: 20px;">
                        🏠 Go Back Home
                    </a>
                </div>
            </body>
        </html>
        """

@app.route('/api/recovery-prediction/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_recovery_prediction(symbol):
    """AI-powered recovery prediction for a stock"""
    try:
        prediction = predict_stock_recovery(symbol)
        
        api_response = {
            "symbol": symbol,
            "prediction": prediction,
            "timestamp": time.time()
        }
        
        # Add HTTP caching with ETag
        etag = generate_etag(api_response)
        if request.headers.get('If-None-Match') == etag:
            response = make_response('', 304)
            response.headers['ETag'] = etag
            return response
            
        response = make_response(json.dumps(api_response))
        response.headers['Content-Type'] = 'application/json'
        response.headers['ETag'] = etag
        return add_cache_headers(response, max_age=60)
        
    except Exception as e:
        logger.error(f"Error predicting recovery for {symbol}: {str(e)}")
        return jsonify({
            "symbol": symbol,
            "prediction": {
                "recovery_score": 0,
                "confidence": "low",
                "timeframe": "unknown",
                "risk_level": "high",
                "factors": [],
                "recommendation": "Unable to analyze recovery potential"
            },
            "error": str(e)
        })

@app.route('/api/sophisticated-timeframe/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_sophisticated_timeframe(symbol):
    """🚀 Advanced timeframe prediction with multiple targets and market dynamics"""
    try:
        logger.info(f"🔥 Sophisticated timeframe API called for {symbol}")
        
        # DEBUG: Test the REAL sophisticated predictor
        print(f"DEBUG: Testing real sophisticated_predictor.predict_recovery_timeframes({symbol})")
        
        try:
            # Get the ACTUAL sophisticated analysis - no fake data!
            sophisticated_result = _sophisticated_cached(symbol.upper())
            print(f"DEBUG: sophisticated_predictor SUCCESS! Result type: {type(sophisticated_result)}")
            print(f"DEBUG: sophisticated_result keys: {list(sophisticated_result.keys()) if isinstance(sophisticated_result, dict) else 'Not a dict'}")
            
            # Get the ACTUAL prediction summary - no fake data!
            prediction_summary = predict_stock_recovery(symbol.upper())
            print(f"DEBUG: predict_stock_recovery SUCCESS! Result type: {type(prediction_summary)}")
            print(f"DEBUG: prediction_summary keys: {list(prediction_summary.keys()) if isinstance(prediction_summary, dict) else 'Not a dict'}")
            
        except Exception as e:
            print(f"CRITICAL ERROR in real analysis: {str(e)}")
            import traceback
            traceback.print_exc()
            
            # Return an error response instead of fake data
            return jsonify({
                "symbol": symbol.upper(),
                "error": f"Real sophisticated analysis failed: {str(e)}",
                "debug_info": "Check server logs for full traceback",
                "api_version": "2.0"
            }), 500
        
        # Add long-term analysis if missing (sophisticated_predictor doesn't generate it)
        if 'timeframe_predictions' in sophisticated_result and 'long_term' not in sophisticated_result['timeframe_predictions']:
            print("DEBUG: Adding long-term analysis - not present in sophisticated_predictor")
            
            # Get current price for calculating targets
            current_price = sophisticated_result.get('current_price', 0)
            print(f"DEBUG: Current price for long-term analysis: {current_price}")
            
            if current_price > 0:
                # Create realistic long-term analysis based on analyst estimates and market data
                long_term_analysis = {}
                
                # Try to get analyst price target from the stock details
                try:
                    symbols = [symbol.upper()]
                    # Real analyst figures: the consensus mean and the actual
                    # published high estimate. The previous "Bull Case Growth
                    # Scenario" was current_price * 1.6 with a hard-coded 35%
                    # probability -- an invented target, which is why it kept
                    # landing below the real consensus it was meant to exceed.
                    analyst = market_data.analyst_target(symbol)
                    if analyst['mean'].ok and current_price > 0:
                        mean_price = analyst['mean'].value
                        mean_upside = ((mean_price - current_price) / current_price) * 100
                        if mean_upside > 0:
                            long_term_analysis['analyst_consensus'] = {
                                "target_price": round(mean_price, 2),
                                "upside_percent": round(mean_upside, 2),
                                "timeframe": "6-12 months",
                                "analysts": analyst['analysts'].value,
                                "description": f"Analyst consensus ({analyst['analysts'].value} analysts)",
                                "source": analyst['mean'].source,
                            }

                    if analyst['high'].ok and current_price > 0:
                        high_price = analyst['high'].value
                        high_upside = ((high_price - current_price) / current_price) * 100
                        if high_upside > 0:
                            long_term_analysis['analyst_high'] = {
                                "target_price": round(high_price, 2),
                                "upside_percent": round(high_upside, 2),
                                "timeframe": "6-12 months",
                                "description": "Highest published analyst target",
                                "source": analyst['high'].source,
                            }

                    if analyst['low'].ok and current_price > 0:
                        low_price = analyst['low'].value
                        low_upside = ((low_price - current_price) / current_price) * 100
                        if low_upside > 0:
                            long_term_analysis['analyst_low'] = {
                                "target_price": round(low_price, 2),
                                "upside_percent": round(low_upside, 2),
                                "timeframe": "6-12 months",
                                "description": "Lowest published analyst target",
                                "source": analyst['low'].source,
                            }
                except Exception as e:
                    logger.warning(f"Long-term analyst targets unavailable for {symbol}: "
                                   f"{type(e).__name__}: {e}")

                # Only add long_term if we have at least one target
                if long_term_analysis:
                    history = market_data.price_history(symbol)
                    if history.ok:
                        long_term_analysis = timeframes.annotate_targets(
                            np.array(history.value, dtype=float), long_term_analysis, 'long')
                    sophisticated_result['timeframe_predictions']['long_term'] = long_term_analysis
                    print(f"DEBUG: Successfully added long_term analysis with {len(long_term_analysis)} targets")
                else:
                    print("DEBUG: No long_term targets could be generated")
            else:
                print("DEBUG: No valid current price for long-term analysis")
        
        api_response = {
            "symbol": symbol.upper(),
            "prediction": prediction_summary,  # Frontend compatible format
            "sophisticated_analysis": sophisticated_result,  # REAL sophisticated analysis
            "api_version": "2.0",
            "description": "Real sophisticated recovery timeframe prediction",
            "timestamp": time.time()
        }
        
        # Add HTTP caching with ETag
        etag = generate_etag(api_response)
        if request.headers.get('If-None-Match') == etag:
            response = make_response('', 304)
            response.headers['ETag'] = etag
            return response
            
        # Custom JSON encoder to handle NumPy types
        def json_serializer(obj):
            if hasattr(obj, 'item'):  # NumPy types
                return obj.item()
            elif hasattr(obj, 'tolist'):  # NumPy arrays
                return obj.tolist()
            raise TypeError(f'Object of type {type(obj)} is not JSON serializable')
        
        response = make_response(json.dumps(api_response, indent=2, default=json_serializer))
        response.headers['Content-Type'] = 'application/json'
        response.headers['ETag'] = etag
        return add_cache_headers(response, max_age=60)
        
    except Exception as e:
        logger.error(f"Sophisticated timeframe API error for {symbol}: {str(e)}")
        
        # Provide both prediction (frontend format) and analysis (detailed format) even on error
        fallback_prediction = {
            "recovery_score": 0,
            "confidence": "low",
            "timeframe": "unknown",
            "risk_level": "high",
            "recommendation": "Analysis unavailable",
            "factors": {"technical": [], "historical": [], "fundamental": [], "news": []},
            "current_drop": 0
        }
        
        return jsonify({
            "symbol": symbol.upper(),
            "prediction": fallback_prediction,  # Frontend compatible format
            "sophisticated_analysis": {  # Detailed format
                "symbol": symbol.upper(),
                "current_price": 0,
                "targets": {},
                "timeframe_predictions": {},
                "confidence_level": "Low",
                "error": str(e)
            },
            "api_version": "2.0",
            "error": "Analysis failed - using fallback data"
        })

@app.route('/api/social-sentiment/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_social_sentiment(symbol):
    """Get social media sentiment for a stock"""
    try:
        sentiment = analyze_social_sentiment(symbol)
        
        return jsonify({
            "symbol": symbol,
            "sentiment": sentiment,
            "timestamp": time.time()
        })
        
    except Exception as e:
        logger.error(f"Error analyzing social sentiment for {symbol}: {str(e)}")
        return jsonify({
            "symbol": symbol,
            # An error path must not emit zeroed counts that read as real
            # observations of a quiet stock.
            "sentiment": {"label": "Unavailable", "color": "#6c757d",
                          "reason": f"request failed ({type(e).__name__})"},
            "trending_phrases": [],
            "available": False,
            "error": str(e)
        })

@app.route('/api/news-analysis/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_news_analysis(symbol):
    """AI-powered news analysis for a specific stock symbol"""
    try:
        # Get real AI analysis from news APIs and financial data
        analysis = analyze_stock_news(symbol)
        
        return jsonify({
            "symbol": symbol,
            "analysis": analysis,
            "timestamp": time.time()
        })
        
    except Exception as e:
        logger.error(f"Error analyzing news for {symbol}: {str(e)}")
        return jsonify({
            "symbol": symbol,
            "analysis": {
                "sentiment": "unknown",
                "reason": "Unable to analyze news at this time",
                "confidence": 0,
                "news_count": 0,
                "icon": "❓"
            },
            "error": str(e)
        })


# Why-it-fell buckets, display-only. Keyword matching over real headlines; the
# label carries no scoring weight until the snapshot record is deep enough to
# measure whether recovery odds actually differ by reason.
FALL_REASONS = [
    ("Earnings miss", ("miss", "misses", "falls short", "disappoint", "q1", "q2", "q3", "q4", "quarterly results", "earnings call")),
    ("Guidance cut", ("guidance", "outlook", "forecast", "lowers", "cuts forecast", "warns")),
    ("Dilution / offering", ("offering", "dilution", "share sale", "secondary", "convertible", "priced", "raises capital")),
    ("Analyst downgrade", ("downgrade", "downgrades", "cut to", "lowers rating", "underweight", "sell rating")),
    ("Legal / regulatory", ("lawsuit", "probe", "investigation", "sec ", "fda", "recall", "fine", "settlement")),
    ("Sector / market move", ("sector", "peers", "market", "sympathy", "broad", "tariff", "rates", "macro")),
]


def classify_fall_reason(headlines):
    """Best-guess reason for the drop, from real headline text, with the match."""
    text = " ".join((h.get("title") or "") for h in (headlines or [])).lower()
    if not text.strip():
        return {"label": "No headlines", "matched": None, "basis": "no recent headlines to classify"}
    for label, needles in FALL_REASONS:
        for needle in needles:
            if needle in text:
                return {"label": label, "matched": needle,
                        "basis": "keyword match over recent headlines; display-only, carries no scoring weight"}
    return {"label": "Unclassified", "matched": None,
            "basis": "no known pattern matched the recent headlines"}


def analyze_stock_news(symbol):
    """Recent headlines plus the current analyst rating spread.

    The old version never read news despite its name -- it inspected
    recommendation trends and returned a prose "reason" with a hard-coded
    confidence of 70. When the endpoint began returning 401 it always emitted
    the same sentence, "Broader market pressures affecting stock performance",
    for every stock, with that same invented confidence.

    Two separable things are reported now, each labelled with its source:
    the actual headlines, and where analysts currently stand. No confidence
    score is attached, because nothing here measures one.
    """
    news = market_data.headlines(symbol, limit=5)
    ratings = market_data.analyst_recommendations(symbol)

    payload = {
        "symbol": symbol,
        "timestamp": datetime.now().isoformat(),
        "headlines": {
            "available": news.ok,
            "source": news.source,
            "items": news.value if news.ok else [],
            "count": len(news.value) if news.ok else 0,
            "reason": None if news.ok else news.reason,
        },
        "fall_reason": classify_fall_reason(news.value if news.ok else []),
        "analyst_posture": {
            "available": ratings.ok,
            "source": ratings.source,
            "spread": ratings.value if ratings.ok else None,
            "reason": None if ratings.ok else ratings.reason,
        },
    }

    if ratings.ok:
        spread = ratings.value
        bullish = spread.get("strongBuy", 0) + spread.get("buy", 0)
        bearish = spread.get("sell", 0) + spread.get("strongSell", 0)
        total = spread.get("total", 0)
        if bearish > bullish:
            posture, icon = "net negative", "📉"
        elif bullish > bearish:
            posture, icon = "net positive", "📈"
        else:
            posture, icon = "split", "📊"
        payload["analyst_posture"]["summary"] = (
            f"{icon} {bullish} buy vs {bearish} sell of {total} ratings ({posture})"
        )

    if news.ok:
        payload["summary"] = f"{payload['headlines']['count']} recent headlines"
    elif ratings.ok:
        payload["summary"] = payload["analyst_posture"]["summary"]
    else:
        payload["summary"] = "No headlines or analyst ratings available"

    return payload

def _attach_empirical_probabilities(symbol, sophisticated_result):
    """Swap invented target probabilities for measured historical hit rates."""
    if not sophisticated_result:
        return sophisticated_result

    history = market_data.price_history(symbol)
    if not history.ok:
        # Without history there is no evidence, so the targets are returned
        # with their probabilities marked unavailable rather than guessed.
        for band in (sophisticated_result.get('timeframe_predictions') or {}).values():
            if isinstance(band, dict):
                for target in band.values():
                    if isinstance(target, dict):
                        target['probability_available'] = False
                        target['probability_reason'] = history.reason
                        target.pop('probability', None)
        return sophisticated_result

    closes = np.array(history.value, dtype=float)
    band_for = {'short_term': 'short', 'medium_term': 'medium', 'long_term': 'long'}

    predictions = sophisticated_result.get('timeframe_predictions') or {}
    for band_key, targets in predictions.items():
        if not isinstance(targets, dict):
            continue
        predictions[band_key] = timeframes.annotate_targets(
            closes, targets, band_for.get(band_key, 'medium'))

    sophisticated_result['probability_basis'] = (
        'Measured frequency with which this stock reached each target within '
        'the horizon, over its own price history. Not a forecast.')
    return sophisticated_result



def _sophisticated_cached(symbol):
    """Compute the timeframe analysis once per symbol per interval.

    The predictor fetches its own inputs from Yahoo -- a year of history, VIX,
    SPY, a sector ETF -- unthrottled, on every modal open. Under the per-IP
    limiter that burst intermittently failed, the predictor fell back to an
    empty result, and the tabs truthfully rendered "no targets" for stocks
    whose data existed moments earlier. Caching the finished result makes one
    computation serve every open for the next half hour.

    A result with no timeframe predictions is the fallback shape produced when
    the predictor was rate-limited mid-computation; it is held only briefly so
    the next open retries instead of pinning an empty page for half an hour.
    """
    key = f"stf:{symbol.upper()}"
    cached = market_data._cache.get(key)
    if cached is not None:
        return cached

    # Cached history keeps the predictor off the network for its main input;
    # profile comes from the same cached blob the table already uses. Only the
    # market-condition context (VIX, SPY) still fetches, and those degrade to
    # neutral rather than empty when unavailable.
    frame = market_data.ohlcv_frame(symbol.upper())
    info_payload = market_data._info(symbol.upper(), allow_fetch=False)
    # The cached blob uses this codebase's names; the predictor reads yfinance's.
    preloaded_info = {}
    if info_payload.get("ok"):
        preloaded_info = {
            "sector": info_payload.get("sector"),
            "industry": info_payload.get("industry"),
            "trailingPE": info_payload.get("trailing_pe"),
            "sharesShort": info_payload.get("shares_short"),
            "shortPercentOfFloat": info_payload.get("short_pct_float"),
            "averageVolume": info_payload.get("avg_volume"),
        }
    result = sophisticated_predictor.predict_recovery_timeframes(
        symbol.upper(), preloaded_hist=frame,
        preloaded_info=preloaded_info or None)
    result = _attach_empirical_probabilities(symbol.upper(), result)

    bands = (result or {}).get('timeframe_predictions') or {}
    populated = any(v for v in bands.values() if v)
    ttl = market_data._effective_ttl(30 * 60) if populated else 90
    market_data._cache.set(key, result, ttl)
    return result


def predict_stock_recovery(symbol):
    """
    🚀 SOPHISTICATED RECOVERY PREDICTION using advanced market dynamics
    Uses real historical patterns, market conditions, and multiple recovery targets
    """
    try:
        logger.info(f"🔥 SOPHISTICATED ANALYSIS for {symbol} - Advanced timeframe prediction!")
        print(f"DEBUG: Starting recovery prediction for {symbol}")  # Debug
        
        # Get sophisticated analysis using our new system
        sophisticated_result = _sophisticated_cached(symbol)

        # Replace the invented probabilities with measured ones.
        #
        # The targets this module produces are real -- previous close, moving
        # averages, analyst consensus. The probability attached to each was not:
        # it began at a hard-coded 70, took fixed integer adjustments, was
        # multiplied by a "signal multiplier" and capped at 95. The cap was hit
        # constantly, so every target on a page showed an identical 95%.
        #
        # Each target now carries the frequency with which this stock actually
        # reached that gain within the horizon, measured over its own history
        # and reported with the sample size behind it. The signal multiplier is
        # deliberately not applied: multiplying a measured frequency by an
        # unfitted constant would destroy the one thing that makes it real.
        sophisticated_result = _attach_empirical_probabilities(symbol, sophisticated_result)
        
        # Convert sophisticated results back to format expected by existing app
        timeframe_predictions = sophisticated_result.get('timeframe_predictions', {})
        
        # Extract short-term predictions (the new structure has short_term and medium_term)
        short_term_predictions = timeframe_predictions.get('short_term', {})
        
        # Determine primary recovery target and timeframe with detailed explanation
        primary_target = None
        primary_timeframe = "uncertain"  # default for unclear situations
        target_description = "Unknown target"
        
        # Priority order: TRUE short-term technical targets only (1-5 days max)
        priority_targets = ['previous_close', '5day_high', '10day_ma', 'intraday_resistance', 'gap_fill']
        target_names = {
            'previous_close': "previous close",
            '5day_high': "5-day high", 
            '10day_ma': "10-day moving average",
            'intraday_resistance': "intraday resistance",
            'gap_fill': "gap fill level"
        }
        
        # Get current price from sophisticated analysis
        current_price = sophisticated_result.get('current_price', 0)
        
        for target_name in priority_targets:
            if target_name in short_term_predictions:
                target_data = short_term_predictions[target_name]
                if target_data.get('upside_percent', 0) > 0:  # Only positive upside targets
                    primary_target = target_data
                    target_price = target_data.get('target_price', 0)
                    upside_percent = target_data.get('upside_percent', 0)
                    upside_dollars = target_price - current_price if current_price > 0 else 0
                    timeframe = target_data.get('timeframe', 'unknown')
                    probability = target_data.get('probability', 0)
                    
                    # Create detailed timeframe description
                    target_description = target_names.get(target_name, target_name.replace('_', ' '))
                    primary_timeframe = f"{timeframe} to reach {target_description} (${target_price:.2f}, +{upside_percent:.1f}% or +${upside_dollars:.2f}) - {probability}% probability"
                    break
        
        # Get enhanced market analysis
        targets = sophisticated_result.get('targets', {})
        market_conditions = sophisticated_result.get('market_conditions', {})
        technical_momentum = sophisticated_result.get('technical_momentum', {})
        
        # Get additional market breadth and sector analysis from sophisticated predictor
        market_breadth = sophisticated_predictor._get_market_breadth()
        sector_analysis = sophisticated_predictor._get_enhanced_sector_analysis(
            sophisticated_result.get('sector_context', {})
        )
        
        # Use enhanced recovery score calculation (pass short-term predictions to match expected format)
        recovery_score, score_breakdown = sophisticated_predictor._calculate_enhanced_recovery_score(
            short_term_predictions, market_conditions, technical_momentum, sector_analysis, market_breadth
        )
        
        # Store breakdown for UI transparency
        base_recovery_score = score_breakdown.get('base_score', recovery_score)
        adjustment = score_breakdown.get('market_adjustment', 0)
        volatility_regime = score_breakdown.get('volatility_regime', 'normal')
        
        # Determine confidence and recommendation (LESS CONSERVATIVE THRESHOLDS)
        if recovery_score >= 65:  # Lowered from 75
            confidence = "very_high"
            risk_level = "low"
            recommendation = "🟢 STRONG BUY THE DIP - High recovery probability with favorable market conditions"
        elif recovery_score >= 50:  # Lowered from 60
            confidence = "high"
            risk_level = "moderate"
            recommendation = "🟡 MODERATE BUY - Good recovery chance with supportive factors"
        elif recovery_score >= 35:  # Lowered from 40
            confidence = "moderate"
            risk_level = "moderate"
            recommendation = "🟡 WAIT & WATCH - Monitor for improved conditions"
        else:
            confidence = "low"
            risk_level = "high"
            recommendation = "🔴 AVOID - Multiple headwinds present"
        
        # Adjust timeframe based on recommendation to ensure logical consistency
        # This prevents confusing scenarios like "AVOID" + short timeframes
        if recommendation.startswith("🔴 AVOID"):
            # For AVOID recommendations, add cautionary language
            if primary_target and primary_target.get('probability', 0) < 50:
                if "to reach" in primary_timeframe:
                    primary_timeframe = primary_timeframe.replace("to reach", "potential recovery to")
                    primary_timeframe += " - NOT RECOMMENDED due to low probability"
                else:
                    primary_timeframe = "uncertain conditions - Low probability recovery"
        elif recommendation.startswith("🟡 WAIT"):
            # For WAIT recommendations, add conditional language
            if "to reach" in primary_timeframe:
                primary_timeframe = primary_timeframe.replace("to reach", "potential recovery to")
                primary_timeframe += " - Monitor conditions before acting"
        
        # Build factors from sophisticated analysis
        technical_factors = []
        historical_factors = []
        fundamental_factors = []
        market_factors = []
        
        # Technical momentum factors
        technical_momentum = sophisticated_result.get('technical_momentum', {})
        rsi = technical_momentum.get('rsi', 50)
        if rsi < 30:
            technical_factors.append(f"🔴 Oversold (RSI: {rsi:.1f})")
        elif rsi < 40:
            technical_factors.append(f"🟡 Near Oversold (RSI: {rsi:.1f})")
        
        if technical_momentum.get('volume_surge', False):
            technical_factors.append("📊 High Volume Selloff")
        
        trend_strength = technical_momentum.get('trend_strength', 'weak')
        if trend_strength == 'strong':
            technical_factors.append("💪 Strong Technical Momentum")
        elif trend_strength == 'moderate':
            technical_factors.append("📊 Moderate Technical Momentum")
        
        # Historical pattern factors
        historical_patterns = sophisticated_result.get('historical_patterns', {})
        if historical_patterns.get('avg_recovery_days', 0) > 0:
            avg_days = historical_patterns['avg_recovery_days']
            historical_factors.append(f"📈 Historical avg recovery: {avg_days} days")
            
        success_rate = historical_patterns.get('historical_success_rate', 0)
        if success_rate > 70:
            historical_factors.append(f"✅ Strong recovery history ({success_rate:.0f}%)")
        elif success_rate > 50:
            historical_factors.append(f"📊 Moderate recovery history ({success_rate:.0f}%)")
        elif success_rate > 0:
            historical_factors.append(f"⚠️ Limited recovery history ({success_rate:.0f}%)")
        
        # Market condition factors
        vix_level = market_conditions.get('vix_level', 20)
        market_sentiment = market_conditions.get('market_sentiment', 'neutral')
        market_factors.append(f"📊 VIX: {vix_level} ({market_sentiment} sentiment)")
        
        spy_trend = market_conditions.get('spy_trend', 'neutral')
        if spy_trend == 'bullish':
            market_factors.append("📈 Bullish market environment")
        elif spy_trend == 'bearish':
            market_factors.append("📉 Bearish market pressure")
        else:
            market_factors.append("➡️ Neutral market conditions")
        
        # Sector context factors  
        sector_context = sophisticated_result.get('sector_context', {})
        sector_performance = sector_context.get('sector_performance', 'neutral')
        if sector_performance in ['strong', 'positive']:
            fundamental_factors.append(f"🏗️ Strong sector performance")
        elif sector_performance in ['negative', 'weak']:
            fundamental_factors.append(f"⚠️ Weak sector performance")
        else:
            fundamental_factors.append(f"📊 Mixed sector signals")
            
        # Catalyst factors
        catalysts = sophisticated_result.get('catalysts', {})
        if catalysts.get('has_upcoming_events', False):
            earnings_days = catalysts.get('earnings_days_away', 30)
            if earnings_days <= 7:
                fundamental_factors.append(f"📅 Earnings in {earnings_days} days (catalyst)")
            else:
                fundamental_factors.append(f"📅 Earnings in {earnings_days} days")
        
        # Calculate price drop for backward compatibility
        current_price = sophisticated_result.get('current_price', 0)
        price_drop = -5.0  # default estimate for losers list
        
        # Add sophisticated targets info to technical factors
        if primary_target:
            target_price = primary_target.get('target_price', 0)
            upside = primary_target.get('upside_percent', 0)
            probability = primary_target.get('probability', 0)
            technical_factors.append(f"🎯 Target: ${target_price} (+{upside:.1f}%) - {probability:.0f}% probability")
        
        # Add multiple target summary (using short-term predictions)
        target_count = len([t for t in short_term_predictions.values() if t.get('upside_percent', 0) > 0])
        if target_count > 1:
            technical_factors.append(f"📊 {target_count} recovery targets identified")
        
        # The enhanced score_breakdown is already calculated above from _calculate_enhanced_recovery_score()
        # Just add any additional context needed for the UI
        if not score_breakdown:
            score_breakdown = {
                "base_score": round(base_recovery_score, 1),
                "market_adjustment": adjustment,
                "volatility_regime": volatility_regime,
                "target_details": []
            }
        
        return {
            "recovery_score": round(recovery_score, 1),
            "confidence": confidence,
            "timeframe": primary_timeframe,
            "risk_level": risk_level,
            "recommendation": recommendation,
            "factors": {
                "technical": technical_factors[:6],  # Limit to 6 factors for UI
                "historical": historical_factors[:4],
                "fundamental": fundamental_factors[:4],
                "news": market_factors[:3]  # Use market factors as "news"
            },
            "current_drop": price_drop,
            # Add score breakdown for transparency
            "score_breakdown": score_breakdown,
            # Add sophisticated data for advanced endpoints
            "sophisticated_analysis": sophisticated_result
        }
        
    except Exception as e:
        logger.error(f"Sophisticated prediction failed for {symbol}: {e}")
        
        # Fallback to basic analysis
        return {
            "recovery_score": 45,
            "confidence": "low",
            "timeframe": "7-14 days",
            "risk_level": "moderate",
            "recommendation": "🟡 WAIT & WATCH - Analysis unavailable",
            "factors": {
                "technical": [f"⚠️ Analysis error: {str(e)}"],
                "historical": ["📊 Using fallback analysis"],
                "fundamental": ["⚠️ Limited data available"],
                "news": ["📊 Market conditions uncertain"]
            },
            "current_drop": -5.0
        }

def analyze_social_sentiment(symbol):
    """Social sentiment from StockTwits tags and, when configured, Reddit.

    Reports a measured ratio with its denominator instead of an unanchored
    1-10 "panic level". The old scale divided total mentions by 500, but Reddit
    search caps at 100 results and a StockTwits page holds ~30 messages, so it
    could never exceed roughly 0.26 and every stock rendered as calm.
    """
    # The company's own name is passed so it can be excluded from trending
    # phrases; StockTwits messages repeat it constantly and it crowds out any
    # actual market theme.
    name_sourced = market_data.profile(symbol).get('name')
    company_name = name_sourced.value if (name_sourced and name_sourced.ok) else None
    data = social.sentiment(symbol, company_name)
    overall = data['overall']

    payload = {
        "symbol": symbol,
        "timestamp": datetime.now().isoformat(),
        "available": overall['available'],
        "sources": {
            "stocktwits": {
                "available": data['stocktwits']['available'],
                "reason": data['stocktwits'].get('reason'),
                "messages": data['stocktwits'].get('messages'),
                "tagged": data['stocktwits'].get('tagged'),
                "bullish": data['stocktwits'].get('bullish'),
                "bearish": data['stocktwits'].get('bearish'),
            },
            "reddit": {
                "available": data['reddit']['available'],
                "reason": data['reddit'].get('reason'),
                "mentions": data['reddit'].get('mentions'),
                "capped": data['reddit'].get('capped'),
            },
        },
        # Real repeated phrases from message text, with their counts, rather
        # than a slice of a hard-coded slang list.
        "trending_phrases": data['trending_phrases'],
        "summary": data['summary'],
    }

    if overall['available']:
        payload["sentiment"] = {
            "bearish_ratio": overall['bearish_ratio'],
            "bullish_ratio": round(1 - overall['bearish_ratio'], 3),
            "tagged_messages": overall['tagged_messages'],
            "label": overall['label'],
            "color": overall['color'],
        }
    else:
        payload["sentiment"] = {"label": "Unavailable", "color": "#6c757d",
                                "reason": overall.get('reason')}

    return payload

def _earnings_block(earnings):
    """Render the earnings Sourced into a display block, honestly labelled."""
    if not earnings.ok:
        return {"available": False, "reason": earnings.reason}
    value = earnings.value
    return {
        "available": True,
        "label": value["label"],
        "date": value["date"],
        "through": value["through"],
        "upcoming": value["upcoming"],
        "days_away": value["days_away"],
        "confirmed": value["confirmed"],
        "source": earnings.source,
    }


def analyze_options_flow(symbol):
    """Options positioning from the real nearest-expiry chain.

    Returns unavailable rather than a neutral-looking placeholder when the chain
    cannot be read. The previous fallback produced a put/call ratio of exactly
    1.0, which reads as a genuine neutral signal and fed a quarter of the
    rebound score on days when no options data existed at all.
    """
    flow = market_data.options_flow(symbol)
    earnings = market_data.earnings_date(symbol)
    timing = _earnings_block(earnings)

    if not flow.ok:
        return {
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "available": False,
            "reason": flow.reason,
            "volume_metrics": {},
            "flow_sentiment": {},
            "key_strikes": {},
            "timing_analysis": timing,
            "alerts": [],
            "summary": f"Options data unavailable: {flow.reason}",
        }

    data = flow.value
    put_call_ratio = data["put_call_ratio"]

    # Volume relative to open interest is a standard read on whether today's
    # activity is new positioning or existing contracts being closed. It needs
    # only the current chain, unlike a volume-vs-average comparison, which the
    # old code faked by dividing today's volume by 80% of itself (a constant
    # 1.25x regardless of input).
    open_interest = (data.get("open_interest_put_call") or 0)
    turnover = safe_ratio(data["total_volume"], data["contracts"])

    if put_call_ratio is None:
        flow_sentiment, sentiment_strength, sentiment_color = "unknown", "unknown", "#6c757d"
    elif put_call_ratio < 0.7:
        flow_sentiment = "call-heavy"
        sentiment_strength = "strong" if put_call_ratio < 0.5 else "moderate"
        sentiment_color = "#28a745"
    elif put_call_ratio > 1.5:
        flow_sentiment = "put-heavy"
        sentiment_strength = "strong" if put_call_ratio > 2.0 else "moderate"
        sentiment_color = "#dc3545"
    else:
        flow_sentiment, sentiment_strength, sentiment_color = "balanced", "weak", "#6c757d"

    alerts = []
    if put_call_ratio is not None and put_call_ratio < 0.4:
        alerts.append("🟢 Heavy call buying")
    elif put_call_ratio is not None and put_call_ratio > 2.5:
        alerts.append("🔴 Heavy put buying")
    if data["total_volume"] > 10000:
        alerts.append(f"📊 {data['total_volume']:,} contracts traded")
    if timing.get("upcoming") and (timing.get("days_away") or 99) <= 14:
        alerts.append(f"📅 Earnings in {timing['days_away']}d")

    return {
        "symbol": symbol,
        "timestamp": datetime.now().isoformat(),
        "available": True,
        "source": flow.source,
        "volume_metrics": {
            "expiry": data["expiry"],
            "call_volume": data["call_volume"],
            "put_volume": data["put_volume"],
            "total_options_volume": f"{data['total_volume']:,}",
            "contracts_listed": data["contracts"],
            "avg_volume_per_contract": round(turnover, 1) if turnover is not None else None,
        },
        "flow_sentiment": {
            "direction": flow_sentiment,
            "strength": sentiment_strength,
            "color": sentiment_color,
            "put_call_ratio": put_call_ratio,
            "open_interest_put_call": open_interest or None,
            "note": "Positioning as observed in the chain, not a directional forecast",
        },
        "key_strikes": {
            "most_active_calls": data["top_calls"],
            "most_active_puts": data["top_puts"],
        },
        "timing_analysis": timing,
        "alerts": alerts,
        "summary": (
            f"{data['total_volume']:,} contracts on {data['expiry']}, "
            f"put/call {put_call_ratio if put_call_ratio is not None else 'n/a'} ({flow_sentiment})"
        ),
    }

def track_institutional_flow(symbol):
    """Institutional ownership from 13F filings, plus reported trading volume.

    What this reports and what it does not:

    Real, from filings -- the percentage of shares held by institutions and the
    largest holders by name. Real, from the exchange -- total share volume.

    Not reported: the intraday split of volume between institutional and retail
    participants, and off-exchange (dark pool) volume. Neither is available from
    any free source. Earlier versions inferred both from a volume ratio and
    presented them as observations, alongside a hedge-fund/mutual-fund/pension
    breakdown derived from the same single number.
    """
    profile = market_data.profile(symbol)
    holders = market_data.institutional_holders(symbol, limit=5)
    technicals = market_data.technicals(symbol)

    held = profile['held_pct_institutions']
    volume_ratio = (technicals.value or {}).get('volume_ratio_20d') if technicals.ok else None

    ownership = {"available": held.ok}
    if held.ok:
        # profile() returns a derived payload when Yahoo reports >100%.
        raw = held.value
        if isinstance(raw, dict):
            ownership.update({
                "pct_held": raw["value"],
                "caveat": raw["note"],
                "estimated": True,
            })
        else:
            ownership.update({"pct_held": raw, "estimated": False})
        ownership["source"] = held.source
    else:
        ownership["reason"] = held.reason

    short_vol = market_data.finra_short_volume(symbol)

    signals = []
    if ownership.get("pct_held") is not None:
        signals.append(f"🏛️ {ownership['pct_held']:.1%} held by institutions")
    if holders.ok and holders.value:
        signals.append(f"📋 Top holder: {holders.value[0]['holder']}")
    if volume_ratio is not None:
        descriptor = "elevated" if volume_ratio > 1.5 else "normal" if volume_ratio > 0.7 else "light"
        signals.append(f"📊 Volume {volume_ratio:.2f}x 20-day average ({descriptor})")
    if short_vol.ok:
        signals.append(f"🩳 {short_vol.value['short_ratio']:.0%} of volume sold short (FINRA, {short_vol.value['as_of']})")

    return {
        "symbol": symbol,
        "timestamp": datetime.now().isoformat(),
        "available": held.ok or holders.ok,
        "short_volume": (
            {"available": True, "source": short_vol.source, **short_vol.value}
            if short_vol.ok else
            {"available": False, "source": short_vol.source, "reason": short_vol.reason}
        ),
        "ownership": ownership,
        "top_holders": {
            "available": holders.ok,
            "source": holders.source,
            "reason": None if holders.ok else holders.reason,
            "holders": holders.value if holders.ok else [],
        },
        "volume": {
            "available": volume_ratio is not None,
            "ratio_20d": volume_ratio,
            "source": technicals.source if technicals.ok else None,
        },
        "not_reported": [
            "intraday institutional vs retail split (no free source)",
            "off-exchange / dark pool volume (FINRA publishes this on a delay)",
            "execution quality: price impact, slippage (needs order-level data)",
        ],
        "smart_money_signals": signals,
        "summary": (
            f"{ownership['pct_held']:.1%} institutional ownership"
            if ownership.get("pct_held") is not None
            else f"Institutional ownership unavailable: {ownership.get('reason', 'unknown')}"
        ),
    }

def get_economic_calendar_impact(symbol):
    """Real upcoming macro events, with the stock's real sector for context.

    Replaces a version that guessed release dates from assumed conventions --
    CPI on the 13th, FOMC on the 20th of eight assumed months -- and presented
    them with exact dates and times. FOMC dates now come from the Federal
    Reserve's published calendar; other releases come from FRED when a key is
    configured, and are reported unavailable when it is not.
    """
    sector_sourced = market_data.profile(symbol)['sector']
    stock_sector = sector_sourced.value if sector_sourced.ok else None

    calendar = econ_calendar.upcoming_events(days_ahead=ECON_HORIZON_DAYS)
    events = calendar['events']

    high_impact = [e for e in events if e.get('impact') == 'high']

    # Volatility outlook reflects how much scheduled macro risk sits inside the
    # horizon. With no events it is "low"; with none available it is unknown,
    # which is different and is reported as such.
    if not calendar['sources']:
        volatility_outlook, outlook_color = 'unavailable', '#6c757d'
    elif len(high_impact) >= 2:
        volatility_outlook, outlook_color = 'high', '#dc3545'
    elif high_impact:
        volatility_outlook, outlook_color = 'moderate', '#ffc107'
    else:
        volatility_outlook, outlook_color = 'low', '#28a745'

    considerations = []
    if calendar['sources']:
        considerations.append(
            f"📅 {len(high_impact)} high-impact event(s) in the next {ECON_HORIZON_DAYS} days"
        )
        if events:
            nearest = events[0]
            considerations.append(
                f"🎯 Next: {nearest['name']} in {nearest['days_away']}d ({nearest['date']})"
            )
    for gap in calendar['unavailable']:
        considerations.append(f"⚠️ {gap['source']} unavailable: {gap['reason']}")

    return {
        "symbol": symbol,
        "sector": stock_sector or "unknown",
        "sector_source": sector_sourced.source if sector_sourced.ok else f"unavailable: {sector_sourced.reason}",
        "timestamp": calendar['as_of'],
        "upcoming_events": events,
        "impact_summary": {
            "total_events": len(events),
            "high_impact_events": len(high_impact),
            "volatility_outlook": volatility_outlook,
            "outlook_color": outlook_color,
            "horizon_days": ECON_HORIZON_DAYS,
        },
        "sources": calendar['sources'],
        "unavailable_sources": calendar['unavailable'],
        "trading_considerations": considerations,
        "summary": (
            f"{len(events)} scheduled macro event(s) in {ECON_HORIZON_DAYS} days; "
            f"volatility outlook {volatility_outlook}"
        ),
    }

def calculate_ai_rebound_prediction(symbol, current_price=None, **_ignored):
    """Rebound assessment for one symbol, from the documented scoring model.

    The previous implementation assigned fixed scores from arbitrary weights and
    treated missing inputs as a neutral 50, so a symbol with no real data still
    produced a confident-looking number. It also derived a price target as
    `current_price * base_multiplier * momentum_factor` and reported it beside
    analyst figures. Both are gone: the model now scores only observable
    factors, renormalises across them, and declines to score when too few exist.

    Extra keyword arguments are accepted and ignored so older callers that
    passed pre-fetched analysis blobs do not break.
    """
    result = score_stock(symbol, current_price, full=True)

    if not result.get('scored'):
        return {
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "scored": False,
            "ai_analysis": {
                "recommendation": result['recommendation'],
                "recommendation_color": result['recommendation_color'],
                "coverage": result['coverage'],
            },
            "reason": result['reason'],
            "factors": result['factors'],
            "methodology": result['methodology'],
            "summary": f"Not scored: {result['reason']}",
        }

    return {
        "symbol": symbol,
        "timestamp": datetime.now().isoformat(),
        "scored": True,
        "ai_analysis": {
            "overall_score": result['score'],
            "recommendation": result['recommendation'],
            "recommendation_color": result['recommendation_color'],
            "confidence_level": result['confidence'],
            "coverage": result['coverage'],
            "factors_used": result['factors_used'],
            "factors_total": result['factors_total'],
        },
        # A list, not a dict: Flask sorts JSON object keys alphabetically, which
        # would discard the ranking by contribution.
        "analysis_breakdown": [
            {
                "key": f['key'],
                "label": f['label'],
                "score": f['score'],
                "effective_weight": f['effective_weight'],
                "contribution": f['contribution'],
                "detail": f['detail'],
                "source": f['source'],
            }
            for f in result['factors']
        ],
        "key_factors": {
            "strongest": [f"{f['label']}: {f['detail']}" for f in result['factors'][:3]],
            "missing": [{"factor": m['label'], "reason": m['reason']} for m in result['missing']],
        },
        "methodology": result['methodology'],
        "summary": (
            f"Rebound score {result['score']}/100 -> {result['recommendation']} "
            f"({result['confidence'].lower()} confidence, "
            f"{result['factors_used']}/{result['factors_total']} inputs available)"
        ),
    }

# Rebound score thresholds -> the sentiment label shown in the main table.
# One model drives both the label and the recommendations panel, so the table
# can no longer disagree with the picks below it.
SENTIMENT_BANDS = [
    (70, '🟢 Oversold Bounce'),
    (58, '🟢 Constructive'),
    (45, '📊 Mixed Signals'),
    (0, '🔴 Weak Setup'),
]


def sentiment_for_score(score):
    for threshold, label in SENTIMENT_BANDS:
        if score >= threshold:
            return label
    return '🔴 Weak Setup'



def _horizon_summaries(symbol, target_price=None):
    """Per-row recovery odds for the table, from cached data only.

    Three concrete, checkable questions, one per horizon: how often has this
    stock reached yesterday's close within 7 trading days, its 20-day mean
    within 21, and the analyst consensus within 126. Each cell carries the
    measured frequency with its sample size, or an em dash when the input is
    genuinely absent. No fetches happen here -- closes, the moving average and
    the target all come from the caches the background warmer maintains.
    """
    empty = {"display": UNAVAILABLE_DISPLAY, "sort": -1.0}
    out = {"short": dict(empty), "medium": dict(empty), "long": dict(empty)}

    history = market_data.price_history(symbol, allow_fetch=False)
    if not history.ok or len(history.value) < 2:
        return out
    closes = np.array(history.value, dtype=float)
    price = closes[-1]
    if not price:
        return out

    tech = market_data.technicals(symbol, allow_fetch=False)
    ma20 = (tech.value or {}).get("ma20") if tech.ok else None

    def measure(key, level, horizon_key):
        if not level or level <= price:
            return
        upside = (level / price - 1.0) * 100.0
        sourced = timeframes.target_probability(closes, upside, horizon_key)
        if sourced.ok:
            v = sourced.value
            pct = v["probability"] * 100
            out[key] = {
                "display": f"{pct:.0f}%",
                "detail": f"{v['hits']}/{v['windows']} windows, +{upside:.1f}% needed",
                "sort": round(pct, 1),
            }

    measure("short", closes[-2], "short")
    measure("medium", ma20, "medium")
    measure("long", target_price, "long")
    return out


def calculate_enhanced_investment_analysis(losers_data, details_data):
    """Attach a rebound score and sentiment label to every stock.

    This previously ran the full sophisticated predictor once per symbol to
    derive a sentiment label -- roughly 0.9s of network calls each, 21.7s for a
    25-symbol list, on top of the score already being computed separately for
    the recommendations panel. Two independent models rated the same stock and
    could disagree.

    Now a single scoring pass over cached market data produces both, so the
    label in the table and the pick below it always come from the same number.
    """
    try:
        original_analysis = calculate_all_investment_analysis(losers_data, details_data)
    except Exception as e:
        logger.error(f"Original analysis failed: {str(e)}")
        original_analysis = []
        for stock in losers_data:
            original_analysis.append({
                'Symbol': stock['Symbol'],
                'Name': stock['Name'],
                'Current Price': UNAVAILABLE_DISPLAY,
                'Target Price': UNAVAILABLE_DISPLAY,
                'Potential Return %': UNAVAILABLE_DISPLAY,
                'Volume': stock.get('Volume', UNAVAILABLE_DISPLAY),
                'Change Today': stock['Change'],
                'Percent Change Today': stock['Percent Change'],
                'Market Cap': stock.get('Market Cap', 'N/A')
            })

    enhanced_analysis = []
    for stock_analysis in original_analysis:
        symbol = stock_analysis.get('Symbol', 'UNKNOWN')
        enhanced = dict(stock_analysis)

        try:
            result = score_stock(symbol, parse_money(stock_analysis.get('Current Price')))
        except Exception as e:
            logger.warning(f"Scoring failed for {symbol}: {type(e).__name__}: {e}")
            result = None

        if result and result.get('scored'):
            enhanced['AI Sentiment'] = sentiment_for_score(result['score'])
            enhanced['Rebound Score'] = result['score']
            enhanced['Confidence'] = result['confidence']
            enhanced['Coverage'] = result['coverage']
            enhanced['Factors Used'] = result['factors_used']
            enhanced['Factors Total'] = result['factors_total']
        else:
            # Not scored is a distinct state from scored-badly, and the label
            # says so rather than defaulting to a bearish-looking verdict.
            reason = (result or {}).get('reason', 'scoring failed')
            enhanced['AI Sentiment'] = '⚪ Insufficient data'
            enhanced['Sentiment Detail'] = reason
            enhanced['Rebound Score'] = None
            enhanced['Confidence'] = 'None'
            enhanced['Coverage'] = result.get('coverage') if result else 0
            enhanced['Score Reason'] = result.get('reason') if result else 'scoring failed'

        try:
            horizons = _horizon_summaries(symbol, parse_money(enhanced.get('Target Price')))
        except Exception as e:
            logger.warning(f"Horizon summary failed for {symbol}: {type(e).__name__}: {e}")
            horizons = {k: {"display": UNAVAILABLE_DISPLAY, "sort": -1.0}
                        for k in ("short", "medium", "long")}
        enhanced['P Short'] = horizons['short']
        enhanced['P Medium'] = horizons['medium']
        enhanced['P Long'] = horizons['long']

        enhanced_analysis.append(enhanced)

    # Highest conviction first: scored stocks above unscored, then by the
    # backtested rebound score, coverage as the tiebreak. This is the "value
    # prop" ordering the table renders with; column headers re-sort client-side.
    enhanced_analysis.sort(
        key=lambda e: (e.get('Rebound Score') is not None,
                       e.get('Rebound Score') or -1,
                       e.get('Coverage') or 0),
        reverse=True)
    return enhanced_analysis

def score_stock(symbol, current_price=None, full=False):
    """Score one symbol with the documented rebound model.

    All inputs come from market_data, which caches aggressively, so scoring the
    full loser list stays affordable on a small instance. Every input is
    optional: score_rebound renormalises over whatever is actually available and
    declines to score at all when too little is.
    """
    targets = market_data.analyst_target(symbol, allow_fetch=full)
    prof = market_data.profile(symbol, allow_fetch=full)
    tech = market_data.technicals(symbol, allow_fetch=full)
    # `full` controls whether the two expensive factors may hit the network.
    # The loser table scores 25 symbols and settles for four of six factors --
    # the model renormalises over what it has and reports the coverage. The
    # detail view asks for everything.
    ratings = market_data.analyst_recommendations(symbol, allow_fetch=full)
    options = market_data.options_flow(symbol, allow_fetch=full)

    price = current_price or (tech.value or {}).get('close')

    return recommendation.score_rebound(
        current_price=price,
        target_mean=targets['mean'].value,
        analyst_count=targets['analysts'].value,
        ratings=ratings.value if ratings.ok else None,
        technicals=tech.value if tech.ok else None,
        put_call_ratio=(options.value or {}).get('put_call_ratio') if options.ok else None,
        short_pct_float=prof['short_pct_float'].value,
    )


def filter_ai_recovery_potential(enhanced_analysis):
    """Rank the loser list by rebound score and return the strongest setups.

    This replaces a filter that gated on emoji sentiment strings and then ran
    the full sophisticated predictor for every surviving symbol on every page
    render -- several network calls per stock, on a 0.5 CPU instance. Scoring
    now reads cached market data instead.

    Only genuinely scored stocks are eligible. A stock whose data was too thin
    to score is excluded rather than being ranked as though it scored zero.
    """
    picks = []

    for stock in enhanced_analysis:
        symbol = stock.get('Symbol')
        if not symbol or symbol == 'ERROR':
            continue

        try:
            price = parse_money(stock.get('Current Price'))
            result = score_stock(symbol, price)
        except Exception as e:
            logger.warning(f"Scoring failed for {symbol}: {type(e).__name__}: {e}")
            continue

        if not result.get('scored'):
            logger.info(f"{symbol} not scored: {result.get('reason')}")
            continue

        if result['score'] < MIN_REBOUND_SCORE:
            continue

        picks.append({
            **stock,
            'Rebound Score': result['score'],
            'Recommendation': result['recommendation'],
            'Recommendation Color': result['recommendation_color'],
            'Confidence': result['confidence'],
            'Coverage': result['coverage'],
            'Factors Used': result['factors_used'],
            'Factors Total': result['factors_total'],
            'Top Factors': [
                f"{f['label']}: {f['detail']}" for f in result['factors'][:3]
            ],
            'Missing Inputs': [m['label'] for m in result['missing']],
        })

    # Rank by score, then by how much of the model was observable, so a
    # well-covered 72 outranks a thinly-covered 74.
    picks.sort(key=lambda p: (p['Rebound Score'], p['Coverage']), reverse=True)
    return picks

@app.route('/api/tasks/start/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def start_background_analysis(symbol):
    """Start background analysis tasks for a symbol"""
    try:
        # Start multiple analysis tasks
        recovery_task = predict_recovery_task.delay(symbol)
        sentiment_task = analyze_sentiment_task.delay(symbol)
        
        response_data = {
            "symbol": symbol,
            "tasks": {
                "recovery_prediction": recovery_task.id,
                "sentiment_analysis": sentiment_task.id
            },
            "status": "started",
            "message": f"Background analysis started for {symbol}"
        }
        
        logger.info("Background analysis started", symbol=symbol, 
                    recovery_task_id=recovery_task.id, 
                    sentiment_task_id=sentiment_task.id)
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error("Failed to start background analysis", symbol=symbol, error=str(e))
        return jsonify({"error": str(e), "symbol": symbol}), 500

@app.route('/api/tasks/status/<task_id>')
def get_task_status(task_id):
    """Get status of a background task"""
    try:
        from celery.result import AsyncResult
        
        task = AsyncResult(task_id, app=celery_app)
        
        if task.state == 'PENDING':
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': 'Task is waiting to be processed'
            }
        elif task.state == 'PROGRESS':
            response = {
                'task_id': task_id,
                'state': task.state,
                'progress': task.info.get('progress', 0),
                'status': task.info.get('status', '')
            }
        elif task.state == 'SUCCESS':
            response = {
                'task_id': task_id,
                'state': task.state,
                'result': task.result
            }
        else:  # FAILURE
            response = {
                'task_id': task_id,
                'state': task.state,
                'error': str(task.info)
            }
            
        return jsonify(response)
        
    except Exception as e:
        logger.error("Failed to get task status", task_id=task_id, error=str(e))
        return jsonify({"error": str(e), "task_id": task_id}), 500

# Professional Trading Feature API Endpoints
@app.route('/api/options-flow/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_options_flow(symbol):
    """Get options flow analysis for a stock"""
    try:
        options_data = analyze_options_flow(symbol.upper())
        
        # Add HTTP caching
        etag = generate_etag(options_data)
        if request.headers.get('If-None-Match') == etag:
            response = make_response('', 304)
            response.headers['ETag'] = etag
            return response
            
        response = make_response(jsonify(options_data))
        response.headers['Content-Type'] = 'application/json'
        response.headers['ETag'] = etag
        return add_cache_headers(response, max_age=60)
        
    except Exception as e:
        logger.error("Failed to get options flow", symbol=symbol, error=str(e))
        return jsonify({"error": str(e), "symbol": symbol}), 500

@app.route('/api/institutional-flow/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_institutional_flow(symbol):
    """Get institutional flow tracking for a stock"""
    try:
        institutional_data = track_institutional_flow(symbol.upper())
        
        # Add HTTP caching
        etag = generate_etag(institutional_data)
        if request.headers.get('If-None-Match') == etag:
            response = make_response('', 304)
            response.headers['ETag'] = etag
            return response
            
        response = make_response(jsonify(institutional_data))
        response.headers['Content-Type'] = 'application/json'
        response.headers['ETag'] = etag
        return add_cache_headers(response, max_age=60)
        
    except Exception as e:
        logger.error("Failed to get institutional flow", symbol=symbol, error=str(e))
        return jsonify({"error": str(e), "symbol": symbol}), 500

@app.route('/api/economic-calendar/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_economic_calendar(symbol):
    """Get economic calendar events impacting the stock"""
    try:
        calendar_data = get_economic_calendar_impact(symbol.upper())
        
        # Add HTTP caching
        etag = generate_etag(calendar_data)
        if request.headers.get('If-None-Match') == etag:
            response = make_response('', 304)
            response.headers['ETag'] = etag
            return response
            
        response = make_response(jsonify(calendar_data))
        response.headers['Content-Type'] = 'application/json' 
        response.headers['ETag'] = etag
        return add_cache_headers(response, max_age=60)
        
    except Exception as e:
        logger.error("Failed to get economic calendar", symbol=symbol, error=str(e))
        return jsonify({"error": str(e), "symbol": symbol}), 500

@app.route('/api/professional-analysis/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_professional_analysis(symbol):
    """Get comprehensive professional trading analysis"""
    try:
        # Get all professional data
        options_data = analyze_options_flow(symbol.upper())
        institutional_data = track_institutional_flow(symbol.upper())
        calendar_data = get_economic_calendar_impact(symbol.upper())
        
        # Combine into comprehensive analysis
        professional_analysis = {
            "symbol": symbol.upper(),
            "timestamp": datetime.now().isoformat(),
            "options_flow": options_data,
            "institutional_flow": institutional_data,
            "economic_calendar": calendar_data,
            # Each source may be unavailable independently, so read defensively
            # and say "unavailable" rather than inventing a neutral reading.
            "overall_sentiment": {
                "options_bias": (options_data.get("flow_sentiment") or {}).get("direction", "unavailable"),
                "institutional_bias": (institutional_data.get("flow_direction") or {}).get("direction", "unavailable"),
                "volatility_outlook": (calendar_data.get("impact_summary") or {}).get("volatility_outlook", "unavailable")
            },
            "trading_signals": [
                *options_data.get("alerts", []),
                *institutional_data.get("smart_money_signals", []),
                *calendar_data.get("trading_considerations", [])
            ],
            "summary": (
                f"Options: {(options_data.get('flow_sentiment') or {}).get('direction', 'unavailable')}; "
                f"institutional: {(institutional_data.get('flow_direction') or {}).get('direction', 'unavailable')}; "
                f"economic volatility: {(calendar_data.get('impact_summary') or {}).get('volatility_outlook', 'unavailable')}"
            )
        }
        
        # Add HTTP caching
        etag = generate_etag(professional_analysis)
        if request.headers.get('If-None-Match') == etag:
            response = make_response('', 304)
            response.headers['ETag'] = etag
            return response
            
        response = make_response(jsonify(professional_analysis))
        response.headers['Content-Type'] = 'application/json'
        response.headers['ETag'] = etag
        return add_cache_headers(response, max_age=60)
        
    except Exception as e:
        logger.error("Failed to get professional analysis", symbol=symbol, error=str(e))
        return jsonify({"error": str(e), "symbol": symbol}), 500

@app.route('/api/ai-analysis/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_ai_stock_analysis(symbol):
    """Get comprehensive AI-powered stock analysis"""
    try:
        # The scoring model pulls exactly the inputs it needs through
        # market_data's cache. The previous version eagerly ran five separate
        # analyses -- including the full sophisticated predictor -- and then
        # used almost none of them.
        ai_prediction = calculate_ai_rebound_prediction(symbol.upper())
        
        # Add HTTP caching
        etag = generate_etag(ai_prediction)
        if request.headers.get('If-None-Match') == etag:
            response = make_response('', 304)
            response.headers['ETag'] = etag
            return response
            
        response = make_response(jsonify(ai_prediction))
        response.headers['Content-Type'] = 'application/json'
        response.headers['ETag'] = etag
        return add_cache_headers(response, max_age=60)
        
    except Exception as e:
        logger.error("Failed to get AI analysis", symbol=symbol, error=str(e))
        return jsonify({"error": str(e), "symbol": symbol}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
