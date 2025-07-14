import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional, Any, List

__version__ = "0.0.4"
LOG_FILE_PATH = "logs/budget_analyzer.log"

logger = logging.getLogger(__name__)


@dataclass
class Transaction:
    """Represents a bank transaction with optional categorization."""
    date: date
    amount: float
    description: str
    account_id: str
    transaction_id: str
    category: Optional[str] = None
    merchant: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert transaction to dictionary for JSON serialization."""
        return {
            'date': self.date.isoformat(),
            'amount': self.amount,
            'description': self.description,
            'account_id': self.account_id,
            'transaction_id': self.transaction_id,
            'category': self.category,
            'merchant': self.merchant
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
            category=data.get('category'),
            merchant=data.get('merchant')
        )


@dataclass
class GeneralConfig:
    """General configuration settings (non-sensitive)."""
    # Environment settings
    sandbox_mode: bool = False

    # Database Configuration
    db_path: str = "data/budget_data.db"

    # File processing settings
    input_dir: str = "input"
    processed_dir: str = "processed"

    # Categorization
    merchant_categories: Dict[str, str] = field(default_factory=lambda: {
        "espresso house":           "Coffee & Bakery",
        "ica maxi":                 "Groceries",
        "coop":                     "Groceries",
        "hemköp":                   "Groceries",
        "trattoria bonasera":       "Dining Out",
        "systembolaget":            "Alcohol & Spirits",
        "apoteket":                 "Pharmacy & Health",
        "fahlmans konditori":       "Coffee & Bakery",
        "subway":                   "Dining Out",
        "mcdonalds":                "Dining Out",
        "preem":                    "Fuel & Gas",
        "shell":                    "Fuel & Gas",
        "circle k":                 "Fuel & Gas",
        "öresundkraft":             "Utilities",
        "spotify":                  "Subscriptions",
        "disney plus":              "Subscriptions",
        "amazon":                   "Shopping",
        "ebay":                     "Shopping",
        "ikea":                     "Shopping",
        "rikshem":                  "Housing & Rent",
        "ownit":                    "Financial Services",
        "klarna":                   "Financial Services",
        "lön":                      "Income",
        "fkassa":                   "Income",
        "biljettköp":               "Travel & Tickets",
        "ryanair":                  "Travel & Tickets",
        "taxi":                     "Transport",
        "voi":                      "Transport",
        "tier":                     "Transport",
    })
    merchant_keys: List[str] = field(default_factory=lambda: list)
    category_threshold: int = 75

    # Reporting
    templates_dir: str = "config"

    # Request settings (kept for backward compatibility)
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

    def __post_init__(self):
        self.merchant_keys = list(self.merchant_categories.keys())


@dataclass
class CredentialsConfig:
    """Sensitive configuration settings (credentials, API keys, etc.)."""
    # Note: Bank API fields kept for backward compatibility but not used
    token_url: str = "https://auth.lansforsakringar.se/oauth2/token"
    api_base: str = "https://api.lansforsakringar.se/v2"
    client_id: str = "not_used_with_file_import"
    client_secret: str = "not_used_with_file_import"
    fapi_financial_id: str = "not_used_with_file_import"
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

        # Check if email credentials need to be configured
        if (credentials_config.smtp_user == "your_email@gmail.com" or
                credentials_config.smtp_password == "your_app_password_here"):
            logger.warning(
                "⚠️  Email credentials contain default values!")
            logger.warning(
                f"Please edit {credentials_file} with your actual email credentials for report sending")

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
                    "Note: Bank API credentials are no longer used - the app now imports files directly",
                    "Only email credentials are needed for report sending:",
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
