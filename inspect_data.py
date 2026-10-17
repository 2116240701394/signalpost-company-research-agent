
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "companies.db"


def display_json(value):
    """Format JSON values for easier reading."""

    try:
        return json.dumps(
            json.loads(value),
            indent=2,
            ensure_ascii=False,
        )
    except (json.JSONDecodeError, TypeError):
        return value


def inspect_database():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        return

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

        print("\n========== DATABASE SUMMARY ==========")
        print(f"Companies: {company_count}")
        print(f"Current facts: {fact_count}")
        print(f"History entries: {history_count}")

        print("\n========== SAMPLE COMPANY FACTS ==========")

        rows = conn.execute("""
            SELECT org_number, fact_name, fact_value,
                   source_url, retrieved_at
            FROM company_facts
            ORDER BY org_number, fact_name
            LIMIT 10
        """).fetchall()

        if not rows:
            print("No company facts found.")

        for (
            org_number,
            fact_name,
            fact_value,
            source_url,
            retrieved_at,
        ) in rows:
            print(f"\nOrganisation number: {org_number}")
            print(f"Fact: {fact_name}")
            print(f"Value: {display_json(fact_value)}")
            print(f"Source: {source_url}")
            print(f"Retrieved: {retrieved_at}")

        print("\n========== RECENT DETECTED CHANGES ==========")

        history = conn.execute("""
            SELECT org_number, fact_name, old_value,
                   new_value, change_type, source_url, retrieved_at
            FROM company_fact_history
            ORDER BY id DESC
            LIMIT 10
        """).fetchall()

        if not history:
            print("No history entries yet.")

        for row in history:
            (
                org_number,
                fact_name,
                old_value,
                new_value,
                change_type,
                source_url,
                retrieved_at,
            ) = row

            print(f"\nOrganisation number: {org_number}")
            print(f"Fact: {fact_name}")
            print(f"Change type: {change_type}")
            print(f"Old value: {display_json(old_value)}")
            print(f"New value: {display_json(new_value)}")
            print(f"Source: {source_url}")
            print(f"Retrieved: {retrieved_at}")


if __name__ == "__main__":
    inspect_database()

