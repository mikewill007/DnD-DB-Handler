"""Decision persistence, resolved views, undo, and rebuild compatibility checks."""
from contextlib import closing
import json
import sqlite3
import unittest

import test_normalizer as fixtures
from normalizer.normalize import build
from normalizer.resolutions import ReviewStore, default_decisions_path


class ResolutionTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting and rerunning the normalizer tests.
    setUp = fixtures.NormalizerTests.setUp
    tearDown = fixtures.NormalizerTests.tearDown
    add = fixtures.NormalizerTests.add
    sword = fixtures.NormalizerTests.sword

    def prepare(self):
        """Build three conflicting 2014 variants plus an independent 2024 record."""
        self.add('five_e_bits', self.sword())
        self.add('five_e_bits', self.sword(99), external='expensive-longsword')
        third = self.sword(50)
        third['weight'] = 5
        self.add('five_e_bits', third, external='heavy-longsword')
        self.add('five_e_bits', self.sword(), edition='2024')
        build(self.inputs, self.output)
        store = ReviewStore(self.output)
        group = next(iter(store.groups.values()))
        return store, group

    def query(self, sql):
        with closing(sqlite3.connect(self.output)) as db:
            return db.execute(sql).fetchall()

    def test_combined_resolution_provenance_undo_and_history(self):
        store, group = self.prepare()
        by_cost = {i['data']['cost_value']: i for i in group['items']}
        store.save(group['key'], group['fingerprint'], 'prefer', by_cost[15]['id'],
                   {'weight_lb': by_cost[50]['id']}, 'Use base price and heavier weight')
        self.assertEqual([(15., 5.)], self.query("SELECT cost_value,weight_lb FROM resolved_weapons WHERE edition='2014'"))
        self.assertEqual([(4,)], self.query('SELECT count(*) FROM items'))
        self.assertEqual([(2,)], self.query('SELECT count(*) FROM resolved_items'))
        self.assertEqual([(3,)], self.query('SELECT count(*) FROM conflicts'))
        refs = self.query('SELECT item_id,original_item_id FROM resolved_record_sources')
        self.assertEqual(4, len(refs))
        self.assertEqual(2, len({r[0] for r in refs}))
        data = json.loads(self.query("SELECT normalized_json FROM resolved_items WHERE edition='2014'")[0][0])
        self.assertEqual(5, data['weight_lb'])
        self.assertEqual('Resolved', store.groups[group['key']]['status'])
        store.undo(group['key'])
        self.assertEqual([(4,)], self.query('SELECT count(*) FROM resolved_items'))
        with closing(sqlite3.connect(default_decisions_path(self.output))) as db:
            self.assertEqual(2, db.execute('SELECT count(*) FROM history').fetchone()[0])
        self.assertEqual([], self.query('PRAGMA foreign_key_check'))

    def test_keep_separate_persists_across_rebuild_and_reopen(self):
        store, group = self.prepare()
        store.save(group['key'], group['fingerprint'], 'separate', note='Different variants')
        report = build(self.inputs, self.output)
        self.assertEqual({'applied': 1, 'stale': 0, 'unavailable': 0}, report['resolutions'])
        self.assertEqual([(4,)], self.query('SELECT count(*) FROM resolved_items'))
        reopened = ReviewStore(self.output)
        self.assertEqual('Kept separate', reopened.groups[group['key']]['status'])

    def test_preferred_resolution_reapplied_then_marked_stale_when_source_changes(self):
        store, group = self.prepare()
        store.save(group['key'], group['fingerprint'], 'prefer', group['items'][0]['id'])
        self.assertEqual(1, build(self.inputs, self.output)['resolutions']['applied'])
        self.assertEqual([(2,)], self.query('SELECT count(*) FROM resolved_items'))
        with fixtures.connection(self.inputs / 'five_e_bits.sqlite') as db:
            row = db.execute("SELECT id,data_json FROM records WHERE external_id='expensive-longsword'").fetchone()
            data = json.loads(row[1])
            # A source-only change must invalidate the decision even when all
            # normalized gameplay fields are identical to the previous build.
            data['custom_ability']['unusual'] = 'updated source-only mechanic'
            db.execute('UPDATE records SET data_json=? WHERE id=?', (json.dumps(data), row[0]))
        report = build(self.inputs, self.output)
        self.assertEqual(1, report['resolutions']['stale'])
        self.assertEqual([(4,)], self.query('SELECT count(*) FROM resolved_items'))
        store.refresh()
        self.assertEqual('Needs review', store.groups[group['key']]['status'])
        with self.assertRaisesRegex(ValueError, 'changed'):
            store.save(group['key'], group['fingerprint'], 'prefer', group['items'][0]['id'])

    def test_rejects_foreign_variants_invalid_fields_and_missing_master(self):
        store, group = self.prepare()
        with self.assertRaises(ValueError):
            store.save(group['key'], group['fingerprint'], 'prefer', 'other-edition')
        with self.assertRaises(ValueError):
            store.save(group['key'], group['fingerprint'], 'prefer', group['items'][0]['id'],
                       {'edition': group['items'][0]['id']})
        missing = self.root / 'missing.sqlite'
        with self.assertRaises(sqlite3.Error):
            ReviewStore(missing)
        self.assertFalse(missing.exists())

    def test_existing_master_migrated_without_rewriting_raw_data(self):
        store, group = self.prepare()
        before = self.query('SELECT * FROM record_sources ORDER BY id')
        with fixtures.connection(self.output) as db:
            for name in ('resolved_record_sources', 'resolved_weapons', 'resolved_armor',
                         'resolved_magic_items', 'resolved_items'):
                db.execute(f'DROP VIEW {name}')
            for name in ('resolution_members', 'resolution_overrides', 'conflict_resolution_status'):
                db.execute(f'DROP TABLE {name}')
        migrated = ReviewStore(self.output)
        self.assertEqual(1, len(migrated.groups))
        self.assertEqual(before, self.query('SELECT * FROM record_sources ORDER BY id'))
        source = group['items'][0]['sources'][0]
        original, attribution = migrated.original(source)
        self.assertEqual('Longsword', original['name'])


if __name__ == '__main__':
    unittest.main()
