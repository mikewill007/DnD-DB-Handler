"""Build equipment tables without modifying any source database.

Pipeline: read provider records -> adapt -> validate -> match -> retain provenance
-> apply saved review decisions -> check integrity -> publish the generated files.

Run: python -m normalizer.normalize [--allow-incomplete]
"""
import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from .adapters import extract
from .matching import canonical_name, differences, equivalent
from .resolutions import apply_saved_resolutions, default_decisions_path
from .schema import FIELDS, initialize
from .validation import issues

# Provider order determines the primary source and the first populated field value
# among compatible records. Conflicting populated values create separate variants.
SOURCES = ('five_e_bits', 'open5e', 'foundry_dnd5e', 'btmorton_srd', 'adkinn_srd521')


def dumps(value):
    """Serialize deterministically for stored records and reproducible identity hashes."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def sha256(path):
    """Hash a file as a stream so large source databases need not fit in memory."""
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build(inputs, output, allow_incomplete=False, resolutions=None):
    """Build a master and JSON report, returning the report's counts and warnings.

    Sources are opened read-only and checked for changes during extraction.
    A separate working database isolates processing failures from the published
    master. Saved decisions come from a persistent, independently stored file.
    """
    # Input selection and output isolation.
    output = Path(output).resolve()
    paths = []
    for source in SOURCES:
        path = Path(inputs) / (source + '.sqlite')
        if not path.exists() and allow_incomplete:
            path = Path(inputs) / (source + '.incomplete.sqlite')
        if not path.exists():
            raise ValueError(f'Missing {source}.sqlite; --allow-incomplete permits its incomplete copy')
        paths.append((source, path.resolve()))
    if output in [p for _, p in paths] or output.parent == Path(inputs).resolve():
        raise ValueError('Output must be outside the source database directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    working = output.with_name(output.name + '.working')
    if working.exists():
        raise ValueError(f'Remove or inspect existing temporary build: {working}')
    report = {'scope': 'equipment', 'sources': {}, 'warnings': [], 'totals': {}}
    # Each scoped identity can hold several disagreeing variants, not just one item.
    candidates = defaultdict(list)
    db = sqlite3.connect(working)
    try:
        initialize(db)
        # Source extraction and attribution.
        for source, path in paths:
            digest = sha256(path)
            stats = dict(records_read=0, imported=0, merged=0, skipped=0, flagged=0, conflicts=0)
            report['sources'][source] = stats
            # A sqlite connection's context manager commits/rolls back but does not
            # close it. Explicit closing also releases Windows file handles promptly.
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as origin:
                origin.row_factory = sqlite3.Row
                metadata = dict(origin.execute('SELECT key,value FROM metadata'))
                if metadata.get('source_id') != source:
                    raise ValueError(f'Source ID mismatch: {path}')
                status = metadata.get('status', 'unknown')
                if status != 'complete' and not allow_incomplete:
                    raise ValueError(f'{source} status is {status}; use --allow-incomplete to include it')
                if status != 'complete':
                    report['warnings'].append(f'{source}: input status {status}')
                # Retain document/license/legal context even though those records
                # do not become equipment entries themselves.
                documents = [dict(r) for r in origin.execute('''SELECT c.name AS collection,r.external_id,r.data_json
                  FROM records r JOIN collections c ON c.id=r.collection_id
                  WHERE c.name IN ('documents','licenses','publishers','rulesets','gamesystems','00 legal')''')]
                document_lookup = {json.loads(d['data_json']).get('key'): json.loads(d['data_json'])
                                   for d in documents if d['collection'] == 'documents'}
                db.execute('INSERT INTO sources VALUES (?,?,?,?,?,?)',
                           (source, str(path), digest, status, dumps(metadata), dumps(documents)))
                for row in origin.execute('''SELECT r.*,c.name AS collection,c.edition FROM records r
                                            JOIN collections c ON c.id=r.collection_id ORDER BY r.id'''):
                    stats['records_read'] += 1
                    obj = json.loads(row['data_json'])
                    # One parent document can yield many items (especially BTMorton).
                    # records_read/skipped count parents; imported/merged count items.
                    derived = list(extract(source, obj, row['collection'], row['edition']))
                    if not derived:
                        stats['skipped'] += 1
                    for pointer, record, attribution in derived:
                        if not record['name']:
                            stats['skipped'] += 1
                            db.execute('INSERT INTO review(source_id,external_id,json_pointer,issue) VALUES (?,?,?,?)',
                                       (source, row['external_id'], pointer, 'Missing name; skipped'))
                            continue
                        attribution = document_lookup.get(attribution.get('key'), attribution)
                        # Validation messages retain the record for later inspection.
                        warnings = issues(record)
                        if warnings:
                            stats['flagged'] += 1
                            for issue in warnings:
                                db.execute('INSERT INTO review(source_id,external_id,json_pointer,issue) VALUES (?,?,?,?)',
                                           (source, row['external_id'], pointer, issue))
                        # Matching is restricted to a single identity scope.
                        key = (canonical_name(record['name']), record['edition'], record['ruleset'], record['record_type'])
                        matches = candidates[key]
                        match = next(((uid, value) for uid, value in matches if equivalent(value, record)), None)
                        if match:
                            # Enrich missing fields without overwriting populated ones.
                            uid, selected = match
                            stats['merged'] += 1
                            for field, value in record.items():
                                if selected.get(field) is None and value is not None:
                                    selected[field] = value
                            db.execute('UPDATE items SET normalized_json=? WHERE id=?', (dumps(selected), uid))
                            for field in FIELDS:
                                value = selected.get(field)
                                db.execute(f'UPDATE items SET {field}=? WHERE id=?',
                                           (dumps(value) if isinstance(value, (list, dict)) else value, uid))
                        else:
                            # Stable IDs use real source identity, including edition
                            # and the locator of any entry extracted from a parent.
                            identity = [source, row['collection'], row['edition'], row['external_id'], pointer]
                            uid = hashlib.sha256(dumps(identity).encode()).hexdigest()[:24]
                            db.execute('''INSERT INTO items(id,name,canonical_name,edition,ruleset,record_type,primary_source,normalized_json)
                                          VALUES (?,?,?,?,?,?,?,?)''',
                                       (uid, record['name'], *key, source, dumps(record)))
                            for field in FIELDS:
                                value = record.get(field)
                                db.execute(f'UPDATE items SET {field}=? WHERE id=?',
                                           (dumps(value) if isinstance(value, (list, dict)) else value, uid))
                            table = {'weapon': 'weapons', 'armor': 'armor', 'magic_item': 'magic_items'}.get(record['record_type'])
                            if table:
                                db.execute(f'INSERT INTO {table} VALUES (?)', (uid,))
                            # Conflicts represent pairs; a group of several variants
                            # can therefore generate several conflict rows.
                            for other_uid, other in matches:
                                diff = differences(other, record)
                                if diff:
                                    db.execute('INSERT INTO conflicts(item_id,other_item_id,fields_json) VALUES (?,?,?)',
                                               (other_uid, uid, dumps(diff)))
                                    stats['conflicts'] += 1
                            matches.append((uid, record.copy()))
                            stats['imported'] += 1
                        # Preserve every provider payload and its own normalized
                        # values, including for records merged into an existing item.
                        db.execute('''INSERT INTO record_sources(item_id,source_id,collection,collection_edition,external_id,original_record_id,
                          source_url,source_file,json_pointer,document_key,attribution_json,original_json,normalized_json)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (uid, source, row['collection'], row['edition'], row['external_id'], row['id'], row['source_url'],
                           row['source_file'], pointer, row['document_key'] or attribution.get('key'),
                           dumps(attribution), row['data_json'], dumps(record)))
            if sha256(path) != digest:
                raise ValueError(f'Source changed during build: {path}')
        # Saved decisions and final integrity checks.
        # Decisions affect resolved_* views; the raw master tables remain available.
        report['resolutions'] = apply_saved_resolutions(db, resolutions or default_decisions_path(output))
        report['resolved_items'] = db.execute('SELECT count(*) FROM resolved_items').fetchone()[0]
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Foreign key validation failed')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity check failed')
        # Report and publication.
        report['totals'] = {key: sum(s[key] for s in report['sources'].values())
                            for key in next(iter(report['sources'].values()))}
        report['tables'] = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                            for table in ('items', 'weapons', 'armor', 'magic_items', 'record_sources', 'conflicts', 'review')}
        db.commit()
        db.close()
        report_path = output.with_suffix('.report.json')
        report_temp = report_path.with_name(report_path.name + '.working')
        report_temp.write_text(dumps(report) + '\n', encoding='utf-8')
        # Close SQLite before replacement for Windows compatibility. Each replace
        # is atomic individually; the database and JSON report are separate files.
        os.replace(working, output)
        os.replace(report_temp, report_path)
        return report
    except BaseException:
        # Remove this attempt's disposable database and propagate the original error.
        db.close()
        if working.exists():
            working.unlink()
        raise


# Command-line entry point.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=Path('databases'))
    parser.add_argument('--output', type=Path, default=Path('output/dnd_master.sqlite'))
    parser.add_argument('--allow-incomplete', action='store_true', help='Permit incomplete inputs, explicitly recorded in report')
    parser.add_argument('--resolutions', type=Path, help='Saved decisions (default: beside output, *.resolutions.sqlite)')
    args = parser.parse_args()
    try:
        report = build(args.inputs, args.output, args.allow_incomplete, args.resolutions)
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f'Normalization failed: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
