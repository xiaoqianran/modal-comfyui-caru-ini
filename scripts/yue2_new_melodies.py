"""Generate three DIFFERENT original YuE2 scores; transfer Vocal notes, render solo-piano audio."""
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
OUTPUT = ROOT / 'outputs/love_unspoken_piano_new_melodies'
STYLE_BASE = ('Entirely original instrumental solo acoustic grand piano composition, Korean romantic television drama emotional atmosphere, exclusively one real concert grand piano, no singing, no human voice, no choir, no humming, no spoken words, no strings, no drums, no guitars, no synthesizer, no other instruments. Original memorable lyrical piano motif, genuine instrumental composition with natural piano playing and sustain pedal, no pre-existing soundtrack melody. ')
VARIANTS = [
 ('01_first_meeting', '初见·心动', 202610311201, 'A tender first-meeting love theme, graceful G major warmth, light lilting 6/8 feeling around 72 BPM. Compose a NEW distinctive short ascending three-note pickup motive answered by descending stepwise melodic motion, leave spacious rests between phrases, delicate right-hand singing notes above sparse left-hand broken chords, quiet high-register embellishments, a single subtle glowing climax and a hopeful gentle cadence. Intimate, innocent, warm, hopeful and understated.'),
 ('02_unspoken_longing', '未说出口·思念', 202610311202, 'A wistful unspoken-love theme, lyrical E minor atmosphere near 66 BPM and expressive 4/4 rubato. Compose an entirely NEW melody beginning with a sustained midrange tone followed by a descending minor-third gesture, deliberate pauses, a rising question-like response, emotive minor-key tension and a wistful unresolved cadence. Delicate left-hand rocking arpeggios, rich suspensions, subtle inner-voice movement, vulnerable pianissimo and one achingly tender middle climax. Quietly bittersweet, reserved and nostalgic.'),
 ('03_reunion', '久别重逢·释然', 202610311203, 'An uplifting yet tearful reunion love theme, D major romantic concert piano at a moderate 78 BPM. Compose a DIFFERENT new melody led by a bold upward fourth leap followed by a flowing descending phrase and contrasting syncopated rhythmic answer, a clear recurring theme that opens into broad octaves and luminous harmonic changes. Low-register bass gently supports independent flowing right-hand melody, expressive legato and chord voicing, increasingly brilliant but tasteful arpeggios, soaring cathartic climax, resolve to calm warm final chord. Cinematic, hopeful, tender relief.')
]


def submit(graph, label):
    body=json.dumps({'client_id': 'yue2-original-piano-melodies', 'prompt':graph}, ensure_ascii=False).encode('utf-8')
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
            new_graph['8']['inputs']['seed']=seed
            new_graph['10']['inputs']['filename_prefix']='audio/love_new_'+name
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
    print('ALL THREE NEW MELODIES COMPLETE',flush=True)


if __name__=='__main__':
    main()