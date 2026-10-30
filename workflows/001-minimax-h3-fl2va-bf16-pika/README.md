# Pika H3 Flex FL2VA (BF16)

Derived from 001-minimax-h3-fl2va-bf16. Uses the same base model, replacing the Turbo 8-step LoRA with the official Pika FL2VA LoRA (strength 1.0). Sampling: LCM + ManualSigmas 1.0, 0.972973, 0.923077, 0.8, 0.571429, 0.333333, 0.0.

The original workflow remains unchanged. The six-step setup is based on official ComfyUI guidance; fresh intermediate noise from the original Pika inference contract is not yet verified in this ComfyUI workflow. Validate an actual render before treating it as equivalent to the official inference pipeline.

Run with WORKFLOW=001-minimax-h3-fl2va-bf16-pika.
