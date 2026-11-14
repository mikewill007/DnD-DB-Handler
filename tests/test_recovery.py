import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import import_dnd as imp

class RecoveryTests(unittest.TestCase):
    def test_bad_detail_link_does_not_abort_following_records(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        imp.init_db(db, {'id':'test','name':'test','type':'test'}, 0)
        pages={
          'https://www.dnd5eapi.co/api/2024':{'proficiencies':'/api/2024/proficiencies'},
          'https://www.dnd5eapi.co/api/2024/proficiencies':{'count':3,'results':[
            {'index':'valid-a','url':'/api/2024/proficiencies/valid-a'},
            {'index':'carpenters-tools','url':'/api/2024/proficiencies/tool-carpenters-tools'},
            {'index':'valid-b','url':'/api/2024/proficiencies/valid-b'}]},
          'https://www.dnd5eapi.co/api/2024/proficiencies/valid-a':{'index':'valid-a','name':'A'},
          'https://www.dnd5eapi.co/api/2024/proficiencies/valid-b':{'index':'valid-b','name':'B'},
        }
        def fetch(_,url):
            if url.endswith('/tool-carpenters-tools'):
                raise ValueError('404 Not Found')
            return pages[url]
        with patch.object(imp,'get_json',side_effect=fetch):
            errors=imp.import_five_e_bits(db,{'base_url':'https://www.dnd5eapi.co/api','editions':['2024']},None,0,0)
        self.assertEqual(1,len(errors))
        self.assertEqual(2,db.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        self.assertEqual(1,db.execute('SELECT COUNT(*) FROM fetch_errors').fetchone()[0])
        self.assertEqual(('error',3,2),db.execute('SELECT status,expected_count,fetched_count FROM collections').fetchone())
    def test_save_database_lock_preserves_staging(self):
        with tempfile.TemporaryDirectory() as td:
            stage=Path(td)/'new.sqlite'
            target=Path(td)/'current.sqlite'
            stage.write_bytes(b'new file')
            target.write_bytes(b'old file')
            with patch.object(Path, 'replace', side_effect=PermissionError('in use')):
                self.assertFalse(imp.save_database(stage,target))
            self.assertEqual(b'new file',stage.read_bytes())
            self.assertEqual(b'old file',target.read_bytes())
if __name__=='__main__': unittest.main()
