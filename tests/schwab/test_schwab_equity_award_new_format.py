"""Test Schwab new equity award format parsing.

Tests the new Schwab Equity Awards Center CSV format with:
- Deposit transactions with detail rows containing VestDate and VestFairMarketValue
- Sale transactions with detail rows showing lots sold
- Individual account transactions with "as of" dates for same-day sell-to-cover
"""

import datetime
from decimal import Decimal
from pathlib import Path

from cgt_calc.model import ActionType
from cgt_calc.parsers.schwab import SchwabParser, _read_schwab_awards


class TestNewEquityAwardFormatParsing:
    """Test parsing of new Schwab equity award CSV format."""

    DATA_DIR = Path(__file__).parent / "data" / "equity_award_new_format"

    def test_awards_file_deposits_parsed(self) -> None:
        """Test Deposit transactions from awards file are parsed correctly."""
        award_prices, awards_transactions = _read_schwab_awards(
            self.DATA_DIR / "awards.csv"
        )

        # Filter for deposits (acquisitions)
        deposits = [
            t for t in awards_transactions if t.action == ActionType.STOCK_ACTIVITY
        ]

        # We have 5 deposits in the test data
        assert len(deposits) == 5

        # Note: transactions are NOT sorted within _read_schwab_awards -
        # they are sorted when combined with individual transactions.
        # Deposits use the Deposit date, not VestDate.
        # The first deposit in file order is 06/27/2024
        first_deposit = deposits[0]
        assert first_deposit.symbol == "TEST"
        assert first_deposit.date == datetime.date(2024, 6, 27)
        assert first_deposit.quantity == Decimal("10.000")
        # Vest price should be looked up from award_prices via the VestDate
        # For 06/27 deposit, vest date is 06/25 with FMV $98.00
        assert first_deposit.price == Decimal("98.00")

    def test_awards_file_sales_parsed(self) -> None:
        """Test Sale transactions from awards file are parsed correctly."""
        award_prices, awards_transactions = _read_schwab_awards(
            self.DATA_DIR / "awards.csv"
        )

        # Filter for sales (disposals)
        sales = [t for t in awards_transactions if t.action == ActionType.SELL]

        # We have 2 sales in the test data
        assert len(sales) == 2

        # First sale in file order is 07/07/2024
        first_sale = sales[0]
        assert first_sale.symbol == "TEST"
        assert first_sale.date == datetime.date(2024, 7, 7)
        assert first_sale.quantity == Decimal("30.000")
        assert first_sale.amount == Decimal("3000.00")
        # Price should be calculated from (amount + fees) / quantity
        expected_price = (Decimal("3000.00") + Decimal("0.01")) / Decimal("30.000")
        assert first_sale.price == expected_price

    def test_award_prices_extracted_from_deposits(self) -> None:
        """Test award prices (vest FMV) are extracted from deposit detail rows."""
        award_prices, awards_transactions = _read_schwab_awards(
            self.DATA_DIR / "awards.csv"
        )

        # Award prices should be extracted from the detail rows
        assert award_prices

        # Check specific vest prices exist
        # 03/25/2024 vest at $90.00
        date, price = award_prices.get(datetime.date(2024, 3, 25), "TEST")
        assert price == Decimal("90.00")

        # 05/25/2024 vest at $95.00
        date, price = award_prices.get(datetime.date(2024, 5, 25), "TEST")
        assert price == Decimal("95.00")

        # 06/25/2024 vest at $98.00
        date, price = award_prices.get(datetime.date(2024, 6, 25), "TEST")
        assert price == Decimal("98.00")

    def test_individual_file_sell_to_cover_parsed(self) -> None:
        """Test individual file with same-day sell-to-cover transactions."""
        # Load awards first (sets class variables)
        SchwabParser.awards_prices, SchwabParser.awards_transactions = (
            _read_schwab_awards(self.DATA_DIR / "awards.csv")
        )

        transactions = SchwabParser().load_from_file(self.DATA_DIR / "individual.csv")

        # Filter for sells (from both individual and awards files combined)
        sells = [t for t in transactions if t.action == ActionType.SELL]

        # We have 3 sells in individual file + 2 from awards = 5 total
        assert len(sells) == 5

        # First sell after sorting is from individual file
        # Date format "{txn_date} as of {settlement_date}" uses txn_date
        # So "04/01/2024 as of 03/28/2024" becomes 04/01/2024
        first_sell = sells[0]
        assert first_sell.symbol == "TEST"
        assert first_sell.quantity == Decimal("15")
        assert first_sell.price == Decimal("90.05")
        # Uses the transaction date (first part), not the "as of" date
        assert first_sell.date == datetime.date(2024, 4, 1)

    def test_as_of_date_uses_transaction_date(self) -> None:
        """Test 'as of' dates use the transaction date (before 'as of'), not settlement."""
        SchwabParser.awards_prices, SchwabParser.awards_transactions = (
            _read_schwab_awards(self.DATA_DIR / "awards.csv")
        )

        transactions = SchwabParser().load_from_file(self.DATA_DIR / "individual.csv")

        # Filter for sells
        sells = [t for t in transactions if t.action == ActionType.SELL]

        # The sell "06/27/2024 as of 06/26/2024" (from individual file)
        # should use 06/27/2024 as the date (transaction date, not settlement)
        june_sell = [s for s in sells if s.date == datetime.date(2024, 6, 27)]
        assert len(june_sell) == 1
        assert june_sell[0].symbol == "TEST"

    def test_nra_tax_adj_without_symbol_is_adjustment(self) -> None:
        """Test NRA Tax Adj without symbol is classified as ADJUSTMENT."""
        SchwabParser.awards_prices, SchwabParser.awards_transactions = (
            _read_schwab_awards(self.DATA_DIR / "awards.csv")
        )

        transactions = SchwabParser().load_from_file(self.DATA_DIR / "individual.csv")

        # Filter for adjustments (NRA Tax Adj without symbol)
        adjustments = [t for t in transactions if t.action == ActionType.ADJUSTMENT]

        # We have 2 NRA Tax Adj entries without symbol in the individual file
        assert len(adjustments) == 2

    def test_combined_transactions_sorted_chronologically(self) -> None:
        """Test combined transactions are sorted oldest first."""
        SchwabParser.awards_prices, SchwabParser.awards_transactions = (
            _read_schwab_awards(self.DATA_DIR / "awards.csv")
        )

        transactions = SchwabParser().load_from_file(self.DATA_DIR / "individual.csv")

        # Verify chronological order (oldest first)
        for i in range(1, len(transactions)):
            assert transactions[i - 1].date <= transactions[i].date

    def test_acquisitions_before_disposals_same_day(self) -> None:
        """Test acquisitions come before disposals on the same day."""
        SchwabParser.awards_prices, SchwabParser.awards_transactions = (
            _read_schwab_awards(self.DATA_DIR / "awards.csv")
        )

        transactions = SchwabParser().load_from_file(self.DATA_DIR / "individual.csv")

        # Group transactions by date
        by_date: dict[datetime.date, list] = {}
        for t in transactions:
            if t.date not in by_date:
                by_date[t.date] = []
            by_date[t.date].append(t)

        # For each date, check acquisitions come before disposals
        for date, day_transactions in by_date.items():
            seen_disposal = False
            for t in day_transactions:
                is_disposal = t.action == ActionType.SELL
                is_acquisition = t.action in [ActionType.BUY, ActionType.STOCK_ACTIVITY]
                if is_disposal:
                    seen_disposal = True
                if is_acquisition and seen_disposal:
                    raise AssertionError(
                        f"Acquisition {t} found after disposal on {date}"
                    )


class TestNewFormatIntegration:
    """Integration tests for new format parsing with actual CGT calculation."""

    DATA_DIR = Path(__file__).parent / "data" / "equity_award_new_format"

    def test_full_parsing_no_errors(self) -> None:
        """Test that full parsing completes without errors."""
        SchwabParser.awards_prices, SchwabParser.awards_transactions = (
            _read_schwab_awards(self.DATA_DIR / "awards.csv")
        )

        # This should not raise any exceptions
        transactions = SchwabParser().load_from_file(self.DATA_DIR / "individual.csv")

        # Verify we have transactions
        assert len(transactions) > 0

        # Verify we have both acquisitions and disposals
        acquisitions = [
            t
            for t in transactions
            if t.action in [ActionType.BUY, ActionType.STOCK_ACTIVITY]
        ]
        disposals = [t for t in transactions if t.action == ActionType.SELL]

        assert len(acquisitions) > 0
        assert len(disposals) > 0
