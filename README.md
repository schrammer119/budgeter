# Budget Analyzer

A production-ready budget analysis tool that imports bank transaction files, categorizes spending using intelligent fuzzy matching, and generates comprehensive financial reports with database persistence.

## Features

- **File-based Import**: Processes CSV/Excel bank statement files automatically
- **Smart Categorization**: Uses fuzzy matching, regex patterns, and merchant tokens for accurate transaction categorization
- **Database Persistence**: SQLite database for transaction history and deduplication
- **Multiple Report Types**: Monthly, quarterly, and yearly reports with trend analysis
- **Email Delivery**: Automated HTML email reports with transaction logs
- **Budget Cycle**: Analyzes transactions from 25th to 24th of each month
- **Visual Analytics**: Generates spending trend charts and category breakdowns

## Setup

### 1. Clone and Install

```bash
git clone <repository-url>
cd budgeter
```

### 2. Install Dependencies

```bash
pip install -r req.txt
```

### 3. Directory Structure

The application will automatically create the following directories:

```
budgeter/
├── config/          # Configuration files
├── data/            # SQLite database
├── input/           # Place new bank statement files here
├── logs/            # Application logs
├── processed/       # Processed files are moved here
├── reports/         # Generated HTML reports
├── transactions/    # Transaction export files (input)
└── utils/           # Utility libraries
```

### 4. Configuration

The application uses three JSON configuration files in the `config/` directory:

#### `config/credentials.json`

```json
{
  "smtp_user": "your-email@gmail.com",
  "smtp_password": "your-app-password",
  "email_from": "your-email@gmail.com",
  "email_to": ["recipient@example.com"]
}
```

#### `config/general.json`

```json
{
  "db_path": "data/transactions.db",
  "input_dir": "transactions/input",
  "processed_dir": "transactions/processed",
  "templates_dir": "templates",
  "smtp_server": "smtp.gmail.com",
  "smtp_port": 587,
  "log_level": "INFO",
  "category_threshold": 70
}
```

#### `config/categorization.json`

```json
{
  "merchant_categories": {
    "Walmart": "Groceries",
    "Shell": "Transportation",
    "Netflix": "Entertainment"
  },
  "merchant_tokens": {
    "grocery": "Groceries",
    "gas": "Transportation"
  },
  "merchant_regex": [["(coffee|cafe)", "Dining", "coffee shop"]],
  "category_types": {
    "Groceries": "Non-Discretionary",
    "Entertainment": "Discretionary",
    "Mortgage": "Non-Discretionary",
    "Debt Payment": "Debt",
    "Savings": "Savings"
  }
}
```

## Usage

### Basic Usage

Run the analyzer for the current month (or previous month if today is before the 25th):

```bash
python budgetAnalyzer.py
```

This will:

1. Import new transaction files from `transactions/input/`
2. Categorize all transactions
3. Generate the appropriate report type (monthly, quarterly, or yearly based on the month)
4. Send an email report
5. Save the HTML report to `reports/`

### Command Line Options

```bash
python budgetAnalyzer.py [OPTIONS]
```

**Options:**

- `--report-type {monthly,quarterly,yearly}` - Specify report type (default: auto-selected based on month)
- `--target-month YYYY-MM` - Analyze a specific month (default: current/previous month)
- `--no-email` - Generate report without sending email
- `--check-categorization` - Dry run to review categorization without generating report
- `-h, --help` - Show help message

### Examples

#### Dry Run - Check Categorization

Review how transactions will be categorized without generating a full report:

```bash
python budgetAnalyzer.py --check-categorization
```

Output:

```
✓ Processed 127 transactions
✓ Saved to: transactions/2025_01_transactions.xlsx
⚠ 5 uncategorized transactions:
  - UNKNOWN MERCHANT 123
  - ACH TRANSFER XYZ
  ...
⚠ 3 low confidence matches:
  - ABC Store (score: 72.5)
  ...
```

Review the Excel file to verify categorization, then add merchants to your config files as needed.

#### Generate Monthly Report (No Email)

```bash
python budgetAnalyzer.py --report-type monthly --no-email
```

#### Generate Yearly Report for Specific Month

```bash
python budgetAnalyzer.py --report-type yearly --target-month 2024-12
```

#### Review Previous Month's Categorization

```bash
python budgetAnalyzer.py --target-month 2024-11 --check-categorization
```

### Report Type Auto-Selection

By default, the report type is automatically selected based on the month:

- **Monthly**: January, February, April, May, July, August, October, November
- **Quarterly**: March, June, September (includes quarter comparison)
- **Yearly**: December (includes full year analysis with charts)

## File Import

### Supported Formats

- CSV files (`.csv`)
- Excel files (`.xls`, `.xlsx`)

### Import Process

1. Place bank statement files in `input/`
2. Run the analyzer (it will automatically detect and process new files)
3. Processed files are moved to `processed/`
4. Transactions are deduplicated and saved to the database

The file handler automatically detects file formats and extracts transaction data (date, description, amount).

## Output

### Generated Files

- **HTML Reports**: `reports/YYYY_MM_{report_type}_report.html`
- **Transaction Logs**: `transactions/YYYY_MM_transactions.xlsx` (categorized transactions with confidence scores)
- **Charts** (yearly reports only):
  - `reports/spending_type_chart.png`
  - `reports/category_spending_chart.png`

### Email Report

The email includes:

- HTML report with embedded styling
- Transaction log as Excel attachment
- Charts as inline images (yearly reports)

## Scheduling

### Windows Task Scheduler

Use the provided `scheduler.bat` to run automatically on the 25th of each month:

1. Open Task Scheduler
2. Create a new task
3. Set trigger: Monthly, day 25, at your preferred time
4. Set action: Run `scheduler.bat`
5. Configure to run whether user is logged in or not

## Troubleshooting

### Uncategorized Transactions

If you see many uncategorized transactions:

1. Run with `--check-categorization` to review
2. Add merchants to `config/categorization.json`:
   - `merchant_categories` for fuzzy matching
   - `merchant_regex` for pattern-based matching
   - `merchant_tokens` for exact matches
3. Adjust `category_threshold` in `general.json` (lower = more lenient matching)

### Email Issues

- Ensure SMTP credentials are correct
- For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833)
- Check `logs/` directory for detailed error messages

### File Import Failures

- Verify file format (CSV/Excel)
- Check that files contain date, description, and amount columns
- Review logs for parsing errors

## Testing

- Use `--check-categorization` to validate configuration without sending emails
- Use `--no-email` to generate reports locally
- Test with historical data using `--target-month`
