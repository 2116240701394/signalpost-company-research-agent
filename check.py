
import sqlite3

conn = sqlite3.connect("companies.db")

rows = conn.execute("""
    SELECT org_number, fact_name, fact_value
    FROM company_facts
    WHERE org_number IN (?, ?)
      AND fact_name IN (?, ?)
    ORDER BY org_number, fact_name
""", ("999551807", "999522424", "industry", "business_address"))

for row in rows:
    print(row)

conn.close()
