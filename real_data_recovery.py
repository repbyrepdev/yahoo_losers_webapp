import yfinance as yf
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup

def predict_stock_recovery_real_data(symbol):
    """
    REAL DATA recovery prediction using live market data from multiple sources
    Analyzes actual stock performance, analyst data, and market conditions
    """
    try:
        # 1. GET REAL STOCK DATA
        stock = yf.Ticker(symbol)
        hist = stock.history(period="3mo")
        info = stock.info
        
        if hist.empty:
            raise ValueError(f"No data available for {symbol}")
            
        current_price = hist['Close'].iloc[-1]
        prev_close = hist['Close'].iloc[-2] if len(hist) > 1 else current_price
        price_drop = ((current_price - prev_close) / prev_close) * 100
        
        # 2. REAL TECHNICAL INDICATORS
        technical_score = 0
        technical_factors = []
        
        # Real RSI calculation
        if len(hist) >= 14:
            delta = hist['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs)).iloc[-1]
            
            if rsi < 30:
                technical_score += 25
                technical_factors.append(f"🔴 Oversold RSI: {rsi:.1f}")
            elif rsi < 40:
                technical_score += 15
                technical_factors.append(f"🟡 Low RSI: {rsi:.1f}")
        
        # Real Volume Analysis
        current_vol = hist['Volume'].iloc[-1]
        avg_vol = hist['Volume'].tail(20).mean()
        vol_ratio = current_vol / avg_vol if avg_vol > 0 else 1
        
        if vol_ratio > 2:
            technical_score += 20
            technical_factors.append(f"📊 High Volume: {vol_ratio:.1f}x avg")
        elif vol_ratio > 1.5:
            technical_score += 10
            technical_factors.append(f"📊 Above Avg Volume: {vol_ratio:.1f}x")
            
        # 3. REAL FUNDAMENTAL DATA FROM YFINANCE
        fundamental_score = 0
        fundamental_factors = []
        
        try:
            # Real PE ratio
            pe_ratio = info.get('trailingPE', 0)
            if pe_ratio and 5 <= pe_ratio <= 15:
                fundamental_score += 15
                fundamental_factors.append(f"💰 Reasonable P/E: {pe_ratio:.1f}")
            elif pe_ratio and pe_ratio < 5:
                fundamental_score += 20
                fundamental_factors.append(f"💰 Very Low P/E: {pe_ratio:.1f}")
                
            # Real debt-to-equity
            debt_equity = info.get('debtToEquity', 0)
            if debt_equity and debt_equity < 50:
                fundamental_score += 10
                fundamental_factors.append(f"💪 Low Debt/Equity: {debt_equity:.1f}%")
                
            # Real profit margins
            profit_margin = info.get('profitMargins', 0)
            if profit_margin and profit_margin > 0.1:
                fundamental_score += 15
                fundamental_factors.append(f"📈 Good Margins: {profit_margin*100:.1f}%")
                
        except:
            fundamental_score += 5  # Default if data unavailable
            fundamental_factors.append("📊 Limited fundamental data available")
        
        # 4. REAL ANALYST RECOMMENDATIONS (from yfinance)
        analyst_score = 0
        analyst_factors = []
        
        try:
            # Real analyst data
            recommendations = stock.recommendations
            if recommendations is not None and not recommendations.empty:
                latest_rec = recommendations.iloc[-1]
                strong_buy = latest_rec.get('strongBuy', 0)
                buy = latest_rec.get('buy', 0) 
                hold = latest_rec.get('hold', 0)
                sell = latest_rec.get('sell', 0)
                
                total_recs = strong_buy + buy + hold + sell
                if total_recs > 0:
                    buy_ratio = (strong_buy + buy) / total_recs
                    if buy_ratio > 0.6:
                        analyst_score += 20
                        analyst_factors.append(f"👍 {buy_ratio*100:.0f}% BUY ratings")
                    elif buy_ratio > 0.4:
                        analyst_score += 10
                        analyst_factors.append(f"👍 {buy_ratio*100:.0f}% positive ratings")
                        
            # Real price targets
            target_price = info.get('targetMeanPrice', 0)
            if target_price and target_price > current_price:
                upside = ((target_price - current_price) / current_price) * 100
                if upside > 20:
                    analyst_score += 15
                    analyst_factors.append(f"🎯 {upside:.0f}% upside to target")
                elif upside > 10:
                    analyst_score += 8
                    analyst_factors.append(f"🎯 {upside:.0f}% upside to target")
                    
        except:
            analyst_factors.append("📊 Analyst data unavailable")
        
        # 5. REAL MARKET CONDITIONS
        market_score = 0
        market_factors = []
        
        try:
            # Get real market context (SPY for market direction)
            spy = yf.Ticker("SPY")
            spy_hist = spy.history(period="5d")
            if not spy_hist.empty:
                spy_change = ((spy_hist['Close'].iloc[-1] - spy_hist['Close'].iloc[-2]) / spy_hist['Close'].iloc[-2]) * 100
                if spy_change > 0:
                    market_score += 10
                    market_factors.append(f"📈 Market up {spy_change:.1f}%")
                elif spy_change > -2:
                    market_score += 5
                    market_factors.append(f"📊 Market flat {spy_change:.1f}%")
                else:
                    market_factors.append(f"📉 Market down {spy_change:.1f}%")
        except:
            market_factors.append("📊 Market data unavailable")
        
        # CALCULATE FINAL RECOVERY SCORE
        total_score = technical_score + fundamental_score + analyst_score + market_score
        
        # Convert to percentage (scale to 0-100)
        recovery_percentage = min(100, max(0, total_score * 1.2))  # Scale factor
        
        # REAL DATA BASED RECOMMENDATIONS
        if recovery_percentage >= 75:
            confidence = "very_high"
            risk_level = "low"
            recommendation = "🟢 STRONG BUY THE DIP - High recovery probability"
            timeframe = "2-5 days"
        elif recovery_percentage >= 60:
            confidence = "high" 
            risk_level = "moderate"
            recommendation = "🟡 MODERATE BUY - Good recovery chance"
            timeframe = "3-7 days"
        elif recovery_percentage >= 40:
            confidence = "moderate"
            risk_level = "moderate" 
            recommendation = "🟡 WAIT & WATCH - Uncertain outcome"
            timeframe = "5-10 days"
        else:
            confidence = "low"
            risk_level = "high"
            recommendation = "🔴 AVOID - Poor recovery outlook"
            timeframe = "7+ days"
            
        all_factors = {
            'technical': technical_factors,
            'fundamental': fundamental_factors, 
            'analyst': analyst_factors,
            'market': market_factors
        }
        
        return {
            'recovery_score': recovery_percentage,
            'recommendation': recommendation,
            'confidence': confidence,
            'risk_level': risk_level,
            'timeframe': timeframe,
            'factors': all_factors,
            'breakdown': {
                'technical_score': technical_score,
                'fundamental_score': fundamental_score,
                'analyst_score': analyst_score,
                'market_score': market_score
            }
        }
        
    except Exception as e:
        # Fallback with minimal data
        return {
            'recovery_score': 45,
            'recommendation': "🟡 WAIT & WATCH - Limited data available",
            'confidence': "low",
            'risk_level': "moderate", 
            'timeframe': "5-10 days",
            'factors': {'technical': [f"Data error: {str(e)}"]},
            'breakdown': {'technical_score': 0, 'fundamental_score': 0, 'analyst_score': 0, 'market_score': 0}
        }

# Test function
if __name__ == "__main__":
    result = predict_stock_recovery_real_data("AAPL")
    print("REAL DATA RECOVERY PREDICTION:")
    print(f"Score: {result['recovery_score']}")
    print(f"Recommendation: {result['recommendation']}")
    print(f"Factors: {result['factors']}")