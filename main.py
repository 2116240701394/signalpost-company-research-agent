"""
Signalpost Company Research Agent
---------------------------------
Refreshes existing Norwegian company records and discovers new candidates
from the official Brønnøysund Register Centre API.

Run from this folder:
    python main.py

Install dependency:
    python -m pip install requests
"""

import json
import re
import sqlite3
import time
from inspect_data import inspect_database
from datetime import datetime, timezone
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).resolve().parent / "companies.db"

API_URL = "https://data.brreg.no/enhetsregisteret/api/enheter"
RECORD_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{}"

HEADERS = {
    "User-Agent": "SignalpostCompanyResearch/1.0",
    "Accept": "application/json",
}

REQUEST_DELAY = 0.2
TIMEOUT = 30
DISCOVERY_TARGET = 20
DISCOVERY_PAGE_SIZE = 100
DISCOVERY_MAX_PAGES = 50

# Internal fact name -> field name returned by the official API.
FACT_FIELDS = {
    "website": "hjemmeside",
    "organisation_type": "organisasjonsform",
    "business_address": "forretningsadresse",
    "industry": "naeringskode1",
    "employee_count": "antallAnsatte",
    "vat_registered": "registrertIMva",
    "bankruptcy": "konkurs",
    "under_closure": "underAvvikling",
    "under_forced_liquidation": "underTvangsavvikling",
    "registered_date": "registreringsdatoEnhetsregisteret",
    "established_date": "stiftelsesdato",
}


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def utc_now():
    """Return the current UTC time as an ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()


def normalized_json(fact_name, value):
    """Serialize a normalized value consistently, regardless of key order."""
    return json.dumps(
        normalize_value(fact_name, value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def normalize_address(value):
    """Normalize API addresses and older comma-separated address strings."""
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        return {
            "address": [parts[0]] if parts and parts[0] else [],
            "municipality": None,
            "municipality_number": None,
            "country": None,
            "country_code": None,
            "postal_code": parts[1] if len(parts) >= 3 else None,
            "postal_place": parts[2] if len(parts) >= 3 else None,
        }

    if not isinstance(value, dict):
        return value

    address = value.get("adresse", value.get("address", []))
    if isinstance(address, str):
        address = [address]
    if address is None:
        address = []

    return {
        "address": address,
        "municipality": value.get("kommune") or value.get("municipality"),
        "municipality_number": (
            value.get("kommunenummer") or value.get("municipality_number")
        ),
        "country": value.get("land") or value.get("country"),
        "country_code": value.get("landkode") or value.get("country_code"),
        "postal_code": value.get("postnummer") or value.get("postal_code"),
        "postal_place": value.get("poststed") or value.get("postal_place"),
    }


def normalize_value(fact_name, value):
    """Canonicalize values from the API or older database rows."""
    if fact_name == "business_address":
        return normalize_address(value)

    if isinstance(value, dict):
        if fact_name in ("industry", "organisation_type"):
            return {
                "code": value.get("code") or value.get("kode"),
                "description": value.get("description") or value.get("beskrivelse"),
            }

        return {
            key: normalize_value(key, item)
            for key, item in sorted(value.items())
            if key != "_links"
        }

    if isinstance(value, list):
        return [normalize_value(fact_name, item) for item in value]

    return value


def parse_stored_value(value):
    """Decode JSON from SQLite, leaving ordinary strings unchanged."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def values_equivalent(fact_name, old_value, new_value):
    """Compare normalized values and tolerate older address formats."""
    old_normalized = normalize_value(fact_name, parse_stored_value(old_value))
    new_normalized = normalize_value(fact_name, parse_stored_value(new_value))

    if old_normalized == new_normalized:
        return True

    # Older address strings may not contain municipality/country fields.
    if fact_name == "business_address":
        if not isinstance(old_normalized, dict) or not isinstance(new_normalized, dict):
            return False
        if not old_normalized.get("address") or not new_normalized.get("address"):
            return False

        for field in (
            "address",
            "postal_code",
            "postal_place",
            "municipality",
            "municipality_number",
            "country",
            "country_code",
        ):
            old_field = old_normalized.get(field)
            if old_field not in (None, "") and old_field != new_normalized.get(field):
                return False
        return True

    return False


def get_table_info(conn, table_name):
    """Return SQLite column metadata for a table."""
    return conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()


def get_table_columns(conn, table_name):
    """Return the names of columns in a table."""
    return {row[1] for row in get_table_info(conn, table_name)}


def choose_column(columns, candidates):
    """Choose the first supported column name from known variants."""
    for candidate in candidates:
        if candidate in columns:
            return candidate
    return None


def insert_row(conn, table_name, values):
    """Insert only columns that exist in the destination table."""
    columns = get_table_columns(conn, table_name)
    selected = [(key, value) for key, value in values.items() if key in columns]

    if not selected:
        raise sqlite3.OperationalError(
            f"No compatible columns found in table {table_name!r}"
        )

    column_sql = ", ".join(f'"{column}"' for column, _ in selected)
    placeholders = ", ".join("?" for _ in selected)
    conn.execute(
        f'INSERT INTO "{table_name}" ({column_sql}) VALUES ({placeholders})',
        [value for _, value in selected],
    )


# ---------------------------------------------------------------------------
# Database setup and compatibility
# ---------------------------------------------------------------------------

def initialize_database():
    """
    Create missing tables without dropping or replacing existing tables.

    Existing databases may have additional columns, including profile_json.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                org_number TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_url TEXT,
                retrieved_at TEXT
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_facts (
                org_number TEXT NOT NULL,
                fact_name TEXT NOT NULL,
                fact_value TEXT,
                source_url TEXT,
                retrieved_at TEXT,
                PRIMARY KEY (org_number, fact_name)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_fact_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_number TEXT NOT NULL,
                fact_name TEXT NOT NULL,
                change_type TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                source_url TEXT,
                retrieved_at TEXT
            )
        """)


def get_existing_org_numbers():
    """Read existing company IDs, supporting common schema variants."""
    with sqlite3.connect(DB_PATH) as conn:
        columns = get_table_columns(conn, "companies")
        org_col = choose_column(columns, ["org_number", "organisation_number"])
        if not org_col:
            raise sqlite3.OperationalError(
                "The companies table needs org_number or organisation_number."
            )
        rows = conn.execute(
            f'SELECT "{org_col}" FROM companies ORDER BY "{org_col}"'
        ).fetchall()

    return [str(row[0]).strip() for row in rows if row[0] is not None]


def get_fact_values(conn, org_number):
    """Return currently stored facts for one organisation."""
    columns = get_table_columns(conn, "company_facts")
    org_col = choose_column(columns, ["org_number", "organisation_number"])
    name_col = choose_column(columns, ["fact_name", "name"])
    value_col = choose_column(columns, ["fact_value", "value"])

    if not all((org_col, name_col, value_col)):
        raise sqlite3.OperationalError(
            "company_facts needs organisation number, fact name and value columns."
        )

    rows = conn.execute(
        f'SELECT "{name_col}", "{value_col}" FROM company_facts '
        f'WHERE "{org_col}" = ?',
        (org_number,),
    ).fetchall()
    return dict(rows)


def record_history(
    conn,
    org_number,
    fact_name,
    change_type,
    old_value,
    new_value,
    source_url,
    retrieved_at,
):
    """Append a history entry using the available column names."""
    columns = get_table_columns(conn, "company_fact_history")
    values = {
        "org_number": org_number,
        "organisation_number": org_number,
        "fact_name": fact_name,
        "change_type": change_type,
        "old_value": old_value,
        "new_value": new_value,
        "source_url": source_url,
        "source": source_url,
        "retrieved_at": retrieved_at,
        "timestamp": retrieved_at,
        "changed_at": retrieved_at,
    }
    selected = [(column, value) for column, value in values.items() if column in columns]

    if not selected:
        raise sqlite3.OperationalError(
            "No compatible columns found in company_fact_history."
        )

    insert_row(conn, "company_fact_history", dict(selected))


def _default_for_required_column(column_name, record, org_number, company_name,
                                 source_url, retrieved_at):
    """Provide sensible values for known required legacy columns."""
    name = column_name.lower()

    if name in ("org_number", "organisation_number", "organization_number"):
        return org_number
    if name in ("name", "company_name", "navn", "company"):
        return company_name
    if name in ("source_url", "source", "url"):
        return source_url
    if name in ("retrieved_at", "updated_at", "last_checked_at", "timestamp"):
        return retrieved_at
    if name in ("profile_json", "profile", "record_json", "company_json", "data_json"):
        return json.dumps(record, ensure_ascii=False, sort_keys=True)
    if name in ("created_at",):
        return retrieved_at
    return None


def _company_values_for_schema(conn, org_number, record, source_url, retrieved_at):
    """Build a company row compatible with the actual existing schema."""
    columns_info = get_table_info(conn, "companies")
    columns = {row[1] for row in columns_info}
    org_col = choose_column(columns, ["org_number", "organisation_number"])
    name_col = choose_column(columns, ["name", "company_name", "navn"])

    if not org_col:
        raise sqlite3.OperationalError(
            "companies table is missing org_number/organisation_number."
        )
    if not name_col:
        raise sqlite3.OperationalError(
            "companies table is missing name/company_name/navn."
        )

    company_name = record["navn"]
    full_record_json = json.dumps(record, ensure_ascii=False, sort_keys=True)

    values = {
        org_col: org_number,
        name_col: company_name,
        "source_url": source_url,
        "source": source_url,
        "retrieved_at": retrieved_at,
        "updated_at": retrieved_at,
        "last_checked_at": retrieved_at,
        "created_at": retrieved_at,
        "profile_json": full_record_json,
        "profile": full_record_json,
        "record_json": full_record_json,
        "company_json": full_record_json,
        "data_json": full_record_json,
    }

    # Fill any other NOT NULL/no-default columns only when a reasonable
    # compatible value can be inferred. Do not overwrite unknown columns.
    for row in columns_info:
        column_name = row[1]
        not_null = bool(row[3])
        default_value = row[4]
        is_primary_key = bool(row[5])

        if column_name in values or not not_null or default_value is not None or is_primary_key:
            continue

        inferred = _default_for_required_column(
            column_name, record, org_number, company_name, source_url, retrieved_at
        )
        if inferred is not None:
            values[column_name] = inferred
        else:
            raise sqlite3.OperationalError(
                f"Cannot safely populate required companies column "
                f"{column_name!r}. Check its intended value/schema."
            )

    return {
        column: value for column, value in values.items() if column in columns
    }, org_col


def save_verified_record(org_number, record, source_url):
    """Upsert a verified company and facts; append history only for changes."""
    retrieved_at = utc_now()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        company_values, org_col = _company_values_for_schema(
            conn, org_number, record, source_url, retrieved_at
        )
        existing_company = conn.execute(
            f'SELECT 1 FROM companies WHERE "{org_col}" = ?',
            (org_number,),
        ).fetchone()

        if existing_company:
            update_values = {
                column: value
                for column, value in company_values.items()
                if column != org_col
            }
            if update_values:
                assignments = ", ".join(
                    f'"{column}" = ?' for column in update_values
                )
                conn.execute(
                    f'UPDATE companies SET {assignments} WHERE "{org_col}" = ?',
                    [*update_values.values(), org_number],
                )
        else:
            insert_row(conn, "companies", company_values)

        old_facts = get_fact_values(conn, org_number)

        fact_columns = get_table_columns(conn, "company_facts")
        fact_org_col = choose_column(
            fact_columns, ["org_number", "organisation_number"]
        )
        fact_name_col = choose_column(fact_columns, ["fact_name", "name"])
        fact_value_col = choose_column(fact_columns, ["fact_value", "value"])
        fact_source_col = choose_column(fact_columns, ["source_url", "source"])
        fact_retrieved_col = choose_column(
            fact_columns, ["retrieved_at", "updated_at", "timestamp"]
        )

        if not all((fact_org_col, fact_name_col, fact_value_col)):
            raise sqlite3.OperationalError(
                "company_facts has incompatible column names."
            )

        for fact_name, api_field in FACT_FIELDS.items():
            raw_value = record.get(api_field)
            if raw_value is None:
                # Do not erase a previously known value when the API omits it.
                continue

            new_value = normalized_json(fact_name, raw_value)
            old_value = old_facts.get(fact_name)

            if old_value is None:
                record_history(
                    conn, org_number, fact_name, "NEW", None,
                    new_value, source_url, retrieved_at
                )
            elif not values_equivalent(fact_name, old_value, new_value):
                record_history(
                    conn, org_number, fact_name, "CHANGED", old_value,
                    new_value, source_url, retrieved_at
                )

            existing_fact = conn.execute(
                f'SELECT 1 FROM company_facts WHERE "{fact_org_col}" = ? '
                f'AND "{fact_name_col}" = ?',
                (org_number, fact_name),
            ).fetchone()

            fact_values = {
                fact_org_col: org_number,
                fact_name_col: fact_name,
                fact_value_col: new_value,
            }
            if fact_source_col:
                fact_values[fact_source_col] = source_url
            if fact_retrieved_col:
                fact_values[fact_retrieved_col] = retrieved_at

            if existing_fact:
                update_values = {
                    column: value
                    for column, value in fact_values.items()
                    if column not in (fact_org_col, fact_name_col)
                }
                if update_values:
                    assignments = ", ".join(
                        f'"{column}" = ?' for column in update_values
                    )
                    conn.execute(
                        f'UPDATE company_facts SET {assignments} '
                        f'WHERE "{fact_org_col}" = ? AND "{fact_name_col}" = ?',
                        [*update_values.values(), org_number, fact_name],
                    )
            else:
                insert_row(conn, "company_facts", fact_values)


# ---------------------------------------------------------------------------
# Official API access and validation
# ---------------------------------------------------------------------------

def fetch_company(session, org_number):
    """Fetch and validate a company record by its 9-digit organisation number."""
    org_number = str(org_number).strip()

    if not re.fullmatch(r"\d{9}", org_number):
        print(f"Skipping invalid organisation number: {org_number}")
        return None

    source_url = RECORD_URL.format(org_number)

    try:
        response = session.get(source_url, timeout=TIMEOUT)

        if response.status_code == 404:
            print(f"Company not found: {org_number}")
            return None

        response.raise_for_status()
        record = response.json()

    except (requests.RequestException, ValueError) as error:
        print(f"Request failed for {org_number}: {error}")
        return None

    if not isinstance(record, dict):
        print(f"Invalid response format for {org_number}; record rejected")
        return None

    returned_number = record.get("organisasjonsnummer")
    if returned_number is None or str(returned_number) != org_number:
        print(f"Organisation number mismatch for {org_number}; record rejected")
        return None

    if not isinstance(record.get("navn"), str) or not record["navn"].strip():
        print(f"Missing company name for {org_number}; record rejected")
        return None

    return record, source_url


def discover_new_org_numbers(session, existing_numbers, target_new=DISCOVERY_TARGET):
    """
    Find new candidate IDs from the official paginated list endpoint.
    Each candidate is fetched and validated independently before being saved.
    """
    seen = {str(number).strip() for number in existing_numbers}
    new_numbers = []
    page = 0

    while len(new_numbers) < target_new and page < DISCOVERY_MAX_PAGES:
        try:
            response = session.get(
                API_URL,
                params={"page": page, "size": DISCOVERY_PAGE_SIZE},
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            data = response.json()

        except (requests.RequestException, ValueError) as error:
            print(f"Discovery stopped on page {page}: {error}")
            break

        if not isinstance(data, dict):
            print(f"Unexpected discovery response on page {page}.")
            break

        embedded = data.get("_embedded", {})
        records = embedded.get("enheter", []) if isinstance(embedded, dict) else []

        if not records:
            print(f"No records returned on discovery page {page}.")
            break

        for item in records:
            if not isinstance(item, dict):
                continue

            number = item.get("organisasjonsnummer")
            if number is None:
                continue

            number = str(number).strip()
            if not re.fullmatch(r"\d{9}", number) or number in seen:
                continue

            seen.add(number)
            new_numbers.append(number)
            print(f"Discovered candidate: {number}")

            if len(new_numbers) >= target_new:
                break

        page_info = data.get("page", {})
        total_pages = page_info.get("totalPages") if isinstance(page_info, dict) else None
        page += 1

        if total_pages is not None and page >= total_pages:
            break

        time.sleep(REQUEST_DELAY)

    print(f"New company candidates found: {len(new_numbers)}")
    return new_numbers


# ---------------------------------------------------------------------------
# Main research run
# ---------------------------------------------------------------------------

def run_research():
    initialize_database()

    existing_numbers = get_existing_org_numbers()
    print(f"Companies already in database: {len(existing_numbers)}")

    session = requests.Session()
    session.headers.update(HEADERS)

    succeeded = 0
    failed = 0
    new_numbers = []

    try:
        new_numbers = discover_new_org_numbers(
            session,
            set(existing_numbers),
            target_new=DISCOVERY_TARGET,
        )

        # Refresh existing companies first, then try newly discovered candidates.
        all_numbers = existing_numbers + new_numbers
        new_number_set = set(new_numbers)
        total = len(all_numbers)

        if total == 0:
            print("No company numbers available to research.")
            return

        for index, org_number in enumerate(all_numbers, start=1):
            label = " (new candidate)" if org_number in new_number_set else ""
            print(f"[{index}/{total}] Researching {org_number}{label}")

            result = fetch_company(session, org_number)
            if result is None:
                failed += 1
                time.sleep(REQUEST_DELAY)
                continue

            record, source_url = result

            try:
                save_verified_record(org_number, record, source_url)
                succeeded += 1
            except sqlite3.Error as error:
                failed += 1
                print(f"Database error for {org_number}: {error}")

            time.sleep(REQUEST_DELAY)

    finally:
        session.close()

    with sqlite3.connect(DB_PATH) as conn:
        company_columns = get_table_columns(conn, "companies")
        org_col = choose_column(company_columns, ["org_number", "organisation_number"])
        fact_count = conn.execute("SELECT COUNT(*) FROM company_facts").fetchone()[0]
        history_count = conn.execute(
            "SELECT COUNT(*) FROM company_fact_history"
        ).fetchone()[0]
        company_count = conn.execute(
            f'SELECT COUNT(*) FROM companies'
        ).fetchone()[0] if org_col else 0

    print("\n========== RESEARCH SUMMARY ==========")
    print(f"Records successfully fetched and saved: {succeeded}")
    print(f"Records failed or skipped: {failed}")
    print(f"Companies in database: {company_count}")
    print(f"Current facts stored: {fact_count}")
    print(f"History entries: {history_count}")
    print(f"Database: {DB_PATH}")


if __name__ == "__main__":
    run_research()
    inspect_database()
