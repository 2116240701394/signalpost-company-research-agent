
import sqlite3
from pathlib import Path

db_path = Path(__file__).parent / "companies.db"

if not db_path.exists():
    print("Database not found:", db_path)
else:
    with sqlite3.connect(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM companies"
        ).fetchone()[0]

        print("Total company profiles:", total)
        print("\nFirst 10 companies:")

        rows = conn.execute("""
            SELECT org_number, name
            FROM companies
            LIMIT 10
        """).fetchall()

        for org_number, name in rows:
            print(org_number, "-", name)
