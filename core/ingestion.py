import logging
import queue
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
    Implements auto-reconnection handlers and a thread-safe producer-consumer pattern.
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
        
        # Thread-safe queue for raw price packets
        self.queue: queue.Queue = queue.Queue()
        self.is_running = False
        self._consumer_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._last_msg_time = time.time()

    def set_callback(self, callback: Callable[[Dict], None]) -> None:
        """Sets the callback handler for parsing received ticker wicks/ticks."""
        self.on_tick_callback = callback

    def connect(self) -> None:
        """Initializes connection to Angel One V2 WebSocket stream and starts worker threads."""
        with self._lock:
            if self.is_running:
                logger.info("WebSocket connection and background threads are already active.")
                return
            self.is_running = True

        # Start Consumer Thread
        self._consumer_thread = threading.Thread(target=self._consumer_loop, daemon=True)
        self._consumer_thread.start()
        logger.info("Started market tick consumer background thread.")

        # Start Heartbeat Thread
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        logger.info("Started 5-second background ping/pong heartbeat monitoring thread.")

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

    def disconnect(self) -> None:
        """Gracefully disconnects and stops background threads."""
        logger.info("Disconnecting WebSocket client and stopping background threads...")
        with self._lock:
            self.is_running = False
            self.is_connected = False
            
        if self.ws:
            try:
                self.ws.close()
            except Exception as e:
                logger.error(f"Error closing WebSocket: {e}")
                
        # Empty the queue and process tasks
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except queue.Empty:
                break
            except Exception:
                break
        logger.info("WebSocket client disconnected successfully.")

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
        self._last_msg_time = time.time()
        
        # Resubscribe to existing tokens if reconnecting
        with self._lock:
            for sub in self.subscriptions:
                try:
                    self.ws.subscribe(sub)
                    logger.info("Resubscribed to active contracts.")
                except Exception as e:
                    logger.error(f"Error resubscribing: {e}")

    def _on_data(self, ws, message) -> None:
        """Producer: Processes raw binary packets and places them instantly into the queue without math/callbacks."""
        self._last_msg_time = time.time()
        self.queue.put(message)

    def _on_error(self, ws, error) -> None:
        logger.error(f"WebSocket Client Error encountered: {error}")

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.warning(f"WebSocket closed. Code: {close_status_code}, Msg: {close_msg}")
        self.is_connected = False
        self._handle_reconnect()

    def _handle_reconnect(self) -> None:
        """Attempts to reconnect using exponential backoff."""
        if not self.is_running:
            return
            
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.critical("Maximum reconnect attempts reached. Ingestion Engine offline!")
            return
            
        self._reconnect_attempts += 1
        sleep_time = 2 ** self._reconnect_attempts
        logger.info(f"Attempting reconnect #{self._reconnect_attempts} in {sleep_time} seconds...")
        time.sleep(sleep_time)
        self.connect()

    def _consumer_loop(self) -> None:
        """Consumer: Worker thread that processes packets from the queue and calls the handler callback."""
        while self.is_running:
            try:
                # Wait for a tick message with a timeout to allow checking self.is_running
                tick_msg = self.queue.get(timeout=1.0)
                if self.on_tick_callback:
                    try:
                        self.on_tick_callback(tick_msg)
                    except Exception as e:
                        logger.error(f"Error processing tick callback: {e}")
                self.queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error in consumer loop thread: {e}")

    def _heartbeat_loop(self) -> None:
        """Runs a 5-second background ping/pong heartbeat check."""
        while self.is_running:
            time.sleep(5.0)
            if not self.is_connected or not self.is_running:
                continue
                
            time_since_last_msg = time.time() - self._last_msg_time
            logger.info(f"Heartbeat check: status=CONNECTED, seconds since last message={time_since_last_msg:.1f}s")
            
            # Send custom/SDK ping check if connection is live
            if self.ws:
                try:
                    # Depending on library, self.ws might have ping methods, or send an empty/ping payload
                    if hasattr(self.ws, "ping"):
                        self.ws.ping()
                    elif hasattr(self.ws, "keepalive"):
                        self.ws.keepalive()
                    else:
                        # Otherwise send a generic heartbeat payload if mock or custom ws
                        pass
                except Exception as e:
                    logger.error(f"WebSocket ping send failed: {e}. Reconnecting...")
                    self.is_connected = False
                    self._handle_reconnect()
                    continue

            # Auto-reconnection condition: if no data or heartbeat response in 15 seconds
            if time_since_last_msg > 15.0:
                logger.warning("No message received in 15 seconds. Triggering auto-reconnect.")
                self.is_connected = False
                self._handle_reconnect()

    def _mock_feed_loop(self) -> None:
        """Simulates real-time market data ticks for developer testing without credential payloads."""
        import random
        prices = {"26000": 22400.0, "26009": 22450.0} # Nifty mock indices
        
        while self.is_running and self.is_connected:
            tokens_to_stream = list(prices.keys())
            with self._lock:
                for sub in self.subscriptions:
                    for item in sub.get("params", {}).get("tokenList", []):
                        for t in item.get("tokens", []):
                            if t not in tokens_to_stream:
                                tokens_to_stream.append(t)
                                if t not in prices:
                                    # Initialize a random starting premium for options contract
                                    prices[t] = random.uniform(100.0, 180.0)
                                    
            for token in tokens_to_stream:
                if token in ("26000", "26009"):
                    change = random.uniform(-10.0, 10.0)
                else:
                    change = random.uniform(-3.0, 3.0)
                
                prices[token] += change
                prices[token] = max(prices[token], 0.05)  # Enforce minimum positive price
                
                tick = {
                    "token": token,
                    "last_traded_price": prices[token],
                    "open": prices[token] - 5,
                    "high": prices[token] + 7,
                    "low": prices[token] - 6,
                    "close": prices[token] - 1,
                    "volume": random.randint(1000, 5000),
                    "timestamp": time.time()
                }
                
                self._last_msg_time = time.time()
                self.queue.put(tick)
                    
            time.sleep(1.0)
