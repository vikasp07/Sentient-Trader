"""
Multi-Ticker Extraction with Hybrid Approach (HARDENED)

Extracts multiple tickers from news articles using THREE methods:
- Method A: Explicit ticker symbols ($AAPL, AAPL) - weight 0.40
- Method B: Company name mapping (Apple Inc → AAPL) - weight 0.30
- Method C: spaCy NER (en_core_web_sm ORG entities) - weight 0.20

CONFIDENCE SCORING (multi-signal):
Each ticker gets a confidence score (0.0-1.0) based on:
- Source reliability: explicit (0.4) > company_name (0.3) > nlp_entity (0.2)
- HEADLINE BOOST: +0.15 if ticker/company appears in headline
- MULTI-METHOD BONUS: +0.10 if 2+ extraction methods agree
- FINANCIAL CONTEXT BONUS: +0.05-0.10 if keywords like 'buy', 'sell', 
  'earnings', 'revenue' appear within 100 chars of mention
- Frequency bonus: +0.05 per additional mention (max +0.20)

HARDENING:
- Dynamic confidence thresholds based on news source quality
- NEVER skips silently - logs WHY extraction failed with rejection_reason
- Index symbol filtering (^DJI, ^GSPC not tradeable)
- Explicit rejection reasons for debugging
"""

import re
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple
from collections import Counter

# Optional spaCy import
try:
    import spacy
    SPACY_AVAILABLE = True
except ImportError:
    SPACY_AVAILABLE = False
    spacy = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class TickerMatch:
    """Represents a single ticker match from text."""
    ticker: str
    method: str  # 'explicit', 'company_name', 'nlp_entity'
    matched_text: str
    position: int  # character position in text
    in_headline: bool = False
    frequency: int = 1


@dataclass
class ExtractionResult:
    """Final extraction result for a ticker with confidence score."""
    ticker: str
    confidence: float
    matches: List[TickerMatch] = field(default_factory=list)
    reasoning: str = ""
    rejection_reason: Optional[str] = None  # HARDENING: Why it was filtered/rejected
    
    def to_dict(self) -> dict:
        result = {
            "ticker": self.ticker,
            "confidence": round(self.confidence, 3),
            "match_count": len(self.matches),
            "methods_used": list(set(m.method for m in self.matches)),
            "in_headline": any(m.in_headline for m in self.matches),
            "reasoning": self.reasoning
        }
        if self.rejection_reason:
            result["rejection_reason"] = self.rejection_reason
        return result


# HARDENING: Source quality tiers for dynamic thresholds
SOURCE_QUALITY_TIERS = {
    "high": ["reuters", "bloomberg", "wsj", "ft.com", "cnbc", "marketwatch"],
    "medium": ["yahoo", "seeking", "fool", "investopedia", "barrons"],
    "low": ["reddit", "twitter", "stocktwits", "seeking alpha comments"],
}


class TickerExtractor:
    """
    Hybrid ticker extraction engine.
    
    Combines multiple methods for robust ticker detection:
    1. Explicit symbols: $AAPL or standalone AAPL
    2. Company name matching: "Apple Inc" → AAPL
    3. NLP entity recognition: ORG entities mapped to tickers
    """
    
    # Common words that look like tickers but aren't
    BLACKLIST = {
        # Common words
        'A', 'I', 'AM', 'PM', 'IS', 'IT', 'BE', 'BY', 'ON', 'OR', 'AS', 'AT',
        'AN', 'IF', 'TO', 'SO', 'GO', 'NO', 'UP', 'DO', 'IN', 'TV', 'AI', 'ML',
        'CEO', 'CFO', 'COO', 'CTO', 'IPO', 'GDP', 'ETF', 'SEC', 'FED', 'USA',
        'FBI', 'CIA', 'NSA', 'IMF', 'WHO', 'NYC', 'NYSE', 'ALL', 'NEW', 'NOW',
        'TOP', 'BIG', 'LOW', 'HIGH', 'BEST', 'NEXT', 'WEEK', 'YEAR', 'TIME',
        'GOOD', 'WELL', 'JUST', 'LIKE', 'MAKE', 'MADE', 'WORK', 'HOME', 'NEWS',
        # Days/Months
        'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN',
        'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC',
        # Financial terms
        'BUY', 'SELL', 'HOLD', 'LONG', 'SHORT', 'CALL', 'PUT', 'EPS', 'PE', 'ROI',
        'YTD', 'QTD', 'MTD', 'YOY', 'MOM', 'EOD', 'ATH', 'ATL',
        # Tech terms
        'API', 'SDK', 'SQL', 'CSV', 'JSON', 'XML', 'HTTP', 'URL', 'AWS', 'GCP',
        # Countries/Regions
        'UK', 'EU', 'US', 'JP', 'CN', 'HK', 'SG',
    }
    
    # Valid US stock tickers (most actively traded)
    VALID_TICKERS = {
        # Tech Giants
        'AAPL', 'MSFT', 'GOOGL', 'GOOG', 'AMZN', 'META', 'NVDA', 'TSLA',
        # Semiconductors
        'AMD', 'INTC', 'AVGO', 'QCOM', 'TXN', 'MU', 'AMAT', 'LRCX', 'KLAC', 'MRVL', 'ON',
        # Software/Cloud
        'CRM', 'ORCL', 'ADBE', 'NOW', 'SNOW', 'PLTR', 'DDOG', 'ZS', 'CRWD', 'NET',
        # Finance
        'JPM', 'BAC', 'WFC', 'GS', 'MS', 'C', 'BLK', 'SCHW', 'AXP', 'V', 'MA', 'PYPL',
        # Healthcare
        'JNJ', 'UNH', 'PFE', 'ABBV', 'MRK', 'LLY', 'TMO', 'ABT', 'BMY', 'AMGN', 'GILD',
        # Retail/Consumer
        'WMT', 'HD', 'COST', 'TGT', 'LOW', 'NKE', 'SBUX', 'MCD', 'KO', 'PEP',
        # Energy
        'XOM', 'CVX', 'COP', 'EOG', 'SLB', 'MPC', 'PSX', 'VLO', 'OXY',
        # Industrials
        'CAT', 'DE', 'BA', 'HON', 'UNP', 'UPS', 'FDX', 'LMT', 'RTX', 'GE', 'MMM',
        # Telecom/Media
        'T', 'VZ', 'TMUS', 'DIS', 'NFLX', 'CMCSA', 'PARA', 'WBD',
        # Auto
        'F', 'GM', 'RIVN', 'LCID', 'NIO', 'XPEV', 'LI',
        # Crypto-related
        'COIN', 'MSTR', 'RIOT', 'MARA', 'HUT',
        # Popular trading stocks
        'GME', 'AMC', 'BBBY', 'BB', 'NOK', 'SOFI', 'HOOD',
        # ETFs
        'SPY', 'QQQ', 'IWM', 'DIA', 'VTI', 'VOO', 'ARKK', 'XLF', 'XLE', 'XLK',
        # Indian stocks (ADRs)
        'INFY', 'WIT', 'HDB', 'IBN', 'TTM', 'VEDL', 'SIFY', 'RDY', 'WNS',
        # More large caps
        'BRK.A', 'BRK.B', 'TSM', 'ASML', 'SAP', 'TM', 'NVO', 'SHEL', 'SNY',
    }
    
    def __init__(self, company_map_path: Optional[str] = None):
        """
        Initialize the ticker extractor.
        
        Args:
            company_map_path: Path to JSON file with company name → ticker mappings
        """
        self.company_map: Dict[str, str] = {}
        self.reverse_map: Dict[str, List[str]] = {}  # ticker → company names
        
        # Load company mappings
        if company_map_path is None:
            # Default path relative to this file
            default_path = Path(__file__).parent.parent.parent / "data" / "company_ticker_map.json"
            if default_path.exists():
                company_map_path = str(default_path)
        
        if company_map_path:
            self._load_company_map(company_map_path)
        
        # ====================================================================
        # SPACY NER INTEGRATION: Additional extraction signal
        # 
        # spaCy's en_core_web_sm model detects ORG (organization) entities.
        # When an ORG entity maps to a known ticker, it adds confidence:
        # - Base weight: 0.20 (lower than explicit/company_name)
        # - BUT: when NER agrees with other methods → +0.10 multi-method bonus
        # 
        # This catches variations like "the iPhone maker" → Apple → AAPL
        # ====================================================================
        self.nlp = None
        if SPACY_AVAILABLE:
            try:
                self.nlp = spacy.load("en_core_web_sm")
                logger.info("spaCy NER model (en_core_web_sm) loaded - ORG entity extraction enabled")
            except OSError:
                logger.warning("spaCy model 'en_core_web_sm' not found. NLP extraction disabled.")
                logger.warning("Install with: python -m spacy download en_core_web_sm")
        else:
            logger.warning("spaCy not installed. NLP extraction disabled.")
        
        # Compile regex patterns
        self._compile_patterns()
        
        logger.info(f"TickerExtractor initialized with {len(self.company_map)} company mappings")
    
    def _load_company_map(self, path: str):
        """Load company name to ticker mappings from JSON file."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Flatten nested structure if present
            for category, mappings in data.items():
                if isinstance(mappings, dict):
                    for company_name, ticker in mappings.items():
                        company_lower = company_name.lower()
                        self.company_map[company_lower] = ticker
                        
                        # Build reverse map
                        if ticker not in self.reverse_map:
                            self.reverse_map[ticker] = []
                        self.reverse_map[ticker].append(company_name)
            
            logger.info(f"Loaded {len(self.company_map)} company mappings from {path}")
            
        except Exception as e:
            logger.error(f"Failed to load company map from {path}: {e}")
    
    def _compile_patterns(self):
        """Compile regex patterns for ticker extraction."""
        # Pattern for explicit $TICKER or standalone TICKER
        # $AAPL or AAPL (1-5 uppercase letters, not preceded by letters)
        self.explicit_pattern = re.compile(
            r'(?<![A-Za-z])\$?([A-Z]{1,5})(?:\.[A-Z])?(?![A-Za-z])',
            re.MULTILINE
        )
        
        # Pattern for company names - built dynamically from company_map
        self._build_company_patterns()
    
    def _build_company_patterns(self):
        """Build regex patterns for company name matching."""
        self.company_patterns: List[Tuple[re.Pattern, str]] = []
        
        for company_name, ticker in self.company_map.items():
            # Escape special regex characters
            escaped = re.escape(company_name)
            # Make it case-insensitive and word-bounded
            pattern = re.compile(rf'\b{escaped}\b', re.IGNORECASE)
            self.company_patterns.append((pattern, ticker))
    
    def extract(self, headline: str, body: str = "", min_confidence: float = 0.5,
                source_url: str = "") -> List[ExtractionResult]:
        """
        Extract tickers from headline and body text.
        
        Args:
            headline: Article headline/title
            body: Article body/snippet text
            min_confidence: Minimum confidence threshold (0.0-1.0)
            source_url: Source URL for quality-based threshold adjustment
        
        Returns:
            List of ExtractionResult objects, sorted by confidence (highest first)
        """
        all_matches: Dict[str, List[TickerMatch]] = {}
        rejected: List[ExtractionResult] = []  # HARDENING: Track rejections for logging
        
        # HARDENING: Adjust min_confidence based on source quality
        adjusted_confidence = self._adjust_confidence_threshold(min_confidence, source_url)
        if adjusted_confidence != min_confidence:
            logger.debug(f"Adjusted confidence threshold: {min_confidence:.2f} -> {adjusted_confidence:.2f} for source: {source_url}")
        
        # Combine texts for processing
        full_text = f"{headline}\n\n{body}"
        headline_len = len(headline)
        
        # Method A: Explicit ticker symbols
        explicit_matches = self._extract_explicit(full_text, headline_len)
        for match in explicit_matches:
            ticker = match.ticker
            if ticker not in all_matches:
                all_matches[ticker] = []
            all_matches[ticker].append(match)
        
        # Method B: Company name matching
        company_matches = self._extract_company_names(full_text, headline_len)
        for match in company_matches:
            ticker = match.ticker
            if ticker not in all_matches:
                all_matches[ticker] = []
            all_matches[ticker].append(match)
        
        # Method C: NLP entity recognition
        if self.nlp:
            nlp_matches = self._extract_nlp_entities(full_text, headline_len)
            for match in nlp_matches:
                ticker = match.ticker
                if ticker not in all_matches:
                    all_matches[ticker] = []
                all_matches[ticker].append(match)
        
        # Calculate confidence and build results
        results: List[ExtractionResult] = []
        
        for ticker, matches in all_matches.items():
            # HARDENING: Skip index symbols
            if ticker.startswith("^"):
                logger.debug(f"Skipping index symbol: {ticker}")
                rejected.append(ExtractionResult(
                    ticker=ticker, confidence=0.0, matches=matches,
                    rejection_reason="index_symbol"
                ))
                continue
            
            confidence, reasoning = self._calculate_confidence(ticker, matches, full_text)
            
            if confidence >= adjusted_confidence:
                result = ExtractionResult(
                    ticker=ticker,
                    confidence=confidence,
                    matches=matches,
                    reasoning=reasoning
                )
                results.append(result)
            else:
                # HARDENING: Log why it was skipped
                rejected.append(ExtractionResult(
                    ticker=ticker, 
                    confidence=confidence, 
                    matches=matches,
                    reasoning=reasoning,
                    rejection_reason=f"below_threshold({confidence:.3f}<{adjusted_confidence:.2f})"
                ))
        
        # Sort by confidence (highest first)
        results.sort(key=lambda x: x.confidence, reverse=True)
        
        # ====================================================================
        # HARDENING: NEVER skip silently - always log WHY extraction failed
        # ====================================================================
        if results:
            logger.info(f"✓ Extracted {len(results)} tickers from headline '{headline[:50]}...': "
                       f"{[(r.ticker, round(r.confidence, 2)) for r in results]}")
        elif rejected:
            # LOG WHY: Show rejected tickers with their rejection reasons
            rejection_summary = [(r.ticker, r.confidence, r.rejection_reason) for r in rejected[:5]]
            logger.warning(
                f"✗ No tickers passed threshold for '{headline[:50]}...'. "
                f"Rejected {len(rejected)} candidates: {rejection_summary}"
            )
        else:
            # LOG WHY: No candidates found at all
            logger.warning(
                f"✗ No ticker candidates found in '{headline[:50]}...'. "
                f"Reasons: no explicit $TICKER symbols, no company name matches, "
                f"spaCy NER={'enabled' if self.nlp else 'DISABLED'}"
            )
        
        return results
    
    def _adjust_confidence_threshold(self, base_threshold: float, source_url: str) -> float:
        """
        HARDENING: Adjust confidence threshold based on source quality.
        
        High quality sources (Reuters, Bloomberg) → lower threshold (0.4)
        Medium quality sources → standard threshold
        Low quality sources → higher threshold (0.65)
        """
        if not source_url:
            return base_threshold
        
        source_lower = source_url.lower()
        
        for domain in SOURCE_QUALITY_TIERS["high"]:
            if domain in source_lower:
                return max(0.4, base_threshold - 0.1)  # Lower threshold for trusted sources
        
        for domain in SOURCE_QUALITY_TIERS["low"]:
            if domain in source_lower:
                return min(0.8, base_threshold + 0.15)  # Higher threshold for noisy sources
        
        return base_threshold
    
    def _extract_explicit(self, text: str, headline_len: int) -> List[TickerMatch]:
        """Extract explicit ticker symbols like $AAPL or AAPL."""
        matches = []
        
        for match in self.explicit_pattern.finditer(text):
            ticker = match.group(1).upper()
            
            # Skip blacklisted terms
            if ticker in self.BLACKLIST:
                continue
            
            # Validate against known tickers or company map
            if ticker not in self.VALID_TICKERS and ticker not in self.reverse_map:
                continue
            
            position = match.start()
            in_headline = position < headline_len
            
            matches.append(TickerMatch(
                ticker=ticker,
                method='explicit',
                matched_text=match.group(0),
                position=position,
                in_headline=in_headline
            ))
        
        return matches
    
    def _extract_company_names(self, text: str, headline_len: int) -> List[TickerMatch]:
        """Extract tickers by matching company names."""
        matches = []
        
        for pattern, ticker in self.company_patterns:
            for match in pattern.finditer(text):
                position = match.start()
                in_headline = position < headline_len
                
                matches.append(TickerMatch(
                    ticker=ticker,
                    method='company_name',
                    matched_text=match.group(0),
                    position=position,
                    in_headline=in_headline
                ))
        
        return matches
    
    def _extract_nlp_entities(self, text: str, headline_len: int) -> List[TickerMatch]:
        """Extract tickers using spaCy NER for ORG entities."""
        matches = []
        
        if not self.nlp:
            return matches
        
        doc = self.nlp(text)
        
        for ent in doc.ents:
            if ent.label_ == 'ORG':
                # Try to map entity to ticker
                entity_lower = ent.text.lower().strip()
                
                # Direct match
                if entity_lower in self.company_map:
                    ticker = self.company_map[entity_lower]
                    position = ent.start_char
                    in_headline = position < headline_len
                    
                    matches.append(TickerMatch(
                        ticker=ticker,
                        method='nlp_entity',
                        matched_text=ent.text,
                        position=position,
                        in_headline=in_headline
                    ))
                else:
                    # Try partial matching for variations
                    ticker = self._fuzzy_company_match(entity_lower)
                    if ticker:
                        position = ent.start_char
                        in_headline = position < headline_len
                        
                        matches.append(TickerMatch(
                            ticker=ticker,
                            method='nlp_entity',
                            matched_text=ent.text,
                            position=position,
                            in_headline=in_headline
                        ))
        
        return matches
    
    def _fuzzy_company_match(self, entity: str) -> Optional[str]:
        """Try to match an entity to a company name with fuzzy matching."""
        entity_words = set(entity.lower().split())
        
        best_match = None
        best_score = 0
        
        for company_name, ticker in self.company_map.items():
            company_words = set(company_name.split())
            
            # Check word overlap
            overlap = len(entity_words & company_words)
            if overlap > 0:
                # Score based on overlap ratio
                score = overlap / max(len(entity_words), len(company_words))
                if score > best_score and score >= 0.5:
                    best_score = score
                    best_match = ticker
        
        return best_match
    
    def _calculate_confidence(self, ticker: str, matches: List[TickerMatch], full_text: str) -> Tuple[float, str]:
        """
        Calculate confidence score for a ticker based on MULTIPLE SIGNALS.
        
        CONFIDENCE FORMULA:
        confidence = source_weight + frequency_weight + headline_boost + multi_method_bonus + context_bonus
        
        CONFIDENCE BOOST FACTORS:
        1. Source reliability: explicit (0.4) > company_name (0.3) > nlp_entity (0.2)
        2. Frequency: +0.05 per mention, capped at +0.20
        3. HEADLINE PRESENCE: +0.15 if ticker/company appears in headline
        4. MULTI-METHOD AGREEMENT: +0.10 if 2+ extraction methods agree
        5. FINANCIAL CONTEXT: +0.05-0.10 if keywords nearby
        
        Returns:
            Tuple of (confidence_score, reasoning_string)
        """
        reasoning_parts = []
        
        # ====================================================================
        # SIGNAL 1: Source reliability weights
        # spaCy NER (nlp_entity) is an additional signal that boosts confidence
        # when it agrees with other methods (see multi_method_bonus below)
        # ====================================================================
        source_weights = {
            'explicit': 0.4,      # Highest - directly mentioned as $TICKER
            'company_name': 0.3,  # Medium - matched known company name
            'nlp_entity': 0.2,    # spaCy NER detected ORG entity → ticker
        }
        
        # 1. Source reliability weight (take best source)
        methods_used = [m.method for m in matches]
        method_counts = Counter(methods_used)
        best_method = max(method_counts.keys(), key=lambda m: source_weights[m])
        source_weight = source_weights[best_method]
        reasoning_parts.append(f"source:{best_method}={source_weight:.2f}")
        
        # ====================================================================
        # SIGNAL 2: Frequency - more mentions = higher confidence
        # ====================================================================
        frequency = len(matches)
        frequency_weight = min(0.2, frequency * 0.05)  # 0.05 per mention, max 0.2
        reasoning_parts.append(f"freq:{frequency}={frequency_weight:.2f}")
        
        # ====================================================================
        # SIGNAL 3: HEADLINE BOOST - company in headline = high relevance
        # Rationale: Headlines contain the main subject of the article
        # ====================================================================
        headline_boost = 0.15 if any(m.in_headline for m in matches) else 0
        if headline_boost > 0:
            reasoning_parts.append(f"headline_boost={headline_boost:.2f}")
        
        # ====================================================================
        # SIGNAL 4: MULTI-METHOD AGREEMENT BONUS
        # When multiple extraction methods agree (e.g., explicit + NER), 
        # confidence increases. This is where spaCy NER adds value!
        # ====================================================================
        unique_methods = len(set(methods_used))
        multi_method_bonus = 0.1 if unique_methods >= 2 else 0
        if multi_method_bonus > 0:
            reasoning_parts.append(f"multi_method_bonus(methods={unique_methods})={multi_method_bonus:.2f}")
        
        # 5. Context keywords bonus (financial action words near ticker)
        context_bonus = self._check_context_keywords(ticker, matches, full_text)
        if context_bonus > 0:
            reasoning_parts.append(f"context_bonus={context_bonus:.2f}")
        
        # Calculate total confidence (capped at 1.0)
        total_confidence = min(1.0, 
            source_weight + 
            frequency_weight + 
            headline_boost + 
            multi_method_bonus + 
            context_bonus
        )
        
        reasoning = f"[{ticker}] " + " + ".join(reasoning_parts) + f" = {total_confidence:.3f}"
        
        return total_confidence, reasoning
    
    def _check_context_keywords(self, ticker: str, matches: List[TickerMatch], full_text: str) -> float:
        """
        SIGNAL 5: FINANCIAL KEYWORDS PROXIMITY BONUS
        
        Check for financial context keywords within 100 chars of ticker mentions.
        This increases confidence when the ticker appears in a financial context.
        
        Bonus values:
        - 2+ context matches: +0.10
        - 1 context match: +0.05
        - No context: +0.00
        
        Returns:
            Confidence bonus (0.0, 0.05, or 0.10)
        """
        # Financial action/event keywords that indicate stock-relevant context
        context_keywords = {
            # Trading actions
            'buy', 'sell', 'hold', 'upgrade', 'downgrade', 'rating', 'target',
            # Financial metrics
            'earnings', 'revenue', 'profit', 'loss', 'growth', 'decline',
            # Analyst/forecast terms
            'beats', 'misses', 'exceeds', 'forecast', 'guidance', 'outlook',
            # Corporate events
            'announces', 'launches', 'acquires', 'merger', 'acquisition',
            # Market terms
            'stock', 'share', 'price', 'market', 'trading', 'investor',
        }
        
        text_lower = full_text.lower()
        
        # Count context keywords within 100 chars of any ticker mention
        context_found = 0
        for match in matches:
            start = max(0, match.position - 100)
            end = min(len(text_lower), match.position + 100)
            window = text_lower[start:end]
            
            for keyword in context_keywords:
                if keyword in window:
                    context_found += 1
                    break  # Only count once per match
        
        # Return bonus based on context density
        if context_found >= 2:
            return 0.1
        elif context_found >= 1:
            return 0.05
        return 0
    
    def add_ticker_to_valid(self, ticker: str):
        """Add a new valid ticker to the set."""
        self.VALID_TICKERS.add(ticker.upper())
    
    def add_company_mapping(self, company_name: str, ticker: str):
        """Add a new company name to ticker mapping."""
        company_lower = company_name.lower()
        self.company_map[company_lower] = ticker.upper()
        
        ticker_upper = ticker.upper()
        if ticker_upper not in self.reverse_map:
            self.reverse_map[ticker_upper] = []
        self.reverse_map[ticker_upper].append(company_name)
        
        # Rebuild patterns
        self._build_company_patterns()


# Convenience function for quick extraction
def extract_tickers(headline: str, body: str = "", min_confidence: float = 0.5,
                   source_url: str = "") -> List[Dict]:
    """
    Quick extraction function.
    
    Args:
        headline: Article headline
        body: Article body text
        min_confidence: Minimum confidence threshold
        source_url: Source URL for quality-based threshold adjustment
    
    Returns:
        List of dicts with ticker, confidence, and reasoning
    """
    extractor = TickerExtractor()
    results = extractor.extract(headline, body, min_confidence, source_url)
    return [r.to_dict() for r in results]


if __name__ == "__main__":
    # Test the extractor
    test_headline = "Apple's iPhone 15 sales surge while Tesla struggles with deliveries"
    test_body = """
    Apple Inc reported record-breaking iPhone 15 sales this quarter, with revenue 
    exceeding analyst expectations. Meanwhile, Tesla's delivery numbers came in 
    below forecast, causing TSLA stock to drop 5% in after-hours trading.
    
    Analysts at Goldman Sachs upgraded $AAPL to buy with a price target of $220.
    Microsoft and Amazon also reported strong cloud revenue growth.
    """
    
    extractor = TickerExtractor()
    results = extractor.extract(test_headline, test_body, min_confidence=0.3)
    
    print("\n=== Ticker Extraction Results ===\n")
    for result in results:
        print(f"Ticker: {result.ticker}")
        print(f"Confidence: {result.confidence:.3f}")
        print(f"Methods: {list(set(m.method for m in result.matches))}")
        print(f"In Headline: {any(m.in_headline for m in result.matches)}")
        print(f"Reasoning: {result.reasoning}")
        print("-" * 50)
