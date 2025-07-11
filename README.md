# Budget Analyzer

This tool fetches your bank transactions monthly (25th–24th), categorizes them, generates an HTML report, and emails it.

## Setup

1. Clone the repo.
2. Create and activate a virtual environment:

        python3 -m venv venv source venv/bin/activate (or venv\Scripts\activate on Windows)
3. Install dependencies:

        pip install -r req.txt
4. Copy `config.json` and fill in your API credentials, email settings, and category mapping.
5. Create a `templates/report.html` file for the Jinja2 template.

## Scheduling

On Windows, use the provided `scheduler.bat` in Task Scheduler to run monthly on the 25th.

## Testing

- Swap real API credentials with sandbox ones.
- Write unit tests for each method in `budget_analyzer.py`.
- Mock HTTP calls with `responses` or `requests-mock`.

