# Signalpost Company Research Agent

## Overview

Signalpost Company Research Agent is a Python application that discovers and refreshes Norwegian company records using the official Brønnøysund Register Centre API. It stores company profiles, current facts, and changes over time in an SQLite database.

## Features

- Discovers candidate Norwegian companies through the official registry API.
- Fetches and validates company records.
- Stores company profiles and structured facts in SQLite.
- Tracks new and changed facts in a history log.
- Refreshes existing records when the agent runs again.
- Records source information and retrieval timestamps where supported by the application.

## Technology Stack

- Python
- Requests
- SQLite
- Brønnøysund Register Centre API

Official API documentation: https://data.brreg.no/enhetsregisteret/api/dokumentasjon/

## Requirements

- Python 3.10 or a compatible version supported by the project.
- Internet access to reach the registry API.
- Windows, macOS, or Linux.

## Installation

Clone or download this repository and open a terminal in the project folder.

Install the dependencies:

```bash
python -m pip install -r requirements.txt
```

## Run the Agent

Execute:

```bash
python main.py
```

The agent discovers candidate companies, retrieves available registry records, and saves the results to the SQLite database.

## Database Inspection

The application uses `companies.db` to store company records and related research data. The exact tables and fields are defined by the application's database schema.

Inspect the database by running:

```bash
python check_db.py
```

Inspect stored records by running:

```bash
python inspect_data.py
```

If `quality_check.py` is included in the repository, run it for additional basic data-quality checks:

```bash
python quality_check.py
```

## Data Sources and Limitations

The primary data source is the official Brønnøysund Register Centre API. Available fields depend on the information provided by the registry. A successfully retrieved record does not guarantee that every possible company fact is available or independently verified.

Source links and retrieval timestamps should be checked before relying on individual facts. The application should not be treated as an independent verification of financial information unless that verification is explicitly implemented.

## Current Test Run

The latest reported successful run produced these results:

- Companies in database: 1,020
- Current facts stored: 11,050
- History entries: 4,065
- Failed or skipped records: 0

These counts describe the reported run and may change after subsequent updates.

## Project Files

- `main.py` — main application entry point.
- `requirements.txt` — Python dependencies.
- `companies.db` — SQLite database, if included in the submission.
- `check_db.py` — database inspection utility.
- `inspect_data.py` — stored-data inspection utility.
- `quality_check.py` — additional data-quality checks, if included.

## License

No license has been specified yet. Add an appropriate license before permitting others to reuse or redistribute the project.



