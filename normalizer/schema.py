"""Generated master schema, shared by normalization and conflict resolution.

Every equipment entry lives in ``items``. Subtype tables contain membership IDs;
their detail views expose the shared fields. Provider payloads and references
stay in ``record_sources`` even when entries are automatically matched.
"""
import sqlite3

# Shared column definitions also control field comparison and resolution overrides.
# Lists/dictionaries use JSON text; optional values stay NULL. Boolean fields use
# SQLite INTEGER values (0/1) while retaining NULL for unknown values.
FIELDS = {
    # Classification and descriptive text.
    'category': 'TEXT', 'subcategory': 'TEXT', 'description': 'TEXT',
    # Price retains its currency; weight is populated only in pounds.
    'cost_value': 'REAL', 'cost_unit': 'TEXT', 'weight_lb': 'REAL',
    # Weapon mechanics, including the edition-specific mastery field.
    'damage_dice': 'TEXT', 'damage_type': 'TEXT', 'properties': 'TEXT',
    'versatile_damage': 'TEXT', 'range_normal': 'REAL', 'range_long': 'REAL',
    'mastery': 'TEXT',
    # Armor mechanics and wearing requirements.
    'armor_class': 'REAL', 'dex_bonus': 'INTEGER',
    'dex_limit': 'REAL', 'stealth_disadvantage': 'INTEGER', 'strength_min': 'REAL',
    # Magic-item rarity and attunement requirements.
    'rarity': 'TEXT', 'attunement': 'INTEGER',
}

def initialize(db):
    """Create a fresh master schema; the caller owns commit and publication.

    ``conflicts`` links disagreeing variants in pairs, whereas ``review`` stores
    individual validation issues. Neither table changes the preserved payloads.
    Review-specific tables/views are added separately by resolutions.py.
    """
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
    CREATE TABLE sources (
      id TEXT PRIMARY KEY, database_path TEXT NOT NULL, sha256 TEXT NOT NULL,
      status TEXT, metadata_json TEXT NOT NULL, documents_json TEXT NOT NULL);
    CREATE TABLE items (
      id TEXT PRIMARY KEY, name TEXT NOT NULL, canonical_name TEXT NOT NULL,
      edition TEXT NOT NULL, ruleset TEXT NOT NULL, record_type TEXT NOT NULL,
      primary_source TEXT NOT NULL REFERENCES sources(id), normalized_json TEXT NOT NULL);
    CREATE TABLE record_sources (
      id INTEGER PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id),
      source_id TEXT NOT NULL REFERENCES sources(id), collection TEXT NOT NULL,
      collection_edition TEXT NOT NULL, external_id TEXT NOT NULL, original_record_id INTEGER NOT NULL,
      source_url TEXT, source_file TEXT, json_pointer TEXT NOT NULL,
      document_key TEXT, attribution_json TEXT NOT NULL,
      original_json TEXT NOT NULL CHECK(json_valid(original_json)),
      normalized_json TEXT NOT NULL CHECK(json_valid(normalized_json)),
      UNIQUE(source_id, collection, collection_edition, external_id, json_pointer));
    CREATE TABLE conflicts (
      id INTEGER PRIMARY KEY, item_id TEXT NOT NULL REFERENCES items(id),
      other_item_id TEXT NOT NULL REFERENCES items(id), fields_json TEXT NOT NULL);
    CREATE TABLE review (
      id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
      external_id TEXT, json_pointer TEXT, issue TEXT NOT NULL);
    CREATE INDEX item_search ON items(canonical_name,edition,record_type);
    CREATE INDEX provenance_item ON record_sources(item_id);
    ''')
    # Keep searchable columns aligned with the field set used by adapters and UI.
    for name, kind in FIELDS.items():
        db.execute(f'ALTER TABLE items ADD COLUMN {name} {kind}')
    # A weapon/armor/magic item still has exactly one row in the shared items table.
    for table in ('weapons', 'armor', 'magic_items'):
        db.execute(f'CREATE TABLE {table} (item_id TEXT PRIMARY KEY REFERENCES items(id))')
        db.execute(f'CREATE VIEW {table}_details AS SELECT items.* FROM items JOIN {table} ON items.id={table}.item_id')
