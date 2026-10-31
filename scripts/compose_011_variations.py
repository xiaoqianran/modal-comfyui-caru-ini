import json,random,sys,hashlib
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'vendor_nodes'))
from yue2_instrumental.instrumentalize import convert_score
from yue2_instrumental.abc_tools import parse_abc
out=root/'outputs/011-piano-strings-innovations/composed';out.mkdir(parents=True,exist_ok=True)
ideas=[('03-northern-lights',113,'D minor',128,['D','F','A','c','d','f','a','c\''],['D','E','F','A','c','d','e','g'],['F','A','c','e','f','a','c\'','e\'']),('04-solar-spiral',227,'A minor',128,['A,','C','E','G','A','c','e','g'],['A,','B,','D','F','A','d','f','a'],['C','E','G','B','c','e','g','b']),('05-prism-odyssey',331,'E minor',128,['E,','G,','B,','D','E','G','b','d\''],['G,','B,','D','F','G','b','d\'','f\''],['C','E','G','B','c','e','g','b'])]
for name,seed,key,bpm,theme1,theme2,theme3 in ideas:
    rng=random.Random(seed);sections=[theme1,theme2,theme3,theme1,theme2,theme3,theme1,theme3]
    bars=[]
    for sec in range(8):
        pool=sections[sec]
        for j in range(8):
            # Eight eighth-notes per bar. Vary rhythmic pitch contour, octave range and register.
            if sec in (0,3): idx=[0,2,4,6,7,5,3,1] if j%2==0 else [1,3,5,7,6,4,2,0]
            elif sec in (1,4): idx=[7,6,4,2,0,1,3,5] if j%2==0 else [4,2,0,3,5,7,6,1]
            elif sec in (2,5): idx=[0,4,7,3,1,5,6,2] if j%2==0 else [7,3,0,5,2,6,1,4]
            else: idx=[0,3,5,1,6,2,7,4] if j%2==0 else [5,2,7,1,4,0,6,3]
            idx=[(k+(j//2)%3)%8 for k in idx]
            if j in (3,7):idx=idx[2:]+idx[:2]
            bars.append(''.join(pool[k] for k in idx))
    abc='X:1\nT:'+''+'\nM:4/4\nL:1/8\nQ:1/4='+str(bpm)+'\nV: Vocal clef=treble name="Vocal Melody" snm="Vocal"\nV: Ins clef=treble name="Ins Melody" snm="Inst."\nK:'+key.split()[0]+'m\n'
    for sec in range(16):
        abc+='% '+['intro','verse','chorus','verse','bridge','chorus','verse','outro'][sec//2]+'\nV: Vocal\n'+'|'.join('z8' for _ in range(4))+'|\nV: Ins\n'+'|'.join(bars[sec*4:(sec+1)*4])+'|\n'
    try:
        converted,report=convert_score(abc)
    except Exception as e:print(name,'INVALID',str(e));continue
    (out/(name+'.abc')).write_text(converted,encoding='utf-8')
    print(name,'VALID',report['output_ins_notes'],'notes',hashlib.sha256(converted.encode()).hexdigest()[:12],flush=True)
    base=json.loads((root/'workflows/comfyui-yue2-3b/audio_yue2_piano_strings_runner_api.json').read_text(encoding='utf-8-sig'))
    base['25']['inputs']['abc']=converted
    base['25']['inputs']['style']='Original instrumental cinematic virtuoso grand piano lead and lush full symphonic strings. '+['Brilliant fast piano arpeggios and violin countermelody, eight clearly contrasting sections, shifting themes, soaring orchestral crescendo.','Restless dramatic rhythmic piano ostinato changing every eight bars, mysterious contrasting lyrical middle, agile contrapuntal cello and violins, explosive piano finale.','Luminous modern classical piano concerto, intricate dancing right hand, sweeping string harmony, inventive melodic variations, strong dynamic arc, cinematic emotional resolution.'][len(list(out.glob('*.abc')))-1]+' No vocals, no choir, no speech, no drums, no electronics.'
    base['25']['inputs']['max_duration']=120
    base['25']['inputs']['seed']=seed+202610310000
    base['8']['inputs']['seed']=seed+202610310000
    base['10']['inputs']['filename_prefix']='audio/011_'+name.replace('-','_')
    (out/(name+'.workflow.json')).write_text(json.dumps(base,ensure_ascii=False,indent=2),encoding='utf-8')
