"""Exercise desktop project/list, thread/list and thread/read on an isolated real backend."""
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid

from desktop_restore import restore_desktop, insert_row
from transfer_core import database, filtered_database, export_bundle


class Backend:
    def __init__(self,home,cwd):
        env=os.environ.copy();env['CODEX_HOME']=str(home)
        self.proc=subprocess.Popen([shutil.which('codex'),'app-server','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,encoding='utf-8',env=env,cwd=cwd)
        self.messages=queue.Queue();self.counter=0
        def reader():
            for line in self.proc.stdout:
                try:self.messages.put(json.loads(line))
                except ValueError:pass
        self.reader=threading.Thread(target=reader,daemon=True);self.reader.start()
        self.call('initialize',{'clientInfo':{'name':'desktop-transfer-test','version':'0.2'},'capabilities':{'experimentalApi':True}})
        self.proc.stdin.write('{"method":"initialized"}\n');self.proc.stdin.flush()
    def call(self,method,params):
        self.counter+=1;current=self.counter
        self.proc.stdin.write(json.dumps({'id':current,'method':method,'params':params})+'\n');self.proc.stdin.flush()
        while True:
            result=self.messages.get(timeout=30)
            if result.get('id')==current:
                if 'error' in result:raise AssertionError(result['error'])
                return result['result']
    def close(self):
        self.proc.terminate();self.proc.wait(timeout=10);self.reader.join(timeout=5)
        self.proc.stdin.close();self.proc.stdout.close()


@unittest.skipUnless(shutil.which('codex'),'Codex backend unavailable')
class DesktopBackendTests(unittest.TestCase):
    def test_project_and_full_chat_visible_in_real_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);home=root/'target-home';home.mkdir();source=root/'source-home';(source/'sessions').mkdir(parents=True)
            project=root/'MyProject';project.mkdir();(project/'hello.txt').write_text('hello')
            backend=Backend(home,root)
            try:
                existing=backend.call('project/create',{'name':'Existing','roots':[{'path':str(root)}],'idempotencyKey':str(uuid.uuid4())})
            finally:backend.close()
            dbpath=next(home.glob('state_*.sqlite'))
            filtered_database(dbpath,source/dbpath.name,set())
            sid=str(uuid.uuid4());turn=str(uuid.uuid4());rollout=source/'sessions'/'source.jsonl'
            def e(kind,payload):return {'timestamp':'2026-09-18T00:00:00Z','type':kind,'payload':payload}
            events=[e('session_meta',{'id':sid,'timestamp':'2026-09-18T00:00:00Z','cwd':str(project),'originator':'codex_vscode','cli_version':'0.114.0','source':'vscode','model_provider':'openai'}),
                    e('event_msg',{'type':'task_started','turn_id':turn,'model_context_window':200000}),
                    e('event_msg',{'type':'user_message','message':'desktop fixture question','images':[],'local_images':[],'text_elements':[]}),
                    e('response_item',{'type':'message','role':'user','content':[{'type':'input_text','text':'desktop fixture question'}]}),
                    e('response_item',{'type':'message','role':'assistant','content':[{'type':'output_text','text':'desktop fixture answer'}]}),
                    e('event_msg',{'type':'agent_message','message':'desktop fixture answer','phase':'final_answer'}),
                    e('event_msg',{'type':'task_complete','turn_id':turn,'last_agent_message':'desktop fixture answer'})]
            rollout.write_text(''.join(json.dumps(event)+'\n' for event in events),encoding='utf-8')
            with database(source/dbpath.name) as db:
                insert_row(db,'threads',{'id':sid,'cwd':str(project),'rollout_path':str(rollout),'created_at':1789689600,'updated_at':1789689600,
                    'source':'vscode','model_provider':'openai','title':'Desktop transfer fixture','sandbox_policy':'{"type":"workspace-write"}',
                    'approval_mode':'on-request','first_user_message':'desktop fixture question','preview':'desktop fixture question','has_user_event':1,'history_mode':'paginated'})
            package=root/'fixture.codextransfer';export_bundle(project,source,package)
            receipt=restore_desktop(package,root/'restored',home,process_check=lambda:None)
            backend=Backend(home,root)
            try:
                projects=backend.call('project/list',{})
                self.assertIn(receipt['project_id'],json.dumps(projects))
                self.assertIn('Existing',json.dumps(projects))
                threads=backend.call('thread/list',{'projectId':receipt['project_id'],'sourceKinds':['vscode'],'limit':20})
                self.assertIn(sid,json.dumps(threads))
                chat=backend.call('thread/read',{'threadId':sid,'includeTurns':True})
                self.assertEqual(chat['thread']['cwd'],receipt['project_path'])
                self.assertIn('desktop fixture question',json.dumps(chat))
                self.assertIn('desktop fixture answer',json.dumps(chat))
            finally:backend.close()


if __name__=='__main__':unittest.main()
