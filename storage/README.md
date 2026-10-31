# Storage: Image / Video / 3D / 3DGS

- **Modal Volume V2 `modal-comfyui-models-output`**: shared persistent staging mounted at `/outputs`, separated by workflow under `/outputs/{workflow}/`; B300 dual instances use `instance-1/` and `instance-2/`. Images may also be saved directly in the workflow output root.
  - The ComfyUI runtime checks outputs every 30 seconds and commits the Volume when eligible file metadata remains unchanged across two scans; graceful shutdown also commits. HF Sync independently archives stable files. A metadata-stable file may still be open, so this is best-effort crash protection, not a guarantee against interrupted writes.
- **Hugging Face Storage Bucket**: permanent source of truth at `comfyui/{workflow}/{category}/...` (category includes `image`, `video`, `3d`, `3dgs`; dual instance files use `{category}/instance-1/..` or `{category}/instance-2/..`).
- **Cloudflare R2**: preview images in `previews/{workflow}/{category}/...`; original files only on request at `downloads/comfyui/...`.
- **Local PC2 console**: the HF download button copies the original to R2 (or reuses an existing identical cached copy), then issues a one-hour presigned URL.
- **R2 downloads/** objects expire after one day; `previews/` and historical objects are not covered by that rule.

## Process

1. ComfyUI writes to `/outputs/{workflow}/` or `/outputs/{workflow}/instance-N/`, including files directly at the output root. The archiver normalizes paths under category-first keys.
2. `hf_sync.py` checks stable files every 15 seconds, verifies HF object size and SHA-256 metadata, then copies image previews to R2. Existing HF objects without SHA-256 metadata are re-uploaded once when encountered.
3. For video, it extracts the first frame using imageio-ffmpeg as `*_preview.png`, then archives the PNG in the next scan.
4. The local `storage/s3.py` implements the independent HF -> CF transfer and avoids retransfers using source ETag + size.

**Current preview limitation:** video previews are automatic. 3D/3DGS output files are archived, but real model renders are only available as PNGs if the corresponding workflow produces them. A ComfyUI interactive preview is not automatically a saved PNG.

**Important:** If HF upload is failing, leave the Modal account/Volume intact until the HF asset appears in the archive. The GPU container shrinking cannot be treated as proof of archival success. Historical R2 files are retained and visible but are not bulk-migrated to HF by this change.

This change affects new deployments; the local regression tests do not substitute for a live Modal Volume/HF/R2 verification.
