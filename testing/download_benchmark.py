"""Fresh Hunyuan3D model download benchmark, no persistent cache."""
import csv, os, tempfile, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import modal
app = modal.App('hunyuan3d-fresh-download-benchmark')
image = modal.Image.debian_slim(python_version='3.11').pip_install('huggingface_hub>=0.30,<2', 'hf_xet')
REPO = 'tencent/Hunyuan3D-2.1'
@app.function(image=image, cpu=8, memory=16384, timeout=3600)
def bench(workers: int):
    import resource
    from huggingface_hub import HfApi, hf_hub_download
    files = [(f.path, f.size) for f in HfApi(token="REDACTED_TOKEN").list_repo_tree(REPO, recursive=True, expand=True) if f.path.endswith(('.ckpt', '.safetensors', '.bin'))]
    if not files: raise RuntimeError('No model weight files found')
    with tempfile.TemporaryDirectory(prefix='fresh-hunyuan-') as directory:
        cache = str(Path(directory) / 'hub')
        os.environ['HF_HOME'] = directory
        os.environ['HF_XET_CACHE'] = str(Path(directory) / 'xet')
        os.environ['HF_XET_HIGH_PERFORMANCE'] = '1'
        def fetch(entry):
            name, expected = entry
            path = hf_hub_download(REPO, name, cache_dir=cache, force_download=True)
            size = Path(path).stat().st_size
            if expected and size != expected: raise RuntimeError(f'Incomplete: {name}')
            return size
        start = time.perf_counter(); cpu_start = time.process_time()
        with ThreadPoolExecutor(max_workers=workers) as pool: sizes = list(pool.map(fetch, files))
        elapsed = time.perf_counter() - start
        return dict(workers=workers, files=len(files), bytes=sum(sizes), wall_s=round(elapsed,2), cpu_s=round(time.process_time()-cpu_start,2), throughput_mib_s=round(sum(sizes)/1048576/elapsed,2), peak_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1))
@app.local_entrypoint()
def main():
    cases = [int(n) for n in os.getenv('BENCH_WORKERS','8,16').split(',')]
    if any(n<1 or n>32 for n in cases): raise ValueError('workers must be 1..32')
    rows = []
    for n in cases:
        print(f'Starting fresh Hunyuan3D download with {n} threads', flush=True)
        result = bench.remote(n); rows.append(result); print(result, flush=True)
    output = Path('testing') / ('hunyuan_benchmark_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'.csv')
    with output.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    print('Saved:',output,'; use Modal billing report for actual cost')
