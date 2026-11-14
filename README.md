# D&D multi-source database importer

One **independent, portable SQLite file (`.sqlite`) per upstream D&D data source**, with
all source collections in each file. No merging or cross-source deduplication.

## Sources in this starter

- `five_e_bits`: https://www.dnd5eapi.co (discovers all `/api/2014` and `/api/2024` collections)
- `open5e`: https://api.open5e.com/v2/ (discovers all v2 collections and follows pagination)
- `foundry_dnd5e`: https://github.com/foundryvtt/dnd5e (`packs/_source/**/*.json`)
- `btmorton_srd`: https://github.com/BTMorton/dnd-5e-srd (`json/**/*.json`)
- `adkinn_srd521`: https://github.com/adkinn/srd-5.2.1 (2024 SRD monsters and conditions)
- `dnd_data_community` (**opt-in only**, *not enabled by default*):
  https://github.com/nick-aschenbach/dnd-data (`data/**/*.json`). Includes
  material attributed to proprietary official and third-party sourcebooks;
  **rights for all included text are not established merely by its MIT code license**.
  Review legality/permissions before selecting this source.

Not exhaustive: more sources can be added to `sources.json` or with a new adapter
for a new API protocol. A repository that merely mirrors another isn't inherently new content.

## VS Code: run it on Windows

### Clone this project onto another computer

Install Git, Git LFS, and Python 3.10 or newer (including Tcl/Tk for the
desktop conflict browser). Then run the following in PowerShell, replacing
`<repository-url>` with this repository's remote URL:

```powershell
git lfs install
git clone <repository-url>
cd dnd_multi_source_importer
git lfs pull
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe review_conflicts.py
```

If you clone into a different folder name, use that name in the `cd` command.
The databases in `databases/` and `output/`, including saved conflict decisions
in `output/dnd_master.resolutions.sqlite`, are versioned with Git LFS. A clone
includes the existing data; running the imports again is optional.
`databases/five_e_bits.incomplete.sqlite` is a preserved incomplete import,
not a successful full import. The virtual environment is local to each computer.

Before switching computers, close the conflict browser and any database editors
and let imports finish, then save your work:

```powershell
git add .
git commit -m "Save project and database updates"
git push
```

On the other computer, commit any local work first, then run `git pull --ff-only`
and `git lfs pull` before opening the databases. SQLite databases cannot be merged
automatically; avoid changing the same database independently on both computers.
Temporary SQLite journal files are excluded from Git.

1. Unzip the project, then use **File > Open Folder** in VS Code.
2. Open Terminal > New Terminal and run:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python import_dnd.py --list
   ```

   If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` in place
   of `python` (no execution-policy change needed).

3. Start with a small **sample** from one source:

   ```powershell
   python import_dnd.py --source five_e_bits --max-per-collection 3
   ```

4. Download **all available structured records** from every configured source:

   ```powershell
   python import_dnd.py
   ```

   Or select just one:

   ```powershell
   python import_dnd.py --source open5e
   ```

5. Examine the database catalog:

   ```powershell
   python inspect_databases.py
   ```

Files are written to `databases/`. Full imports produce:

```text
databases/
  five_e_bits.sqlite
  open5e.sqlite
  foundry_dnd5e.sqlite
  btmorton_srd.sqlite
  adkinn_srd521.sqlite
```

The `.sqlite` files are the complete portable database files; no database server required.
Any initial `.working.sqlite` is a temporary build file. Errors produce an
`.incomplete.sqlite` **instead of replacing your last successful database**.
Preview imports are clearly named `*_sample.sqlite`.

## SQLite layout (identical across sources)

- `metadata`: importer version, source ID, URL, start/end times, status.
- `collections`: collection name, rules edition (when explicitly available), URL,
  expected and imported record counts, status/errors.
- `records`: one record per API resource / JSON entity, with searchable `name`,
  `description`, `document_key`, source URL/path and the **entire upstream JSON** in `data_json`.
- `source_files`: for GitHub sources, unmodified parsed content of each original JSON
  file (and its SHA-256) to preserve data even when its structure doesn't flatten neatly.
- `catalog`: view of the status and record count for each collection.

Useful queries in the SQLite VS Code extension or DB Browser for SQLite:

```sql
SELECT collection, edition, records FROM catalog ORDER BY collection, edition;

SELECT c.edition, r.name, r.description
FROM records r JOIN collections c ON c.id = r.collection_id
WHERE c.name = 'equipment' ORDER BY r.name LIMIT 30;

SELECT r.name, json_extract(r.data_json, '$.weight') AS weight
FROM records r JOIN collections c ON c.id = r.collection_id
WHERE c.name = 'equipment' AND c.edition = '2014' LIMIT 20;
```

`description` is an optional convenience column: all original fields remain in `data_json`.
Some GitHub source files can contain grouped JSON objects; they are preserved in
`source_files` even if they don't create separate normalized rows.

## Rules and limitations

- The project reads publicly accessible endpoints and GitHub repositories; no login or
  API key is required for the four configured sources, subject to provider changes.
- Public accessibility does **not** mean all data is licensed for redistribution.
  Check each upstream project's licenses and attribution requirements. For Open5e,
  license may vary by source document, and Foundry images/assets have separate terms.
- This is **not** a promise of every officially published D&D book or every D&D API
  on the Internet. Restricted/proprietary sources are not included.
- Source data overlaps considerably (especially SRD content). We do not deduplicate
  it: each provider's records stay in its own DB as requested.
- API schemas are not identical. Storing full raw JSON prevents losses. Later, add
  an optional shared search/normalization layer *without overwriting source copies*.
- GitHub import takes only configured JSON files, not executable code or copyrighted images.
- Data from Foundry is not assigned an edition purely by folder-name guessing; consult
  original records to identify edition-specific mechanics.
- Imports are read-only HTTP GETs and use retry/backoff for transient API errors.
- Network failures or missing collections produce an **incomplete** file and nonzero exit code.

## Adding another JSON repository

Add a `github_json` entry to `sources.json`:

```json
{
  "id": "my_dataset",
  "name": "My licensed JSON repository",
  "type": "github_json",
  "repository": "owner/repo",
  "branch": "main",
  "include_prefixes": ["data/"],
  "strip_prefix": "data/",
  "enabled": true
}
```

The importer will download that GitHub branch as a ZIP and import JSON under `data/`.
For a novel API JSON shape, add another importer adapter, not just another URL.

`--list` also shows disabled sources. An explicitly named `--source` can select
one of those opt-in sources, but that is **not** an endorsement of its copyright status.

Other candidates to examine individually (not yet supported in this starter) include
[The DM's Toolkit JSON API](https://apisearch.thedmstoolkit.com/) which documents
2024 SRD, a large legacy/third-party dataset, and community-expanded data. Its
response envelopes differ (`data` versus `results`) and bulk usage/rights require review.

## Equipment master database

The optional `normalizer` package builds a separate master database alongside the
existing importer. It reads all five original databases in SQLite read-only mode
and checks their SHA-256 hashes before and after processing.

```powershell
.\.venv\Scripts\python.exe -m normalizer.normalize
```

The current local 5e-bits download is incomplete. To explicitly include it:

```powershell
.\.venv\Scripts\python.exe -m normalizer.normalize --allow-incomplete
```

A complete `five_e_bits.sqlite` is preferred whenever present; the incomplete
file is used only when the complete file is absent and the flag is supplied.
Incomplete status is retained in the output's source metadata and report.
All five inputs are required. Adkinn currently has monsters and conditions only,
so its records are counted as skipped for this equipment phase.

Outputs:

- `output/dnd_master.sqlite`: equipment database, published after integrity and
  foreign-key checks. An unsuccessful build leaves the previous database intact.
- `output/dnd_master.report.json`: counts by source and overall, plus warnings.

Use `--inputs PATH` and `--output PATH` to change locations. Output must be outside
the input directory. Close database viewers before regenerating on Windows.
No network access or additional dependencies are required.

`items` contains every equipment record, including weapons, armor, and magic
items. `weapons`, `armor`, and `magic_items` link to their item IDs;
`weapons_details`, `armor_details`, and `magic_items_details` expose the complete
normalized fields for each subtype. Costs retain their original currency;
weights are normalized only when the source supplies pounds. Unrecognized units
and source-specific mechanics remain available in the original JSON.

`record_sources` preserves actual provider IDs, collection and edition, source
file/URL, original record ID, nested JSON pointer, original JSON, the adapter's
normalized JSON, and attribution. BTMorton pointers ending in `/table/N` identify
the zero-based row of its column-oriented table (a derived row locator rather
than a literal JSON Pointer into the original column arrays). Magic-item paths
are escaped JSON Pointers into the source document. Full original parent JSON is
retained for each extracted entry. `sources` stores input hashes, importer
metadata, and available document/license/publisher/ruleset/legal records.
Unknown licensing information remains unknown; normalization does not establish
redistribution rights.

Matching uses canonical name, edition, type, and ruleset/document scope.
Explicit SRD document IDs can match across providers; other documents stay in
separate scopes. Foundry editions come from `system.source.rules`, never pack
names. BTMorton's legacy SRD equipment is mapped to 2014. Unknown editions remain
separate by provider and document and receive review entries.

Shared populated gameplay values must agree, with at least one shared cost,
weight, damage, armor class, or description as matching evidence. Missing values
may be filled from agreeing sources. The provider order is 5e-bits, Open5e,
Foundry, BTMorton, adkinn; first populated values win only among compatible
records. Conflicting variants remain separate and are linked in `conflicts`.
Magic-item descriptions must agree exactly after HTML/whitespace cleanup, so
text variants deliberately remain separate for review. This is conservative
matching, not a claim that every remaining variant is a distinct game object.

Report counts:

- `records_read` and `skipped` count original importer records; skipped includes
  out-of-scope categories, Foundry folders, and non-equipment documents.
- `imported` counts new master entries; `merged` counts additional extracted
  entries linked to an existing master entry. A nested source record can yield
  many equipment entries, so these do not sum to `records_read`.
- `flagged` counts extracted entries with validation issues; `review` contains
  individual issues, so one entry may generate multiple rows.
- `conflicts` counts pairs of retained variants with disagreeing populated
  gameplay fields. Details are stored in `fields_json`.

```sql
SELECT name, edition, cost_value, cost_unit, damage_dice, versatile_damage
FROM weapons_details WHERE canonical_name = 'longsword';

SELECT i.name, i.edition, r.source_id, r.collection, r.external_id, r.json_pointer
FROM items i JOIN record_sources r ON r.item_id = i.id
WHERE i.canonical_name = 'longsword';

SELECT i.name, i.edition, c.fields_json
FROM conflicts c JOIN items i ON i.id = c.item_id;
```

This first phase covers equipment only. Spells, character options, monsters, and
rules remain in their source databases for later adapters. No comparison CSVs
were present in this workspace when the equipment pipeline was implemented.

Run the offline test suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Browse and resolve conflicts in a desktop window

Double-click **Review Conflicts.cmd**, or run:

```powershell
.\.venv\Scripts\python.exe review_conflicts.py
```

The browser uses Python's included Tkinter UI. Search by item name, ruleset, or
type, and filter by edition or review status. Each group contains all variants
of the same name, edition, ruleset, and type, so you can compare more than two
records at once. Scroll horizontally for additional variants. Gold highlights
differences; green highlights the value selected for the combined record.

1. Select a group on the left. **Compare records** shows normalized fields;
   **Original data & attribution** shows every provider's original JSON, reference,
   and available attribution. Long descriptions can scroll independently.
2. Select the preferred variant. Its values become the starting point. Click
   **Use this value** on another variant to choose individual fields. Changing
   the preferred variant resets those individual field choices.
3. Inspect **Resolution preview**, optionally add a note, then click
   **Save preferred / combined record**. This presents one item with all its
   original source references. Alternatively, **Keep all separate** marks the
   group reviewed while retaining every variant.
4. Use the **All groups** or **Resolved** filter to revisit decisions.
   **Undo saved decision** restores the original variants. Successful saves
   advance to the next visible group. Ctrl+F focuses search; Ctrl+S saves the
   preferred / combined record.

Your decisions and undo history are saved in
`output/dnd_master.resolutions.sqlite`. **Back up this file**: it is separate
from the generated master and is never replaced by the normalizer. Decisions
are automatically reapplied on rebuild. The exact normalized records, source
references, attribution, and original JSON are fingerprinted; a change makes the
decision **Needs review** and the new master exposes the original variants until
you review and save again. Decisions for groups no longer present are retained
in the decision file and counted as `unavailable` in the next build report.

The raw `items`, `record_sources`, and `conflicts` tables stay intact. Applications
should use these views to consume your decisions:

```sql
SELECT * FROM resolved_items;          -- all equipment after saved choices
SELECT * FROM resolved_weapons;        -- equivalent views for armor / magic_items
SELECT * FROM resolved_record_sources; -- all provenance using the resolved item ID
```

The subtype views are `resolved_weapons`, `resolved_armor`, and
`resolved_magic_items`. `resolved_record_sources.original_item_id` retains the
item ID assigned before human review. `conflict_resolution_status` records which
decisions were applied, became stale, or no longer match a group. The browser
updates these views immediately; the JSON build report's resolution counts are
a snapshot from the last normalization run.

To use other paths, supply `--database PATH --resolutions PATH` to the browser;
pass the same `--resolutions PATH` to the normalizer. Close the browser while
rebuilding, then reopen it. Decisions with different editions or document scopes
cannot be combined in the browser. This UI reviews conflict groups; the separate
validation issues in `review` remain available through SQL.

An optional GUI smoke test exercises comparison, field choices, source inspection,
save, undo, filters, and refresh using temporary databases:

```powershell
.\.venv\Scripts\python.exe tests/gui_smoke.py
```
