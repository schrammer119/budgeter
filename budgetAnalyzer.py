#!/usr/bin/env python3
"""
Enhanced Budget Analyzer for Länsförsäkringar Bank
==================================================

Production-ready budget analyzer with database persistence, multiple reporting types,
and comprehensive error handling.

Author: Alexander Schramm
License: MIT
"""

import json
import logging
import sqlite3
import smtplib
import uuid
import sys
import os
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)
os.makedirs('config', exist_ok=True)

__version__ = "0.0.3"
LOG_FILE_PATH = "logs/budget_analyzer.log"


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


@dataclass
class Transaction:
    """Represents a bank transaction with optional categorization."""
    date: date
    amount: float
    description: str
    account_id: str
    transaction_id: str
    category: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert transaction to dictionary for JSON serialization."""
        return {
            'date': self.date.isoformat(),
            'amount': self.amount,
            'description': self.description,
            'account_id': self.account_id,
            'transaction_id': self.transaction_id,
            'category': self.category
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Transaction':
        """Create Transaction from dictionary."""
        return cls(
            date=datetime.fromisoformat(data['date']).date(),
            amount=data['amount'],
            description=data['description'],
            account_id=data['account_id'],
            transaction_id=data['transaction_id'],
            category=data.get('category')
        )


@dataclass
class GeneralConfig:
    """General configuration settings (non-sensitive)."""
    # Environment settings
    sandbox_mode: bool = False

    # Database Configuration
    db_path: str = "data/budget_data.db"

    # Categorization
    categories: Dict[str, str] = field(default_factory=lambda: {
        "ica": "Groceries",
        "coop": "Groceries",
        "willys": "Groceries",
        "hemköp": "Groceries",
        "spotify": "Entertainment",
        "netflix": "Entertainment",
        "gym": "Health & Fitness",
        "apoteket": "Health & Fitness",
        "sl": "Transportation",
        "shell": "Transportation",
        "circle k": "Transportation",
        "statoil": "Transportation",
        "parkering": "Transportation",
        "restaurant": "Dining",
        "pizzeria": "Dining",
        "cafe": "Dining",
        "mc": "Dining",
        "max": "Dining",
        "burger": "Dining",
        "elgiganten": "Electronics",
        "mediamarkt": "Electronics",
        "kjell": "Electronics",
        "inet": "Electronics",
        "komplett": "Electronics",
        "hyra": "Housing",
        "el": "Utilities",
        "vatten": "Utilities",
        "tele2": "Utilities",
        "telenor": "Utilities",
        "telia": "Utilities",
        "internet": "Utilities"
    })

    # Reporting
    templates_dir: str = "config"

    # Request settings
    psu_ip: str = "127.0.0.1"
    psu_ua: str = f"BudgetAnalyzer/{__version__}"
    request_timeout: int = 30
    retry_attempts: int = 3

    # Email settings (non-sensitive)
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587

    # Logging
    log_level: str = "INFO"
    log_file: str = LOG_FILE_PATH


@dataclass
class CredentialsConfig:
    """Sensitive configuration settings (credentials, API keys, etc.)."""
    # Bank API Configuration
    token_url: str = "https://auth.lansforsakringar.se/oauth2/token"
    api_base: str = "https://api.lansforsakringar.se/v2"
    client_id: str = "your_client_id_here"
    client_secret: str = "your_client_secret_here"
    fapi_financial_id: str = "your_fapi_financial_id_here"
    consent_id: Optional[str] = None

    # SMTP Credentials
    smtp_user: str = "your_email@gmail.com"
    smtp_password: str = "your_app_password_here"
    email_from: str = "your_email@gmail.com"
    email_to: str = "your_email@gmail.com"


@dataclass
class Config:
    """Combined configuration for the budget analyzer."""
    general: GeneralConfig
    credentials: CredentialsConfig

    @staticmethod
    def load(config_dir: str = "config") -> "Config":
        """Load configuration from config directory."""
        config_path = Path(config_dir)
        general_file = config_path / "general.json"
        credentials_file = config_path / "credentials.json"

        # Create config directory if it doesn't exist
        config_path.mkdir(exist_ok=True)

        # Load or create general config
        if general_file.exists():
            try:
                with open(general_file, "r", encoding="utf-8") as f:
                    general_data = json.load(f)
                general_config = GeneralConfig(**general_data)
                logger.info(
                    f"General configuration loaded from {general_file}")
            except (json.JSONDecodeError, TypeError) as e:
                logger.error(
                    f"Invalid JSON in general configuration file: {e}")
                logger.info("Creating new general configuration file")
                general_config = GeneralConfig()
                Config._save_general_config(general_config, general_file)
        else:
            logger.info(
                f"General configuration file not found, creating {general_file}")
            general_config = GeneralConfig()
            Config._save_general_config(general_config, general_file)

        # Load or create credentials config
        if credentials_file.exists():
            try:
                with open(credentials_file, "r", encoding="utf-8") as f:
                    credentials_data = json.load(f)
                credentials_config = CredentialsConfig(**credentials_data)
                logger.info(
                    f"Credentials configuration loaded from {credentials_file}")
            except (json.JSONDecodeError, TypeError) as e:
                logger.error(
                    f"Invalid JSON in credentials configuration file: {e}")
                logger.info("Creating new credentials configuration file")
                credentials_config = CredentialsConfig()
                Config._save_credentials_config(
                    credentials_config, credentials_file)
        else:
            logger.info(
                f"Credentials configuration file not found, creating {credentials_file}")
            credentials_config = CredentialsConfig()
            Config._save_credentials_config(
                credentials_config, credentials_file)

        # Check if credentials need to be configured
        if (credentials_config.client_id == "your_client_id_here" or
                credentials_config.client_secret == "your_client_secret_here"):
            logger.warning(
                "⚠️  Credentials configuration contains default values!")
            logger.warning(
                f"Please edit {credentials_file} with your actual API credentials")

        return Config(general=general_config, credentials=credentials_config)

    @staticmethod
    def _save_general_config(config: GeneralConfig, file_path: Path):
        """Save general configuration to file."""
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(asdict(config), f, indent=2, ensure_ascii=False)
            logger.info(f"General configuration saved to {file_path}")
        except Exception as e:
            logger.error(f"Failed to save general configuration: {e}")
            raise

    @staticmethod
    def _save_credentials_config(config: CredentialsConfig, file_path: Path):
        """Save credentials configuration to file."""
        try:
            config_dict = asdict(config)

            # Add comments to the JSON for better user experience
            config_with_comments = {
                "_comment": [
                    "This file contains sensitive credentials - keep it secure!",
                    "Replace the placeholder values with your actual credentials:",
                    "- Get API credentials from Länsförsäkringar Developer Portal",
                    "- For Gmail SMTP, use an App Password, not your regular password",
                    "- Never commit this file to version control"
                ],
                **config_dict
            }

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(config_with_comments, f,
                          indent=2, ensure_ascii=False)

            # Set restrictive permissions on credentials file (Unix-like systems)
            try:
                file_path.chmod(0o600)  # Read/write for owner only
            except (AttributeError, OSError):
                pass  # Windows or permission error

            logger.info(f"Credentials configuration saved to {file_path}")
        except Exception as e:
            logger.error(f"Failed to save credentials configuration: {e}")
            raise

    def save(self, config_dir: str = "config"):
        """Save both configuration files."""
        config_path = Path(config_dir)
        config_path.mkdir(exist_ok=True)

        general_file = config_path / "general.json"
        credentials_file = config_path / "credentials.json"

        self._save_general_config(self.general, general_file)
        self._save_credentials_config(self.credentials, credentials_file)


class BankAPIError(Exception):
    """Custom exception for Bank API errors."""
    pass


class BankAPI:
    """
    Enhanced Bank API client for Länsförsäkringar with proper OAuth2 flow,
    error handling, and retry logic.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._token: Optional[str] = None
        self._token_expiry: datetime = datetime.min
        self._session = self._create_session()

        # Adjust URLs for sandbox mode
        if cfg.general.sandbox_mode:
            self.cfg.credentials.token_url = self.cfg.credentials.token_url.replace(
                "/oauth2/", "/sandbox/oauth2/")
            self.cfg.credentials.api_base = self.cfg.credentials.api_base.replace(
                "/v2/", "/sandbox/v2/")
            logger.info("Running in sandbox mode")

    def _create_session(self) -> requests.Session:
        """Create a requests session with retry strategy."""
        session = requests.Session()
        retry_strategy = Retry(
            total=self.cfg.general.retry_attempts,

            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def authenticate(self) -> str:
        """Authenticate with the bank API using OAuth2 client credentials flow."""
        if self._token and datetime.utcnow() < self._token_expiry:
            return self._token

        logger.info("Authenticating with bank API...")

        data = {
            "grant_type": "client_credentials",
            "scope": "AIS",
            "client_id": self.cfg.credentials.client_id,
            "client_secret": self.cfg.credentials.client_secret,
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "x-fapi-financial-id": self.cfg.credentials.fapi_financial_id,
        }

        try:
            response = self._session.post(
                self.cfg.credentials.token_url,
                data=data,
                headers=headers,
                timeout=self.cfg.general.request_timeout
            )
            response.raise_for_status()

            token_data = response.json()
            self._token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 3600)
            self._token_expiry = datetime.utcnow() + timedelta(seconds=expires_in - 60)

            logger.info("Authentication successful")
            return self._token

        except requests.exceptions.RequestException as e:
            logger.error(f"Authentication failed: {e}")
            raise BankAPIError(f"Authentication failed: {e}")
        except KeyError as e:
            logger.error(f"Invalid token response format: {e}")
            raise BankAPIError(f"Invalid token response: {e}")

    def _get_headers(self) -> Dict[str, str]:
        """Get headers for API requests."""
        return {
            "Authorization": f"Bearer {self.authenticate()}",
            "x-fapi-financial-id": self.cfg.credentials.fapi_financial_id,
            "X-Request-ID": str(uuid.uuid4()),
            "Accept": "application/json",
            "PSU-IP-Address": self.cfg.general.psu_ip,
            "PSU-User-Agent": self.cfg.general.psu_ua,
        }

    def list_accounts(self) -> List[Dict[str, Any]]:
        """List all accounts for the authenticated user."""
        url = f"{self.cfg.credentials.api_base}/accounts"
        headers = self._get_headers()

        try:
            response = self._session.get(
                url, headers=headers, timeout=self.cfg.general.request_timeout)
            response.raise_for_status()

            data = response.json()
            accounts = data.get("Data", {}).get("Account", [])

            logger.info(f"Retrieved {len(accounts)} accounts")
            return accounts

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to list accounts: {e}")
            raise BankAPIError(f"Failed to list accounts: {e}")

    def get_transactions(
        self,
        account_id: str,
        start_date: date,
        end_date: date
    ) -> List[Dict[str, Any]]:
        """Get transactions for a specific account within a date range."""
        url = f"{self.cfg.credentials.api_base}/accounts/{account_id}/transactions"
        headers = self._get_headers()

        params = {
            "dateFrom": start_date.isoformat(),
            "dateTo": end_date.isoformat(),
            "bookingStatus": "booked,pending"
        }

        all_transactions = []

        try:
            while url:
                response = self._session.get(
                    url,
                    headers=headers,
                    params=params if not all_transactions else None,
                    timeout=30
                )
                response.raise_for_status()

                data = response.json()
                transactions_data = data.get(
                    "Data", {}).get("transactions", {})

                # Add both booked and pending transactions
                booked = transactions_data.get("booked", [])
                pending = transactions_data.get("pending", [])
                all_transactions.extend(booked + pending)

                # Check for pagination
                links = transactions_data.get("_links", {})
                next_link = links.get("next", {}).get("href")
                url = next_link if next_link else None
                params = None  # Clear params for subsequent requests

            logger.info(
                f"Retrieved {len(all_transactions)} transactions for account {account_id}")
            return all_transactions

        except requests.exceptions.RequestException as e:
            logger.error(
                f"Failed to get transactions for account {account_id}: {e}")
            raise BankAPIError(f"Failed to get transactions: {e}")


class DatabaseManager:
    """Manages SQLite database operations for transaction storage."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        """Initialize the database schema."""
        try:
            with sqlite3.connect(str(Path(self.db_path).absolute().resolve())) as conn:
                conn.execute('''
                    CREATE TABLE IF NOT EXISTS transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        date DATE NOT NULL,
                        amount REAL NOT NULL,
                        description TEXT NOT NULL,
                        account_id TEXT NOT NULL,
                        transaction_id TEXT UNIQUE NOT NULL,
                        category TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_date ON transactions(date)
                ''')

                conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_account ON transactions(account_id)
                ''')

                conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_transaction_id ON transactions(transaction_id)
                ''')

                conn.commit()
                logger.info("Database initialized successfully")

        except sqlite3.Error as e:
            logger.error(f"Database initialization failed: {e}")
            raise

    def save_transactions(self, transactions: List[Transaction]) -> int:
        """Save transactions to database, ignoring duplicates."""
        saved_count = 0

        try:
            with sqlite3.connect(self.db_path) as conn:
                for tx in transactions:
                    try:
                        conn.execute('''
                            INSERT INTO transactions
                            (date, amount, description, account_id, transaction_id, category)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (
                            tx.date.isoformat(),
                            tx.amount,
                            tx.description,
                            tx.account_id,
                            tx.transaction_id,
                            tx.category
                        ))
                        saved_count += 1
                    except sqlite3.IntegrityError:
                        # Transaction already exists, update category if needed
                        if tx.category:
                            conn.execute('''
                                UPDATE transactions
                                SET category = ?, updated_at = CURRENT_TIMESTAMP
                                WHERE transaction_id = ?
                            ''', (tx.category, tx.transaction_id))

                conn.commit()

        except sqlite3.Error as e:
            logger.error(f"Failed to save transactions: {e}")
            raise

        logger.info(f"Saved {saved_count} new transactions to database")
        return saved_count

    def get_transactions(
        self,
        start_date: date,
        end_date: date
    ) -> List[Transaction]:
        """Retrieve transactions from database within date range."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute('''
                    SELECT date, amount, description, account_id, transaction_id, category
                    FROM transactions
                    WHERE date BETWEEN ? AND ?
                    ORDER BY date DESC
                ''', (start_date.isoformat(), end_date.isoformat()))

                transactions = []
                for row in cursor.fetchall():
                    transactions.append(Transaction(
                        date=datetime.fromisoformat(row[0]).date(),
                        amount=row[1],
                        description=row[2],
                        account_id=row[3],
                        transaction_id=row[4],
                        category=row[5]
                    ))

                logger.info(
                    f"Retrieved {len(transactions)} transactions from database")
                return transactions

        except sqlite3.Error as e:
            logger.error(f"Failed to retrieve transactions: {e}")
            raise

    def cleanup_old_data(self, cutoff_date: date):
        """Remove data older than specified date."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute('''
                    DELETE FROM transactions WHERE date < ?
                ''', (cutoff_date.isoformat(),))

                deleted_count = cursor.rowcount
                conn.commit()

                logger.info(f"Cleaned up {deleted_count} old transactions")

        except sqlite3.Error as e:
            logger.error(f"Failed to cleanup old data: {e}")
            raise


class BudgetAnalyzer:
    """
    Enhanced budget analyzer with database persistence and multiple report types.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bank_api = BankAPI(cfg)
        self.db = DatabaseManager(cfg.general.db_path)
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

    def _categorize_transactions(self, transactions: List[Transaction]) -> None:
        """Categorize transactions based on description keywords."""
        for tx in transactions:
            if tx.category:
                continue  # Already categorized

            desc_lower = tx.description.lower()
            tx.category = next(
                (category for keyword, category in self.cfg.general.categories.items()
                 if keyword.lower() in desc_lower),
                "Uncategorized"
            )

    def _parse_api_transactions(
        self,
        api_transactions: List[Dict[str, Any]],
        account_id: str
    ) -> List[Transaction]:
        """Parse API transaction data into Transaction objects."""
        transactions = []

        for tx_data in api_transactions:
            try:
                # Parse amount
                amount_data = tx_data.get("transactionAmount", {})
                amount = float(amount_data.get("amount", 0))

                # Parse date
                booking_date = tx_data.get("bookingDate")
                if booking_date:
                    tx_date = datetime.fromisoformat(booking_date).date()
                else:
                    continue  # Skip transactions without date

                # Parse description
                description = tx_data.get(
                    "remittanceInformationUnstructured", "")
                if not description:
                    description = tx_data.get(
                        "additionalInformation", "Unknown transaction")

                # Create transaction
                transaction = Transaction(
                    date=tx_date,
                    amount=amount,
                    description=description,
                    account_id=account_id,
                    transaction_id=tx_data.get(
                        "transactionId", str(uuid.uuid4()))
                )

                transactions.append(transaction)

            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Failed to parse transaction: {e}")
                continue

        return transactions

    def run_budget(self, target_month: Optional[date] = None) -> List[Transaction]:
        """
        Run budget analysis for a specific month.

        Args:
            target_month: Optional target month. If None, uses current month.

        Returns:
            List of transactions for the period.
        """
        start_date, end_date = self._get_date_range(target_month)

        logger.info(
            f"Running budget analysis for period: {start_date} to {end_date}")

        # Try to get transactions from database first
        db_transactions = self.db.get_transactions(start_date, end_date)

        # If no data in database or running for current month, fetch from API
        if not db_transactions or target_month is None:
            try:
                accounts = self.bank_api.list_accounts()
                all_transactions = []

                for account in accounts:
                    account_id = account["AccountId"]
                    api_transactions = self.bank_api.get_transactions(
                        account_id, start_date, end_date
                    )

                    parsed_transactions = self._parse_api_transactions(
                        api_transactions, account_id
                    )
                    all_transactions.extend(parsed_transactions)

                # Categorize and save transactions
                self._categorize_transactions(all_transactions)
                self.db.save_transactions(all_transactions)

                return all_transactions

            except BankAPIError as e:
                logger.error(f"API fetch failed: {e}")
                if db_transactions:
                    logger.info("Falling back to database data")
                    return db_transactions
                raise

        return db_transactions

    def init_database(self, dry_run: bool = False):
        """
        Initialize database with past year of transaction data.

        Args:
            dry_run: If True, only simulate the operation without saving data.
        """
        logger.info("Initializing database with historical data...")

        # Calculate date range for past year
        today = date.today()
        start_date = today - relativedelta(years=1)

        # Clean up old data first
        if not dry_run:
            self.db.cleanup_old_data(start_date)

        try:
            accounts = self.bank_api.list_accounts()
            total_transactions = 0

            for account in accounts:
                account_id = account["AccountId"]
                logger.info(f"Processing account {account_id}")

                # Fetch transactions in monthly chunks to avoid large API calls
                current_date = start_date
                while current_date < today:
                    month_end = min(
                        current_date +
                        relativedelta(months=1) - timedelta(days=1),
                        today
                    )

                    try:
                        api_transactions = self.bank_api.get_transactions(
                            account_id, current_date, month_end
                        )

                        parsed_transactions = self._parse_api_transactions(
                            api_transactions, account_id
                        )

                        self._categorize_transactions(parsed_transactions)

                        if not dry_run:
                            saved_count = self.db.save_transactions(
                                parsed_transactions)
                            total_transactions += saved_count
                        else:
                            total_transactions += len(parsed_transactions)

                        logger.info(
                            f"Processed {len(parsed_transactions)} transactions for {current_date.strftime('%Y-%m')}")

                    except BankAPIError as e:
                        logger.error(
                            f"Failed to fetch transactions for {current_date}: {e}")

                    current_date = month_end + timedelta(days=1)

            action = "Would process" if dry_run else "Processed"
            logger.info(
                f"{action} {total_transactions} historical transactions")

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
            template = self.jinja_env.get_template("report_basic.html")
            return template.render(**template_data)

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
            msg["To"] = self.cfg.credentials.email_from

            html_part = MIMEText(html_content, "html", "utf-8")
            msg.attach(html_part)

            with smtplib.SMTP(self.cfg.general.smtp_server, self.cfg.general.smtp_port) as server:
                server.starttls()
                server.login(self.cfg.credentials.smtp_user,
                             self.cfg.credentials.smtp_password)
                server.send_message(
                    msg, to_addrs=self.cfg.credentials.email_to)

            logger.info("Email report sent successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to send email report: {e}")
            return False

    def run_full_analysis(
        self,
        target_month: Optional[date] = None,
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
            # Run budget analysis
            transactions = self.run_budget(target_month)

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

            with open("data/report.html", "w", encoding="utf-8") as f:
                f.write(html_report)

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

    parser = argparse.ArgumentParser(description="Enhanced Budget Analyzer")
    parser.add_argument("--config", default="config",
                        help="Configuration directory with general.json and credentials.json")
    parser.add_argument("--init-db", action="store_true",
                        help="Initialize database with historical data")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run in dry-run mode")
    parser.add_argument("--report-type", choices=["monthly", "quarterly", "yearly"],
                        default="monthly", help="Type of report to generate")
    parser.add_argument("--no-email", action="store_true",
                        help="Don't send email report")
    parser.add_argument(
        "--target-month", help="Target month in YYYY-MM format")

    args = parser.parse_args()

    try:
        # Load configuration
        cfg = Config.load(args.config)

        # update logging configuration
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
            send_email=not args.no_email
        )

        if results["success"]:
            logger.info(
                f"Analysis completed successfully: {results['transactions_count']} transactions processed")
        else:
            logger.error(f"Analysis failed: {results['error']}")

    except Exception as e:
        logger.error(f"Application failed: {e}")
        raise


if __name__ == "__main__":
    main()
