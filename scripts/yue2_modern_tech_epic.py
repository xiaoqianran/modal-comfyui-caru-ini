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
OUTPUT = ROOT / 'outputs/modern_tech_epic_20261031'
STYLE_BASE = ("Entirely original modern instrumental blockbuster soundtrack. Futuristic technological scale, high energy, massive cinematic impact, futuristic electronics and modern pop production, crystal-clear punchy modern mix, bold memorable NEW melodic motif. No nostalgia, no traditional Asian instruments, no folk instruments, no acoustic ethnic melodies. INSTRUMENTAL ONLY: no human voice, no singing, no spoken words, no lyrics, no humming, no choir, no chant, no vocoder, no vocal samples, no vocal chops, no voice-like lead sounds. Fully instrumental arrangement, modern high-fidelity studio mastering. ")
VARIANTS = [
 ('01_quantum_breakthrough', '量子突破', 202610312201, 'Epic futuristic hybrid orchestral electronic instrumental, 128 BPM driving 4/4, F minor. Invent a powerful short rising minor-sixth synth-brass instrumental hook answered by sharp syncopated descending notes. Modern EDM supersaw lead, immense synthetic sub bass, precise punchy four-on-floor kick, industrial cinematic toms, crisp hi hats, fast gated arpeggiators, high strings playing rhythmic ostinato, sharp brass stabs, distorted risers and futuristic impact sound design. Immediate punchy opening, relentless escalating tension, gigantic euphoric instrumental drop at midpoint, breathtaking wide stereo final climax. Massive high-tech spaceship launch and breakthrough atmosphere, heroic not sentimental. No voices of any kind.'),
 ('02_neon_megacity', '霓虹巨城', 202610312202, 'Modern cinematic cyberpunk electronic instrumental with powerful festival EDM production, 118 BPM 4/4, C sharp minor. Compose a NEW iconic five-note syncopated metallic pluck melody with rhythmic rests, repeat with changing harmonic answers. Aggressive analog bass synth, heavy sidechain-pumping electronic kick, tight electronic snare, glitchy percussion, shimmering digital arpeggiators, synthetic brass chords, crisp futuristic synth lead and atmospheric digital noise sweeps. Progressive club tension, crisp rhythmic groove, huge dark neon skyline panorama, massive bass drop with a memorable instrumental lead, high-energy technology show opening, confident forward momentum, no nostalgic or melancholy piano, absolutely no voice-like sounds.'),
 ('03_horizon_engine', '地平线引擎', 202610312203, 'Colossal modern science-fiction cinematic trailer score with hybrid electronic orchestra, 105 BPM cinematic 4/4, D minor shifting to F major. Compose a distinct ORIGINAL wide-interval heroic theme with a dramatic rising perfect-fifth motif and powerful instrumental answer. Huge low brass synth layers, staccato modern string ostinatos, aggressive synthetic percussion and massive low-frequency impacts, distorted electric guitar power-chord textures, analog modular arpeggiators, deep pulsing bass, thunderous cinematic drums, soaring bright synthesizer melody, high-speed risers. Build from intense futuristic pulse to awe-inspiring vast planetary panorama, extremely powerful climax with tightly layered instruments, confident triumphant resolution. High definition stadium-scale mix, no vocals or choir.')
]


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
            new_graph['8']['inputs']['seed']=seed
            new_graph['10']['inputs']['filename_prefix']='audio/modern_tech_epic_'+name
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