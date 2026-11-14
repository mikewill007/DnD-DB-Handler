"""Offline adapter/build checks using isolated, minimal source databases."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from normalizer.adapters import extract, number
from normalizer.normalize import SOURCES, build


@contextmanager
def connection(path):
    """Commit/roll back test writes and release file handles before Windows cleanup."""
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


class NormalizerTests(unittest.TestCase):
    # Temporary source fixtures shared with the resolution tests and GUI smoke test.
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.output = self.root / 'output' / 'master.sqlite'
        for source in SOURCES:
            with connection(self.inputs / (source + '.sqlite')) as db:
                db.executescript('''CREATE TABLE metadata(key TEXT,value TEXT);
                CREATE TABLE collections(id INTEGER PRIMARY KEY,name TEXT,edition TEXT);
                CREATE TABLE records(id INTEGER PRIMARY KEY,collection_id INTEGER,external_id TEXT,
                  name TEXT,description TEXT,document_key TEXT,source_url TEXT,source_file TEXT,data_json TEXT);''')
                db.executemany('INSERT INTO metadata VALUES (?,?)', [('source_id', source), ('status', 'complete')])

    def tearDown(self):
        self.temp.cleanup()

    def add(self, source, obj, collection='equipment', edition='2014', external='longsword'):
        with connection(self.inputs / (source + '.sqlite')) as db:
            cid = db.execute('INSERT INTO collections(name,edition) VALUES (?,?)', (collection, edition)).lastrowid
            db.execute('INSERT INTO records(collection_id,external_id,name,data_json) VALUES (?,?,?,?)',
                       (cid, external, obj.get('name'), json.dumps(obj)))

    def sword(self, price=15):
        # custom_ability is intentionally outside the normalized field set: the
        # provenance checks ensure provider-only mechanics survive extraction.
        return dict(name='Longsword', weapon_category='Martial', cost=dict(quantity=price, unit='gp'),
                    weight=3, damage=dict(damage_dice='1d8', damage_type=dict(name='Slashing')),
                    custom_ability={'unusual': True})

    # Build invariants: provenance, identity boundaries, and repeatability.

    def test_merge_editions_conflicts_provenance_repeatability_and_read_only(self):
        self.add('five_e_bits', self.sword())
        self.add('five_e_bits', self.sword(), edition='2024')
        other = self.sword()
        other['document'] = {'key': 'srd-2014', 'gamesystem': {'key': '5e-2014'}}
        self.add('open5e', other, 'weapons', '')
        self.add('five_e_bits', self.sword(99), external='expensive-longsword')
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.inputs.iterdir()}
        report = build(self.inputs, self.output)
        with connection(self.output) as db:
            self.assertEqual(3, db.execute('SELECT count(*) FROM items').fetchone()[0])
            self.assertEqual(4, db.execute('SELECT count(*) FROM record_sources').fetchone()[0])
            self.assertEqual(1, db.execute('SELECT count(*) FROM conflicts').fetchone()[0])
            self.assertTrue(json.loads(db.execute('SELECT original_json FROM record_sources LIMIT 1').fetchone()[0])['custom_ability']['unusual'])
            self.assertEqual([], db.execute('PRAGMA foreign_key_check').fetchall())
            first = db.iterdump()
            snapshot = list(first)
        self.assertEqual(1, report['totals']['merged'])
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.inputs.iterdir()})
        self.assertEqual(report, build(self.inputs, self.output))
        with connection(self.output) as db:
            self.assertEqual(snapshot, list(db.iterdump()))

    def test_unknown_edition_and_document_scopes_stay_separate(self):
        self.add('five_e_bits', self.sword(), edition='')
        other = self.sword()
        other['document'] = {'key': 'homebrew', 'gamesystem': {'key': '5e-2014'}}
        self.add('open5e', other, 'weapons', '')
        self.add('five_e_bits', self.sword())
        report = build(self.inputs, self.output)
        self.assertEqual(3, report['tables']['items'])
        self.assertEqual(1, report['totals']['flagged'])

    def test_incomplete_requires_opt_in_and_failed_build_preserves_output(self):
        build(self.inputs, self.output)
        original = self.output.read_bytes()
        with connection(self.inputs / 'five_e_bits.sqlite') as db:
            db.execute("UPDATE metadata SET value='incomplete' WHERE key='status'")
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            build(self.inputs, self.output)
        self.assertEqual(original, self.output.read_bytes())
        self.assertEqual(1, len(build(self.inputs, self.output, True)['warnings']))

    # Provider-specific payload shapes and known normalization edge cases.

    def test_foundry_folder_excluded_and_explicit_edition_required(self):
        self.assertEqual([], list(extract('foundry_dnd5e', {'name': 'Weapons', 'type': 'Item'}, 'equipment24', '')))
        obj = {'name': 'Longsword', 'type': 'weapon', 'system': {'damage': {
            'base': {'number': 1, 'denomination': 8, 'types': ['slashing']},
            'versatile': {'number': 1, 'denomination': 10}}, 'armor': {'value': 10}}}
        r = list(extract('foundry_dnd5e', obj, 'equipment24', ''))[0][1]
        self.assertEqual('unknown', r['edition'])
        self.assertEqual('1d10', r['versatile_damage'])
        self.assertIsNone(r['armor_class'])

    def test_btmorton_table_and_magic_item_paths(self):
        obj = {'Equipment': {'Weapons': {'Weapons List': {'Martial Melee Weapons': {'table': {
            'Name': ['Longsword'], 'Cost': ['15 gp'], 'Weight': ['3 lb.'],
            'Damage': ['1d8 slashing'], 'Properties': ['Versatile (1d10)']}}}}}}
        rows = list(extract('btmorton_srd', obj, '04 equipment', ''))
        self.assertEqual(1, len(rows))
        self.assertEqual('1d10', rows[0][1]['versatile_damage'])
        self.assertTrue(rows[0][0].endswith('/table/0'))
        obj = {'Magic Items': {'Amulet': {'content': ['*Wondrous item, rare (requires attunement)*', 'Effect']}}}
        row = list(extract('btmorton_srd', obj, '10 magic items', ''))[0]
        self.assertEqual('rare', row[1]['rarity'])
        self.assertTrue(row[1]['attunement'])

    def test_2024_categories_and_open5e_zero_range(self):
        obj = self.sword()
        del obj['weapon_category']
        obj['equipment_categories'] = [{'index': 'weapons'}]
        r = list(extract('five_e_bits', obj, 'equipment', '2024'))[0][1]
        self.assertEqual('weapon', r['record_type'])
        obj['range'] = 0
        obj['long_range'] = 0
        r = list(extract('open5e', obj, 'weapons', '2014'))[0][1]
        self.assertIsNone(r['range_normal'])
        self.assertEqual(.25, number('1/4 lb.'))


if __name__ == '__main__':
    unittest.main()
