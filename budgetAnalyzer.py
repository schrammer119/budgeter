#!/usr/bin/env python3
"""
Enhanced Budget Analyzer for File-based Import
==============================================

Production-ready budget analyzer with database persistence, file import handling,
and comprehensive error handling.

Author: Alexander Schramm
License: MIT
"""

from config.macros import (MERCHANT_TOKENS,
                           MERCHANT_REGEXES,
                           MERCHANT_CATEGORIES,
                           CATEGORY_TYPES)
from utils.classes import (Transaction,
                           Config,
                           LOG_FILE_PATH)
from utils.database import DatabaseManager
from utils.fileHandler import FileHandler
from collections import defaultdict
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from rapidfuzz import process, fuzz
from typing import Dict, List, Optional, Tuple, Any
import calendar
import logging
import os
import re
import smtplib
import sys

os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)
os.makedirs('config', exist_ok=True)


def setup_logger():
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    # Remove all handlers
    for h in logger.handlers[:]:
        logger.removeHandler(h)

    # Detect if running in VS Code debugger
    in_vscode = 'VSCODE_PID' in os.environ or 'PYTHONBREAKPOINT' in os.environ

    if in_vscode or sys.stdout.isatty():
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.FileHandler(LOG_FILE_PATH, encoding='utf-8')

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s',
        datefmt='%d/%m/%Y %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger


logger = setup_logger()


class BudgetAnalyzer:
    """
    Enhanced budget analyzer with database persistence and file import handling.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DatabaseManager(cfg.general.db_path)
        self.file_handler = FileHandler(
            cfg.general.input_dir, cfg.general.processed_dir)
        self.jinja_env = Environment(
            loader=FileSystemLoader(cfg.general.templates_dir))

        # Create templates directory if it doesn't exist
        Path(cfg.general.templates_dir).mkdir(exist_ok=True)

    def _get_date_range(self, target_month: Optional[date] = None) -> Tuple[date, date]:
        """Get date range for budget analysis (25th to 24th cycle)."""
        if target_month:
            # Use provided month
            base_date = target_month.replace(day=1)
        else:
            # Use current month
            base_date = date.today().replace(day=1)

        # Budget cycle runs from 25th of previous month to 24th of current month
        start_date = (base_date - relativedelta(months=1)).replace(day=25)
        end_date = base_date.replace(day=24)

        return start_date, end_date

    def _get_quarter_range(self, target_month: Optional[date] = None) -> Tuple[date, date]:
        """Get date range for quarter analysis."""
        if target_month:
            base_date = target_month.replace(day=1)
        else:
            base_date = date.today().replace(day=1)

        # Determine quarter
        quarter = ((base_date.month - 1) // 3) + 1

        # Quarter start month
        quarter_start_month = ((quarter - 1) * 3) + 1

        # Get first day of quarter
        quarter_start = base_date.replace(month=quarter_start_month, day=1)

        # Get last day of quarter
        if quarter == 4:
            quarter_end = base_date.replace(month=12, day=31)
        else:
            next_quarter_start = base_date.replace(
                month=quarter_start_month + 3, day=1)
            quarter_end = next_quarter_start - timedelta(days=1)

        return quarter_start, quarter_end

    def _get_year_range(self, target_month: Optional[date] = None) -> Tuple[date, date]:
        """Get date range for year analysis."""
        if target_month:
            base_date = target_month.replace(day=1)
        else:
            base_date = date.today().replace(day=1)

        year_start = base_date.replace(month=1, day=1)
        year_end = base_date.replace(month=12, day=31)

        return year_start, year_end

    def _normalize(self, text: str) -> str:
        """
        Lowercase, strip punctuation & numbers, collapse whitespace.
        """
        text = text.lower()
        if sum(c.isalpha() for c in text):
            # Only remove punctuation and digits if there are alphabetic characters
            # remove punctuation, except hyphens
            text = re.sub(r"[^\w\s-]", " ", text)
            # remove standalone digits
            text = re.sub(r"(?<![\w-])(?:\d+(?:-\d+)*)(?![\w-])", " ", text)
        # collapse multiple spaces
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _categorize_transactions(self, transactions: List[Transaction]) -> None:
        """Categorize transactions based on description keywords."""

        for tx in transactions:
            if tx.category:
                continue  # Already categorized
            norm = self._normalize(tx.description)

            # Rule-based early categorization
            for token, category in MERCHANT_TOKENS.items():
                token_norm = self._normalize(token)
                if len(token_norm) > len(norm):
                    continue
                if token_norm in norm:
                    tx.category = category
                    tx.merchant = token
                    break
            if tx.category:
                continue  # Already categorized

            # Regex patterns
            for pattern, category, name in MERCHANT_REGEXES:
                match = re.search(pattern, norm)
                if match:
                    tx.category = category
                    tx.merchant = name
                    break
            if tx.category:
                continue  # Already categorized

            # Fuzzy matching
            normalized_to_original = {
                self._normalize(k): k for k in MERCHANT_CATEGORIES.keys()}
            normalized_keys = [
                k for k in normalized_to_original.keys() if len(k) <= len(norm)]

            if not normalized_keys:
                if tx.amount > 0:
                    tx.category = "Uncategorized Income"
                else:
                    tx.category = "Uncategorized"
                tx.merchant = tx.description
                continue

            match_list = process.extract(
                norm, normalized_keys, scorer=fuzz.partial_ratio)
            best_norm, score, _ = max(
                match_list, key=lambda x: (x[1], len(x[0])))
            best = normalized_to_original[best_norm]
            if score >= self.cfg.general.category_threshold:
                tx.category = MERCHANT_CATEGORIES[best]
                tx.merchant = best
            else:
                if tx.amount > 0:
                    tx.category = "Uncategorized Income"
                else:
                    tx.category = "Uncategorized"
                tx.merchant = tx.description

    def recategorize_transactions(self, target_month: Optional[date] = None, report_type: str = "monthly") -> Dict[str, Any]:
        """
        Re-categorize transactions for the specified month using current rules.

        Args:
            target_month: Target month to re-categorize. If None, uses current month.

        Returns:
            Dictionary with re-categorization results
        """
        start_date, end_date = self._get_date_range(target_month)
        if report_type != "monthly":
            start_date -= timedelta(days=366)

        logger.info(
            f"Re-categorizing transactions for period: {start_date} to {end_date}")

        try:
            # Get all transactions for the period
            transactions = self.db.get_transactions(start_date, end_date)

            if not transactions:
                logger.warning(
                    f"No transactions found for period {start_date} to {end_date}")
                return {
                    "success": False,
                    "message": "No transactions found for the specified period",
                    "transactions_processed": 0,
                    "changes_made": 0
                }

            # Store original categories for comparison
            original_categories = {
                tx.transaction_id: (tx.category, tx.merchant) for tx in transactions}

            # Clear existing categorization
            for tx in transactions:
                tx.category = None
                tx.merchant = None

            # Re-categorize using current rules
            self._categorize_transactions(transactions)

            # Count changes made
            changes_made = 0
            updated_transactions = []

            for tx in transactions:
                original_cat, original_merchant = original_categories.get(
                    tx.transaction_id, (None, None))
                if tx.category != original_cat or tx.merchant != original_merchant:
                    updated_transactions.append(tx)
                    logger.debug(f"Transaction {tx.transaction_id}: {original_cat} -> {tx.category}, "
                                 f"Merchant: {original_merchant} -> {tx.merchant}")

            # Update database with new categorizations
            if updated_transactions:
                changes_made = self.db.update_transactions(
                    updated_transactions)
                logger.info(
                    f"Updated {len(updated_transactions)} transactions in database")

            return {
                "success": True,
                "transactions_processed": len(transactions),
                "changes_made": changes_made,
                "start_date": start_date,
                "end_date": end_date,
                "message": f"Re-categorized {len(transactions)} transactions, {changes_made} changes made"
            }

        except Exception as e:
            logger.error(
                f"Failed to re-categorize transactions: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "transactions_processed": 0,
                "changes_made": 0
            }

    def _get_monthly_data(self,
                          transactions: List[Transaction],
                          target_month: date,
                          days_in_month: int) -> Dict[str, Any]:
        """
        Convert a list of Transaction objects to monthly_data dictionary format.

        Args:
            transactions: List of Transaction objects
            target_month: Month number (1-12)
            target_year: Year (e.g., 2024)

        Returns:
            Dictionary in monthly_data format
        """

        # Initialize calculations
        total_income = 0.0
        total_expenses = 0.0
        category_totals = defaultdict(float)
        category_transactions = defaultdict(list)
        income_sources = defaultdict(float)

        # Process each transaction
        for tx in transactions:
            amount = tx.amount
            category = tx.category or 'Uncategorized'
            merchant = tx.merchant or tx.description

            # Positive amounts are income, negative are expenses
            if category == 'Income' or category == 'Uncategorized Income':
                total_income += amount
                # Group income by category or merchant
                income_sources[merchant] += amount

            else:
                # Convert to positive for expense tracking
                expense_amount = -amount
                total_expenses += expense_amount
                category_totals[category] += expense_amount

                # Add transaction to category breakdown
                category_transactions[category].append({
                    'merchant': merchant,
                    'date': tx.date.isoformat(),
                    'amount': expense_amount
                })

        # Calculate financial metrics
        net_cash_flow = total_income - total_expenses

        # Calculate spending by type
        discretionary_spending = 0.0
        non_discretionary_spending = 0.0
        debt_payments = 0.0
        total_savings = 0.0
        unknown_spending = 0.0

        for category, amount in category_totals.items():
            category_type = CATEGORY_TYPES.get(
                category, 'Unkown')
            if category_type == 'Discretionary':
                discretionary_spending += amount
            elif category_type == 'Non-Discretionary':
                non_discretionary_spending += amount
            elif category_type == 'Debt':
                debt_payments += amount
            elif category_type == 'Savings':
                total_savings += amount
            else:
                unknown_spending += amount

        # Calculate percentages
        discretionary_percentage = (
            discretionary_spending / total_income * 100) if total_income > 0 else 0
        non_discretionary_percentage = (
            non_discretionary_spending / total_income * 100) if total_income > 0 else 0
        debt_percentage = (debt_payments / total_income *
                           100) if total_income > 0 else 0
        savings_rate = (total_savings / total_income *
                        100) if total_income > 0 else 0
        # Only positive cash flow counts as savings
        savings_amount = total_savings

        # Build category breakdown
        category_breakdown = []
        for category, amount in category_totals.items():
            category_breakdown.append({
                'name': category,
                'amount': amount,
                'percentage_of_expenses': (amount / total_expenses * 100) if total_expenses > 0 else 0,
                'percentage_of_income': (amount / total_income * 100) if total_income > 0 else 0,
                'type': CATEGORY_TYPES.get(category, 'Discretionary'),
                'transactions': category_transactions[category]
            })

        # Sort by amount (descending)
        category_breakdown.sort(key=lambda x: x['amount'], reverse=True)

        # Build income sources list
        income_sources_list = []
        for source, amount in income_sources.items():
            percentage = (amount / total_income *
                          100) if total_income > 0 else 0
            income_sources_list.append({
                'name': source,
                'amount': amount,
                'percentage': percentage
            })

        # Sort by amount (descending)
        income_sources_list.sort(key=lambda x: x['amount'], reverse=True)

        # Prepare category labels and amounts for charts
        category_labels = [cat['name'] for cat in category_breakdown]
        category_amounts = [cat['amount'] for cat in category_breakdown]

        # Build the monthly_data dictionary
        monthly_data = {
            'month_name': target_month.strftime('%B'),
            'year': target_month.strftime('%Y'),
            'report_date': datetime.now().strftime('%Y-%m-%d'),
            'days_in_month': days_in_month,
            'total_income': round(total_income, 0),
            'total_expenses': round(total_expenses, 0),
            'net_cash_flow': round(net_cash_flow, 0),
            'savings_rate': round(savings_rate, 1),
            'savings_amount': round(savings_amount, 0),
            'discretionary_spending': round(discretionary_spending, 0),
            'discretionary_percentage': round(discretionary_percentage, 1),
            'non_discretionary_spending': round(non_discretionary_spending, 0),
            'non_discretionary_percentage': round(non_discretionary_percentage, 1),
            'debt_payments': round(debt_payments, 0),
            'debt_percentage': round(debt_percentage, 1),
            'category_breakdown': category_breakdown,
            'income_sources': income_sources_list,
            'category_labels': category_labels,
            'category_amounts': category_amounts
        }

        return monthly_data

    def _calculate_expense_volatility(self, monthly_expenses):
        """Calculate expense volatility as coefficient of variation"""
        if len(monthly_expenses) < 2:
            return 0

        mean_expense = sum(monthly_expenses) / len(monthly_expenses)
        variance = sum((x - mean_expense) **
                       2 for x in monthly_expenses) / len(monthly_expenses)
        std_dev = variance ** 0.5

        if mean_expense == 0:
            return 0

        coefficient_of_variation = (std_dev / mean_expense) * 100
        return min(coefficient_of_variation, 100)

    def _calculate_cash_flow_stability(self, monthly_cashflows, monthly_expenses):
        """Calculate cash flow stability score (0-100)"""
        if len(monthly_cashflows) < 2:
            return 100

        # Count positive cash flow months
        positive_months = sum(1 for flow in monthly_cashflows if flow > 0)
        consistency_score = (positive_months / len(monthly_cashflows)) * 100

        # Calculate volatility penalty
        volatility_penalty = self._calculate_expense_volatility(
            monthly_expenses) * 0.5

        return max(consistency_score - volatility_penalty, 0)

    def _calculate_financial_health_score(self, savings_rate: float, debt_ratio: float, expense_volatility: float, cash_flow_stability: float) -> int:
        """
        Calculate a composite financial health score (0-100)

        Factors:
        - Savings rate (40% weight)
        - Debt ratio (30% weight)
        - Expense volatility (15% weight)
        - Cash flow stability (15% weight)
        """
        savings_score = min(
            savings_rate * 5, 100)  # 20% savings rate = 100 points
        # 30% debt ratio = 0 points
        debt_score = max(100 - (debt_ratio * 3.33), 0)
        # Lower volatility = higher score
        volatility_score = max(100 - expense_volatility, 0)
        stability_score = cash_flow_stability  # 0-100 scale

        weighted_score = (
            savings_score * 0.4 +
            debt_score * 0.3 +
            volatility_score * 0.15 +
            stability_score * 0.15
        )

        return int(weighted_score)

    def _get_quarterly_data(self, target_month: Optional[date] = None) -> Dict[str, Any]:
        """
        Get comprehensive quarterly data including current quarter and previous year comparison.

        Args:
            target_month: Target month for the quarter

        Returns:
            Dictionary with quarterly analysis data
        """
        if not target_month:
            target_month = date.today()

        # Calculate quarter info
        quarter_num = ((target_month.month - 1) // 3) + 1
        # Calculate quarter months
        quarter_months = [
            ['January', 'February', 'March'],
            ['April', 'May', 'June'],
            ['July', 'August', 'September'],
            ['October', 'November', 'December']
        ][quarter_num - 1]

        # Get current quarter range
        current_quarter_start, current_quarter_end = self._get_quarter_range(
            target_month)

        # Get previous year same quarter
        prev_year_target = target_month - relativedelta(years=1)
        prev_quarter_start, prev_quarter_end = self._get_quarter_range(
            prev_year_target)

        # Get transactions for both periods
        current_quarter_transactions = self.db.get_transactions(
            current_quarter_start, current_quarter_end)
        prev_quarter_transactions = self.db.get_transactions(
            prev_quarter_start, prev_quarter_end)

        # quarterly totals
        quarterly_income = 0.0
        quarterly_expenses = 0.0
        monthly_income = [0.0, 0.0, 0.0]
        monthly_expenses = [0.0, 0.0, 0.0]
        monthly_savings = [0.0, 0.0, 0.0]
        monthly_discretionary = [0.0, 0.0, 0.0]
        monthly_non_discretionary = [0.0, 0.0, 0.0]
        monthly_debt = [0.0, 0.0, 0.0]
        category_totals = defaultdict(float)
        monthly_categories = [defaultdict(
            float), defaultdict(float), defaultdict(float)]

        for tx in current_quarter_transactions:
            amount = tx.amount
            category = tx.category or 'Uncategorized'

            # Positive amounts are income, negative are expenses
            if category == 'Income' or category == 'Uncategorized Income':
                quarterly_income += amount
                monthly_income[tx.date.month -
                               current_quarter_start.month] += amount

            else:
                # Convert to positive for expense tracking
                expense_amount = -amount
                quarterly_expenses += expense_amount
                monthly_expenses[tx.date.month -
                                 current_quarter_start.month] += expense_amount

                category_type = CATEGORY_TYPES.get(category, 'Unkown')
                if category_type == 'Discretionary':
                    monthly_discretionary[tx.date.month -
                                          current_quarter_start.month] += expense_amount
                elif category_type == 'Non-Discretionary':
                    monthly_non_discretionary[tx.date.month -
                                              current_quarter_start.month] += expense_amount
                elif category_type == 'Debt':
                    monthly_debt[tx.date.month -
                                 current_quarter_start.month] += expense_amount
                elif category_type == 'Savings':
                    monthly_savings[tx.date.month -
                                    current_quarter_start.month] += expense_amount

                category_totals[category] += expense_amount
                monthly_categories[tx.date.month -
                                   current_quarter_start.month][category] += expense_amount

        # Calculate financial metrics
        quarterly_net_flow = quarterly_income - quarterly_expenses
        monthly_cashflows = [i - e for i,
                             e in zip(monthly_income, monthly_expenses)]

        # Calculate spending by type
        quarterly_discretionary = 0.0
        quarterly_non_discretionary = 0.0
        quarterly_debt = 0.0
        quarterly_savings = 0.0
        q_unknown_spending = 0.0

        for category, amount in category_totals.items():
            category_type = CATEGORY_TYPES.get(
                category, 'Unkown')
            if category_type == 'Discretionary':
                quarterly_discretionary += amount
            elif category_type == 'Non-Discretionary':
                quarterly_non_discretionary += amount
            elif category_type == 'Debt':
                quarterly_debt += amount
            elif category_type == 'Savings':
                quarterly_savings += amount
            else:
                q_unknown_spending += amount

        quarterly_net_flow = quarterly_income - quarterly_expenses
        quarterly_discretionary_pct = (
            quarterly_discretionary / quarterly_income * 100) if quarterly_income > 0 else 0
        quarterly_non_discretionary_pct = (
            quarterly_non_discretionary / quarterly_income * 100) if quarterly_income > 0 else 0
        quarterly_debt_pct = (quarterly_debt / quarterly_income *
                              100) if quarterly_income > 0 else 0
        quarterly_savings_pct = (quarterly_savings / quarterly_income *
                                 100) if quarterly_income > 0 else 0

        # quarterly totals
        prev_quarterly_income = 0.0
        prev_quarterly_expenses = 0.0
        prev_monthly_income = [0.0, 0.0, 0.0]
        prev_monthly_expenses = [0.0, 0.0, 0.0]
        prev_category_totals = defaultdict(float)

        for tx in prev_quarter_transactions:
            amount = tx.amount
            category = tx.category or 'Uncategorized'

            # Positive amounts are income, negative are expenses
            if category == 'Income' or category == 'Uncategorized Income':
                prev_quarterly_income += amount
                prev_monthly_income[tx.date.month -
                                    prev_quarter_start.month] += amount

            else:
                # Convert to positive for expense tracking
                expense_amount = -amount
                prev_quarterly_expenses += expense_amount
                prev_monthly_expenses[tx.date.month -
                                      prev_quarter_start.month] += expense_amount
                prev_category_totals[category] += expense_amount

        # Calculate financial metrics
        prev_quarterly_net_flow = prev_quarterly_income - prev_quarterly_expenses
        prev_monthly_cashflows = [i - e for i,
                                  e in zip(monthly_income, monthly_expenses)]

        # Calculate spending by type
        prev_quarterly_discretionary = 0.0
        prev_quarterly_non_discretionary = 0.0
        prev_quarterly_debt = 0.0
        prev_quarterly_savings = 0.0
        prev_q_unknown_spending = 0.0

        for category, amount in prev_category_totals.items():
            category_type = CATEGORY_TYPES.get(
                category, 'Unkown')
            if category_type == 'Discretionary':
                prev_quarterly_discretionary += amount
            elif category_type == 'Non-Discretionary':
                prev_quarterly_non_discretionary += amount
            elif category_type == 'Debt':
                prev_quarterly_debt += amount
            elif category_type == 'Savings':
                prev_quarterly_savings += amount
            else:
                prev_q_unknown_spending += amount

        prev_quarterly_net_flow = prev_quarterly_income - prev_quarterly_expenses
        prev_quarterly_discretionary_pct = (
            prev_quarterly_discretionary / prev_quarterly_income * 100) if prev_quarterly_income > 0 else 0
        prev_quarterly_non_discretionary_pct = (
            prev_quarterly_non_discretionary / prev_quarterly_income * 100) if prev_quarterly_income > 0 else 0
        prev_quarterly_debt_pct = (prev_quarterly_debt / prev_quarterly_income *
                                   100) if prev_quarterly_income > 0 else 0
        prev_quarterly_savings_pct = (prev_quarterly_savings / prev_quarterly_income *
                                      100) if prev_quarterly_income > 0 else 0

        # YoY analysis
        yoy_income_change = (quarterly_income / prev_quarterly_income *
                             100 - 100) if prev_quarterly_income > 0 else 0
        yoy_expenses_change = (quarterly_expenses / prev_quarterly_expenses *
                               100 - 100) if prev_quarterly_expenses > 0 else 0
        yoy_net_flow_change = (quarterly_net_flow / prev_quarterly_net_flow *
                               100 - 100) if prev_quarterly_net_flow > 0 else 0
        yoy_discretionary_change = quarterly_discretionary_pct - \
            prev_quarterly_discretionary_pct
        yoy_non_discretionary_change = quarterly_non_discretionary_pct - \
            prev_quarterly_non_discretionary_pct
        yoy_debt_change = quarterly_debt_pct - prev_quarterly_debt_pct
        yoy_savings_change = quarterly_savings_pct - prev_quarterly_savings_pct

        # Health status

        financial_health_score = self._calculate_financial_health_score(
            savings_rate=quarterly_savings_pct,
            debt_ratio=quarterly_debt_pct,
            expense_volatility=self._calculate_expense_volatility(
                monthly_expenses),
            cash_flow_stability=self._calculate_cash_flow_stability(
                monthly_cashflows, monthly_expenses)
        )

        prev_financial_health_score = self._calculate_financial_health_score(
            savings_rate=prev_quarterly_savings_pct,
            debt_ratio=prev_quarterly_debt_pct,
            expense_volatility=self._calculate_expense_volatility(
                prev_monthly_expenses),
            cash_flow_stability=self._calculate_cash_flow_stability(
                prev_monthly_cashflows, prev_monthly_expenses)
        )

        financial_health_change = financial_health_score - prev_financial_health_score

        # Category YoY changes
        category_changes = []
        for category, current_amount in category_totals.items():
            previous_amount = prev_category_totals.get(category, 0)
            if previous_amount > 0:
                change_percent = (
                    (current_amount - previous_amount) / previous_amount) * 100
                category_changes.append({
                    'category': category,
                    'change_percent': change_percent,
                    'current_amount': current_amount,
                    'previous_amount': previous_amount
                })

        # Sort by absolute change and return top 10
        category_changes .sort(key=lambda x: abs(
            x['change_percent']), reverse=True)
        category_changes = category_changes[:10]

        # category volatility
        category_volatility = {}

        # Group expenses by category across months
        for month in monthly_categories:
            for category, amount in month.items():
                if category not in category_volatility:
                    category_volatility[category] = []
                category_volatility[category].append(amount)

        # Calculate volatility score for each category
        volatility_analysis = []
        for category, amounts in category_volatility.items():
            if len(amounts) < 2:
                volatility_score = 0
            else:
                mean_amount = sum(amounts) / len(amounts)
                if mean_amount == 0:
                    volatility_score = 0
                else:
                    variance = sum((x - mean_amount) **
                                   2 for x in amounts) / len(amounts)
                    std_dev = variance ** 0.5
                    coefficient_of_variation = (std_dev / mean_amount) * 100
                    volatility_score = min(coefficient_of_variation, 100)

            volatility_analysis.append({
                'category': category,
                'volatility_score': volatility_score,
                'mean_amount': mean_amount
            })

        # Sort by volatility score and return top 10
        volatility_analysis.sort(
            key=lambda x: x['volatility_score'], reverse=True)
        volatility_analysis = volatility_analysis[:10]

        # qoq
        current_quarter = {
            'income': quarterly_income,
            'expenses': quarterly_expenses,
            'net_flow': quarterly_net_flow,
            'discretionary': quarterly_discretionary,
            'discretionary': quarterly_non_discretionary,
            'savings': quarterly_debt,
            'savings': quarterly_savings,
        }

        previous_quarter = {
            'income': prev_quarterly_income,
            'expenses': prev_quarterly_expenses,
            'net_flow': prev_quarterly_net_flow,
            'discretionary': prev_quarterly_discretionary,
            'discretionary': prev_quarterly_non_discretionary,
            'savings': prev_quarterly_debt,
            'savings': prev_quarterly_savings,
        }

        qoq_comparison = []
        for metric, current_value in current_quarter.items():
            previous_value = previous_quarter.get(metric, 0)
            change = current_value - previous_value
            percentage_change = (
                current_value / previous_value * 100 - 100) if previous_value > 0 else 0

            qoq_comparison.append({
                'name': metric.replace('_', ' ').title(),
                'current': current_value,
                'previous': previous_value,
                'change': change,
                'percentage_change': percentage_change
            })

        # achievements
        achievements = []

        if quarterly_savings_pct >= 20:
            achievements.append(
                f"Maintained excellent savings rate of {quarterly_savings_pct:.1f}%")
        elif quarterly_savings_pct > prev_quarterly_savings_pct:
            achievements.append(
                f"Improved savings rate by {quarterly_savings_pct - prev_quarterly_savings_pct:.1f} percentage points")

        # Check for consistent positive cash flow
        positive_months = sum(1 for x in monthly_cashflows if x > 0)
        if positive_months == 3:
            achievements.append("Achieved positive cash flow in all months")
        elif positive_months > 1:
            achievements.append(
                f"Maintained positive cash flow in {positive_months} out of 3 months")

        # Check for reduced discretionary spending
        if quarterly_discretionary < prev_quarterly_discretionary:
            reduction = ((prev_quarterly_discretionary -
                         quarterly_discretionary) / prev_quarterly_discretionary) * 100
            achievements.append(
                f"Reduced discretionary spending by {reduction:.1f}% compared to last year")

        # Check for debt reduction
        if quarterly_debt > prev_quarterly_debt:
            increase = ((quarterly_debt - prev_quarterly_debt) /
                        prev_quarterly_debt) * 100 if prev_quarterly_debt > 0 else 0
            achievements.append(
                f"Increased debt payments by {increase:.1f}% compared to last year")

        quarterly_achievements = achievements if achievements else [
            "Completed financial tracking for the quarter"]

        # concerns
        quarterly_concerns = []

        if quarterly_savings_pct < 10:
            quarterly_concerns.append(
                f"Savings rate of {quarterly_savings_pct:.1f}% is below recommended 20%")

        if financial_health_score < 60:
            quarterly_concerns.append(
                f"Financial health score of {financial_health_score}/100 needs improvement")

        # Check for high expense volatility
        volatility = self._calculate_expense_volatility(monthly_expenses)
        if volatility > 20:
            quarterly_concerns.append(
                f"High expense volatility ({volatility:.1f}%) indicates inconsistent spending")

        # Check for increasing debt burden
        if quarterly_debt_pct > 30:
            quarterly_concerns.append(
                f"Debt burden of {quarterly_debt_pct:.1f}% exceeds recommended 30%")

        # Check for negative cash flow months
        negative_months = sum(1 for x in monthly_cashflows if x < 0)
        if negative_months == 3:
            quarterly_concerns.append(
                "Experienced negative cash flow in all months")
        elif negative_months > 1:
            quarterly_concerns.append(
                f"Experienced negative cash flow in {negative_months} out of 3 months")

        # recommendations
        recommendations = []

        # Recommendations based on category changes
        if category_changes:
            top_increase = max(
                category_changes, key=lambda x: x['change_percent'])
            if top_increase['change_percent'] > 20:
                recommendations.append(
                    f"Consider reviewing {top_increase['category']} spending - increased by {top_increase['change_percent']:.1f}%")

        # Recommendations based on volatility
        if volatility_analysis:
            most_volatile = volatility_analysis[0]
            if most_volatile['volatility_score'] > 30:
                recommendations.append(
                    f"Create a budget for {most_volatile['category']} to reduce spending volatility")

        # Savings rate recommendations
        if quarterly_savings_pct < 20:
            recommendations.append(
                "Consider increasing automating savings transfers to improve consistency")
            recommendations.append(
                "Review discretionary spending categories for potential cuts")

        # Cash flow recommendations
        if negative_months > 0:
            recommendations.append(
                "Build an emergency fund to handle irregular expenses")
            recommendations.append(
                "Consider income diversification or expense reduction strategies")

        quarterly_recommendations = recommendations if recommendations else [
            "Continue monitoring spending patterns and maintain current financial discipline"]

        # projections
        if quarter_num == 1:
            projected_annual_income = quarterly_income * 4
        elif quarter_num == 2:
            projected_annual_income = quarterly_income * 2  # Based on H1 performance
        elif quarter_num == 3:
            projected_annual_income = quarterly_income * 4/3  # Based on 9-month performance
        else:
            projected_annual_income = quarterly_income

        seasonal_multipliers = {
            1: 1.0,  # Q1 baseline
            2: 1.0,  # Q2 similar to Q1
            3: 1.0,  # Q3 similar
            4: 1.1   # Q4 higher due to holidays
        }

        base_projection = quarterly_expenses * 4
        seasonal_adjustment = seasonal_multipliers.get(quarter_num, 1.0)

        projected_annual_expenses = base_projection * seasonal_adjustment

        return {
            # Basic info
            'quarter': quarter_num,
            'year': target_month.year,
            'quarter_months': quarter_months,

            # Quarterly totals
            'quarterly_income': quarterly_income,
            'quarterly_expenses': quarterly_expenses,
            'quarterly_net_flow': quarterly_net_flow,
            'quarterly_discretionary': quarterly_discretionary,
            'quarterly_discretionary_pct': quarterly_discretionary_pct,
            'quarterly_non_discretionary': quarterly_non_discretionary,
            'quarterly_non_discretionary_pct': quarterly_non_discretionary_pct,
            'quarterly_debt': quarterly_debt,
            'quarterly_debt_pct': quarterly_debt_pct,
            'quarterly_savings': quarterly_savings,
            'quarterly_savings_pct': quarterly_savings_pct,

            # Year-over-year changes
            'yoy_income_change': yoy_income_change,
            'yoy_expenses_change': yoy_expenses_change,
            'yoy_net_flow_change': yoy_net_flow_change,
            'yoy_discretionary_change': yoy_discretionary_change,
            'yoy_non_discretionary_change': yoy_non_discretionary_change,
            'yoy_debt_change': yoy_debt_change,
            'yoy_savings_change': yoy_savings_change,

            # Financial health
            'financial_health_score': financial_health_score,
            'financial_health_change': financial_health_change,

            # Chart data
            'monthly_income_data': monthly_income,
            'monthly_expenses_data': monthly_expenses,
            'monthly_cashflow_data': monthly_cashflows,
            'monthly_discretionary_data': monthly_discretionary,
            'monthly_non_discretionary_data': monthly_non_discretionary,
            'monthly_debt_data': monthly_debt,
            'monthly_savings_data': monthly_savings,
            'yoy_previous_data': [
                prev_quarterly_income,
                prev_quarterly_expenses,
                prev_quarterly_net_flow,
                prev_quarterly_discretionary,
                prev_quarterly_non_discretionary,
                prev_quarterly_debt,
                prev_quarterly_savings,
            ],
            'yoy_current_data': [
                quarterly_income,
                quarterly_expenses,
                quarterly_net_flow,
                quarterly_discretionary,
                quarterly_non_discretionary,
                quarterly_debt,
                quarterly_savings,
            ],

            # Category analysis
            'category_change_labels': [item['category'] for item in category_changes],
            'category_change_data': [item['change_percent'] for item in category_changes],
            'volatility_labels': [item['category'] for item in volatility_analysis],
            'volatility_data': [item['volatility_score'] for item in volatility_analysis],

            # Comparisons
            'qoq_comparison': qoq_comparison,

            # Insights
            'quarterly_achievements': quarterly_achievements,
            'quarterly_concerns': quarterly_concerns,
            'quarterly_recommendations': quarterly_recommendations,

            # Projections
            'projected_annual_income': projected_annual_income,
            'projected_annual_expenses': projected_annual_expenses,
        }

    def _get_yearly_data(self, target_month: Optional[date] = None) -> Dict[str, Any]:
        """
        Get comprehensive yearly data with trends and analysis.

        Args:
            target_month: Target month for the year

        Returns:
            Dictionary with yearly analysis data
        """
        if not target_month:
            target_month = date.today()

        # Get current year range
        year_start, year_end = self._get_year_range(target_month)

        # Get previous year for comparison
        prev_year_start = year_start - relativedelta(years=1)
        prev_year_end = year_end - relativedelta(years=1)

        # Get transactions for both years
        current_year_transactions = self.db.get_transactions(
            year_start, year_end)
        prev_year_transactions = self.db.get_transactions(
            prev_year_start, prev_year_end)

        # Calculate current year metrics
        current_year_total = sum(tx.amount for tx in current_year_transactions)
        current_year_categories = {}
        monthly_breakdown = {}
        quarterly_breakdown = {}

        for tx in current_year_transactions:
            # Category totals
            category = tx.category or "Uncategorized"
            current_year_categories[category] = current_year_categories.get(
                category, 0) + tx.amount

            # Monthly breakdown
            month_key = tx.date.strftime("%Y-%m")
            if month_key not in monthly_breakdown:
                monthly_breakdown[month_key] = {
                    "total": 0, "categories": {}, "count": 0}
            monthly_breakdown[month_key]["total"] += tx.amount
            monthly_breakdown[month_key]["count"] += 1
            monthly_breakdown[month_key]["categories"][category] = \
                monthly_breakdown[month_key]["categories"].get(
                    category, 0) + tx.amount

            # Quarterly breakdown
            quarter = ((tx.date.month - 1) // 3) + 1
            quarter_key = f"Q{quarter}"
            if quarter_key not in quarterly_breakdown:
                quarterly_breakdown[quarter_key] = {
                    "total": 0, "categories": {}, "count": 0}
            quarterly_breakdown[quarter_key]["total"] += tx.amount
            quarterly_breakdown[quarter_key]["count"] += 1
            quarterly_breakdown[quarter_key]["categories"][category] = \
                quarterly_breakdown[quarter_key]["categories"].get(
                    category, 0) + tx.amount

        # Calculate previous year metrics
        prev_year_total = sum(tx.amount for tx in prev_year_transactions)
        prev_year_categories = {}

        for tx in prev_year_transactions:
            category = tx.category or "Uncategorized"
            prev_year_categories[category] = prev_year_categories.get(
                category, 0) + tx.amount

        # Calculate trends and analysis
        months_with_data = len(monthly_breakdown)
        monthly_average = current_year_total / max(months_with_data, 1)

        # Find highest and lowest spending months
        highest_month = max(monthly_breakdown.items(
        ), key=lambda x: x[1]["total"]) if monthly_breakdown else None
        lowest_month = min(monthly_breakdown.items(
        ), key=lambda x: x[1]["total"]) if monthly_breakdown else None

        # Year-over-year comparison
        yoy_change = current_year_total - prev_year_total
        yoy_change_pct = (yoy_change / abs(prev_year_total)) * \
            100 if prev_year_total != 0 else 0

        # Category analysis
        category_analysis = {}
        all_categories = set(current_year_categories.keys()) | set(
            prev_year_categories.keys())

        for category in all_categories:
            current_amount = current_year_categories.get(category, 0)
            prev_amount = prev_year_categories.get(category, 0)
            change = current_amount - prev_amount
            change_pct = (change / abs(prev_amount)) * \
                100 if prev_amount != 0 else 0

            # Calculate percentage of total spending
            current_pct_of_total = (
                current_amount / abs(current_year_total)) * 100 if current_year_total != 0 else 0
            prev_pct_of_total = (
                prev_amount / abs(prev_year_total)) * 100 if prev_year_total != 0 else 0

            category_analysis[category] = {
                "current": current_amount,
                "previous": prev_amount,
                "change": change,
                "change_pct": change_pct,
                "current_pct_of_total": current_pct_of_total,
                "prev_pct_of_total": prev_pct_of_total
            }

        # Calculate spending trends (month-over-month)
        sorted_months = sorted(monthly_breakdown.keys())
        spending_trend = []
        for i in range(1, len(sorted_months)):
            current_month_total = monthly_breakdown[sorted_months[i]]["total"]
            prev_month_total = monthly_breakdown[sorted_months[i-1]]["total"]
            mom_change = current_month_total - prev_month_total
            mom_change_pct = (mom_change / abs(prev_month_total)
                              ) * 100 if prev_month_total != 0 else 0

            spending_trend.append({
                "month": sorted_months[i],
                "total": current_month_total,
                "change": mom_change,
                "change_pct": mom_change_pct
            })

        return {
            "year": target_month.year,
            "year_start": year_start,
            "year_end": year_end,
            "current_year": {
                "total": current_year_total,
                "transactions_count": len(current_year_transactions),
                "categories": current_year_categories,
                "monthly_breakdown": monthly_breakdown,
                "quarterly_breakdown": quarterly_breakdown,
                "monthly_average": monthly_average,
                "months_with_data": months_with_data,
                "highest_month": highest_month,
                "lowest_month": lowest_month
            },
            "previous_year": {
                "total": prev_year_total,
                "transactions_count": len(prev_year_transactions),
                "categories": prev_year_categories,
                "year": prev_year_start.year
            },
            "year_over_year": {
                "change": yoy_change,
                "change_pct": yoy_change_pct,
                "category_analysis": category_analysis
            },
            "trends": {
                "spending_trend": spending_trend,
                "quarterly_comparison": quarterly_breakdown
            }
        }

    def process_new_files(self) -> List[Transaction]:
        """
        Process any new files in the input directory.

        Returns:
            List of transactions from new files.
        """
        logger.info("Processing new files from input directory")

        try:
            new_transactions = self.file_handler.process_new_files()

            if new_transactions:
                # Categorize transactions
                self._categorize_transactions(new_transactions)

                # Save to database
                saved_count = self.db.save_transactions(new_transactions)
                logger.info(
                    f"Saved {saved_count} new transactions to database")

            return new_transactions

        except Exception as e:
            logger.error(f"Failed to process new files: {e}", exc_info=True)
            return []

    def run_budget(self, target_month: Optional[date] = None, force_file_import: bool = False) -> List[Transaction]:
        """
        Run budget analysis for a specific month.

        Args:
            target_month: Optional target month. If None, uses current month.
            force_file_import: If True, always process files even if data exists in database.

        Returns:
            List of transactions for the period.
        """
        start_date, end_date = self._get_date_range(target_month)

        logger.info(
            f"Running budget analysis for period: {start_date} to {end_date}")

        # Process new files first
        if force_file_import or target_month is None:
            new_transactions = self.process_new_files()
            if new_transactions:
                logger.info(
                    f"Imported {len(new_transactions)} new transactions from files")

        # Get transactions from database for the specified period
        transactions = self.db.get_transactions(start_date, end_date)

        if not transactions:
            logger.warning(
                f"No transactions found for period {start_date} to {end_date}")
            logger.info(
                "Check if files have been placed in the input directory and contain data for this period")

            # Show file summary for debugging
            file_summary = self.file_handler.get_file_summary()
            logger.info(f"File summary: {file_summary}")

        return transactions

    def generate_report(
        self,
        transactions: List[Transaction],
        report_type: str = "monthly",
        target_month: Optional[date] = None
    ) -> str | None:
        """
        Generate HTML report based on transactions and report type.

        Args:
            transactions: List of transactions to include in report
            report_type: Type of report ("monthly", "quarterly", "yearly")
            target_month: Target month for the report

        Returns:
            HTML report as string
        """
        start_date, end_date = self._get_date_range(target_month)

        template_data = self._get_monthly_data(
            transactions, end_date, (end_date - start_date).days)
        template_data["quarterly_data"] = False
        template_data["yearly_data"] = False

        # Add report-specific data
        if report_type == "quarterly":
            quarterly_data = self._get_quarterly_data(target_month)
            template_data["quarterly_data"] = True
            template_data.update(quarterly_data)
        elif report_type == "yearly":
            quarterly_data = self._get_quarterly_data(target_month)
            template_data["quarterly_data"] = True
            template_data.update(quarterly_data)
            yearly_data = self._get_yearly_data(target_month)
            template_data["yearly_data"] = True
            template_data.update(yearly_data)

        # Render template
        template_name = f"report_monthly.html"
        try:
            template = self.jinja_env.get_template(template_name)
            return template.render(**template_data)
        except Exception as e:
            logger.error(
                f"Failed to render template {template_name}: {e}", exc_info=True)
            return None

    def send_email_report(self, html_content: str, subject: str) -> bool:
        """Send email report with error handling."""
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.cfg.credentials.email_from
            msg["To"] = self.cfg.credentials.email_to

            html_part = MIMEText(html_content, "html", "utf-8")
            msg.attach(html_part)

            with smtplib.SMTP(self.cfg.general.smtp_server, self.cfg.general.smtp_port) as server:
                server.starttls()
                server.login(self.cfg.credentials.smtp_user,
                             self.cfg.credentials.smtp_password)
                server.send_message(msg)

            logger.info("Email report sent successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to send email report: {e}", exc_info=True)
            return False

    def run_full_analysis(
        self,
        target_month: Optional[date] = None,
        report_type: str = "monthly",
        send_email: bool = True,
        force_file_import: bool = False
    ) -> Dict[str, Any]:
        """
        Run complete budget analysis with report generation and email sending.

        Args:
            target_month: Target month for analysis
            report_type: Type of report to generate
            send_email: Whether to send email report
            force_file_import: Whether to force processing of files

        Returns:
            Dictionary with analysis results
        """
        try:
            # Run budget analysis
            transactions = self.run_budget(target_month, force_file_import)

            # Generate report
            html_report = self.generate_report(
                transactions, report_type, target_month)

            # Prepare results
            results = {
                "success": True,
                "transactions_count": len(transactions),
                "report_type": report_type,
                "html_report": html_report,
                "email_sent": False
            }

            if html_report:
                # Save report to file
                fname = f"data/{target_month.strftime('%Y_%m')}_{report_type}_report.html"
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(html_report)
                logger.info(f"Report saved to {fname}")

                # Send email if requested
                if send_email:
                    start_date, end_date = self._get_date_range(target_month)
                    subject = f"Budget Report ({report_type.title()}) - {start_date} to {end_date}"
                    results["email_sent"] = self.send_email_report(
                        html_report, subject)

            return results

        except Exception as e:
            logger.error(f"Full analysis failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "transactions_count": 0,
                "report_type": report_type,
                "html_report": None,
                "email_sent": False
            }


def main():
    """Main entry point for the budget analyzer."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Enhanced Budget Analyzer with File Import")
    parser.add_argument("--report-type", choices=["monthly", "quarterly", "yearly", None],
                        default=None, help="Type of report to generate")
    parser.add_argument("--no-email", action="store_true",
                        help="Don't send email report")
    parser.add_argument(
        "--target-month", help="Target month in YYYY-MM format")
    parser.add_argument("--force-import", action="store_true",
                        help="Force processing of files even if data exists")
    parser.add_argument("--recategorize", action="store_true",
                        help="Re-categorize transactions for the target month")

    args = parser.parse_args()

    try:
        # Load configuration
        cfg = Config.load()

        # Update logging configuration
        match cfg.general.log_level:
            case "CRITICAL" | "FATAL":
                logger.setLevel(logging.CRITICAL)
            case "ERROR":
                logger.setLevel(logging.ERROR)
            case "WARNING" | "WARN":
                logger.setLevel(logging.WARNING)
            case "INFO":
                logger.setLevel(logging.INFO)
            case "DEBUG":
                logger.setLevel(logging.DEBUG)
            case "NOTSET":
                logger.setLevel(logging.NOTSET)

        if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
            # If file handler exists, update its path
            for handler in logger.handlers:
                if isinstance(handler, logging.FileHandler):
                    handler.baseFilename = cfg.general.log_file

        # Initialize analyzer
        analyzer = BudgetAnalyzer(cfg)

        # Parse target month if provided
        target_month = None
        if args.target_month:
            try:
                target_month = datetime.strptime(
                    args.target_month, "%Y-%m").date()
            except ValueError:
                logger.error("Invalid target month format. Use YYYY-MM")
                return

        # report type
        if args.report_type:
            report_type = args.report_type
        else:
            report_type = ['0_indexed',
                           'monthly',
                           'monthly',
                           'quarterly',
                           'monthly',
                           'monthly',
                           'quarterly',
                           'monthly',
                           'monthly',
                           'quarterly',
                           'monthly',
                           'monthly',
                           'yearly'][target_month.month]

        # Handle re-categorization request
        if args.recategorize:
            logger.info("Re-categorizing transactions...")
            recategorize_results = analyzer.recategorize_transactions(
                target_month, report_type)

            if recategorize_results["success"]:
                logger.info(recategorize_results["message"])
                print(f"Re-categorization completed:")
                print(
                    f"  - Transactions processed: {recategorize_results['transactions_processed']}")
                print(
                    f"  - Changes made: {recategorize_results['changes_made']}")
                print(
                    f"  - Period: {recategorize_results['start_date']} to {recategorize_results['end_date']}")
            else:
                logger.error(
                    f"Re-categorization failed: {recategorize_results.get('error', 'Unknown error')}")
                print(
                    f"Re-categorization failed: {recategorize_results.get('message', 'Unknown error')}")

            # Exit after re-categorization unless user wants to continue with report
            # if not input("Continue with report generation? (y/N): ").lower().startswith('y'):
            #     return

        # Run analysis
        results = analyzer.run_full_analysis(
            target_month=target_month,
            report_type=report_type,
            send_email=not args.no_email,
            force_file_import=args.force_import
        )

        if results["success"]:
            logger.info(
                f"Analysis completed successfully: {results['transactions_count']} transactions processed")
            if results["email_sent"]:
                logger.info("Email report sent")
        else:
            logger.error(f"Analysis failed: {results['error']}")

    except Exception as e:
        logger.error(f"Application failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
