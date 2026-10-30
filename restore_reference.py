"""Restore a reference PNG from numbered base64 chunks and verify SHA-256."""
import argparse, base64, hashlib
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument('chunk_dir', type=Path)
ap.add_argument('output', type=Path)
ap.add_argument('--sha256', required=True)
args = ap.parse_args()
chunks = sorted(args.chunk_dir.glob('*.b64'))
if not chunks:
    raise SystemExit('No .b64 chunks found; original reference not transferred')
data = b''.join(base64.b64decode(f.read_text(encoding='ascii'), validate=True) for f in chunks)
actual = hashlib.sha256(data).hexdigest()
if actual.lower() != args.sha256.lower():
    raise SystemExit(f'SHA-256 mismatch: {actual}')
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_bytes(data)
print(f'OK: {args.output} ({len(data)} bytes), SHA-256 {actual}')
