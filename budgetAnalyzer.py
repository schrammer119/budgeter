#!/usr/bin/env python3

import json
import logging
import requests
import smtplib
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from jinja2 import Environment, FileSystemLoader
from typing import Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


@dataclass
class Transaction:
    date: date
    amount: float
    description: str
    category: Optional[str] = None


@dataclass
class Config:
    # AIS v2 / OAuth2
    token_url: str
    api_base: str
    client_id: str
    client_secret: str
    fapi_financial_id: str
    consent_id: Optional[str] = None
    psu_ip: str = "127.0.0.1"
    psu_ua: str = "BudgetAnalyzer/1.0"
    # SMTP / Email
    smtp_server: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    email_from: str
    email_to: str
    # Categorization keywords
    categories: Dict[str, str] = field(default_factory=dict)

    @staticmethod
    def load(path: str = "config.json") -> "Config":
        try:
            with open(path, "r") as f:
                data = json.load(f)
            return Config(**data)
        except Exception as e:
            logging.error(f"Failed to load config: {e}")
            raise


class BankAPI:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._token: Optional[str] = None
        self._token_expiry: datetime = datetime.min

    def authenticate(self) -> str:
        if self._token and datetime.utcnow() < self._token_expiry:
            return self._token

        data = {
            "grant_type":    "client_credentials",
            "scope":         "AIS",
            "client_id":     self.cfg.client_id,
            "client_secret": self.cfg.client_secret,
        }
        headers = {
            "Content-Type":       "application/x-www-form-urlencoded",
            "x-fapi-financial-id": self.cfg.fapi_financial_id,
        }

        r = requests.post(self.cfg.token_url, data=data,
                          headers=headers, timeout=10)
        r.raise_for_status()
        tok = r.json()
        self._token = tok["access_token"]
        self._token_expiry = datetime.utcnow() + \
            timedelta(seconds=tok.get("expires_in", 300))
        return self._token

    def _headers(self) -> Dict[str, str]:
        h = {
            "Authorization":      f"Bearer {self.authenticate()}",
            "x-fapi-financial-id": self.cfg.fapi_financial_id,
            "X-Request-ID":        str(uuid.uuid4()),
            "Accept":              "application/json",
            "PSU-IP-Address":      self.cfg.psu_ip,
            "PSU-User-Agent":      self.cfg.psu_ua,
        }
        if self.cfg.consent_id:
            h["Consent-ID"] = self.cfg.consent_id
        return h

    def list_accounts(self) -> List[Dict]:
        url = f"{self.cfg.api_base}/accounts"
        r = requests.get(url, headers=self._headers(), timeout=20)
        r.raise_for_status()
        return r.json()["Data"]["Account"]

    def get_transactions(
        self, account_id: str, start: date, end: date
    ) -> List[Dict]:
        url = f"{self.cfg.api_base}/accounts/{account_id}/transactions"
        params = {
            "dateFrom":      start.isoformat(),
            "dateTo":        end.isoformat(),
            "bookingStatus": "booked,pending"
        }

        all_tx: List[Dict] = []
        while url:
            r = requests.get(url, headers=self._headers(),
                             params=params if all_tx == [] else None, timeout=30)
            r.raise_for_status()
            data = r.json()["Data"]["transactions"]
            all_tx += data.get("booked", []) + data.get("pending", [])
            # paging
            next_link = data.get("_links", {}).get("next", {}).get("href")
            url = next_link if next_link else None

        return


class BudgetAnalyzer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bank = BankAPI(cfg)
        self.env = Environment(loader=FileSystemLoader("templates"))

    def _get_date_range(self) -> Tuple[date, date]:
        today = date.today()
        start = (today.replace(day=25) - relativedelta(months=1))
        end = today.replace(day=24)
        logging.info(f"Running for period: {start} → {end}")
        return start, end

    def run(self):
        start, end = self._get_date_range()

        accounts = self.bank.list_accounts()
        all_txs: List[Transaction] = []
        for acct in accounts:
            acct_id = acct["AccountId"]
            all_txs.extend(self.bank.get_transactions(acct_id, start, end))

        self._categorize(all_txs)
        html = self._render_report(all_txs, start, end)
        subject = f"Budget Report {start.isoformat()}–{end.isoformat()}"
        self._send_email(html, subject)

    def _categorize(self, txs: List[Transaction]) -> None:
        for tx in txs:
            desc = tx.description.lower()
            tx.category = next(
                (cat for kw, cat in self.cfg.categories.items() if kw in desc),
                "Uncategorized"
            )

    def _render_report(
        self, txs: List[Transaction], start: date, end: date
    ) -> str:
        template = self.env.get_template("report.html")
        grouped: Dict[str, List[Transaction]] = {}
        for tx in txs:
            grouped.setdefault(tx.category, []).append(tx)
        return template.render(
            start=start, end=end, generated_on=datetime.now(), groups=grouped
        )

    def _send_email(self, html: str, subject: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self.cfg.email_from
        msg["To"] = self.cfg.email_to
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(self.cfg.smtp_server, self.cfg.smtp_port) as s:
            s.starttls()
            s.login(self.cfg.smtp_user, self.cfg.smtp_password)
            s.send_message(msg)
        logging.info("Report emailed successfully")


if __name__ == "__main__":
    cfg = Config.load("config.json")
    BudgetAnalyzer(cfg).run()
