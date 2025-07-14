
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from utils.classes import Config

logger = logging.getLogger(__name__)


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
