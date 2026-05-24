import datetime
import logging
import socket
import sqlite3
import sys
from typing import Dict, List, Optional

from config import settings

logger = logging.getLogger("ExecutionEngine")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Guard against missing pyotp
try:
    import pyotp
except ImportError:
    logger.warning("pyotp library not installed. Running simulated OTP validation.")
    pyotp = None

# Guard against missing Angel One SmartAPI package during initial scaffolding
try:
    from SmartApi import SmartConnect
except ImportError:
    logger.warning("SmartConnect from SmartApi not installed. Operating in mock execution mode.")
    SmartConnect = None


class AngelOneExecutor:
    """
    Manages secure broker sessions, institutional database logging,
    and payload compliance for both Angel One SmartAPI and Zerodha Kite Connect.
    """

    def __init__(self) -> None:
        self.smart_connect: Optional[SmartConnect] = None
        self.jwt_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.feed_token: Optional[str] = None
        
        # Initialize SQLite database for Institutional Flows and Trades
        self.db_path = settings.DB_PATH
        self._init_database()
        
        # Enforce Static IP Guard at startup
        self._enforce_ip_whitelist()

    def _init_database(self) -> None:
        """Initializes SQLite database and tables for local auditing."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Table for FII and DII EOD institutional data
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS institutional_flows (
                    flow_date TEXT PRIMARY KEY,
                    fii_net_buy_cr REAL,
                    dii_net_buy_cr REAL,
                    composite_sentiment REAL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Table for executed trades logs
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_audit_logs (
                    trade_id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    symbol TEXT,
                    token TEXT,
                    exchange TEXT,
                    quantity INTEGER,
                    price REAL,
                    order_type TEXT,
                    transaction_type TEXT,
                    pnl_phase TEXT
                )
            """)
            conn.commit()
            conn.close()
            logger.info(f"Audit Database initialized successfully at {self.db_path}")
        except Exception as e:
            logger.error(f"Error initializing SQLite database: {e}")

    def _enforce_ip_whitelist(self) -> None:
        """Verifies current machine IP matches static whitelist values to satisfy regulatory compliance."""
        if not settings.ENFORCE_IP_WHITELIST:
            logger.info("IP Whitelisting guard is disabled in config. Skipping.")
            return

        try:
            # Retrieve local hostname and IP address
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            
            # Also fetch external public IP for cloud VMs
            try:
                import urllib.request
                external_ip = urllib.request.urlopen('https://ident.me', timeout=3).read().decode('utf8')
            except Exception:
                external_ip = None

            detected_ips = [local_ip]
            if external_ip:
                detected_ips.append(external_ip)
                
            is_whitelisted = any(ip in settings.WHITELISTED_IPS for ip in detected_ips)
            
            if not is_whitelisted:
                logger.critical(
                    f"COMPLIANCE ALERT: Execution blocked. System running on non-whitelisted IP address. "
                    f"Detected IPs: {detected_ips}. Whitelist: {settings.WHITELISTED_IPS}"
                )
                sys.exit("COMPLIANCE FAILURE: Unauthorized IP endpoint.")
                
            logger.info(f"IP Whitelist check passed. Running on authorized endpoint: {detected_ips}")
        except Exception as e:
            logger.error(f"Error executing IP verification checks: {e}")
            if settings.ENFORCE_IP_WHITELIST:
                sys.exit("COMPLIANCE FAILURE: IP verification error.")

    def generate_totp_token(self) -> str:
        """Generates dynamic Time-based One-Time Password using configuration secret."""
        if not pyotp:
            logger.warning("Mocking TOTP token response (pyotp absent).")
            return "123456"
        
        try:
            totp = pyotp.TOTP(settings.TOTP_SECRET)
            current_totp = totp.now()
            logger.info("Generated new session TOTP token successfully.")
            return current_totp
        except Exception as e:
            logger.error(f"Failed to generate TOTP secret: {e}")
            raise e

    def authenticate_session(self) -> bool:
        """Establishes login session with Angel One SmartAPI using TOTP and API credentials."""
        if SmartConnect is None:
            logger.warning("Operating in Mock Session authentication mode.")
            self.jwt_token = "mock_jwt_token_xxxx"
            self.refresh_token = "mock_refresh_token_yyyy"
            self.feed_token = "mock_feed_token_zzzz"
            return True

        try:
            self.smart_connect = SmartConnect(api_key=settings.API_KEY)
            totp_token = self.generate_totp_token()
            
            session_data = self.smart_connect.generateSession(
                clientCode=settings.CLIENT_ID,
                password=settings.PASSWORD,
                totp=totp_token
            )
            
            if session_data.get("status"):
                self.jwt_token = session_data["data"]["jwtToken"]
                self.refresh_token = session_data["data"]["refreshToken"]
                self.feed_token = session_data["data"]["feedToken"]
                logger.info(f"Angel One session authenticated successfully. Client: {settings.CLIENT_ID}")
                return True
            else:
                logger.error(f"Authentication failed: {session_data.get('message')}")
                return False
        except Exception as e:
            logger.error(f"Critical error during API session authentication: {e}")
            return False

    def refresh_session_tokens(self) -> None:
        """Refreshes the active JWT session token to prevent mid-day connection drops."""
        if not self.smart_connect or not self.refresh_token:
            logger.warning("Skipping session refresh (mock connection active).")
            return

        try:
            token_response = self.smart_connect.renewAccessToken(self.refresh_token)
            if token_response.get("status"):
                self.jwt_token = token_response["data"]["jwtToken"]
                logger.info("SmartAPI session token renewed successfully.")
            else:
                logger.error(f"Failed to renew session token: {token_response.get('message')}")
        except Exception as e:
            logger.error(f"Error renewing session JWT: {e}")

    def to_angel_one_payload(self, p: Dict) -> Dict:
        """
        Maps generic order arguments into a valid payload for Angel One SmartAPI.
        """
        # Determine exchange/segment product mapping (INTRADAY vs CARRYOVER / DELIVERY)
        exchange = p.get("exchange", "NFO")
        default_product = "CARRYOVER" if exchange == "NFO" else "DELIVERY"
        product = p.get("product_type", default_product).upper()
        
        # Enforce intraday MIS product type for short positions (SELL to open)
        if p.get("transaction_type") == "SELL" and exchange == "NSE":
            product = "INTRADAY"

        return {
            "variety": "NORMAL",
            "tradingsymbol": p["symbol"],
            "symboltoken": p["token"],
            "transactiontype": p["transaction_type"],
            "exchange": exchange,
            "ordertype": p.get("order_type", "LIMIT"),
            "producttype": product,
            "duration": "DAY",
            "price": f"{p['limit_price']:.2f}",
            "triggerprice": f"{p.get('trigger_price', 0.0):.2f}",
            "quantity": str(p["quantity"])
        }

    def to_zerodha_payload(self, p: Dict) -> Dict:
        """
        Maps generic order arguments into a valid payload for Zerodha Kite Connect.
        """
        exchange = p.get("exchange", "NFO")
        default_product = "NRML" if exchange == "NFO" else "CNC"
        product = p.get("product_type", default_product).upper()
        
        # Enforce intraday MIS product type for short positions (SELL to open)
        if p.get("transaction_type") == "SELL":
            product = "MIS"

        return {
            "exchange": exchange,
            "tradingsymbol": p["symbol"],
            "transaction_type": p["transaction_type"],
            "quantity": int(p["quantity"]),
            "price": float(f"{p['limit_price']:.2f}"),
            "product": product,
            "order_type": p.get("order_type", "LIMIT"),
            "trigger_price": float(f"{p.get('trigger_price', 0.0):.2f}"),
            "validity": "DAY"
        }

    def execute_order(self, order_payload: Dict, broker: str = "ANGELONE") -> Optional[str]:
        """
        Sends order instructions to the exchange.
        Enforces strict compliance:
        - Rounds prices precisely to the nearest tick size of ₹0.05.
        - Absolute 'Market' and 'IOC' orders are prohibited.
        - Converts all orders into LIMIT or STOP-LIMIT styles.
        """
        # 1. Enforce tick size (₹0.05) rounding for all pricing fields
        tick = 0.05
        if "limit_price" in order_payload:
            order_payload["limit_price"] = round(order_payload["limit_price"] / tick) * tick
        if "trigger_price" in order_payload:
            order_payload["trigger_price"] = round(order_payload["trigger_price"] / tick) * tick

        # 2. Validate order type compliance
        ord_type = order_payload.get("order_type", "").upper()
        duration = order_payload.get("duration", "DAY").upper()
        
        if ord_type == "MARKET" or duration == "IOC":
            raise ValueError(
                f"COMPLIANCE VIOLATION: Absolute MARKET and IOC orders are strictly prohibited. "
                f"Attempted order type: {ord_type}, duration: {duration}"
            )
            
        logger.info(
            f"Dispatching compliant order for {broker}: {order_payload.get('transaction_type')} "
            f"{order_payload.get('quantity')} {order_payload.get('symbol')} "
            f"@{order_payload.get('limit_price')}"
        )
        
        # Generate target payload mapping
        if broker.upper() == "ZERODHA":
            broker_payload = self.to_zerodha_payload(order_payload)
            logger.info(f"Mapped Zerodha payload: {broker_payload}")
        else:
            broker_payload = self.to_angel_one_payload(order_payload)
            logger.info(f"Mapped Angel One payload: {broker_payload}")

        # If in Mock execution mode
        if not self.smart_connect or broker.upper() == "ZERODHA":
            mock_order_id = f"MOCK_ORD_{datetime.datetime.now().strftime('%M%S%f')}"
            logger.info(f"Simulated execution complete. Mock Order ID: {mock_order_id}")
            self._log_trade_to_audit(mock_order_id, order_payload)
            return mock_order_id

        # Angel One Live Execution
        try:
            response = self.smart_connect.placeOrder(broker_payload)
            if response.get("status"):
                order_id = response["data"]["orderid"]
                logger.info(f"Order successfully filled on exchange. Exchange ID: {order_id}")
                self._log_trade_to_audit(order_id, order_payload)
                return order_id
            else:
                logger.error(f"Exchange rejected order: {response.get('message')}")
                return None
        except Exception as e:
            logger.critical(f"Network error dispatching order to exchange endpoint: {e}")
            return None

    def _log_trade_to_audit(self, trade_id: str, payload: Dict) -> None:
        """Stores transaction record in local audit database."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO trade_audit_logs 
                (trade_id, timestamp, symbol, token, exchange, quantity, price, order_type, transaction_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_id,
                    datetime.datetime.now().isoformat(),
                    payload["symbol"],
                    payload["token"],
                    payload["exchange"],
                    payload["quantity"],
                    payload["limit_price"],
                    payload["order_type"],
                    payload["transaction_type"]
                )
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error logging trade audit data: {e}")

    # --- Institutional EOD Sentiment Data Parsers ---
    
    def log_fii_dii_flow(self, date_str: str, fii_net_cr: float, dii_net_cr: float) -> None:
        """
        Logs FII and DII net cash volumes in Crore Rupees.
        Calculates a composite sentiment multiplier used to self-correct trading sizes.
        """
        composite_score = (fii_net_cr + dii_net_cr) / 1000.0
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO institutional_flows 
                (flow_date, fii_net_buy_cr, dii_net_buy_cr, composite_sentiment)
                VALUES (?, ?, ?, ?)
                """,
                (date_str, fii_net_cr, dii_net_cr, composite_score)
            )
            conn.commit()
            conn.close()
            logger.info(f"Logged Institutional Flows for {date_str}: FII={fii_net_cr}Cr, DII={dii_net_cr}Cr. Sentiment={composite_score:.2f}")
        except Exception as e:
            logger.error(f"Error inserting institutional flow: {e}")

    def get_historical_institutional_bias(self, lookback_days: int = 5) -> float:
        """
        Queries DB to calculate recent institutional net bias.
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT composite_sentiment FROM institutional_flows 
                ORDER BY flow_date DESC LIMIT ?
                """,
                (lookback_days,)
            )
            rows = cursor.fetchall()
            conn.close()
            
            if not rows:
                return 1.0
                
            avg_sentiment = sum(row[0] for row in rows) / len(rows)
            modifier = 1.0 + (avg_sentiment * 0.1)
            modifier = max(0.8, min(modifier, 1.2))
            
            logger.info(f"Calculated institutional bias multiplier over {len(rows)} days: {modifier:.2f}")
            return modifier
        except Exception as e:
            logger.error(f"Error querying historical institutional flow: {e}")
            return 1.0
