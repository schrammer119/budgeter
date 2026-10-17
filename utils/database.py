
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import List

from utils.classes import Transaction

logger = logging.getLogger(__name__)


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
                        merchant TEXT,
                        match_score REAL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                columns = {
                    row[1] for row in conn.execute(
                        "PRAGMA table_info(transactions)").fetchall()
                }
                if "category" not in columns:
                    conn.execute(
                        "ALTER TABLE transactions ADD COLUMN category TEXT")
                if "merchant" not in columns:
                    conn.execute(
                        "ALTER TABLE transactions ADD COLUMN merchant TEXT")
                if "match_score" not in columns:
                    conn.execute(
                        "ALTER TABLE transactions ADD COLUMN match_score REAL")

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
                            (date, amount, description, account_id, transaction_id)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (
                            tx.date.isoformat(),
                            tx.amount,
                            tx.description,
                            tx.account_id,
                            tx.transaction_id
                        ))
                        saved_count += 1
                    except sqlite3.IntegrityError:
                        # Transaction already exists
                        pass

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
                    SELECT date, amount, description, account_id, transaction_id,
                              category, merchant, match_score
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
                        category=row[5],
                        merchant=row[6],
                        match_score=row[7]
                    ))

                logger.info(
                    f"Retrieved {len(transactions)} transactions from database")
                return transactions

        except sqlite3.Error as e:
            logger.error(f"Failed to retrieve transactions: {e}")
            raise

    def update_transaction_categories(self, transactions: List[Transaction]) -> int:
        """Update categorization fields for the supplied transactions only."""
        updated_count = 0

        try:
            with sqlite3.connect(self.db_path) as conn:
                for tx in transactions:
                    cursor = conn.execute('''
                        UPDATE transactions
                                                SET category = ?, merchant = ?, match_score = ?,
                                                        updated_at = CURRENT_TIMESTAMP
                        WHERE transaction_id = ?
                                        ''', (tx.category, tx.merchant, tx.match_score,
                                                    tx.transaction_id))
                    updated_count += cursor.rowcount

                conn.commit()

        except sqlite3.Error as e:
            logger.error(f"Failed to update transaction categories: {e}")
            raise

        logger.info(f"Updated categories for {updated_count} transactions")
        return updated_count

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
