import logging
import math
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

from config import settings

# Setup logging
logger = logging.getLogger("RiskManagement")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


@dataclass
class Position:
    """Represents an active trade tracked by the Risk Engine."""
    symbol: str
    token: str
    exchange: str
    is_long: bool
    entry_price: float
    initial_sl: float
    current_sl: float
    initial_qty: int
    current_qty: int
    phase: int = 1            # Phase 1, 2, or 3
    is_active: bool = True
    realized_pnl: float = 0.0


class PositionSizingEngine:
    """
    Calculates appropriate transaction volume sizes based on capital limits,
    per-trade risk allocations, and asset-specific lot sizes.
    """
    
    @staticmethod
    def calculate_quantity(
        available_capital: float,
        entry_price: float,
        stop_loss_price: float,
        lot_size: Optional[int] = None,
        risk_pct: float = settings.MAX_RISK_PER_TRADE_PCT
    ) -> int:
        """
        Enforces that potential loss does not exceed risk_pct of available capital.
        
        :param available_capital: Current available balance (equity/free cash)
        :param entry_price: Planned entry price
        :param stop_loss_price: Planned stop loss price
        :param lot_size: Optional lot size for options contracts
        :param risk_pct: Risk percentage per trade (e.g. 0.015 for 1.5%)
        :return: Calculated safe quantity to trade (shares or contracts)
        """
        risk_per_unit = abs(entry_price - stop_loss_price)
        if risk_per_unit <= 0:
            logger.warning("Stop loss is identical to entry price. Risk per unit is zero; returning 0 quantity.")
            return 0
            
        # Maximum capital loss allowed in currency
        max_capital_loss = available_capital * risk_pct
        
        # Base raw quantity based on risk parameter
        raw_qty = max_capital_loss / risk_per_unit
        
        # Cap raw quantity based on absolute capital limits (cannot buy more than we can afford)
        max_affordable_qty = available_capital / entry_price
        qty = min(raw_qty, max_affordable_qty)
        
        # Adjust for options contract lot sizes if applicable
        if lot_size and lot_size > 0:
            # Round down to nearest lot size
            qty = math.floor(qty / lot_size) * lot_size
            if qty < lot_size:
                logger.warning(f"Calculated quantity {qty} is less than lot size {lot_size}. Minimum allocation forced to 0 to prevent risk breach.")
                return 0
        else:
            # Equities / Cash sizing
            qty = math.floor(qty)
            
        logger.info(
            f"Sizing Calculation: Capital={available_capital:.2f}, Risk Pct={risk_pct:.2%}, "
            f"Risk/Unit={risk_per_unit:.2f}, Target Qty={qty} (Lot={lot_size})"
        )
        return qty


class RiskLifecycleOptimizer:
    """
    Implements Smdh's 3-Phase Lifecycle Optimizer.
    Tracks state transitions of active trades and issues execution actions when target structures are reached.
    """
    
    def __init__(self, settings_mod=settings) -> None:
        self.settings = settings_mod
        
    def evaluate_position(self, position: Position, current_price: float) -> Optional[Dict]:
        """
        Evaluates current asset pricing against the position lifecycle rules.
        Returns an execution instruction payload if a phase transition is triggered.
        
        :param position: The Position object to evaluate
        :param current_price: Current market tick price for the instrument
        :return: Optional execution order payload if action needed, else None
        """
        if not position.is_active:
            return None
            
        risk_per_unit = abs(position.entry_price - position.initial_sl)
        if risk_per_unit <= 0:
            return None

        # Determine directional movement
        if position.is_long:
            reward_per_unit = current_price - position.entry_price
            hit_stop = current_price <= position.current_sl
        else:
            reward_per_unit = position.entry_price - current_price
            hit_stop = current_price >= position.current_sl

        # Calculate current achieved Risk-to-Reward ratio
        current_rr = reward_per_unit / risk_per_unit

        # --- Phase 1 & 2 Transition Check ---
        if position.phase == 1:
            if current_rr >= self.settings.PHASE1_RR_THRESHOLD:
                # Trigger Phase 1: Secure early profits by selling 50%
                sell_qty = math.ceil(position.current_qty * self.settings.PHASE1_SELL_QUANTITY_PCT)
                if sell_qty <= 0:
                    sell_qty = position.current_qty
                
                logger.info(
                    f"[PHASE 1 TRIGGERED] {position.symbol} achieved {current_rr:.2f} RR (Threshold: {self.settings.PHASE1_RR_THRESHOLD}). "
                    f"Action: Selling 50% ({sell_qty} units). Transitioning to Phase 2."
                )
                
                # Execute simultaneous Phase 2 update: move Stop-Loss to entry
                old_sl = position.current_sl
                position.current_sl = position.entry_price
                position.current_qty -= sell_qty
                position.phase = 3  # Instantly transition to Phase 3 (trailing/riding the rest)
                
                # Calculate limit price with slippage buffer
                limit_price = self._calculate_execution_limit(
                    current_price=current_price, 
                    is_buy=not position.is_long  # Exiting a position is opposite transaction type
                )
                
                return {
                    "action": "PARTIAL_EXIT",
                    "symbol": position.symbol,
                    "token": position.token,
                    "exchange": position.exchange,
                    "quantity": sell_qty,
                    "limit_price": limit_price,
                    "updated_sl": position.current_sl,
                    "log": f"Sold 50% at profit, SL adjusted from {old_sl} to entry price {position.entry_price}."
                }
                
        # --- Phase 3 / Final Exit Check ---
        if position.phase == 3:
            # 1. Stop-Loss Trigger (Risk-free exit at entry price)
            if hit_stop:
                logger.info(
                    f"[STOP LOSS TRIGGERED] {position.symbol} hit trailing Stop-Loss at {position.current_sl}. "
                    f"Action: Closing remaining position of {position.current_qty} units."
                )
                position.is_active = False
                limit_price = self._calculate_execution_limit(current_price=current_price, is_buy=not position.is_long)
                
                return {
                    "action": "FULL_EXIT_SL",
                    "symbol": position.symbol,
                    "token": position.token,
                    "exchange": position.exchange,
                    "quantity": position.current_qty,
                    "limit_price": limit_price,
                    "log": f"Trailing SL hit at {position.current_sl}. Closed remaining position."
                }
                
            # 2. Macro structural target reached (e.g. 1:4+ RR target)
            if current_rr >= self.settings.PHASE3_MACRO_RR_TARGET:
                logger.info(
                    f"[MACRO TARGET REACHED] {position.symbol} hit macro target of {self.settings.PHASE3_MACRO_RR_TARGET} RR. "
                    f"Action: Closing remaining position of {position.current_qty} units."
                )
                position.is_active = False
                limit_price = self._calculate_execution_limit(current_price=current_price, is_buy=not position.is_long)
                
                return {
                    "action": "FULL_EXIT_TARGET",
                    "symbol": position.symbol,
                    "token": position.token,
                    "exchange": position.exchange,
                    "quantity": position.current_qty,
                    "limit_price": limit_price,
                    "log": f"Macro profit target hit at {current_price}. Closed remaining position."
                }
                
        # If stop hit in Phase 1 before achieving target
        if position.phase == 1 and hit_stop:
            logger.info(
                f"[INITIAL STOP TRIGGERED] {position.symbol} hit initial Stop-Loss at {position.current_sl}. "
                f"Action: Closing full position of {position.current_qty} units."
            )
            position.is_active = False
            limit_price = self._calculate_execution_limit(current_price=current_price, is_buy=not position.is_long)
            
            return {
                "action": "FULL_EXIT_SL",
                "symbol": position.symbol,
                "token": position.token,
                "exchange": position.exchange,
                "quantity": position.current_qty,
                "limit_price": limit_price,
                "log": f"Initial SL hit at {position.current_sl}. Closed position."
            }

        return None

    def _calculate_execution_limit(self, current_price: float, is_buy: bool) -> float:
        """
        Calculates a compliant Limit price with a slippage buffer in place of forbidden Market/IOC orders.
        For a BUY order, places limit slightly above the current price.
        For a SELL order, places limit slightly below the current price.
        """
        buffer = self.settings.SLIPPAGE_BUFFER_TICKS * self.settings.TICK_SIZE
        if is_buy:
            limit = current_price + buffer
        else:
            limit = current_price - buffer
            
        # Round to nearest valid tick size
        limit = round(limit / self.settings.TICK_SIZE) * self.settings.TICK_SIZE
        return max(limit, self.settings.TICK_SIZE)


class DailyCircuitBreakerManager:
    """
    Monitors overall account equity parameters and triggers an immediate system disconnect
    if cumulative losses reach defined thresholds (25% of total capital).
    """
    
    def __init__(self, initial_capital: float = settings.INITIAL_CAPITAL) -> None:
        self.initial_capital = initial_capital
        self.max_loss_allowed = initial_capital * settings.MAX_DAILY_LOSS_LIMIT_PCT
        logger.info(
            f"Daily Circuit Breaker initialized: Initial Capital=INR {initial_capital:,.2f}, "
            f"Max Loss Allowed=INR {self.max_loss_allowed:,.2f} ({settings.MAX_DAILY_LOSS_LIMIT_PCT:.1%})"
        )

    def calculate_total_pnl(self, realized_pnl: float, active_positions: List[Position], current_prices: Dict[str, float]) -> float:
        """
        Calculates total (realized + floating) PnL.
        """
        total_pnl = realized_pnl
        
        for pos in active_positions:
            if not pos.is_active:
                total_pnl += pos.realized_pnl
                continue
                
            cur_price = current_prices.get(pos.token)
            if cur_price is None:
                logger.warning(f"Live price missing for token {pos.token} / {pos.symbol}. Using entry price as fallback.")
                cur_price = pos.entry_price
                
            # Floating PnL calculation
            if pos.is_long:
                floating_pnl = (cur_price - pos.entry_price) * pos.current_qty
            else:
                floating_pnl = (pos.entry_price - cur_price) * pos.current_qty
                
            total_pnl += floating_pnl + pos.realized_pnl
            
        return total_pnl

    def monitor_pnl(self, realized_pnl: float, active_positions: List[Position], current_prices: Dict[str, float], executor_client=None) -> None:
        """
        Actively checks current PnL. If circuit breaker condition met, executing disconnect.
        """
        total_pnl = self.calculate_total_pnl(realized_pnl, active_positions, current_prices)
        
        # If total PnL is negative and magnitude exceeds the circuit breaker limit
        if total_pnl < 0 and abs(total_pnl) >= self.max_loss_allowed:
            logger.critical(
                f"!!! CIRCUIT BREAKER CRITICAL BREACH !!! "
                f"Total Daily Loss: INR {abs(total_pnl):,.2f} exceeds Max Limit: INR {self.max_loss_allowed:,.2f}. "
                f"Initiating emergency system shutdown."
            )
            self._trigger_emergency_shutdown(active_positions, current_prices, executor_client)
            
    def _trigger_emergency_shutdown(self, active_positions: List[Position], current_prices: Dict[str, float], executor_client) -> None:
        """
        Emergency closeout routines.
        Sends immediate limit exit orders with slippage buffers to liquidate all holdings.
        """
        logger.critical("EMERGENCY SHUTDOWN: Closing all open positions...")
        
        # Exit order helper
        optimizer = RiskLifecycleOptimizer()
        
        for pos in active_positions:
            if pos.is_active:
                cur_price = current_prices.get(pos.token)
                if not cur_price:
                    logger.warning(f"No current price available for {pos.symbol} during emergency close. Using entry.")
                    cur_price = pos.entry_price
                    
                limit_price = optimizer._calculate_execution_limit(current_price=cur_price, is_buy=not pos.is_long)
                
                exit_payload = {
                    "action": "EMERGENCY_EXIT",
                    "symbol": pos.symbol,
                    "token": pos.token,
                    "exchange": pos.exchange,
                    "quantity": pos.current_qty,
                    "limit_price": limit_price,
                }
                
                logger.critical(f"Dispatching exit order: {exit_payload}")
                if executor_client:
                    try:
                        executor_client.execute_order(exit_payload)
                        pos.is_active = False
                    except Exception as e:
                        logger.critical(f"Failed to execute emergency exit for {pos.symbol}: {e}")
                else:
                    logger.warning("No executor client interface supplied to shutdown routine. Logged exit intent only.")
                    pos.is_active = False
                    
        logger.critical("All active system processes terminated. Prevented revenge trading loops. Exiting environment.")
        sys.exit("SYSTEM SHUTDOWN: Daily Circuit Breaker loss limit breached.")
