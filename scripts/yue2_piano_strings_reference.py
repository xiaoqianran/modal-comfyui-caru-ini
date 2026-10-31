"""Compose independent, fully original multitimbral instrumentals using official YuE2 score transfer."""
import copy
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor_nodes'))
from batch_run import request, get_json, download, collect_files, _save_json
from yue2_instrumental.instrumentalize import convert_score
from yue2_instrumental.abc_tools import parse_abc

ENDPOINT = json.loads((ROOT / 'endpoints.json').read_text(encoding='utf-8'))['yue2'].rstrip('/')
TEMPLATE = json.loads((ROOT / 'workflows/comfyui-yue2-3b/audio_yue2_love_unspoken_piano_api.json').read_text(encoding='utf-8'))
OUTPUT = ROOT / 'outputs/piano_strings_reference_20261031'
STYLE_BASE = 'Entirely original instrumental cinematic virtuoso grand piano and symphonic strings, approximately 112 BPM with expressive rubato. Piano is clearly the lead instrument, fast rippling right hand arpeggios, cascading high-register runs, broad left-hand broken octaves, shifting minor-to-major harmonic colors, strong rhythmic pulse, rich romantic-modern harmonies, dramatic register contrasts, breathtaking climactic ascending runs. Full symphonic violin viola cello and double bass sections swell and answer the piano melody with lush counterpoint and broad sustained chords; piano stays foreground. Original memorable lyrical theme, exciting agile piano figuration, cinematic emotional development, grand orchestral crescendo and delicate final piano cadence. Inspired only by general virtuoso flowing piano texture and dramatic pacing of the user audio reference, do not transcribe or copy its melody, exact motifs or chord sequence. No voice, no singing, no humming, no choir, no speech, no vocals, no lyrics, no synth, no EDM drums, no ethnic instruments. High fidelity expressive concert performance.'
VARIANTS = [('01_celestial_cascade', '星穹流光', 202610312801, 'Original newly composed flowing piano and symphonic strings, virtuosic cascading piano melody, cinematic dramatic harmonic development, 120 second complete instrumental form.')]

def submit(graph, label):
    body=json.dumps({'client_id': 'yue2-modern-tech-epic', 'prompt':graph}, ensure_ascii=False).encode('utf-8')
    answer=json.loads(request(ENDPOINT+'/prompt',body,{'Content-Type':'application/json; charset=utf-8'}, timeout=80,retries=1))
    if answer.get('node_errors'):
        raise RuntimeError(f'{label}: {answer["node_errors"]}')
    return answer['prompt_id']


def history(pid):
    return get_json(ENDPOINT, '/history/'+pid).get(pid)


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, title, seed, description in VARIANTS:
        folder=OUTPUT/name
        folder.mkdir(exist_ok=True)
        record=folder/'status.json'
        st=json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
        if st.get('status')=='completed' and (folder/st['filename']).is_file():
            print(name,'COMPLETED (cached)',flush=True)
            continue
        if not st.get('planning_prompt_id'):
            plan={'15': copy.deepcopy(TEMPLATE['15']), '24':copy.deepcopy(TEMPLATE['24']), '99': {'class_type':'PreviewAny','inputs':{'source':['24',0]}}}
            plan['24']['inputs'].update(style=STYLE_BASE+description, lyrics='[Intro]\n\n[Verse]\n\n[Chorus]\n\n[Outro]', seed=seed)
            _save_json(folder/'planning_workflow.json',plan)
            st.update(title=title,seed=seed,planning_prompt_id=submit(plan,name),status='planning')
            _save_json(record,st)
            print(name,'PLAN SUBMITTED',st['planning_prompt_id'],flush=True)
        else:
            print(name,'PLAN RESUMED',st['planning_prompt_id'],flush=True)
    # Wait for planning, convert each plan, verify and submit its own render.
    remaining={name for name,*_ in VARIANTS}
    deadline=time.monotonic()+1500
    while remaining:
        for name, title, seed, description in VARIANTS:
            if name not in remaining:
                continue
            folder=OUTPUT/name
            record=folder/'status.json'
            st=json.loads(record.read_text(encoding='utf-8'))
            if st.get('generation_prompt_id'):
                remaining.remove(name)
                continue
            h=history(st['planning_prompt_id'])
            if not h:
                continue
            if h.get('status',{}).get('status_str') != 'success':
                raise RuntimeError(f'{name} planning failed: {h.get("status")}')
            abc=h['outputs']['99']['text'][0]
            (folder/'original.abc').write_text(abc,encoding='utf-8')
            transformed,report=convert_score(abc)
            if report['vocal_notes_after'] or not report['output_ins_notes']:
                raise ValueError(f'{name} Vocal notes remain!')
            (folder/'instrumental.abc').write_text(transformed,encoding='utf-8')
            _save_json(folder/'transfer.json', report)
            st.update(status='converted',vocal_before=report['vocal_notes_before'],vocal_after=report['vocal_notes_after'],ins_notes=report['output_ins_notes'])
            _save_json(record,st)
            print(name,'CONVERTED',report['vocal_notes_before'],'->',report['vocal_notes_after'],'Vocal; Ins',report['output_ins_notes'],flush=True)
            new_graph=copy.deepcopy(TEMPLATE)
            new_graph.pop('24')
            new_graph.pop('40')
            new_graph['25']['inputs'].update(style=STYLE_BASE+description,lyrics='',abc=transformed,seed=seed)
            new_graph['25']['inputs']['max_duration']=120
            new_graph['8']['inputs']['seed']=seed
            new_graph['10']['inputs']['filename_prefix']='audio/piano_strings_original_'+name
            _save_json(folder/'generation_workflow.json',new_graph)
            st['generation_prompt_id']=submit(new_graph,name)
            st['status']='submitted'
            _save_json(record,st)
            print(name,'AUDIO SUBMITTED',st['generation_prompt_id'],flush=True)
            remaining.remove(name)
        if remaining:
            if time.monotonic()>deadline:
                raise TimeoutError('Plan wait exceeded; rerun to resume')
            time.sleep(5)
    # Distinct score confirmation, based on parsed note events rather than text hashes.
    signatures=[]
    for name,*_ in VARIANTS:
        sc=parse_abc((OUTPUT/name/'instrumental.abc').read_text(encoding='utf-8'))
        notes=sc.voices['Ins'].notes
        signature=hashlib.sha256(repr(notes).encode()).hexdigest()
        signatures.append(signature)
        print(name,'SCORE SHA256',signature[:16],'note_events',len(notes),flush=True)
    if len(set(signatures)) != len(signatures):
        raise RuntimeError('Generated scores are not distinct!')
    # Fetch rendered audio outputs.
    waiting={name for name,*_ in VARIANTS}
    while waiting:
        for name in tuple(waiting):
            folder=OUTPUT/name
            record=folder/'status.json'
            st=json.loads(record.read_text(encoding='utf-8'))
            if st['status']=='completed':
                waiting.remove(name)
                continue
            h=history(st['generation_prompt_id'])
            if not h:
                continue
            status=h.get('status',{}).get('status_str')
            if status=='error':
                raise RuntimeError(f'{name} audio failed: {h.get("status")}')
            if status!='success':
                continue
            files=[f for f in collect_files(h.get('outputs',{})) if f['filename'].endswith('.flac')]
            if not files:
                raise RuntimeError(f'{name}: no flac output')
            target=folder/Path(files[0]['filename']).name
            if not target.is_file():
                download(ENDPOINT,files[0],target)
            st.update(status='completed',filename=target.name,bytes=target.stat().st_size)
            _save_json(record,st)
            print(name,'COMPLETED',target.name,st['bytes'],'bytes',flush=True)
            waiting.remove(name)
        if waiting:
            if time.monotonic()>deadline:
                raise TimeoutError('Audio wait exceeded; rerun to resume')
            time.sleep(5)
    print('ALL THREE MODERN TECHNOLOGY INSTRUMENTALS COMPLETE',flush=True)


if __name__=='__main__':
    main()