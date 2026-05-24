import sys
import os
from pathlib import Path
import json

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from config import settings
from core.risk_management import (
    calculate_friction,
    calculate_friction_adjusted_target,
    validate_trade_constraints,
    StateManager,
    DailyCircuitBreakerManager,
    Position,
    RiskLifecycleOptimizer
)
from core.executor import AngelOneExecutor

def run_tests():
    print("========================================")
    print("   STOCKBOT SAFETY & COMPLIANCE TESTS   ")
    print("========================================")

    # --- Test 1: Short Selling Constraints ---
    print("\n--- Test 1: Short Selling Constraints ---")
    # Case A: Intraday short-selling
    res_intraday = validate_trade_constraints(transaction_type="SELL", holding_timeframe="INTRADAY")
    print(f"Intraday Short-selling validation: {res_intraday} (Expected: True)")
    
    # Case B: Swing short-selling (should fail)
    res_swing = validate_trade_constraints(transaction_type="SELL", holding_timeframe="SWING")
    print(f"Swing Short-selling validation: {res_swing} (Expected: False)")

    # Case C: Long term short-selling (should fail)
    res_long = validate_trade_constraints(transaction_type="SELL", holding_timeframe="LONG_TERM")
    print(f"Long-term Short-selling validation: {res_long} (Expected: False)")

    # --- Test 2: Friction Adjusted Target (1:2 RR) ---
    print("\n--- Test 2: Friction-Adjusted 1:2 RR Targets ---")
    entry_price = 150.0
    sl_price = 100.0
    qty = 500
    
    # Long position target
    target_long = calculate_friction_adjusted_target(
        entry=entry_price,
        sl=sl_price,
        qty=qty,
        is_long=True,
        is_options=True
    )
    # Expected net risk: Qty * (Entry - SL) + Buy_Friction(Entry) + Sell_Friction(SL)
    # Let's verify target is > Entry + 2 * (Entry - SL)
    print(f"Long Option Entry: {entry_price}, SL: {sl_price}, Qty: {qty}")
    print(f"Friction-Adjusted Target Price: {target_long} (Standard 2.0x RR target would be {entry_price + 2*(entry_price-sl_price)})")
    
    # Short position target
    target_short = calculate_friction_adjusted_target(
        entry=entry_price,
        sl=200.0,
        qty=qty,
        is_long=False,
        is_options=True
    )
    print(f"Short Option Entry: {entry_price}, SL: 200.0, Qty: {qty}")
    print(f"Friction-Adjusted Target Price: {target_short} (Standard 2.0x RR target would be {entry_price - 2*(200.0-entry_price)})")

    # --- Test 3: Daily Drawdown Circuit Breaker & Lockout State ---
    print("\n--- Test 3: Circuit Breaker & Lockout Persistence ---")
    state_file = settings.STATE_FILE_PATH
    print(f"State file path: {state_file}")
    
    # Clear any active lockout for tests
    state_manager = StateManager(state_file)
    state_manager.update_state(daily_loss_add=-1000000.0, is_locked_out=False)
    
    initial_state = state_manager.get_state()
    print(f"Initial State: {initial_state}")

    # Set up Daily Circuit Breaker Manager
    breaker = DailyCircuitBreakerManager(initial_capital=100000.0, state_manager=state_manager)
    # Capital is 100,000. 25% drawdown = 25,000 loss limit.
    
    # Simulate active position losing ₹30,000
    pos = Position(
        symbol="NIFTY26MAY2622350CE",
        token="token_opt_123",
        exchange="NFO",
        is_long=True,
        option_entry_price=150.0,
        underlying_entry=22350.0,
        current_sl=22300.0,
        target_price=22450.0,
        initial_qty=600,
        current_qty=600
    )
    
    # If option price drops to 90.0, loss is (90 - 150) * 600 = -36,000
    current_prices = {"token_opt_123": 90.0}
    
    print("Monitoring PnL with -36,000 loss (drawdown limit 25,000)...")
    try:
        # We pass a mock executor client that records emergency close attempts
        class MockExecutor:
            def execute_order(self, payload):
                print(f"Mock Executor: Dispatching emergency order: {payload}")
                return "MOCK_EMERGENCY_ID"
                
        class MockWSClient:
            def disconnect(self):
                print("Mock WS Client: Disconnecting connection.")
                
        breaker.monitor_pnl(
            realized_pnl=0.0,
            active_positions=[pos],
            current_prices=current_prices,
            executor_client=MockExecutor(),
            ws_client=MockWSClient()
        )
    except SystemExit as se:
        print(f"System shut down expectedly: {se}")
        
    # Check that lockout state is updated in state.json
    final_state = state_manager.get_state()
    print(f"Final persisted state: {final_state}")
    
    if final_state.get("is_locked_out"):
        print("SUCCESS: Over-trading Lockout successfully triggered and persisted.")
    else:
        print("FAILED: Lockout state was not persisted.")

if __name__ == "__main__":
    run_tests()
