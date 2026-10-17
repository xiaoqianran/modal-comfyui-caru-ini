# Community SHARP

Default deployment: `sharp_basic.json`.

All four upstream UI workflows are at the directory root and mounted into the deployed ComfyUI user workflow browser.

## Model
Apple SHARP checkpoint `sharp_2572gikvuh.pt` is downloaded automatically by the plugin on first inference to `ComfyUI/models/sharp/`. It is NOT baked into the image by `models.json`, and must be downloaded again for a fresh ephemeral container unless cached elsewhere.

## Plugins
`PozzettiAndrea/ComfyUI-GaussianPack` is explicitly cloned and its requirements installed before `PozzettiAndrea/ComfyUI-Sharp`; SHARP's `install.py` handles its comfy-env setup and any additional nodes. Panorama examples may need additional nodes. `sharp_basic.json` is the default deployment selection; GPU inference has not yet been validated.

## Storage
Only the selected workflow's local workflow JSON and configurations are copied into the Modal image, plus the small `examples/` files for ComfyUI's workflow browser. Local directories elsewhere are not uploaded. The generated 3D files under ComfyUI/output/3d are written to R2 `comfyui/community-sharp/3d/`.
