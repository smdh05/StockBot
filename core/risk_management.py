import logging
import math
import os
import json
import sys
import datetime
import threading
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
    option_entry_price: float     # Premium price of the option contract at entry
    underlying_entry: float       # Price of the underlying index at entry
    current_sl: float             # Stop-loss price on the underlying index
    target_price: float           # Friction-adjusted target price on the underlying index
    initial_qty: int
    current_qty: int
    phase: int = 1
    is_active: bool = True
    realized_pnl: float = 0.0


def calculate_friction(price: float, qty: int, transaction_type: str, is_options: bool = True) -> float:
    """
    Calculates statutory transaction fees and statutory friction for Indian markets (NSE/BSE).
    Includes: Brokerage, STT, Exchange transaction charges, GST, SEBI charges, and Stamp Duty.
    """
    if price <= 0 or qty <= 0:
        return 0.0

    if is_options:
        brokerage = 20.0  # Flat ₹20 per trade (Angel One / Zerodha)
        
        if transaction_type.upper() == "BUY":
            stt = 0.0
            stamp_duty = 0.00003 * price * qty  # 0.003% on buy side premium
        else:
            stt = 0.00125 * price * qty  # 0.125% on sell side premium
            stamp_duty = 0.0
            
        exchange_charges = 0.000495 * price * qty  # NSE Option transaction charges: 0.0495%
        gst = 0.18 * (brokerage + exchange_charges)  # 18% GST on brokerage + exchange txn fee
        sebi_charges = 0.000001 * price * qty  # SEBI charges: 0.0001% (Rs 10 per crore)
        
        total_friction = brokerage + stt + exchange_charges + gst + sebi_charges + stamp_duty
        return total_friction
    else:
        brokerage = min(20.0, 0.0003 * price * qty)  # 0.03% capped at ₹20
        
        if transaction_type.upper() == "BUY":
            stt = 0.0
            stamp_duty = 0.00003 * price * qty  # 0.003% on buy side
        else:
            stt = 0.00025 * price * qty  # 0.025% on sell side (MIS/Intraday)
            stamp_duty = 0.0
            
        exchange_charges = 0.0000325 * price * qty  # NSE Equity transaction charges: 0.00325%
        gst = 0.18 * (brokerage + exchange_charges)
        sebi_charges = 0.000001 * price * qty
        
        total_friction = brokerage + stt + exchange_charges + gst + sebi_charges + stamp_duty
        return total_friction


def calculate_friction_adjusted_target(entry: float, sl: float, qty: int, is_long: bool, is_options: bool = True) -> float:
    """
    Solves algebraically for the gross target price required to achieve a strict 1:2 Risk-to-Reward ratio
    net of all transaction costs and statutory friction.
    """
    if qty <= 0:
        return entry

    # Calculate known friction amounts
    buy_f_entry = calculate_friction(entry, qty, "BUY", is_options)
    sell_f_sl = calculate_friction(sl, qty, "SELL", is_options)
    sell_f_entry = calculate_friction(entry, qty, "SELL", is_options)
    buy_f_sl = calculate_friction(sl, qty, "BUY", is_options)

    if is_long:
        net_risk = qty * (entry - sl) + buy_f_entry + sell_f_sl
        
        if is_options:
            f_sell_fixed = 20.0 * 1.18
            f_sell_var = 0.00125 + 0.000495 * 1.18 + 0.000001
        else:
            f_sell_fixed = 20.0 * 1.18
            f_sell_var = 0.00025 + 0.0000325 * 1.18 + 0.000001

        target = (2.0 * net_risk + qty * entry + buy_f_entry + f_sell_fixed) / (qty * (1.0 - f_sell_var))
    else:
        net_risk = qty * (sl - entry) + sell_f_entry + buy_f_sl
        
        if is_options:
            f_buy_fixed = 20.0 * 1.18
            f_buy_var = 0.000495 * 1.18 + 0.000001 + 0.00003
        else:
            f_buy_fixed = 20.0 * 1.18
            f_buy_var = 0.0000325 * 1.18 + 0.000001 + 0.00003

        target = (qty * entry - sell_f_entry - f_buy_fixed - 2.0 * net_risk) / (qty * (1.0 + f_buy_var))

    # Round precisely to the nearest tick size of ₹0.05
    tick_size = 0.05
    target = round(target / tick_size) * tick_size
    return max(target, tick_size)


def validate_trade_constraints(transaction_type: str, holding_timeframe: str) -> bool:
    """
    Enforces intraday and short-selling guidelines.
    """
    tx_type = transaction_type.upper()
    timeframe = holding_timeframe.upper()
    
    if tx_type == "SELL":
        if timeframe in ("SWING", "LONG_TERM"):
            logger.error(
                f"COMPLIANCE VIOLATION: Short-selling (transaction_type={transaction_type}) "
                f"is strictly blocked for holding timeframe: {holding_timeframe}."
            )
            return False
            
    return True


class StateManager:
    """
    Manages persistent local state.json tracking daily loss and trade counts
    to enforce over-trading lockouts.
    """
    
    def __init__(self, state_file_path: str = "db/state.json") -> None:
        self.state_file_path = state_file_path
        self._lock = threading.RLock()
        self._init_state()

    def _init_state(self) -> None:
        """Ensures state file exists and is initialized for today."""
        dir_name = os.path.dirname(self.state_file_path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        
        with self._lock:
            today_str = datetime.date.today().isoformat()
            state_exists = os.path.exists(self.state_file_path)
            
            if not state_exists:
                self._write_state_unsafe({
                    "date": today_str,
                    "daily_loss_amount": 0.0,
                    "trade_count_today": 0,
                    "is_locked_out": False
                })
            else:
                try:
                    state = self._read_state_unsafe()
                    if state.get("date") != today_str:
                        state["date"] = today_str
                        state["daily_loss_amount"] = 0.0
                        state["trade_count_today"] = 0
                        state["is_locked_out"] = False
                        self._write_state_unsafe(state)
                except Exception as e:
                    logger.error(f"Error reading state file, resetting: {e}")
                    self._write_state_unsafe({
                        "date": today_str,
                        "daily_loss_amount": 0.0,
                        "trade_count_today": 0,
                        "is_locked_out": False
                    })

    def _read_state_unsafe(self) -> Dict:
        with open(self.state_file_path, "r") as f:
            return json.load(f)

    def _write_state_unsafe(self, state: Dict) -> None:
        with open(self.state_file_path, "w") as f:
            json.dump(state, f, indent=4)

    def get_state(self) -> Dict:
        with self._lock:
            self._init_state()
            return self._read_state_unsafe()

    def update_state(self, daily_loss_add: float = 0.0, trade_add: int = 0, is_locked_out: Optional[bool] = None) -> Dict:
        with self._lock:
            self._init_state()
            state = self._read_state_unsafe()
            state["daily_loss_amount"] = max(0.0, state["daily_loss_amount"] + daily_loss_add)
            state["trade_count_today"] += trade_add
            if is_locked_out is not None:
                state["is_locked_out"] = is_locked_out
            self._write_state_unsafe(state)
            return state


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
        """
        risk_per_unit = abs(entry_price - stop_loss_price)
        if risk_per_unit <= 0:
            logger.warning("Stop loss is identical to entry price. Risk per unit is zero; returning 0 quantity.")
            return 0
            
        max_capital_loss = available_capital * risk_pct
        raw_qty = max_capital_loss / risk_per_unit
        max_affordable_qty = available_capital / entry_price
        qty = min(raw_qty, max_affordable_qty)
        
        if lot_size and lot_size > 0:
            qty = math.floor(qty / lot_size) * lot_size
            if qty < lot_size:
                logger.warning(f"Calculated quantity {qty} is less than lot size {lot_size}. Minimum allocation forced to 0.")
                return 0
        else:
            qty = math.floor(qty)
            
        logger.info(
            f"Sizing Calculation: Capital={available_capital:.2f}, Risk Pct={risk_pct:.2%}, "
            f"Risk/Unit={risk_per_unit:.2f}, Target Qty={qty} (Lot={lot_size})"
        )
        return qty


class RiskLifecycleOptimizer:
    """
    Implements a strict 1:2 Risk-to-Reward exit optimizer.
    Tracks state transitions of active trades and issues execution actions when target structures are reached.
    """
    
    def __init__(self, settings_mod=settings) -> None:
        self.settings = settings_mod
        
    def evaluate_position(self, position: Position, current_price: float) -> Optional[Dict]:
        """
        Evaluates current asset pricing against the position lifecycle rules.
        Exits the position if either the Stop Loss or the friction-adjusted Target Price is reached.
        
        :param position: The Position object to evaluate
        :param current_price: Current market tick price for the underlying index
        :return: Optional execution order payload if action needed, else None
        """
        if not position.is_active:
            return None

        # Determine directional triggers
        if position.is_long:
            hit_stop = current_price <= position.current_sl
            hit_target = current_price >= position.target_price if position.target_price > 0 else False
        else:
            hit_stop = current_price >= position.current_sl
            hit_target = current_price <= position.target_price if position.target_price > 0 else False

        # 1. Stop-Loss Trigger (Exit at stop-loss level)
        if hit_stop:
            logger.info(
                f"[STOP LOSS TRIGGERED] {position.symbol} hit Stop-Loss at {position.current_sl}. "
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
                "log": f"SL hit at {position.current_sl}. Closed position."
            }

        # 2. Strict 1:2 RR Target Trigger (Exit at friction-adjusted target)
        if hit_target:
            logger.info(
                f"[TARGET REACHED] {position.symbol} hit friction-adjusted 1:2 RR target at {position.target_price}. "
                f"Action: Closing full position of {position.current_qty} units."
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
                "log": f"Target reached at {position.target_price}. Closed position."
            }

        return None

    def _calculate_execution_limit(self, current_price: float, is_buy: bool) -> float:
        """
        Calculates a compliant Limit price with a slippage buffer.
        """
        buffer = self.settings.SLIPPAGE_BUFFER_TICKS * self.settings.TICK_SIZE
        if is_buy:
            limit = current_price + buffer
        else:
            limit = current_price - buffer
            
        limit = round(limit / self.settings.TICK_SIZE) * self.settings.TICK_SIZE
        return max(limit, self.settings.TICK_SIZE)


class DailyCircuitBreakerManager:
    """
    Monitors overall account equity parameters and triggers an immediate system disconnect
    if cumulative losses reach defined thresholds (25% of total capital) or daily trade limit is reached.
    """
    
    def __init__(self, initial_capital: float = settings.INITIAL_CAPITAL, state_manager: Optional[StateManager] = None) -> None:
        self.initial_capital = initial_capital
        self.max_loss_allowed = initial_capital * settings.MAX_DAILY_LOSS_LIMIT_PCT
        self.state_manager = state_manager
        logger.info(
            f"Daily Circuit Breaker initialized: Initial Capital=INR {initial_capital:,.2f}, "
            f"Max Loss Allowed=INR {self.max_loss_allowed:,.2f} ({settings.MAX_DAILY_LOSS_LIMIT_PCT:.1%})"
        )

    def calculate_total_pnl(self, realized_pnl: float, active_positions: List[Position], current_prices: Dict[str, float]) -> float:
        """Calculates total (realized + floating) PnL using option premiums."""
        total_pnl = realized_pnl
        
        for pos in active_positions:
            if not pos.is_active:
                total_pnl += pos.realized_pnl
                continue
                
            cur_price = current_prices.get(pos.token)
            if cur_price is None:
                # Use option premium entry price as fallback
                cur_price = pos.option_entry_price
                
            if pos.is_long:
                floating_pnl = (cur_price - pos.option_entry_price) * pos.current_qty
            else:
                floating_pnl = (pos.option_entry_price - cur_price) * pos.current_qty
                
            total_pnl += floating_pnl + pos.realized_pnl
            
        return total_pnl

    def monitor_pnl(
        self, 
        realized_pnl: float, 
        active_positions: List[Position], 
        current_prices: Dict[str, float], 
        executor_client=None,
        ws_client=None
    ) -> None:
        """
        Actively checks current PnL and trade counts. If limits are breached,
        disconnects the WebSocket, exits positions, and locks out.
        """
        if self.state_manager:
            state = self.state_manager.get_state()
            if state.get("is_locked_out"):
                return
                
            # Check Trade count breach
            max_trades = getattr(settings, "MAX_DAILY_TRADE_LIMIT", 5)
            if state.get("trade_count_today", 0) >= max_trades:
                logger.critical(
                    f"!!! TRADE LIMIT BREACH !!! Today's trade count {state['trade_count_today']} "
                    f"has reached the daily limit of {max_trades}. Locking out further executions."
                )
                self.state_manager.update_state(is_locked_out=True)
                self._trigger_emergency_shutdown(active_positions, current_prices, executor_client, ws_client)
                return

        total_pnl = self.calculate_total_pnl(realized_pnl, active_positions, current_prices)
        
        # If total PnL is negative and magnitude exceeds the circuit breaker limit
        if total_pnl < 0 and abs(total_pnl) >= self.max_loss_allowed:
            logger.critical(
                f"!!! CIRCUIT BREAKER CRITICAL BREACH !!! "
                f"Total Daily Loss: INR {abs(total_pnl):,.2f} exceeds Max Limit: INR {self.max_loss_allowed:,.2f}. "
                f"Initiating emergency system shutdown."
            )
            if self.state_manager:
                self.state_manager.update_state(daily_loss_add=abs(total_pnl), is_locked_out=True)
            self._trigger_emergency_shutdown(active_positions, current_prices, executor_client, ws_client)

    def _trigger_emergency_shutdown(
        self, 
        active_positions: List[Position], 
        current_prices: Dict[str, float], 
        executor_client,
        ws_client
    ) -> None:
        """
        Emergency closeout routines.
        Sends immediate limit exit orders with slippage buffers to liquidate all holdings.
        """
        logger.critical("EMERGENCY SHUTDOWN: Closing all open positions...")
        
        optimizer = RiskLifecycleOptimizer()
        
        for pos in active_positions:
            if pos.is_active:
                # To exit option contracts, we use option premium prices
                cur_price = current_prices.get(pos.token)
                if not cur_price:
                    logger.warning(f"No current price available for {pos.symbol} during emergency close. Using entry.")
                    cur_price = pos.option_entry_price
                    
                limit_price = optimizer._calculate_execution_limit(current_price=cur_price, is_buy=not pos.is_long)
                
                exit_payload = {
                    "action": "EMERGENCY_EXIT",
                    "symbol": pos.symbol,
                    "token": pos.token,
                    "exchange": pos.exchange,
                    "quantity": pos.current_qty,
                    "limit_price": limit_price,
                    "order_type": "LIMIT",
                    "transaction_type": "SELL" if pos.is_long else "BUY",
                    "product_type": "INTRADAY" if pos.exchange == "NSE" else "CARRYOVER"
                }
                
                logger.critical(f"Dispatching exit order: {exit_payload}")
                if executor_client:
                    try:
                        executor_client.execute_order(exit_payload)
                        pos.is_active = False
                    except Exception as e:
                        logger.critical(f"Failed to execute emergency exit for {pos.symbol}: {e}")
                else:
                    logger.warning("No executor client interface supplied. Logged exit intent only.")
                    pos.is_active = False

        if ws_client:
            try:
                logger.critical("Disconnecting WebSocket client connection...")
                ws_client.disconnect()
            except Exception as e:
                logger.critical(f"Failed to disconnect WebSocket client during shutdown: {e}")
                
        logger.critical("All active system processes terminated. Prevented revenge trading loops. Exiting environment.")
        sys.exit("SYSTEM SHUTDOWN: Daily Circuit Breaker loss limit breached.")
