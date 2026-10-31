import json,sys,time,hashlib
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'vendor_nodes'))
sys.path.insert(0,str(root))
from batch_run import get_json
from yue2_instrumental.instrumentalize import convert_score
from yue2_instrumental.abc_tools import parse_abc
folder=root/'outputs/011-piano-strings-reference/composition'
base=json.loads((root/'endpoints.json').read_text(encoding='utf-8'))['yue2'].rstrip('/')
items=sorted(folder.glob('*.status.json'))
assert len(items)==3
rows=[]
for path in items:
    st=json.loads(path.read_text(encoding='utf-8-sig'))
    for _ in range(240):
        h=get_json(base,'/history/'+st['prompt_id']).get(st['prompt_id'])
        if h:break
        time.sleep(5)
    else:raise TimeoutError(st['id'])
    if h['status']['status_str']!='success':raise RuntimeError(str(h['status']))
    abc=h['outputs']['99']['text'][0]
    converted,report=convert_score(abc)
    assert report['vocal_notes_after']==0 and report['output_ins_notes']>0
    (folder/(st['id']+'.abc')).write_text(converted,encoding='utf-8')
    sig=hashlib.sha256(repr(parse_abc(converted).voices['Ins'].notes).encode()).hexdigest()
    rows.append((st,converted,report,sig))
    print(st['id'],'notes',report['output_ins_notes'],'signature',sig[:16],flush=True)
assert len({r[3] for r in rows})==3,'Duplicate melody score'
template=json.loads((root/'workflows/comfyui-yue2-3b/audio_yue2_piano_strings_runner_api.json').read_text(encoding='utf-8-sig'))
entries=[]
for st,abc,report,sig in rows:
    entries.append({'name':st['id'],'set':{'25.abc':abc,'25.style':st['style'],'25.lyrics':'','25.seed':st['seed'],'8.seed':st['seed'],'10.filename_prefix':'audio/011_'+st['id'].replace('-','_')}})
task={'name':'011-piano-strings-innovations','mode':'batch','steps':[{'id':'music','endpoint':'yue2','workflow':'workflows/comfyui-yue2-3b/audio_yue2_piano_strings_runner_api.json','foreach':entries}]}
path=root/'tasks/011-piano-strings-innovations.json'
path.write_text(json.dumps(task,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('TASK_READY',path,flush=True)
