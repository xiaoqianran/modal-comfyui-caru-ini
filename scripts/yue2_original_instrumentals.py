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
OUTPUT = ROOT / 'outputs/original_instrumentals_20261031'
STYLE_BASE = ("Original instrumental-only score for cinematic soundtracks. INSTRUMENTAL MUSIC ONLY. Absolutely no vocals, singing, speech, lyrics, choir, chants, humming, human voice or vocal-like synthesizer. Empty lyrics at audio stage. Compose an entirely new recognizable instrumental motif, cohesive harmonic motion and paced arrangement, never quote existing melodies. Natural instruments and clear melodic development. ")
VARIANTS = [
 ('01_starlit_letters', '星河来信', 202610311401, 'Cinematic chamber ambient instrumental: expressive solo cello plays a completely original five-note motif over spacious felt piano chords and glittering celesta accents, subtle bowed violin and viola harmonic swells, soft warm double-bass foundation. 76 BPM expressive 6/8, E-flat major with moments of C minor. Intro: distant celesta and sparse piano; verse: cello enters on a downward sixth then rises in an intimate arch; theme: violin harmonizes cello melody; climax: wide cinematic string lift without percussion; outro: solitary celesta and piano fading into silence. Tender, yearning, bittersweet but hopeful, high-quality film soundtrack recording. Strictly instruments only, NO vocals.'),
 ('02_rain_on_glass', '玻璃上的雨', 202610311402, 'Original instrumental electronic minimalism: mellow analog synth pluck melody and wooden marimba with a soft Rhodes electric piano, warm subbass, restrained nonvocal atmospheric granular textures, small downtempo electronic kick and hi-hat groove. 92 BPM, D dorian, gently syncopated repeated four-note motif, arpeggio variations, rhythmic displacement, carefully articulated percussion, gradual harmonic brightness from minor to suspended voicings, a memorable melodic hook without singers; second section features marimba call-and-response with Rhodes; contemplative nocturnal city rain with hopeful sunrise. Clean cinematic instrumental only, no human samples and absolutely no voice-like pads.'),
 ('03_mountains_after_snow', '雪后千山', 202610311403, 'Original East Asian cinematic instrumental soundtrack: solo guqin plucked strings introduces a distinct pentatonic ascending-fourth motif, airy Chinese bamboo dizi flute plays expressive reply, warm erhu solo carries the main melody, understated concert harp arpeggios and chamber string orchestra sustain distant harmonies, soft low taiko and woodblock percussion only for dramatic swells. 78 BPM with spacious rubato, D pentatonic major and wistful B minor colors. Open with guqin harmonics; dizi melody expands into reflective spacious landscape, emotional erhu bridge, soaring strings at one restrained peak, close with only guqin plucks and wind chime resonance. Elegant and vividly acoustic, purely instrumental, no vocals, no chants or choir.')
]


def submit(graph, label):
    body=json.dumps({'client_id': 'yue2-original-instrumentals', 'prompt':graph}, ensure_ascii=False).encode('utf-8')
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
            new_graph['10']['inputs']['filename_prefix']='audio/original_instrumental_'+name
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
    print('ALL THREE NEW INSTRUMENTAL COMPOSITIONS COMPLETE',flush=True)


if __name__=='__main__':
    main()