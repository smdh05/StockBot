import logging
import urllib.request
import json
from typing import Dict, Optional, Tuple
import pandas as pd
import numpy as np

logger = logging.getLogger("StrategyEngine")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class SMCSignalDetector:
    """
    Parses OHLCV historical arrays via pandas to identify institutional SMC patterns
    including Liquidity Sweeps and Order Blocks.
    """

    @staticmethod
    def identify_liquidity_sweeps(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
        """
        Identifies candle intervals where wicks sweep key support/resistance barriers
        and immediately snap back within the trading range.
        
        Input DataFrame must contain columns: ['open', 'high', 'low', 'close']
        """
        df = df.copy()
        df['bullish_sweep'] = False
        df['bearish_sweep'] = False
        
        # Shifted high/low to avoid lookahead bias
        df['prev_high_barrier'] = df['high'].shift(1).rolling(window=lookback-1).max()
        df['prev_low_barrier'] = df['low'].shift(1).rolling(window=lookback-1).min()
        
        for i in range(lookback, len(df)):
            row = df.iloc[i]
            prev_high = row['prev_high_barrier']
            prev_low = row['prev_low_barrier']
            
            # Bearish Liquidity Sweep (Swept Highs):
            # Current high breaches historical high, but close snaps back below the barrier.
            if row['high'] > prev_high and row['close'] < prev_high:
                df.at[df.index[i], 'bearish_sweep'] = True
                
            # Bullish Liquidity Sweep (Swept Lows):
            # Current low breaches historical low, but close snaps back above the barrier.
            if row['low'] < prev_low and row['close'] > prev_low:
                df.at[df.index[i], 'bullish_sweep'] = True
                
        return df

    @staticmethod
    def find_order_blocks(df: pd.DataFrame, volume_multiplier: float = 1.8) -> List[Dict]:
        """
        Locates institutional Order Blocks (OB).
        A Bullish Order Block is defined as the last bearish candle before an explosive upward expansion
        that breaks structure (BOS) with above-average volume.
        
        Returns a list of order blocks with price zones.
        """
        order_blocks = []
        df = df.copy()
        
        # Calculate volume average to detect institutional entry
        df['volume_sma'] = df['volume'].rolling(window=20).mean()
        
        # Loop to identify breakouts
        for i in range(2, len(df) - 1):
            # Identify explosive bullish candle (breakout)
            current_vol = df.iloc[i]['volume']
            avg_vol = df.iloc[i]['volume_sma']
            
            is_explosive_up = (
                df.iloc[i]['close'] > df.iloc[i]['open'] and 
                (df.iloc[i]['close'] - df.iloc[i]['open']) > (df.iloc[i-1]['high'] - df.iloc[i-1]['low']) * 2 and
                current_vol > avg_vol * volume_multiplier
            )
            
            if is_explosive_up:
                # Look for the last bearish candle (the Order Block)
                for j in range(i-1, max(0, i-5), -1):
                    prior_candle = df.iloc[j]
                    if prior_candle['close'] < prior_candle['open']:
                        # Found Bullish Order Block
                        ob_zone = {
                            "type": "BULLISH_OB",
                            "index": j,
                            "top_zone": prior_candle['high'],
                            "bottom_zone": prior_candle['low'],
                            "volume": prior_candle['volume'],
                            "breakout_index": i
                        }
                        order_blocks.append(ob_zone)
                        break
                        
            is_explosive_down = (
                df.iloc[i]['close'] < df.iloc[i]['open'] and
                (df.iloc[i]['open'] - df.iloc[i]['close']) > (df.iloc[i-1]['high'] - df.iloc[i-1]['low']) * 2 and
                current_vol > avg_vol * volume_multiplier
            )
            
            if is_explosive_down:
                # Look for the last bullish candle (Bearish Order Block)
                for j in range(i-1, max(0, i-5), -1):
                    prior_candle = df.iloc[j]
                    if prior_candle['close'] > prior_candle['open']:
                        ob_zone = {
                            "type": "BEARISH_OB",
                            "index": j,
                            "top_zone": prior_candle['high'],
                            "bottom_zone": prior_candle['low'],
                            "volume": prior_candle['volume'],
                            "breakout_index": i
                        }
                        order_blocks.append(ob_zone)
                        break
                        
        return order_blocks


class GlobalSentimentLayer:
    """
    Parses macro factors to output a Daily Market Bias.
    Indices: S&P500/Nasdaq, FX: USD-INR, Commodities: Brent/WTI Crude Oil.
    """

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    def fetch_overnight_metrics(self) -> Dict[str, float]:
        """
        Fetches macro tickers. In production, this hits external global market APIs (e.g. Yahoo Finance/AlphaVantage).
        Returns rate changes compared to the prior day.
        """
        # Mock/simulated data parser matching real API structures
        logger.info("Accessing overnight global macroeconomic feeds...")
        try:
            # Simulated responses (would perform requests.get in production environment)
            return {
                "usd_inr_change_pct": 0.05,       # USD-INR appreciation (pos = negative for NSE)
                "brent_crude_change_pct": -1.2,   # Crude dip (neg = positive for NSE)
                "sp500_change_pct": 0.75,         # S&P500 rise (pos = positive for NSE)
                "nasdaq_change_pct": 1.10          # Nasdaq rise (pos = positive for NSE)
            }
        except Exception as e:
            logger.error(f"Error loading global macro sentiment: {e}. Defaulting to neutral bias metrics.")
            return {
                "usd_inr_change_pct": 0.0,
                "brent_crude_change_pct": 0.0,
                "sp500_change_pct": 0.0,
                "nasdaq_change_pct": 0.0
            }

    def determine_daily_bias(self) -> str:
        """
        Synthesizes macro metrics to output daily bias: 'BULLISH', 'BEARISH', or 'NEUTRAL'.
        
        Logic:
        - Weak USD-INR is bullish (< 0% change).
        - Falling Crude is bullish (< 0% change).
        - Rising US indices are bullish (> 0% change).
        """
        metrics = self.fetch_overnight_metrics()
        score = 0
        
        # 1. USD-INR Check
        if metrics["usd_inr_change_pct"] > 0.15:
            score -= 1  # INR weakening severely is negative
        elif metrics["usd_inr_change_pct"] < -0.15:
            score += 1
            
        # 2. Crude Oil Check (high oil prices hurt Indian trade balance)
        if metrics["brent_crude_change_pct"] > 1.0:
            score -= 1
        elif metrics["brent_crude_change_pct"] < -1.0:
            score += 1
            
        # 3. S&P500 / Nasdaq index movements
        us_avg = (metrics["sp500_change_pct"] + metrics["nasdaq_change_pct"]) / 2.0
        if us_avg > 0.5:
            score += 1
        elif us_avg < -0.5:
            score -= 1
            
        logger.info(f"Synthesized Daily Bias Score: {score} based on metrics: {metrics}")
        
        if score >= 1:
            return "BULLISH"
        elif score <= -1:
            return "BEARISH"
        else:
            return "NEUTRAL"
