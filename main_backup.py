import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


# =========================================================
# CONFIGURATION
# =========================================================

DB_PATH = Path(__file__).resolve().parent / "companies.db"

API_URL = "https://data.brreg.no/enhetsregisteret/api/enheter"
RECORD_URL = "https://virksomhet.brreg.no/nb/oppslag/enheter/"

HEADERS = {
    "User-Agent": "SignalpostCompanyResearch/1.0",
    "Accept": "application/json",
}

REQUEST_DELAY = 0.2
REQUEST_TIMEOUT = 30

FACT_FIELDS = {
    "website": "hjemmeside",
    "organisation_type": "organisasjonsform",
    "business_address": "forretningsadresse",
    "industry": "naeringskode1",
    "employee_count": "antallAnsatte",
    "vat_registered": "registrertIMvaregisteret",
    "bankruptcy": "konkurs",
    "under_closure": "underAvvikling",
    "under_forced_liquidation":
        "underTvangsavviklingEllerTvangsopplosning",
    "registered_date": "registreringsdatoEnhetsregisteret",
    "established_date": "stiftelsesdato",
}


# =========================================================
# UTILITIES
# =========================================================

def now_utc():
    return datetime.now(timezone.utc).isoformat()


def to_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def valid_org_number(org_number):
    return bool(re.fullmatch(r"\d{9}", str(org_number or "")))


# =========================================================
# NORMALIZE ADDRESSES
# =========================================================

def normalize_address(value):
    """
    Convert Norwegian API addresses, English-keyed addresses,
    and older formatted address strings into a common structure.
    """

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

    return {
        "address": address or [],
        "municipality": (
            value.get("kommune")
            or value.get("municipality")
        ),
        "municipality_number": (
            value.get("kommunenummer")
            or value.get("municipality_number")
        ),
        "country": value.get("land") or value.get("country"),
        "country_code": (
            value.get("landkode")
            or value.get("country_code")
        ),
        "postal_code": (
            value.get("postnummer")
            or value.get("postal_code")
        ),
        "postal_place": (
            value.get("poststed")
            or value.get("postal_place")
        ),
    }


# =========================================================
# NORMALIZE FACT VALUES
# =========================================================

def normalize_value(fact_name, value):
    """Normalize API values into consistent formats."""

    if isinstance(value, dict):

        # Industry and organisation type
        if fact_name in ("industry", "organisation_type"):
            return {
                "code": value.get("code") or value.get("kode"),
                "description": (
                    value.get("description")
                    or value.get("beskrivelse")
                ),
            }

        # Business address
        if fact_name == "business_address":
            return normalize_address(value)

        # Other dictionaries
        return {
            key: normalize_value(key, item)
            for key, item in sorted(value.items())
            if key != "_links"
        }

    if isinstance(value, list):
        return [
            normalize_value(fact_name, item)
            for item in value
        ]

    return value


def normalized_json(fact_name, value):
    return to_json(normalize_value(fact_name, value))


# =========================================================
# DATABASE
# =========================================================

def initialize_database():
    """Create tables if they do not already exist."""

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                org_number TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                profile_json TEXT,
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
                PRIMARY KEY (org_number, fact_name),
                FOREIGN KEY (org_number)
                    REFERENCES companies(org_number)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_fact_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_number TEXT NOT NULL,
                fact_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                change_type TEXT NOT NULL,
                source_url TEXT NOT NULL,
                retrieved_at TEXT NOT NULL
            )
        """)

        conn.commit()


# =========================================================
# FETCH AND VERIFY COMPANY
# =========================================================

def fetch_company(session, org_number):
    """Fetch a company and verify its registered number."""

    org_number = str(org_number).strip()

    if not valid_org_number(org_number):
        print(f"Invalid organisation number: {org_number}")
        return None

    url = f"{API_URL}/{org_number}"

    try:
        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 404:
            print(f"Company not found: {org_number}")
            return None

        response.raise_for_status()
        record = response.json()

    except requests.RequestException as error:
        print(f"Request failed for {org_number}: {error}")
        return None

    except ValueError:
        print(f"Invalid JSON response for {org_number}")
        return None

    returned_number = str(
        record.get("organisasjonsnummer", "")
    ).strip()

    if returned_number != org_number:
        print(
            f"IDENTITY MISMATCH: requested {org_number}, "
            f"received {returned_number}. Record rejected."
        )
        return None

    if not str(record.get("navn", "")).strip():
        print(f"Missing registered name: {org_number}")
        return None

    return record, response.url


# =========================================================
# EXTRACT COMPANY FACTS
# =========================================================

def get_fact_values(record):
    """Extract available facts without inventing missing values."""

    facts = {}

    for fact_name, api_field in FACT_FIELDS.items():
        value = record.get(api_field)

        # False and zero are valid values.
        if value is not None and value != "":
            facts[fact_name] = normalize_value(
                fact_name,
                value,
            )

    return facts


# =========================================================
# NORMALIZE OLD STORED VALUES
# =========================================================

def normalize_old_value(fact_name, old_value):
    """Convert previously stored values to the current format."""

    if old_value is None:
        return None

    try:
        parsed = json.loads(old_value)
    except (json.JSONDecodeError, TypeError):
        parsed = old_value

    return normalized_json(fact_name, parsed)


# =========================================================
# COMPARE OLD AND NEW VALUES
# =========================================================

def values_equivalent(fact_name, old_value, new_value):
    """
    Compare normalized JSON values.
    For legacy addresses, compare fields that were known before.
    """

    if old_value == new_value:
        return True

    if fact_name != "business_address":
        return False

    try:
        old = json.loads(old_value) if old_value is not None else None
        new = json.loads(new_value) if new_value is not None else None

    except (json.JSONDecodeError, TypeError):
        return False

    if not isinstance(old, dict) or not isinstance(new, dict):
        return False

    # An address must be available on both sides.
    if not old.get("address") or not new.get("address"):
        return False

    # Compare every field that was known in the old record.
    fields_to_compare = (
        "address",
        "postal_code",
        "postal_place",
        "municipality",
        "municipality_number",
        "country",
        "country_code",
    )

    for field in fields_to_compare:
        old_field = old.get(field)

        # Do not treat absent legacy metadata as a change.
        if old_field is not None and old_field != "":
            if old_field != new.get(field):
                return False

    return True


# =========================================================
# SAVE COMPANY AND TRACK CHANGES
# =========================================================

def save_verified_record(org_number, record, source_url):
    """Save the profile and append genuine fact changes."""

    retrieved_at = now_utc()
    name = str(record.get("navn", "")).strip()

    if not name:
        print(f"Missing name for {org_number}; skipped.")
        return

    facts = get_fact_values(record)
    official_url = RECORD_URL + org_number

    profile = {
        "org_number": org_number,
        "name": name,
        "facts": facts,
        "source_url": source_url,
        "official_record_url": official_url,
        "retrieved_at": retrieved_at,
    }

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        # Save or refresh company profile.
        conn.execute("""
            INSERT INTO companies (
                org_number,
                name,
                profile_json,
                source_url,
                retrieved_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(org_number) DO UPDATE SET
                name = excluded.name,
                profile_json = excluded.profile_json,
                source_url = excluded.source_url,
                retrieved_at = excluded.retrieved_at
        """, (
            org_number,
            name,
            to_json(profile),
            source_url,
            retrieved_at,
        ))

        for fact_name, value in facts.items():
            new_value = normalized_json(fact_name, value)

            previous = conn.execute("""
                SELECT fact_value
                FROM company_facts
                WHERE org_number = ? AND fact_name = ?
            """, (
                org_number,
                fact_name,
            )).fetchone()

            if previous is None:
                old_normalized = None
                change_type = "NEW"

            else:
                old_normalized = normalize_old_value(
                    fact_name,
                    previous[0],
                )

                if values_equivalent(
                    fact_name,
                    old_normalized,
                    new_value,
                ):
                    # Refresh evidence without false history.
                    conn.execute("""
                        UPDATE company_facts
                        SET fact_value = ?,
                            source_url = ?,
                            retrieved_at = ?
                        WHERE org_number = ? AND fact_name = ?
                    """, (
                        new_value,
                        source_url,
                        retrieved_at,
                        org_number,
                        fact_name,
                    ))
                    continue

                change_type = "CHANGED"

            # Record only new or changed facts.
            conn.execute("""
                INSERT INTO company_fact_history (
                    org_number,
                    fact_name,
                    old_value,
                    new_value,
                    change_type,
                    source_url,
                    retrieved_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                org_number,
                fact_name,
                old_normalized,
                new_value,
                change_type,
                source_url,
                retrieved_at,
            ))

            # Save the latest fact value.
            conn.execute("""
                INSERT INTO company_facts (
                    org_number,
                    fact_name,
                    fact_value,
                    source_url,
                    retrieved_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(org_number, fact_name) DO UPDATE SET
                    fact_value = excluded.fact_value,
                    source_url = excluded.source_url,
                    retrieved_at = excluded.retrieved_at
            """, (
                org_number,
                fact_name,
                new_value,
                source_url,
                retrieved_at,
            ))

        conn.commit()


# =========================================================
# RESEARCH STORED COMPANIES
# =========================================================

def run_research():
    initialize_database()

    with sqlite3.connect(DB_PATH) as conn:
        org_numbers = conn.execute("""
            SELECT org_number
            FROM companies
            ORDER BY org_number
        """).fetchall()

    if not org_numbers:
        print("No companies found. Run your collector first.")
        return

    session = requests.Session()
    session.headers.update(HEADERS)

    succeeded = 0
    failed = 0
    total = len(org_numbers)

    try:
        for index, (org_number,) in enumerate(
            org_numbers,
            start=1,
        ):
            print(f"[{index}/{total}] Researching {org_number}")

            result = fetch_company(session, org_number)

            if result is None:
                failed += 1
                time.sleep(REQUEST_DELAY)
                continue

            record, source_url = result

            try:
                save_verified_record(
                    org_number,
                    record,
                    source_url,
                )
                succeeded += 1

            except sqlite3.Error as error:
                failed += 1
                print(
                    f"Database error for {org_number}: {error}"
                )

            time.sleep(REQUEST_DELAY)

    finally:
        session.close()

    with sqlite3.connect(DB_PATH) as conn:
        company_count = conn.execute(
            "SELECT COUNT(*) FROM companies"
        ).fetchone()[0]

        fact_count = conn.execute(
            "SELECT COUNT(*) FROM company_facts"
        ).fetchone()[0]

        history_count = conn.execute(
            "SELECT COUNT(*) FROM company_fact_history"
        ).fetchone()[0]

    print("\n========== RESEARCH SUMMARY ==========")
    print(f"Records successfully fetched: {succeeded}")
    print(f"Records failed or skipped: {failed}")
    print(f"Companies in database: {company_count}")
    print(f"Current facts stored: {fact_count}")
    print(f"History entries: {history_count}")
    print(f"Database: {DB_PATH}")


if __name__ == "__main__":
    run_research()
