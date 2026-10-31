# Pika H3 Flex FL2VA — official ComfyUI workflow, BF16 weights
Source: https://github.com/Comfy-Org/workflow_templates
Files: video_pika_h3_flex_t2v.json and video_pika_h3_flex_fl2va.json.
These are **ComfyUI UI-format workflow JSONs**, adapted ONLY to the already-downloaded BF16 base model, FP16 video VAE, and Pika-Labs BF16 LoRA; original official node graph and connections are retained.
The Pika LoRA connects directly to the guider (no disabled toggle). FL2VA accepts first and last frame.
The repository's automated API workflow runner may require an API-format export from a running ComfyUI instance: UI-format JSON must NOT be submitted directly to /prompt.
Actual GPU inference with BF16 and the Pika-Labs LoRA has not yet been tested.
