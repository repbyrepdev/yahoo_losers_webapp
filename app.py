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

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create SSL context to handle certificate issues
ssl._create_default_https_context = ssl._create_unverified_context

def scrape_yahoo_losers():
    """Step 1: Scrape day losers from Yahoo Finance"""
    try:
        today = datetime.date.today().strftime('%Y-%m-%d')
        url = "https://finance.yahoo.com/screener/predefined/day_losers"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Find the table containing stock data
        table_rows = soup.find_all('tr', {'class': 'simpTblRow'})
        
        stocks_data = []
        
        for row in table_rows:
            cells = row.find_all('td')
            if len(cells) >= 6:
                symbol = cells[0].get_text(strip=True)
                name = cells[1].get_text(strip=True)
                price = cells[2].get_text(strip=True)
                change = cells[3].get_text(strip=True)
                percent_change = cells[4].get_text(strip=True)
                market_cap = cells[5].get_text(strip=True) if len(cells) > 5 else 'N/A'
                
                stocks_data.append({
                    'Symbol': symbol,
                    'Name': name,
                    'Price': price,
                    'Change': change,
                    'Percent Change': percent_change,
                    'Market Cap': market_cap
                })
        
        return stocks_data
        
    except Exception as e:
        logger.error(f"Error scraping Yahoo Finance losers: {str(e)}")
        return []

def get_stock_details(symbols):
    """Step 2: Get additional stock details"""
    stock_details = []
    
    for symbol in symbols[:20]:  # Limit to first 20 stocks to avoid timeouts
        try:
            url = f"https://finance.yahoo.com/quote/{symbol}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Extract various price points and metrics
            try:
                price_elem = soup.find('fin-streamer', {'data-field': 'regularMarketPrice'})
                current_price = price_elem.get_text(strip=True) if price_elem else 'N/A'
                
                prev_close_elem = soup.find('td', string='Previous Close')
                prev_close = prev_close_elem.find_next_sibling('td').get_text(strip=True) if prev_close_elem else 'N/A'
                
                volume_elem = soup.find('td', string='Volume')
                volume = volume_elem.find_next_sibling('td').get_text(strip=True) if volume_elem else 'N/A'
                
                # Try to find price target
                target_elem = soup.find('td', string='1y Target Est')
                target_price = target_elem.find_next_sibling('td').get_text(strip=True) if target_elem else 'N/A'
                
                stock_details.append({
                    'Symbol': symbol,
                    'Current Price': current_price,
                    'Previous Close': prev_close,
                    'Volume': volume,
                    'Price Target': target_price
                })
                
            except Exception as e:
                logger.error(f"Error extracting details for {symbol}: {str(e)}")
                stock_details.append({
                    'Symbol': symbol,
                    'Current Price': 'N/A',
                    'Previous Close': 'N/A',
                    'Volume': 'N/A',
                    'Price Target': 'N/A'
                })
                
        except Exception as e:
            logger.error(f"Error fetching details for {symbol}: {str(e)}")
            continue
    
    return stock_details

def calculate_investment_potential(losers_data, details_data):
    """Step 3: Calculate investment potential"""
    investment_recommendations = []
    
    # Create lookup dictionary for details
    details_dict = {item['Symbol']: item for item in details_data}
    
    for stock in losers_data:
        symbol = stock['Symbol']
        if symbol in details_dict:
            details = details_dict[symbol]
            
            try:
                # Clean and convert prices
                current_price_str = details['Current Price'].replace('$', '').replace(',', '')
                target_price_str = details['Price Target'].replace('$', '').replace(',', '')
                
                if current_price_str != 'N/A' and target_price_str != 'N/A':
                    current_price = float(current_price_str)
                    target_price = float(target_price_str)
                    
                    if current_price > 0:
                        potential_return = ((target_price - current_price) / current_price) * 100
                        
                        if potential_return > 65:  # Filter for >65% potential return
                            investment_recommendations.append({
                                'Symbol': symbol,
                                'Name': stock['Name'],
                                'Current Price': current_price,
                                'Target Price': target_price,
                                'Potential Return %': round(potential_return, 2),
                                'Volume': details['Volume'],
                                'Change Today': stock['Change'],
                                'Percent Change Today': stock['Percent Change']
                            })
            except (ValueError, TypeError) as e:
                logger.error(f"Error calculating potential for {symbol}: {str(e)}")
                continue
    
    return investment_recommendations

def format_results_as_html(losers_data, details_data, recommendations):
    """Format all results as HTML"""
    
    html_template = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Yahoo Finance Daily Losers Analysis</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; }
            h1, h2 { color: #333; text-align: center; }
            .section { background: white; margin: 20px 0; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            table { width: 100%; border-collapse: collapse; margin: 10px 0; }
            th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #ddd; }
            th { background-color: #f8f9fa; font-weight: bold; }
            .positive { color: #28a745; }
            .negative { color: #dc3545; }
            .highlight { background-color: #fff3cd; }
            .timestamp { text-align: center; color: #666; font-size: 14px; }
            .summary { background-color: #e7f3ff; padding: 15px; border-radius: 5px; margin: 15px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>📉 Yahoo Finance Daily Losers Analysis</h1>
            <div class="timestamp">Generated on: {{ timestamp }}</div>
            
            <div class="summary">
                <h3>📊 Summary</h3>
                <ul>
                    <li><strong>Total Losers Analyzed:</strong> {{ total_losers }}</li>
                    <li><strong>Detailed Analysis:</strong> {{ detailed_count }}</li>
                    <li><strong>High Potential Investments:</strong> {{ recommendations_count }}</li>
                </ul>
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
                                <td><strong>{{ stock.Symbol }}</strong></td>
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
                            <td><strong>{{ stock.Symbol }}</strong></td>
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
                            <td><strong>{{ stock.Symbol }}</strong></td>
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
                <h3>⚠️ Disclaimer</h3>
                <p><em>This analysis is for informational purposes only and should not be considered as financial advice. 
                Stock investments carry risk, and past performance does not guarantee future results. 
                Always consult with a qualified financial advisor before making investment decisions.</em></p>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html_template

@app.route('/')
def index():
    """Main route that runs the Yahoo Finance losers analysis"""
    try:
        # Step 1: Scrape today's losers
        logger.info("Step 1: Scraping Yahoo Finance losers...")
        losers_data = scrape_yahoo_losers()
        
        if not losers_data:
            return "<h1>Error: Could not fetch Yahoo Finance data. Please try again later.</h1>"
        
        # Step 2: Get detailed information for top stocks
        logger.info("Step 2: Getting detailed stock information...")
        symbols = [stock['Symbol'] for stock in losers_data]
        details_data = get_stock_details(symbols)
        
        # Step 3: Calculate investment potential
        logger.info("Step 3: Calculating investment potential...")
        recommendations = calculate_investment_potential(losers_data, details_data)
        
        # Step 4: Format as HTML
        logger.info("Step 4: Formatting results...")
        html_template = format_results_as_html(losers_data, details_data, recommendations)
        
        # Prepare template variables
        template_vars = {
            'timestamp': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC'),
            'total_losers': len(losers_data),
            'detailed_count': len(details_data),
            'recommendations_count': len(recommendations),
            'losers_data': losers_data,
            'details_data': details_data,
            'recommendations': recommendations
        }
        
        return render_template_string(html_template, **template_vars)
        
    except Exception as e:
        logger.error(f"Error in main analysis: {str(e)}")
        return f"<h1>Error occurred during analysis: {str(e)}</h1><p>Please try refreshing the page.</p>"

@app.route('/health')
def health_check():
    """Health check endpoint for monitoring"""
    return {"status": "healthy", "timestamp": datetime.datetime.now().isoformat()}

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)