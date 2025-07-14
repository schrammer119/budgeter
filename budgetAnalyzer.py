#!/usr/bin/env python3
"""
Enhanced Budget Analyzer for File-based Import
==============================================

Production-ready budget analyzer with database persistence, file import handling,
and comprehensive error handling.

Author: Alexander Schramm
License: MIT
"""

from utils.database import DatabaseManager
from utils.fileHandler import FileHandler
from utils.classes import Transaction, Config, LOG_FILE_PATH
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dateutil.relativedelta import relativedelta
from datetime import date, datetime, timedelta
import os
import sys
import logging
import smtplib
import rapidfuzz
import re

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

    def _normalize(self, text: str) -> str:
        """
        Lowercase, strip punctuation & numbers, collapse whitespace.
        """
        text = text.lower()
        # remove punctuation
        text = re.sub(r"[^\w\s]", " ", text)
        # remove digits
        text = re.sub(r"\d+", " ", text)
        # collapse multiple spaces
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _categorize_transactions(self, transactions: List[Transaction]) -> None:
        """Categorize transactions based on description keywords."""
        for tx in transactions:
            if tx.category:
                continue  # Already categorized

            norm = self._normalize(tx.description)
            best, score, _ = rapidfuzz.process.extractOne(
                norm, self.cfg.general.merchant_keys, scorer=rapidfuzz.fuzz.token_sort_ratio)
            if score >= self.cfg.general.category_threshold:
                tx.category = self.cfg.general.merchant_categories[best]
            else:
                tx.category = "Uncategorized"

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

    def init_database(self, dry_run: bool = False):
        """
        Initialize database with data from files in input directory.

        Args:
            dry_run: If True, only simulate the operation without saving data.
        """
        logger.info("Initializing database with data from files...")

        try:
            # Process all files in input directory
            if dry_run:
                logger.info("DRY RUN: Simulating file processing")
                files = self.file_handler.get_new_files()
                total_transactions = 0

                for file_path in files:
                    try:
                        transactions = self.file_handler.parse_file(file_path)
                        total_transactions += len(transactions)
                        logger.info(
                            f"Would process {len(transactions)} transactions from {file_path}")
                    except Exception as e:
                        logger.error(f"Failed to parse {file_path}: {e}")

                logger.info(
                    f"DRY RUN: Would process {total_transactions} total transactions")

            else:
                # Clean up old data first (keep last year)
                today = date.today()
                cutoff_date = today - relativedelta(years=1)
                self.db.cleanup_old_data(cutoff_date)

                # Process all files
                all_transactions = self.file_handler.process_new_files()

                if all_transactions:
                    # Categorize transactions
                    self._categorize_transactions(all_transactions)

                    # Save to database
                    saved_count = self.db.save_transactions(all_transactions)
                    logger.info(
                        f"Processed {saved_count} transactions from files")
                else:
                    logger.warning("No files found to process")

        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            raise

    def generate_report(
        self,
        transactions: List[Transaction],
        report_type: str = "monthly",
        target_month: Optional[date] = None
    ) -> str:
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
        if report_type in ["quarterly", "yearly"]:
            template_data.update(
                self._get_comparative_data(target_month, report_type))

        # Render template
        template_name = f"report_{report_type}.html"
        try:
            template = self.jinja_env.get_template(template_name)
            return template.render(**template_data)
        except Exception as e:
            logger.error(f"Failed to render template {template_name}: {e}")
            # Fallback to basic template
            try:
                template = self.jinja_env.get_template("report_basic.html")
                return template.render(**template_data)
            except Exception as e2:
                logger.error(f"Failed to render basic template: {e2}")
                # Generate minimal HTML report
                return self._generate_minimal_report(template_data)

    def _generate_minimal_report(self, data: Dict[str, Any]) -> str:
        """Generate a minimal HTML report when templates fail."""
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Budget Report - {data['report_type'].title()}</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; }}
                .header {{ background-color: #f0f0f0; padding: 15px; margin-bottom: 20px; }}
                .category {{ margin-bottom: 15px; }}
                .total {{ font-weight: bold; color: #0066cc; }}
                table {{ border-collapse: collapse; width: 100%; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                th {{ background-color: #f2f2f2; }}
            </style>
        </head>
        <body>
            <div class="header">
                <h1>Budget Report - {data['report_type'].title()}</h1>
                <p>Period: {data['start_date']} to {data['end_date']}</p>
                <p>Generated: {data['generated_on'].strftime('%Y-%m-%d %H:%M:%S')}</p>
            </div>

            <h2>Summary</h2>
            <p><strong>Total Transactions:</strong> {data['total_transactions']}</p>
            <p><strong>Total Amount:</strong> {data['total_amount']:.2f} SEK</p>

            <h2>By Category</h2>
            <table>
                <thead>
                    <tr>
                        <th>Category</th>
                        <th>Amount</th>
                        <th>Count</th>
                    </tr>
                </thead>
                <tbody>
        """

        for category, total in data['category_totals'].items():
            count = len(data['groups'][category])
            html += f"""
                    <tr>
                        <td>{category}</td>
                        <td>{total:.2f} SEK</td>
                        <td>{count}</td>
                    </tr>
            """

        html += """
                </tbody>
            </table>
        </body>
        </html>
        """

        return html

    def _get_comparative_data(self, target_month: Optional[date], report_type: str) -> Dict[str, Any]:
        """Get comparative data for quarterly and yearly reports."""
        if not target_month:
            target_month = date.today()

        comparative_data = {}

        if report_type == "quarterly":
            # Get same quarter last year
            last_year = target_month - relativedelta(years=1)
            last_year_start, last_year_end = self._get_date_range(last_year)

            last_year_transactions = self.db.get_transactions(
                last_year_start, last_year_end)

            comparative_data["last_year_same_quarter"] = {
                "transactions": len(last_year_transactions),
                "total_amount": sum(tx.amount for tx in last_year_transactions)
            }

        elif report_type == "yearly":
            # Get year-to-date data
            year_start = target_month.replace(month=1, day=1)
            ytd_transactions = self.db.get_transactions(
                year_start, target_month)

            comparative_data["year_to_date"] = {
                "transactions": len(ytd_transactions),
                "total_amount": sum(tx.amount for tx in ytd_transactions)
            }

        return comparative_data

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

    def get_status(self) -> Dict[str, Any]:
        """Get current status of the budget analyzer."""
        try:
            file_summary = self.file_handler.get_file_summary()

            # Get database stats
            today = date.today()
            current_month_start, current_month_end = self._get_date_range()
            current_month_transactions = self.db.get_transactions(
                current_month_start, current_month_end)

            # Get last 3 months for trend
            last_3_months = []
            for i in range(3):
                month_date = today - relativedelta(months=i)
                start_date, end_date = self._get_date_range(month_date)
                transactions = self.db.get_transactions(start_date, end_date)
                last_3_months.append({
                    'month': month_date.strftime('%Y-%m'),
                    'count': len(transactions),
                    'total': sum(tx.amount for tx in transactions)
                })

            return {
                'file_summary': file_summary,
                'current_month': {
                    'start_date': current_month_start,
                    'end_date': current_month_end,
                    'transaction_count': len(current_month_transactions),
                    'total_amount': sum(tx.amount for tx in current_month_transactions)
                },
                'last_3_months': last_3_months
            }

        except Exception as e:
            logger.error(f"Failed to get status: {e}")
            return {'error': str(e)}

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
    parser.add_argument("--config", default="config",
                        help="Configuration directory with general.json and credentials.json")
    parser.add_argument("--init-db", action="store_true",
                        help="Initialize database with data from files")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run in dry-run mode (simulation only)")
    parser.add_argument("--report-type", choices=["monthly", "quarterly", "yearly"],
                        default="monthly", help="Type of report to generate")
    parser.add_argument("--no-email", action="store_true",
                        help="Don't send email report")
    parser.add_argument(
        "--target-month", help="Target month in YYYY-MM format")
    parser.add_argument("--force-import", action="store_true",
                        help="Force processing of files even if data exists")
    parser.add_argument("--status", action="store_true",
                        help="Show current status and exit")

    args = parser.parse_args()

    try:
        # Load configuration
        cfg = Config.load(args.config)

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

        # Show status if requested
        if args.status:
            status = analyzer.get_status()
            print("=== Budget Analyzer Status ===")
            print(f"Files: {status.get('file_summary', {})}")
            print(f"Current Month: {status.get('current_month', {})}")
            print(f"Last 3 Months: {status.get('last_3_months', [])}")
            return

        # Parse target month if provided
        target_month = None
        if args.target_month:
            try:
                target_month = datetime.strptime(
                    args.target_month, "%Y-%m").date()
            except ValueError:
                logger.error("Invalid target month format. Use YYYY-MM")
                return

        # Initialize database if requested
        if args.init_db:
            analyzer.init_database(dry_run=args.dry_run)
            if args.dry_run:
                logger.info("Dry run completed - no data was saved")
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
