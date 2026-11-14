"""Durable review decisions and a resolved view over the untouched master records.

The separate *.resolutions.sqlite file owns decisions and undo history. Tables
inside the generated master are disposable projections of those decisions;
rebuilding or undoing a choice never deletes the raw items or their provenance.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .schema import FIELDS


# Stable identifiers and generated review schema.


def encode(value):
    """Use deterministic JSON for group identifiers and content fingerprints."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def default_decisions_path(master):
    """Keep durable decisions beside the master under a distinct filename."""
    return Path(master).with_suffix('.resolutions.sqlite')


def group_key(record):
    """Identify a review group independently of its current values or variant count."""
    return encode([record[k] for k in ('canonical_name', 'edition', 'ruleset', 'record_type')])


def initialize_views(db):
    """Add review tables/views to existing or new masters without committing.

    resolution_members maps variants to the preferred ID. resolution_overrides
    holds complete preferred rows with any selected field values substituted.
    resolved_items hides mapped-away variants and uses the override for the winner.
    """
    # Individual execute calls preserve the caller's transaction (unlike executescript).
    statements = [
        '''CREATE TABLE IF NOT EXISTS resolution_members (
             item_id TEXT PRIMARY KEY REFERENCES items(id),
             resolved_item_id TEXT NOT NULL REFERENCES items(id))''',
        'CREATE TABLE IF NOT EXISTS resolution_overrides AS SELECT * FROM items WHERE 0',
        'CREATE UNIQUE INDEX IF NOT EXISTS resolution_override_id ON resolution_overrides(id)',
        '''CREATE TABLE IF NOT EXISTS conflict_resolution_status (
             group_key TEXT PRIMARY KEY, status TEXT NOT NULL, mode TEXT NOT NULL,
             note TEXT NOT NULL, updated_utc TEXT NOT NULL)''',
        '''CREATE VIEW IF NOT EXISTS resolved_items AS
             SELECT i.* FROM items i
             WHERE NOT EXISTS (SELECT 1 FROM resolution_members m
                               WHERE m.item_id=i.id AND m.resolved_item_id<>i.id)
               AND NOT EXISTS (SELECT 1 FROM resolution_overrides o WHERE o.id=i.id)
             UNION ALL SELECT * FROM resolution_overrides''',
    ]
    # Expose all provenance under the chosen canonical item without moving/deleting it.
    columns = [r[1] for r in db.execute('PRAGMA table_info(record_sources)')]
    projection = ', '.join(
        'COALESCE(m.resolved_item_id,r.item_id) AS item_id' if c == 'item_id' else f'r.{c}'
        for c in columns)
    statements.append(f'''CREATE VIEW IF NOT EXISTS resolved_record_sources AS
        SELECT {projection}, r.item_id AS original_item_id FROM record_sources r
        LEFT JOIN resolution_members m ON m.item_id=r.item_id''')
    # Filter the resolved rows themselves so subtype views respect the same choices.
    for name, kind in (('weapons', 'weapon'), ('armor', 'armor'), ('magic_items', 'magic_item')):
        statements.append(f'''CREATE VIEW IF NOT EXISTS resolved_{name} AS
            SELECT * FROM resolved_items WHERE record_type='{kind}' ''')
    for sql in statements:
        db.execute(sql)


# Conflict catalog and stale-decision detection.


def catalog(db):
    """Load groups, variant fields, provenance summaries, and content fingerprints.

    A group includes every item in the conflicted identity scope, not only the
    endpoints of one pair. Raw JSON is read one payload at a time for hashing;
    the UI loads its complete text separately when the user asks to inspect it.
    This function sets the supplied connection's row factory to sqlite3.Row.
    """
    db.row_factory = sqlite3.Row
    groups, by_item = {}, {}
    for row in db.execute('''SELECT * FROM items WHERE
        (canonical_name,edition,ruleset,record_type) IN (
          SELECT i.canonical_name,i.edition,i.ruleset,i.record_type
          FROM conflicts c JOIN items i ON i.id=c.item_id)
        ORDER BY name,edition,primary_source,id'''):
        item = dict(row)
        item['data'] = json.loads(item['normalized_json'])
        item['sources'] = []
        key = group_key(item)
        group = groups.setdefault(key, dict(key=key, name=item['name'], edition=item['edition'],
            ruleset=item['ruleset'], record_type=item['record_type'], items=[], pairs=0))
        group['items'].append(item)
        by_item[item['id']] = item
    for row in db.execute('''SELECT item_id,source_id,collection,collection_edition,external_id,
        json_pointer,source_url,source_file,document_key,normalized_json,attribution_json,original_json
        FROM record_sources ORDER BY source_id,collection,collection_edition,external_id,json_pointer'''):
        if row['item_id'] not in by_item:
            continue
        source = dict(row)
        source['original_sha256'] = hashlib.sha256(source.pop('original_json').encode()).hexdigest()
        by_item[source['item_id']]['sources'].append(source)
    for row in db.execute('SELECT item_id FROM conflicts'):
        groups[group_key(by_item[row[0]])]['pairs'] += 1
    # Include source-only changes and attribution as well as normalized mechanics.
    # Explicit SQL ordering plus sorted JSON makes identical rebuilds fingerprint alike.
    for group in groups.values():
        snapshot = [[i['id'], i['normalized_json'], i['sources']] for i in group['items']]
        group['fingerprint'] = hashlib.sha256(encode(snapshot).encode()).hexdigest()
    return groups


def read_decisions(path):
    """Read the persistent decision file; its absence means no decisions were saved."""
    path = Path(path)
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        return {key: json.loads(payload) for key, payload in db.execute('SELECT group_key,payload_json FROM decisions')}


def validate_decision(group, decision):
    """Reject outdated snapshots and field selections outside this group's variants."""
    if decision.get('fingerprint') != group['fingerprint']:
        raise ValueError('These records changed. Refresh and review the current values before saving.')
    mode = decision.get('mode')
    if mode not in ('prefer', 'separate'):
        raise ValueError('Choose a preferred record or keep the records separate.')
    if mode == 'prefer':
        ids = {i['id'] for i in group['items']}
        if decision.get('preferred_id') not in ids:
            raise ValueError('The preferred record is no longer in this conflict group.')
        # Only shared equipment fields may be combined; identity fields such as
        # edition and ruleset cannot be edited into a different review scope.
        choices = decision.get('field_choices', {})
        if not isinstance(choices, dict) or any(k not in FIELDS or v not in ids for k, v in choices.items()):
            raise ValueError('Field choices must use fields and variants in this group.')


# Rebuildable projection of saved choices into the master.


def apply_saved_resolutions(db, decisions_path, groups=None):
    """Recreate resolved-view backing data and return decision-status counts.

    The caller owns the transaction and must pass a fresh catalog if supplying
    groups. Stale or unavailable decisions are reported and retained in the
    persistent file, but do not hide any raw variants from the resolved views.
    """
    initialize_views(db)
    # Recompute from raw data so undo and repeated synchronization are deterministic.
    for table in ('resolution_members', 'resolution_overrides', 'conflict_resolution_status'):
        db.execute(f'DELETE FROM {table}')
    decisions = read_decisions(decisions_path)
    groups = catalog(db) if decisions and groups is None else (groups or {})
    summary = dict(applied=0, stale=0, unavailable=0)
    for key, decision in decisions.items():
        group = groups.get(key)
        status = 'applied'
        if group is None:
            status = 'unavailable'
        else:
            try:
                validate_decision(group, decision)
            except ValueError:
                status = 'stale'
        summary[status] += 1
        db.execute('INSERT INTO conflict_resolution_status VALUES (?,?,?,?,?)',
                   (key, status, decision.get('mode', ''), decision.get('note', ''), decision.get('updated_utc', '')))
        # "Keep separate" acknowledges review without changing the equipment view.
        if status != 'applied' or decision['mode'] == 'separate':
            continue
        preferred = next(i for i in group['items'] if i['id'] == decision['preferred_id'])
        db.executemany('INSERT INTO resolution_members VALUES (?,?)',
                       [(i['id'], preferred['id']) for i in group['items']])
        # A missing selected value remains NULL: choosing it explicitly must not
        # fall back to a populated value from the preferred variant.
        selected = preferred['data'].copy()
        items = {i['id']: i for i in group['items']}
        for field, uid in decision.get('field_choices', {}).items():
            selected[field] = items[uid]['data'].get(field)
        db.execute('INSERT INTO resolution_overrides SELECT * FROM items WHERE id=?', (preferred['id'],))
        db.execute('UPDATE resolution_overrides SET normalized_json=? WHERE id=?', (encode(selected), preferred['id']))
        # Keep the searchable SQL columns and normalized JSON representation aligned.
        for field in FIELDS:
            value = selected.get(field)
            if isinstance(value, (list, dict)):
                value = encode(value)
            db.execute(f'UPDATE resolution_overrides SET {field}=? WHERE id=?', (value, preferred['id']))
    return summary


# Desktop-facing storage API.


class ReviewStore:
    """Coordinate short-lived master connections and a durable decision file.

    Decisions commit before the generated projection is synchronized. If a
    master-file lock prevents sync, the saved decision remains available for a
    later refresh/rebuild. The two databases do not share one atomic transaction.
    """
    def __init__(self, master, decisions=None):
        self.master = Path(master).resolve()
        self.decisions = Path(decisions or default_decisions_path(master)).resolve()
        if self.master == self.decisions:
            raise ValueError('The decision file must be separate from the master database.')
        # Read and validate before any mutation, including a path typo.
        self.groups = self.refresh()
        self.sync()

    def refresh(self):
        """Reload raw groups and annotate their current review status without writing."""
        with closing(sqlite3.connect(self.master.as_uri() + '?mode=ro', uri=True)) as db:
            self.groups = catalog(db)
        decisions = read_decisions(self.decisions)
        for key, group in self.groups.items():
            decision = decisions.get(key)
            group['decision'] = decision
            group['status'] = ('Unresolved' if decision is None else
                               'Needs review' if decision.get('fingerprint') != group['fingerprint'] else
                               'Kept separate' if decision.get('mode') == 'separate' else 'Resolved')
        return self.groups

    def sync(self):
        """Apply saved decisions in one master transaction using the current catalog."""
        with closing(sqlite3.connect(self.master.as_uri() + '?mode=rw', uri=True)) as db:
            with db:
                db.execute('PRAGMA foreign_keys=ON')
                summary = apply_saved_resolutions(db, self.decisions, self.groups)
        return summary

    def save(self, key, fingerprint, mode, preferred_id=None, field_choices=None, note=''):
        """Validate the displayed snapshot, persist a decision, then update the views."""
        # Re-read first: a rebuild may have changed the records while the UI was open.
        self.refresh()
        if key not in self.groups:
            raise ValueError('This conflict group is no longer available. Refresh the list.')
        payload = dict(fingerprint=fingerprint, mode=mode, preferred_id=preferred_id,
            field_choices=field_choices or {}, note=note,
            updated_utc=datetime.now(timezone.utc).isoformat())
        validate_decision(self.groups[key], payload)
        self._write(key, payload)
        self.refresh()
        self.sync()

    def undo(self, key):
        """Remove the active decision, retain an audit event, and restore raw variants."""
        self._write(key, None)
        self.refresh()
        self.sync()

    def _write(self, key, payload):
        """Commit the active choice and its history event together; None denotes undo."""
        self.decisions.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.decisions)) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS decisions(group_key TEXT PRIMARY KEY,payload_json TEXT NOT NULL)')
                db.execute('''CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,group_key TEXT NOT NULL,
                    payload_json TEXT,created_utc TEXT NOT NULL)''')
                db.execute('INSERT INTO history(group_key,payload_json,created_utc) VALUES (?,?,?)',
                           (key, encode(payload) if payload else None, datetime.now(timezone.utc).isoformat()))
                if payload is None:
                    db.execute('DELETE FROM decisions WHERE group_key=?', (key,))
                else:
                    db.execute('INSERT OR REPLACE INTO decisions VALUES (?,?)', (key, encode(payload)))

    def original(self, source):
        """Fetch one full provider payload and its attribution only when inspected."""
        fields = ('item_id', 'source_id', 'collection', 'collection_edition', 'external_id', 'json_pointer')
        with closing(sqlite3.connect(self.master.as_uri() + '?mode=ro', uri=True)) as db:
            row = db.execute('SELECT original_json,attribution_json FROM record_sources WHERE ' +
                             ' AND '.join(f'{f}=?' for f in fields), [source[f] for f in fields]).fetchone()
        if row is None:
            raise ValueError('This source record changed; refresh the conflict list.')
        return json.loads(row[0]), json.loads(row[1])
