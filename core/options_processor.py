import datetime
import json
import logging
import math
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import settings

# Setup logger for the options engine
logger = logging.getLogger("OptionsProcessor")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# --- Standard Normal Distribution Helpers for Black-Scholes ---
def norm_cdf(x: float) -> float:
    """Cumulative Distribution Function of standard normal distribution."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def norm_pdf(x: float) -> float:
    """Probability Density Function of standard normal distribution."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


class OptionsTokenRegistry:
    """
    Manages fetching, caching, and searching the Angel One instruments master file.
    The instruments JSON contains mapping for all active derivatives contracts (symbol, token, strike, expiry, lot size).
    """
    INSTRUMENTS_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    
    def __init__(self, cache_dir: Path = settings.DB_DIR) -> None:
        self.cache_path = cache_dir / "instruments_cache.json"
        self._instruments_cache: List[Dict] = []
        self._last_loaded_date: Optional[str] = None
        
    def sync_instruments(self, force: bool = False) -> None:
        """
        Downloads and caches instruments from Angel One API if not already cached today.
        Uses a robust stream read to handle network disconnects safely.
        """
        today_str = datetime.date.today().isoformat()
        
        if not force and self.cache_path.exists():
            # Check file modification date
            mtime = datetime.date.fromtimestamp(self.cache_path.stat().st_mtime)
            if mtime == datetime.date.today():
                logger.info("Instrument list is already cached for today. Skipping download.")
                self._load_from_cache()
                return
        
        logger.info("Downloading instruments master file from Angel One (this may take a few seconds)...")
        try:
            req = urllib.request.Request(
                self.INSTRUMENTS_URL, 
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode())
                
            # Filter only active NSE & NFO contracts (Equities and Options) to keep memory footprint low
            filtered_data = [
                inst for inst in data 
                if inst.get("exch_seg") in ("NSE", "NFO")
            ]
            
            with open(self.cache_path, "w") as f:
                json.dump(filtered_data, f)
                
            self._instruments_cache = filtered_data
            self._last_loaded_date = today_str
            logger.info(f"Successfully synchronized {len(filtered_data)} active contracts.")
        except Exception as e:
            logger.error(f"Failed to sync instrument registry from remote server: {e}. Falling back to old cache if available.")
            if self.cache_path.exists():
                self._load_from_cache()
            else:
                logger.critical("No local instruments cache found! Symbol mapping will fail.")
                raise e

    def _load_from_cache(self) -> None:
        """Loads cached instruments list into memory."""
        try:
            with open(self.cache_path, "r") as f:
                self._instruments_cache = json.load(f)
            self._last_loaded_date = datetime.date.today().isoformat()
            logger.info(f"Loaded {len(self._instruments_cache)} cached instruments into memory.")
        except Exception as e:
            logger.error(f"Error loading cached instruments: {e}")
            self._instruments_cache = []

    def get_token_details(
        self, underlying: str, expiry_date: str, strike_price: float, option_type: str
    ) -> Optional[Dict]:
        """
        Finds the exact Angel One token and trading symbol details for a specific option contract.
        
        :param underlying: Underlying symbol name (e.g. NIFTY, BANKNIFTY, RELIANCE)
        :param expiry_date: Format 'DDMMMYYYY' (e.g., '28MAY2026') or whatever representation Angel One uses.
        :param strike_price: Strike price (e.g., 22000.0)
        :param option_type: 'CE' or 'PE'
        :return: Dict of token properties or None
        """
        if not self._instruments_cache:
            self.sync_instruments()

        # Convert strike to standard float representing paises or matching precision format
        target_strike = float(strike_price)
        target_opt_type = option_type.upper()
        target_underlying = underlying.upper()

        for inst in self._instruments_cache:
            if inst.get("exch_seg") == "NFO":
                # Typically, options symbol format contains underlying, expiry, strike and option type
                # Example: NIFTY28MAY26C22000 or name matches option type criteria
                name = inst.get("name", "").upper()
                symbol = inst.get("symbol", "").upper()
                
                # Check symbol structure match
                if name == target_underlying:
                    try:
                        inst_strike = float(inst.get("strike", 0))
                        # Match parameters (allowing small epsilon for float comparison and handling Angel One x100 NFO options strike scaling)
                        if abs(inst_strike - target_strike) < 0.01 or abs((inst_strike / 100.0) - target_strike) < 0.01:
                            # Verify if the contract is CE or PE
                            symbol_suffix = symbol[-2:] # Usually CE or PE at end
                            if target_opt_type in symbol or symbol.endswith(target_opt_type) or inst.get("symbol", "").endswith(target_opt_type):
                                # Expiry filter - match dates safely (standardize format)
                                inst_expiry = inst.get("expiry", "") # Format: DDMMMYYYY
                                if expiry_date.upper() in inst_expiry.upper():
                                    return inst
                    except ValueError:
                        continue
        return None


class OptionsGreekCalculator:
    """
    Calculates Black-Scholes Greeks (Delta, Theta) and option pricing in pure Python.
    Optimized for execution checks.
    """
    
    @staticmethod
    def calculate_greeks(
        spot: float,
        strike: float,
        time_to_expiry_years: float,
        volatility: float = settings.DEFAULT_IV_ESTIMATE,
        rate: float = settings.RISK_FREE_RATE,
    ) -> Tuple[float, float, float, float]:
        """
        Calculates Black-Scholes Call Price, Put Price, Call Theta, and Put Theta.
        
        :param spot: Current stock/index spot price
        :param strike: Contract strike price
        :param time_to_expiry_years: Time left until expiry (as a fraction of a 365-day year)
        :param volatility: Annualized implied volatility (e.g. 0.18 for 18%)
        :param rate: Annual risk-free interest rate (e.g. 0.07 for 7%)
        :return: Tuple of (call_price, put_price, call_theta_daily, put_theta_daily)
        """
        # Edge case: Option already expired or time is zero
        if time_to_expiry_years <= 0.00001:
            call_val = max(0.0, spot - strike)
            put_val = max(0.0, strike - spot)
            return call_val, put_val, 0.0, 0.0

        # Avoid zero volatility crash
        vol = max(0.0001, volatility)

        d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * time_to_expiry_years) / (vol * math.sqrt(time_to_expiry_years))
        d2 = d1 - vol * math.sqrt(time_to_expiry_years)

        # Standard cumulative distributions
        n_d1 = norm_cdf(d1)
        n_d2 = norm_cdf(d2)
        n_minus_d1 = norm_cdf(-d1)
        n_minus_d2 = norm_cdf(-d2)

        # Option pricing
        call_price = spot * n_d1 - strike * math.exp(-rate * time_to_expiry_years) * n_d2
        put_price = strike * math.exp(-rate * time_to_expiry_years) * n_minus_d2 - spot * n_minus_d1

        # Probability density function for d1
        pdf_d1 = norm_pdf(d1)

        # Annual Theta formulas
        theta_call_annual = (
            -(spot * pdf_d1 * vol) / (2.0 * math.sqrt(time_to_expiry_years))
            - rate * strike * math.exp(-rate * time_to_expiry_years) * n_d2
        )
        
        theta_put_annual = (
            -(spot * pdf_d1 * vol) / (2.0 * math.sqrt(time_to_expiry_years))
            + rate * strike * math.exp(-rate * time_to_expiry_years) * n_minus_d2
        )

        # Convert to daily Theta (assuming 365 calendar days)
        call_theta_daily = theta_call_annual / 365.0
        put_theta_daily = theta_put_annual / 365.0

        return call_price, put_price, call_theta_daily, put_theta_daily


class OptionsProcessor:
    """
    Core engine handling strike selection, theta decay monitoring, and position hedging configurations.
    """
    
    def __init__(self, token_registry: OptionsTokenRegistry) -> None:
        self.registry = token_registry
        self.greek_calc = OptionsGreekCalculator()
        
    def select_strike(
        self, 
        underlying: str, 
        spot_price: float, 
        option_type: str, 
        expiry_date: str,
        bias: str = "ATM",
        step_size: float = 50.0
    ) -> Dict:
        """
        Maps spot price to the optimal Call/Put option contract.
        
        :param underlying: Underlying symbol (e.g. NIFTY)
        :param spot_price: Spot price of the underlying asset
        :param option_type: 'CE' or 'PE'
        :param expiry_date: Target expiry format 'DDMMMYYYY' (e.g., '28MAY2026')
        :param bias: 'ATM' (At-The-Money), 'OTM' (Out-of-The-Money)
        :param step_size: Strike increment step (e.g., 50.0 for Nifty, 100.0 for Bank Nifty)
        :return: Selected instrument token dictionary from registry
        """
        # Round to closest strike
        atm_strike = round(spot_price / step_size) * step_size
        target_strike = atm_strike
        
        if bias.upper() == "OTM":
            if option_type.upper() == "CE":
                # For Call, OTM is one step higher
                target_strike = atm_strike + step_size
            elif option_type.upper() == "PE":
                # For Put, OTM is one step lower
                target_strike = atm_strike - step_size
                
        logger.info(f"Selecting strike for {underlying}: Spot={spot_price:.2f}, Target Strike={target_strike:.2f} ({bias})")
        
        token_info = self.registry.get_token_details(
            underlying=underlying,
            expiry_date=expiry_date,
            strike_price=target_strike,
            option_type=option_type
        )
        
        if not token_info:
            # Fall back to ATM if OTM target doesn't exist
            logger.warning(f"Target strike {target_strike} not found for {option_type}. Falling back to ATM {atm_strike}.")
            token_info = self.registry.get_token_details(
                underlying=underlying,
                expiry_date=expiry_date,
                strike_price=atm_strike,
                option_type=option_type
            )
            
        if not token_info:
            raise ValueError(f"No option contract token details found for {underlying} Option Type {option_type} Expiry {expiry_date}")
            
        return token_info

    def should_block_long_purchase(
        self,
        spot_price: float,
        strike_price: float,
        expiry_date_str: str,
        is_market_consolidating: bool,
        volatility_estimate: float = settings.DEFAULT_IV_ESTIMATE,
    ) -> bool:
        """
        Theta Decay Filter logic. Blocks buying premium long contracts if the contract is close to expiry
        and the underlying index is consolidating, rendering premium decay highly disadvantageous.
        
        :param spot_price: Spot price of the underlying index
        :param strike_price: Target contract strike price
        :param expiry_date_str: Target expiry date 'YYYY-MM-DD'
        :param is_market_consolidating: Flag signaling structural range-bound consolidation (Low ADR/ATR)
        :param volatility_estimate: IV estimation
        :return: True if the purchase should be blocked due to decay risk, False otherwise
        """
        try:
            expiry_date = datetime.datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
        except ValueError:
            # Handle standard Angel One format if passed (e.g. 28MAY2026)
            try:
                expiry_date = datetime.datetime.strptime(expiry_date_str, "%d%b%Y").date()
            except ValueError:
                logger.error(f"Invalid expiry format: {expiry_date_str}. Permitting purchase as fallback.")
                return False

        today = datetime.date.today()
        days_to_expiry = (expiry_date - today).days

        # Risk parameters
        # 1. Close to expiry: less than or equal to 2 days left
        is_near_expiry = days_to_expiry <= 2
        
        if is_near_expiry:
            time_fraction = max(days_to_expiry, 0.5) / 365.0
            _, _, c_theta, p_theta = self.greek_calc.calculate_greeks(
                spot=spot_price,
                strike=strike_price,
                time_to_expiry_years=time_fraction,
                volatility=volatility_estimate
            )
            
            # Calculate option premium estimate
            call_price, _, _, _ = self.greek_calc.calculate_greeks(
                spot=spot_price,
                strike=strike_price,
                time_to_expiry_years=time_fraction,
                volatility=volatility_estimate
            )
            
            # Estimate percentage daily decay rate
            theta_impact = abs(c_theta)
            premium_est = max(call_price, 5.0)  # Avoid division by zero
            decay_percentage_daily = theta_impact / premium_est
            
            logger.info(f"Theta Decay Stats: Days to Expiry={days_to_expiry}, Est. Daily Decay={decay_percentage_daily:.2%}")

            # Condition to block: If market is consolidating and daily decay exceeds 15% of total option premium value
            if is_market_consolidating and (decay_percentage_daily > 0.15 or days_to_expiry <= 1):
                logger.warning(
                    f"BLOCKING option purchase: Expiry in {days_to_expiry} days while market is consolidating. "
                    f"Expected daily premium decay of {decay_percentage_daily:.1%} is excessive."
                )
                return True
                
        return False

    def calculate_protective_hedge(
        self,
        underlying: str,
        spot_price: float,
        primary_order_type: str,  # 'CASH' or 'CE'
        primary_qty: int,
        expiry_date: str,
        step_size: float = 50.0,
        hedge_ratio: float = 0.20  # Allocate ~20% of premium/position delta exposure to hedge
    ) -> Dict:
        """
        Hedging Core position insurance routine. Calculates target PE insurance contract properties
        to act as a structural loss floor when a large long Cash/CE directional position is opened.
        
        :param underlying: Underlying symbol (e.g. NIFTY)
        :param spot_price: Spot price of the asset
        :param primary_order_type: 'CASH' or 'CE'
        :param primary_qty: Entry volume quantity of primary trade
        :param expiry_date: Option expiry date 'DDMMMYYYY'
        :param step_size: Strike stepping size
        :param hedge_ratio: Percent weighting representing protective cover magnitude
        :return: Dict containing execution setup for the PE hedge contract
        """
        # Determine PE strike
        # Typically protective puts are purchased OTM (e.g., 2-3 step sizes below ATM)
        atm_strike = round(spot_price / step_size) * step_size
        pe_strike = atm_strike - (2 * step_size)  # Safe buffer OTM put option
        
        try:
            hedge_token_info = self.registry.get_token_details(
                underlying=underlying,
                expiry_date=expiry_date,
                strike_price=pe_strike,
                option_type="PE"
            )
        except Exception as e:
            logger.error(f"Error fetching hedge details from token registry: {e}")
            hedge_token_info = None

        if not hedge_token_info:
            # Fall back to ATM PE
            logger.warning(f"OTM protective strike {pe_strike} not found. Attempting ATM PE strike.")
            try:
                hedge_token_info = self.registry.get_token_details(
                    underlying=underlying,
                    expiry_date=expiry_date,
                    strike_price=atm_strike,
                    option_type="PE"
                )
            except Exception as e:
                logger.critical(f"Failed to resolve fallback ATM PE protective strike: {e}")
                raise e

        # Calculate appropriate lot quantity based on lot sizes
        lot_size = int(hedge_token_info.get("lotsize", settings.LOT_SIZES.get(underlying, 50)))
        
        # Calculate protective put premium estimate (using Black-Scholes formula)
        time_to_expiry_fraction = 7.0 / 365.0  # Assume 1 week to expiry as default estimate
        _, put_premium_est, _, _ = self.greek_calc.calculate_greeks(
            spot=spot_price, strike=pe_strike, time_to_expiry_years=time_to_expiry_fraction
        )
        put_premium_est = max(put_premium_est, 10.0) # Floor price estimation
        # Round to valid tick size
        put_premium_est = round(put_premium_est / settings.TICK_SIZE) * settings.TICK_SIZE
        
        if primary_order_type.upper() == "CASH":
            # For cash shares, we hedge based on contract delta equivalence
            # Let's target a hedge position size such that total hedge option value = primary position value * hedge_ratio
            est_underlying_value = primary_qty * spot_price
            target_hedge_value = est_underlying_value * hedge_ratio
            calculated_qty = int(target_hedge_value / (put_premium_est * lot_size)) * lot_size
        else:
            # If primary order is CE, hedge is direct contract volume ratio
            # Example: 1 hedge PE contract for every 4-5 CE contracts
            calculated_qty = max(int(primary_qty * hedge_ratio / lot_size) * lot_size, lot_size)

        # Force minimum 1 lot to prevent empty protection if calculations round down to zero
        if calculated_qty <= 0:
            calculated_qty = lot_size

        logger.info(
            f"Calculated protective Put Option: Contract={hedge_token_info.get('symbol')}, "
            f"Qty={calculated_qty} (Lot Size={lot_size}), Strike={pe_strike}"
        )
        
        return {
            "symbol": hedge_token_info.get("symbol"),
            "token": hedge_token_info.get("token"),
            "exchange": "NFO",
            "quantity": calculated_qty,
            "limit_price": put_premium_est,
            "order_type": "LIMIT",
            "transaction_type": "BUY"
        }
