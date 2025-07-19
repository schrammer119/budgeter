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
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from rapidfuzz import process, fuzz
from scipy.interpolate import make_interp_spline
from typing import Dict, List, Optional, Tuple, Any
import logging
import matplotlib.pyplot as plt
import minify_html
import numpy as np
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

# Set up professional plot styling
plt.style.use('seaborn-v0_8-whitegrid')
plt.rcParams['font.family'] = 'Arial'
plt.rcParams['font.size'] = 10
plt.rcParams['axes.labelsize'] = 11
plt.rcParams['axes.titlesize'] = 14
plt.rcParams['legend.fontsize'] = 9
plt.rcParams['xtick.labelsize'] = 9
plt.rcParams['ytick.labelsize'] = 9
plt.rcParams['lines.linewidth'] = 2.5
plt.rcParams['lines.markersize'] = 6
plt.rcParams['axes.spines.top'] = False
plt.rcParams['axes.spines.right'] = False


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

    def _get_date_range(self, target_month: date) -> Tuple[date, date]:
        """Get date range for budget analysis (25th to 24th cycle)."""
        end_date = target_month.replace(day=24)

        # Budget cycle runs from 25th of previous month to 24th of current month
        start_date = (end_date - relativedelta(months=1)).replace(day=25)

        return start_date, end_date

    def _get_quarter_range(self, target_month: date) -> Tuple[date, date]:
        """Get date range for quarter analysis."""
        base_date = target_month.replace(day=1)

        # Determine quarter
        quarter = ((base_date.month - 1) // 3) + 1

        # Quarter start month
        quarter_start_month = ((quarter - 1) * 3) + 1

        # Get first day of quarter
        quarter_start = (base_date.replace(
            month=quarter_start_month, day=1) - timedelta(days=2)).replace(day=25)
        quarter_end = (quarter_start + relativedelta(months=3)).replace(day=24)

        return quarter_start, quarter_end

    def _get_year_range(self, target_month: date) -> Tuple[date, date]:
        """Get date range for year analysis."""
        year_end = target_month.replace(day=24)

        year_start = (year_end - relativedelta(years=1)).replace(day=25)

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

    def _get_month_index(self, date: date, start_date: date) -> int:

        month = date.month
        if date.day > 24:
            month = (month + 1) % 12

        index = (month - (start_date.month + 1)) % 12

        return index

    def create_spending_type_chart(self, monthly_labels, monthly_discretionary_pct, monthly_non_discretionary_pct,
                                   monthly_savings_pct, monthly_debt_pct):
        """
        Create line chart for spending type as percentage of income

        Parameters:
        - monthly_labels: list of month labels (strings or datetime objects)
        - monthly_discretionary_pct: list of discretionary spending percentages
        - monthly_non_discretionary_pct: list of non-discretionary spending percentages
        - monthly_savings_pct: list of savings percentages
        - monthly_debt_pct: list of debt payment percentages
        """

        # Create figure
        fig, ax = plt.subplots(1, 1, figsize=(10, 6))
        fig.patch.set_facecolor('white')

        # Professional colors for financial reports
        colors = ['#3498db', '#e74c3c', '#27ae60', '#f39c12']
        labels = ['Discretionary', 'Non-Discretionary',
                  'Savings', 'Debt Payment']
        data = [monthly_discretionary_pct, monthly_non_discretionary_pct,
                monthly_savings_pct, monthly_debt_pct]

        # Plot lines for each spending type with smooth curves
        for i, (spending_data, label, color) in enumerate(zip(data, labels, colors)):
            # Create numeric x values for interpolation
            x_numeric = np.arange(len(monthly_labels))
            y_values = np.array(spending_data)

            # Create smooth curve using spline interpolation
            if len(x_numeric) > 2:  # Need at least 3 points for spline
                x_smooth = np.linspace(x_numeric.min(), x_numeric.max(), 300)
                spline = make_interp_spline(
                    x_numeric, y_values, k=3)
                y_smooth = spline(x_smooth)

                # Map smooth x values back to labels for plotting
                x_smooth_labels = np.interp(x_smooth, x_numeric, x_numeric)
                ax.plot(x_smooth_labels, y_smooth, color=color, linewidth=2.8,
                        solid_capstyle='round', solid_joinstyle='round', alpha=0.8)
            else:
                # Fallback for insufficient points
                ax.plot(x_numeric, y_values, color=color, linewidth=2.8,
                        solid_capstyle='round', solid_joinstyle='round', alpha=0.8)

            # Add markers only at actual data points
            ax.scatter(x_numeric, y_values, color=color, s=50, zorder=5,
                       edgecolors='white', linewidth=1.5, label=label)

        # Set x-axis ticks to show month labels
        ax.set_xticks(range(len(monthly_labels)))
        ax.set_xticklabels(monthly_labels)

        # Customize chart
        ax.set_ylabel('Percentage of Income (%)',
                      fontweight='500', color='#333333')
        ax.legend(loc='lower right', frameon=True, fancybox=True, shadow=False,
                  framealpha=0.95, edgecolor='#E0E0E0', facecolor='white')
        ax.grid(True, alpha=0.2, linestyle='-', linewidth=0.5)
        ax.set_facecolor('#FAFBFC')

        # Remove top and right spines for cleaner look
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_color('#E0E0E0')
        ax.spines['bottom'].set_color('#E0E0E0')

        # Format x-axis
        if len(monthly_labels) > 6:
            ax.tick_params(axis='x', rotation=45, colors='#666666')
        ax.tick_params(axis='y', colors='#666666')

        # Adjust layout
        plt.tight_layout()

        fig.savefig(self.cfg.general.spending_type_chart, dpi=96, bbox_inches='tight',
                    facecolor='white', edgecolor='none', format='png')

        plt.close()

    def create_category_spending_chart(self, monthly_labels, category_spending_dict):
        """
        Create line chart for top 5 category spending over time

        Parameters:
        - monthly_labels: list of month labels (strings or datetime objects)
        - category_spending_dict: dictionary with category names as keys and lists of monthly amounts as values
        """

        # Select top 5 categories based on criteria
        def select_top_categories(category_dict, top_n=5):
            stats = {}

            # 1) Compute stats
            for cat, amounts in category_dict.items():
                if cat == 'Savings':
                    continue  # Skip this category
                arr = np.asarray(amounts, dtype=float)
                x = np.arange(arr.size)
                slope, intercept = np.polyfit(x, arr, 1)
                resid_std = np.std(arr - (slope * x + intercept))

                stats[cat] = {
                    'amounts': amounts,
                    'peak': arr.max(),
                    'total': arr.sum(),
                    'growth': slope,
                    'volatility': resid_std,
                }

            # 2) Select top categories by priority metrics
            remaining = set(stats)
            selected = {}

            for metric in ('peak', 'growth', 'volatility'):
                if len(selected) >= top_n or not remaining:
                    break
                best = max(remaining, key=lambda c: stats[c][metric])
                selected[best] = metric
                remaining.remove(best)

            # 3) Fill remaining slots by total
            if len(selected) < top_n:
                top_totals = sorted(
                    remaining, key=lambda c: stats[c]['total'], reverse=True)
                for cat in top_totals[: top_n - len(selected)]:
                    selected[cat] = 'top'

            # 4) Return amounts + reasons
            return {
                cat: (stats[cat]['amounts'], reason) for cat, reason in selected.items()
            }

        # Get top 5 categories
        top_categories = select_top_categories(
            category_spending_dict, 6)

        # Create figure
        fig, ax = plt.subplots(1, 1, figsize=(10, 6))
        fig.patch.set_facecolor('white')

        # Professional colors for categories
        colors = ['#1F4E79', '#8B4F9F', '#E07A28',
                  '#4C9E91', '#516ACC', '#D94F45', '#C1A134']

        # Plot lines for each category with smooth curves
        for i, (category, (amounts, reason)) in enumerate(top_categories.items()):
            # Create numeric x values for interpolation
            x_numeric = np.arange(len(monthly_labels))
            y_values = np.array(amounts)

            # Create smooth curve using spline interpolation
            if len(x_numeric) > 2:  # Need at least 3 points for spline
                x_smooth = np.linspace(x_numeric.min(), x_numeric.max(), 300)
                spline = make_interp_spline(
                    x_numeric, y_values, k=3)
                y_smooth = spline(x_smooth)

                # Map smooth x values back to labels for plotting
                x_smooth_labels = np.interp(x_smooth, x_numeric, x_numeric)
                ax.plot(x_smooth_labels, y_smooth, color=colors[i % len(colors)], linewidth=2.8,
                        solid_capstyle='round', solid_joinstyle='round', alpha=0.8)
            else:
                # Fallback for insufficient points
                ax.plot(x_numeric, y_values, color=colors[i % len(colors)], linewidth=2.8,
                        solid_capstyle='round', solid_joinstyle='round', alpha=0.8)

            # Add markers only at actual data points
            ax.scatter(x_numeric, y_values, color=colors[i % len(colors)], s=50, zorder=5,
                       edgecolors='white', linewidth=1.5, label=category + ' - ' + reason)

        # Set x-axis ticks to show month labels
        ax.set_xticks(range(len(monthly_labels)))
        ax.set_xticklabels(monthly_labels)

        # Customize chart
        ax.set_ylabel('Amount (kr)', fontweight='500', color='#333333')
        ax.legend(loc='lower right', frameon=True, fancybox=True, shadow=False,
                  framealpha=0.95, edgecolor='#E0E0E0', facecolor='white')
        ax.grid(True, alpha=0.2, linestyle='-', linewidth=0.5)
        ax.set_facecolor('#FAFBFC')

        # Remove top and right spines for cleaner look
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['left'].set_color('#E0E0E0')
        ax.spines['bottom'].set_color('#E0E0E0')

        # Format x-axis
        if len(monthly_labels) > 6:
            ax.tick_params(axis='x', rotation=45, colors='#666666')
        ax.tick_params(axis='y', colors='#666666')

        # Format y-axis to show currency
        ax.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda x, p: f'${x:,.0f}'))

        # Adjust layout
        plt.tight_layout()

        fig.savefig(self.cfg.general.category_spending_chart, dpi=96, bbox_inches='tight',
                    facecolor='white', edgecolor='none', format='png')

        plt.close()

    def _get_monthly_data(self,
                          target_month: date) -> Dict[str, Any]:
        """
        Convert a list of Transaction objects to monthly_data dictionary format.

        Args:
            transactions: List of Transaction objects
            target_month: Month number (1-12)
            target_year: Year (e.g., 2024)

        Returns:
            Dictionary in monthly_data format
        """

        # Get current quarter range
        month_start, month_end = self._get_date_range(target_month)
        days_in_month = (month_end - month_start).days

        # Get transactions for both periods
        transactions = self.db.get_transactions(month_start, month_end)
        self._categorize_transactions(transactions)

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
            'income_sources': income_sources_list
        }

        return monthly_data

    def _get_quarterly_data(self, target_month: date) -> Dict[str, Any]:
        """
        Get comprehensive quarterly data including current quarter and previous year comparison.

        Args:
            target_month: Target month for the quarter

        Returns:
            Dictionary with quarterly analysis data
        """

        # Calculate quarter info
        quarter_num = ((target_month.month - 1) // 3) + 1

        # Get current quarter range
        current_quarter_start, current_quarter_end = self._get_quarter_range(
            target_month)

        # Get previous year same quarter
        prev_year_target = target_month - relativedelta(years=1)
        prev_year_start, prev_year_end = self._get_quarter_range(
            prev_year_target)

        # Get transactions for both periods
        current_quarter_transactions = self.db.get_transactions(
            current_quarter_start, current_quarter_end)
        self._categorize_transactions(current_quarter_transactions)

        prev_year_transactions = self.db.get_transactions(
            prev_year_start, prev_year_end)
        self._categorize_transactions(prev_year_transactions)

        # quarterly totals
        quarterly_income = 0.0
        quarterly_expenses = 0.0
        monthly_income = [0.0, 0.0, 0.0]
        monthly_expenses = [0.0, 0.0, 0.0]
        category_totals = defaultdict(float)
        monthly_categories = [defaultdict(
            float), defaultdict(float), defaultdict(float)]

        for tx in current_quarter_transactions:
            amount = tx.amount
            category = tx.category or 'Uncategorized'
            month_index = self._get_month_index(tx.date, current_quarter_start)

            # Positive amounts are income, negative are expenses
            if category == 'Income' or category == 'Uncategorized Income':
                quarterly_income += amount
                monthly_income[month_index] += amount

            else:
                # Convert to positive for expense tracking
                expense_amount = -amount
                quarterly_expenses += expense_amount
                monthly_expenses[month_index] += expense_amount

                category_totals[category] += expense_amount
                monthly_categories[month_index][category] += expense_amount

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
        prev_category_totals = defaultdict(float)

        for tx in prev_year_transactions:
            amount = tx.amount
            category = tx.category or 'Uncategorized'
            month_index = self._get_month_index(tx.date, current_quarter_start)

            # Positive amounts are income, negative are expenses
            if category == 'Income' or category == 'Uncategorized Income':
                prev_quarterly_income += amount

            else:
                # Convert to positive for expense tracking
                expense_amount = -amount
                prev_quarterly_expenses += expense_amount
                prev_category_totals[category] += expense_amount

        # Calculate financial metrics
        prev_quarterly_net_flow = prev_quarterly_income - prev_quarterly_expenses

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

        quarterly_achievements = achievements if achievements else [
            "Completed financial tracking for the quarter"]

        # concerns
        quarterly_concerns = []

        if quarterly_savings_pct < 10:
            quarterly_concerns.append(
                f"Savings rate of {quarterly_savings_pct:.1f}% is below recommended 20%")

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

        return {
            # Basic info
            'quarter': quarter_num,

            # Quarterly totals
            'quarterly_income': quarterly_income,
            'quarterly_expenses': quarterly_expenses,
            'quarterly_net_flow': quarterly_net_flow,
            'quarterly_discretionary_pct': quarterly_discretionary_pct,
            'quarterly_non_discretionary_pct': quarterly_non_discretionary_pct,
            'quarterly_debt_pct': quarterly_debt_pct,
            'quarterly_savings_pct': quarterly_savings_pct,

            # Year-over-year changes
            'yoy_income_change': yoy_income_change,
            'yoy_expenses_change': yoy_expenses_change,
            'yoy_net_flow_change': yoy_net_flow_change,
            'yoy_discretionary_change': yoy_discretionary_change,
            'yoy_non_discretionary_change': yoy_non_discretionary_change,
            'yoy_debt_change': yoy_debt_change,
            'yoy_savings_change': yoy_savings_change,

            # Chart data
            'yoy_previous_data': [
                prev_quarterly_income,
                prev_quarterly_expenses,
                prev_quarterly_net_flow,
                prev_quarterly_discretionary,
                prev_quarterly_non_discretionary,
                prev_quarterly_debt,
                prev_quarterly_savings,
            ],

            # Insights
            'quarterly_achievements': quarterly_achievements,
            'quarterly_concerns': quarterly_concerns,
            'quarterly_recommendations': quarterly_recommendations,
        }

    def _get_yearly_data(self, target_month: date) -> Dict[str, Any]:
        """
        Get comprehensive yearly data with trends and analysis.

        Args:
            target_month: Target month for the year

        Returns:
            Dictionary with yearly analysis data
        """

        # Get current year range
        year_start, year_end = self._get_year_range(target_month)

        # Get transactions for both years
        current_year_transactions = self.db.get_transactions(
            year_start, year_end)
        self._categorize_transactions(current_year_transactions)

        # metrics
        yearly_income = 0.0
        yearly_expenses = 0.0
        monthly_income = [0.0] * 12
        monthly_expenses = [0.0] * 12
        monthly_savings = [0.0] * 12
        monthly_discretionary = [0.0] * 12
        monthly_non_discretionary = [0.0] * 12
        monthly_debt = [0.0] * 12
        category_totals = defaultdict(float)
        income_sources = defaultdict(float)
        monthly_categories = defaultdict(lambda: [0.0] * 12)

        for tx in current_year_transactions:
            amount = tx.amount
            category = tx.category or 'Uncategorized'
            merchant = tx.merchant or tx.description
            month_index = self._get_month_index(tx.date, year_start)

            # Positive amounts are income, negative are expenses
            if category == 'Income' or category == 'Uncategorized Income':
                yearly_income += amount
                monthly_income[month_index] += amount
                # Group income by category or merchant
                income_sources[merchant] += amount

            else:
                # Convert to positive for expense tracking
                expense_amount = -amount
                yearly_expenses += expense_amount
                monthly_expenses[month_index] += expense_amount

                category_type = CATEGORY_TYPES.get(category, 'Unkown')
                if category_type == 'Discretionary':
                    monthly_discretionary[month_index] += expense_amount
                elif category_type == 'Non-Discretionary':
                    monthly_non_discretionary[month_index] += expense_amount
                elif category_type == 'Debt':
                    monthly_debt[month_index] += expense_amount
                elif category_type == 'Savings':
                    monthly_savings[month_index] += expense_amount

                category_totals[category] += expense_amount
                monthly_categories[category][month_index] += expense_amount

        category_totals = dict(
            sorted(category_totals.items(), key=lambda item: item[1], reverse=True))
        income_sources = dict(
            sorted(income_sources.items(), key=lambda item: item[1], reverse=True))

        # Calculate financial metrics
        yearly_net_flow = yearly_income - yearly_expenses

        # Calculate spending by type
        yearly_discretionary = 0.0
        yearly_non_discretionary = 0.0
        yearly_debt = 0.0
        yearly_savings = 0.0
        y_unknown_spending = 0.0

        for category, amount in category_totals.items():
            category_type = CATEGORY_TYPES.get(
                category, 'Unkown')
            if category_type == 'Discretionary':
                yearly_discretionary += amount
            elif category_type == 'Non-Discretionary':
                yearly_non_discretionary += amount
            elif category_type == 'Debt':
                yearly_debt += amount
            elif category_type == 'Savings':
                yearly_savings += amount
            else:
                y_unknown_spending += amount

        yearly_discretionary_pct = (
            yearly_discretionary / yearly_income * 100) if yearly_income > 0 else 0
        yearly_non_discretionary_pct = (
            yearly_non_discretionary / yearly_income * 100) if yearly_income > 0 else 0
        yearly_debt_pct = (yearly_debt / yearly_income *
                           100) if yearly_income > 0 else 0
        yearly_savings_pct = (yearly_savings / yearly_income *
                              100) if yearly_income > 0 else 0

        # months
        monthly_labels = []
        current = date(year_start.year, year_start.month +
                       1, 1)  # normalize to first of month
        while current <= year_end:
            monthly_labels.append(current.strftime(
                '%b %Y'))  # e.g., 'Jan 2025'
            current += relativedelta(months=1)

        monthly_discretionary_pct = [
            a / b * 100 if b > 0 else 0 for a, b in zip(monthly_discretionary, monthly_income)]
        monthly_non_discretionary_pct = [
            a / b * 100 if b > 0 else 0 for a, b in zip(monthly_non_discretionary, monthly_income)]
        monthly_savings_pct = [
            a / b * 100 if b > 0 else 0 for a, b in zip(monthly_savings, monthly_income)]
        monthly_debt_pct = [a / b * 100 if b > 0 else 0 for a,
                            b in zip(monthly_debt, monthly_income)]

        # charts
        self.create_spending_type_chart(
            monthly_labels, monthly_discretionary_pct, monthly_non_discretionary_pct, monthly_savings_pct, monthly_debt_pct)
        self.create_category_spending_chart(
            monthly_labels, monthly_categories)

        return {
            # metrics
            'yearly_income': yearly_income,
            'yearly_expenses': yearly_expenses,
            'yearly_net_flow': yearly_net_flow,
            'yearly_discretionary': yearly_discretionary,
            'yearly_non_discretionary': yearly_non_discretionary,
            'yearly_savings': yearly_savings,
            'yearly_debt': yearly_debt,
            # rates
            'yearly_discretionary_pct': yearly_discretionary_pct,
            'yearly_non_discretionary_pct': yearly_non_discretionary_pct,
            'yearly_savings_pct': yearly_savings_pct,
            'yearly_debt_pct': yearly_debt_pct,
            # summary charts
            'yearly_income_labels': list(income_sources.keys()),
            'yearly_income_amounts': list(income_sources.values()),
            'yearly_category_labels': list(category_totals.keys()),
            'yearly_category_amounts': list(category_totals.values()),
            # progression charts
            'spending_type_chart': Path(self.cfg.general.spending_type_chart).stem,
            'category_spending_chart': Path(self.cfg.general.category_spending_chart).stem,
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
                # Save to database
                saved_count = self.db.save_transactions(new_transactions)
                logger.info(
                    f"Saved {saved_count} new transactions to database")

            return new_transactions

        except Exception as e:
            logger.error(f"Failed to process new files: {e}", exc_info=True)
            return []

    def import_new_files(self):
        """
        Check for new files, import if so
        """

        # Process new files first
        input_dir = Path(self.cfg.general.input_dir)
        patterns = ["*.csv", "*.xls", "*.xlsx"]
        files = []
        for pattern in patterns:
            files.extend(input_dir.glob(pattern))

        if files:
            new_transactions = self.process_new_files()
            if new_transactions:
                logger.info(
                    f"Imported {len(new_transactions)} new transactions from files")

        return

    def generate_report(
        self,
        target_month: date,
        report_type: str = "monthly",
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

        template_data = self._get_monthly_data(target_month)
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
        template_name = f"report_template.html"
        try:
            template = self.jinja_env.get_template(template_name)
            html_content = template.render(**template_data)
            html_content_min = minify_html.minify(html_content,
                                                  # Safe optimizations - keep these
                                                  allow_noncompliant_unquoted_attribute_values=True,
                                                  allow_optimal_entities=True,
                                                  allow_removing_spaces_between_attributes=True,
                                                  keep_input_type_text_attr=False,
                                                  minify_doctype=True,
                                                  minify_js=True,
                                                  remove_bangs=True,

                                                  # CRITICAL: Keep these for email compatibility
                                                  minify_css=False,
                                                  keep_closing_tags=True,
                                                  keep_comments=True,
                                                  keep_html_and_head_opening_tags=True,
                                                  keep_ssi_comments=True,
                                                  remove_processing_instructions=False,

                                                  # Template syntax - adjust based on your needs
                                                  preserve_brace_template_syntax=False,
                                                  preserve_chevron_percent_template_syntax=False,
                                                  )

            html_size = len(html_content_min.encode('utf-8'))
            if html_size > 100000:
                logger.warning(
                    f"Generated HTML is {html_size // 1000}kB > 100kB, will be clipped in email!")
            else:
                logger.info(f"Generated HTML of size {html_size // 1000}kB")
            return html_content_min
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
            msg["To"] = self.cfg.credentials.email_from

            msg_alt = MIMEMultipart("alternative")
            msg.attach(msg_alt)

            html_part = MIMEText(html_content, "html", "utf-8")
            msg_alt.attach(html_part)

            with open(self.cfg.general.spending_type_chart, "rb") as img_file:
                mime_img = MIMEImage(img_file.read())
                mime_img.add_header(
                    "Content-ID", f"<{Path(self.cfg.general.spending_type_chart).stem}>")
                mime_img.add_header(
                    "Content-Disposition", "inline", filename=Path(self.cfg.general.spending_type_chart).name)
                msg.attach(mime_img)

            with open(self.cfg.general.category_spending_chart, "rb") as img_file:
                mime_img = MIMEImage(img_file.read())
                mime_img.add_header(
                    "Content-ID", f"<{Path(self.cfg.general.category_spending_chart).stem}>")
                mime_img.add_header(
                    "Content-Disposition", "inline", filename=Path(self.cfg.general.category_spending_chart).name)
                msg.attach(mime_img)

            with smtplib.SMTP(self.cfg.general.smtp_server, self.cfg.general.smtp_port) as server:
                server.starttls()
                server.login(self.cfg.credentials.smtp_user,
                             self.cfg.credentials.smtp_password)
                server.send_message(
                    msg, to_addrs=self.cfg.credentials.email_from)

            logger.info("Email report sent successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to send email report: {e}", exc_info=True)
            return False

    def run_full_analysis(
        self,
        target_month: date,
        report_type: str = "monthly",
        send_email: bool = True
    ) -> Dict[str, Any]:
        """
        Run complete budget analysis with report generation and email sending.

        Args:
            target_month: Target month for analysis
            report_type: Type of report to generate
            send_email: Whether to send email report

        Returns:
            Dictionary with analysis results
        """
        try:
            # Import bank statement files
            self.import_new_files()

            # Generate report
            html_report = self.generate_report(target_month, report_type)

            # Prepare results
            results = {
                "success": True,
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
                    subject = f"{report_type.title()} Budget Report, {target_month.strftime('%B %Y')}"
                    results["email_sent"] = self.send_email_report(
                        html_report, subject)

            return results

        except Exception as e:
            logger.error(f"Full analysis failed: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
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
        else:
            target_month = date.today()
            if target_month.day < 25:
                target_month -= relativedelta(months=1)
            target_month = target_month.replace(day=1)

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

        # Run analysis
        results = analyzer.run_full_analysis(
            target_month=target_month,
            report_type=report_type,
            send_email=not args.no_email
        )

        if results["success"]:
            logger.info(
                f"Analysis completed successfully")
            if results["email_sent"]:
                logger.info("Email report sent")
        else:
            logger.error(f"Analysis failed: {results['error']}")

    except Exception as e:
        logger.error(f"Application failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
