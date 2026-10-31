import json
import urllib.request
from pathlib import Path

root = Path(__file__).resolve().parents[1]
base = 'https://raw.githubusercontent.com/KaiyueSun98/T2V-CompBench/V2/prompts/'
sources = [('dynamic_attr', '2_dynamic_attr.txt'), ('action_binding', '5_action_binding.txt'), ('interaction', '6_interaction.txt')]
template = json.loads((root / 'tasks' / '020_moviegenbench_h3skill_20.json').read_text(encoding='utf-8'))
step = template['steps'][0]
jobs, manifest = [], []
for category, filename in sources:
    raw = urllib.request.urlopen(base + filename, timeout=30).read().decode('utf-8-sig')
    lines = [(i, line.strip()) for i, line in enumerate(raw.splitlines(), 1) if line.strip()]
    assert len(lines) >= 10, (filename, len(lines))
    for line_number, original in lines[:10]:
        index = len(jobs) + 1
        name = f'S{index:02d}'
        prompt = (
            'integrated_multimodal_description: [Shot 1] ' + original +
            ' Preserve the original subjects, their exact attributes, quantities, spatial relations, actions, and interactions. '
            'A single continuous five-second shot; frame all relevant subjects clearly and keep their identities stable. '
            'Use coherent physical motion, realistic timing and a stable cinematic camera. '
            'Do not add events, characters, objects, cuts or visual overlays.\n\n'
            'overall_soundscape: Natural subtle environmental ambience appropriate to the described scene; no spoken dialogue.\n\n'
            'non_diegetic_music: None.'
        )
        jobs.append({'name': name, 'set': {'140:131.prompt': prompt, '140:133.value': 5,
                                          '92.filename_prefix': f'video/021_t2v_compbench_h3_{name}'}})
        manifest.append({'shot': name, 'category': category, 'source_file': filename,
                         'source_line': line_number, 'original_prompt': original, 'h3_prompt': prompt})
task = {'name': '021_t2v_compbench_h3_30', 'steps': [{'id': 'video', 'workflow': step['workflow'],
         'endpoint': step['endpoint'], 'foreach': jobs}]}
assert step['endpoint'] == 'pika'
out = root / 'tasks' / '021_t2v_compbench_h3_30.json'
assert not out.exists(), out
out.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding='utf-8')
(root / 'tasks' / '021_t2v_compbench_h3_30_manifest.json').write_text(
    json.dumps({'source': 'KaiyueSun98/T2V-CompBench V2', 'samples': manifest}, ensure_ascii=False, indent=2), encoding='utf-8')
print('CREATED', out, 'JOBS', len(jobs))
for x in manifest: print(x['shot'], x['category'], x['source_line'], x['original_prompt'][:120])
