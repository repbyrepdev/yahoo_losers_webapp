import yfinance as yf
import numpy as np
import pandas as pd
from datetime import datetime, date
from typing import Dict, Tuple
import warnings
import time
import logging
warnings.filterwarnings('ignore')

# Set up logging
logger = logging.getLogger(__name__)

class SophisticatedTimeframePredictor:
    """
    Advanced recovery timeframe prediction using real market data and multiple recovery targets.
    Analyzes historical patterns, market conditions, and catalysts to predict realistic timeframes.
    """
    
    def __init__(self):
        self.vix_thresholds = {
            'low': 20,      # VIX < 20: Calm market
            'medium': 30,   # VIX 20-30: Normal volatility
            'high': 40      # VIX > 40: High fear
        }
        
        # Enhanced market regime thresholds (less conservative)
        self.recovery_thresholds = {
            'strong_buy': 65,    # Lowered from 75
            'buy': 50,           # Lowered from 60  
            'watch': 30          # Lowered from 40
        }
        
        # Free data source configurations
        self.data_sources = {
            'fred_api': 'https://api.stlouisfed.org/fred/series/observations',
            'yahoo_finance': True,  # Already using
            'cboe_vix': 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv',
            'economic_calendar': 'https://api.tradingeconomics.com/calendar',
        }
        
        # Sector ETF mapping for broader market analysis
        self.sector_etfs = {
            'Technology': 'XLK',
            'Healthcare': 'XLV', 
            'Financial': 'XLF',
            'Energy': 'XLE',
            'Utilities': 'XLU',
            'Consumer Discretionary': 'XLY',
            'Consumer Staples': 'XLP',
            'Industrial': 'XLI',
            'Materials': 'XLB',
            'Real Estate': 'XLRE',
            'Communication': 'XLC'
        }
        
        # Cache for API calls to avoid rate limits
        self._cache = {}
        self._cache_timeout = 300  # 5 minutes
        
    def predict_recovery_timeframes(self, symbol: str, preloaded_hist=None,
                                    preloaded_info=None) -> Dict:
        """
        Main function that predicts sophisticated recovery timeframes with multiple targets

        Callers that already hold cached history pass it in, so this does not
        refetch a year of bars on the request path. The company profile is
        optional and non-fatal: under Yahoo's per-IP limiter the .info call was
        the difference between a degraded result and no result at all, because
        its exception escaped to the outer handler and produced the empty
        fallback -- which the UI faithfully rendered as "no targets".
        """
        try:
            # Get real stock data
            stock = yf.Ticker(symbol)
            hist = preloaded_hist if preloaded_hist is not None else stock.history(period="1y")
            if preloaded_info is not None:
                info = preloaded_info
            else:
                try:
                    info = stock.info or {}
                except Exception:
                    info = {}
            
            if hist.empty:
                return self._fallback_prediction(symbol)
                
            current_price = hist['Close'].iloc[-1]
            hist['Close'].iloc[-2] if len(hist) > 1 else current_price
            
            # 1. DEFINE SHORT-TERM RECOVERY TARGETS (1-5 days)
            targets = self._calculate_recovery_targets(stock, hist, info, current_price)
            
            # 2. DEFINE MEDIUM-TERM RECOVERY TARGETS (1-4 weeks)
            medium_targets = self._calculate_medium_term_targets(stock, hist, info, current_price)
            
            # 2. GET MARKET CONDITIONS
            market_conditions = self._analyze_market_conditions()
            
            # 3. HISTORICAL RECOVERY ANALYSIS
            historical_patterns = self._analyze_historical_recovery_patterns(symbol, hist)
            
            # 4. CATALYST ANALYSIS
            catalysts = self._analyze_upcoming_catalysts(symbol, info)
            
            # 5. TECHNICAL MOMENTUM ANALYSIS
            technical_momentum = self._analyze_technical_momentum(hist)
            
            # 6. SECTOR PERFORMANCE CONTEXT
            sector_context = self._analyze_sector_performance(info)
            
            # 7. NEW ENHANCED SIGNALS (EXPANDED WITH HIGH-ACCURACY INDICATORS)
            volume_signal = self._calculate_volume_surge_signal(hist)
            rsi_signal = self._calculate_rsi_mean_reversion_signal(hist)
            regime_filter = self._calculate_economic_regime_filter(market_conditions)
            
            # NEW HIGH-ACCURACY INDICATORS
            mfi_signal = self._calculate_money_flow_index(hist)
            macd_signal = self._calculate_macd_histogram_signal(hist)
            bollinger_signal = self._calculate_bollinger_squeeze_signal(hist)
            put_call_signal = self._calculate_put_call_ratio_signal(symbol)
            short_interest_signal = self._calculate_short_interest_signal(symbol, info)
            
            # 8. CALCULATE SOPHISTICATED TIMEFRAMES (with all enhanced signals)
            timeframe_predictions = self._calculate_sophisticated_timeframes(
                targets, medium_targets, market_conditions, historical_patterns, 
                catalysts, technical_momentum, sector_context, current_price,
                volume_signal, rsi_signal, regime_filter,
                mfi_signal, macd_signal, bollinger_signal, put_call_signal, short_interest_signal
            )
            
            return {
                'symbol': symbol,
                'current_price': round(current_price, 2),
                'targets': targets,
                'medium_targets': medium_targets,
                'market_conditions': market_conditions,
                'historical_patterns': historical_patterns,
                'catalysts': catalysts,
                'technical_momentum': technical_momentum,
                'sector_context': sector_context,
                'timeframe_predictions': timeframe_predictions,
                'confidence_level': self._calculate_confidence_level(
                    historical_patterns, market_conditions, technical_momentum
                ),
                # ENHANCED SIGNALS (EXPANDED WITH HIGH-ACCURACY INDICATORS)
                'enhanced_signals': {
                    'volume_surge': volume_signal,
                    'rsi_reversion': rsi_signal,
                    'economic_regime': regime_filter,
                    # NEW HIGH-ACCURACY SIGNALS
                    'money_flow_index': mfi_signal,
                    'macd_histogram': macd_signal,
                    'bollinger_squeeze': bollinger_signal,
                    'put_call_ratio': put_call_signal,
                    'short_interest': short_interest_signal
                }
            }
            
        except Exception as e:
            print(f"Error in sophisticated timeframe prediction for {symbol}: {e}")
            return self._fallback_prediction(symbol)
    
    def _calculate_recovery_targets(self, stock, hist: pd.DataFrame, info: Dict, current_price: float) -> Dict:
        """Calculate multiple recovery target levels with real data"""
        targets = {}
        
        # Target 1: Previous Close (Day Recovery)
        if len(hist) > 1:
            targets['previous_close'] = {
                'price': round(hist['Close'].iloc[-2], 2),
                'upside_percent': round(((hist['Close'].iloc[-2] - current_price) / current_price) * 100, 2),
                'description': 'Return to yesterday\'s close'
            }
        
        # Target 2: 5-day high (Short-term bounce)
        five_day_high = hist['High'].tail(5).max()
        targets['5day_high'] = {
            'price': round(five_day_high, 2),
            'upside_percent': round(((five_day_high - current_price) / current_price) * 100, 2),
            'description': '5-day high recovery'
        }
        
        # Target 3: 10-day moving average (Short-term technical recovery)
        ma_10 = hist['Close'].tail(10).mean()
        targets['10day_ma'] = {
            'price': round(ma_10, 2),
            'upside_percent': round(((ma_10 - current_price) / current_price) * 100, 2),
            'description': '10-day moving average bounce'
        }
        
        # Target 4: Intraday resistance (Very short-term bounce)
        # Use recent 5-day high/low range for quick reversals
        recent_high = hist['High'].tail(3).max()
        recent_low = hist['Low'].tail(3).min()
        intraday_resistance = recent_low + ((recent_high - recent_low) * 0.618)  # 61.8% Fibonacci
        targets['intraday_resistance'] = {
            'price': round(intraday_resistance, 2),
            'upside_percent': round(((intraday_resistance - current_price) / current_price) * 100, 2),
            'description': 'Short-term resistance level'
        }
        
        # Target 5: Gap fill (Technical gap closure)
        # Look for recent price gaps in the last 5 days that could fill quickly
        if len(hist) >= 5:
            for i in range(1, min(5, len(hist))):
                hist['Low'].iloc[-i] > hist['High'].iloc[-i-1]  # Gap up
                gap_down = hist['High'].iloc[-i] < hist['Low'].iloc[-i-1]  # Gap down
                
                if gap_down and current_price < hist['Low'].iloc[-i-1]:
                    # Current price is below the gap - gap fill opportunity
                    gap_fill_level = hist['Low'].iloc[-i-1]
                    if gap_fill_level > current_price:
                        targets['gap_fill'] = {
                            'price': round(gap_fill_level, 2),
                            'upside_percent': round(((gap_fill_level - current_price) / current_price) * 100, 2),
                            'description': f'Gap fill from {i} days ago'
                        }
                        break
        
        return targets
    
    def _calculate_medium_term_targets(self, stock, hist: pd.DataFrame, info: Dict, current_price: float) -> Dict:
        """Calculate medium-term recovery targets (1-4 weeks)"""
        targets = {}
        
        # Target 1: 20-day moving average (Medium-term technical recovery)
        if len(hist) >= 20:
            ma_20 = hist['Close'].tail(20).mean()
            targets['20day_ma'] = {
                'price': round(ma_20, 2),
                'upside_percent': round(((ma_20 - current_price) / current_price) * 100, 2),
                'description': '20-day moving average reversion'
            }
        
        # Target 2: Support level bounce (Medium-term technical)
        if len(hist) >= 30:
            support_level = hist['Low'].tail(30).min() * 1.02  # 2% above 30-day low
            targets['support_bounce'] = {
                'price': round(support_level, 2),
                'upside_percent': round(((support_level - current_price) / current_price) * 100, 2),
                'description': 'Support level bounce from 30-day low'
            }
        
        # Target 3: Fair value estimate (Medium-term fundamental)
        try:
            pe_ratio = info.get('trailingPE', 0)
            if pe_ratio and pe_ratio > 0:
                # Estimate fair value based on sector average P/E
                sector_avg_pe = 18  # Market average approximation
                fair_value = (current_price / pe_ratio) * sector_avg_pe
                if fair_value > current_price:
                    targets['fair_value'] = {
                        'price': round(fair_value, 2),
                        'upside_percent': round(((fair_value - current_price) / current_price) * 100, 2),
                        'description': 'Estimated fair value (P/E based)'
                    }
        except:
            pass
        
        # Target 4: 50-day moving average (Medium-term trend)
        if len(hist) >= 50:
            ma_50 = hist['Close'].tail(50).mean()
            if ma_50 > current_price:
                targets['50day_ma'] = {
                    'price': round(ma_50, 2),
                    'upside_percent': round(((ma_50 - current_price) / current_price) * 100, 2),
                    'description': '50-day moving average recovery'
                }
        
        return targets
    
    def _analyze_market_conditions(self) -> Dict:
        """Analyze current market conditions using real data"""
        try:
            # Get VIX (fear index)
            vix = yf.Ticker("^VIX")
            vix_hist = vix.history(period="5d")
            current_vix = vix_hist['Close'].iloc[-1] if not vix_hist.empty else 20
            
            # Get SPY (market direction) 
            spy = yf.Ticker("SPY")
            spy_hist = spy.history(period="1mo")
            spy_trend = 'neutral'
            
            if not spy_hist.empty and len(spy_hist) > 5:
                recent_change = ((spy_hist['Close'].iloc[-1] - spy_hist['Close'].iloc[-5]) / spy_hist['Close'].iloc[-5]) * 100
                if recent_change > 2:
                    spy_trend = 'bullish'
                elif recent_change < -2:
                    spy_trend = 'bearish'
            
            # Determine volatility regime
            if current_vix < self.vix_thresholds['low']:
                volatility_regime = 'low'
                market_sentiment = 'complacent'
            elif current_vix < self.vix_thresholds['medium']:
                volatility_regime = 'normal'
                market_sentiment = 'neutral'
            elif current_vix < self.vix_thresholds['high']:
                volatility_regime = 'elevated'
                market_sentiment = 'concerned'
            else:
                volatility_regime = 'extreme'
                market_sentiment = 'fearful'
            
            return {
                'vix_level': round(current_vix, 1),
                'volatility_regime': volatility_regime,
                'market_sentiment': market_sentiment,
                'spy_trend': spy_trend,
                'recovery_multiplier': self._get_recovery_multiplier(volatility_regime, spy_trend)
            }
        except Exception:
            # Fallback market conditions
            return {
                'vix_level': 20.0,
                'volatility_regime': 'normal',
                'market_sentiment': 'neutral',
                'spy_trend': 'neutral',
                'recovery_multiplier': 1.0
            }
    
    def _get_recovery_multiplier(self, volatility_regime: str, spy_trend: str) -> float:
        """Calculate recovery speed multiplier based on market conditions"""
        base_multiplier = 1.0
        
        # Volatility impact
        if volatility_regime == 'low':
            base_multiplier *= 0.8  # Slower recoveries in calm markets
        elif volatility_regime == 'elevated':
            base_multiplier *= 1.2  # Faster reversals
        elif volatility_regime == 'extreme':
            base_multiplier *= 1.5  # Very fast reversals when fear peaks
        
        # Market trend impact
        if spy_trend == 'bullish':
            base_multiplier *= 0.7  # Faster recovery in bull market
        elif spy_trend == 'bearish':
            base_multiplier *= 1.3  # Slower recovery in bear market
        
        return round(base_multiplier, 2)
    
    def _analyze_historical_recovery_patterns(self, symbol: str, hist: pd.DataFrame) -> Dict:
        """Analyze historical recovery patterns for this specific stock"""
        try:
            patterns = {
                'avg_recovery_days': 0,
                'median_recovery_days': 0,
                'fastest_recovery': 0,
                'historical_success_rate': 0,
                'similar_drawdowns': []
            }
            
            if len(hist) < 60:  # Need sufficient history
                return patterns
            
            # Find historical drawdowns similar to current situation
            current_drawdown = self._calculate_current_drawdown(hist)
            recoveries = []
            
            # Look for historical drawdowns of similar magnitude
            highs = hist['High'].rolling(window=20).max()
            drawdowns = (hist['Close'] / highs - 1) * 100
            
            similar_threshold = 2.0  # Within 2% of current drawdown
            for i in range(20, len(hist)-5):  # Leave room for recovery analysis
                historical_drawdown = drawdowns.iloc[i]
                
                # If drawdown is similar to current situation
                if abs(historical_drawdown - current_drawdown) <= similar_threshold and historical_drawdown < -3:
                    # Find how long it took to recover
                    recovery_days = self._find_recovery_days(hist, i)
                    if recovery_days > 0:
                        recoveries.append(recovery_days)
                        patterns['similar_drawdowns'].append({
                            'date': hist.index[i].strftime('%Y-%m-%d'),
                            'drawdown': round(historical_drawdown, 1),
                            'recovery_days': recovery_days
                        })
            
            if recoveries:
                patterns['avg_recovery_days'] = round(float(np.mean(recoveries)), 1)
                patterns['median_recovery_days'] = round(float(np.median(recoveries)), 1)
                patterns['fastest_recovery'] = min(recoveries)
                patterns['historical_success_rate'] = round((len(recoveries) / max(1, len([d for d in drawdowns if d < -3]))) * 100, 1)
            
            return patterns
            
        except Exception:
            return {'avg_recovery_days': 0, 'median_recovery_days': 0, 'fastest_recovery': 0, 'historical_success_rate': 0, 'similar_drawdowns': []}
    
    def _calculate_current_drawdown(self, hist: pd.DataFrame) -> float:
        """Calculate current drawdown from recent high"""
        if len(hist) < 20:
            return 0
        recent_high = hist['High'].tail(20).max()
        current_price = hist['Close'].iloc[-1]
        return ((current_price / recent_high) - 1) * 100
    
    def _find_recovery_days(self, hist: pd.DataFrame, drawdown_index: int) -> int:
        """Find how many days it took to recover from a historical drawdown"""
        drawdown_price = hist['Close'].iloc[drawdown_index]
        recovery_target = drawdown_price * 1.05  # 5% recovery
        
        for days in range(1, min(30, len(hist) - drawdown_index)):  # Look up to 30 days ahead
            future_index = drawdown_index + days
            if future_index >= len(hist):
                break
            if hist['Close'].iloc[future_index] >= recovery_target:
                return days
        return 0
    
    def _analyze_upcoming_catalysts(self, symbol: str, info: Dict) -> Dict:
        """Analyze upcoming catalysts that could affect recovery timing"""
        catalysts = {
            'earnings_days_away': None,
            'next_earnings_date': None,
            'has_upcoming_events': False,
            'catalyst_impact': 'neutral'
        }
        
        try:
            # Get earnings calendar data
            earnings_date = info.get('earningsDate')
            if earnings_date:
                try:
                    if isinstance(earnings_date, list) and earnings_date:
                        next_earnings = earnings_date[0]
                    else:
                        next_earnings = earnings_date
                    
                    # Convert to datetime if it's a timestamp
                    if hasattr(next_earnings, 'date'):
                        next_earnings = next_earnings.date()
                    elif isinstance(next_earnings, str):
                        next_earnings = datetime.strptime(next_earnings, '%Y-%m-%d').date()
                    
                    days_to_earnings = (next_earnings - date.today()).days
                    
                    if 0 <= days_to_earnings <= 30:  # Earnings within 30 days
                        catalysts['earnings_days_away'] = days_to_earnings
                        catalysts['next_earnings_date'] = next_earnings.strftime('%Y-%m-%d')
                        catalysts['has_upcoming_events'] = True
                        
                        # Impact assessment
                        if days_to_earnings <= 7:
                            catalysts['catalyst_impact'] = 'high'  # Earnings very soon
                        elif days_to_earnings <= 14:
                            catalysts['catalyst_impact'] = 'moderate'
                        else:
                            catalysts['catalyst_impact'] = 'low'
                            
                except Exception:
                    pass
            
            # Check for dividend dates (simplified - would need more data in production)
            if info.get('dividendYield', 0) > 0.02:  # 2%+ dividend yield
                catalysts['has_dividend_support'] = True
            
            return catalysts
            
        except Exception:
            return catalysts
    
    def _analyze_technical_momentum(self, hist: pd.DataFrame) -> Dict:
        """Analyze technical momentum indicators for recovery prediction"""
        try:
            momentum = {
                'rsi': 50,
                'volume_surge': False,
                'trend_strength': 'weak',
                'momentum_score': 0
            }
            
            if len(hist) < 14:
                return momentum
            
            # RSI calculation
            delta = hist['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs)).iloc[-1]
            momentum['rsi'] = round(rsi, 1)
            
            # Volume analysis
            current_vol = hist['Volume'].iloc[-1]
            avg_vol = hist['Volume'].tail(20).mean()
            if current_vol > avg_vol * 1.5:
                momentum['volume_surge'] = True
            
            # Price momentum (last 5 days)
            recent_change = ((hist['Close'].iloc[-1] - hist['Close'].iloc[-5]) / hist['Close'].iloc[-5]) * 100
            if abs(recent_change) > 5:
                momentum['trend_strength'] = 'strong'
            elif abs(recent_change) > 2:
                momentum['trend_strength'] = 'moderate'
            
            # Combined momentum score
            score = 0
            if rsi < 30:
                score += 3  # Oversold
            elif rsi < 40:
                score += 1
            
            if momentum['volume_surge']:
                score += 2
            
            if momentum['trend_strength'] == 'strong':
                score += 1
            
            momentum['momentum_score'] = score
            return momentum
            
        except Exception:
            return {'rsi': 50, 'volume_surge': False, 'trend_strength': 'weak', 'momentum_score': 0}
    
    def _analyze_sector_performance(self, info: Dict) -> Dict:
        """Analyze sector performance for context"""
        try:
            sector = info.get('sector', 'Unknown')
            industry = info.get('industry', 'Unknown')
            
            # Get sector ETF performance (simplified mapping)
            sector_etfs = {
                'Technology': 'XLK',
                'Healthcare': 'XLV', 
                'Financial Services': 'XLF',
                'Consumer Cyclical': 'XLY',
                'Energy': 'XLE',
                'Industrials': 'XLI',
                'Consumer Defensive': 'XLP',
                'Utilities': 'XLU',
                'Real Estate': 'XLRE',
                'Materials': 'XLB',
                'Communication Services': 'XLC'
            }
            
            sector_performance = 'neutral'
            sector_trend = 'flat'
            
            if sector in sector_etfs:
                try:
                    sector_etf = yf.Ticker(sector_etfs[sector])
                    sector_hist = sector_etf.history(period="1mo")
                    if not sector_hist.empty and len(sector_hist) > 5:
                        sector_change = ((sector_hist['Close'].iloc[-1] - sector_hist['Close'].iloc[-5]) / sector_hist['Close'].iloc[-5]) * 100
                        
                        if sector_change > 3:
                            sector_performance = 'strong'
                            sector_trend = 'uptrend'
                        elif sector_change > 0:
                            sector_performance = 'positive'
                            sector_trend = 'slight_up'
                        elif sector_change < -3:
                            sector_performance = 'weak'
                            sector_trend = 'downtrend'
                        elif sector_change < 0:
                            sector_performance = 'negative'
                            sector_trend = 'slight_down'
                except:
                    pass
            
            return {
                'sector': sector,
                'industry': industry,
                'sector_performance': sector_performance,
                'sector_trend': sector_trend,
                'recovery_context': self._get_sector_recovery_context(sector_performance)
            }
            
        except Exception:
            return {
                'sector': 'Unknown',
                'industry': 'Unknown', 
                'sector_performance': 'neutral',
                'sector_trend': 'flat',
                'recovery_context': 'Sector performance unavailable'
            }
    
    def _get_sector_recovery_context(self, sector_performance: str) -> str:
        """Get recovery context based on sector performance"""
        contexts = {
            'strong': 'Sector tailwinds support faster recovery',
            'positive': 'Favorable sector conditions',
            'neutral': 'Mixed sector signals',
            'negative': 'Sector headwinds may slow recovery',
            'weak': 'Strong sector headwinds present'
        }
        return contexts.get(sector_performance, 'Sector impact unclear')
    
    def _calculate_volume_surge_signal(self, hist: pd.DataFrame) -> Dict:
        """
        SIGNAL 1: Volume Surge Analysis - Detect institutional activity
        High reliability predictor of short-term reversals
        """
        try:
            if len(hist) < 20:
                return {'surge_detected': False, 'surge_multiplier': 1.0, 'volume_percentile': 50}
            
            current_volume = hist['Volume'].iloc[-1]
            avg_20_volume = hist['Volume'].tail(20).mean()
            volume_ratio = current_volume / avg_20_volume if avg_20_volume > 0 else 1.0
            
            # Calculate volume percentile over last 60 days
            volume_60d = hist['Volume'].tail(60)
            volume_percentile = (volume_60d < current_volume).sum() / len(volume_60d) * 100
            
            # Volume surge thresholds
            surge_detected = False
            surge_multiplier = 1.0
            
            if volume_ratio >= 3.0:  # 3x average volume
                surge_detected = True
                surge_multiplier = 1.4  # 40% boost to recovery probability
            elif volume_ratio >= 2.0:  # 2x average volume  
                surge_detected = True
                surge_multiplier = 1.25  # 25% boost
            elif volume_ratio >= 1.5:  # 1.5x average volume
                surge_detected = True
                surge_multiplier = 1.15  # 15% boost
            
            return {
                'surge_detected': surge_detected,
                'volume_ratio': round(volume_ratio, 2),
                'surge_multiplier': surge_multiplier,
                'volume_percentile': round(volume_percentile, 1),
                'signal_strength': 'strong' if volume_ratio >= 2.5 else 'moderate' if volume_ratio >= 1.8 else 'weak'
            }
        except Exception:
            return {'surge_detected': False, 'surge_multiplier': 1.0, 'volume_percentile': 50}
    
    def _calculate_rsi_mean_reversion_signal(self, hist: pd.DataFrame) -> Dict:
        """
        SIGNAL 2: RSI Mean Reversion - Detect oversold conditions with improving momentum
        Applies to all timeframes with different thresholds
        """
        try:
            if len(hist) < 14:
                return {'oversold': False, 'rsi': 50, 'reversion_multiplier': 1.0}
            
            # Calculate RSI
            delta = hist['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs)).iloc[-1]
            
            # RSI momentum (compare last 3 days)
            rsi_series = 100 - (100 / (1 + rs))
            rsi_momentum = rsi_series.iloc[-1] - rsi_series.iloc[-4] if len(rsi_series) >= 4 else 0
            
            # Mean reversion thresholds
            oversold = False
            timeframe_multipliers = {'short': 1.0, 'medium': 1.0, 'long': 1.0}
            
            if rsi <= 25:  # Extremely oversold
                oversold = True
                timeframe_multipliers = {'short': 1.5, 'medium': 1.3, 'long': 1.2}
            elif rsi <= 30:  # Oversold
                oversold = True  
                timeframe_multipliers = {'short': 1.3, 'medium': 1.2, 'long': 1.1}
            elif rsi <= 35:  # Approaching oversold
                timeframe_multipliers = {'short': 1.15, 'medium': 1.1, 'long': 1.05}
            
            # Boost if RSI is improving (momentum up)
            if rsi_momentum > 2:  # RSI rising
                for key in timeframe_multipliers:
                    timeframe_multipliers[key] *= 1.1
            
            return {
                'oversold': oversold,
                'rsi': round(rsi, 1),
                'rsi_momentum': round(rsi_momentum, 2),
                'timeframe_multipliers': timeframe_multipliers,
                'signal_strength': 'strong' if rsi <= 25 else 'moderate' if rsi <= 30 else 'weak'
            }
        except Exception:
            return {'oversold': False, 'rsi': 50, 'reversion_multiplier': 1.0}
    
    def _calculate_economic_regime_filter(self, market_conditions: Dict) -> Dict:
        """
        SIGNAL 3: Economic Regime Filter - VIX-based recovery probability multipliers  
        Applies to all timeframes as market structure determines recovery speed
        """
        try:
            vix_level = market_conditions.get('vix', 20)
            
            # Economic regime classification
            if vix_level < 15:
                regime = 'ultra_low_vol'
                regime_multipliers = {'short': 0.8, 'medium': 0.9, 'long': 1.0}  # Harder to get sharp reversals
                recovery_environment = 'Complacent market - limited reversal opportunities'
            elif vix_level < 20:
                regime = 'low_vol'
                regime_multipliers = {'short': 0.9, 'medium': 0.95, 'long': 1.0}
                recovery_environment = 'Calm conditions - gradual recovery patterns'
            elif vix_level < 25:
                regime = 'normal_vol'
                regime_multipliers = {'short': 1.0, 'medium': 1.0, 'long': 1.0}  # Base case
                recovery_environment = 'Normal volatility - balanced recovery potential'
            elif vix_level < 35:
                regime = 'elevated_vol'
                regime_multipliers = {'short': 1.3, 'medium': 1.2, 'long': 1.1}  # Better reversal conditions
                recovery_environment = 'Elevated fear - good reversal opportunities'
            else:
                regime = 'high_vol'  
                regime_multipliers = {'short': 1.5, 'medium': 1.3, 'long': 1.15}  # Excellent reversal potential
                recovery_environment = 'High fear environment - strong reversal potential'
            
            # Market regime impact on different timeframes
            regime_impact = {
                'short': 'VIX impacts short-term reversals most strongly',
                'medium': 'Elevated volatility supports medium-term recoveries', 
                'long': 'Economic uncertainty affects long-term valuations'
            }
            
            return {
                'regime': regime,
                'vix_level': vix_level,
                'regime_multipliers': regime_multipliers,
                'recovery_environment': recovery_environment,
                'regime_impact': regime_impact
            }
        except Exception:
            return {
                'regime': 'normal_vol',
                'regime_multipliers': {'short': 1.0, 'medium': 1.0, 'long': 1.0},
                'recovery_environment': 'Normal market conditions'
            }
    
    def _calculate_sophisticated_timeframes(self, targets: Dict, medium_targets: Dict, market_conditions: Dict, 
                                         historical_patterns: Dict, catalysts: Dict,
                                         technical_momentum: Dict, sector_context: Dict,
                                         current_price: float, volume_signal: Dict = None,
                                         rsi_signal: Dict = None, regime_filter: Dict = None,
                                         mfi_signal: Dict = None, macd_signal: Dict = None, 
                                         bollinger_signal: Dict = None, put_call_signal: Dict = None,
                                         short_interest_signal: Dict = None) -> Dict:
        """Calculate sophisticated timeframes for each recovery target"""
        
        predictions = {}
        # TRUE SHORT-TERM recovery timeframes (all under 1 week)
        base_recovery_days = {
            'previous_close': 1,           # Same day/next day recovery
            '5day_high': 2,               # 2-3 days to retest recent high  
            '10day_ma': 3,                # 3-5 days for MA reversion
            'intraday_resistance': 1,     # Intraday to next day
            'gap_fill': 1                 # Gaps typically fill quickly (1-2 days)
        }
        
        for target_name, target_data in targets.items():
            if target_name not in base_recovery_days:
                continue
                
            base_days = base_recovery_days[target_name]
            upside_percent = target_data.get('upside_percent', 0)
            
            # Skip targets that are below current price
            if upside_percent <= 0:
                continue
            
            # Apply market conditions multiplier
            adjusted_days = base_days * market_conditions.get('recovery_multiplier', 1.0)
            
            # Apply historical pattern adjustments
            if historical_patterns.get('avg_recovery_days', 0) > 0:
                historical_factor = historical_patterns['avg_recovery_days'] / base_days
                historical_factor = min(2.0, max(0.5, historical_factor))  # Cap between 0.5x and 2x
                adjusted_days *= historical_factor
            
            # Apply catalyst adjustments
            if catalysts.get('has_upcoming_events', False):
                earnings_days = catalysts.get('earnings_days_away', 30)
                if earnings_days <= 7:
                    adjusted_days *= 0.8  # Faster recovery before earnings
                elif earnings_days <= 14:
                    adjusted_days *= 0.9
            
            # Apply technical momentum
            momentum_score = technical_momentum.get('momentum_score', 0)
            if momentum_score >= 4:
                adjusted_days *= 0.7  # Strong momentum = faster recovery
            elif momentum_score >= 2:
                adjusted_days *= 0.85
            elif momentum_score == 0:
                adjusted_days *= 1.2  # Weak momentum = slower recovery
            
            # Apply sector performance
            sector_perf = sector_context.get('sector_performance', 'neutral')
            if sector_perf == 'strong':
                adjusted_days *= 0.8
            elif sector_perf == 'positive':
                adjusted_days *= 0.9
            elif sector_perf == 'negative':
                adjusted_days *= 1.1
            elif sector_perf == 'weak':
                adjusted_days *= 1.3
            
            # APPLY NEW ENHANCED SIGNALS
            signal_multiplier = 1.0
            
            # 1. Volume Surge Signal (strongest for short-term)
            if volume_signal and volume_signal.get('surge_detected', False):
                volume_boost = volume_signal.get('surge_multiplier', 1.0)
                adjusted_days *= (2.0 - volume_boost)  # Convert multiplier to time reduction
                signal_multiplier *= volume_boost
            
            # 2. RSI Mean Reversion Signal (all timeframes)
            if rsi_signal and rsi_signal.get('oversold', False):
                timeframe_key = 'short'  # This section is for short-term
                rsi_boost = rsi_signal.get('timeframe_multipliers', {}).get(timeframe_key, 1.0)
                adjusted_days *= (2.0 - rsi_boost)  # Convert multiplier to time reduction
                signal_multiplier *= rsi_boost
            
            # 3. Economic Regime Filter (affects all timeframes)
            if regime_filter:
                regime_boost = regime_filter.get('regime_multipliers', {}).get('short', 1.0)
                adjusted_days *= (2.0 - regime_boost)  # Convert multiplier to time reduction  
                signal_multiplier *= regime_boost
            
            # 4. Money Flow Index Signal (volume-weighted RSI - more reliable)
            if mfi_signal and mfi_signal.get('oversold_detected', False):
                mfi_boost = mfi_signal.get('recovery_multiplier', 1.0)
                adjusted_days *= (2.0 - mfi_boost)  # MFI affects time to recovery
                signal_multiplier *= mfi_boost
            
            # 5. MACD Histogram Signal (momentum shift detection)
            if macd_signal and macd_signal.get('momentum_shift', False):
                macd_boost = macd_signal.get('recovery_multiplier', 1.0)
                adjusted_days *= (2.0 - macd_boost)  # Momentum affects recovery speed
                signal_multiplier *= macd_boost
            
            # 6. Bollinger Band Squeeze Signal (breakout prediction)
            if bollinger_signal and bollinger_signal.get('signal_type') in ['oversold_bounce', 'squeeze_setup', 'oversold']:
                bb_boost = bollinger_signal.get('recovery_multiplier', 1.0)
                adjusted_days *= (2.0 - bb_boost)  # Bollinger signals affect timing
                signal_multiplier *= bb_boost
            
            # 7. Put/Call Ratio Signal (contrarian sentiment)
            if put_call_signal and put_call_signal.get('extreme_sentiment', False):
                pc_boost = put_call_signal.get('recovery_multiplier', 1.0)
                adjusted_days *= (2.0 - pc_boost)  # Sentiment extremes affect recovery
                signal_multiplier *= pc_boost
            
            # 8. Short Interest Signal (squeeze potential)
            if short_interest_signal and short_interest_signal.get('squeeze_potential', False):
                si_boost = short_interest_signal.get('recovery_multiplier', 1.0)
                adjusted_days *= (2.0 - si_boost)  # Short squeeze affects recovery speed
                signal_multiplier *= si_boost
            
            # Calculate probability based on multiple factors (including new signals)
            probability = self._calculate_recovery_probability(
                upside_percent, historical_patterns, market_conditions,
                technical_momentum, sector_context
            )
            
            # Apply signal multiplier to probability (capped at 95%)
            probability = min(probability * signal_multiplier, 95.0)
            
            # Format timeframe
            timeframe_str = self._format_timeframe(adjusted_days)
            
            predictions[target_name] = {
                'target_price': target_data['price'],
                'upside_percent': upside_percent,
                'expected_days': round(adjusted_days, 1),
                'timeframe': timeframe_str,
                'probability': probability,
                'confidence': self._get_confidence_level(probability),
                'description': target_data['description']
            }
        
        # MEDIUM-TERM recovery timeframes (1-4 weeks)
        medium_predictions = {}
        medium_base_recovery_days = {
            '20day_ma': 14,              # 2 weeks for MA reversion
            'support_bounce': 21,        # 3 weeks for support bounce
            'fair_value': 28,            # 4 weeks for fundamental recovery
            '50day_ma': 21               # 3 weeks for longer MA reversion
        }
        
        for target_name, target_data in medium_targets.items():
            if target_name not in medium_base_recovery_days:
                continue
                
            base_days = medium_base_recovery_days[target_name]
            upside_percent = target_data.get('upside_percent', 0)
            
            # Skip targets that are below current price
            if upside_percent <= 0:
                continue
            
            # Apply market conditions multiplier (less impact for medium-term)
            adjusted_days = base_days * (1 + (market_conditions.get('recovery_multiplier', 1.0) - 1) * 0.5)
            
            # Apply historical pattern adjustments (medium-term patterns)
            if historical_patterns.get('avg_recovery_days', 0) > 0:
                historical_factor = historical_patterns['avg_recovery_days'] / (base_days / 2)  # Different weighting for medium-term
                historical_factor = min(1.8, max(0.6, historical_factor))
                adjusted_days *= historical_factor
            
            # APPLY NEW ENHANCED SIGNALS (Medium-term)
            med_signal_multiplier = 1.0
            
            # Volume surge has moderate impact on medium-term
            if volume_signal and volume_signal.get('surge_detected', False):
                volume_boost = volume_signal.get('surge_multiplier', 1.0)
                med_volume_boost = 1.0 + ((volume_boost - 1.0) * 0.7)  # Reduce impact for medium-term
                adjusted_days *= (2.0 - med_volume_boost)
                med_signal_multiplier *= med_volume_boost
            
            # RSI signal for medium-term
            if rsi_signal:
                rsi_boost = rsi_signal.get('timeframe_multipliers', {}).get('medium', 1.0)
                adjusted_days *= (2.0 - rsi_boost)
                med_signal_multiplier *= rsi_boost
            
            # Economic regime filter (medium-term)
            if regime_filter:
                regime_boost = regime_filter.get('regime_multipliers', {}).get('medium', 1.0)
                adjusted_days *= (2.0 - regime_boost)
                med_signal_multiplier *= regime_boost
            
            # NEW INDICATORS (Medium-term with reduced impact)
            # Money Flow Index (50% impact for medium-term)
            if mfi_signal and mfi_signal.get('oversold_detected', False):
                mfi_boost = 1.0 + ((mfi_signal.get('recovery_multiplier', 1.0) - 1.0) * 0.5)
                adjusted_days *= (2.0 - mfi_boost)
                med_signal_multiplier *= mfi_boost
            
            # MACD Histogram (70% impact for medium-term)
            if macd_signal and macd_signal.get('momentum_shift', False):
                macd_boost = 1.0 + ((macd_signal.get('recovery_multiplier', 1.0) - 1.0) * 0.7)
                adjusted_days *= (2.0 - macd_boost)
                med_signal_multiplier *= macd_boost
            
            # Bollinger Bands (60% impact for medium-term)
            if bollinger_signal and bollinger_signal.get('signal_type') in ['oversold_bounce', 'squeeze_setup', 'oversold']:
                bb_boost = 1.0 + ((bollinger_signal.get('recovery_multiplier', 1.0) - 1.0) * 0.6)
                adjusted_days *= (2.0 - bb_boost)
                med_signal_multiplier *= bb_boost
            
            # Put/Call Ratio (40% impact for medium-term)
            if put_call_signal and put_call_signal.get('extreme_sentiment', False):
                pc_boost = 1.0 + ((put_call_signal.get('recovery_multiplier', 1.0) - 1.0) * 0.4)
                adjusted_days *= (2.0 - pc_boost)
                med_signal_multiplier *= pc_boost
            
            # Short Interest (30% impact for medium-term)
            if short_interest_signal and short_interest_signal.get('squeeze_potential', False):
                si_boost = 1.0 + ((short_interest_signal.get('recovery_multiplier', 1.0) - 1.0) * 0.3)
                adjusted_days *= (2.0 - si_boost)
                med_signal_multiplier *= si_boost
            
            # Calculate probability for medium-term targets
            probability = self._calculate_recovery_probability(
                upside_percent, historical_patterns, market_conditions,
                technical_momentum, sector_context
            )
            # Medium-term targets are generally more reliable, so boost probability slightly
            probability = min(85, probability + 5)
            
            # Apply medium-term signal multiplier
            probability = min(probability * med_signal_multiplier, 90.0)
            
            # Format timeframe
            timeframe_str = self._format_timeframe(adjusted_days)
            
            medium_predictions[target_name] = {
                'target_price': target_data['price'],
                'upside_percent': upside_percent,
                'expected_days': round(adjusted_days, 1),
                'timeframe': timeframe_str,
                'probability': probability,
                'confidence': self._get_confidence_level(probability),
                'description': target_data['description']
            }
        
        return {
            'short_term': predictions,
            'medium_term': medium_predictions
        }
    
    def _calculate_recovery_probability(self, upside_percent: float, historical_patterns: Dict,
                                     market_conditions: Dict, technical_momentum: Dict,
                                     sector_context: Dict) -> float:
        """Calculate probability of reaching recovery target"""
        
        base_probability = 70  # Start with 70% base
        
        # Adjust for upside magnitude
        if upside_percent > 20:
            base_probability -= 15  # Large moves less likely
        elif upside_percent > 10:
            base_probability -= 8
        elif upside_percent < 3:
            base_probability += 10  # Small moves more likely
        
        # Historical success rate
        historical_success = historical_patterns.get('historical_success_rate', 50)
        if historical_success > 70:
            base_probability += 10
        elif historical_success < 30:
            base_probability -= 15
        
        # Market conditions
        volatility = market_conditions.get('volatility_regime', 'normal')
        if volatility == 'extreme':
            base_probability += 15  # High volatility = higher reversal chance
        elif volatility == 'low':
            base_probability -= 10  # Low vol = less movement
        
        # Technical momentum
        momentum_score = technical_momentum.get('momentum_score', 0)
        base_probability += momentum_score * 3  # Each momentum point adds 3%
        
        # Sector performance
        sector_perf = sector_context.get('sector_performance', 'neutral')
        if sector_perf == 'strong':
            base_probability += 8
        elif sector_perf == 'weak':
            base_probability -= 12
        
        # Cap between 10% and 90%
        return max(10, min(90, round(base_probability, 1)))
    
    def _format_timeframe(self, days: float) -> str:
        """Format days into readable timeframe"""
        if days < 1:
            hours = int(days * 24)
            return f"{hours} hours"
        elif days < 7:
            return f"{int(days)} days"
        elif days < 30:
            weeks = int(days / 7)
            remaining_days = int(days % 7)
            if remaining_days > 0:
                return f"{weeks}w {remaining_days}d"
            else:
                return f"{weeks} weeks"
        else:
            months = int(days / 30)
            remaining_days = int(days % 30)
            if remaining_days > 7:
                remaining_weeks = int(remaining_days / 7)
                return f"{months}m {remaining_weeks}w"
            else:
                return f"{months} months"
    
    def _get_confidence_level(self, probability: float) -> str:
        """Convert probability to confidence level"""
        if probability >= 80:
            return "Very High"
        elif probability >= 65:
            return "High"
        elif probability >= 45:
            return "Moderate"
        elif probability >= 30:
            return "Low"
        else:
            return "Very Low"
    
    def _calculate_confidence_level(self, historical_patterns: Dict, market_conditions: Dict,
                                  technical_momentum: Dict) -> str:
        """Calculate overall confidence in predictions"""
        confidence_score = 0
        
        # Historical data availability
        if historical_patterns.get('similar_drawdowns'):
            confidence_score += 2
        
        # Market clarity
        if market_conditions.get('volatility_regime') in ['normal', 'elevated']:
            confidence_score += 1
        
        # Technical clarity
        if technical_momentum.get('momentum_score', 0) > 2:
            confidence_score += 1
        
        if confidence_score >= 4:
            return "High"
        elif confidence_score >= 2:
            return "Moderate"
        else:
            return "Low"
    
    def _get_cached_data(self, key: str):
        """Get cached data to avoid excessive API calls"""
        if key in self._cache:
            timestamp, data = self._cache[key]
            if time.time() - timestamp < self._cache_timeout:
                return data
        return None
    
    def _cache_data(self, key: str, data):
        """Cache data with timestamp"""
        self._cache[key] = (time.time(), data)
    
    def _get_market_breadth(self) -> Dict:
        """Get broader market context using free data sources"""
        try:
            # Get SPY and QQQ for market direction
            spy = yf.Ticker("SPY").history(period="1mo")
            qqq = yf.Ticker("QQQ").history(period="1mo") 
            
            if spy.empty or qqq.empty:
                return {'spy_trend': 'neutral', 'qqq_trend': 'neutral', 'market_momentum': 0}
            
            # Calculate momentum
            spy_5d_change = ((spy['Close'].iloc[-1] - spy['Close'].iloc[-5]) / spy['Close'].iloc[-5]) * 100
            qqq_5d_change = ((qqq['Close'].iloc[-1] - qqq['Close'].iloc[-5]) / qqq['Close'].iloc[-5]) * 100
            
            spy_trend = 'bullish' if spy_5d_change > 2 else 'bearish' if spy_5d_change < -2 else 'neutral'
            qqq_trend = 'bullish' if qqq_5d_change > 2 else 'bearish' if qqq_5d_change < -2 else 'neutral'
            
            # Overall market momentum (helps with recovery probability)
            market_momentum = (spy_5d_change + qqq_5d_change) / 2
            
            return {
                'spy_trend': spy_trend,
                'spy_5d_change': round(spy_5d_change, 2),
                'qqq_trend': qqq_trend, 
                'qqq_5d_change': round(qqq_5d_change, 2),
                'market_momentum': round(market_momentum, 2),
                'market_supportive': market_momentum > -1  # Market not in free fall
            }
            
        except Exception as e:
            logger.warning(f"Error getting market breadth: {e}")
            return {'spy_trend': 'neutral', 'qqq_trend': 'neutral', 'market_momentum': 0, 'market_supportive': True}
    
    def _get_enhanced_sector_analysis(self, info: Dict) -> Dict:
        """Enhanced sector analysis using sector ETFs"""
        try:
            sector = info.get('sector', 'Unknown')
            etf_symbol = self.sector_etfs.get(sector)
            
            if not etf_symbol:
                return {'sector_momentum': 0, 'sector_trend': 'neutral', 'relative_strength': 'neutral'}
            
            # Get sector ETF data
            sector_etf = yf.Ticker(etf_symbol)
            etf_hist = sector_etf.history(period="1mo")
            
            if etf_hist.empty:
                return {'sector_momentum': 0, 'sector_trend': 'neutral', 'relative_strength': 'neutral'}
            
            # Calculate sector momentum
            sector_5d_change = ((etf_hist['Close'].iloc[-1] - etf_hist['Close'].iloc[-5]) / etf_hist['Close'].iloc[-5]) * 100
            sector_trend = 'bullish' if sector_5d_change > 1 else 'bearish' if sector_5d_change < -1 else 'neutral'
            
            # Compare to SPY for relative strength
            spy = yf.Ticker("SPY").history(period="1mo")
            if not spy.empty:
                spy_5d_change = ((spy['Close'].iloc[-1] - spy['Close'].iloc[-5]) / spy['Close'].iloc[-5]) * 100
                relative_performance = sector_5d_change - spy_5d_change
                relative_strength = 'outperforming' if relative_performance > 1 else 'underperforming' if relative_performance < -1 else 'neutral'
            else:
                relative_strength = 'neutral'
            
            return {
                'sector': sector,
                'sector_etf': etf_symbol,
                'sector_momentum': round(sector_5d_change, 2),
                'sector_trend': sector_trend,
                'relative_strength': relative_strength,
                'sector_supportive': sector_5d_change > -2  # Sector not collapsing
            }
            
        except Exception as e:
            logger.warning(f"Error in enhanced sector analysis: {e}")
            return {'sector_momentum': 0, 'sector_trend': 'neutral', 'relative_strength': 'neutral', 'sector_supportive': True}
    
    def _calculate_enhanced_recovery_score(self, targets: Dict, market_conditions: Dict, 
                                         technical_momentum: Dict, sector_analysis: Dict, 
                                         market_breadth: Dict) -> Tuple[float, Dict]:
        """Calculate enhanced recovery score with less conservative weighting"""
        try:
            if not targets:
                return 25.0, {}
            
            weighted_scores = []
            target_details = []
            
            for target_name, target_data in targets.items():
                if target_data.get('upside_percent', 0) <= 0:
                    continue
                
                upside_percent = target_data['upside_percent']
                base_probability = target_data.get('probability', 50)
                
                # LESS CONSERVATIVE WEIGHTING - More generous for larger moves
                if upside_percent <= 8:
                    weight_factor = 1.0
                elif upside_percent <= 15:
                    weight_factor = 0.9  # Was 0.8
                elif upside_percent <= 25:
                    weight_factor = 0.8  # Was 0.6
                else:
                    weight_factor = 0.7  # Was 0.6
                
                # Market condition boosts (NEW - helps with recovery)
                market_boost = 0
                if market_breadth.get('market_supportive', False):
                    market_boost += 5
                if sector_analysis.get('sector_supportive', False):
                    market_boost += 3
                if technical_momentum.get('momentum_score', 0) > 2:
                    market_boost += 5
                
                # Enhanced probability with market context
                enhanced_probability = min(85, base_probability + market_boost)
                
                weighted_score = enhanced_probability * weight_factor
                weighted_scores.append(weighted_score)
                
                # Store detailed breakdown for transparency
                target_details.append({
                    'target_type': target_name.replace('_', ' ').title(),
                    'probability': round(enhanced_probability, 1),
                    'upside_percent': round(upside_percent, 1),
                    'weight_factor': weight_factor,
                    'weighted_contribution': round(weighted_score, 1),
                    'reasoning': f"Base: {base_probability}% + Market boost: +{market_boost}% × {weight_factor} weight"
                })
            
            if not weighted_scores:
                return 25.0, {'target_details': []}
            
            # Calculate base score
            base_score = sum(weighted_scores) / len(weighted_scores)
            
            # Market regime adjustment (less harsh than before)
            volatility_regime = market_conditions.get('volatility_regime', 'normal')
            if volatility_regime == 'extreme':
                adjustment = 8  # Was 10
            elif volatility_regime == 'elevated':
                adjustment = 4  # Was 5
            elif volatility_regime == 'low':
                adjustment = -3  # Was -5
            else:
                adjustment = 0
            
            final_score = max(5, min(95, base_score + adjustment))
            
            score_breakdown = {
                'base_score': round(base_score, 1),
                'market_adjustment': adjustment,
                'volatility_regime': volatility_regime,
                'target_details': target_details
            }
            
            return final_score, score_breakdown
            
        except Exception as e:
            logger.error(f"Error calculating enhanced recovery score: {e}")
            return 25.0, {}
    
    def _calculate_money_flow_index(self, hist: pd.DataFrame) -> Dict:
        """
        SIGNAL 4: Money Flow Index - Volume-weighted RSI for institutional activity detection
        More reliable than RSI as it includes volume - catches smart money accumulation
        """
        try:
            if len(hist) < 14:
                return None
            
            # Calculate typical price (HLC/3)
            typical_price = (hist['High'] + hist['Low'] + hist['Close']) / 3
            
            # Calculate money flow (typical price * volume)
            money_flow = typical_price * hist['Volume']
            
            # Positive and negative money flow
            positive_mf = []
            negative_mf = []
            
            for i in range(1, len(typical_price)):
                if typical_price.iloc[i] > typical_price.iloc[i-1]:
                    positive_mf.append(money_flow.iloc[i])
                    negative_mf.append(0)
                elif typical_price.iloc[i] < typical_price.iloc[i-1]:
                    positive_mf.append(0)
                    negative_mf.append(money_flow.iloc[i])
                else:
                    positive_mf.append(0)
                    negative_mf.append(0)
            
            # Calculate 14-period positive and negative money flow
            positive_mf = pd.Series([0] + positive_mf)  # Add initial 0
            negative_mf = pd.Series([0] + negative_mf)  # Add initial 0
            
            pos_mf_14 = positive_mf.rolling(window=14).sum()
            neg_mf_14 = negative_mf.rolling(window=14).sum()
            
            # Calculate Money Flow Index
            money_ratio = pos_mf_14 / neg_mf_14.replace(0, 1)  # Avoid division by zero
            mfi = 100 - (100 / (1 + money_ratio))
            
            current_mfi = mfi.iloc[-1]
            
            # MFI signals (more reliable than RSI due to volume)
            if current_mfi <= 20:  # Strong oversold with volume confirmation
                return {
                    'oversold_detected': True,
                    'mfi_value': round(current_mfi, 1),
                    'signal_strength': 'strong',
                    'volume_confirmed': True,
                    'recovery_multiplier': 1.6,  # 60% boost - stronger than RSI
                    'description': f'MFI oversold at {current_mfi:.1f} with volume confirmation'
                }
            elif current_mfi <= 30:  # Moderate oversold with volume
                return {
                    'oversold_detected': True,
                    'mfi_value': round(current_mfi, 1),
                    'signal_strength': 'moderate',
                    'volume_confirmed': True,
                    'recovery_multiplier': 1.4,  # 40% boost
                    'description': f'MFI oversold at {current_mfi:.1f}'
                }
            else:
                return {
                    'oversold_detected': False,
                    'mfi_value': round(current_mfi, 1),
                    'signal_strength': 'neutral',
                    'volume_confirmed': False,
                    'recovery_multiplier': 1.0,
                    'description': f'MFI neutral at {current_mfi:.1f}'
                }
                
        except Exception as e:
            logger.warning(f"Error calculating Money Flow Index: {e}")
            return None
    
    def _calculate_macd_histogram_signal(self, hist: pd.DataFrame) -> Dict:
        """
        SIGNAL 5: MACD Histogram + Signal Line Divergence
        Research shows MACD-based strategies are "safest and most effective" for 2024
        """
        try:
            if len(hist) < 26:
                return None
            
            close_prices = hist['Close']
            
            # Calculate MACD components
            ema_12 = close_prices.ewm(span=12).mean()
            ema_26 = close_prices.ewm(span=26).mean()
            macd_line = ema_12 - ema_26
            signal_line = macd_line.ewm(span=9).mean()
            histogram = macd_line - signal_line
            
            current_histogram = histogram.iloc[-1]
            prev_histogram = histogram.iloc[-2]
            current_macd = macd_line.iloc[-1]
            signal_line.iloc[-1]
            
            # Check for bullish divergence (price declining, MACD improving)
            price_trend = (close_prices.iloc[-1] - close_prices.iloc[-5]) / close_prices.iloc[-5]
            macd_trend = (current_macd - macd_line.iloc[-5]) / abs(macd_line.iloc[-5])
            
            bullish_divergence = price_trend < -0.02 and macd_trend > 0.05  # Price down 2%+, MACD improving
            
            # MACD signals
            if current_histogram > 0 and prev_histogram <= 0:  # Histogram crossing above zero
                return {
                    'momentum_shift': True,
                    'macd_value': round(current_macd, 4),
                    'histogram_value': round(current_histogram, 4),
                    'signal_type': 'bullish_crossover',
                    'recovery_multiplier': 1.5,  # 50% boost
                    'description': 'MACD histogram bullish crossover - momentum shift detected'
                }
            elif bullish_divergence:  # Bullish divergence
                return {
                    'momentum_shift': True,
                    'macd_value': round(current_macd, 4),
                    'histogram_value': round(current_histogram, 4),
                    'signal_type': 'bullish_divergence',
                    'recovery_multiplier': 1.4,  # 40% boost
                    'description': 'MACD bullish divergence - price/momentum disconnect'
                }
            elif current_histogram > prev_histogram and current_histogram > 0:  # Strengthening momentum
                return {
                    'momentum_shift': True,
                    'macd_value': round(current_macd, 4),
                    'histogram_value': round(current_histogram, 4),
                    'signal_type': 'momentum_acceleration',
                    'recovery_multiplier': 1.2,  # 20% boost
                    'description': 'MACD histogram strengthening - positive momentum'
                }
            else:
                return {
                    'momentum_shift': False,
                    'macd_value': round(current_macd, 4),
                    'histogram_value': round(current_histogram, 4),
                    'signal_type': 'neutral',
                    'recovery_multiplier': 1.0,
                    'description': 'MACD neutral - no clear momentum signal'
                }
                
        except Exception as e:
            logger.warning(f"Error calculating MACD histogram signal: {e}")
            return None
    
    def _calculate_bollinger_squeeze_signal(self, hist: pd.DataFrame) -> Dict:
        """
        SIGNAL 6: Bollinger Band Squeeze + Expansion signals
        "One of the most trusted indicators" - predicts breakout direction and timing
        """
        try:
            if len(hist) < 20:
                return None
            
            close_prices = hist['Close']
            
            # Calculate Bollinger Bands (20-period, 2 std dev)
            sma_20 = close_prices.rolling(window=20).mean()
            std_20 = close_prices.rolling(window=20).std()
            upper_band = sma_20 + (2 * std_20)
            lower_band = sma_20 - (2 * std_20)
            
            # Calculate %B (position within bands)
            percent_b = (close_prices - lower_band) / (upper_band - lower_band)
            
            # Calculate band width
            band_width = (upper_band - lower_band) / sma_20
            avg_bandwidth = band_width.rolling(window=20).mean()
            
            current_bandwidth = band_width.iloc[-1]
            current_percent_b = percent_b.iloc[-1]
            close_prices.iloc[-1]
            lower_band.iloc[-1]
            upper_band.iloc[-1]
            
            # Squeeze detection (bandwidth below average)
            squeeze_ratio = current_bandwidth / avg_bandwidth.iloc[-1] if avg_bandwidth.iloc[-1] > 0 else 1
            is_squeeze = squeeze_ratio < 0.8  # Bands tighter than 80% of average
            
            # Bollinger signals
            if current_percent_b <= 0.1 and hist['Volume'].iloc[-1] > hist['Volume'].rolling(20).mean().iloc[-1] * 1.5:
                # Price near lower band + volume spike = bounce signal
                return {
                    'squeeze_detected': is_squeeze,
                    'percent_b': round(current_percent_b, 3),
                    'squeeze_ratio': round(squeeze_ratio, 2),
                    'signal_type': 'oversold_bounce',
                    'recovery_multiplier': 1.5,  # 50% boost
                    'description': f'Bollinger oversold bounce - %B at {current_percent_b:.2f} with volume'
                }
            elif is_squeeze and current_percent_b < 0.2:  # Squeeze + oversold setup
                return {
                    'squeeze_detected': True,
                    'percent_b': round(current_percent_b, 3),
                    'squeeze_ratio': round(squeeze_ratio, 2),
                    'signal_type': 'squeeze_setup',
                    'recovery_multiplier': 1.3,  # 30% boost
                    'description': f'Bollinger squeeze setup - breakout likely, %B at {current_percent_b:.2f}'
                }
            elif current_percent_b <= 0.15:  # Near lower band
                return {
                    'squeeze_detected': is_squeeze,
                    'percent_b': round(current_percent_b, 3),
                    'squeeze_ratio': round(squeeze_ratio, 2),
                    'signal_type': 'oversold',
                    'recovery_multiplier': 1.2,  # 20% boost
                    'description': f'Bollinger oversold - %B at {current_percent_b:.2f}'
                }
            else:
                return {
                    'squeeze_detected': is_squeeze,
                    'percent_b': round(current_percent_b, 3),
                    'squeeze_ratio': round(squeeze_ratio, 2),
                    'signal_type': 'neutral',
                    'recovery_multiplier': 1.0,
                    'description': f'Bollinger neutral - %B at {current_percent_b:.2f}'
                }
                
        except Exception as e:
            logger.warning(f"Error calculating Bollinger Band squeeze signal: {e}")
            return None
    
    def _calculate_put_call_ratio_signal(self, symbol: str) -> Dict:
        """
        SIGNAL 7: Put/Call Ratio Analysis from real options data
        Contrarian indicator - extreme bearish sentiment = buying opportunity
        """
        try:
            import yfinance as yf
            
            # Get options data from Yahoo Finance
            stock = yf.Ticker(symbol)
            
            try:
                # Get next expiration date
                exp_dates = stock.options
                if not exp_dates:
                    return None
                
                next_exp = exp_dates[0]  # Nearest expiration
                option_chain = stock.option_chain(next_exp)
                
                calls = option_chain.calls
                puts = option_chain.puts
                
                # Calculate put/call ratio by volume and open interest
                total_call_volume = calls['volume'].fillna(0).sum()
                total_put_volume = puts['volume'].fillna(0).sum()
                total_call_oi = calls['openInterest'].fillna(0).sum()
                total_put_oi = puts['openInterest'].fillna(0).sum()
                
                # Volume-based P/C ratio (more immediate)
                pc_volume_ratio = total_put_volume / max(total_call_volume, 1)
                # Open Interest-based P/C ratio (longer-term positioning)
                pc_oi_ratio = total_put_oi / max(total_call_oi, 1)
                
                # Combined ratio (weighted toward volume for immediate sentiment)
                combined_pc_ratio = (pc_volume_ratio * 0.7) + (pc_oi_ratio * 0.3)
                
                # Put/Call ratio signals (contrarian)
                if combined_pc_ratio >= 1.5:  # Extreme bearish sentiment
                    return {
                        'extreme_sentiment': True,
                        'pc_ratio': round(combined_pc_ratio, 2),
                        'pc_volume_ratio': round(pc_volume_ratio, 2),
                        'pc_oi_ratio': round(pc_oi_ratio, 2),
                        'sentiment_type': 'extreme_bearish',
                        'recovery_multiplier': 1.4,  # 40% boost - contrarian signal
                        'description': f'Extreme bearish sentiment P/C {combined_pc_ratio:.2f} - contrarian buy'
                    }
                elif combined_pc_ratio >= 1.2:  # High bearish sentiment
                    return {
                        'extreme_sentiment': True,
                        'pc_ratio': round(combined_pc_ratio, 2),
                        'pc_volume_ratio': round(pc_volume_ratio, 2),
                        'pc_oi_ratio': round(pc_oi_ratio, 2),
                        'sentiment_type': 'high_bearish',
                        'recovery_multiplier': 1.25,  # 25% boost
                        'description': f'High bearish sentiment P/C {combined_pc_ratio:.2f} - potential reversal'
                    }
                else:
                    return {
                        'extreme_sentiment': False,
                        'pc_ratio': round(combined_pc_ratio, 2),
                        'pc_volume_ratio': round(pc_volume_ratio, 2),
                        'pc_oi_ratio': round(pc_oi_ratio, 2),
                        'sentiment_type': 'neutral',
                        'recovery_multiplier': 1.0,
                        'description': f'Neutral options sentiment P/C {combined_pc_ratio:.2f}'
                    }
                    
            except Exception as e:
                logger.warning(f"Could not get options data for {symbol}: {e}")
                return None
                
        except Exception as e:
            logger.warning(f"Error calculating Put/Call ratio signal: {e}")
            return None
    
    def _calculate_short_interest_signal(self, symbol: str, info: Dict) -> Dict:
        """
        SIGNAL 8: Short Interest + Days to Cover Analysis
        High short interest with low volume = potential short squeeze
        """
        try:
            
            # Get short interest data from Yahoo Finance info
            short_percent = info.get('shortPercentOfFloat')
            shares_short = info.get('sharesShort')
            avg_volume = info.get('averageVolume')
            
            if not all([short_percent, shares_short, avg_volume]):
                return None
            
            # Calculate days to cover (shares short / average daily volume)
            days_to_cover = shares_short / max(avg_volume, 1)
            
            # Short interest thresholds
            if short_percent >= 20 and days_to_cover >= 7:  # High short interest + high days to cover
                return {
                    'squeeze_potential': True,
                    'short_percent': round(short_percent, 1),
                    'days_to_cover': round(days_to_cover, 1),
                    'squeeze_risk': 'high',
                    'recovery_multiplier': 1.4,  # 40% boost - potential squeeze
                    'description': f'High short squeeze risk: {short_percent}% short, {days_to_cover:.1f} days to cover'
                }
            elif short_percent >= 15 and days_to_cover >= 5:  # Moderate short interest
                return {
                    'squeeze_potential': True,
                    'short_percent': round(short_percent, 1),
                    'days_to_cover': round(days_to_cover, 1),
                    'squeeze_risk': 'moderate',
                    'recovery_multiplier': 1.25,  # 25% boost
                    'description': f'Moderate short squeeze risk: {short_percent}% short, {days_to_cover:.1f} days to cover'
                }
            elif short_percent >= 10:  # Some short interest
                return {
                    'squeeze_potential': False,
                    'short_percent': round(short_percent, 1),
                    'days_to_cover': round(days_to_cover, 1),
                    'squeeze_risk': 'low',
                    'recovery_multiplier': 1.1,  # 10% boost
                    'description': f'Low short interest: {short_percent}% short, {days_to_cover:.1f} days to cover'
                }
            else:
                return {
                    'squeeze_potential': False,
                    'short_percent': round(short_percent, 1) if short_percent else 0,
                    'days_to_cover': round(days_to_cover, 1),
                    'squeeze_risk': 'minimal',
                    'recovery_multiplier': 1.0,
                    'description': f'Minimal short interest: {short_percent or 0}% short'
                }
                
        except Exception as e:
            logger.warning(f"Error calculating short interest signal: {e}")
            return None
    
    def _fallback_prediction(self, symbol: str) -> Dict:
        """Fallback prediction when data is insufficient"""
        return {
            'symbol': symbol,
            'current_price': 0,
            'targets': {},
            'market_conditions': {'vix_level': 20.0, 'volatility_regime': 'normal'},
            'historical_patterns': {'avg_recovery_days': 0},
            'catalysts': {'has_upcoming_events': False},
            'technical_momentum': {'momentum_score': 0},
            'sector_context': {'sector': 'Unknown'},
            'timeframe_predictions': {},
            'confidence_level': 'Low'
        }

# Test function
if __name__ == "__main__":
    predictor = SophisticatedTimeframePredictor()
    result = predictor.predict_recovery_timeframes("AAPL")
    
    print("🚀 SOPHISTICATED RECOVERY TIMEFRAME ANALYSIS")
    print(f"Symbol: {result['symbol']}")
    print(f"Current Price: ${result['current_price']}")
    print(f"Market Conditions: VIX {result['market_conditions']['vix_level']} ({result['market_conditions']['volatility_regime']})")
    print(f"Overall Confidence: {result['confidence_level']}")
    print("\n📈 RECOVERY TARGETS & TIMEFRAMES:")
    
    for target_name, prediction in result['timeframe_predictions'].items():
        print(f"• {prediction['description']}: ${prediction['target_price']} ({prediction['upside_percent']:+.1f}%) - {prediction['timeframe']} ({prediction['probability']:.0f}% probability)")