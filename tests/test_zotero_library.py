import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import numpy as np
from research_rss import Paper, PaperStore, _analysis_hash, _content_hash, analyze_with_deepseek, select_papers
from zotero_library import MODEL, LibraryMatcher, read_library


class LibraryTests(unittest.TestCase):
    def test_only_personal_documents_and_standalone_pdfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'test.sqlite'
            c = sqlite3.connect(db)
            c.executescript('''
                CREATE TABLE libraries(libraryID,type);
                CREATE TABLE items(itemID,libraryID,itemTypeID,key);
                CREATE TABLE itemTypes(itemTypeID,typeName);
                CREATE TABLE itemData(itemID,fieldID,valueID);
                CREATE TABLE fields(fieldID,fieldName);
                CREATE TABLE itemDataValues(valueID,value);
                CREATE TABLE deletedItems(itemID);
                CREATE TABLE feedItems(itemID);
                CREATE TABLE itemAttachments(itemID,parentItemID,contentType,path);
                INSERT INTO libraries VALUES(1,'user'),(2,'feed'),(3,'group');
                INSERT INTO itemTypes VALUES(1,'journalArticle'),(2,'attachment'),(3,'note');
                INSERT INTO fields VALUES(1,'title'),(2,'abstractNote');
                INSERT INTO items VALUES(1,1,1,'A'),(2,2,1,'B'),(3,1,1,'C'),
                    (4,1,2,'D'),(5,1,2,'E'),(6,1,3,'F'),(7,3,1,'G');
                INSERT INTO deletedItems VALUES(3);
                INSERT INTO feedItems VALUES(2);
                INSERT INTO itemAttachments VALUES(4,1,'application/pdf','missing.pdf'),
                    (5,NULL,'application/pdf','missing.pdf');
            ''')
            for i in range(1,8):
                c.execute('INSERT INTO itemDataValues VALUES(?,?)', (i,f'title {i}'))
                c.execute('INSERT INTO itemData VALUES(?,1,?)',(i,i))
            c.commit()
            c.close()
            papers, stats = read_library(db, Path(tmp), {})
            self.assertEqual([p['key'] for p in papers], ['A','E'])
            self.assertEqual(stats['title_only'], 2)

    def test_matching_uses_entire_library(self):
        class Encoder:
            def embed(self, texts, **kwargs):
                return [np.array([0.,1.]) for _ in texts]
        profile = dict(model=MODEL, version='v1', vectors=[[1.,0.],[0.,1.]], papers=[
            dict(title='A', abstract='a', evidence_source='metadata'),
            dict(title='B', abstract='b', evidence_source='pdf_excerpt')])
        records = [dict(title='candidate')]
        LibraryMatcher(profile, Encoder()).match(records)
        self.assertEqual(records[0]['_library_context'][0]['title'], 'B')

    def test_interest_changes_do_not_invalidate_translation(self):
        record = dict(title='paper', abstract='abstract')
        self.assertNotEqual(_analysis_hash(record, {'_library_version':'a'}),
                            _analysis_hash(record, {'_library_version':'b'}))
        self.assertEqual(_content_hash(record, {'_library_version':'a'}),
                         _content_hash(record, {'_library_version':'b'}))

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'test'})
    def test_relevance_refresh_preserves_translation(self):
        store = PaperStore.empty()
        key = store.upsert(Paper('test','1','Paper','https://example.test'), now='2026-09-28')
        r = store.data['papers'][key]
        config = {'_library_version':'new', '_budget':Mock()}
        r.update(title_zh='translation', summary_zh='summary', content_hash=_content_hash(r, config))
        answer = dict(choices=[dict(message=dict(content=json.dumps([
            dict(id=key,relevance_score=80,tags=[],reason_zh='relevant',insight_zh='inference')])) )])
        with patch('research_rss._request',return_value=json.dumps(answer).encode()):
            analyze_with_deepseek(store,[key],config)
        self.assertEqual(r['title_zh'],'translation')
        self.assertEqual(r['summary_zh'],'summary')

    def test_failed_analysis_does_not_select_keyword_only_records(self):
        class Matcher:
            def match(self, records):
                for r in records:
                    r['_library_context'] = [{'abstract':'PRIVATE'}]
                    r['library_similarity'] = 0.9
        store = PaperStore.empty()
        key = store.upsert(Paper('test','1','GIS','https://example.test'), now='2026-09-28')
        config = {'_library_matcher':Matcher(), '_library_version':'v',
                  'topics':{'primary':['GIS']},'selection':{'min_score':0}}
        with patch('research_rss.analyze_with_deepseek',return_value='disabled:no-secret'):
            result = select_papers(store,config,'2026-09-28')
        self.assertEqual(result,[])
        self.assertNotIn('PRIVATE',json.dumps(store.data))


if __name__ == '__main__':
    unittest.main()
