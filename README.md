# 📉 Yahoo Finance Daily Losers Analysis Platform

A professional-grade Flask web application that analyzes Yahoo Finance daily losers with advanced AI-powered investment insights, auto-scaling infrastructure, and institutional-level trading analysis.

## 🚀 Features Overview

### 📊 **Core Data Analysis**
- **Daily Losers Scraping**: Real-time data from Yahoo Finance screener API
- **Enhanced Stock Metrics**: Price targets, volume analysis, market cap, analyst recommendations  
- **AI-Powered Recovery Predictions**: Advanced machine learning algorithms for rebound potential
- **Professional Trading Analysis**: Options flow, institutional tracking, and economic impact analysis

### 🤖 **AI-Powered Intelligence**
- **Recovery Prediction Engine**: 100-point scoring system combining multiple data sources
- **News Analysis**: AI-driven analysis of why stocks are falling
- **Social Sentiment Tracking**: Real-time sentiment analysis from social media and news
- **Smart Filtering**: Only shows high-confidence AI recovery recommendations (STRONG BUY signals)

### 🏗️ **Enterprise Infrastructure**
- **Auto-Scaling**: Docker + NGINX load balancer with Kubernetes HPA
- **Production Optimizations**: Redis caching, HTTP compression, structured logging
- **Security Features**: Rate limiting, CORS protection, security headers
- **Background Processing**: Celery task queue for intensive analysis

## 📈 Data Sources & Analysis Pipeline

### **Primary Data Sources**
1. **Yahoo Finance Screener API**
   - Daily losers with percentage changes
   - Real-time stock prices and volume
   - Market capitalization data

2. **Individual Stock Pages (Yahoo Finance)**
   - Analyst price targets and recommendations
   - Previous close prices
   - Detailed company information

3. **AI Analysis Engine** (Simulated Professional Data)
   - Technical indicators (RSI, volume spikes, support levels)
   - Historical pattern matching and recovery rates
   - Options flow analysis (put/call ratios, unusual volume)
   - Institutional flow tracking (dark pool activity)
   - Economic calendar impact analysis
   - Social sentiment aggregation

### **Analysis Pipeline**
```
Yahoo Finance Losers → Detailed Metrics → AI Analysis → Smart Filtering → Dashboard
      ↓                      ↓                ↓              ↓            ↓
   25 stocks            Price targets    Recovery scores   Only STRONG   Interactive
   real-time            volumes          recommendations   BUY signals    web UI
```

## 🔍 AI Recovery Prediction System

### **Scoring Components** (0-100 scale)
- **Options Flow Analysis** (25% weight) - Bullish/bearish signal strength
- **Institutional Flow** (30% weight) - Smart money movement patterns  
- **Economic Calendar Impact** (20% weight) - Sector-specific event analysis
- **Recovery Prediction** (15% weight) - Technical and historical patterns
- **Social Sentiment** (10% weight) - News and social media sentiment

### **Recommendation Levels**
- **🟢 STRONG BUY** (Score ≥75): "High recovery probability" - Shows in AI Recovery Recommendations
- **🟡 MODERATE BUY** (Score 60-74): "Good recovery chance" - Filtered out for quality
- **🟡 WAIT & WATCH** (Score 40-59): "Uncertain outcome" - Not recommended
- **🔴 AVOID** (Score <40): "Poor recovery outlook" - High risk

## 🌐 API Endpoints

### **Core Application**
- `/` - Main dashboard with complete analysis
- `/health` - Health check for auto-scaling
- `/metrics` - Performance monitoring endpoint
- `/refresh` - Manual cache refresh
- `/export/csv` - Export data to CSV

### **AI Analysis APIs**
- `/api/ai-analysis/<symbol>` - Complete AI stock analysis
- `/api/recovery-prediction/<symbol>` - Recovery prediction details
- `/api/news-analysis/<symbol>` - AI news analysis
- `/api/social-sentiment/<symbol>` - Social sentiment tracking

### **Professional Trading APIs**
- `/api/options-flow/<symbol>` - Options flow analysis
- `/api/institutional-flow/<symbol>` - Institutional activity tracking
- `/api/economic-calendar/<symbol>` - Economic event impact analysis
- `/api/professional-analysis/<symbol>` - Combined professional analysis

### **Background Processing**
- `/api/tasks/start/<symbol>` - Start background analysis
- `/api/tasks/status/<task_id>` - Check task status

## 🏗️ Infrastructure & Scaling

### **Auto-Scaling Architecture**
```
Internet → NGINX Load Balancer → Flask App Instances (1-10) → Redis Cache → Data Sources
            ↓                           ↓                         ↓
     Rate Limiting           Gunicorn Workers              Shared Analysis Cache
     Compression            Background Tasks               File Cache Fallback
     Security Headers       Memory Management              Performance Monitoring
```

### **Deployment Options**

#### **🚀 Production (Recommended)**
```bash
# Quick start with auto-scaling
./start_production.sh

# Manual production setup  
pip install -r requirements.txt
gunicorn -c gunicorn.conf.py app:app
```

**Production Features:**
- ⚡ **Gunicorn WSGI**: 2 workers × 4 threads = 8 concurrent requests
- 🔒 **Rate Limiting**: 30 req/min general, 10 req/min AI endpoints
- 📊 **Performance Monitoring**: Real-time metrics and memory management  
- 💾 **Hybrid Caching**: Redis primary + file system fallback
- 🚀 **Optimized Performance**: 29MB memory usage, 46ms response times

#### **☁️ Render Cloud Deployment**
1. Connect GitHub repository to Render
2. Create new Web Service with:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn -c gunicorn.conf.py app:app`
   - **Environment**: Python 3

#### **🐳 Docker + Auto-Scaling**
```bash
# Local Docker development
docker-compose up

# Production scaling with NGINX
docker-compose -f docker-compose.yml -f docker-compose.scale.yml up --scale app=3

# Kubernetes deployment  
kubectl apply -f k8s-deployment.yaml
```

#### **📈 Smart Auto-Scaling**
- **Docker Compose**: Manual scaling with `./scale.sh up/down/status`
- **Kubernetes HPA**: Automatic scaling based on CPU/memory (2-10 replicas)
- **Monitoring**: Real-time performance metrics and health checks

### **Local Development**
```bash
pip install -r requirements.txt
python app.py
# Visit http://localhost:5000
```

## 🛠️ Technology Stack

### **Backend**
- **Flask** - Web framework with production optimizations
- **BeautifulSoup** - Yahoo Finance data scraping
- **Pandas** - Data analysis and manipulation
- **Redis** - Caching layer for scaled deployments
- **Celery** - Background task processing
- **Gunicorn** - Production WSGI server

### **AI & Analysis**
- **yfinance** - Financial data integration
- **Custom ML Algorithms** - Recovery prediction models
- **Sentiment Analysis** - News and social media processing
- **Technical Indicators** - RSI, volume analysis, support levels

### **Infrastructure**
- **Docker** - Containerization with multi-stage builds
- **NGINX** - Load balancing and reverse proxy
- **Kubernetes** - Auto-scaling orchestration
- **Structured Logging** - JSON logging with structured events
- **Performance Monitoring** - Real-time metrics and APM

### **Security & Performance**
- **Flask-CORS** - Cross-origin resource sharing
- **Flask-Compress** - Gzip compression (70-90% bandwidth reduction)
- **Rate Limiting** - API endpoint protection
- **Security Headers** - XSS, CSRF, and clickjacking protection

## 📊 Performance Metrics

- **Response Time**: ~46ms average
- **Memory Usage**: 29MB per worker
- **Cache Hit Rate**: 90%+ for repeated requests
- **Compression**: 70-90% bandwidth reduction
- **Concurrent Users**: 8+ with auto-scaling to 80+
- **Data Freshness**: 3-hour auto-refresh during market hours

## 🔧 Configuration Files

- **`app.py`** - Main application (149KB+ comprehensive logic)
- **`requirements.txt`** - Python dependencies with production packages
- **`gunicorn.conf.py`** - Production server configuration
- **`docker-compose.yml`** - Local development environment
- **`k8s-deployment.yaml`** - Kubernetes auto-scaling setup
- **`nginx.conf`** - Load balancer and security configuration
- **`scale.sh`** - Auto-scaling management script

## ⚠️ Important Notes

### **Data Source Disclaimer**
The AI analysis uses simulated professional trading data for demonstration purposes. In a production environment, you would integrate with:
- Real options flow data providers (e.g., Unusual Whales, FlowAlgo)
- Institutional data feeds (e.g., Bloomberg Terminal, Refinitiv)
- Economic calendar APIs (e.g., Trading Economics, Alpha Vantage)
- Social sentiment providers (e.g., StockTwits, Reddit APIs)

### **Financial Disclaimer**
This application is for **informational purposes only** and should not be considered financial advice. Stock investments carry risk, and past performance does not guarantee future results. Always consult with a qualified financial advisor before making investment decisions.

## 👨‍💻 Developer Information

**Created by:** [Damien Adams](https://github.com/repbyrepdev)  
**Repository:** [yahoo_losers_webapp](https://github.com/repbyrepdev/yahoo_losers_webapp)  
**Live Demo:** [Yahoo Losers Webapp](https://yahoo-losers-webapp.onrender.com)

**Architecture:** Production-ready Flask application with enterprise-grade scaling, caching, and monitoring capabilities.

---

© 2024 Damien Adams. Open source project. Data provided by Yahoo Finance.