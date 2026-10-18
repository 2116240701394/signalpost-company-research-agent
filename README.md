# Signalpost Company Research Agent

## Overview

Signalpost Company Research Agent is a Python application that discovers and refreshes Norwegian company records using the official Brønnøysund Register Centre API. It stores company profiles, structured facts, and historical changes in an SQLite database.

## Features

- Discovers Norwegian companies through the official registry API.
- Retrieves and validates company records.
- Stores company profiles and structured facts in SQLite.
- Tracks newly discovered and changed facts in a history log.
- Refreshes existing records when the agent runs again.
- Records source information and retrieval timestamps where supported.

## Technology Stack

- **Programming language:** Python
- **HTTP requests:** Requests
- **Database:** SQLite
- **Data source:** Brønnøysund Register Centre API

Official API documentation: https://data.brreg.no/enhetsregisteret/api/dokumentasjon/

## Requirements

- Python 3.10 or a compatible version.
- Internet access to connect to the registry API.
- Windows, macOS, or Linux.

## Installation

Clone or download the repository and open a terminal in the project directory.

Install the required dependencies:

```bash
python -m pip install -r requirements.txt
```

## Running the Agent

Run the following command:

```bash
python main.py
```

The agent discovers candidate companies, retrieves available registry records, and stores the results in the SQLite database.

## Database Inspection

The application stores company records, current facts, and research history in `companies.db`.

To inspect the database, run:

```bash
python check_db.py
```

To inspect stored company records, run:

```bash
python inspect_data.py
```

If `quality_check.py` is included in the repository, run additional data-quality checks with:

```bash
python quality_check.py
```

## Current Test Run

The latest reported run produced the following results:

| Metric | Result |
|---|---:|
| Companies in database | 1,020 |
| Current facts stored | 11,050 |
| History entries | 4,065 |
| Failed or skipped records | 0 |

These figures reflect the reported test run and may change after subsequent updates.

## Expected Run Cost

The application is designed to run locally without paid software or AI services.

| Component | Estimated Cost |
|---|---:|
| Public registry API | ₹0 |
| SQLite database | ₹0 |
| Python and open-source dependencies | ₹0 |
| Paid AI/LLM services | ₹0 |
| Cloud hosting for local execution | ₹0 |
| **Total estimated software cost per run** | **₹0** |

This estimate assumes local execution, use of the public registry API without a paid subscription, and local database storage. Electricity, internet access, computer hardware, and any future paid services are excluded.

## Data Sources and Limitations

The primary data source is the official Brønnøysund Register Centre API. The available information depends on the records provided by the registry.

Retrieving a company record does not guarantee that every possible company fact is available or independently verified. Source links and retrieval timestamps should be checked before relying on individual facts. Financial information should not be treated as independently verified unless the application explicitly implements that verification.

## Project Files

- `main.py` — Main application entry point.
- `requirements.txt` — Python dependencies.
- `companies.db` — SQLite database, if included in the repository.
- `check_db.py` — Database inspection utility.
- `inspect_data.py` — Stored-record inspection utility.
- `quality_check.py` — Additional data-quality checks, if included.

## License

No license has been specified yet. Add an appropriate license before permitting others to reuse or redistribute the project.



