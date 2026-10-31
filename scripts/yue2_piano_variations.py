"""Submit and resume YuE2 piano performances from one validated instrumental ABC.

Uses native ComfyUI nodes only; instrument conversion is performed offline once.
Run: uv run python scripts/yue2_piano_variations.py tasks/010_love_unspoken_piano_comparison.json
"""
import argparse
import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor_nodes'))
from batch_run import _save_json, collect_files, download, get_json, request
from yue2_instrumental.abc_tools import parse_abc


def submit(task_file, only_check=False, timeout=1500):
    task = json.loads(task_file.read_text(encoding='utf-8'))
    score_path = ROOT / task['score']
    score_text = score_path.read_text(encoding='utf-8')
    score = parse_abc(score_text)
    if score.voices['Vocal'].notes or not score.voices['Ins'].notes:
        raise ValueError('ABC must have no Vocal notes and at least one Ins note')
    endpoints = json.loads((ROOT / 'endpoints.json').read_text(encoding='utf-8'))
    base = endpoints[task['endpoint']].rstrip('/')
    original = json.loads((ROOT / task['workflow']).read_text(encoding='utf-8'))
    if original['25']['inputs'].get('abc') != ['40', 0]:
        raise ValueError('Expected instrumental workflow template with ABC conversion node')
    out = ROOT / task['output']
    out.mkdir(parents=True, exist_ok=True)
    active = {}
    for variant in task['variants']:
        name = variant['id']
        if '/' in name or '\\' in name or name.startswith('.'):
            raise ValueError(f'Invalid name: {name}')
        folder = out / name
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / 'status.json'
        previous = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {}
        if previous.get('status') == 'completed' and (folder / previous.get('filename', '-')).is_file():
            print(name, 'already completed; skip', flush=True)
            continue
        if previous.get('prompt_id'):
            pid = previous['prompt_id']
            print(name, 'resume', pid, flush=True)
        elif only_check:
            print(name, 'not submitted', flush=True)
            continue
        else:
            graph = copy.deepcopy(original)
            graph.pop('24')
            graph.pop('40')
            graph['25']['inputs']['abc'] = score_text
            graph['25']['inputs']['lyrics'] = ''
            graph['25']['inputs']['style'] = variant['style']
            graph['25']['inputs']['seed'] = variant['seed']
            graph['8']['inputs']['seed'] = variant['seed']
            graph['10']['inputs']['filename_prefix'] = 'audio/love_unspoken_' + name
            _save_json(folder / 'workflow.json', graph)
            body = json.dumps({'prompt': graph, 'client_id': 'yue2-piano-comparison'}, ensure_ascii=False).encode('utf-8')
            reply = json.loads(request(base + '/prompt', body, {'Content-Type':'application/json; charset=utf-8'}, timeout=90, retries=1))
            if reply.get('node_errors'):
                raise RuntimeError(f'Node errors for {name}: {reply["node_errors"]}')
            pid = reply['prompt_id']
            _save_json(manifest, {'status':'submitted','prompt_id':pid,'reference':variant['reference_technique'],'seed':variant['seed']})
            print(name, 'submitted', pid, flush=True)
        active[name] = (pid, folder, manifest)
    started = time.monotonic()
    while active:
        for name, (pid, folder, manifest) in list(active.items()):
            history = get_json(base, f'/history/{pid}')
            if pid not in history:
                continue
            record = history[pid]
            state = record.get('status', {}).get('status_str')
            if state == 'error':
                _save_json(manifest, {'status':'error','prompt_id':pid,'remote_status':record['status']})
                raise RuntimeError(f'{name}: remote failed: {record["status"]}')
            if state != 'success':
                continue
            outputs = collect_files(record.get('outputs', {}))
            audio = [i for i in outputs if i['filename'].lower().endswith('.flac')]
            if not audio:
                raise RuntimeError(f'{name}: success without FLAC output')
            target = folder / Path(audio[0]['filename']).name
            if not target.is_file():
                download(base, audio[0], target)
            _save_json(manifest, {'status':'completed','prompt_id':pid,'filename':target.name,'bytes':target.stat().st_size,'reference':next(v['reference_technique'] for v in task['variants'] if v['id']==name)})
            print(name, 'COMPLETED',target,'bytes',target.stat().st_size, flush=True)
            del active[name]
        if not active:
            break
        if time.monotonic()-started > timeout:
            print('Still pending:', ','.join(active), 'resume by rerunning script', flush=True)
            return 3
        time.sleep(5)
    return 0

if __name__=='__main__':
    cli=argparse.ArgumentParser()
    cli.add_argument('task',type=Path)
    cli.add_argument('--check',action='store_true')
    cli.add_argument('--timeout',type=int,default=1500)
    args=cli.parse_args()
    raise SystemExit(submit((ROOT / args.task).resolve(), only_check=args.check, timeout=args.timeout))