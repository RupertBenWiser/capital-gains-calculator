"""Charles Schwab parser."""

from __future__ import annotations

import argparse
from collections import OrderedDict, defaultdict
import csv
from dataclasses import dataclass
import datetime
from decimal import Decimal
from enum import Enum
import logging
from typing import TYPE_CHECKING, ClassVar, Final, TextIO

from cgt_calc.args_validators import DeprecatedAction, existing_file_type
from cgt_calc.const import TICKER_RENAMES
from cgt_calc.exceptions import (
    ParsingError,
    SymbolMissingError,
    UnexpectedColumnCountError,
    UnexpectedRowCountError,
)
from cgt_calc.model import ActionType, BrokerTransaction
from cgt_calc.parsers.schwab_cusip_bonds import adjust_cusip_bond_price

from .base_parsers import BaseSingleFileParser

if TYPE_CHECKING:
    from pathlib import Path

OLD_COLUMNS_NUM: Final = 9
NEW_COLUMNS_NUM: Final = 8
LOGGER = logging.getLogger(__name__)

# Cancel Buy search window: Arbitrary time window chosen as a sensible limit for
# how far to search backward from a Cancel Buy to find the original Buy transaction.
# This is not based on any documented Schwab settlement period - just a practical limit.
CANCEL_BUY_SEARCH_DAYS: Final = 5


class SchwabTransactionsFileRequiredHeaders(str, Enum):
    """Enum to list the headers in Schwab transactions file that we will use."""

    DATE = "Date"
    ACTION = "Action"
    SYMBOL = "Symbol"
    DESCRIPTION = "Description"
    PRICE = "Price"
    QUANTITY = "Quantity"
    FEES_AND_COMM = "Fees & Comm"
    AMOUNT = "Amount"


class AwardsTransactionsFileRequiredHeaders(str, Enum):
    """Enum to list the headers in Awards transactions file that we will use."""

    DATE = "Date"
    ACTION = "Action"
    SYMBOL = "Symbol"
    DESCRIPTION = "Description"
    QUANTITY = "Quantity"
    FEES_AND_COMM = "FeesAndCommissions"
    AMOUNT = "Amount"
    # Old format used FairMarketValuePrice, new format uses VestFairMarketValue
    FAIR_MARKET_VALUE_PRICE = "FairMarketValuePrice"
    VEST_FAIR_MARKET_VALUE = "VestFairMarketValue"
    VEST_DATE = "VestDate"


@dataclass
class AwardPrices:
    """Class to store initial stock prices."""

    award_prices: dict[datetime.date, dict[str, Decimal]]

    def __bool__(self) -> bool:
        """Return True if not empty."""
        return bool(self.award_prices)

    def get(self, date: datetime.date, symbol: str) -> tuple[datetime.date, Decimal]:
        """Get initial stock price at given date."""
        # Award dates may go back for few days, depending on
        # holidays or weekends, so we do a linear search
        # in the past to find the award price
        symbol = TICKER_RENAMES.get(symbol, symbol)
        for i in range(7):
            to_search = date - datetime.timedelta(days=i)

            if (
                to_search in self.award_prices
                and symbol in self.award_prices[to_search]
            ):
                return (to_search, self.award_prices[to_search][symbol])
        raise KeyError(f"Award price is not found for symbol {symbol} for date {date}")


def action_from_str(label: str, file: Path) -> ActionType:
    """Convert string label to ActionType."""
    if label in ["Buy", "Cancel Buy"]:
        return ActionType.BUY

    if label == "Sell":
        return ActionType.SELL

    if label in [
        "MoneyLink Transfer",
        "Misc Cash Entry",
        "Service Fee",
        "Wire Funds",
        "Wire Sent",
        "Funds Received",
        "Journal",
        "Cash In Lieu",
        "Visa Purchase",
        "MoneyLink Deposit",
        "MoneyLink Adj",  # likely a returned transfer
        "Security Transfer",
    ]:
        return ActionType.TRANSFER

    if label == "Stock Plan Activity":
        return ActionType.STOCK_ACTIVITY

    if label in [
        "Qualified Dividend",
        "Cash Dividend",
        "Qual Div Reinvest",
        "Div Adjustment",
        "Special Qual Div",
        "Non-Qualified Div",
    ]:
        return ActionType.DIVIDEND

    if label in ["NRA Tax Adj", "NRA Withholding", "Foreign Tax Paid"]:
        return ActionType.DIVIDEND_TAX

    if label == "ADR Mgmt Fee":
        return ActionType.FEE

    if label in ["Adjustment", "IRS Withhold Adj", "Wire Funds Adj"]:
        return ActionType.ADJUSTMENT

    if label in ["Short Term Cap Gain", "Long Term Cap Gain"]:
        return ActionType.CAPITAL_GAIN

    if label == "Spin-off":
        return ActionType.SPIN_OFF

    if label in ["Credit Interest", "Bond Interest"]:
        return ActionType.INTEREST

    if label == "Reinvest Shares":
        return ActionType.REINVEST_SHARES

    if label == "Reinvest Dividend":
        return ActionType.REINVEST_DIVIDENDS

    if label == "Wire Funds Received":
        return ActionType.WIRE_FUNDS_RECEIVED

    if label == "Stock Split":
        return ActionType.STOCK_SPLIT

    if label in ["Cash Merger", "Cash Merger Adj"]:
        return ActionType.CASH_MERGER

    if label in ["Full Redemption", "Full Redemption Adj"]:
        return ActionType.FULL_REDEMPTION

    raise ParsingError(file, f"Unknown action: '{label}'")


class SchwabTransaction(BrokerTransaction):
    """Represent single Schwab transaction."""

    def __init__(
        self,
        row_dict: OrderedDict[str, str],
        file: Path,
    ):
        """Create transaction from CSV row."""
        if len(row_dict) < NEW_COLUMNS_NUM or len(row_dict) > OLD_COLUMNS_NUM:
            # Old transactions had empty 9th column.
            raise UnexpectedColumnCountError(
                list(row_dict.values()), NEW_COLUMNS_NUM, file
            )
        if len(row_dict) == OLD_COLUMNS_NUM and list(row_dict.values())[-1] != "":
            raise ParsingError(file, f"Column {OLD_COLUMNS_NUM} should be empty")
        as_of_str = " as of "
        date_header = SchwabTransactionsFileRequiredHeaders.DATE.value
        if as_of_str in row_dict[date_header]:
            index = row_dict[date_header].find(as_of_str)
            date_str = row_dict[date_header][:index]
        else:
            date_str = row_dict[date_header]
        try:
            date = datetime.datetime.strptime(date_str, "%m/%d/%Y").date()
        except ValueError as exc:
            raise ParsingError(
                file, f"Invalid date format: {date_str} from row: {row_dict}"
            ) from exc
        action_header = SchwabTransactionsFileRequiredHeaders.ACTION.value
        self.raw_action = row_dict[action_header]
        action = action_from_str(self.raw_action, file)
        symbol_header = SchwabTransactionsFileRequiredHeaders.SYMBOL.value
        symbol = row_dict[symbol_header] if row_dict[symbol_header] != "" else None
        if symbol is not None:
            symbol = TICKER_RENAMES.get(symbol, symbol)
        description_header = SchwabTransactionsFileRequiredHeaders.DESCRIPTION.value
        description = row_dict[description_header]
        price_header = SchwabTransactionsFileRequiredHeaders.PRICE.value
        price = (
            Decimal(row_dict[price_header].replace("$", "").replace(",", ""))
            if row_dict[price_header] != ""
            else None
        )
        quantity_header = SchwabTransactionsFileRequiredHeaders.QUANTITY.value
        quantity = (
            Decimal(row_dict[quantity_header].replace(",", ""))
            if row_dict[quantity_header] != ""
            else None
        )
        fees_header = SchwabTransactionsFileRequiredHeaders.FEES_AND_COMM.value
        fees = (
            Decimal(row_dict[fees_header].replace("$", ""))
            if row_dict[fees_header] != ""
            else Decimal(0)
        )
        amount_header = SchwabTransactionsFileRequiredHeaders.AMOUNT.value
        amount = (
            Decimal(row_dict[amount_header].replace("$", ""))
            if row_dict[amount_header] != ""
            else None
        )

        # Handle bonds/notes: CUSIP symbols have price per $100 face value
        price, fees = adjust_cusip_bond_price(symbol, price, quantity, amount, fees)

        currency = "USD"
        broker = "Charles Schwab"
        super().__init__(
            date,
            action,
            symbol,
            description,
            quantity,
            price,
            fees,
            amount,
            currency,
            broker,
        )

    @staticmethod
    def create(
        row_dict: OrderedDict[str, str],
        file: Path,
        awards_prices: AwardPrices,
        derived_prices: dict[datetime.date, dict[str, Decimal]] | None = None,
    ) -> SchwabTransaction:
        """Create and post process a SchwabTransaction."""
        transaction = SchwabTransaction(row_dict, file)

        # Handle NRA Tax Adj without symbol (e.g., withholding on interest)
        # as an adjustment rather than dividend tax
        if (
            transaction.action == ActionType.DIVIDEND_TAX
            and transaction.symbol is None
        ):
            transaction.action = ActionType.ADJUSTMENT

        if (
            transaction.price is None
            and transaction.action == ActionType.STOCK_ACTIVITY
        ):
            symbol = transaction.symbol
            if symbol is None:
                raise SymbolMissingError(transaction)
            # Schwab transaction list contains sometimes incorrect date
            # for awards which don't match the PDF statements.
            # We want to make sure to match date and price from the awards
            # spreadsheet.
            try:
                _vest_date, transaction.price = awards_prices.get(
                    transaction.date, symbol
                )
            except KeyError:
                # If not found in awards file, try derived prices from same-day sells
                if derived_prices is not None:
                    # Search same date range as AwardPrices.get does (up to 7 days back)
                    for i in range(7):
                        search_date = transaction.date - datetime.timedelta(days=i)
                        if (
                            search_date in derived_prices
                            and symbol in derived_prices[search_date]
                        ):
                            transaction.price = derived_prices[search_date][symbol]
                            LOGGER.info(
                                "Using derived vest price for %s on %s: $%s "
                                "(from same-day sell transaction)",
                                symbol,
                                transaction.date,
                                transaction.price,
                            )
                            break
                    else:
                        raise KeyError(
                            f"Award price is not found for symbol {symbol} "
                            f"for date {transaction.date}"
                        )
                else:
                    raise
        return transaction


def _derive_vest_prices_from_sells(
    lines: list[list[str]],
    headers: list[str],
    file: Path,
) -> dict[datetime.date, dict[str, Decimal]]:
    """Derive vest prices from same-day sell transactions.

    For RSU vest + sell-to-cover transactions, the sell price closely approximates
    the Fair Market Value at vest time. This is used as a fallback when the
    Awards file doesn't contain vest price data for certain dates.

    This function looks for Stock Plan Activity entries immediately followed
    by Sell entries on the same date (or with "as of" dating) and uses the
    sell price as the derived vest FMV.
    """
    derived_prices: dict[datetime.date, dict[str, Decimal]] = defaultdict(dict)

    date_header = SchwabTransactionsFileRequiredHeaders.DATE.value
    action_header = SchwabTransactionsFileRequiredHeaders.ACTION.value
    symbol_header = SchwabTransactionsFileRequiredHeaders.SYMBOL.value
    price_header = SchwabTransactionsFileRequiredHeaders.PRICE.value

    as_of_str = " as of "

    # Parse all rows into a list of dicts for easier processing
    rows = []
    for line in lines:
        if not any(line):
            continue
        row_dict = OrderedDict(zip(headers, line, strict=True))
        rows.append(row_dict)

    # Look for Stock Plan Activity followed by same-day Sell
    for i, row in enumerate(rows):
        if row[action_header] != "Stock Plan Activity":
            continue

        # Get the activity date
        date_str = row[date_header]
        if as_of_str in date_str:
            # Use the "as of" date for matching
            date_str = date_str[date_str.find(as_of_str) + len(as_of_str) :]
        try:
            activity_date = datetime.datetime.strptime(date_str, "%m/%d/%Y").date()
        except ValueError:
            continue

        symbol = row[symbol_header]
        if not symbol:
            continue

        symbol = TICKER_RENAMES.get(symbol, symbol)

        # Look for a Sell on the same "as of" date nearby (search up to 3 rows)
        for j in range(max(0, i - 3), min(len(rows), i + 4)):
            if j == i:
                continue
            sell_row = rows[j]
            if sell_row[action_header] != "Sell":
                continue
            if sell_row[symbol_header] != row[symbol_header]:
                continue

            # Check if the sell date matches (with "as of" support)
            sell_date_str = sell_row[date_header]
            if as_of_str in sell_date_str:
                sell_date_str = sell_date_str[
                    sell_date_str.find(as_of_str) + len(as_of_str) :
                ]
            try:
                sell_date = datetime.datetime.strptime(sell_date_str, "%m/%d/%Y").date()
            except ValueError:
                continue

            # Allow a small date range (vest might be a few days off from sell
            # due to weekends/holidays - "as of" dates can be up to 4 days apart)
            if abs((sell_date - activity_date).days) <= 4:
                price_str = sell_row[price_header]
                if price_str:
                    price = Decimal(price_str.replace("$", "").replace(",", ""))
                    # Use the activity date as the key (that's when we need the FMV)
                    derived_prices[activity_date][symbol] = price
                    LOGGER.debug(
                        "Derived vest price for %s on %s: $%s from sell on %s",
                        symbol,
                        activity_date,
                        price,
                        sell_date,
                    )
                break

    return dict(derived_prices)


def _combine_cash_merger_pair(
    cash_merger: SchwabTransaction,
    cash_merger_adj: SchwabTransaction,
    transactions_file: Path,
) -> SchwabTransaction:
    """Combine Cash Merger + Cash Merger Adj into single transaction.

    Cash Merger: Has Amount (proceeds), No Quantity/Price
    Cash Merger Adj: Has Quantity (shares), No Amount

    Returns: Unified transaction with calculated price
    """
    try:
        # Validate matching fields
        assert cash_merger.description == cash_merger_adj.description
        assert cash_merger.symbol == cash_merger_adj.symbol
        assert cash_merger.date == cash_merger_adj.date

        # Validate data pattern: Cash Merger has amount, Adj has quantity
        assert cash_merger.amount is not None, "Cash Merger must have Amount"
        assert cash_merger.quantity is None, "Cash Merger should not have Quantity"
        assert cash_merger.price is None, "Cash Merger should not have Price"

        assert cash_merger_adj.quantity is not None, (
            "Cash Merger Adj must have Quantity"
        )
        assert cash_merger_adj.amount is None, "Cash Merger Adj should not have Amount"

    except AssertionError as err:
        raise ParsingError(
            transactions_file,
            f"Invalid Cash Merger format: {cash_merger.raw_action}, "
            "run with --verbose for more details",
        ) from err

    # Create unified transaction
    unified = cash_merger
    # Quantity is negative (shares leaving), convert to positive for SELL
    unified.quantity = -1 * cash_merger_adj.quantity
    # Mypy: at this point unified.amount and unified.quantity are guaranteed non-None
    assert unified.amount is not None
    assert unified.quantity is not None
    unified.price = unified.amount / unified.quantity
    unified.fees += cash_merger_adj.fees

    return unified


def _combine_full_redemption_pair(
    full_redemption_adj: SchwabTransaction,
    full_redemption: SchwabTransaction,
    transactions_file: Path,
) -> SchwabTransaction:
    """Combine Full Redemption Adj + Full Redemption into single transaction.

    Actual Schwab CSV format:
    Full Redemption Adj: Has Amount (proceeds), No Price/Quantity
    Full Redemption: Has Quantity (shares), No Price/Amount

    Returns: Unified transaction with calculated price
    """
    try:
        # Validate matching fields
        assert full_redemption_adj.description == full_redemption.description
        assert full_redemption_adj.symbol == full_redemption.symbol
        assert full_redemption_adj.date == full_redemption.date

        # Validate data pattern: Adj has amount, Full Redemption has quantity
        assert full_redemption_adj.amount is not None, (
            "Full Redemption Adj must have Amount"
        )
        assert full_redemption_adj.quantity is None, (
            "Full Redemption Adj should not have Quantity"
        )
        assert full_redemption_adj.price is None, (
            "Full Redemption Adj should not have Price"
        )

        assert full_redemption.quantity is not None, (
            "Full Redemption must have Quantity"
        )
        assert full_redemption.price is None, "Full Redemption should not have Price"
        assert full_redemption.amount is None, "Full Redemption should not have Amount"

    except AssertionError as err:
        raise ParsingError(
            transactions_file,
            f"Invalid Full Redemption format: {full_redemption.raw_action}, "
            "run with --verbose for more details",
        ) from err

    # Create unified transaction (use Full Redemption as base for action type)
    unified = full_redemption
    # Quantity is negative (shares leaving), convert to positive
    unified.quantity = -1 * full_redemption.quantity
    unified.amount = full_redemption_adj.amount
    # Mypy: at this point unified.amount and unified.quantity are guaranteed non-None
    assert unified.amount is not None
    assert unified.quantity is not None
    unified.price = unified.amount / unified.quantity
    unified.fees += full_redemption_adj.fees

    return unified


def _unify_schwab_paired_transactions(
    transactions: list[SchwabTransaction],
    transactions_file: Path,
) -> list[SchwabTransaction]:
    """Unify paired transactions (Cash Merger and Full Redemption).

    Both follow a similar pattern where transactions are split into two rows:
    1. One row has the amount (proceeds)
    2. Other row has the quantity (shares)
    We combine them to calculate the price per share.

    Cash Merger pattern:
        Row 1: "Cash Merger" - Has Amount ($1000), No Quantity/Price
        Row 2: "Cash Merger Adj" - Has Quantity (-100 shares), No Amount
        Result: Sell 100 shares at $10/share

    Full Redemption pattern:
        Row 1: "Full Redemption Adj" - Has Quantity (-100 shares), No Amount
        Row 2: "Full Redemption" - Has Amount ($1000), No Quantity/Price
        Result: Sell 100 shares at $10/share
    """
    filtered: list[SchwabTransaction] = []
    i = 0
    while i < len(transactions):
        transaction = transactions[i]

        if transaction.raw_action == "Cash Merger Adj":
            # Cash Merger Adj comes AFTER Cash Merger
            assert len(filtered) > 0, (
                "Cash Merger Adj must be preceded by a Cash Merger transaction"
            )
            main_transaction = filtered[-1]
            adj_transaction = transaction

            # Validate it's a Cash Merger pair
            assert main_transaction.raw_action == "Cash Merger", (
                "Cash Merger Adj must follow Cash Merger"
            )

            unified = _combine_cash_merger_pair(
                main_transaction, adj_transaction, transactions_file
            )

            # Replace the previous Cash Merger with unified transaction
            filtered[-1] = unified
            LOGGER.warning(
                "Cash Merger support is not complete and doesn't cover the "
                "cases when shares are received aside from cash, "
                "please review this transaction carefully: %s",
                unified,
            )

        elif transaction.raw_action == "Full Redemption Adj":
            # Full Redemption Adj comes BEFORE Full Redemption
            assert i + 1 < len(transactions), (
                "Full Redemption Adj must be followed by a Full Redemption transaction"
            )
            adj_transaction = transaction
            main_transaction = transactions[i + 1]

            # Validate it's a Full Redemption pair
            assert main_transaction.raw_action == "Full Redemption", (
                "Full Redemption Adj must be followed by Full Redemption"
            )

            unified = _combine_full_redemption_pair(
                adj_transaction, main_transaction, transactions_file
            )

            # Add unified transaction and skip next (Full Redemption)
            filtered.append(unified)
            i += 1  # Skip the Full Redemption transaction
            LOGGER.warning(
                "Full Redemption combined with adjustment: %s",
                unified,
            )

        else:
            filtered.append(transaction)

        i += 1

    return filtered


def _filter_cancelled_buy_transactions(
    transactions: list[SchwabTransaction],
) -> list[SchwabTransaction]:
    """Filter out Cancel Buy transactions and their matching Buy transactions.

    Schwab reports both the original Buy and a "Cancel Buy" transaction when a
    purchase is cancelled. Both need to be removed to avoid incorrect capital
    gains calculations.

    This is a Schwab-specific quirk - other brokers may not report cancellations
    at all or may handle them differently.

    Args:
        transactions: List of parsed Schwab transactions

    Returns:
        Filtered list with Cancel Buy pairs removed

    """
    indices_to_remove: set[int] = set()

    # Find all Cancel Buy transactions
    for cancel_idx, transaction in enumerate(transactions):
        if transaction.raw_action != "Cancel Buy":
            continue

        # Already marked for removal
        if cancel_idx in indices_to_remove:
            continue

        # Search backward for matching Buy within search window
        for buy_idx in range(cancel_idx - 1, -1, -1):
            buy_txn = transactions[buy_idx]

            # Stop if beyond search window
            if abs((buy_txn.date - transaction.date).days) > CANCEL_BUY_SEARCH_DAYS:
                break

            # Skip if already marked for removal
            if buy_idx in indices_to_remove:
                continue

            # Check if this is the matching Buy transaction
            if (
                buy_txn.action == ActionType.BUY
                and buy_txn.symbol == transaction.symbol
                and buy_txn.quantity == transaction.quantity
                and buy_txn.price == transaction.price
            ):
                # Found matching pair - mark both for removal
                indices_to_remove.add(cancel_idx)
                indices_to_remove.add(buy_idx)
                LOGGER.info(
                    "Matched Cancel Buy with original Buy: symbol=%s, qty=%s, "
                    "price=%s, buy_date=%s, cancel_date=%s",
                    buy_txn.symbol,
                    buy_txn.quantity,
                    buy_txn.price,
                    buy_txn.date,
                    transaction.date,
                )
                break
        else:
            # No matching Buy found
            LOGGER.warning(
                "Could not find matching Buy for Cancel Buy: %s",
                transaction,
            )

    if len(indices_to_remove) > 0:
        LOGGER.info(
            "Removed %d cancelled transaction(s) and their originals",
            len(indices_to_remove),
        )

    # Return filtered list
    return [txn for i, txn in enumerate(transactions) if i not in indices_to_remove]


def _read_schwab_awards_old_format(
    lines: list[list[str]],
    headers: list[str],
    schwab_award_transactions_file: Path,
) -> AwardPrices:
    """Read awards from old format (paired rows with FairMarketValuePrice)."""
    initial_prices: dict[datetime.date, dict[str, Decimal]] = defaultdict(dict)

    modulo = len(lines) % 2
    if modulo != 0:
        raise UnexpectedRowCountError(
            len(lines) - modulo + 2, schwab_award_transactions_file
        )

    date_column = AwardsTransactionsFileRequiredHeaders.DATE.value
    symbol_header = AwardsTransactionsFileRequiredHeaders.SYMBOL.value
    price_column = AwardsTransactionsFileRequiredHeaders.FAIR_MARKET_VALUE_PRICE.value

    for upper_row, lower_row in zip(lines[::2], lines[1::2], strict=True):
        # in this format each row is split into two rows,
        # so we combine them safely below
        row = []
        for upper_col, lower_col in zip(upper_row, lower_row, strict=True):
            assert upper_col == "" or lower_col == ""
            row.append(upper_col + lower_col)

        if len(row) != len(headers):
            raise UnexpectedColumnCountError(
                row, len(headers), schwab_award_transactions_file
            )

        row_dict = OrderedDict(zip(headers, row, strict=True))
        date_str = row_dict[date_column]
        try:
            date = datetime.datetime.strptime(date_str, "%Y/%m/%d").date()
        except ValueError:
            date = datetime.datetime.strptime(date_str, "%m/%d/%Y").date()
        symbol = row_dict[symbol_header] if row_dict[symbol_header] != "" else None
        price = (
            Decimal(row_dict[price_column].replace("$", ""))
            if row_dict[price_column] != ""
            else None
        )
        if symbol is not None and price is not None:
            symbol = TICKER_RENAMES.get(symbol, symbol)
            initial_prices[date][symbol] = price

    return AwardPrices(award_prices=dict(initial_prices))


def _read_schwab_awards_new_format(
    lines: list[list[str]],
    headers: list[str],
) -> AwardPrices:
    """Read awards from new format (VestDate and VestFairMarketValue columns).

    New format has different row structures:
    - Sale/Wire Transfer/Dividend rows may have multiple detail rows
    - Deposit rows follow the 2-row pattern (header + detail)
    - We extract VestDate and VestFairMarketValue from any row that has them
    """
    initial_prices: dict[datetime.date, dict[str, Decimal]] = defaultdict(dict)

    date_column = AwardsTransactionsFileRequiredHeaders.VEST_DATE.value
    symbol_header = AwardsTransactionsFileRequiredHeaders.SYMBOL.value
    price_column = AwardsTransactionsFileRequiredHeaders.VEST_FAIR_MARKET_VALUE.value

    date_idx = headers.index(date_column)
    symbol_idx = headers.index(symbol_header)
    price_idx = headers.index(price_column)

    # Track current symbol from main rows (Deposit rows have symbol)
    current_symbol: str | None = None

    for row in lines:
        if len(row) != len(headers):
            continue

        # Update current symbol if this row has one
        if row[symbol_idx]:
            current_symbol = row[symbol_idx]

        # Extract vest date and price if present
        date_str = row[date_idx]
        price_str = row[price_idx]

        if date_str and price_str:
            try:
                date = datetime.datetime.strptime(date_str, "%Y/%m/%d").date()
            except ValueError:
                date = datetime.datetime.strptime(date_str, "%m/%d/%Y").date()

            price = Decimal(price_str.replace("$", "").replace(",", ""))

            if current_symbol is not None:
                symbol = TICKER_RENAMES.get(current_symbol, current_symbol)
                initial_prices[date][symbol] = price

    return AwardPrices(award_prices=dict(initial_prices))


def _read_schwab_awards_transactions_new_format(
    lines: list[list[str]],
    headers: list[str],
    file_path: Path,  # noqa: ARG001
    award_prices: AwardPrices,
) -> list[BrokerTransaction]:
    """Extract actual transactions from new format awards file.

    The new format contains:
    - Deposit rows: RSU vest (Stock Plan Activity) - acquisition
    - Sale rows: Sell-to-cover transactions - disposal
    - Wire Transfer, Dividend, Tax Withholding: ancillary

    Sale rows have detail rows beneath them showing which lots were sold,
    with VestDate and VestFairMarketValue for each lot.
    """
    transactions: list[BrokerTransaction] = []

    # Get column indices
    date_idx = headers.index(AwardsTransactionsFileRequiredHeaders.DATE.value)
    action_idx = headers.index(AwardsTransactionsFileRequiredHeaders.ACTION.value)
    symbol_idx = headers.index(AwardsTransactionsFileRequiredHeaders.SYMBOL.value)
    desc_idx = headers.index(AwardsTransactionsFileRequiredHeaders.DESCRIPTION.value)
    qty_idx = headers.index(AwardsTransactionsFileRequiredHeaders.QUANTITY.value)
    fees_idx = headers.index(AwardsTransactionsFileRequiredHeaders.FEES_AND_COMM.value)
    amount_idx = headers.index(AwardsTransactionsFileRequiredHeaders.AMOUNT.value)
    vest_fmv_idx = headers.index(
        AwardsTransactionsFileRequiredHeaders.VEST_FAIR_MARKET_VALUE.value
    )

    current_symbol: str | None = None

    for row in lines:
        if len(row) != len(headers):
            continue

        # Update current symbol if this row has one
        if row[symbol_idx]:
            current_symbol = row[symbol_idx]

        date_str = row[date_idx]
        action = row[action_idx]

        if not date_str or not action:
            continue

        # Parse date
        try:
            date = datetime.datetime.strptime(date_str, "%m/%d/%Y").date()
        except ValueError:
            try:
                date = datetime.datetime.strptime(date_str, "%Y/%m/%d").date()
            except ValueError:
                continue

        symbol = current_symbol
        if symbol:
            symbol = TICKER_RENAMES.get(symbol, symbol)

        description = row[desc_idx]

        # Parse quantity
        qty_str = row[qty_idx]
        quantity = (
            Decimal(qty_str.replace(",", ""))
            if qty_str
            else None
        )

        # Parse fees
        fees_str = row[fees_idx]
        fees = (
            Decimal(fees_str.replace("$", "").replace(",", ""))
            if fees_str
            else Decimal(0)
        )

        # Parse amount
        amount_str = row[amount_idx]
        amount = (
            Decimal(amount_str.replace("$", "").replace(",", ""))
            if amount_str
            else None
        )

        if action == "Sale" and symbol and quantity:
            # This is a sell transaction
            # The main Sale row doesn't have SalePrice - it's in the detail rows.
            # Calculate price from amount/quantity for the main Sale row.
            # Note: amount already has fees deducted, so we add fees back to get gross
            price = None
            if amount is not None and quantity > 0:
                # Amount is net proceeds (after fees), so gross = amount + fees
                gross_amount = amount + fees
                price = gross_amount / quantity

            txn = BrokerTransaction(
                date=date,
                action=ActionType.SELL,
                symbol=symbol,
                description=description,
                quantity=quantity,
                price=price,
                fees=fees,
                amount=amount,
                currency="USD",
                broker="Charles Schwab",
            )
            transactions.append(txn)
            LOGGER.debug(
                "Extracted Sale from awards file: %s %s @ $%s on %s",
                quantity,
                symbol,
                price,
                date,
            )

        elif action == "Deposit" and symbol and quantity:
            # This is a stock plan activity (RSU vest)
            # Get vest price from award_prices or from the detail row
            vest_fmv_str = row[vest_fmv_idx]

            price = None
            if vest_fmv_str:
                price = Decimal(vest_fmv_str.replace("$", "").replace(",", ""))
            elif award_prices:
                try:
                    _, price = award_prices.get(date, symbol)
                except KeyError:
                    pass

            txn = BrokerTransaction(
                date=date,
                action=ActionType.STOCK_ACTIVITY,
                symbol=symbol,
                description=description,
                quantity=quantity,
                price=price,
                fees=fees,
                amount=None,
                currency="USD",
                broker="Charles Schwab",
            )
            transactions.append(txn)
            LOGGER.debug(
                "Extracted Deposit from awards file: %s %s @ $%s on %s",
                quantity,
                symbol,
                price,
                date,
            )

    return transactions


def _read_schwab_awards(
    schwab_award_transactions_file: Path | None,
) -> tuple[AwardPrices, list[BrokerTransaction]]:
    """Read initial stock prices and transactions from CSV file.

    Returns:
        Tuple of (AwardPrices for vest FMV lookup, list of transactions from awards file)
    """
    if schwab_award_transactions_file is None:
        return AwardPrices(award_prices={}), []

    headers: list[str] = []
    lines: list[list[str]] = []

    with schwab_award_transactions_file.open(encoding="utf-8") as csv_file:
        print(f"Parsing {schwab_award_transactions_file}...")
        lines = list(csv.reader(csv_file))
    if not lines:
        raise ParsingError(
            schwab_award_transactions_file, "Charles Schwab Award CSV file is empty"
        )
    headers = lines[0]

    # Detect format: new format has VestFairMarketValue, old has FairMarketValuePrice
    old_price_header = AwardsTransactionsFileRequiredHeaders.FAIR_MARKET_VALUE_PRICE
    new_price_header = AwardsTransactionsFileRequiredHeaders.VEST_FAIR_MARKET_VALUE
    new_date_header = AwardsTransactionsFileRequiredHeaders.VEST_DATE

    is_new_format = new_price_header.value in headers

    if is_new_format:
        # New format: require VestFairMarketValue and VestDate
        required_headers = {
            AwardsTransactionsFileRequiredHeaders.SYMBOL.value,
            new_price_header.value,
            new_date_header.value,
        }
    else:
        # Old format: require Date and FairMarketValuePrice
        required_headers = {
            AwardsTransactionsFileRequiredHeaders.DATE.value,
            AwardsTransactionsFileRequiredHeaders.SYMBOL.value,
            old_price_header.value,
        }

    if not required_headers.issubset(headers):
        raise ParsingError(
            schwab_award_transactions_file,
            f"Missing columns in awards file: {required_headers.difference(headers)}",
        )

    # Remove headers
    lines = lines[1:]

    if is_new_format:
        award_prices = _read_schwab_awards_new_format(lines, headers)
        # Extract actual transactions from the new format awards file
        award_transactions = _read_schwab_awards_transactions_new_format(
            lines, headers, schwab_award_transactions_file, award_prices
        )
        return award_prices, award_transactions

    return _read_schwab_awards_old_format(
        lines, headers, schwab_award_transactions_file
    ), []


class SchwabParser(BaseSingleFileParser):
    """Parser for RAW format transaction files."""

    arg_name = "schwab"
    pretty_name = "Charles Schwab"
    format_name = "CSV"
    deprecated_flags: ClassVar[list[str]] = ["--schwab"]

    awards_prices: AwardPrices = AwardPrices(award_prices={})
    awards_transactions: list[BrokerTransaction] = []

    @classmethod
    def register_arguments(cls, arg_group: argparse._ArgumentGroup) -> None:
        """Register argparse arguments for this broker."""
        arg_group.add_argument(
            "--schwab-award-file",
            type=existing_file_type,
            default=None,
            metavar="PATH",
            help="Charles Schwab Equity Awards transaction history in CSV format",
        )
        arg_group.add_argument(
            "--schwab-award",
            action=DeprecatedAction,
            dest="schwab_award_file",
            type=existing_file_type,
            help=argparse.SUPPRESS,
        )
        super().register_arguments(arg_group)

    @classmethod
    def load_from_args(cls, args: argparse.Namespace) -> list[BrokerTransaction]:
        """Load broker data from parsed arguments."""
        award_path = args.schwab_award_file
        cls.awards_prices, cls.awards_transactions = _read_schwab_awards(award_path)
        return super().load_from_args(args)

    @classmethod
    def read_transactions(
        cls, file: TextIO, file_path: Path
    ) -> list[BrokerTransaction]:
        """Read Schwab transactions from file."""

        lines = list(csv.reader(file))
        if not lines:
            raise ParsingError(
                file_path, "Charles Schwab transactions CSV file is empty"
            )
        if not cls.awards_prices:
            LOGGER.warning("No Schwab Award file provided")
        headers = lines[0]

        required_headers = set(
            {header.value for header in SchwabTransactionsFileRequiredHeaders}
        )
        if not required_headers.issubset(headers):
            raise ParsingError(
                file_path,
                "Missing columns in Schwab transaction file: "
                f"{required_headers.difference(headers)}",
            )

        # Remove header
        lines = lines[1:]

        # Derive vest prices from same-day sell transactions as a fallback
        # for when the awards file doesn't have vest data for certain dates
        derived_prices = _derive_vest_prices_from_sells(lines, headers, file_path)

        transactions: list[BrokerTransaction] = [
            SchwabTransaction.create(
                OrderedDict(zip(headers, row, strict=True)),
                file_path,
                cls.awards_prices,
                derived_prices,
            )
            for row in lines
            if any(row)
        ]
        transactions = _unify_schwab_paired_transactions(transactions, file_path)
        transactions = _filter_cancelled_buy_transactions(transactions)

        # Add transactions from the awards file (Deposits and Sales)
        # These are tracked separately from the Individual account
        if cls.awards_transactions:
            transactions.extend(cls.awards_transactions)
            LOGGER.info(
                "Added %d transactions from awards file",
                len(cls.awards_transactions),
            )

        # Sort transactions by date, ensuring acquisitions come before disposals.
        # Action order priority for same date:
        # 0: Acquisitions (Buy, Stock Activity, Reinvest Shares, etc.)
        # 1: Everything else (Sell, Cash Merger, Transfers, etc.)
        def action_sort_key(action: ActionType) -> int:
            if action in (
                ActionType.BUY,
                ActionType.STOCK_ACTIVITY,
                ActionType.REINVEST_SHARES,
                ActionType.SPIN_OFF,
                ActionType.STOCK_SPLIT,
            ):
                return 0
            return 1

        transactions.sort(
            key=lambda t: (
                t.date,
                action_sort_key(t.action),
                t.symbol or "",
            )
        )
        return list(transactions)
