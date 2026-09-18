import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import test_transfer
from transfer_core import database
from desktop_restore import restore_desktop, preview_restore, recover_pending, require_codex_closed


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_transfer.TransferTests()
        self.fixture.setUp()
        f = self.fixture
        f.export()
        self.target_home = f.root / 'destination-home'
        self.target_home.mkdir()
        self.parent = f.root / 'destination-projects'
        self.parent.mkdir()
        self.existing = str(uuid.uuid4())
        with database(self.target_home / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL, metadata TEXT NOT NULL, position INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)')
            db.execute('CREATE TABLE project_roots(project_id TEXT NOT NULL REFERENCES projects(id), position INTEGER NOT NULL, path TEXT NOT NULL, PRIMARY KEY(project_id,position))')
            db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,title TEXT,archived INTEGER,history_mode TEXT,project_id TEXT REFERENCES projects(id),thread_section_id TEXT,is_pinned INTEGER)')
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?,?)', ('existing-project','Existing','{}',0,1,1))
            db.execute('INSERT INTO project_roots VALUES(?,?,?)', ('existing-project',0,str(f.root/'existing-project')))
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?)', (self.existing,'untouched','old.jsonl','Existing chat',0,'legacy','existing-project',None,0))
        self.global_path = self.target_home / '.codex-global-state.json'
        self.original_global = {'unrelated': {'keep': True}, 'local-projects': {'existing-project': {'name': 'Existing'}}, 'project-order': ['existing-project']}
        self.global_path.write_text(json.dumps(self.original_global),encoding='utf-8')
        (self.target_home/'auth.json').write_text('target credentials',encoding='utf-8')
        (self.target_home/'session_index.jsonl').write_text(json.dumps({'id':self.existing})+'\n',encoding='utf-8')

    def tearDown(self):
        self.fixture.tearDown()

    def restore(self, **kwargs):
        return restore_desktop(self.fixture.package,self.parent,self.target_home,process_check=lambda:None,**kwargs)

    def test_original_folder_name_and_desktop_registration(self):
        r=self.restore(); target=self.parent/self.fixture.project.name
        self.assertEqual(r['project_path'],str(target))
        self.assertTrue((target/'main.py').exists())
        self.assertFalse((target/'project').exists())
        self.assertFalse((target/'codex').exists())
        with database(self.target_home/'state_5.sqlite') as db:
            self.assertEqual(db.execute('SELECT title FROM threads WHERE id=?',(self.existing,)).fetchone()[0],'Existing chat')
            row=db.execute('SELECT cwd,project_id,title,rollout_path FROM threads WHERE id=?',(self.fixture.sid,)).fetchone()
            self.assertEqual(row[:3],(str(target),r['project_id'],'대화 제목'))
            events=[json.loads(line) for line in Path(row[3]).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(events[0]['payload']['cwd'],str(target))
            self.assertEqual(events[2],self.fixture.events[2])
            self.assertEqual(db.execute('SELECT path FROM project_roots WHERE project_id=?',(r['project_id'],)).fetchone()[0],str(target))
        g=json.loads(self.global_path.read_text(encoding='utf-8'))
        self.assertEqual(g['unrelated'],{'keep':True})
        self.assertEqual(g['thread-project-assignments'][self.fixture.sid]['projectId'],r['project_id'])
        self.assertIn('existing-project',g['project-order'])
        self.assertEqual((self.target_home/'auth.json').read_text(),'target credentials')
        self.assertTrue((Path(r['backup_path'])/'state_5.sqlite').exists())
        self.assertEqual(json.loads((Path(r['backup_path'])/'journal.json').read_text(encoding='utf-8'))['status'],'committed')

    def test_duplicate_ids_are_copied_not_overwritten(self):
        with database(self.target_home/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET id=?',(self.fixture.sid,))
        info=preview_restore(self.fixture.package,self.parent,self.target_home)
        self.assertEqual(info['conflicts'],[self.fixture.sid])
        r=self.restore(); sid=r['sessions'][0]['id']
        self.assertNotEqual(sid,self.fixture.sid)
        with database(self.target_home/'state_5.sqlite') as db:
            self.assertEqual(db.execute('SELECT title FROM threads WHERE id=?',(self.fixture.sid,)).fetchone()[0],'Existing chat')
            path=db.execute('SELECT rollout_path FROM threads WHERE id=?',(sid,)).fetchone()[0]
        self.assertEqual(json.loads(Path(path).read_text(encoding='utf-8').splitlines()[0])['payload']['id'],sid)

    def test_existing_project_folder_rejected(self):
        dest=self.parent/self.fixture.project.name;dest.mkdir();(dest/'keep').write_text('keep')
        with self.assertRaises(FileExistsError):self.restore()
        self.assertEqual((dest/'keep').read_text(),'keep')

    def test_running_codex_prevents_mutation(self):
        def running():raise RuntimeError('Codex running')
        with self.assertRaisesRegex(RuntimeError,'running'):
            restore_desktop(self.fixture.package,self.parent,self.target_home,process_check=running)
        self.assertFalse((self.parent/self.fixture.project.name).exists())

    def test_exception_rolls_back_files_and_database(self):
        for point in ['database_prepared','files_published','database_committed']:
            with self.subTest(point=point):
                original=self.global_path.read_bytes()
                def fail(stage):
                    if stage==point:raise RuntimeError('injected failure')
                with self.assertRaisesRegex(RuntimeError,'injected'):self.restore(fault=fail)
                self.assertEqual(self.global_path.read_bytes(),original)
                self.assertFalse((self.parent/self.fixture.project.name).exists())
                with database(self.target_home/'state_5.sqlite') as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM threads').fetchone()[0],1)
                    self.assertEqual(db.execute('SELECT count(*) FROM projects').fetchone()[0],1)

    def test_unsupported_schema_stops_before_writes(self):
        with database(self.target_home/'state_5.sqlite') as db:db.execute('DROP TABLE project_roots')
        with self.assertRaisesRegex(ValueError,'저장 형식'):self.restore()
        self.assertFalse((self.parent/self.fixture.project.name).exists())

    def test_pending_recovery_preserves_project_files(self):
        r=self.restore();journal=Path(r['backup_path'])/'journal.json'
        state=json.loads(journal.read_text(encoding='utf-8'));state['status']='applying';journal.write_text(json.dumps(state),encoding='utf-8')
        recovered=recover_pending(self.target_home,process_check=lambda:None)
        self.assertEqual(len(recovered),1)
        self.assertTrue((Path(r['project_path'])/'main.py').exists())
        self.assertEqual(json.loads(self.global_path.read_text(encoding='utf-8')),self.original_global)
        with database(self.target_home/'state_5.sqlite') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM threads').fetchone()[0],1)

    def test_recovery_does_not_erase_later_chat_changes(self):
        r=self.restore();journal=Path(r['backup_path'])/'journal.json'
        state=json.loads(journal.read_text(encoding='utf-8'));state['status']='applying';journal.write_text(json.dumps(state),encoding='utf-8')
        with database(self.target_home/'state_5.sqlite') as db:db.execute('UPDATE threads SET title=? WHERE id=?',('Changed after restore',self.fixture.sid))
        before=self.global_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'대화가 변경'):recover_pending(self.target_home,process_check=lambda:None)
        self.assertEqual(self.global_path.read_bytes(),before)
        with database(self.target_home/'state_5.sqlite') as db:
            self.assertEqual(db.execute('SELECT title FROM threads WHERE id=?',(self.fixture.sid,)).fetchone()[0],'Changed after restore')


if __name__=='__main__':unittest.main()
