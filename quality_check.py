
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("companies.db")

if not DB_PATH.exists():
    print("ERROR: companies.db was not found.")
    raise SystemExit(1)

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("DATABASE:", DB_PATH.resolve())
print("\nTABLES:")

tables = cur.execute("""
    SELECT name
    FROM sqlite_master
    WHERE type = 'table'
      AND name NOT LIKE 'sqlite_%'
    ORDER BY name
""").fetchall()

for table in tables:
    print("-", table["name"])

print("\nROW COUNTS:")
for table in tables:
    name = table["name"]
    # Table names are read from SQLite's own table list.
    count = cur.execute(
        f'SELECT COUNT(*) FROM "{name}"'
    ).fetchone()[0]
    print(f"{name}: {count}")

company_tables = [
    t["name"] for t in tables if t["name"] == "companies"
]

if company_tables:
    columns = {
        row["name"]
        for row in cur.execute("PRAGMA table_info(companies)")
    }

    print("\nCOMPANY DATA CHECKS:")

    if "org_number" in columns:
        invalid = cur.execute("""
            SELECT COUNT(*) FROM companies
            WHERE org_number IS NULL
               OR LENGTH(TRIM(CAST(org_number AS TEXT))) != 9
               OR TRIM(CAST(org_number AS TEXT))
                    GLOB '*[^0-9]*'
        """).fetchone()[0]
        print("Invalid organisation numbers:", invalid)

        duplicates = cur.execute("""
            SELECT COUNT(*) FROM (
                SELECT org_number
                FROM companies
                GROUP BY org_number
                HAVING COUNT(*) > 1
            )
        """).fetchone()[0]
        print("Duplicate organisation numbers:", duplicates)
    else:
        print("Could not check org_number: column not found.")

    if "profile_json" in columns:
        missing_profiles = cur.execute("""
            SELECT COUNT(*) FROM companies
            WHERE profile_json IS NULL
               OR TRIM(profile_json) = ''
        """).fetchone()[0]
        print("Missing profiles:", missing_profiles)

        malformed = 0
        rows = cur.execute("""
            SELECT profile_json FROM companies
            WHERE profile_json IS NOT NULL
              AND TRIM(profile_json) != ''
        """)
        import json

        for row in rows:
            try:
                json.loads(row["profile_json"])
            except (ValueError, TypeError):
                malformed += 1

        print("Invalid profile JSON records:", malformed)

conn.close()
print("\nQuality check completed.")
