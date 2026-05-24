import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from config import settings

logger = logging.getLogger("IngestionEngine")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Guard against missing Angel One SmartAPI package during initial scaffolding
try:
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
except ImportError:
    logger.warning("SmartApi python package not installed. Using mock socket implementation for local tests.")
    SmartWebSocketV2 = None


class AngelOneWebSocketClient:
    """
    Maintains a persistent, multi-threaded binary socket connection streaming real-time ticker data.
    Implements auto-reconnection handlers.
    """
    
    def __init__(self, auth_token: str, feed_token: str, client_code: str) -> None:
        self.auth_token = auth_token
        self.feed_token = feed_token
        self.client_code = client_code
        self.ws: Optional[SmartWebSocketV2] = None
        self.subscriptions: List[Dict] = []
        self.on_tick_callback: Optional[Callable[[Dict], None]] = None
        self.is_connected = False
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5
        self._lock = threading.Lock()

    def set_callback(self, callback: Callable[[Dict], None]) -> None:
        """Sets the callback handler for parsing received ticker wicks/ticks."""
        self.on_tick_callback = callback

    def connect(self) -> None:
        """Initializes connection to Angel One V2 WebSocket stream."""
        if SmartWebSocketV2 is None:
            logger.info("Starting Mock WebSocket connection...")
            self.is_connected = True
            threading.Thread(target=self._mock_feed_loop, daemon=True).start()
            return

        with self._lock:
            try:
                self.ws = SmartWebSocketV2(self.auth_token, self.feed_token, self.client_code, "correlation_id")
                
                # Assign V2 Callbacks
                self.ws.on_open = self._on_open
                self.ws.on_data = self._on_data
                self.ws.on_error = self._on_error
                self.ws.on_close = self._on_close

                # Connect in a background thread to prevent thread blocking
                logger.info("Connecting to Angel One V2 Streaming Server...")
                threading.Thread(target=self.ws.connect, daemon=True).start()
            except Exception as e:
                logger.error(f"Error opening WebSocket connection: {e}")
                self._handle_reconnect()

    def subscribe(self, token_list: List[str], segment: str = "NFO") -> None:
        """
        Subscribes to a list of exchange tokens.
        
        :param token_list: List of numerical instrument tokens (e.g. ['26000', '26009'])
        :param segment: Exchange Segment (NSE = 1, NFO = 2 usually represented as integer codes or segments)
        """
        # Map segment name to Angel Segment Code
        segment_code = 1 if segment == "NSE" else 2 # Default mapping
        
        sub_payload = {
            "action": 1,  # 1 = Subscribe, 2 = Unsubscribe
            "params": {
                "mode": 3,  # 1 = LTP, 2 = Quote, 3 = Snap Quote (Depth/OHLC)
                "tokenList": [
                    {"exchangeType": segment_code, "tokens": token_list}
                ]
            }
        }
        
        with self._lock:
            self.subscriptions.append(sub_payload)
            if self.is_connected and self.ws:
                try:
                    self.ws.subscribe(sub_payload)
                    logger.info(f"Subscribed to {len(token_list)} tokens in segment {segment}")
                except Exception as e:
                    logger.error(f"Error sending subscription payload: {e}")

    def _on_open(self) -> None:
        logger.info("Angel One V2 WebSocket Connection Established.")
        self.is_connected = True
        self._reconnect_attempts = 0
        
        # Resubscribe to existing tokens if reconnecting
        with self._lock:
            for sub in self.subscriptions:
                try:
                    self.ws.subscribe(sub)
                    logger.info("Resubscribed to active contracts.")
                except Exception as e:
                    logger.error(f"Error resubscribing: {e}")

    def _on_data(self, ws, message) -> None:
        """Processes raw binary packets and forwards to strategy parser."""
        if self.on_tick_callback:
            try:
                # In production, message is a parsed dictionary from V2 binary parser
                self.on_tick_callback(message)
            except Exception as e:
                logger.error(f"Error processing tick callback: {e}")

    def _on_error(self, ws, error) -> None:
        logger.error(f"WebSocket Client Error encountered: {error}")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.warning(f"WebSocket closed. Code: {close_status_code}, Msg: {close_msg}")
        self.is_connected = False
        self._handle_reconnect()

    def _handle_reconnect(self) -> None:
        """Attempts to reconnect using exponential backoff."""
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.critical("Maximum reconnect attempts reached. Ingestion Engine offline!")
            return
            
        self._reconnect_attempts += 1
        sleep_time = 2 ** self._reconnect_attempts
        logger.info(f"Attempting reconnect #{self._reconnect_attempts} in {sleep_time} seconds...")
        time.sleep(sleep_time)
        self.connect()

    def _mock_feed_loop(self) -> None:
        """Simulates real-time market data ticks for developer testing without credential payloads."""
        import random
        prices = {"26000": 22400.0, "26009": 22450.0} # Nifty mock indices
        
        while self.is_connected:
            for token, price in list(prices.items()):
                # Simulate small price movement
                change = random.uniform(-5.0, 5.0)
                prices[token] += change
                
                tick = {
                    "token": token,
                    "last_traded_price": prices[token],
                    "open": prices[token] - 50,
                    "high": prices[token] + 70,
                    "low": prices[token] - 60,
                    "close": prices[token] - 10,
                    "volume": random.randint(1000, 5000),
                    "timestamp": time.time()
                }
                
                if self.on_tick_callback:
                    self.on_tick_callback(tick)
                    
            time.sleep(1.0)
