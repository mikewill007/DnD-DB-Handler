"""Display counts and some names from each imported SQLite database."""
from pathlib import Path
import sqlite3

folder = Path(__file__).resolve().parent / "databases"
for path in sorted(folder.glob("*.sqlite")):
    print(f"\n=== {path.name} ===")
    try:
        with sqlite3.connect(path) as db:
            status = db.execute("SELECT value FROM metadata WHERE key='status'").fetchone()
            count = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            print(f"Status: {status[0] if status else '?'} | Records: {count}")
            for collection, edition, num, state in db.execute(
                "SELECT name, edition, fetched_count, status FROM collections ORDER BY name, edition"):
                print(f"  {collection:<25} {edition:<5} {num:>6} ({state})")
            print("Sample names:", [r[0] for r in db.execute("SELECT name FROM records WHERE name IS NOT NULL LIMIT 8")])
    except sqlite3.DatabaseError as exc:
        print("Unreadable database:", exc)
