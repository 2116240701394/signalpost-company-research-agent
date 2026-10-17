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

Clone or download this repository, then open a terminal in the project folder.

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Run the Agent

Execute:

```bash
python main.py
```

The agent discovers candidate companies, retrieves available registry records, and saves the results to the SQLite database.

## Database

The application uses `companies.db` to store company records and related research data. The exact tables and fields are defined by the application's database schema.

To inspect the database, run:

```bash
python check_db.py
python inspect_data.py
```

If included in the project, `quality_check.py` can be used for additional basic database checks.

## Data Sources and Limitations

The primary data source is the official Brønnøysund Register Centre API. Available fields depend on what the registry provides. A successfully retrieved record does not guarantee that every possible company fact is available or independently verified.

Source links and retrieval timestamps should be checked before relying on individual facts. The application should not be treated as an independent verification of financial information unless that verification is implemented explicitly.

## Current Test Run

Record the results from your latest successful run here:

- Companies in database: 1,020
- Current facts stored: 11,050
- History entries: 4,065
- Failed or skipped records: 0

These counts describe the reported run and may change after subsequent updates.

## Project Files

- `main.py` — main application entry point.
- `requirements.txt` — Python dependencies.
- `companies.db` — SQLite database, if included in the submission.
- `check_db.py` — database inspection/checking utility.
- `inspect_data.py` — data inspection utility.
- `quality_check.py` — additional basic data-quality checks, if included.

## License

Specify the license applicable to this project before redistributing it.
t

