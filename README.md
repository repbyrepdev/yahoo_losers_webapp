# Yahoo Finance Losers Web App

A Flask web application that scrapes Yahoo Finance daily losers and provides investment analysis.

## Features

- **Daily Losers Scraping**: Fetches the biggest stock losers from Yahoo Finance
- **Detailed Stock Analysis**: Gets additional metrics like price targets, volume, and previous close
- **Investment Recommendations**: Identifies stocks with >65% potential return based on analyst price targets
- **HTML Dashboard**: Presents all data in a clean, responsive web interface

## Deployment

### Render Deployment

This app is configured for easy deployment on Render:

1. Connect your GitHub repository to Render
2. Create a new Web Service
3. Use the following settings:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Environment**: Python 3

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python app.py
```

Visit `http://localhost:5000` to see the application.

## API Endpoints

- `/` - Main dashboard with complete analysis
- `/health` - Health check endpoint

## Data Sources

- Yahoo Finance Screener (Day Losers)
- Individual stock quote pages for detailed metrics

## Disclaimer

This application is for informational purposes only and should not be considered financial advice. Always consult with a qualified financial advisor before making investment decisions.