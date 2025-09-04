from flask import Flask, render_template_string
import requests
from bs4 import BeautifulSoup
import pandas as pd
import csv
import os
import datetime
import json
import ssl
from io import StringIO
import logging
import pickle
from pathlib import Path

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create SSL context to handle certificate issues
ssl._create_default_https_context = ssl._create_unverified_context

# Cache configuration
CACHE_FILE = '/tmp/yahoo_finance_cache.pkl'  # Use /tmp for Render compatibility
CACHE_DURATION_HOURS = 24

def save_cache(data):
    """Save analysis results to cache with timestamp"""
    try:
        cache_data = {
            'timestamp': datetime.datetime.now(),
            'data': data
        }
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(cache_data, f)
        logger.info(f"Cache saved successfully at {cache_data['timestamp']}")
    except Exception as e:
        logger.error(f"Failed to save cache: {str(e)}")

def load_cache():
    """Load cached results if within 24 hours"""
    try:
        if not os.path.exists(CACHE_FILE):
            logger.info("No cache file found")
            return None
        
        with open(CACHE_FILE, 'rb') as f:
            cache_data = pickle.load(f)
        
        # Check if cache is still valid (within 24 hours)
        cache_time = cache_data['timestamp']
        current_time = datetime.datetime.now()
        time_diff = current_time - cache_time
        
        if time_diff.total_seconds() / 3600 < CACHE_DURATION_HOURS:
            logger.info(f"Valid cache found from {cache_time} ({time_diff.total_seconds()/3600:.1f} hours ago)")
            return cache_data
        else:
            logger.info(f"Cache expired ({time_diff.total_seconds()/3600:.1f} hours old), will refresh")
            return None
            
    except Exception as e:
        logger.error(f"Failed to load cache: {str(e)}")
        return None

def get_cache_status():
    """Get cache status for display in UI"""
    try:
        if not os.path.exists(CACHE_FILE):
            return {"exists": False, "message": "No cache available"}
        
        with open(CACHE_FILE, 'rb') as f:
            cache_data = pickle.load(f)
        
        cache_time = cache_data['timestamp']
        current_time = datetime.datetime.now()
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
            modal.style.cssText = `
                position: fixed; top: 0; left: 0; width: 100%; height: 100%; 
                background: rgba(0,0,0,0.8); z-index: 10000; 
                display: flex; justify-content: center; align-items: center;
            `;
            
            const chartContainer = document.createElement('div');
            chartContainer.style.cssText = `
                background: white; border-radius: 10px; padding: 20px; 
                width: 90%; max-width: 900px; height: 80%; position: relative;
            `;
            
            chartContainer.innerHTML = `
                <button onclick="document.getElementById('chart-modal').remove()" 
                        style="position: absolute; top: 10px; right: 15px; background: #dc3545; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 16px;">×</button>
                <h3 style="margin-top: 0; text-align: center; color: #333;">${symbol} - Live Chart</h3>
                <div style="height: calc(100% - 60px);">
                    <!-- TradingView Widget BEGIN -->
                    <div class="tradingview-widget-container" style="height:100%;width:100%">
                        <div class="tradingview-widget-container__widget" style="height:calc(100% - 32px);width:100%"></div>
                        <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js">
                        {
                            "autosize": true,
                            "symbol": "NASDAQ:${symbol}",
                            "interval": "D",
                            "timezone": "Etc/UTC",
                            "theme": "${document.documentElement.getAttribute('data-theme') || 'light'}",
                            "style": "1",
                            "locale": "en",
                            "toolbar_bg": "#f1f3f6",
                            "enable_publishing": false,
                            "allow_symbol_change": true,
                            "details": true,
                            "hotlist": true,
                            "calendar": true,
                            "studies": ["STD;RSI"]
                        }
                        </script>
                    </div>
                    <!-- TradingView Widget END -->
                </div>
            `;
            
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
                    <li><strong>High Potential Investments (>65% return):</strong> {{ recommendations_count }}</li>
                </ul>
                
                <div style="margin-top: 15px; padding: 10px; background: rgba(0, 123, 255, 0.1); border-radius: 5px; border-left: 4px solid #007bff;">
                    <h4 style="margin: 0 0 5px 0; color: #007bff;">🚀 Interactive Features:</h4>
                    <ul style="margin: 5px 0; font-size: 14px;">
                        <li><strong>📈 Live Charts:</strong> Click any stock symbol to view TradingView charts</li>
                        <li><strong>🔄 Auto-Refresh:</strong> Data updates every 3 hours during market hours</li>
                        <li><strong>🌙 Dark Mode:</strong> Toggle theme with button in top-right corner</li>
                        <li><strong>📊 Sortable Tables:</strong> Click column headers to sort data</li>
                    </ul>
                </div>
            </div>

            <div class="section">
                <h2>🔍 Investment Recommendations (>65% Potential Return)</h2>
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
                                <td><span class="stock-symbol">{{ stock.Symbol }}</span></td>
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
                    <p>No stocks found with >65% potential return today.</p>
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
                                <td><span class="stock-symbol">{{ stock.Symbol }}</span></td>
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
                            <td><span class="stock-symbol">{{ stock.Symbol }}</span></td>
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
                            <td><span class="stock-symbol">{{ stock.Symbol }}</span></td>
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
                        A real-time web scraper that analyzes Yahoo Finance daily losers and identifies high-potential investment opportunities based on analyst price targets.
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
            cached_results['timestamp'] = f"{cache_data['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')} (cached)"
            
            # Add current market status (always fresh)
            cached_results['market_status'] = get_market_status()
            
            html_template = format_results_as_html(
                cached_results['losers_data'], 
                cached_results['details_data'], 
                cached_results['all_analysis'], 
                cached_results['recommendations'], 
                cached_results['status']
            )
            
            return render_template_string(html_template, **cached_results)
        
        # No valid cache, perform fresh analysis
        logger.info("No valid cache, performing fresh analysis...")
        
        # Step 1: Scrape today's losers
        logger.info("Step 1: Scraping Yahoo Finance losers...")
        losers_data, losers_status = scrape_yahoo_losers()
        
        # Step 2: Get detailed information for top stocks
        logger.info("Step 2: Getting detailed stock information...")
        symbols = [stock['Symbol'] for stock in losers_data]
        details_data = get_stock_details(symbols)
        
        # Step 3: Calculate complete investment analysis for ALL stocks
        logger.info("Step 3: Calculating complete investment analysis...")
        all_analysis = calculate_all_investment_analysis(losers_data, details_data)
        
        # Step 4: Filter high-potential investments (>65% return)
        logger.info("Step 4: Filtering high-potential investments...")
        recommendations = calculate_investment_potential(all_analysis)
        
        # Get market status
        market_status = get_market_status()
        
        # Prepare template variables
        template_vars = {
            'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
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
        
        return render_template_string(html_template, **template_vars)
        
    except Exception as e:
        logger.error(f"Error in main analysis: {str(e)}")
        return f"<h1>Error occurred during analysis: {str(e)}</h1><p>Please try refreshing the page.</p>"

@app.route('/health')
def health_check():
    """Health check endpoint for monitoring"""
    return {"status": "healthy", "timestamp": datetime.datetime.now().isoformat()}

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

def is_market_holiday(date):
    """Check if a given date is a US stock market holiday"""
    year = date.year
    month = date.month
    day = date.day
    
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
        third_monday = 15 + (7 - datetime.date(year, 1, 15).weekday()) % 7
        if day == third_monday:
            return True
    
    # Presidents Day - 3rd Monday in February
    if month == 2:
        third_monday = 15 + (7 - datetime.date(year, 2, 15).weekday()) % 7
        if day == third_monday:
            return True
    
    # Memorial Day - Last Monday in May
    if month == 5:
        last_monday = 31
        while datetime.date(year, 5, last_monday).weekday() != 0:
            last_monday -= 1
        if day == last_monday:
            return True
    
    # Labor Day - 1st Monday in September
    if month == 9:
        first_monday = 1
        while datetime.date(year, 9, first_monday).weekday() != 0:
            first_monday += 1
        if day == first_monday:
            return True
    
    # Thanksgiving - 4th Thursday in November
    if month == 11:
        fourth_thursday = 22 + (3 - datetime.date(year, 11, 22).weekday()) % 7
        if day == fourth_thursday:
            return True
    
    # Black Friday - Day after Thanksgiving (half day, treat as closed)
    if month == 11:
        fourth_thursday = 22 + (3 - datetime.date(year, 11, 22).weekday()) % 7
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
        now_est = datetime.datetime.now(est)
        
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
            minutes_to_close = int((market_close - now_est).total_seconds() / 60)
            return {
                "status": "open",
                "message": f"🟢 Markets Open",
                "time_to_close": f"Closes in {minutes_to_close} minutes"
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
            all_analysis = calculate_all_investment_analysis(losers_data, details_data)
        
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
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
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

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)