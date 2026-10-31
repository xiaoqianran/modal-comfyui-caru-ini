# Pika H3 Flex Ref2VA — official ComfyUI workflow, BF16 weights
Source: https://github.com/Comfy-Org/workflow_templates/blob/main/templates/video_pika_h3_flex_ref2va.json
This is the **official UI-format node graph** with three model references changed to existing BF16 / FP16 / Pika-Labs weights.
Original ComfyUI graph structure, LCM sampler, ManualSigmas and LoRA-to-guider connection are preserved.
Only models required by the official Ref2VA graph remain in models.json; unused ControlNet and SDPose models were removed from this variant's manifest.
UI-format JSON is not directly usable with /prompt without API conversion. Real GPU rendering with the BF16 checkpoint is not yet verified.
