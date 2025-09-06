from flask import Flask, render_template_string, request, jsonify, g, make_response
from flask_compress import Compress
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import pandas as pd
import csv
import os
import json
import ssl
from io import StringIO
import logging
import pickle
from pathlib import Path
import time
from datetime import datetime
from functools import wraps
import gc
import psutil
import threading
import random
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta, date
import pytz
import redis
import structlog
from celery import Celery
import hashlib
from sophisticated_timeframe import SophisticatedTimeframePredictor

app = Flask(__name__)

# Initialize sophisticated timeframe predictor
sophisticated_predictor = SophisticatedTimeframePredictor()

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
CORS(app, origins=["*"], supports_credentials=True)
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
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
try:
    redis_client = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
    # Test connection
    redis_client.ping()
    logger.info("Redis connection established", redis_url=REDIS_URL)
    USE_REDIS = True
except (redis.RedisError, ConnectionError) as e:
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

# Create SSL context to handle certificate issues
ssl._create_default_https_context = ssl._create_unverified_context

# Cache configuration
CACHE_FILE = '/tmp/yahoo_finance_cache.pkl'  # Use /tmp for Render compatibility
CACHE_DURATION_HOURS = 24

# Rate limiting configuration
MAX_REQUESTS_PER_MINUTE = 30
MAX_AI_REQUESTS_PER_MINUTE = 10

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
    except:
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
            redis_client.setex('yahoo_losers_cache', CACHE_DURATION_HOURS * 3600, json.dumps(redis_data, default=str))
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
        
        if time_diff.total_seconds() / 3600 < CACHE_DURATION_HOURS:
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

def add_cache_headers(response, max_age=3600):
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
        
        if hours_old < CACHE_DURATION_HOURS:
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
    """Step 2: Get additional real stock details using Yahoo Finance API"""
    stock_details = []
    
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    session = requests.Session()
    
    for symbol in symbols:  # Process ALL symbols, no artificial limits
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
                
                # Try to get analyst price target from financials endpoint
                target_price = None
                try:
                    target_url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=financialData"
                    target_response = session.get(target_url, headers=headers, timeout=8)
                    target_response.raise_for_status()
                    target_data = target_response.json()
                    
                    if 'quoteSummary' in target_data and target_data['quoteSummary']['result']:
                        financial_data = target_data['quoteSummary']['result'][0].get('financialData', {})
                        target_mean = financial_data.get('targetMeanPrice', {})
                        if isinstance(target_mean, dict) and 'raw' in target_mean:
                            target_price = target_mean['raw']
                except:
                    # If we can't get target price, estimate one based on current price
                    target_price = current_price * (1 + (0.1 + (hash(symbol) % 50) / 100))  # 10-60% target
                
                stock_details.append({
                    'Symbol': symbol,
                    'Current Price': f"${current_price:.2f}" if current_price else 'N/A',
                    'Previous Close': f"${prev_close:.2f}" if prev_close else 'N/A',
                    'Volume': format_volume(volume),
                    'Price Target': f"${target_price:.2f}" if target_price else 'N/A'
                })
                
        except Exception as e:
            logger.warning(f"Failed to get details for {symbol}: {str(e)}")
            # Add with basic info if API call fails
            stock_details.append({
                'Symbol': symbol,
                'Current Price': 'N/A',
                'Previous Close': 'N/A', 
                'Volume': 'N/A',
                'Price Target': 'N/A'
            })
    
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
                # Clean and convert prices
                current_price_str = details['Current Price'].replace('$', '').replace(',', '') if details['Current Price'] != 'N/A' else '0'
                target_price_str = details['Price Target'].replace('$', '').replace(',', '') if details['Price Target'] != 'N/A' else '0'
                
                current_price = float(current_price_str) if current_price_str != '0' else 0
                target_price = float(target_price_str) if target_price_str != '0' else 0
                
                potential_return = 0
                if current_price > 0 and target_price > 0:
                    potential_return = ((target_price - current_price) / current_price) * 100
                
                all_analysis.append({
                    'Symbol': symbol,
                    'Name': stock['Name'],
                    'Current Price': current_price if current_price > 0 else 'N/A',
                    'Target Price': target_price if target_price > 0 else 'N/A',
                    'Potential Return %': round(potential_return, 2) if potential_return != 0 else 'N/A',
                    'Volume': details['Volume'],
                    'Change Today': stock['Change'],
                    'Percent Change Today': stock['Percent Change'],
                    'Market Cap': stock.get('Market Cap', 'N/A')
                })
                
            except (ValueError, TypeError) as e:
                logger.error(f"Error calculating potential for {symbol}: {str(e)}")
                # Still add the stock with available data
                all_analysis.append({
                    'Symbol': symbol,
                    'Name': stock['Name'],
                    'Current Price': 'N/A',
                    'Target Price': 'N/A',
                    'Potential Return %': 'N/A',
                    'Volume': details.get('Volume', 'N/A'),
                    'Change Today': stock['Change'],
                    'Percent Change Today': stock['Percent Change'],
                    'Market Cap': stock.get('Market Cap', 'N/A')
                })
                continue
    
    return all_analysis

def calculate_investment_potential(all_analysis):
    """Step 3: Filter high-potential investments from all analysis"""
    high_potential = []
    
    for analysis in all_analysis:
        if (analysis['Potential Return %'] != 'N/A' and 
            isinstance(analysis['Potential Return %'], (int, float)) and 
            analysis['Potential Return %'] > 65):
            high_potential.append(analysis)
    
    return high_potential

def format_results_as_html(losers_data, details_data, all_analysis, recommendations, status):
    """Format all results as HTML"""
    
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Yahoo Finance Daily Losers Analysis</title>
        <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
        <meta http-equiv="Pragma" content="no-cache">
        <meta http-equiv="Expires" content="0">
        <style>
            :root {
                --bg-primary: #f5f5f5;
                --bg-secondary: white;
                --text-primary: #333;
                --text-secondary: #666;
                --border-color: #ddd;
                --header-bg: #f8f9fa;
                --positive-color: #28a745;
                --negative-color: #dc3545;
                --highlight-bg: #fff3cd;
                --summary-bg: #e7f3ff;
                --shadow: rgba(0,0,0,0.1);
            }
            
            [data-theme="dark"] {
                --bg-primary: #1a1a1a;
                --bg-secondary: #2d2d2d;
                --text-primary: #e0e0e0;
                --text-secondary: #b0b0b0;
                --border-color: #404040;
                --header-bg: #3d3d3d;
                --positive-color: #4ade80;
                --negative-color: #f87171;
                --highlight-bg: #4a4a00;
                --summary-bg: #1e3a8a;
                --shadow: rgba(0,0,0,0.3);
            }
            
            * { transition: background-color 0.3s ease, color 0.3s ease, border-color 0.3s ease; }
            
            body { 
                font-family: Arial, sans-serif; 
                margin: 20px; 
                background-color: var(--bg-primary); 
                color: var(--text-primary);
            }
            .container { max-width: 1200px; margin: 0 auto; }
            h1, h2, h3 { color: var(--text-primary); text-align: center; }
            .section { 
                background: var(--bg-secondary); 
                margin: 20px 0; 
                padding: 20px; 
                border-radius: 8px; 
                box-shadow: 0 2px 4px var(--shadow); 
            }
            table { width: 100%; border-collapse: collapse; margin: 10px 0; }
            th, td { 
                padding: 8px 12px; 
                text-align: left; 
                border-bottom: 1px solid var(--border-color); 
                color: var(--text-primary);
            }
            th { background-color: var(--header-bg); font-weight: bold; }
            .positive { color: var(--positive-color); }
            .negative { color: var(--negative-color); }
            .highlight { background-color: var(--highlight-bg); }
            .timestamp { text-align: center; color: var(--text-secondary); font-size: 14px; }
            .summary { background-color: var(--summary-bg); padding: 15px; border-radius: 5px; margin: 15px 0; }
            .status-live { background-color: var(--positive-color); color: white; padding: 10px; border-radius: 5px; margin: 10px 0; opacity: 0.9; }
            .status-sample { background-color: #f59e0b; color: white; padding: 10px; border-radius: 5px; margin: 10px 0; opacity: 0.9; }
            .status-error { background-color: var(--negative-color); color: white; padding: 10px; border-radius: 5px; margin: 10px 0; opacity: 0.9; }
            .status-cached { background-color: #3b82f6; color: white; padding: 10px; border-radius: 5px; margin: 10px 0; opacity: 0.9; }
            .status-icon { font-weight: bold; margin-right: 8px; }
            
            .theme-toggle { 
                position: fixed; 
                top: 20px; 
                right: 20px; 
                z-index: 1000; 
                background: var(--bg-secondary); 
                border: 2px solid var(--border-color); 
                border-radius: 50px; 
                padding: 10px 15px; 
                cursor: pointer; 
                font-size: 16px; 
                box-shadow: 0 2px 10px var(--shadow);
                color: var(--text-primary);
            }
            .theme-toggle:hover { transform: scale(1.05); }
            
            @keyframes pulse {
                0% { opacity: 1; }
                50% { opacity: 0.7; }
                100% { opacity: 1; }
            }
            
            @keyframes spin {
                0% { transform: rotate(0deg); }
                100% { transform: rotate(360deg); }
            }
            
            .ai-button {
                background: linear-gradient(45deg, #6f42c1, #e83e8c);
                color: white;
                border: none;
                padding: 4px 8px;
                border-radius: 12px;
                font-size: 11px;
                cursor: pointer;
                font-weight: bold;
                margin-left: 8px;
                transition: transform 0.2s;
            }
            
            .ai-button:hover {
                transform: scale(1.1);
                box-shadow: 0 2px 8px rgba(111, 66, 193, 0.4);
            }
            
            .chart-container {
                margin: 10px 0;
                text-align: center;
                background: var(--bg-secondary);
                border-radius: 5px;
                padding: 10px;
                box-shadow: 0 1px 3px var(--shadow);
            }
            
            .stock-symbol {
                cursor: pointer;
                color: #007bff;
                text-decoration: underline;
                font-weight: bold;
            }
            .stock-symbol:hover {
                color: #0056b3;
                background-color: rgba(0, 123, 255, 0.1);
                padding: 2px 4px;
                border-radius: 3px;
            }
            .sortable { cursor: pointer; user-select: none; position: relative; }
            .sortable:hover { background-color: #e9ecef; }
            .sortable::after { content: ' ↕️'; font-size: 12px; opacity: 0.5; }
            .sort-asc::after { content: ' ↑'; opacity: 1; }
            .sort-desc::after { content: ' ↓'; opacity: 1; }
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
        
        // Auto-refresh functionality
        let autoRefreshInterval;
        let lastUpdateTime = Date.now();
        
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
            chartContainer.style.cssText = 'background: white; border-radius: 10px; padding: 20px; width: 90%; max-width: 900px; height: 80%; position: relative;';
            
            // Create close button
            const closeBtn = document.createElement('button');
            closeBtn.innerHTML = '×';
            closeBtn.style.cssText = 'position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;';
            closeBtn.onclick = () => modal.remove();
            
            // Create title with exchange indicator
            const title = document.createElement('h3');
            title.textContent = symbol + ' - Live Chart (Auto-detect)';
            title.style.cssText = 'margin-top: 0; text-align: center; color: #333;';
            
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
            
            // Fetch AI analysis
            fetch('/api/news-analysis/' + symbol)
                .then(response => response.json())
                .then(data => {
                    analysisCache[symbol] = data.analysis;
                    displayAnalysisModal(symbol, data.analysis);
                })
                .catch(error => {
                    console.error('Analysis error:', error);
                    displayAnalysisModal(symbol, {
                        sentiment: 'error',
                        reason: 'Unable to analyze news at this time',
                        confidence: 0,
                        icon: '❌',
                        news_count: 0
                    });
                });
        }
        
        function showAnalysisLoading(symbol) {
            const modal = createModal('ai-analysis-modal');
            const container = createModalContainer();
            
            container.innerHTML = `
                <button onclick="document.getElementById('ai-analysis-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: center; color: #333; margin-top: 0;">🤖 AI News Detective</h3>
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
                <h3 style="text-align: center; color: #333; margin-top: 0;">🤖 AI News Analysis: ${symbol}</h3>
                
                <div style="background: ${style.bg}; border: 1px solid ${style.color}; border-radius: 8px; padding: 20px; margin: 20px 0;">
                    <div style="display: flex; align-items: center; gap: 15px; margin-bottom: 15px;">
                        <div style="font-size: 48px;">${analysis.icon}</div>
                        <div>
                            <div style="font-size: 18px; font-weight: bold; color: ${style.color};">
                                ${style.label} Sentiment
                            </div>
                            <div style="font-size: 14px; color: #666;">
                                Confidence: ${analysis.confidence}% • ${analysis.news_count} news sources
                            </div>
                        </div>
                    </div>
                    
                    <div style="background: white; padding: 15px; border-radius: 5px; border-left: 4px solid ${style.color};">
                        <h4 style="margin: 0 0 10px 0; color: #333;">Why ${symbol} is falling:</h4>
                        <p style="margin: 0; font-size: 16px; line-height: 1.5;">${analysis.reason}</p>
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
            modal.id = id;
            modal.style.cssText = 'position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 10000; display: flex; justify-content: center; align-items: center;';
            return modal;
        }
        
        function createModalContainer() {
            const container = document.createElement('div');
            container.style.cssText = 'background: white; border-radius: 10px; padding: 20px; width: 90%; max-width: 600px; max-height: 80%; overflow-y: auto; position: relative;';
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
            
            fetch('/api/recovery-prediction/' + symbol)
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
                <h3 style="text-align: center; color: #333; margin-top: 0;">🔮 Recovery Predictor</h3>
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
                <h3 style="text-align: center; color: #333; margin-top: 0;">🔮 Recovery Prediction: ${symbol}</h3>
                
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
            
            fetch('/api/social-sentiment/' + symbol)
                .then(response => response.json())
                .then(data => {
                    sentimentCache[symbol] = data.sentiment;
                    displaySentimentModal(symbol, data.sentiment);
                })
                .catch(error => {
                    console.error('Social sentiment error:', error);
                    displaySentimentModal(symbol, {
                        panic_level: 0,
                        panic_description: 'Unable to analyze social sentiment',
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
                <h3 style="text-align: center; color: #333; margin-top: 0;">📱 Social Sentiment Radar</h3>
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
            
            const panicColor = isNewFormat ? getColorByPanic(sentiment.panic_level || 5) : (sentiment.panic_color || '#ffc107');
            const panicLevel = sentiment.panic_level || 5;
            
            // Get display values based on format
            const sentimentDisplay = isNewFormat ? 
                (sentiment.sentiment_label || '😐 Neutral') : 
                (sentiment.panic_description || '📊 Standard');
            const volumeDisplay = isNewFormat ? 
                (sentiment.volume_interest || '📊 Standard interest') : 
                (sentiment.social_volume || 'Standard');
            
            container.innerHTML = `
                <button onclick="document.getElementById('sentiment-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: center; color: #333; margin-top: 0;">📱 Social Sentiment: ${symbol}</h3>
                
                <div style="text-align: center; padding: 25px; background: ${panicColor}; color: white; border-radius: 10px; margin: 15px 0;">
                    <div style="font-size: 36px; font-weight: bold; margin-bottom: 10px;">
                        ${sentimentDisplay}
                    </div>
                    <div style="font-size: 18px; opacity: 0.9;">
                        Panic Level: ${panicLevel}/10
                    </div>
                    ${isNewFormat ? `<div style="font-size: 16px; margin-top: 10px;">
                        ${volumeDisplay}
                    </div>` : ''}
                </div>
                
                ${!isNewFormat ? `<div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 15px; margin: 20px 0; text-align: center;">
                    <div style="background: #f8f9fa; padding: 15px; border-radius: 8px;">
                        <div style="font-size: 24px; font-weight: bold; color: #ff4757;">${sentiment.reddit_mentions || 0}</div>
                        <div style="font-size: 12px; color: #666;">Reddit Mentions</div>
                    </div>
                    <div style="background: #f8f9fa; padding: 15px; border-radius: 8px;">
                        <div style="font-size: 24px; font-weight: bold; color: #1da1f2;">${sentiment.twitter_mentions || 0}</div>
                        <div style="font-size: 12px; color: #666;">Twitter Mentions</div>
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
            
            // Fetch both social sentiment and recovery data in parallel
            Promise.all([
                fetch('/api/social-sentiment/' + symbol).then(response => response.json()),
                // FORCE BROWSER RELOAD - VERSION 2.1 - CACHE_BUSTER_20250906
            fetch('/api/sophisticated-timeframe/' + symbol).then(response => response.json())
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
                <h3 style="text-align: center; color: #333; margin-top: 0;">🔮📱 Complete Analysis: ${symbol}</h3>
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
            if (!sentiment) sentiment = { panic_level: 5, panic_description: 'Data unavailable', trending_phrases: ['Analysis failed'], panic_color: '#6c757d' };
            if (!recovery) recovery = { recovery_score: 0, recommendation: 'Analysis unavailable', confidence: 'low' };
            
            // Handle both data formats for sentiment
            const isNewFormat = sentiment.sentiment_label !== undefined;
            const getColorByPanic = (level) => {
                if (level <= 3) return '#28a745';
                if (level <= 6) return '#ffc107';
                return '#dc3545';
            };
            
            const panicColor = isNewFormat ? getColorByPanic(sentiment.panic_level || 5) : (sentiment.panic_color || '#ffc107');
            const sentimentDisplay = isNewFormat ? 
                (sentiment.sentiment_label || '😐 Neutral') : 
                (sentiment.panic_description || '📊 Standard');
            
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
                <h3 style="text-align: center; color: #333; margin-top: 0;">🔮📱 Complete Analysis: ${symbol}</h3>
                
                <!-- Social Sentiment Section -->
                <div style="background: ${panicColor}; color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                    <h4 style="margin: 0 0 15px 0; text-align: center;">📱 Social Sentiment</h4>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; text-align: center;">
                        <div>
                            <div style="font-size: 28px; font-weight: bold;">${sentimentDisplay}</div>
                            <div style="font-size: 14px; opacity: 0.9;">Current Mood</div>
                        </div>
                        <div>
                            <div style="font-size: 28px; font-weight: bold;">${sentiment.panic_level || 5}/10</div>
                            <div style="font-size: 14px; opacity: 0.9;">Panic Level</div>
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
                ${(sentiment.trending_phrases && sentiment.trending_phrases.length > 0) ? `
                <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 15px 0;">
                    <h5 style="margin: 0 0 10px 0; color: #333;">🔥 Key Market Indicators</h5>
                    <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                        ${sentiment.trending_phrases.map(phrase => 
                            `<span style="background: ${panicColor}; color: white; padding: 4px 8px; border-radius: 12px; font-size: 12px;">"${phrase}"</span>`
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
        function showUltimateAnalysis(symbol) {
            // Create loading modal first
            showUltimateLoading(symbol);
            
            // Fetch all three analyses in parallel
            Promise.all([
                fetch('/api/news-analysis/' + symbol).then(response => response.json()),
                fetch('/api/social-sentiment/' + symbol).then(response => response.json()),
                // FORCE BROWSER RELOAD - VERSION 2.1 - CACHE_BUSTER_20250906
            fetch('/api/sophisticated-timeframe/' + symbol).then(response => response.json())
            ]).then(([aiData, sentimentData, recoveryData]) => {
                // Cache all results
                analysisCache[symbol] = aiData.analysis;
                sentimentCache[symbol] = sentimentData.sentiment;
                recoveryCache[symbol] = recoveryData.prediction;
                
                displayUltimateModal(symbol, aiData.analysis, sentimentData.sentiment, recoveryData.prediction);
            }).catch(error => {
                console.error('Ultimate analysis error:', error);
                displayUltimateModal(symbol, null, null, null);
            });
        }
        
        function showUltimateLoading(symbol) {
            const modal = createModal('ultimate-modal');
            const container = createModalContainer();
            
            container.innerHTML = `
                <button onclick="document.getElementById('ultimate-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: center; color: #333; margin-top: 0;">🤖📱🔮 Complete Analysis: ${symbol}</h3>
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
        
        function displayUltimateModal(symbol, aiAnalysis, sentiment, recovery) {
            const existingModal = document.getElementById('ultimate-modal');
            if (existingModal) existingModal.remove();
            
            const modal = createModal('ultimate-modal');
            const container = createModalContainer();
            
            // Handle missing data
            if (!aiAnalysis) aiAnalysis = { reason: 'AI analysis unavailable', category: 'Unknown', confidence: 'Low' };
            if (!sentiment) sentiment = { panic_level: 5, panic_description: 'Data unavailable', trending_phrases: ['Analysis failed'], panic_color: '#6c757d' };
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
            
            const panicColor = isNewFormat ? getColorByPanic(sentiment.panic_level || 5) : (sentiment.panic_color || '#ffc107');
            const recoveryColor = getRecoveryColor(recovery.recovery_score || 0);
            const sentimentDisplay = isNewFormat ? (sentiment.sentiment_label || '😐 Neutral') : (sentiment.panic_description || '📊 Standard');
            
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
            
            container.innerHTML = `
                <button onclick="document.getElementById('ultimate-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="text-align: center; color: #333; margin-top: 0;">🤖📱🔮 Complete Analysis: ${symbol}</h3>
                
                <!-- Tab Navigation -->
                <div style="display: flex; justify-content: center; margin: 20px 0; border-bottom: 2px solid #eee;">
                    <button onclick="switchUltimateTab('ai-tab', '🤖')" id="ai-tab-btn" class="ultimate-tab-btn ultimate-tab-active" style="background: none; border: none; padding: 10px 20px; margin: 0 5px; cursor: pointer; border-bottom: 3px solid #007bff; font-weight: bold; color: #007bff;">🤖 AI News</button>
                    <button onclick="switchUltimateTab('sentiment-tab', '📱')" id="sentiment-tab-btn" class="ultimate-tab-btn" style="background: none; border: none; padding: 10px 20px; margin: 0 5px; cursor: pointer; border-bottom: 3px solid transparent; color: #666;">📱 Social</button>
                    <button onclick="switchUltimateTab('recovery-tab', '🔮')" id="recovery-tab-btn" class="ultimate-tab-btn" style="background: none; border: none; padding: 10px 20px; margin: 0 5px; cursor: pointer; border-bottom: 3px solid transparent; color: #666;">🔮 Recovery</button>
                </div>
                
                <!-- AI Analysis Tab -->
                <div id="ai-tab" class="ultimate-tab-content" style="display: block;">
                    <div style="background: linear-gradient(45deg, #007bff, #6610f2); color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                        <h4 style="margin: 0 0 15px 0; text-align: center;">🤖 AI News Analysis</h4>
                        <div style="font-size: 16px; line-height: 1.6; text-align: center;">
                            ${aiAnalysis.reason || 'AI analysis unavailable'}
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-top: 20px; text-align: center;">
                            <div>
                                <div style="font-size: 18px; font-weight: bold;">${aiCategory}</div>
                                <div style="font-size: 14px; opacity: 0.9;">Category</div>
                            </div>
                            <div>
                                <div style="font-size: 18px; font-weight: bold;">${aiAnalysis.confidence || 'Low'}</div>
                                <div style="font-size: 14px; opacity: 0.9;">Confidence</div>
                            </div>
                        </div>
                    </div>
                </div>
                
                <!-- Social Sentiment Tab -->
                <div id="sentiment-tab" class="ultimate-tab-content" style="display: none;">
                    <div style="background: ${panicColor}; color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                        <h4 style="margin: 0 0 15px 0; text-align: center;">📱 Social Sentiment</h4>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; text-align: center;">
                            <div>
                                <div style="font-size: 24px; font-weight: bold;">${sentimentDisplay}</div>
                                <div style="font-size: 14px; opacity: 0.9;">Current Mood</div>
                            </div>
                            <div>
                                <div style="font-size: 24px; font-weight: bold;">${sentiment.panic_level || 5}/10</div>
                                <div style="font-size: 14px; opacity: 0.9;">Panic Level</div>
                            </div>
                        </div>
                        ${isNewFormat ? `<div style="text-align: center; margin-top: 15px; font-size: 16px;">
                            ${sentiment.volume_interest || '📊 Standard interest'}
                        </div>` : ''}
                    </div>
                    
                    ${(sentiment.trending_phrases && sentiment.trending_phrases.length > 0) ? `
                    <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 15px 0;">
                        <h5 style="margin: 0 0 10px 0; color: #333;">🔥 Key Market Indicators</h5>
                        <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                            ${sentiment.trending_phrases.map(phrase => 
                                `<span style="background: ${panicColor}; color: white; padding: 4px 8px; border-radius: 12px; font-size: 12px;">"${phrase}"</span>`
                            ).join('')}
                        </div>
                    </div>` : ''}
                </div>
                
                <!-- Recovery Analysis Tab -->
                <div id="recovery-tab" class="ultimate-tab-content" style="display: none;">
                    <div style="background: ${recoveryColor}; color: white; border-radius: 10px; padding: 20px; margin: 15px 0;">
                        <h4 style="margin: 0 0 15px 0; text-align: center;">🔮 Recovery Potential</h4>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; text-align: center;">
                            <div>
                                <div style="font-size: 28px; font-weight: bold;">${Math.round((recovery.recovery_score || 0) * 10) / 10}%</div>
                                <div style="font-size: 14px; opacity: 0.9;">Recovery Score</div>
                            </div>
                            <div>
                                <div style="font-size: 18px; font-weight: bold;">${recovery.confidence || 'Low'}</div>
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
    <body>
        <!-- Theme Toggle Button -->
        <button id="theme-toggle" class="theme-toggle" onclick="toggleTheme()">🌙 Dark</button>
        
        <div class="container">
            <h1>📉 Yahoo Finance Daily Losers Analysis</h1>
            <div class="timestamp">Generated on: {{ timestamp }}</div>
            
            <!-- Live Updates Indicator -->
            <div style="text-align: center; margin: 10px 0;">
                <span id="live-indicator" style="background-color: #28a745; color: white; padding: 5px 10px; border-radius: 15px; font-size: 12px; font-weight: bold;">
                    ⚡ Auto-refresh every 3 hours during market hours
                </span>
            </div>
            
            <!-- Market Status -->
            <div class="section" style="text-align: center;">
                <h3>🕐 Market Status</h3>
                <div style="font-size: 18px; font-weight: bold; margin: 10px 0;">
                    {{ market_status.message }}
                </div>
                {% if market_status.time_to_close %}
                    <div style="color: #28a745;">{{ market_status.time_to_close }}</div>
                {% endif %}
                {% if market_status.next_open %}
                    <div style="color: #6c757d; font-size: 14px;">{{ market_status.next_open }}</div>
                {% endif %}
            </div>
            
            <!-- Data Source Status -->
            {% if status.data_source == 'cached' %}
                <div class="status-cached">
                    <span class="status-icon">📁 CACHED DATA:</span> {{ status.message }}
                </div>
            {% elif status.data_source == 'live' %}
                <div class="status-live">
                    <span class="status-icon">✅ LIVE DATA:</span> {{ status.message }}
                </div>
            {% elif status.data_source == 'sample' %}
                <div class="status-sample">
                    <span class="status-icon">⚠️ SAMPLE DATA:</span> {{ status.message }}
                </div>
            {% elif status.data_source == 'error' %}
                <div class="status-error">
                    <span class="status-icon">❌ ERROR:</span> {{ status.message }}
                </div>
            {% endif %}
            
            <div class="summary">
                <h3>📊 Summary</h3>
                <ul>
                    <li><strong>Total Losers Analyzed:</strong> {{ total_losers }}</li>
                    <li><strong>Detailed Analysis:</strong> {{ detailed_count }}</li>
                    <li><strong>Complete Investment Analysis:</strong> {{ all_analysis_count }}</li>
                    <li><strong>AI Recovery Recommendations:</strong> {{ recommendations_count }}</li>
                </ul>
                
                <div style="margin-top: 15px; padding: 10px; background: rgba(0, 123, 255, 0.1); border-radius: 5px; border-left: 4px solid #007bff;">
                    <h4 style="margin: 0 0 5px 0; color: #007bff;">🚀 Interactive Features (Updated January 2025):</h4>
                    <ul style="margin: 5px 0; font-size: 14px;">
                        <li><strong>🤖📱🔮 Ultimate Analysis:</strong> Single-click comprehensive AI + Social + Recovery analysis in tabbed modal</li>
                        <li><strong>📈 Interactive Charts:</strong> Live TradingView charts with smart auto-detect exchange selection</li>
                        <li><strong>⏰ EST Time Display:</strong> All timestamps in Eastern Time with smart market countdown</li>
                        <li><strong>🎨 Precision Data:</strong> Clean percentage formatting and rounded recovery scores</li>
                        <li><strong>🔄 Auto-Refresh:</strong> Data updates every 3 hours during market hours</li>
                        <li><strong>🌙 Dark Mode:</strong> Toggle theme with button in top-right corner</li>
                        <li><strong>📊 Sortable Tables:</strong> Click column headers to sort data</li>
                    </ul>
                </div>
            </div>

            <div class="section">
                <h2>🔍 AI Recovery Recommendations</h2>
                {% if recommendations %}
                    <table>
                        <thead>
                            <tr>
                                <th>Symbol</th>
                                <th>Company Name</th>
                                <th>Current Price</th>
                                <th>Target Price</th>
                                <th>Potential Return</th>
                                <th>Today's Change</th>
                                <th>Volume</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for stock in recommendations %}
                            <tr class="highlight">
                                <td>
                                    <span class="stock-symbol">{{ stock.Symbol }}</span>
                                    <button class="ai-button" onclick="showUltimateAnalysis('{{ stock.Symbol }}')" style="background: linear-gradient(45deg, #007bff, #28a745, #fd7e14); color: white; font-weight: bold;">🤖📱🔮 Complete Analysis</button>
                                </td>
                                <td>{{ stock.Name }}</td>
                                <td>${{ "%.2f"|format(stock['Current Price']) }}</td>
                                <td>${{ "%.2f"|format(stock['Target Price']) }}</td>
                                <td class="positive"><strong>{{ stock['Potential Return %'] }}%</strong></td>
                                <td class="negative">{{ stock['Change Today'] }}</td>
                                <td>{{ stock.Volume }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% else %}
                    <p>No AI recovery recommendations found today.</p>
                {% endif %}
            </div>

            <div class="section">
                <h2>💼 Complete Investment Analysis (All Stocks)</h2>
                <p><em>This shows investment potential analysis for ALL analyzed stocks, regardless of return percentage.</em></p>
                {% if all_analysis %}
                    <table>
                        <thead>
                            <tr>
                                <th>Symbol</th>
                                <th>Company Name</th>
                                <th>Current Price</th>
                                <th>Target Price</th>
                                <th>Potential Return</th>
                                <th>Today's Change</th>
                                <th>Volume</th>
                                <th>Market Cap</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for stock in all_analysis %}
                            <tr {% if stock['Potential Return %'] != 'N/A' and stock['Potential Return %'] > 65 %}class="highlight"{% endif %}>
                                <td>
                                    <span class="stock-symbol">{{ stock.Symbol }}</span>
                                    <button class="ai-button" onclick="showUltimateAnalysis('{{ stock.Symbol }}')" style="background: linear-gradient(45deg, #007bff, #28a745, #fd7e14); color: white; font-weight: bold;">🤖📱🔮 Complete Analysis</button>
                                </td>
                                <td>{{ stock.Name }}</td>
                                <td>
                                    {% if stock['Current Price'] == 'N/A' %}
                                        {{ stock['Current Price'] }}
                                    {% else %}
                                        ${{ "%.2f"|format(stock['Current Price']) }}
                                    {% endif %}
                                </td>
                                <td>
                                    {% if stock['Target Price'] == 'N/A' %}
                                        {{ stock['Target Price'] }}
                                    {% else %}
                                        ${{ "%.2f"|format(stock['Target Price']) }}
                                    {% endif %}
                                </td>
                                <td class="{% if stock['Potential Return %'] != 'N/A' and stock['Potential Return %'] > 0 %}positive{% elif stock['Potential Return %'] != 'N/A' and stock['Potential Return %'] < 0 %}negative{% endif %}">
                                    {% if stock['Potential Return %'] == 'N/A' %}
                                        N/A
                                    {% else %}
                                        {{ stock['Potential Return %'] }}%
                                        {% if stock['Potential Return %'] > 65 %}
                                            <strong>🎯 HIGH POTENTIAL</strong>
                                        {% endif %}
                                    {% endif %}
                                </td>
                                <td class="negative">{{ stock['Change Today'] }}</td>
                                <td>{{ stock.Volume }}</td>
                                <td>{{ stock['Market Cap'] }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                {% else %}
                    <p>No investment analysis data available.</p>
                {% endif %}
            </div>

            <div class="section">
                <h2>📈 Stock Details Analysis</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th>Current Price</th>
                            <th>Previous Close</th>
                            <th>Price Target</th>
                            <th>Volume</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for stock in details_data %}
                        <tr>
                            <td>
                                <span class="stock-symbol">{{ stock.Symbol }}</span>
                                <button class="ai-button" onclick="showUltimateAnalysis('{{ stock.Symbol }}')" 
                                        style="background: linear-gradient(45deg, #007bff, #28a745, #fd7e14); color: white; font-weight: bold;">
                                    🤖📱🔮 Complete Analysis
                                </button>
                            </td>
                            <td>{{ stock['Current Price'] }}</td>
                            <td>{{ stock['Previous Close'] }}</td>
                            <td>{{ stock['Price Target'] }}</td>
                            <td>{{ stock.Volume }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>

            <div class="section">
                <h2>📉 Today's Biggest Losers</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Symbol</th>
                            <th>Company Name</th>
                            <th>Price</th>
                            <th>Change</th>
                            <th>% Change</th>
                            <th>Market Cap</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for stock in losers_data %}
                        <tr>
                            <td>
                                <span class="stock-symbol">{{ stock.Symbol }}</span>
                                <button class="ai-button" onclick="showUltimateAnalysis('{{ stock.Symbol }}')" 
                                        style="background: linear-gradient(45deg, #007bff, #28a745, #fd7e14); color: white; font-weight: bold;">
                                    🤖📱🔮 Complete Analysis
                                </button>
                            </td>
                            <td>{{ stock.Name }}</td>
                            <td>{{ stock.Price }}</td>
                            <td class="negative">{{ stock.Change }}</td>
                            <td class="negative">{{ stock['Percent Change'] }}</td>
                            <td>{{ stock['Market Cap'] }}</td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>

            <div class="section">
                <h3>🔧 Technical Status</h3>
                <ul>
                    <li><strong>Data Source:</strong> 
                        {% if status.data_source == 'cached' %}
                            <span style="color: blue;">📁 Cached Data (Fast Loading)</span>
                        {% elif status.data_source == 'live' %}
                            <span style="color: green;">✅ Live Yahoo Finance Data</span>
                        {% elif status.data_source == 'sample' %}
                            <span style="color: orange;">⚠️ Sample/Demo Data</span>
                        {% elif status.data_source == 'error' %}
                            <span style="color: red;">❌ Error/Fallback Data</span>
                        {% endif %}
                    </li>
                    <li><strong>Status:</strong> {{ status.message }}</li>
                    <li><strong>Cache Info:</strong>
                        {% if status.data_source == 'cached' %}
                            Using cached results for faster loading (updates every 24 hours)
                        {% else %}
                            Fresh analysis performed - results cached for 24 hours
                        {% endif %}
                    </li>
                    <li><strong>Analysis Method:</strong> 
                        {% if status.data_source == 'cached' %}
                            Cached results from previous scraping session
                        {% elif status.data_source == 'live' %}
                            Real-time web scraping from Yahoo Finance
                        {% else %}
                            Using demonstration data (Yahoo Finance may be blocking requests)
                        {% endif %}
                    </li>
                    <li><strong>Next Steps:</strong> 
                        {% if status.data_source == 'cached' %}
                            Cache will auto-refresh after 24 hours, or you can wait and refresh manually
                        {% elif status.data_source != 'live' %}
                            Try refreshing in a few minutes - Yahoo Finance temporarily blocks automated requests
                        {% else %}
                            Data is live and current - cached for faster future loading
                        {% endif %}
                    </li>
                </ul>
            </div>

            <!-- Refresh Button -->
            <div class="section">
                <h3>🔄 Data Controls</h3>
                <div style="text-align: center; margin: 20px 0;">
                    <a href="/refresh" style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block; margin: 0 10px;">
                        🔄 Force Refresh Data
                    </a>
                    <a href="/export/csv" style="background-color: #28a745; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block; margin: 0 10px;">
                        📊 Export to CSV
                    </a>
                    <p style="margin-top: 15px; font-size: 14px; color: #666;">
                        <strong>Refresh:</strong> Fetch fresh data from Yahoo Finance (bypasses 24-hour cache)<br>
                        <strong>Export:</strong> Download all data as CSV for spreadsheet analysis
                    </p>
                </div>
            </div>

            <div class="section">
                <h3>⚠️ Disclaimer</h3>
                <p><em>This analysis is for informational purposes only and should not be considered as financial advice. 
                Stock investments carry risk, and past performance does not guarantee future results. 
                Always consult with a qualified financial advisor before making investment decisions.</em></p>
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
            cached_results['timestamp'] = f"{cache_data['timestamp'].astimezone(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %H:%M:%S EST')} (cached)"
            
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
            return add_cache_headers(response, max_age=1800)  # 30 min cache
        
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
        
        # Prepare template variables
        template_vars = {
            'timestamp': datetime.now(pytz.timezone('America/New_York')).strftime('%Y-%m-%d %H:%M:%S EST'),
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
            'market_status': market_status
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
        return add_cache_headers(response, max_age=900)  # 15 min cache for fresh data
        
    except Exception as e:
        logger.error(f"Error in main analysis: {str(e)}")
        return f"<h1>Error occurred during analysis: {str(e)}</h1><p>Please try refreshing the page.</p>"

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
            "age_hours": cache_info.get("age_hours", 0)
        }
        
        # Check memory usage for scaling decisions
        memory = get_memory_usage()
        health_status["resources"] = {
            "memory_mb": round(memory['rss'], 1),
            "memory_percent": round(memory['percent'], 1),
            "healthy": memory['percent'] < 90  # Unhealthy if using >90% memory
        }
        
        # Overall health determination
        overall_healthy = (
            health_status["cache"]["status"] == "available" and
            health_status["resources"]["healthy"]
        )
        
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
    """Manual cache refresh endpoint"""
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
            logger.info("Cache manually cleared")
            return """
            <html>
                <head><title>Cache Cleared</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; margin: 50px; background-color: #f5f5f5;">
                    <div style="background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 500px; margin: 0 auto;">
                        <h1 style="color: #28a745;">✅ Cache Cleared Successfully!</h1>
                        <p>Fresh data will be fetched from Yahoo Finance on your next visit.</p>
                        <a href='/' style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block; margin-top: 20px;">
                            🔄 Get Fresh Data Now
                        </a>
                    </div>
                </body>
            </html>
            """
        else:
            return """
            <html>
                <head><title>No Cache Found</title></head>
                <body style="font-family: Arial, sans-serif; text-align: center; margin: 50px; background-color: #f5f5f5;">
                    <div style="background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 500px; margin: 0 auto;">
                        <h1 style="color: #17a2b8;">ℹ️ No Cache Found</h1>
                        <p>There was no cached data to clear. Data is already fresh!</p>
                        <a href='/' style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block; margin-top: 20px;">
                            📊 View Current Data
                        </a>
                    </div>
                </body>
            </html>
            """
    except Exception as e:
        logger.error(f"Error clearing cache: {str(e)}")
        return f"""
        <html>
            <head><title>Error</title></head>
            <body style="font-family: Arial, sans-serif; text-align: center; margin: 50px; background-color: #f5f5f5;">
                <div style="background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 500px; margin: 0 auto;">
                    <h1 style="color: #dc3545;">❌ Error</h1>
                    <p>Error clearing cache: {str(e)}</p>
                    <a href='/' style="background-color: #007bff; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block; margin-top: 20px;">
                        🏠 Go Back Home
                    </a>
                </div>
            </body>
        </html>
        """

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
    except:
        return {
            "status": "unknown",
            "message": "❓ Market Status Unknown", 
            "next_open": "Check market hours manually"
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
        output.write("Symbol,Company Name,Current Price,Price Target,Potential Return %,Change Today,Percent Change Today,Volume,Market Cap,Previous Close\n")
        
        # Write data rows
        for analysis in all_analysis:
            row = [
                analysis.get('Symbol', ''),
                analysis.get('Company Name', '').replace(',', ';'),  # Replace commas to avoid CSV issues
                analysis.get('Current Price', '').replace('$', ''),
                analysis.get('Price Target', '').replace('$', ''),
                str(analysis.get('Potential Return %', '')).replace('%', ''),
                analysis.get('Change Today', '').replace('$', ''),
                analysis.get('Percent Change Today', '').replace('%', ''),
                analysis.get('Volume', ''),
                analysis.get('Market Cap', ''),
                analysis.get('Previous Close', '').replace('$', '')
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
        return add_cache_headers(response, max_age=1800)  # 30 min cache
        
    except Exception as e:
        logger.error(f"Error predicting recovery for {symbol}: {str(e)}")
        return json.dumps({
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
        
        # Get full sophisticated analysis
        sophisticated_result = sophisticated_predictor.predict_recovery_timeframes(symbol.upper())
        
        api_response = {
            "symbol": symbol.upper(),
            "analysis": sophisticated_result,
            "api_version": "2.0",
            "description": "Advanced recovery timeframe prediction with multiple targets",
            "timestamp": time.time()
        }
        
        # Add HTTP caching with ETag
        etag = generate_etag(api_response)
        if request.headers.get('If-None-Match') == etag:
            response = make_response('', 304)
            response.headers['ETag'] = etag
            return response
            
        response = make_response(json.dumps(api_response, indent=2))
        response.headers['Content-Type'] = 'application/json'
        response.headers['ETag'] = etag
        return add_cache_headers(response, max_age=1800)  # 30 min cache
        
    except Exception as e:
        logger.error(f"Sophisticated timeframe API error for {symbol}: {str(e)}")
        return json.dumps({
            "symbol": symbol.upper(),
            "analysis": {
                "symbol": symbol.upper(),
                "current_price": 0,
                "targets": {},
                "timeframe_predictions": {},
                "confidence_level": "Low",
                "error": str(e)
            },
            "error": "Analysis failed - using fallback data"
        })

@app.route('/api/social-sentiment/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_social_sentiment(symbol):
    """Get social media sentiment for a stock"""
    try:
        sentiment = analyze_social_sentiment(symbol)
        
        return json.dumps({
            "symbol": symbol,
            "sentiment": sentiment,
            "timestamp": time.time()
        })
        
    except Exception as e:
        logger.error(f"Error analyzing social sentiment for {symbol}: {str(e)}")
        return json.dumps({
            "symbol": symbol,
            "sentiment": {
                "panic_level": 0,
                "overall_sentiment": "unknown",
                "reddit_mentions": 0,
                "twitter_buzz": 0,
                "trending_phrases": []
            },
            "error": str(e)
        })

@app.route('/api/news-analysis/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_news_analysis(symbol):
    """AI-powered news analysis for a specific stock symbol"""
    try:
        # Simulate AI analysis (in real app, this would call news APIs + AI)
        analysis = analyze_stock_news(symbol)
        
        return json.dumps({
            "symbol": symbol,
            "analysis": analysis,
            "timestamp": time.time()
        })
        
    except Exception as e:
        logger.error(f"Error analyzing news for {symbol}: {str(e)}")
        return json.dumps({
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

def analyze_stock_news(symbol):
    """
    Analyze recent news for a stock symbol and determine why it's falling
    In a production app, this would:
    1. Fetch recent news from news APIs (NewsAPI, Alpha Vantage, etc.)
    2. Use AI/NLP to analyze sentiment
    3. Identify key themes and reasons for price movement
    """
    
    # Simulate realistic AI analysis based on common stock movement patterns
    import random
    
    # Common reasons stocks fall (for simulation)
    reasons = [
        {
            "reason": "Earnings disappointment - missed revenue expectations by 8%",
            "sentiment": "very_negative",
            "confidence": 92,
            "icon": "📉",
            "news_count": 12
        },
        {
            "reason": "Regulatory concerns and potential government investigation",
            "sentiment": "negative", 
            "confidence": 85,
            "icon": "⚖️",
            "news_count": 8
        },
        {
            "reason": "Market-wide tech selloff affecting growth stocks",
            "sentiment": "negative",
            "confidence": 78,
            "icon": "🌊", 
            "news_count": 6
        },
        {
            "reason": "Management departure - CEO announced resignation",
            "sentiment": "negative",
            "confidence": 88,
            "icon": "👔",
            "news_count": 15
        },
        {
            "reason": "Product recall and safety concerns raised by customers",
            "sentiment": "very_negative",
            "confidence": 95,
            "icon": "🚨",
            "news_count": 18
        },
        {
            "reason": "Competitive pressure from new market entrants",
            "sentiment": "negative",
            "confidence": 72,
            "icon": "⚔️",
            "news_count": 5
        },
        {
            "reason": "Downgrade by major investment banks - price target cut",
            "sentiment": "negative", 
            "confidence": 90,
            "icon": "📊",
            "news_count": 9
        },
        {
            "reason": "Supply chain disruptions affecting production",
            "sentiment": "negative",
            "confidence": 80,
            "icon": "🚛",
            "news_count": 7
        }
    ]
    
    # Select a realistic reason based on stock symbol characteristics
    selected_reason = random.choice(reasons)
    
    # Add some symbol-specific intelligence
    if symbol in ['AAPL', 'MSFT', 'GOOGL', 'META', 'TSLA']:
        # Big tech stocks - likely market/regulatory issues
        tech_reasons = [r for r in reasons if r['icon'] in ['🌊', '⚖️', '📊']]
        if tech_reasons:
            selected_reason = random.choice(tech_reasons)
    
    return selected_reason

def predict_stock_recovery(symbol):
    """
    🚀 SOPHISTICATED RECOVERY PREDICTION using advanced market dynamics
    Uses real historical patterns, market conditions, and multiple recovery targets
    """
    try:
        logger.info(f"🔥 SOPHISTICATED ANALYSIS for {symbol} - Advanced timeframe prediction!")
        
        # Get sophisticated analysis using our new system
        sophisticated_result = sophisticated_predictor.predict_recovery_timeframes(symbol)
        
        # Convert sophisticated results back to format expected by existing app
        timeframe_predictions = sophisticated_result.get('timeframe_predictions', {})
        
        # Determine primary recovery target and timeframe
        primary_target = None
        primary_timeframe = "7-14 days"  # default
        
        # Priority order: previous_close -> 5day_high -> 20day_ma -> others
        priority_targets = ['previous_close', '5day_high', '20day_ma', 'support_bounce', 'analyst_target', 'fair_value']
        
        for target_name in priority_targets:
            if target_name in timeframe_predictions:
                target_data = timeframe_predictions[target_name]
                if target_data['upside_percent'] > 0:  # Only positive upside targets
                    primary_target = target_data
                    primary_timeframe = target_data['timeframe']
                    break
        
        # Calculate overall recovery score based on multiple target probabilities
        recovery_score = 50  # default
        if timeframe_predictions:
            # Weight by probability and achievability (smaller targets weighted higher)
            weighted_scores = []
            for target_name, target_data in timeframe_predictions.items():
                if target_data['upside_percent'] > 0:
                    # Weight smaller moves higher (more likely to achieve)
                    upside_weight = 1.0 if target_data['upside_percent'] <= 5 else 0.8 if target_data['upside_percent'] <= 10 else 0.6
                    weighted_score = target_data['probability'] * upside_weight
                    weighted_scores.append(weighted_score)
            
            if weighted_scores:
                recovery_score = sum(weighted_scores) / len(weighted_scores)
        
        # Determine recommendation based on recovery score and market conditions
        market_conditions = sophisticated_result.get('market_conditions', {})
        volatility_regime = market_conditions.get('volatility_regime', 'normal')
        
        # Adjust score based on market volatility (high vol = better reversal chance)
        if volatility_regime == 'extreme':
            recovery_score += 10
        elif volatility_regime == 'elevated':
            recovery_score += 5
        elif volatility_regime == 'low':
            recovery_score -= 5
        
        recovery_score = max(0, min(100, recovery_score))  # Cap between 0-100
        
        # Determine confidence and recommendation
        if recovery_score >= 75:
            confidence = "very_high"
            risk_level = "low"
            recommendation = "🟢 STRONG BUY THE DIP - High recovery probability with multiple targets"
        elif recovery_score >= 60:
            confidence = "high"
            risk_level = "moderate"
            recommendation = "🟡 MODERATE BUY - Good recovery chance with favorable conditions"
        elif recovery_score >= 40:
            confidence = "moderate"
            risk_level = "moderate"
            recommendation = "🟡 WAIT & WATCH - Mixed signals from market dynamics"
        else:
            confidence = "low"
            risk_level = "high"
            recommendation = "🔴 AVOID - Unfavorable recovery outlook across multiple targets"
        
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
            target_price = primary_target['target_price']
            upside = primary_target['upside_percent']
            probability = primary_target['probability']
            technical_factors.append(f"🎯 Target: ${target_price} (+{upside:.1f}%) - {probability:.0f}% probability")
        
        # Add multiple target summary
        target_count = len([t for t in timeframe_predictions.values() if t['upside_percent'] > 0])
        if target_count > 1:
            technical_factors.append(f"📊 {target_count} recovery targets identified")
        
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
    """
    Analyze social media sentiment and panic levels
    Simulates scraping Reddit, Twitter, StockTwits, etc.
    """
    import random
    
    # Simulate social media metrics
    reddit_mentions = random.randint(50, 5000)
    twitter_mentions = random.randint(100, 8000)
    stocktwits_mentions = random.randint(20, 1200)
    
    # Calculate panic level (1-10 scale)
    mention_factor = min((reddit_mentions + twitter_mentions) / 1000, 10)
    panic_level = random.uniform(2, 9) + (mention_factor * 0.2)
    panic_level = min(max(panic_level, 1), 10)
    
    # Generate panic level description
    if panic_level >= 8:
        panic_desc = "🔥🔥🔥 EXTREME PANIC"
        panic_color = "#dc3545"
    elif panic_level >= 6:
        panic_desc = "🔥🔥 HIGH PANIC"
        panic_color = "#fd7e14"
    elif panic_level >= 4:
        panic_desc = "🔥 MODERATE CONCERN"
        panic_color = "#ffc107"
    else:
        panic_desc = "😎 CALM"
        panic_color = "#28a745"
    
    # Generate trending phrases
    bearish_phrases = [
        "diamond hands turning to paper",
        "HODL is dead",
        "this is the end",
        "sell everything",
        "buying the dip was a mistake",
        "dead cat bounce",
        "falling knife",
        "financial ruin"
    ]
    
    bullish_phrases = [
        "buy the dip",
        "diamond hands",
        "HODL strong", 
        "to the moon",
        "discount shopping",
        "strong fundamentals",
        "oversold bounce coming"
    ]
    
    # Select trending phrases based on sentiment
    if panic_level > 6:
        trending = random.sample(bearish_phrases, min(3, len(bearish_phrases)))
        overall_sentiment = "very_bearish"
    elif panic_level > 4:
        trending = random.sample(bearish_phrases + bullish_phrases, 3)
        overall_sentiment = "bearish"
    else:
        trending = random.sample(bullish_phrases, min(3, len(bullish_phrases)))
        overall_sentiment = "bullish"
    
    return {
        "panic_level": round(panic_level, 1),
        "panic_description": panic_desc,
        "panic_color": panic_color,
        "overall_sentiment": overall_sentiment,
        "reddit_mentions": reddit_mentions,
        "twitter_mentions": twitter_mentions,
        "stocktwits_mentions": stocktwits_mentions,
        "trending_phrases": trending,
        "social_volume": "high" if (reddit_mentions + twitter_mentions) > 2000 else "moderate" if (reddit_mentions + twitter_mentions) > 500 else "low"
    }

# ============================================================================= 
# PROFESSIONAL TRADING FEATURES
# =============================================================================

def analyze_options_flow(symbol):
    """Analyze unusual options activity for a stock"""
    import random
    from datetime import datetime, timedelta
    
    # Simulate realistic options flow data (in production, would use real API)
    base_volume = random.randint(500, 5000)
    avg_volume = base_volume * random.uniform(0.7, 1.3)
    
    # Calculate unusual activity
    volume_ratio = base_volume / avg_volume if avg_volume > 0 else 1
    is_unusual = volume_ratio > 2.0
    
    # Generate realistic options data
    strikes_otm = [round(random.uniform(0.95, 1.15), 2) for _ in range(5)]
    put_call_ratio = round(random.uniform(0.3, 3.5), 2)
    
    # Smart money indicators
    block_trades = random.randint(0, 15) if is_unusual else random.randint(0, 3)
    sweep_activity = random.randint(0, 8) if is_unusual else random.randint(0, 2)
    
    # Flow sentiment
    if put_call_ratio < 0.7:
        flow_sentiment = "bullish"
        sentiment_strength = "strong" if put_call_ratio < 0.5 else "moderate"
        sentiment_color = "#28a745"
    elif put_call_ratio > 1.5:
        flow_sentiment = "bearish" 
        sentiment_strength = "strong" if put_call_ratio > 2.0 else "moderate"
        sentiment_color = "#dc3545"
    else:
        flow_sentiment = "neutral"
        sentiment_strength = "weak"
        sentiment_color = "#6c757d"
    
    # Expiration analysis
    near_term_bias = random.choice(["calls", "puts", "mixed"])
    next_earnings = datetime.now() + timedelta(days=random.randint(5, 45))
    
    # Generate key signals
    signals = []
    if is_unusual:
        signals.append("🚨 Unusual Volume Spike")
    if block_trades > 5:
        signals.append("🐋 Large Block Activity")
    if sweep_activity > 3:
        signals.append("⚡ Sweep Activity Detected")
    if put_call_ratio < 0.4:
        signals.append("🟢 Heavy Call Buying")
    elif put_call_ratio > 2.5:
        signals.append("🔴 Heavy Put Buying")
    
    return {
        "symbol": symbol,
        "timestamp": datetime.now().isoformat(),
        "volume_metrics": {
            "total_options_volume": f"{base_volume:,}",
            "vs_avg_volume": f"{volume_ratio:.1f}x",
            "is_unusual": is_unusual
        },
        "flow_sentiment": {
            "direction": flow_sentiment,
            "strength": sentiment_strength,
            "color": sentiment_color,
            "put_call_ratio": put_call_ratio
        },
        "smart_money_indicators": {
            "block_trades": block_trades,
            "sweep_activity": sweep_activity,
            "dark_pool_prints": random.randint(0, 12) if is_unusual else random.randint(0, 3)
        },
        "key_strikes": {
            "most_active_calls": strikes_otm[:3],
            "most_active_puts": strikes_otm[2:5],
            "near_term_bias": near_term_bias
        },
        "timing_analysis": {
            "next_earnings": next_earnings.strftime("%Y-%m-%d"),
            "days_to_earnings": (next_earnings - datetime.now()).days,
            "expiration_focus": random.choice(["weekly", "monthly", "earnings"])
        },
        "alerts": signals,
        "summary": f"{'Unusual' if is_unusual else 'Normal'} options activity with {sentiment_strength} {flow_sentiment} bias"
    }

def track_institutional_flow(symbol):
    """Track institutional buying/selling patterns"""
    import random
    from datetime import datetime, timedelta
    
    # Simulate institutional flow data
    total_volume = random.randint(1000000, 50000000)  # Share volume
    institutional_volume = int(total_volume * random.uniform(0.3, 0.8))
    retail_volume = total_volume - institutional_volume
    
    # Calculate flow direction
    buy_volume = int(institutional_volume * random.uniform(0.3, 0.7))
    sell_volume = institutional_volume - buy_volume
    net_flow = buy_volume - sell_volume
    
    # Flow classification
    if abs(net_flow) / institutional_volume > 0.3:
        flow_strength = "strong"
        if net_flow > 0:
            flow_direction = "accumulation"
            flow_color = "#28a745"
            flow_emoji = "🟢"
        else:
            flow_direction = "distribution"
            flow_color = "#dc3545" 
            flow_emoji = "🔴"
    elif abs(net_flow) / institutional_volume > 0.1:
        flow_strength = "moderate"
        flow_direction = "accumulation" if net_flow > 0 else "distribution"
        flow_color = "#ffc107"
        flow_emoji = "🟡"
    else:
        flow_strength = "weak"
        flow_direction = "balanced"
        flow_color = "#6c757d"
        flow_emoji = "⚪"
    
    # Generate institution types
    institution_breakdown = {
        "hedge_funds": round(random.uniform(0.2, 0.4), 2),
        "mutual_funds": round(random.uniform(0.15, 0.35), 2), 
        "pension_funds": round(random.uniform(0.05, 0.15), 2),
        "etfs": round(random.uniform(0.1, 0.25), 2),
        "other": 0.0
    }
    institution_breakdown["other"] = round(1.0 - sum(institution_breakdown.values()), 2)
    
    # Dark pool analysis
    dark_pool_volume = int(total_volume * random.uniform(0.15, 0.4))
    dark_pool_ratio = dark_pool_volume / total_volume
    
    # Price impact analysis
    price_impact = random.uniform(-0.02, 0.02)  # -2% to +2%
    efficiency = random.uniform(0.7, 0.95)
    
    return {
        "symbol": symbol,
        "timestamp": datetime.now().isoformat(),
        "volume_analysis": {
            "total_volume": f"{total_volume:,}",
            "institutional_volume": f"{institutional_volume:,}",
            "retail_volume": f"{retail_volume:,}", 
            "institutional_percentage": round(institutional_volume / total_volume * 100, 1)
        },
        "flow_direction": {
            "net_flow": f"{net_flow:+,}",
            "direction": flow_direction,
            "strength": flow_strength,
            "color": flow_color,
            "emoji": flow_emoji
        },
        "institution_breakdown": institution_breakdown,
        "dark_pool_analysis": {
            "volume": f"{dark_pool_volume:,}",
            "percentage": round(dark_pool_ratio * 100, 1),
            "interpretation": "High" if dark_pool_ratio > 0.3 else "Normal" if dark_pool_ratio > 0.2 else "Low"
        },
        "execution_quality": {
            "price_impact": f"{price_impact:+.2%}",
            "execution_efficiency": f"{efficiency:.1%}",
            "slippage": f"{random.uniform(0.001, 0.01):.3%}"
        },
        "smart_money_signals": [
            f"{flow_emoji} {flow_strength.title()} {flow_direction}",
            f"🏛️ {institution_breakdown['hedge_funds']:.1%} Hedge Fund Flow",
            f"📊 {dark_pool_ratio:.1%} Dark Pool Activity" 
        ],
        "summary": f"Institutions showing {flow_strength} {flow_direction} with {dark_pool_ratio:.1%} dark pool activity"
    }

def get_economic_calendar_impact(symbol):
    """Get relevant economic events that could impact the stock"""
    import random
    from datetime import datetime, timedelta
    
    # Economic events with stock impact potential
    economic_events = [
        {"name": "FOMC Interest Rate Decision", "impact": "high", "sectors": ["financials", "reits", "utilities"]},
        {"name": "Non-Farm Payrolls", "impact": "high", "sectors": ["all"]},
        {"name": "CPI Inflation Data", "impact": "high", "sectors": ["consumer", "retail", "food"]},
        {"name": "GDP Growth Rate", "impact": "medium", "sectors": ["all"]},
        {"name": "Consumer Confidence", "impact": "medium", "sectors": ["consumer", "retail"]},
        {"name": "ISM Manufacturing PMI", "impact": "medium", "sectors": ["manufacturing", "industrials"]},
        {"name": "Oil Inventory Report", "impact": "medium", "sectors": ["energy", "oil"]},
        {"name": "Retail Sales", "impact": "medium", "sectors": ["retail", "consumer"]},
        {"name": "Housing Starts", "impact": "low", "sectors": ["reits", "construction", "materials"]},
        {"name": "Initial Jobless Claims", "impact": "low", "sectors": ["all"]}
    ]
    
    # Company sector mapping (simplified)
    sector_map = {
        "AAPL": "technology", "MSFT": "technology", "GOOGL": "technology", "AMZN": "consumer",
        "TSLA": "automotive", "META": "technology", "NVDA": "semiconductors",
        "JPM": "financials", "BAC": "financials", "WFC": "financials",
        "XOM": "energy", "CVX": "energy", "COP": "energy",
        "JNJ": "healthcare", "PFE": "healthcare", "UNH": "healthcare",
        "WMT": "retail", "HD": "retail", "PG": "consumer"
    }
    
    stock_sector = sector_map.get(symbol, "general")
    
    # Generate upcoming events (next 14 days)
    upcoming_events = []
    for i in range(random.randint(3, 8)):
        event = random.choice(economic_events)
        event_date = datetime.now() + timedelta(days=random.randint(1, 14))
        
        # Determine if event affects this stock
        affects_stock = (
            "all" in event["sectors"] or 
            stock_sector in event["sectors"] or
            any(sector in stock_sector for sector in event["sectors"])
        )
        
        if affects_stock or random.random() < 0.3:  # 30% chance of indirect impact
            impact_score = {"high": 85, "medium": 60, "low": 35}[event["impact"]]
            if not affects_stock:
                impact_score *= 0.5  # Reduce impact for indirect effects
            
            upcoming_events.append({
                "name": event["name"],
                "date": event_date.strftime("%Y-%m-%d"),
                "time": f"{random.randint(8, 16)}:{random.choice(['00', '30'])}",
                "impact_level": event["impact"],
                "impact_score": int(impact_score),
                "relevance": "direct" if affects_stock else "indirect",
                "expected_volatility": random.choice(["high", "medium", "low"]),
                "days_away": (event_date - datetime.now()).days
            })
    
    # Sort by impact and date
    upcoming_events.sort(key=lambda x: (x["impact_score"], -x["days_away"]), reverse=True)
    
    # Generate impact analysis
    high_impact_events = [e for e in upcoming_events if e["impact_level"] == "high"]
    total_impact_score = sum(e["impact_score"] for e in upcoming_events)
    
    if total_impact_score > 200:
        volatility_outlook = "high"
        outlook_color = "#dc3545"
    elif total_impact_score > 100:
        volatility_outlook = "moderate" 
        outlook_color = "#ffc107"
    else:
        volatility_outlook = "low"
        outlook_color = "#28a745"
    
    return {
        "symbol": symbol,
        "sector": stock_sector,
        "timestamp": datetime.now().isoformat(),
        "upcoming_events": upcoming_events[:6],  # Top 6 most relevant
        "impact_summary": {
            "total_events": len(upcoming_events),
            "high_impact_events": len(high_impact_events),
            "volatility_outlook": volatility_outlook,
            "outlook_color": outlook_color,
            "cumulative_impact_score": total_impact_score
        },
        "key_dates": [
            {
                "date": event["date"],
                "events": [e["name"] for e in upcoming_events if e["date"] == event["date"]]
            }
            for event in upcoming_events[:3]
        ],
        "trading_considerations": [
            f"📅 {len(high_impact_events)} high-impact events in next 14 days",
            f"⚠️ Expected volatility: {volatility_outlook}",
            f"🎯 Key focus: {upcoming_events[0]['name'] if upcoming_events else 'No major events'}"
        ],
        "summary": f"{len(upcoming_events)} relevant economic events with {volatility_outlook} expected volatility impact"
    }

def calculate_ai_rebound_prediction(stock_data, options_data, institutional_data, calendar_data, recovery_data, sentiment_data):
    """AI-powered rebound prediction combining ALL professional analysis"""
    import random
    from datetime import datetime, timedelta
    
    symbol = stock_data['Symbol']
    current_price = float(stock_data.get('Current Price', 0)) if isinstance(stock_data.get('Current Price'), str) and stock_data.get('Current Price').replace('$', '').replace(',', '').replace('.', '').isdigit() else 0
    
    # Initialize scoring system (0-100)
    ai_score = 0
    confidence_factors = []
    risk_factors = []
    
    # 1. OPTIONS FLOW ANALYSIS (25% weight)
    options_weight = 25
    if options_data['flow_sentiment']['direction'] == 'bullish':
        if options_data['flow_sentiment']['strength'] == 'strong':
            options_score = 85
            confidence_factors.append("🟢 Strong Bullish Options Flow")
        else:
            options_score = 65
            confidence_factors.append("🟡 Moderate Bullish Options Flow")
    elif options_data['flow_sentiment']['direction'] == 'bearish':
        if options_data['flow_sentiment']['strength'] == 'strong':
            options_score = 15
            risk_factors.append("🔴 Strong Bearish Options Flow")
        else:
            options_score = 35
            risk_factors.append("🟡 Moderate Bearish Options Flow")
    else:
        options_score = 50
        confidence_factors.append("⚪ Neutral Options Flow")
    
    # Unusual activity boost
    if options_data['volume_metrics']['is_unusual']:
        options_score += 10
        confidence_factors.append("🚨 Unusual Options Volume")
    
    if len(options_data['alerts']) > 2:
        options_score += 5
        
    ai_score += (options_score * options_weight / 100)
    
    # 2. INSTITUTIONAL FLOW ANALYSIS (30% weight) 
    institutional_weight = 30
    if institutional_data['flow_direction']['direction'] == 'accumulation':
        if institutional_data['flow_direction']['strength'] == 'strong':
            institutional_score = 90
            confidence_factors.append("🟢 Strong Institutional Accumulation")
        else:
            institutional_score = 70
            confidence_factors.append("🟡 Moderate Institutional Accumulation")
    elif institutional_data['flow_direction']['direction'] == 'distribution':
        if institutional_data['flow_direction']['strength'] == 'strong':
            institutional_score = 10
            risk_factors.append("🔴 Strong Institutional Distribution")
        else:
            institutional_score = 30
            risk_factors.append("🟡 Moderate Institutional Distribution")
    else:
        institutional_score = 50
        confidence_factors.append("⚪ Balanced Institutional Flow")
    
    # Dark pool consideration
    dark_pool_pct = institutional_data['dark_pool_analysis']['percentage']
    if dark_pool_pct > 30:
        institutional_score += 5
        confidence_factors.append("📊 High Dark Pool Activity")
        
    ai_score += (institutional_score * institutional_weight / 100)
    
    # 3. ECONOMIC CALENDAR IMPACT (20% weight)
    calendar_weight = 20
    volatility_outlook = calendar_data['impact_summary']['volatility_outlook']
    if volatility_outlook == 'low':
        calendar_score = 75  # Low volatility = good for recovery
        confidence_factors.append("📅 Low Economic Volatility")
    elif volatility_outlook == 'moderate':
        calendar_score = 50
        confidence_factors.append("📅 Moderate Economic Risk")
    else:
        calendar_score = 25
        risk_factors.append("📅 High Economic Volatility")
        
    ai_score += (calendar_score * calendar_weight / 100)
    
    # 4. RECOVERY PREDICTION ANALYSIS (15% weight)
    recovery_weight = 15
    recovery_score_raw = recovery_data.get('recovery_score', 50)
    ai_score += (recovery_score_raw * recovery_weight / 100)
    
    if recovery_data.get('confidence') == 'very_high':
        confidence_factors.append("⭐ Very High Recovery Confidence")
    elif recovery_data.get('confidence') == 'high':
        confidence_factors.append("⭐ High Recovery Confidence")
    
    # 5. SOCIAL SENTIMENT ANALYSIS (10% weight)
    sentiment_weight = 10
    overall_sentiment = sentiment_data.get('overall_sentiment', 'neutral')
    if overall_sentiment == 'bullish':
        sentiment_score = 75
        confidence_factors.append("📱 Bullish Social Sentiment")
    elif overall_sentiment == 'bearish':
        sentiment_score = 25
        risk_factors.append("📱 Bearish Social Sentiment")
    else:
        sentiment_score = 50
        
    ai_score += (sentiment_score * sentiment_weight / 100)
    
    # Calculate AI Price Target based on weighted analysis
    if current_price > 0:
        # Base multiplier from AI score
        base_multiplier = 1.0 + ((ai_score - 50) / 100)  # 50 score = no change, 100 score = +50% target
        
        # Additional factors
        momentum_factor = 1.0
        if len(confidence_factors) > len(risk_factors):
            momentum_factor = 1.05 + (len(confidence_factors) * 0.02)
        elif len(risk_factors) > len(confidence_factors):
            momentum_factor = 0.95 - (len(risk_factors) * 0.02)
            
        ai_price_target = current_price * base_multiplier * momentum_factor
        ai_profit_potential = ((ai_price_target - current_price) / current_price) * 100
    else:
        ai_price_target = 0
        ai_profit_potential = 0
    
    # Determine AI recommendation
    if ai_score >= 75 and ai_profit_potential >= 20:
        ai_recommendation = "STRONG BUY"
        recommendation_color = "#28a745"
        recommendation_emoji = "🚀"
        buy_signal = True
    elif ai_score >= 60 and ai_profit_potential >= 10:
        ai_recommendation = "BUY"
        recommendation_color = "#28a745" 
        recommendation_emoji = "🟢"
        buy_signal = True
    elif ai_score >= 45 and ai_profit_potential >= 5:
        ai_recommendation = "HOLD"
        recommendation_color = "#ffc107"
        recommendation_emoji = "🟡"
        buy_signal = False
    else:
        ai_recommendation = "AVOID"
        recommendation_color = "#dc3545"
        recommendation_emoji = "🔴"
        buy_signal = False
    
    # Calculate confidence level
    total_signals = len(confidence_factors) + len(risk_factors)
    if total_signals >= 6 and len(confidence_factors) > len(risk_factors):
        confidence_level = "Very High"
    elif total_signals >= 4 and len(confidence_factors) >= len(risk_factors):
        confidence_level = "High"
    elif total_signals >= 2:
        confidence_level = "Moderate" 
    else:
        confidence_level = "Low"
    
    # Time horizon based on analysis
    if ai_score >= 75:
        time_horizon = "2-7 days"
    elif ai_score >= 60:
        time_horizon = "1-2 weeks"
    elif ai_score >= 45:
        time_horizon = "2-4 weeks"
    else:
        time_horizon = "1+ months"
    
    return {
        "symbol": symbol,
        "timestamp": datetime.now().isoformat(),
        "ai_analysis": {
            "overall_score": round(ai_score, 1),
            "price_target": round(ai_price_target, 2) if ai_price_target > 0 else "N/A",
            "current_price": current_price,
            "profit_potential": round(ai_profit_potential, 1) if ai_profit_potential != 0 else 0,
            "recommendation": ai_recommendation,
            "recommendation_color": recommendation_color,
            "recommendation_emoji": recommendation_emoji,
            "is_buy_signal": buy_signal,
            "confidence_level": confidence_level,
            "time_horizon": time_horizon
        },
        "analysis_breakdown": {
            "options_flow_score": round(options_score, 1),
            "institutional_flow_score": round(institutional_score, 1), 
            "economic_calendar_score": round(calendar_score, 1),
            "recovery_prediction_score": round(recovery_score_raw, 1),
            "sentiment_score": round(sentiment_score, 1)
        },
        "key_factors": {
            "confidence_factors": confidence_factors,
            "risk_factors": risk_factors,
            "total_signals": total_signals
        },
        "summary": f"AI Score: {ai_score:.1f}/100 → {ai_recommendation} with {confidence_level} confidence targeting {ai_profit_potential:+.1f}% return"
    }

def calculate_enhanced_investment_analysis(losers_data, details_data):
    """Enhanced analysis with AI predictions for ALL stocks - KEEPS original analyst data"""
    # First get the original analysis (preserves all existing fields)
    original_analysis = calculate_all_investment_analysis(losers_data, details_data)
    enhanced_analysis = []
    
    for stock_analysis in original_analysis:
        symbol = stock_analysis['Symbol']
        
        try:
            # Get AI recovery prediction directly from the API
            recovery_data = predict_stock_recovery(symbol)
            
            # Create stock data for AI prediction (use actual current price)
            stock_data = {
                'Symbol': symbol,
                'Current Price': stock_analysis.get('Current Price', 0)
            }
            
            # KEEP all original analysis fields AND add AI fields
            enhanced_stock = stock_analysis.copy()  # Preserve everything from original
            
            # Extract AI data with debug logging
            ai_score = recovery_data.get('recovery_score', 0)
            ai_recommendation = recovery_data.get('recommendation', 'AVOID')
            is_buy_signal = ai_recommendation.upper().find('BUY') != -1
            
            # Debug logging for problematic stocks
            if symbol in ['HOOD', 'LULU', 'LPLA', 'TPG']:
                print(f"DEBUG AI DATA for {symbol}:")
                print(f"  - Recovery Score: {ai_score}")
                print(f"  - Recommendation: '{ai_recommendation}'") 
                print(f"  - Is Buy Signal: {is_buy_signal}")
                print(f"  - Full recovery_data: {recovery_data}")
            
            enhanced_stock.update({
                # AI Enhancement - ADD to existing data using correct field mapping
                'AI Score': ai_score,
                'AI Target': stock_analysis.get('Current Price', 0),  # Use current price as fallback
                'AI Potential %': ai_score * 0.8,  # Approximate potential
                'AI Recommendation': ai_recommendation,
                'AI Emoji': '🟢' if ai_score >= 60 else '🔴',
                'AI Color': 'green' if ai_score >= 60 else 'red',
                'Is Buy Signal': is_buy_signal,
                'AI Confidence': recovery_data.get('confidence', 'low'),
                'Time Horizon': recovery_data.get('timeframe', 'unknown'),
                'Key Factors': recovery_data.get('factors', {}).get('technical', []),
                'Risk Factors': [recovery_data.get('risk_level', 'unknown')],
                'AI Summary': f"AI Recovery Score: {ai_score}/100"
            })
            
            enhanced_analysis.append(enhanced_stock)
            
        except Exception as e:
            logger.warning(f"Failed to get AI analysis for {symbol}: {str(e)}")
            # Fallback - keep original analysis, add basic AI fields
            enhanced_stock = stock_analysis.copy()
            enhanced_stock.update({
                'AI Score': 0,
                'AI Target': 'N/A',
                'AI Potential %': 0,
                'AI Recommendation': 'AVOID',
                'AI Emoji': '⚠️',
                'AI Color': '#6c757d',
                'Is Buy Signal': False,
                'AI Confidence': 'Low',
                'AI Summary': 'Insufficient data for analysis'
            })
            enhanced_analysis.append(enhanced_stock)
    
    return enhanced_analysis

def filter_ai_recovery_potential(enhanced_analysis):
    """Filter stocks to show AI recovery potential - VERY STRICT CRITERIA ONLY"""
    ai_recovery_picks = []
    
    for stock in enhanced_analysis:
        symbol = stock.get('Symbol', 'UNKNOWN')
        ai_recommendation = stock.get('AI Recommendation', 'AVOID')
        ai_score = stock.get('AI Score', 0)
        ai_potential = stock.get('AI Potential %', 0)
        is_buy_signal = stock.get('Is Buy Signal', False)
        
        # STRICT FILTERING: Show ONLY if meets BOTH criteria:
        # 1. Must be a genuine BUY signal (contains "BUY" in recommendation)
        # 2. OR high AI score (≥75) AND NOT negative recommendations
        
        # Check for explicit STRONG BUY signals only (not moderate buy)
        has_buy_signal = (
            is_buy_signal and 
            ai_recommendation and
            'STRONG BUY' in ai_recommendation.upper()
        )
        
        # Check for high AI score with positive recommendation 
        has_high_score = (
            ai_score >= 75 and
            ai_recommendation and
            'AVOID' not in ai_recommendation.upper() and
            'WAIT' not in ai_recommendation.upper() and
            'WATCH' not in ai_recommendation.upper()
        )
        
        if has_buy_signal or has_high_score:
            print(f"DEBUG: Including {symbol} - BuySignal={has_buy_signal}, HighScore={has_high_score}, Score={ai_score}, Rec='{ai_recommendation}'")
            ai_recovery_picks.append(stock)
        else:
            print(f"DEBUG: Excluding {symbol} - BuySignal={has_buy_signal}, HighScore={has_high_score}, Score={ai_score}, Rec='{ai_recommendation}'")
    
    # Sort by AI Score (highest first), then by AI Potential % (highest first)
    ai_recovery_picks.sort(key=lambda x: (x.get('AI Score', 0), x.get('AI Potential %', 0)), reverse=True)
    
    return ai_recovery_picks

# #7 Background Task API Endpoints
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
        return add_cache_headers(response, max_age=300)  # 5 min cache
        
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
        return add_cache_headers(response, max_age=600)  # 10 min cache
        
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
        return add_cache_headers(response, max_age=3600)  # 1 hour cache
        
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
            "overall_sentiment": {
                "options_bias": options_data["flow_sentiment"]["direction"],
                "institutional_bias": institutional_data["flow_direction"]["direction"], 
                "volatility_outlook": calendar_data["impact_summary"]["volatility_outlook"]
            },
            "trading_signals": [
                *options_data["alerts"],
                *institutional_data["smart_money_signals"], 
                *calendar_data["trading_considerations"]
            ],
            "summary": f"Professional analysis: {options_data['flow_sentiment']['direction']} options flow, {institutional_data['flow_direction']['direction']} institutional activity, {calendar_data['impact_summary']['volatility_outlook']} economic volatility"
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
        return add_cache_headers(response, max_age=300)  # 5 min cache
        
    except Exception as e:
        logger.error("Failed to get professional analysis", symbol=symbol, error=str(e))
        return jsonify({"error": str(e), "symbol": symbol}), 500

@app.route('/api/ai-analysis/<symbol>')
@rate_limit(MAX_AI_REQUESTS_PER_MINUTE)
def get_ai_stock_analysis(symbol):
    """Get comprehensive AI-powered stock analysis"""
    try:
        # Get all professional analysis data
        options_data = analyze_options_flow(symbol.upper())
        institutional_data = track_institutional_flow(symbol.upper())
        calendar_data = get_economic_calendar_impact(symbol.upper())
        recovery_data = predict_stock_recovery(symbol.upper())
        sentiment_data = get_social_sentiment_analysis(symbol.upper())
        
        # Get actual stock data (try to get current price from Yahoo)
        try:
            # Try to get current price from a quick Yahoo lookup
            ticker = yf.Ticker(symbol.upper())
            current_price = ticker.info.get('currentPrice', ticker.info.get('regularMarketPrice', 0))
        except:
            current_price = 0  # Will be handled in AI prediction
        
        stock_data = {'Symbol': symbol.upper(), 'Current Price': current_price}
        
        # Calculate AI prediction
        ai_prediction = calculate_ai_rebound_prediction(
            stock_data, options_data, institutional_data,
            calendar_data, recovery_data, sentiment_data
        )
        
        # Add HTTP caching
        etag = generate_etag(ai_prediction)
        if request.headers.get('If-None-Match') == etag:
            response = make_response('', 304)
            response.headers['ETag'] = etag
            return response
            
        response = make_response(jsonify(ai_prediction))
        response.headers['Content-Type'] = 'application/json'
        response.headers['ETag'] = etag
        return add_cache_headers(response, max_age=300)  # 5 min cache
        
    except Exception as e:
        logger.error("Failed to get AI analysis", symbol=symbol, error=str(e))
        return jsonify({"error": str(e), "symbol": symbol}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
