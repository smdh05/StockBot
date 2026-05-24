import logging
from typing import Dict

logger = logging.getLogger("ResultsAnalyzer")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class CompanyResultsAnalyzer:
    """
    Analyzes corporate financial results (Balance Sheet, Profit & Loss, Cashflow Statements)
    to enforce fundamental eligibility criteria for trading.
    """
    
    @staticmethod
    def analyze_financials(financials: Dict) -> Dict:
        """
        Analyzes a company's earnings reports across three financial statements:
        - balance_sheet: {'debt', 'equity', 'current_assets', 'current_liabilities'}
        - profit_loss: {'revenue', 'net_profit', 'revenue_growth_pct', 'profit_growth_pct'}
        - cash_flow: {'operating_cash_flow', 'investing_cash_flow', 'financing_cash_flow'}
        
        Returns:
          A dictionary with:
            - score: float (0 to 100)
            - is_eligible: bool
            - reason: str
        """
        score = 100.0
        reasons = []
        
        bs = financials.get("balance_sheet", {})
        pl = financials.get("profit_loss", {})
        cf = financials.get("cash_flow", {})
        
        # 1. Balance Sheet: Debt-to-Equity (Solvency check)
        debt = bs.get("debt", 0.0)
        equity = bs.get("equity", 1.0)
        de_ratio = debt / max(1.0, equity)
        if de_ratio > 2.0:
            score -= 20
            reasons.append(f"High Debt-to-Equity: {de_ratio:.2f}")
        elif de_ratio > 1.5:
            score -= 10
            reasons.append(f"Moderate Debt-to-Equity: {de_ratio:.2f}")
            
        # Balance Sheet: Current Ratio (Liquidity check)
        assets = bs.get("current_assets", 1.0)
        liabilities = bs.get("current_liabilities", 1.0)
        current_ratio = assets / max(1.0, liabilities)
        if current_ratio < 1.0:
            score -= 15
            reasons.append(f"Severe liquidity risk: Current Ratio {current_ratio:.2f} < 1.0")
        elif current_ratio < 1.2:
            score -= 5
            reasons.append(f"Low Current Ratio: {current_ratio:.2f}")

        # 2. Profit & Loss: Profit Margin
        revenue = pl.get("revenue", 1.0)
        net_profit = pl.get("net_profit", 0.0)
        np_margin = net_profit / max(1.0, revenue)
        if np_margin < 0.05:
            score -= 20
            reasons.append(f"Low Net Profit Margin: {np_margin:.2%}")
            
        # Profit & Loss: Growth Rates
        profit_growth = pl.get("profit_growth_pct", 0.0)
        if profit_growth < -15.0:
            score -= 15
            reasons.append(f"Severe profit contraction: {profit_growth:.1f}%")
        elif profit_growth < 0.0:
            score -= 5
            reasons.append(f"Negative profit growth: {profit_growth:.1f}%")

        # 3. Cash Flow Statement: Operating Cash Flow
        ocf = cf.get("operating_cash_flow", 0.0)
        if ocf <= 0.0:
            score -= 25
            reasons.append("Negative Operating Cash Flow (OCF)")
        
        # Cash Flow: Earnings Quality (OCF to Net Profit ratio)
        if net_profit > 0.0:
            cf_quality = ocf / net_profit
            if cf_quality < 0.5:
                score -= 10
                reasons.append(f"Poor earnings quality: OCF/Net Profit ratio {cf_quality:.2f} < 0.5")

        score = max(0.0, score)
        
        # Eligible if fundamental score is >= settings limit (default 60)
        from config import settings
        min_score = getattr(settings, "MIN_FUNDAMENTAL_SCORE", 60.0)
        is_eligible = score >= min_score
        
        reason_str = ", ".join(reasons) if reasons else "Robust fundamental metrics."
        logger.info(f"Financials Analysis: Score={score:.1f}/100, Eligible={is_eligible}, Reasons: {reason_str}")
        
        return {
            "score": score,
            "is_eligible": is_eligible,
            "reason": reason_str
        }
