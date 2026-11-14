import io
import json
import sqlite3
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import import_dnd as imp


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        imp.init_db(self.db, {"id": "test", "name": "Test", "type": "test"}, 0)

    def tearDown(self):
        self.db.close()

    def test_five_e_bits_separate_editions(self):
        pages = {
            'https://www.dnd5eapi.co/api/2014': {"equipment": "/api/2014/equipment"},
            'https://www.dnd5eapi.co/api/2024': {"equipment": "/api/2024/equipment"},
            'https://www.dnd5eapi.co/api/2014/equipment': {"count": 1,"results": [{"index": "club", "url": "/api/2014/equipment/club"}]},
            'https://www.dnd5eapi.co/api/2024/equipment': {"count": 1,"results": [{"index": "club", "url": "/api/2024/equipment/club"}]},
            'https://www.dnd5eapi.co/api/2014/equipment/club': {"index": "club", "name": "Club", "cost": {"quantity":1,"unit":"sp"}},
            'https://www.dnd5eapi.co/api/2024/equipment/club': {"index": "club", "name": "Club", "cost": {"quantity":1,"unit":"sp"},"edition":"2024"}
        }
        with patch.object(imp, 'get_json', side_effect=lambda sess, url: pages[url]):
            errs = imp.import_five_e_bits(self.db, {
                'base_url':'https://www.dnd5eapi.co/api', 'editions':['2014','2024']}, None, 0, 0)
        self.assertEqual([], errs)
        self.assertEqual(2, self.db.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        self.assertEqual([('2014',), ('2024',)], self.db.execute('SELECT edition FROM collections ORDER BY edition').fetchall())

    def test_open5e_pages(self):
        pages = {
            'https://api.open5e.com/v2/': {'items': 'https://api.open5e.com/v2/items/'},
            'https://api.open5e.com/v2/items/': {'count':2, 'results':[{'id':'a','name':'A','document': {'key':'first'}}], 'next':'?page=2'},
            'https://api.open5e.com/v2/items/?page=2': {'count':2, 'results':[{'id':'b','name':'B'}], 'next':None}
        }
        with patch.object(imp, 'get_json', side_effect=lambda sess, url: pages[url]):
            errs = imp.import_open5e(self.db, {'root_url':'https://api.open5e.com/v2/'}, None, 0, 0)
        self.assertEqual([], errs)
        self.assertEqual(['A', 'B'], [r[0] for r in self.db.execute('SELECT name FROM records ORDER BY id')])

    def test_json_entries_preserves_single_document(self):
        doc = {'name':'Magic Sword', 'system':{'damage':'1d8'}}
        self.assertEqual([('',0,doc)], list(imp.json_entries(doc)))
        grouped = {'weapons':[{'name':'Spear'}], 'armor':[{'name':'Plate'}]}
        self.assertEqual(2, len(list(imp.json_entries(grouped))))

    def test_source_file_schema(self):
        cid = imp.add_collection(self.db, 'weapons')
        imp.add_record(self.db, cid, 'spear', {'name':'Spear','nested':{'property':1}})
        self.assertEqual(1, self.db.execute("SELECT json_extract(data_json,'$.nested.property') FROM records").fetchone()[0])
        self.assertEqual(1, self.db.execute('SELECT records FROM catalog').fetchone()[0])

if __name__ == '__main__':
    unittest.main()
