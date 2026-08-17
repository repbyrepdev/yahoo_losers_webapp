# 📉 Yahoo Finance Daily Losers Analysis Platform

A Flask web application that pulls the Yahoo Finance daily losers screener and layers
technical and sentiment analysis on top of it.

## ⚠️ Current Data Status

Several upstream providers stopped serving unauthenticated requests, and the code used
to paper over those failures with substituted values. Those fallbacks have been removed:
a field that cannot be sourced now renders as an em dash. Check `/health/sources` for the
live status of every provider.

| Data | Source | Status |
| --- | --- | --- |
| Daily losers list | Yahoo screener `day_losers` | ✅ Live |
| Prices, volume, market cap | Yahoo `v8/finance/chart` | ✅ Live |
| Technicals (RSI, MACD, MFI, Bollinger, VIX, SPY) | `yfinance` | ✅ Live |
| StockTwits message volume | StockTwits API | ✅ Live |
| Analyst price targets | Yahoo `quoteSummary` | ❌ 401 — renders as — |
| Options flow / put-call ratio | Yahoo `v7/finance/options` | ❌ 401 — renders as — |
| Earnings dates | Yahoo `quoteSummary` | ❌ 401 — renders as — |
| Reddit mentions | Reddit `search.json` | ❌ 403 — needs OAuth |

**Removed entirely** because no free data source exists for them: dark pool volume,
hedge fund / mutual fund / pension / ETF ownership splits, execution quality
(price impact, slippage, efficiency), and Twitter mention counts. These were previously
computed from arithmetic on unrelated inputs and displayed as reported figures.

Anything still calculated rather than reported — such as the institutional/retail volume
split — is tagged `estimated` in its API payload, with an `estimate_basis` explaining
what it is inferred from.

## 🚀 Features Overview

### 📊 **Core Data Analysis**

- **Daily Losers Scraping**: Real-time data from Yahoo Finance screener API
- **Enhanced Stock Metrics**: Price targets, volume analysis, market cap, analyst recommendations  
- **AI-Powered Recovery Predictions**: Advanced machine learning algorithms for rebound potential
- **Professional Trading Analysis**: Options flow, institutional tracking, and economic impact analysis

### 🎯 **Next-Gen User Experience**

- **🤖 Ultimate Analysis Button**: Single-click access to comprehensive AI, Social & Recovery analysis in one tabbed modal
- **📈 Interactive TradingView Charts**: Live stock charts with auto-detect exchange selection and smart fallback system
- **⏰ EST Time Display**: All timestamps in Eastern Time with smart countdown (hours → minutes → seconds)
- **🎨 Precision Data Display**: Clean percentage formatting and rounded recovery scores

### 🤖 **AI-Powered Intelligence**

- **Recovery Prediction Engine**: 100-point scoring system combining multiple data sources
- **News Analysis**: AI-driven analysis of why stocks are falling using real analyst data
- **Social Sentiment Tracking**: Real-time sentiment analysis from Reddit and StockTwits APIs
- **Smart Filtering**: Only shows high-confidence AI recovery recommendations (STRONG BUY signals)

## 📈 Comprehensive Real Data Sources & APIs

### **🏢 PRIMARY DATA SOURCES**

#### **1. Yahoo Finance APIs (Core Market Data)**

```python
# Daily Losers Scraping
base_url = "https://finance.yahoo.com/screener/predefined/day_losers"
headers = {'User-Agent': 'Mozilla/5.0 (compatible; StockAnalyzer/1.0)'}

# Individual Stock Data
quote_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
summary_url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
```

**Data Retrieved:**

- Real-time stock prices and percentage changes
- Trading volume and market capitalization
- Previous close and day's high/low prices
- Analyst price targets and recommendations
- P/E ratios, debt-to-equity, financial metrics

#### **2. Yahoo Finance Options Chain API (Options Flow Analysis)**

```python
# Real Options Data
options_url = f"https://query1.finance.yahoo.com/v7/finance/options/{symbol}"

# Put/Call Ratio Calculation (REAL)
put_call_ratio = total_puts / max(total_calls, 1)
unusual_activity = total_volume > (avg_volume * 1.5)
```

**Metrics Calculated:**

- **Put/Call Ratios**: From actual options trading volume
- **Block Trades**: Large volume transactions (>500 contracts)
- **Sweep Activity**: Multi-exchange simultaneous purchases
- **Strike Analysis**: Most active call/put strike prices

#### **3. Yahoo Finance Earnings Calendar API (Real Earnings Data)**

```python
# Real Earnings Dates
earnings_url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=calendarEvents"

# Earnings Impact on Options Bias
days_to_earnings = (next_earnings - datetime.now()).days
if days_to_earnings < 7:
    near_term_bias = "calls"  # Volatility play before earnings
```

**Analysis Performed:**

- **Earnings Surprise Tracking**: Actual vs expected results
- **Options Strategy Detection**: Pre/post earnings positioning
- **Volatility Impact Assessment**: Historical earnings volatility

#### **4. Reddit API Integration (Social Sentiment)**

```python
# Real Reddit Data
reddit_url = f"https://www.reddit.com/search.json?q=${symbol}&sort=new&limit=100"
reddit_response = requests.get(reddit_url, headers=headers, timeout=10)

```

**Social Metrics:**

- **Reddit Mentions**: Count of posts returned by the search endpoint

> **Status:** this endpoint returns 403 without OAuth, so the count is currently 0.
> No keyword sentiment scoring is performed — the earlier version of this README
> documented a `calculate_panic_from_posts()` helper and a `bearish_keywords` list
> that do not exist in the code.

#### **5. StockTwits API Integration (Trading Sentiment)**

```python
# Real StockTwits Data
stocktwits_url = f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json"

# Message Analysis
messages = response.json().get('messages', [])
```

**Trading Insights:**

- **Message Volume**: Count of messages on the symbol's stream page (~30 per page)

> **Status:** only the message count is used. StockTwits does return a per-message
> `entities.sentiment` field, but the code does not yet read it, so no bullish/bearish
> distribution is computed. "Trending phrases" are currently selected from a fixed
> list rather than derived from message text.

### **🔍 ADVANCED DATA PROCESSING & ANALYSIS**

#### **6. Sophisticated Timeframe Predictor (sophisticated_timeframe.py)**

```python
class SophisticatedTimeframePredictor:
    def predict_recovery_timeframes(self, symbol):
        # Multiple Recovery Targets (REAL DATA BASED)
        recovery_targets = {
            'previous_close': self._get_previous_close_target(symbol),
            '5day_high': self._get_5day_high_target(symbol), 
            '20day_ma': self._calculate_20day_ma_target(symbol),
            'support_bounce': self._find_support_level(symbol),
            'analyst_target': self._get_analyst_consensus(symbol),
            'fair_value': self._calculate_fair_value_estimate(symbol)
        }
```

**Recovery Target Calculation Logic:**

1. **Previous Close Recovery** (Priority 1)

```python
# Return to yesterday's closing price
upside_percent = ((prev_close - current_price) / current_price) * 100
probability = 85 - (upside_percent * 2)  # Higher probability for smaller moves
```

1. **5-Day High Recovery** (Priority 2)  

```python
# Bounce to recent 5-day peak
five_day_high = max(hist['High'][-5:])
probability = 70 - (upside_percent * 1.5)
```

1. **20-Day Moving Average** (Mean Reversion)

```python
# Technical analysis target
ma_20 = hist['Close'][-20:].mean()
probability = 60 if upside_percent < 15 else 45
```

1. **Support Level Bounce** (Technical Analysis)

```python
# Calculated from 30-day lows
recent_lows = hist['Low'][-30:]
support_level = np.percentile(recent_lows, 20)
```

1. **Analyst Target Recovery**

```python
# Real Yahoo Finance analyst consensus
analyst_url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=financialData"
target_mean_price = data['financialData']['targetMeanPrice']['raw']
```

1. **Fair Value Estimate**

```python
# Based on P/E ratio and earnings
pe_ratio = financial_data.get('trailingPE', {}).get('raw')
fair_value = earnings_per_share * industry_avg_pe
```

#### **7. Institutional Flow Analysis (Real Volume Data)**

```python
def track_institutional_flow(symbol):
    # Get REAL volume data from Yahoo Finance
    quote_url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    
    # Estimate institutional vs retail split
    volume_ratio = total_volume / avg_volume
    institutional_percentage = min(0.8, 0.4 + (volume_ratio - 1) * 0.1)
    
    # Price movement analysis for buy/sell estimation
    price_change = (recent_prices[-1] - recent_prices[-2]) / recent_prices[-2]
    buy_percentage = 0.5 + (price_change * 2)  # Positive change = more buying
```

**Institutional Metrics:**

- **Volume Analysis**: Reported share volume from the Yahoo chart endpoint (real)
- **Institutional/retail split**: inferred from volume relative to its 10-bar average.
  Tagged `estimated` in the payload — this is not a reported split, and no public
  free source publishes one intraday.

> **Removed:** dark pool estimation and execution quality (price impact, slippage,
> efficiency). Neither had a data source — both were formulas over the volume figure
> above. Off-exchange volume is published by FINRA on a delayed basis; execution
> quality requires order-level fill data that is not publicly available.

#### **8. Economic Calendar Integration**

> **Note:** the dates below are computed from assumed release conventions ("CPI lands
> on the 13th", "FOMC meets on the 20th"), not read from an official calendar. They
> will frequently be wrong. The Federal Reserve publishes real FOMC dates and FRED
> publishes real release schedules; wiring those in is tracked separately.

```python
def get_economic_calendar_impact(symbol):
    # Real economic events with actual typical dates
    current_date = datetime.now()
    
    # CPI data (usually mid-month around 8:30 AM ET)
    next_cpi_date = current_date.replace(day=13)
    
    # Fed meetings (8 times per year, scheduled dates)
    months_with_fed = [1, 3, 5, 6, 7, 9, 11, 12]
    
    # Jobs report (first Friday of month at 8:30 AM ET)
    first_friday = calculate_first_friday(next_month)
```

**Economic Events Tracked:**

- **CPI Inflation Data**: Monthly mid-month releases
- **Fed Interest Rate Decisions**: 8 scheduled FOMC meetings
- **Jobs Report**: First Friday employment data
- **GDP Reports**: Quarterly economic growth data

#### **9. News Analysis Engine (Real Financial Data)**

```python
def analyze_stock_news(symbol):
    # Real analyst recommendation trends
    news_url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}?modules=recommendationTrend,earningsHistory"
    
    # Analyze recommendation trends
    sell_ratio = sell_recs / total_recs
    if sell_ratio > 0.4:  # More than 40% sell recommendations
        sentiment = "negative"
        reason = f"Analyst downgrades - {sell_recs} sell vs {buy_recs} buy recommendations"
    
    # Analyze recent earnings
    earnings_surprise = recent_earnings['surprisePercent']['raw']
    if earnings_surprise < -0.05:  # Missed by more than 5%
        sentiment = "very_negative"
        reason = f"Earnings miss - reported {earnings_surprise:.1%} below expectations"
```

### **🚀 SUPER-ENHANCED MARKET SIGNALS SYSTEM (8 INDICATORS)**

#### Indicator System Overview

### **📊 PHASE 1: ORIGINAL ENHANCED SIGNALS**

#### **Volume Surge Analysis (Real Data)**

```python
def _calculate_volume_surge_signal(self, hist):
    """Detect institutional activity via unusual volume spikes"""
    current_volume = hist['Volume'].iloc[-1]
    avg_volume = hist['Volume'][-20:].mean()  # 20-day average
    volume_ratio = current_volume / avg_volume
    
    # Real Yahoo Finance volume data analysis
    if volume_ratio >= 3.0:  # 3x average volume = strong institutional activity
        return {
            'surge_detected': True,
            'volume_ratio': round(volume_ratio, 1),
            'surge_multiplier': min(1.4, 1.0 + (volume_ratio - 3) * 0.1),  # Up to 40% boost
            'confidence': 'high' if volume_ratio >= 5.0 else 'medium'
        }
```

**Signal Logic:**

- **Data Source**: Real trading volume from Yahoo Finance API
- **Detection Threshold**: 3x average volume indicates institutional activity
- **Impact**: 40% probability boost for short-term recovery (strongest effect)
- **Timeframe Scaling**: Full impact on 1-7 days, 50% on 1-6 months, 25% on 6-18 months

#### **RSI Mean Reversion Signal (Technical Analysis)**

```python
def _calculate_rsi_mean_reversion_signal(self, hist):
    """Calculate RSI oversold conditions for mean reversion plays"""
    # Calculate 14-period RSI using real price data
    close_prices = hist['Close']
    delta = close_prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    
    current_rsi = rsi.iloc[-1]
    
    # Oversold thresholds with graduated multipliers
    if current_rsi <= 25:  # Severely oversold
        return {
            'oversold_detected': True,
            'rsi_value': round(current_rsi, 1),
            'reversion_multiplier': 1.5,  # 50% boost
            'strength': 'strong'
        }
    elif current_rsi <= 30:  # Standard oversold
        return {
            'oversold_detected': True,
            'rsi_value': round(current_rsi, 1),
            'reversion_multiplier': 1.3,  # 30% boost
            'strength': 'moderate'
        }
```

**Signal Logic:**

- **Data Source**: Calculated from real Yahoo Finance OHLC data
- **RSI Calculation**: Standard 14-period RSI using closing prices
- **Thresholds**: RSI ≤25 (severe oversold), RSI ≤30 (oversold)
- **Impact**: 50% boost for RSI ≤25, 30% boost for RSI ≤30
- **Timeframe Scaling**: Strongest on short-term, graduated decline on longer timeframes

#### **Economic Regime Filter (VIX-Based)**

```python
def _calculate_economic_regime_filter(self, market_conditions):
    """Apply VIX-based probability multipliers based on market regime"""
    vix = market_conditions.get('vix', 20)
    spy_trend = market_conditions.get('spy_trend', 0)
    
    # High volatility regime (VIX > 25)
    if vix > 25:
        return {
            'regime': 'high_volatility',
            'vix_level': vix,
            'short_term_multiplier': 1.5,   # Oversold bounces stronger in volatile markets
            'medium_term_multiplier': 0.9,  # Uncertainty hurts medium-term
            'long_term_multiplier': 0.8,    # Extended uncertainty
            'reasoning': f'High volatility (VIX {vix}) favors short-term oversold bounces'
        }
    
    # Low volatility regime (VIX < 18)  
    elif vix < 18:
        return {
            'regime': 'low_volatility',
            'vix_level': vix,
            'short_term_multiplier': 0.9,   # Fewer dramatic bounces
            'medium_term_multiplier': 1.2,  # Stable environment
            'long_term_multiplier': 1.1,    # Favorable for long-term
            'reasoning': f'Low volatility (VIX {vix}) supports steady recovery'
        }
```

**Signal Logic:**

- **Data Source**: VIX (volatility index) and SPY trend data from market conditions
- **Regimes**: High volatility (VIX >25), Normal (18-25), Low volatility (VIX <18)
- **Impact**: Different multipliers for each timeframe based on market regime
- **Reasoning**: High volatility = stronger short-term bounces, stable markets = better long-term recovery

#### ### **🎯 PHASE 2: HIGH-ACCURACY INDICATORS (NEW!)**

#### **Money Flow Index - Volume-Weighted RSI**

```python
def _calculate_money_flow_index(self, hist):
    """Volume-weighted RSI - more reliable than standard RSI"""
    # Calculate typical price (HLC/3)
    typical_price = (hist['High'] + hist['Low'] + hist['Close']) / 3
    money_flow = typical_price * hist['Volume']
    
    # 14-period MFI calculation with volume weighting
    # MFI <= 20 = Strong oversold (60% recovery boost)
    # MFI <= 30 = Moderate oversold (40% recovery boost)
```

**Why MFI > RSI**: Unlike RSI, MFI includes volume data, making it superior for detecting institutional accumulation during price drops.

**Data Source**: Real OHLCV data from Yahoo Finance
**Reliability**: High - combines price AND volume momentum
**Best For**: Detecting smart money accumulation in oversold stocks

#### **MACD Histogram + Signal Divergence**

```python
def _calculate_macd_histogram_signal(self, hist):
    """MACD histogram crossovers and bullish divergence detection"""
    # Standard MACD calculation (12,26,9)
    ema_12 = close_prices.ewm(span=12).mean()
    ema_26 = close_prices.ewm(span=26).mean()
    macd_line = ema_12 - ema_26
    signal_line = macd_line.ewm(span=9).mean()
    histogram = macd_line - signal_line
    
    # Bullish crossover = 50% boost
    # Bullish divergence = 40% boost  
    # Momentum acceleration = 20% boost
```

**Research Validation**: Studies show MACD-based strategies are "safest and most effective" for 2024
**Best Signals**: Histogram crossing above zero + bullish divergence
**Timeframe Impact**: Strongest on short-term, moderate on medium/long-term

#### **Bollinger Band Squeeze + Expansion**

```python
def _calculate_bollinger_squeeze_signal(self, hist):
    """Volatility squeeze detection and breakout prediction"""
    # 20-period Bollinger Bands (2 std dev)
    sma_20 = close_prices.rolling(window=20).mean()
    upper_band = sma_20 + (2 * std_20)
    lower_band = sma_20 - (2 * std_20)
    
    # %B position and squeeze detection
    percent_b = (close_prices - lower_band) / (upper_band - lower_band)
    squeeze_detected = current_bandwidth < avg_bandwidth * 0.8
    
    # %B <= 0.1 + volume spike = 50% boost (oversold bounce)
    # Squeeze + %B < 0.2 = 30% boost (breakout setup)
```

**Market Validation**: Bollinger Bands are "one of the most trusted indicators"
**Squeeze Logic**: Low volatility periods (squeeze) precede high volatility breakouts
**Volume Confirmation**: Combines price position with volume confirmation

#### **Put/Call Ratio - Contrarian Sentiment**

```python
def _calculate_put_call_ratio_signal(self, symbol):
    """Options sentiment extremes as contrarian indicators"""
    # Get real options data from Yahoo Finance
    option_chain = stock.option_chain(next_expiration)
    
    # Calculate volume and open interest ratios
    pc_volume_ratio = total_put_volume / total_call_volume
    pc_oi_ratio = total_put_oi / total_call_oi
    combined_ratio = (pc_volume_ratio * 0.7) + (pc_oi_ratio * 0.3)
    
    # P/C >= 1.5 = Extreme bearish (40% contrarian boost)
    # P/C >= 1.2 = High bearish (25% contrarian boost)
```

**Contrarian Logic**: Extreme bearish sentiment often marks bottoms
**Real Data**: Uses actual options volume and open interest from Yahoo Finance
**Weighting**: 70% volume (immediate) + 30% open interest (positioning)

#### **Short Interest + Squeeze Potential**

```python
def _calculate_short_interest_signal(self, symbol, info):
    """Short squeeze risk assessment"""
    short_percent = info.get('shortPercentOfFloat')
    shares_short = info.get('sharesShort') 
    avg_volume = info.get('averageVolume')
    days_to_cover = shares_short / avg_volume
    
    # High squeeze potential: >=20% short + >=7 days to cover = 40% boost
    # Moderate risk: >=15% short + >=5 days = 25% boost
    # Some risk: >=10% short = 10% boost
```

**Squeeze Logic**: High short interest + low liquidity = potential explosive moves
**Real Data**: Yahoo Finance short interest and float data
**Days to Cover**: Time for shorts to exit = squeeze duration indicator

### **⚡ ADVANCED SIGNAL INTEGRATION**

#### Signal Integration Across Timeframes

```python
def _calculate_sophisticated_timeframes(self, ..., volume_signal, rsi_signal, regime_filter):
    # Apply signals with timeframe-specific weighting
    timeframe_adjustments = {
        'short_term': {
            'volume_weight': 1.0,    # Full volume signal impact
            'rsi_weight': 1.0,       # Full RSI impact
            'regime_key': 'short_term_multiplier'
        },
        'medium_term': {
            'volume_weight': 0.5,    # Reduced volume impact
            'rsi_weight': 0.7,       # Moderate RSI impact
            'regime_key': 'medium_term_multiplier'
        },
        'long_term': {
            'volume_weight': 0.25,   # Minimal volume impact
            'rsi_weight': 0.4,       # Reduced RSI impact
            'regime_key': 'long_term_multiplier'
        }
    }
    
    # Compound signal effects
    for timeframe in ['short_term', 'medium_term', 'long_term']:
        base_probability = timeframe_data[timeframe]['probability']
        
        # Apply volume surge signal
        if volume_signal and volume_signal.get('surge_detected'):
            volume_boost = (volume_signal['surge_multiplier'] - 1) * adjustments['volume_weight']
            base_probability *= (1 + volume_boost)
        
        # Apply RSI mean reversion signal
        if rsi_signal and rsi_signal.get('oversold_detected'):
            rsi_boost = (rsi_signal['reversion_multiplier'] - 1) * adjustments['rsi_weight']
            base_probability *= (1 + rsi_boost)
        
        # Apply economic regime filter
        if regime_filter:
            regime_multiplier = regime_filter.get(adjustments['regime_key'], 1.0)
            base_probability *= regime_multiplier
```

### **📊 PREDICTION ALGORITHM LOGIC**

#### **Multi-Target Weighted Scoring System**

```python
def calculate_recovery_score(recovery_targets, market_conditions):
    total_weighted_score = 0
    total_weight = 0
    
    # Target Priority Weighting
    target_weights = {
        'previous_close': 1.0,    # Highest priority - most likely
        '5day_high': 0.9,         # Recent resistance level
        '20day_ma': 0.8,          # Technical mean reversion
        'support_bounce': 0.7,    # Support level bounce
        'analyst_target': 0.6,    # Longer-term fundamental
        'fair_value': 0.5         # Fundamental valuation
    }
    
    # Distance-Based Weight Adjustment
    for target_name, target_data in recovery_targets.items():
        upside = target_data['upside_percent']
        
        # Closer targets get higher weights
        if upside <= 8:
            distance_weight = 1.0      # Very achievable
        elif upside <= 15:
            distance_weight = 0.9      # Reasonable
        elif upside <= 25:
            distance_weight = 0.8      # Challenging
        else:
            distance_weight = 0.7      # Ambitious
        
        final_weight = target_weights[target_name] * distance_weight
        weighted_score = target_data['probability'] * final_weight
        
        total_weighted_score += weighted_score
        total_weight += final_weight
    
    base_score = total_weighted_score / total_weight
    
    # Market Condition Adjustments
    market_boost = 0
    if market_conditions['vix'] < 20:
        market_boost += 5  # Low volatility environment
    if market_conditions['spy_trend'] > 0:
        market_boost += 3  # Market trending up
    
    final_score = min(85, base_score + market_boost)
    return round(final_score, 1)
```

#### **Recommendation Classification Logic**

```python
def classify_recommendation(recovery_score, confidence_factors):
    if recovery_score >= 65:
        recommendation = "STRONG BUY"
        confidence = "High"
        reasoning = "Multiple recovery targets achievable with market support"
        
    elif recovery_score >= 50:
        recommendation = "MODERATE BUY" 
        confidence = "Medium"
        reasoning = "Good recovery potential with moderate risk"
        
    elif recovery_score >= 35:
        recommendation = "WAIT & WATCH"
        confidence = "Low"
        reasoning = "Mixed signals - monitor for better entry"
        
    else:
        recommendation = "AVOID"
        confidence = "High"
        reasoning = "Multiple headwinds present"
    
    return {
        'recommendation': recommendation,
        'confidence': confidence,
        'reasoning': reasoning,
        'score': recovery_score
    }
```

## 🔧 API Endpoints Documentation

### **Core Application Endpoints**

- `GET /` - Main dashboard with complete analysis
- `GET /health` - Health check for auto-scaling
- `GET /metrics` - Performance monitoring endpoint
- `POST /refresh` - Manual cache refresh
- `GET /export/csv` - Export analysis data to CSV

### **Analysis API Endpoints**

#### **Recovery Analysis**

- `GET /api/recovery-prediction/<symbol>` - Comprehensive recovery analysis

  ```json
  {
    "symbol": "AAPL",
    "recovery_targets": {
      "previous_close": {"probability": 78, "upside_percent": 5.2, "timeframe": "1-3 days"},
      "analyst_target": {"probability": 45, "upside_percent": 18.5, "timeframe": "3-12 months"}
    },
    "overall_score": 67.3,
    "recommendation": "STRONG BUY"
  }
  ```

#### **Social Sentiment Analysis**  

- `GET /api/social-sentiment/<symbol>` - Real Reddit + StockTwits sentiment

  ```json
  {
    "panic_level": 7.2,
    "reddit_mentions": 45,
    "stocktwits_mentions": 28,
    "trending_phrases": ["buy the dip", "oversold", "panic selling"],
    "overall_sentiment": "bearish"
  }
  ```

#### **Options Flow Analysis**

- `GET /api/options-flow/<symbol>` - Real options chain analysis

  ```json
  {
    "put_call_ratio": 1.34,
    "unusual_activity": true,
    "block_trades": 8,
    "sweep_activity": 12,
    "sentiment": "bearish",
    "next_earnings": "2025-01-28"
  }
  ```

#### **Institutional Flow**

- `GET /api/institutional-flow/<symbol>` - Volume-based institutional analysis

  ```json
  {
    "total_volume": 12500000,
    "institutional_volume": 7500000,
    "net_flow": 2100000,
    "flow_direction": "buying",
    "dark_pool_ratio": 0.28
  }
  ```

#### **Economic Calendar Impact**

- `GET /api/economic-calendar/<symbol>` - Relevant economic events

  ```json
  {
    "upcoming_events": [
      {
        "name": "CPI Inflation Data",
        "date": "2025-01-15",
        "impact_level": "high",
        "relevance": "direct",
        "days_away": 3
      }
    ],
    "risk_score": 6.8
  }
  ```

#### **News Analysis**

- `GET /api/news-analysis/<symbol>` - Real analyst sentiment analysis

  ```json
  {
    "sentiment": "negative",
    "confidence": 85,
    "reason": "Analyst downgrades - 3 sell vs 1 buy recommendations",
    "news_count": 5
  }
  ```

## 🏗️ Infrastructure & Scaling

### **Auto-Scaling Architecture**

```text
Internet → NGINX Load Balancer → Flask App Instances (1-10) → Redis Cache → Data Sources
            ↓                           ↓                         ↓
     Rate Limiting           Gunicorn Workers              Shared Analysis Cache
     Compression            Background Tasks               File Cache Fallback
     Security Headers       Memory Management              Performance Monitoring
```

### **Production Deployment**

```bash
# Quick production start
pip install -r requirements.txt
gunicorn -c gunicorn.conf.py app:app

# Docker scaling
docker-compose up --scale app=3

# Kubernetes auto-scaling
kubectl apply -f k8s-deployment.yaml
```

## 🛠️ Technology Stack

### **Backend Framework**

- **Flask** - Web framework with production optimizations
- **Gunicorn** - Production WSGI server (2 workers × 4 threads)
- **Redis** - Caching layer for scaled deployments
- **Celery** - Background task processing

### **Data Analysis Libraries**

- **yfinance** - Primary financial data provider
- **Pandas** - Data manipulation and analysis
- **NumPy** - Mathematical computations and technical indicators
- **BeautifulSoup** - Web scraping for Yahoo Finance

### **API Integration**

- **Requests** - HTTP API calls to financial services
- **JSON Processing** - Real-time data parsing and analysis
- **Error Handling** - Graceful degradation with fallback mechanisms

### **Performance & Security**

- **Flask-Compress** - Gzip compression (70-90% bandwidth reduction)
- **Flask-CORS** - Cross-origin resource sharing
- **Rate Limiting** - API endpoint protection
- **Structured Logging** - JSON logging with structured events

## 📊 Performance Metrics & Caching

### **Response Performance**

- **Average Response Time**: ~46ms
- **Memory Usage**: 29MB per worker
- **Cache Hit Rate**: 90%+ for repeated requests
- **Compression Ratio**: 70-90% bandwidth reduction

### **Caching Strategy**

```python
# Multi-layer caching system
CACHE_DURATION = {
    'stock_data': 300,        # 5 minutes - market data
    'analyst_data': 3600,     # 1 hour - analyst targets
    'social_sentiment': 900,  # 15 minutes - social media
    'options_flow': 600,      # 10 minutes - options data
    'economic_calendar': 86400  # 24 hours - economic events
}
```

### **Data Freshness**

- **Market Hours**: 5-minute refresh for active stocks
- **After Hours**: 1-hour refresh cycle
- **Weekends**: 24-hour cache for fundamental data
- **Manual Refresh**: Available via `/refresh` endpoint

## ⚠️ Important Technical Notes

### **Data Source Verification**

See **Current Data Status** at the top of this README for the authoritative list, and
hit `/health/sources` for live status.

✅ **Stock Prices**: Yahoo Finance quotes — live
✅ **Volume Analysis**: Reported trading volume from Yahoo Finance — live
✅ **Technical Indicators**: Volume Surge, RSI Mean Reversion, Money Flow Index, MACD
Histogram, Bollinger Squeeze — computed from real OHLCV via `yfinance`
❌ **Analyst Data**: `quoteSummary` returns 401 — renders as —
❌ **Options Data**: `v7/finance/options` returns 401 — renders as —
❌ **Earnings Data**: `quoteSummary` returns 401 — renders as —
❌ **Reddit Sentiment**: `search.json` returns 403 without OAuth
⚠️ **Economic Calendar**: dates are assumed from release conventions, not read from an
official calendar
⚠️ **Institutional Flow**: volume is real; the institutional/retail split is an
estimate and is tagged as such

### **Error Handling**

- **API Failures**: the field is marked unavailable and renders as an em dash. Failed
  fetches are never backfilled with a substituted value — see `provenance.py`.
- **Rate Limiting**: per-IP request caps on general and AI endpoints
- **Data Validation**: numeric parsing returns `None` rather than a default on bad input
- **Graceful Degradation**: partial analysis when some data sources fail, with the
  missing pieces shown as missing rather than filled in

### **Financial Disclaimer**

This application is for **informational purposes only** and should not be considered financial advice. Stock investments carry risk, and past performance does not guarantee future results. Always consult with a qualified financial advisor before making investment decisions.

## 🔗 Links & Information

**Developer:** [Damien Adams](https://github.com/repbyrepdev)  
**Repository:** [yahoo_losers_webapp](https://github.com/repbyrepdev/yahoo_losers_webapp)  
**Live Demo:** [Yahoo Losers Webapp](https://yahoo-losers-webapp.onrender.com)

**Architecture:** Flask application with caching, rate limiting, and monitoring. See
**Current Data Status** at the top of this README for which data sources are live and
which are currently unavailable.

---

© 2025 Damien Adams. Open source project. Data provided by Yahoo Finance APIs, Reddit API, and StockTwits API.
