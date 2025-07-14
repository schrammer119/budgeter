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
                           MERCHANT_CATEGORIES)
from utils.classes import (Transaction,
                           Config,
                           LOG_FILE_PATH)
from utils.database import DatabaseManager
from utils.fileHandler import FileHandler
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from rapidfuzz import process, fuzz
from typing import Dict, List, Optional, Tuple, Any
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
                break
            if tx.category:
                continue  # Already categorized

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
                tx.merchant = best

    def recategorize_transactions(self, target_month: Optional[date] = None) -> Dict[str, Any]:
        """
        Re-categorize transactions for the specified month using current rules.

        Args:
            target_month: Target month to re-categorize. If None, uses current month.

        Returns:
            Dictionary with re-categorization results
        """
        start_date, end_date = self._get_date_range(target_month)

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
                tx.id: (tx.category, tx.merchant) for tx in transactions}

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
                    tx.id, (None, None))
                if tx.category != original_cat or tx.merchant != original_merchant:
                    updated_transactions.append(tx)
                    logger.debug(f"Transaction {tx.id}: {original_cat} -> {tx.category}, "
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
            logger.error(f"Failed to re-categorize transactions: {e}")
            return {
                "success": False,
                "error": str(e),
                "transactions_processed": 0,
                "changes_made": 0
            }

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

        # Calculate current quarter metrics
        current_quarter_total = sum(
            tx.amount for tx in current_quarter_transactions)
        current_quarter_categories = {}
        current_quarter_monthly = {}

        for tx in current_quarter_transactions:
            # Category totals
            category = tx.category or "Uncategorized"
            current_quarter_categories[category] = current_quarter_categories.get(
                category, 0) + tx.amount

            # Monthly breakdown
            month_key = tx.date.strftime("%Y-%m")
            if month_key not in current_quarter_monthly:
                current_quarter_monthly[month_key] = {
                    "total": 0, "categories": {}}
            current_quarter_monthly[month_key]["total"] += tx.amount
            current_quarter_monthly[month_key]["categories"][category] = \
                current_quarter_monthly[month_key]["categories"].get(
                    category, 0) + tx.amount

        # Calculate previous year quarter metrics
        prev_quarter_total = sum(tx.amount for tx in prev_quarter_transactions)
        prev_quarter_categories = {}

        for tx in prev_quarter_transactions:
            category = tx.category or "Uncategorized"
            prev_quarter_categories[category] = prev_quarter_categories.get(
                category, 0) + tx.amount

        # Calculate quarter info
        quarter_num = ((target_month.month - 1) // 3) + 1
        quarter_name = f"Q{quarter_num} {target_month.year}"
        prev_quarter_name = f"Q{quarter_num} {prev_year_target.year}"

        # Calculate averages and trends
        months_in_quarter = len(current_quarter_monthly)
        monthly_average = current_quarter_total / max(months_in_quarter, 1)

        # Year-over-year comparison
        yoy_change = current_quarter_total - prev_quarter_total
        yoy_change_pct = (yoy_change / abs(prev_quarter_total)
                          ) * 100 if prev_quarter_total != 0 else 0

        # Category comparisons
        category_comparisons = {}
        all_categories = set(current_quarter_categories.keys()) | set(
            prev_quarter_categories.keys())

        for category in all_categories:
            current_amount = current_quarter_categories.get(category, 0)
            prev_amount = prev_quarter_categories.get(category, 0)
            change = current_amount - prev_amount
            change_pct = (change / abs(prev_amount)) * \
                100 if prev_amount != 0 else 0

            category_comparisons[category] = {
                "current": current_amount,
                "previous": prev_amount,
                "change": change,
                "change_pct": change_pct
            }

        return {
            "quarter_name": quarter_name,
            "quarter_start": current_quarter_start,
            "quarter_end": current_quarter_end,
            "current_quarter": {
                "total": current_quarter_total,
                "transactions_count": len(current_quarter_transactions),
                "categories": current_quarter_categories,
                "monthly_breakdown": current_quarter_monthly,
                "monthly_average": monthly_average,
                "months_included": months_in_quarter
            },
            "previous_year_quarter": {
                "name": prev_quarter_name,
                "total": prev_quarter_total,
                "transactions_count": len(prev_quarter_transactions),
                "categories": prev_quarter_categories,
                "start": prev_quarter_start,
                "end": prev_quarter_end
            },
            "year_over_year": {
                "change": yoy_change,
                "change_pct": yoy_change_pct,
                "category_comparisons": category_comparisons
            }
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
            logger.error(f"Failed to process new files: {e}")
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

        # Group transactions by category
        grouped_transactions = {}
        for tx in transactions:
            category = tx.category or "Uncategorized"
            if category not in grouped_transactions:
                grouped_transactions[category] = []
            grouped_transactions[category].append(tx)

        # Calculate totals
        category_totals = {
            category: sum(tx.amount for tx in txs)
            for category, txs in grouped_transactions.items()
        }

        # Prepare template data
        template_data = {
            "start_date": start_date,
            "end_date": end_date,
            "generated_on": datetime.now(),
            "report_type": report_type,
            "groups": grouped_transactions,
            "category_totals": category_totals,
            "total_amount": sum(category_totals.values()),
            "total_transactions": len(transactions)
        }

        # Add report-specific data
        if report_type == "quarterly":
            template_data["quarterly_data"] = self._get_quarterly_data(
                target_month)
        elif report_type == "yearly":
            template_data["yearly_data"] = self._get_yearly_data(target_month)

        # Render template
        template_name = f"report_{report_type}.html"
        try:
            template = self.jinja_env.get_template(template_name)
            return template.render(**template_data)
        except Exception as e:
            logger.error(f"Failed to render template {template_name}: {e}")
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
            logger.error(f"Failed to send email report: {e}")
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
                with open("data/report.html", "w", encoding="utf-8") as f:
                    f.write(html_report)
                logger.info("Report saved to data/report.html")

                # Send email if requested
                if send_email:
                    start_date, end_date = self._get_date_range(target_month)
                    subject = f"Budget Report ({report_type.title()}) - {start_date} to {end_date}"
                    results["email_sent"] = self.send_email_report(
                        html_report, subject)

            return results

        except Exception as e:
            logger.error(f"Full analysis failed: {e}")
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
    parser.add_argument("--report-type", choices=["monthly", "quarterly", "yearly"],
                        default="monthly", help="Type of report to generate")
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

        # Handle re-categorization request
        if args.recategorize:
            logger.info("Re-categorizing transactions...")
            recategorize_results = analyzer.recategorize_transactions(
                target_month)

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
            if not input("Continue with report generation? (y/N): ").lower().startswith('y'):
                return

        # Run analysis
        results = analyzer.run_full_analysis(
            target_month=target_month,
            report_type=args.report_type,
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
        logger.error(f"Application failed: {e}")
        raise


if __name__ == "__main__":
    main()
