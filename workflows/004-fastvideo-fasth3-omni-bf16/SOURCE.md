# FastH3 Omni 8-Step V1 BF16 Ref2VA

Official model: https://huggingface.co/FastVideo/FastH3-Omni-Comfy

Adapted from 002 MiniMax H3 Ref2VA. Native MiniMaxH3ReferenceToVideo, encoder, audio and video VAEs retained. Eight sampler steps. Original Turbo LoRA removed.

Official recommended guidance 1.0, video shift 12, audio shift 3. The imported original graph does not expose explicit video/audio shift settings; verify with native ComfyUI runtime. Float 5 is duration, not shift.

Not yet inference-tested.
