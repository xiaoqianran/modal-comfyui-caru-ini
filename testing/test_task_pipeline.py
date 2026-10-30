import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import batch_run as r

class PipelineTests(unittest.TestCase):
    def test_cross_endpoint_and_resume(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'endpoints.json').write_text(json.dumps({'a':'https://server-a','b':'https://server-b'}))
            (root/'graph.json').write_text(json.dumps({'1':{'inputs':{'image':'','prompt':''}}}))
            task={'name':'test','steps':[
                {'id':'first','endpoint':'a','workflow':'graph.json','set':{'1.prompt':'first'}},
                {'id':'second','endpoint':'b','workflow':'graph.json','set':{'1.image':'$first.image','1.prompt':'second'}}
            ]}
            (root/'task.json').write_text(json.dumps(task))
            submitted=[]
            uploaded=[]
            def fake_request(url, data=None, headers=None, timeout=300, retries=6):
                payload=json.loads(data)
                submitted.append((url,payload['prompt']))
                return json.dumps({'prompt_id':f'p{len(submitted)}'}).encode()
            def fake_wait(base,pid,timeout):
                return {'9':{'images':[{'filename':pid+'.png','type':'output'}]}}
            def fake_download(base,item,dest):
                dest.write_bytes(b'PNG-DATA')
            def fake_upload(base,path):
                uploaded.append((base,Path(path).read_bytes()))
                return 'uploaded.png'
            with patch.object(r,'request',side_effect=fake_request),patch.object(r,'wait_done',side_effect=fake_wait),patch.object(r,'download',side_effect=fake_download),patch.object(r,'upload',side_effect=fake_upload):
                r.run_task(root/'task.json',root/'endpoints.json')
                self.assertEqual(len(submitted),2)
                self.assertEqual(submitted[0][0],'https://server-a/prompt')
                self.assertEqual(submitted[1][0],'https://server-b/prompt')
                self.assertEqual(uploaded,[('https://server-b',b'PNG-DATA')])
                self.assertEqual(submitted[1][1]['1']['inputs']['image'],'uploaded.png')
                r.run_task(root/'task.json',root/'endpoints.json')
                self.assertEqual(len(submitted),2)
                status=root/'outputs/test/second/main/status.json'
                self.assertEqual(json.loads(status.read_text())['status'],'completed')
    def test_unknown_endpoint_and_uncertain(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'endpoints.json').write_text('{"a":"https://a"}')
            (root/'wf.json').write_text('{"1":{"inputs":{"prompt":"x"}}}')
            task={'name':'x','steps':[{'id':'one','endpoint':'a','workflow':'wf.json','set':{'1.prompt':'x'}}]}
            (root/'task.json').write_text(json.dumps(task))
            with patch.object(r,'request',side_effect=TimeoutError('timeout')):
                with self.assertRaisesRegex(RuntimeError,'submission uncertain'):
                    r.run_task(root/'task.json',root/'endpoints.json')
            with patch.object(r,'request') as mock_req:
                with self.assertRaisesRegex(RuntimeError,'status uncertain'):
                    r.run_task(root/'task.json',root/'endpoints.json')
                mock_req.assert_not_called()
if __name__=='__main__':
    unittest.main()
