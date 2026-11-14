# Troubleshooting incomplete imports

This importer makes one SQLite file per provider. Data from separate 2014/2024 rules versions share a provider file, with `edition` in the collections table.

A 404 in a provider's index causes that individual record to be logged in `fetch_errors`. The remaining details continue downloading, and the resulting provider file is marked `.incomplete.sqlite`. Do not treat incomplete files as comprehensive.

When Windows cannot replace an existing `.sqlite` file because a viewer or VS Code SQLite extension holds it open, the script **keeps** the new `.working.<pid>.sqlite` file and continues with other sources. Close any database viewers and move/rename the preserved file after verifying it. Running the importer again downloads from scratch; it does not resume.

Check what's saved:

```powershell
Get-ChildItem .\databases\*.sqlite | Select-Object Name, Length
.\.venv\Scripts\python.exe inspect_databases.py
```

In SQLite, inspect:

```sql
SELECT edition, name, expected_count, fetched_count, status, error
FROM collections WHERE status = 'error';
SELECT c.edition, c.name, f.external_id, f.source_url, f.error
FROM fetch_errors AS f JOIN collections AS c ON c.id=f.collection_id;
```
