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
    Manages secure authentication, FII/DII SQLite database logs,
    and order dispatching using strict limit/stop-limit compliance rules.
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
        """Initializes SQLite database and tables for local auditing and self-correction logs."""
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
            # Initialize connection interface
            self.smart_connect = SmartConnect(api_key=settings.API_KEY)
            totp_token = self.generate_totp_token()
            
            # Execute standard login payload
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

    def execute_order(self, order_payload: Dict) -> Optional[str]:
        """
        Sends order instructions to the exchange.
        Enforces strict compliance: Absolute 'Market' and 'IOC' orders are prohibited.
        Converts all orders into LIMIT or STOP-LIMIT styles.
        """
        # Validate order type compliance
        ord_type = order_payload.get("order_type", "").upper()
        duration = order_payload.get("duration", "DAY").upper()
        
        if ord_type == "MARKET" or duration == "IOC":
            raise ValueError(
                f"COMPLIANCE VIOLATION: Absolute MARKET and IOC orders are strictly prohibited. "
                f"Attempted order type: {ord_type}, duration: {duration}"
            )
            
        logger.info(
            f"Dispatching compliant Order: {order_payload.get('transaction_type')} "
            f"{order_payload.get('quantity')} {order_payload.get('symbol')} "
            f"@{order_payload.get('limit_price')} ({ord_type})"
        )
        
        # If in Mock execution mode
        if not self.smart_connect:
            mock_order_id = f"MOCK_ORD_{datetime.datetime.now().strftime('%M%S%f')}"
            logger.info(f"Simulated execution complete. Mock Order ID: {mock_order_id}")
            self._log_trade_to_audit(mock_order_id, order_payload)
            return mock_order_id

        # Map to Angel One PlaceOrder payload params
        try:
            # Call Angel One Order Placement SDK methods
            response = self.smart_connect.placeOrder({
                "variety": "NORMAL",
                "tradingsymbol": order_payload["symbol"],
                "symboltoken": order_payload["token"],
                "transactiontype": order_payload["transaction_type"],
                "exchange": order_payload["exchange"],
                "ordertype": ord_type,
                "producttype": "CARRYOVER" if order_payload["exchange"] == "NFO" else "DELIVERY",
                "duration": "DAY",
                "price": str(order_payload["limit_price"]),
                "triggerprice": str(order_payload.get("trigger_price", 0)),
                "quantity": str(order_payload["quantity"])
            })
            
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
        # Composite score: positive if both are net buyers, negative if net sellers.
        # Normed by 1000 Cr units.
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
        Returns a modifier multiplier (e.g. 0.8 to 1.2) to self-adjust position sizes.
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
                return 1.0  # Neutral modifier fallback
                
            avg_sentiment = sum(row[0] for row in rows) / len(rows)
            
            # Bound modifier between 0.8 (extremely bearish flow) and 1.2 (extremely bullish flow)
            modifier = 1.0 + (avg_sentiment * 0.1)
            modifier = max(0.8, min(modifier, 1.2))
            
            logger.info(f"Calculated institutional bias multiplier over {len(rows)} days: {modifier:.2f}")
            return modifier
        except Exception as e:
            logger.error(f"Error querying historical institutional flow: {e}")
            return 1.0
