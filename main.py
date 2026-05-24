import asyncio
import datetime
import logging
import random
import sys
from typing import Dict, List

import pandas as pd
import numpy as np

from config import settings
from core.executor import AngelOneExecutor
from core.ingestion import AngelOneWebSocketClient
from core.options_processor import OptionsProcessor, OptionsTokenRegistry
from core.results_analyzer import CompanyResultsAnalyzer
from core.risk_management import (
    DailyCircuitBreakerManager,
    Position,
    PositionSizingEngine,
    RiskLifecycleOptimizer,
    StateManager,
    calculate_friction_adjusted_target,
    validate_trade_constraints,
)
from core.strategy import GlobalSentimentLayer, SMCSignalDetector

# Setup main orchestrator logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("TradingBotOrchestrator")


class TradingBot:
    """
    Main orchestrator coordinating market data feeds, strategy parsing,
    options selection, risk controls, earnings analysis, and compliant execution.
    """
    
    def __init__(self) -> None:
        logger.info("Initializing Quantum Trading System Architect...")
        logger.info("Target Return on Investment (ROI) calibrated to safe 15-25% bounds.")
        
        # 1. Initialize State and Risk Controllers
        self.state_manager = StateManager(settings.STATE_FILE_PATH)
        self.circuit_breaker = DailyCircuitBreakerManager(
            initial_capital=settings.INITIAL_CAPITAL,
            state_manager=self.state_manager
        )
        self.risk_optimizer = RiskLifecycleOptimizer()
        
        # 2. Initialize Broker Interface & Token Master Registry
        self.executor = AngelOneExecutor()
        self.registry = OptionsTokenRegistry()
        self.options_proc = OptionsProcessor(self.registry)
        
        # 3. Initialize Analytical Engine
        self.detector = SMCSignalDetector(volume_multiplier=1.8)
        
        # 4. State Variables
        self.active_positions: List[Position] = []
        self.realized_pnl: float = 0.0
        self.current_prices: Dict[str, float] = {}
        self.daily_bias = "NEUTRAL"
        self.trade_day_status = "TRADE_DAY"
        self.trade_day_reason = "Initial check"
        self.is_running = True

    async def initialize(self) -> None:
        """Runs credentials login, token sync, and macro bias / uncertainty evaluation."""
        logger.info("Step 1: Authenticating Angel One SmartAPI Session...")
        if not self.executor.authenticate_session():
            logger.critical("Authentication failed. Terminating system startup.")
            sys.exit("AUTH_FAILURE")
            
        logger.info("Step 2: Syncing Option Chain Token Master List...")
        try:
            self.registry.sync_instruments()
        except Exception as e:
            logger.critical(f"Failed to initialize contract master list: {e}")
            sys.exit("REGISTRY_FAILURE")
            
        logger.info("Step 3: Calculating Daily Market Bias from global macros...")
        sentiment_layer = GlobalSentimentLayer()
        self.daily_bias = sentiment_layer.determine_daily_bias()
        logger.info(f"Daily Market Bias established: {self.daily_bias}")

        # Check macro uncertainty (Trade Day vs No Trade Day)
        metrics = sentiment_layer.fetch_overnight_metrics()
        self.trade_day_status, self.trade_day_reason = sentiment_layer.evaluate_market_uncertainty(metrics)
        logger.info(f"Step 4: Trade Day Status established: {self.trade_day_status} ({self.trade_day_reason})")

    def on_market_tick(self, tick: Dict) -> None:
        """Callback handler processing incoming real-time socket ticker streams."""
        # 1. First safety gate: Check lockout status
        state = self.state_manager.get_state()
        if state.get("is_locked_out"):
            if self.is_running:
                logger.warning("Over-Trading Lockout active. Disconnecting and shutting down loop.")
                self.is_running = False
                if hasattr(self, "ws_client") and self.ws_client.is_connected:
                    self.ws_client.disconnect()
            return

        token = tick.get("token")
        ltp = tick.get("last_traded_price")
        if not token or not ltp:
            return
            
        self.current_prices[token] = ltp
        
        # Log occasionally to prevent stdout congestion
        if random.random() < 0.05:
            logger.info(f"Market Stream Update: Token {token} = Price INR {ltp:.2f}")
            
        # 2. Run Risk Management: Daily Circuit Breaker Check
        self.circuit_breaker.monitor_pnl(
            realized_pnl=self.realized_pnl,
            active_positions=self.active_positions,
            current_prices=self.current_prices,
            executor_client=self.executor,
            ws_client=getattr(self, "ws_client", None)
        )
        
        # 3. Run Risk Management: Position Lifecycle Optimizer (Strict 1:2 RR on index price)
        index_price = self.current_prices.get("26000")
        if index_price:
            for pos in self.active_positions:
                if pos.is_active:
                    action = self.risk_optimizer.evaluate_position(pos, index_price)
                    if action:
                        self._handle_risk_action(pos, action)

    def _handle_risk_action(self, pos: Position, action: Dict) -> None:
        """Dispatches orders issued by the Risk Engine."""
        logger.info(f"Executing Risk Action for {pos.symbol}: {action['action']}")
        
        opt_price = self.current_prices.get(pos.token)
        if not opt_price:
            logger.warning(f"Live option premium for {pos.symbol} missing. Falling back to entry premium.")
            opt_price = pos.option_entry_price
            
        limit_price = self.risk_optimizer._calculate_execution_limit(
            current_price=opt_price,
            is_buy=not pos.is_long
        )
        
        order_payload = {
            "symbol": pos.symbol,
            "token": pos.token,
            "exchange": pos.exchange,
            "quantity": pos.current_qty,
            "limit_price": limit_price,
            "order_type": "LIMIT",
            "transaction_type": "SELL" if pos.is_long else "BUY"
        }
        
        try:
            order_id = self.executor.execute_order(order_payload)
            if order_id:
                logger.info(f"Risk action order successfully placed. Exchange ID: {order_id}. Context: {action.get('log')}")
                
                if pos.is_long:
                    profit = (limit_price - pos.option_entry_price) * pos.current_qty
                else:
                    profit = (pos.option_entry_price - limit_price) * pos.current_qty
                
                pos.realized_pnl += profit
                self.realized_pnl += pos.realized_pnl
                pos.is_active = False
                
                logger.info(f"Position {pos.symbol} fully closed. Realized PnL: INR {pos.realized_pnl:,.2f}")
                
                self.circuit_breaker.monitor_pnl(
                    realized_pnl=self.realized_pnl,
                    active_positions=self.active_positions,
                    current_prices=self.current_prices,
                    executor_client=self.executor,
                    ws_client=getattr(self, "ws_client", None)
                )
            else:
                logger.error(f"Failed to execute risk action order for {pos.symbol}")
        except Exception as e:
            logger.critical(f"Critical execution error during risk exit: {e}")

    def simulate_entry_signal(self, underlying: str = "NIFTY") -> None:
        """Simulates strategy triggers, checking earnings screening and trade-day filters."""
        # 1. State lockout verification
        state = self.state_manager.get_state()
        if state.get("is_locked_out"):
            logger.warning("Cannot simulate entry signal: Over-Trading lockout is active.")
            return

        # 2. Trade Day vs No Trade Day verification
        if self.trade_day_status == "NO_TRADE_DAY":
            logger.warning(
                f"Trade entry BLOCKED: Today is declared a NO_TRADE_DAY "
                f"due to market uncertainty: {self.trade_day_reason}"
            )
            return

        logger.info(f"Analyzing SMC structures for {underlying}...")
        
        # 3. Analyze corporate results (fundamental earnings screening)
        # Mock financial statements (healthy metrics)
        mock_financials = {
            "balance_sheet": {
                "debt": 100.0,
                "equity": 250.0,  # D/E ratio = 0.4 (Very strong)
                "current_assets": 180.0,
                "current_liabilities": 110.0  # Current ratio = 1.63 (Strong)
            },
            "profit_loss": {
                "revenue": 1200.0,
                "net_profit": 150.0,  # NPM = 12.5% (Healthy)
                "revenue_growth_pct": 14.5,
                "profit_growth_pct": 18.0  # Double digit earnings growth
            },
            "cash_flow": {
                "operating_cash_flow": 160.0,  # OCF > Net Profit (High quality)
                "investing_cash_flow": -70.0,
                "financing_cash_flow": -30.0
            }
        }
        
        logger.info(f"Screening company results / balance sheet for {underlying}...")
        analysis_result = CompanyResultsAnalyzer.analyze_financials(mock_financials)
        
        if not analysis_result["is_eligible"]:
            logger.warning(
                f"Trade entry BLOCKED: Ticker {underlying} failed corporate earnings screen. "
                f"Reason: {analysis_result['reason']}"
            )
            return

        # 4. Build mock HTF (15m) and LTF (1m) candles
        now = datetime.datetime.now()
        
        htf_data = {
            "open": np.random.uniform(22300, 22400, 35),
            "high": np.random.uniform(22400, 22500, 35),
            "low": np.random.uniform(22200, 22300, 35),
            "close": np.random.uniform(22300, 22400, 35),
            "volume": np.random.randint(1000, 5000, 35),
            "timestamp": [now - datetime.timedelta(minutes=15 * (35 - i)) for i in range(35)]
        }
        htf_df = pd.DataFrame(htf_data)
        
        # Inject completed Order Block setup into completed HTF candles (to avoid look-ahead bias)
        htf_df.loc[25, 'open'] = 22200.0
        htf_df.loc[25, 'close'] = 22380.0
        htf_df.loc[25, 'high'] = 22390.0
        htf_df.loc[25, 'low'] = 22190.0
        htf_df.loc[25, 'volume'] = 12000.0
        
        htf_df.loc[24, 'open'] = 22250.0
        htf_df.loc[24, 'close'] = 22190.0
        htf_df.loc[24, 'high'] = 22260.0
        htf_df.loc[24, 'low'] = 22180.0
        htf_df.loc[24, 'volume'] = 3000.0
        
        ltf_data = {
            "open": np.random.uniform(22300, 22350, 35),
            "high": np.random.uniform(22350, 22400, 35),
            "low": np.random.uniform(22250, 22300, 35),
            "close": np.random.uniform(22300, 22350, 35),
            "volume": np.random.randint(200, 1000, 35),
            "timestamp": [now - datetime.timedelta(minutes=1 * (35 - i)) for i in range(35)]
        }
        ltf_df = pd.DataFrame(ltf_data)
        
        # Inject completed liquidity sweep candle in LTF at index -2 (the last COMPLETED candle)
        ltf_df.loc[33, 'low'] = 22170.0   # Sweeps low barriers
        ltf_df.loc[33, 'close'] = 22310.0 # Snaps back up above support

        # 5. Update HTF Order Blocks (cached, only recalculated when HTF candle closes)
        self.detector.update_htf_order_blocks(htf_df)
        
        # 6. Detect LTF sweeps
        ltf_analyzed = SMCSignalDetector.identify_liquidity_sweeps(ltf_df, lookback=10)
        
        # Avoid Look-Ahead Bias: retrieve indicators from fully completed candle (iloc[-2])
        last_completed_ltf = ltf_analyzed.iloc[-2]
        
        spot_price = float(last_completed_ltf['close'])
        ltf_low = float(last_completed_ltf['low'])
        
        # Check if the completed LTF sweep low hit/interacted with a cached HTF Order Block
        inside_htf_ob = False
        for ob in self.detector.cached_htf_order_blocks:
            if ob["type"] == "BULLISH_OB":
                if (ob["bottom_zone"] - 20) <= ltf_low <= (ob["top_zone"] + 20):
                    inside_htf_ob = True
                    break
        
        # 7. Evaluate SMC Trigger conditions
        is_bullish_sweep = last_completed_ltf['bullish_sweep']
        
        if is_bullish_sweep and inside_htf_ob and self.daily_bias in ("BULLISH", "NEUTRAL"):
            logger.info("SMC Signal detected: LTF Bullish Sweep inside HTF Order Block! Generating trade setup...")
            
            # Enforce Intraday Constraints / Short-Selling Blocks
            if not validate_trade_constraints(transaction_type="BUY", holding_timeframe=settings.HOLDING_TIMEFRAME):
                logger.warning("Trade skipped due to constraint violations.")
                return
                
            expiry = "26MAY2026"
            
            try:
                # 8. Strike Selection
                option_details = self.options_proc.select_strike(
                    underlying=underlying,
                    spot_price=spot_price,
                    option_type="CE",
                    expiry_date=expiry,
                    bias="ATM"
                )
                
                strike = float(option_details.get("strike", 22300))
                
                # 9. Theta Decay check
                if self.options_proc.should_block_long_purchase(
                    spot_price=spot_price,
                    strike_price=strike,
                    expiry_date_str="2026-05-26",
                    is_market_consolidating=True
                ):
                    logger.warning("Trade skipped: Premium decay risk is high (Theta Filter).")
                    return
                
                # 10. Sizing & Risk parameters
                option_entry_premium = 150.0
                option_stop_loss_premium = 100.0
                
                underlying_entry = spot_price
                underlying_sl = spot_price - 50.0
                
                inst_multiplier = self.executor.get_historical_institutional_bias()
                adjusted_risk = settings.MAX_RISK_PER_TRADE_PCT * inst_multiplier
                
                qty = PositionSizingEngine.calculate_quantity(
                    available_capital=settings.INITIAL_CAPITAL,
                    entry_price=option_entry_premium,
                    stop_loss_price=option_stop_loss_premium,
                    lot_size=int(option_details.get("lotsize", 50)),
                    risk_pct=adjusted_risk
                )
                
                if qty <= 0:
                    logger.warning("Calculated order quantity is 0; skipping execution.")
                    return
                    
                # 11. Hedging Core
                logger.info("Calculating protective insurance hedge...")
                hedge_payload = self.options_proc.calculate_protective_hedge(
                    underlying=underlying,
                    spot_price=spot_price,
                    primary_order_type="CE",
                    primary_qty=qty,
                    expiry_date=expiry
                )
                
                # 12. Enforce strict 1:2 RR target adjusted for fees and statutory friction
                underlying_target = calculate_friction_adjusted_target(
                    entry=underlying_entry,
                    sl=underlying_sl,
                    qty=qty,
                    is_long=True,
                    is_options=False
                )
                
                # Round limit prices compliant with 0.05 tick size
                tick = 0.05
                option_entry_premium = round(option_entry_premium / tick) * tick
                hedge_payload["limit_price"] = round(hedge_payload["limit_price"] / tick) * tick
                
                primary_payload = {
                    "symbol": option_details["symbol"],
                    "token": option_details["token"],
                    "exchange": "NFO",
                    "quantity": qty,
                    "limit_price": option_entry_premium,
                    "order_type": "LIMIT",
                    "transaction_type": "BUY"
                }
                
                logger.info("Executing Primary CE position and protective PE position simultaneously...")
                prim_order_id = self.executor.execute_order(primary_payload)
                hedge_order_id = self.executor.execute_order(hedge_payload)
                
                if prim_order_id:
                    self.state_manager.update_state(trade_add=1)
                    
                    new_pos = Position(
                        symbol=option_details["symbol"],
                        token=option_details["token"],
                        exchange="NFO",
                        is_long=True,
                        option_entry_price=option_entry_premium,
                        underlying_entry=underlying_entry,
                        current_sl=underlying_sl,
                        target_price=underlying_target,
                        initial_qty=qty,
                        current_qty=qty
                    )
                    self.active_positions.append(new_pos)
                    logger.info(
                        f"Active positions updated: {self.active_positions}. "
                        f"Benchmark entry: {underlying_entry:.2f}, SL: {underlying_sl:.2f}, "
                        f"Friction-adjusted Target (1:2 RR): {underlying_target:.2f}"
                    )
                    
                    if hasattr(self, "ws_client"):
                        self.ws_client.subscribe([option_details["token"]], segment="NFO")
                    
            except Exception as e:
                logger.error(f"Error structuring entry order setups: {e}")
        else:
            logger.info("No valid SMC entry structures detected.")

    async def run(self) -> None:
        """Main system loop running background services."""
        await self.initialize()
        
        # Initialize and connect live socket client
        self.ws_client = AngelOneWebSocketClient("token123", "feed456", settings.CLIENT_ID)
        self.ws_client.set_callback(self.on_market_tick)
        self.ws_client.connect()
        
        # Subscribe to Nifty index token
        self.ws_client.subscribe(["26000"], segment="NSE")
        
        logger.info("Bot fully operational. Listening to streams and checking structures.")
        
        self.executor.log_fii_dii_flow("2026-05-22", 1250.45, 840.12)
        self.executor.log_fii_dii_flow("2026-05-23", -210.50, 410.20)
        
        if self.state_manager.get_state().get("is_locked_out"):
            logger.critical("Bot is locked out due to prior limit breaches. Terminating execution.")
            self.ws_client.disconnect()
            return

        # Simulate a trade trigger check after 3 seconds
        await asyncio.sleep(3.0)
        self.simulate_entry_signal("NIFTY")
        
        # Let client run to process incoming ticks
        loop_counter = 0
        while self.is_running and loop_counter < 10:
            await asyncio.sleep(1)
            loop_counter += 1
            
        logger.info("Orchestrator simulation run completed successfully. Closing connections.")
        self.ws_client.disconnect()


if __name__ == "__main__":
    asyncio.run(TradingBot().run())
