import os
from pathlib import Path
from typing import List

# Base Directory of the Project
BASE_DIR = Path(__file__).resolve().parent.parent

# --- Angel One SmartAPI Credentials ---
# In production, these should be loaded from environment variables or a secure vault
API_KEY = os.getenv("SMARTAPI_API_KEY", "your_api_key_here")
CLIENT_ID = os.getenv("SMARTAPI_CLIENT_ID", "your_client_id_here")
PASSWORD = os.getenv("SMARTAPI_PASSWORD", "your_password_here")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET", "your_totp_secret_here")

# --- Compliance & Network Guards ---
WHITELISTED_IPS: List[str] = [
    "127.0.0.1",  # Localhost for dev
    "192.168.1.100",  # Example static server IP
]
ENFORCE_IP_WHITELIST: bool = False  # Set to True in production to block non-whitelisted runtimes

# --- Risk Management Core Parameters ---
INITIAL_CAPITAL: float = 1000000.0  # ₹10,000,000 (10 Lakhs INR)
MAX_DAILY_LOSS_LIMIT_PCT: float = 0.25  # 25% daily circuit breaker
MAX_RISK_PER_TRADE_PCT: float = 0.015  # 1.5% capital risked per trade
HOLDING_TIMEFRAME: str = "INTRADAY"  # Options: INTRADAY, SWING, LONG_TERM
MAX_DAILY_TRADE_LIMIT: int = 5  # Lockout after 5 executed trades today
STATE_FILE_PATH = BASE_DIR / "db" / "state.json"
MAX_VIX_THRESHOLD: float = 22.0  # Max VIX allowed for trade days
MIN_FUNDAMENTAL_SCORE: float = 60.0  # Min fundamental results score required

# Smdh's 3-Phase Lifecycle Optimizer Constants
PHASE1_RR_THRESHOLD: float = 1.5  # Risk-to-Reward ratio to secure 50% profits
PHASE1_SELL_QUANTITY_PCT: float = 0.50  # Sell 50% of the position
PHASE3_MACRO_RR_TARGET: float = 4.0  # Final macro target

# --- Order Execution Buffers (Tick Size = 0.05 INR for Indian Markets)
TICK_SIZE: float = 0.05
SLIPPAGE_BUFFER_TICKS: int = 5  # Buffer to add to stop-limit or limit orders to ensure execution

# --- Options Processor Parameters ---
LOT_SIZES = {
    "NIFTY": 50,      # Current NSE Nifty lot size
    "BANKNIFTY": 15,  # Current NSE Bank Nifty lot size
    "FINNIFTY": 40,   # Current NSE Fin Nifty lot size
}

DEFAULT_IV_ESTIMATE: float = 0.18  # 18% implied volatility default
RISK_FREE_RATE: float = 0.07       # 7% standard Indian Treasury rate

# --- SQLite Database Settings ---
DB_DIR = BASE_DIR / "db"
DB_DIR.mkdir(exist_ok=True)
DB_PATH = DB_DIR / "trades_logger.db"
