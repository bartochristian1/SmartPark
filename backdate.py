import sqlite3

conn = sqlite3.connect("parking")
cur = conn.cursor()

# Get all database tables
tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

updated_count = 0
for table in tables:
    for col in ['entry_time', 'entry_timestamp', 'check_in_time', 'created_at', 'time_in']:
        try:
            cur.execute(f"UPDATE {table} SET {col} = datetime('now', '-2 hours')")
            updated_count += 1
            print(f"Updated table: {table}, column: {col}")
        except Exception:
            pass

conn.commit()
conn.close()

print(f"\nSuccess! Backdated parking entries by 2 hours.")