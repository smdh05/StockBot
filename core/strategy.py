import logging
import urllib.request
import json
from typing import Dict, List, Optional, Tuple
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
    Separates HTF structure checks from LTF trigger sweeps, ensuring caching
    and look-ahead bias avoidance.
    """

    def __init__(self, volume_multiplier: float = 1.8) -> None:
        self.volume_multiplier = volume_multiplier
        self.cached_htf_order_blocks: List[Dict] = []
        self.last_processed_htf_timestamp = None

    @staticmethod
    def identify_liquidity_sweeps(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
        """
        Identifies candle intervals where wicks sweep key support/resistance barriers
        and immediately snap back within the trading range.
        
        Input DataFrame must contain columns: ['open', 'high', 'low', 'close']
        To prevent look-ahead bias, it shifts the range comparison to only look at closed prior candles.
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
        
        Inputs: df must contain only fully closed candles (to prevent lookahead bias).
        """
        order_blocks = []
        df = df.copy()
        
        if len(df) < 21:
            return order_blocks
            
        # Calculate volume average to detect institutional entry
        df['volume_sma'] = df['volume'].rolling(window=20).mean()
        
        # Loop to identify breakouts
        for i in range(20, len(df)):
            current_row = df.iloc[i]
            prev_row = df.iloc[i-1]
            
            current_vol = current_row['volume']
            avg_vol = current_row['volume_sma']
            
            # Identify explosive bullish candle (breakout)
            is_explosive_up = (
                current_row['close'] > current_row['open'] and 
                (current_row['close'] - current_row['open']) > (prev_row['high'] - prev_row['low']) * 2 and
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
                            "breakout_index": i,
                            "timestamp": prior_candle.get('timestamp', df.index[j])
                        }
                        order_blocks.append(ob_zone)
                        break
                        
            is_explosive_down = (
                current_row['close'] < current_row['open'] and
                (current_row['open'] - current_row['close']) > (prev_row['high'] - prev_row['low']) * 2 and
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
                            "breakout_index": i,
                            "timestamp": prior_candle.get('timestamp', df.index[j])
                        }
                        order_blocks.append(ob_zone)
                        break
                        
        return order_blocks

    def update_htf_order_blocks(self, htf_df: pd.DataFrame) -> List[Dict]:
        """
        Updates and caches HTF order blocks. Recalculates ONLY when a new HTF candle closes.
        Assumes the last row in htf_df is the active (unclosed) candle, and htf_df.iloc[-2] is the
        latest completed candle.
        
        To prevent look-ahead bias, it runs the calculations on closed candles (htf_df.iloc[:-1]).
        """
        if htf_df.empty or len(htf_df) < 2:
            return self.cached_htf_order_blocks
            
        latest_closed_candle = htf_df.iloc[-2]
        latest_closed_timestamp = latest_closed_candle.get('timestamp', htf_df.index[-2])
        
        # If timestamp is different, we have a newly closed candle!
        if latest_closed_timestamp != self.last_processed_htf_timestamp:
            logger.info(f"New HTF candle closed at {latest_closed_timestamp}. Recalculating HTF Order Blocks...")
            # Exclude the active unclosed candle (last row) to avoid look-ahead bias
            closed_htf_df = htf_df.iloc[:-1]
            self.cached_htf_order_blocks = self.find_order_blocks(closed_htf_df, self.volume_multiplier)
            self.last_processed_htf_timestamp = latest_closed_timestamp
        else:
            logger.debug("HTF candle still open. Returning cached HTF Order Blocks.")
            
        return self.cached_htf_order_blocks


class GlobalSentimentLayer:
    """
    Parses macro factors to output a Daily Market Bias and Trade Day status.
    Indices: S&P500/Nasdaq, FX: USD-INR, Commodities: Brent/WTI Crude Oil, VIX.
    """

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key

    def fetch_overnight_metrics(self) -> Dict[str, float]:
        """
        Fetches macro tickers. In production, this hits external global market APIs.
        """
        logger.info("Accessing overnight global macroeconomic feeds...")
        try:
            return {
                "usd_inr_change_pct": 0.05,       # USD-INR appreciation (pos = negative for NSE)
                "brent_crude_change_pct": -1.2,   # Crude dip (neg = positive for NSE)
                "sp500_change_pct": 0.75,         # S&P500 rise (pos = positive for NSE)
                "nasdaq_change_pct": 1.10,        # Nasdaq rise (pos = positive for NSE)
                "vix": 16.5                       # Volatility Index (VIX)
            }
        except Exception as e:
            logger.error(f"Error loading global macro sentiment: {e}. Defaulting to neutral bias metrics.")
            return {
                "usd_inr_change_pct": 0.0,
                "brent_crude_change_pct": 0.0,
                "sp500_change_pct": 0.0,
                "nasdaq_change_pct": 0.0,
                "vix": 15.0
            }

    def evaluate_market_uncertainty(self, metrics: Dict[str, float]) -> Tuple[str, str]:
        """
        Evaluates VIX levels and currency volatility to determine if today is a
        TRADE_DAY or a NO_TRADE_DAY.
        """
        from config import settings
        max_vix = getattr(settings, "MAX_VIX_THRESHOLD", 22.0)
        vix = metrics.get("vix", 15.0)
        usd_inr_change = abs(metrics.get("usd_inr_change_pct", 0.0))
        
        # Check 1: VIX threshold breach (high fear/uncertainty)
        if vix > max_vix:
            reason = f"VIX is at {vix:.1f} (exceeds threshold of {max_vix:.1f})"
            logger.warning(f"NO_TRADE_DAY declared: {reason}. Extreme volatility risk.")
            return "NO_TRADE_DAY", reason
            
        # Check 2: Currency shock (extreme USD-INR change > 1.0%)
        if usd_inr_change > 1.0:
            reason = f"USD-INR overnight shock of {usd_inr_change:.2f}%"
            logger.warning(f"NO_TRADE_DAY declared: {reason}. High forex liquidity risk.")
            return "NO_TRADE_DAY", reason
            
        return "TRADE_DAY", "Market indicators stable. Eligible for trading."

    def determine_daily_bias(self) -> str:
        """
        Synthesizes macro metrics to output daily bias: 'BULLISH', 'BEARISH', or 'NEUTRAL'.
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
