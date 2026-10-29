# Storage: 3D / 3DGS / Video

- **Modal Volume V2 `modal-comfyui-models-output`**: shared persistent staging mounted at `/outputs`, separated by workflow into `/outputs/{workflow}/3d/`, `3dgs/`, and `video/`.
  - The ComfyUI runtime checks outputs every 30 seconds and commits the Volume when eligible file metadata remains unchanged across two scans; graceful shutdown also commits. HF Sync independently archives stable files. A metadata-stable file may still be open, so this is best-effort crash protection, not a guarantee against interrupted writes.
- **Hugging Face Storage Bucket**: permanent source of truth at `comfyui/{workflow}/{category}/...`.
- **Cloudflare R2**: preview images in `previews/{workflow}/{category}/...`; original files only on request at `downloads/comfyui/...`.
- **Local PC2 console**: the HF download button copies the original to R2 (or reuses an existing identical cached copy), then issues a one-hour presigned URL.
- **R2 downloads/** objects expire after one day; `previews/` and historical objects are not covered by that rule.

## Process

1. ComfyUI writes files to `/outputs/{workflow}/<category>/`. Standard SaveVideo workflows already write under `video/`.
2. `hf_sync.py` checks stable files every 15 seconds, uploads to HF, verifies the remote size, then copies any preview images to the R2 preview mount.
3. For video, it extracts the first frame using imageio-ffmpeg as `*_preview.png`, and archives that image in the next scan.
4. The local `storage/s3.py` implements the independent HF -> CF transfer and avoids retransfers using source ETag + size.

**Current preview limitation:** video previews are automatic. 3D/3DGS output files are archived, but real model renders are only available as PNGs if the corresponding workflow produces them. A ComfyUI interactive preview is not automatically a saved PNG.

**Important:** If HF upload is failing, leave the Modal account/Volume intact until the HF asset appears in the archive. The GPU container shrinking cannot be treated as proof of archival success. Historical R2 files are retained and visible but are not bulk-migrated to HF by this change.

The only live deployed example so far is `community-infinisplat`; redeploy other workflows to use the new output handling.
