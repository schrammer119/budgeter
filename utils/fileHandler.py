#!/usr/bin/env python3
"""
File Handler for Budget Analyzer
================================

Handles importing and parsing bank statement files from the input/ directory.
Supports both .xls and .csv formats with automatic processing.

Author: Alexander Schramm
License: MIT
"""

import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime
import pandas as pd
from utils.classes import Transaction

logger = logging.getLogger(__name__)


class FileHandler:
    """
    Handles importing and parsing bank statement files.
    """

    def __init__(self, input_dir: str = "input", processed_dir: str = "processed"):
        """
        Initialize the file handler.

        Args:
            input_dir: Directory containing unprocessed bank statement files
            processed_dir: Directory to move processed files to
        """
        self.input_dir = Path(input_dir)
        self.processed_dir = Path(processed_dir)

        # Create directories if they don't exist
        self.input_dir.mkdir(exist_ok=True)
        self.processed_dir.mkdir(exist_ok=True)

        # Track processed files to avoid duplicates
        self.processed_files: Dict[str, str] = {}  # filename -> file_hash

    def _calculate_file_hash(self, file_path: Path) -> str:
        """Calculate SHA-256 hash of file content."""
        hash_sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)
        return hash_sha256.hexdigest()

    def _parse_csv_file(self, file_path: Path) -> List[Transaction]:
        """Parse CSV bank statement file."""
        transactions = []

        try:
            # Read the CSV file, skipping the header row
            df = pd.read_csv(file_path, skiprows=1, encoding='utf-8')

            # Handle potential encoding issues
            if df.empty:
                try:
                    df = pd.read_csv(file_path, skiprows=1, encoding='latin-1')
                except:
                    df = pd.read_csv(file_path, skiprows=1, encoding='cp1252')

            for _, row in df.iterrows():
                try:
                    # Parse the transaction data based on the format shown in the CSV
                    booking_date = pd.to_datetime(
                        row['Bokföringsdatum']).date()
                    description = str(row['Text']).strip()
                    amount_str = str(row['Insättning/Uttag']).replace(',', '.')

                    # Skip empty rows
                    if pd.isna(amount_str) or amount_str == 'nan':
                        continue

                    # Parse amount - handle both positive and negative values
                    if amount_str.startswith('-'):
                        amount = -float(amount_str[1:])
                    else:
                        amount = float(amount_str)

                    # Create transaction ID from date and description
                    transaction_id = hashlib.md5(
                        f"{booking_date}_{description}_{amount}".encode(
                            'utf-8')
                    ).hexdigest()

                    transaction = Transaction(
                        date=booking_date,
                        amount=amount,
                        description=description,
                        account_id=file_path.stem,  # Use filename as account ID
                        transaction_id=transaction_id
                    )

                    transactions.append(transaction)

                except (ValueError, KeyError, pd.errors.ParserError) as e:
                    logger.warning(f"Failed to parse row in {file_path}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to parse CSV file {file_path}: {e}")
            raise

        return transactions

    def _parse_xls_file(self, file_path: Path) -> List[Transaction]:
        """Parse XLS bank statement file."""
        transactions = []

        try:
            # Read the Excel file, skipping the header row
            df = pd.read_excel(file_path, skiprows=1)

            for i, row in df.iterrows():
                try:
                    # Parse the transaction data based on the format
                    booking_date = pd.to_datetime(
                        row['Bokföringsdatum']).date()
                    description = str(row['Text']).strip()
                    amount_str = str(row['Insättning/Uttag'])

                    # Skip empty rows
                    if pd.isna(amount_str) or amount_str == 'nan':
                        continue

                    # Parse amount - handle both positive and negative values
                    if isinstance(amount_str, str):
                        amount_str = amount_str.replace(',', '.')
                        if amount_str.startswith('-'):
                            amount = -float(amount_str[1:])
                        else:
                            amount = float(amount_str)
                    else:
                        amount = float(amount_str)

                    # Create transaction ID from date and description
                    transaction_id = hashlib.md5(
                        f"{booking_date}_{description}_{i}_{amount}".encode(
                            'utf-8')
                    ).hexdigest()

                    transaction = Transaction(
                        date=booking_date,
                        amount=amount,
                        description=description,
                        account_id=file_path.stem,  # Use filename as account ID
                        transaction_id=transaction_id
                    )

                    transactions.append(transaction)

                except (ValueError, KeyError, pd.errors.ParserError) as e:
                    logger.warning(f"Failed to parse row in {file_path}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to parse XLS file {file_path}: {e}")
            raise

        return transactions

    def _move_processed_file(self, file_path: Path) -> None:
        """Move processed file to processed directory."""
        try:
            # Create timestamp for processed file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            processed_name = f"{file_path.stem}_{timestamp}{file_path.suffix}"
            processed_path = self.processed_dir / processed_name

            # Move the file
            file_path.rename(processed_path)
            logger.info(f"Moved processed file to {processed_path}")

        except Exception as e:
            logger.error(f"Failed to move processed file {file_path}: {e}")
            # Don't raise - processing was successful, just cleanup failed

    def get_new_files(self) -> List[Path]:
        """Get list of new files to process."""
        new_files = []

        # Look for CSV and XLS files in input directory
        for pattern in ['*.csv', '*.xls', '*.xlsx']:
            for file_path in self.input_dir.glob(pattern):
                if file_path.is_file():
                    # Check if file has been processed before
                    file_hash = self._calculate_file_hash(file_path)

                    if file_path.name not in self.processed_files:
                        new_files.append(file_path)
                        self.processed_files[file_path.name] = file_hash
                    elif self.processed_files[file_path.name] != file_hash:
                        # File has been modified
                        logger.info(
                            f"File {file_path.name} has been modified, reprocessing")
                        new_files.append(file_path)
                        self.processed_files[file_path.name] = file_hash

        return new_files

    def parse_file(self, file_path: Path) -> List[Transaction]:
        """
        Parse a single bank statement file.

        Args:
            file_path: Path to the bank statement file

        Returns:
            List of transactions from the file
        """
        logger.info(f"Parsing file: {file_path}")

        # Determine file type and parse accordingly
        if file_path.suffix.lower() == '.csv':
            transactions = self._parse_csv_file(file_path)
        elif file_path.suffix.lower() in ['.xls', '.xlsx']:
            transactions = self._parse_xls_file(file_path)
        else:
            raise ValueError(f"Unsupported file format: {file_path.suffix}")

        logger.info(
            f"Parsed {len(transactions)} transactions from {file_path}")
        return transactions

    def process_new_files(self) -> List[Transaction]:
        """
        Process all new files in the input directory.

        Returns:
            List of all transactions from new files
        """
        new_files = self.get_new_files()
        all_transactions = []

        if not new_files:
            logger.info("No new files to process")
            return all_transactions

        logger.info(f"Processing {len(new_files)} new files")

        for file_path in new_files:
            try:
                transactions = self.parse_file(file_path)
                all_transactions.extend(transactions)

                # Move processed file
                self._move_processed_file(file_path)

            except Exception as e:
                logger.error(f"Failed to process file {file_path}: {e}")
                continue

        logger.info(
            f"Processed {len(all_transactions)} total transactions from {len(new_files)} files")
        return all_transactions

    def get_file_summary(self) -> Dict[str, Any]:
        """Get summary of files in input directory."""
        files = list(self.input_dir.glob('*'))

        summary = {
            'total_files': len(files),
            'csv_files': len(list(self.input_dir.glob('*.csv'))),
            'xls_files': len(list(self.input_dir.glob('*.xls'))) + len(list(self.input_dir.glob('*.xlsx'))),
            'other_files': len(files) - len(list(self.input_dir.glob('*.csv'))) - len(list(self.input_dir.glob('*.xls'))) - len(list(self.input_dir.glob('*.xlsx'))),
            'processed_files': len(self.processed_files)
        }

        return summary
