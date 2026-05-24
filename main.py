import asyncio
import datetime
import logging
import random
import sys
from typing import Dict, List

from config import settings
from core.executor import AngelOneExecutor
from core.ingestion import AngelOneWebSocketClient
from core.options_processor import OptionsProcessor, OptionsTokenRegistry
from core.risk_management import (
    DailyCircuitBreakerManager,
    Position,
    PositionSizingEngine,
    RiskLifecycleOptimizer,
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
    options selection, risk controls, and compliant execution.
    """
    
    def __init__(self) -> None:
        logger.info("Initializing Quantum Trading System Architect...")
        
        # 1. Initialize Broker Interface
        self.executor = AngelOneExecutor()
        
        # 2. Initialize Options Utilities
        self.registry = OptionsTokenRegistry()
        self.options_proc = OptionsProcessor(self.registry)
        
        # 3. Initialize Risk Control Systems
        self.risk_optimizer = RiskLifecycleOptimizer()
        self.circuit_breaker = DailyCircuitBreakerManager()
        
        # 4. State Variables
        self.active_positions: List[Position] = []
        self.realized_pnl: float = 0.0
        self.current_prices: Dict[str, float] = {}
        self.daily_bias = "NEUTRAL"
        self.is_running = True

    async def initialize(self) -> None:
        """Runs credentials login, token sync, and macro bias evaluation."""
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

    def on_market_tick(self, tick: Dict) -> None:
        """Callback handler processing incoming real-time socket ticker streams."""
        token = tick.get("token")
        ltp = tick.get("last_traded_price")
        if not token or not ltp:
            return
            
        self.current_prices[token] = ltp
        # Print status of updates periodically
        if random.random() < 0.05:  # Log occasionally to prevent stdout congestion
            logger.info(f"Market Stream Update: Token {token} = Price INR {ltp:.2f}")
            
        # 1. Run Risk Management: Daily Circuit Breaker Check
        self.circuit_breaker.monitor_pnl(
            realized_pnl=self.realized_pnl,
            active_positions=self.active_positions,
            current_prices=self.current_prices,
            executor_client=self.executor
        )
        
        # 2. Run Risk Management: Position Lifecycle Optimizer (3-Phases)
        for pos in self.active_positions:
            if pos.is_active and pos.token == token:
                action = self.risk_optimizer.evaluate_position(pos, ltp)
                if action:
                    self._handle_risk_action(pos, action)

    def _handle_risk_action(self, pos: Position, action: Dict) -> None:
        """Dispatches orders issued by the Risk Engine."""
        logger.info(f"Executing Risk Action for {pos.symbol}: {action['action']}")
        
        order_payload = {
            "symbol": action["symbol"],
            "token": action["token"],
            "exchange": action["exchange"],
            "quantity": action["quantity"],
            "limit_price": action["limit_price"],
            "order_type": "LIMIT",
            "transaction_type": "SELL" if pos.is_long else "BUY"
        }
        
        try:
            order_id = self.executor.execute_order(order_payload)
            if order_id:
                logger.info(f"Risk action order successfully placed. Exchange ID: {order_id}. Context: {action.get('log')}")
                if action["action"] == "PARTIAL_EXIT":
                    # Realize 50% profit
                    profit = (action["limit_price"] - pos.entry_price) * action["quantity"] if pos.is_long else (pos.entry_price - action["limit_price"]) * action["quantity"]
                    pos.realized_pnl += profit
                elif action["action"] in ("FULL_EXIT_SL", "FULL_EXIT_TARGET"):
                    profit = (action["limit_price"] - pos.entry_price) * action["quantity"] if pos.is_long else (pos.entry_price - action["limit_price"]) * action["quantity"]
                    pos.realized_pnl += profit
                    self.realized_pnl += pos.realized_pnl
                    logger.info(f"Position {pos.symbol} fully closed. Net Position PnL: INR {pos.realized_pnl:,.2f}")
            else:
                logger.error(f"Failed to execute risk action order for {pos.symbol}")
        except Exception as e:
            logger.critical(f"Critical execution error during risk exit: {e}")

    def simulate_entry_signal(self, underlying: str = "NIFTY") -> None:
        """Simulates how the SMC strategy parser triggers entries and calculates hedges."""
        logger.info(f"Analyzing SMC structures for {underlying}...")
        
        # Simulate receiving OHLCV candle data
        import numpy as np
        data = {
            "open": np.random.uniform(22300, 22400, 30),
            "high": np.random.uniform(22400, 22500, 30),
            "low": np.random.uniform(22200, 22300, 30),
            "close": np.random.uniform(22300, 22400, 30),
            "volume": np.random.randint(1000, 5000, 30)
        }
        df = pd.DataFrame(data)
        
        # Add a mock liquidity sweep candle at the end
        df.loc[df.index[-1], 'low'] = 22150.0  # Sweeps low wicks
        df.loc[df.index[-1], 'close'] = 22350.0  # Snaps back up
        
        # Check sweep signals
        df_analyzed = SMCSignalDetector.identify_liquidity_sweeps(df, lookback=10)
        last_row = df_analyzed.iloc[-1]
        
        spot_price = float(last_row['close'])
        
        if last_row['bullish_sweep'] and self.daily_bias in ("BULLISH", "NEUTRAL"):
            logger.info("SMC Signal detected: Bullish Liquidity Sweep found!")
            
            # Setup Options Target contract
            expiry = "26MAY2026"  # Mock current expiry contract date
            
            try:
                # 1. Strike Selection
                option_details = self.options_proc.select_strike(
                    underlying=underlying,
                    spot_price=spot_price,
                    option_type="CE",
                    expiry_date=expiry,
                    bias="ATM"
                )
                
                strike = float(option_details.get("strike", 22350))
                
                # 2. Theta Decay check (e.g. block if consolidating near expiry)
                is_consolidating = True  # Mock consolidation status
                if self.options_proc.should_block_long_purchase(
                    spot_price=spot_price,
                    strike_price=strike,
                    expiry_date_str="2026-05-26",
                    is_market_consolidating=is_consolidating
                ):
                    logger.warning("Trade skipped: Premium decay risk is high (Theta Filter).")
                    return
                
                # 3. Position Sizing
                # Risk calculation setup based on option premiums
                option_entry_premium = 150.0
                option_stop_loss_premium = 100.0
                
                # Underlying index price targets for Risk Engine tracking
                underlying_entry = spot_price
                underlying_sl = spot_price - 50.0
                
                # Fetch recent institutional flows to adjust risk sizes dynamically
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
                    
                # 4. Hedging Core: Calculate Protective opposite PE Option contract
                logger.info("Calculating protective insurance hedge...")
                hedge_payload = self.options_proc.calculate_protective_hedge(
                    underlying=underlying,
                    spot_price=spot_price,
                    primary_order_type="CE",
                    primary_qty=qty,
                    expiry_date=expiry
                )
                
                # 5. Compliant Order Dispatching
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
                    # Register new position in Risk Engine
                    new_pos = Position(
                        symbol=option_details["symbol"],
                        token=option_details["token"],
                        exchange="NFO",
                        is_long=True,
                        entry_price=underlying_entry,
                        initial_sl=underlying_sl,
                        current_sl=underlying_sl,
                        initial_qty=qty,
                        current_qty=qty
                    )
                    self.active_positions.append(new_pos)
                    logger.info(f"Active positions updated: {self.active_positions}")
                    
            except Exception as e:
                logger.error(f"Error structuring entry order setups: {e}")

    async def run(self) -> None:
        """Main system loop running background services."""
        await self.initialize()
        
        # Initialize and connect live socket client
        # For testing, uses mock sockets feeding updates
        self.ws_client = AngelOneWebSocketClient("token123", "feed456", settings.CLIENT_ID)
        self.ws_client.set_callback(self.on_market_tick)
        self.ws_client.connect()
        
        # Subscribe to Nifty index token
        self.ws_client.subscribe(["26000"], segment="NSE")
        
        logger.info("Bot fully operational. Listening to streams and checking structures.")
        
        # Simulate EOD institutional logs insertion into DB (historical corrections check)
        self.executor.log_fii_dii_flow("2026-05-22", 1250.45, 840.12)
        self.executor.log_fii_dii_flow("2026-05-23", -210.50, 410.20)
        
        # Simulate a trade trigger check after 3 seconds
        await asyncio.sleep(3.0)
        self.simulate_entry_signal("NIFTY")
        
        # Let client run for demonstration
        loop_counter = 0
        while self.is_running and loop_counter < 10:
            await asyncio.sleep(1)
            loop_counter += 1
            
        logger.info("Orchestrator simulation run completed successfully. Closing connections.")


if __name__ == "__main__":
    # Ensure pandas is loaded for mock strategies
    try:
        import pandas as pd
    except ImportError:
        logger.error("pandas library not found. Please run 'pip install pandas numpy'")
        sys.exit(1)
        
    asyncio.run(TradingBot().run())
