# Pika H3 Flex Ref2VA BF16
Based on `002-minimax-h3-ref2va-bf16`; original workflow untouched.
Three API workflows: R2V, multiframe reference, and Fun ControlNet Union.
Reuse the MiniMax-H3 Ref2VA base checkpoint; switch to Pika's official BF16 Ref2VA LoRA at strength 1.0.
The LoRA enable switch is ON, LCM is selected, and ManualSigmas has seven values (six sampling intervals).
**IMPORTANT:** These sigmas are an approximate ComfyUI configuration, not a verified implementation of the Pika sampling contract (knots 1000/750/500/250/100/40/0, video shift 12, audio shift 3, and fresh intermediate noise).
ComfyUI end-to-end generation and compatibility of the ControlNet extension are NOT yet verified.
Run `WORKFLOW=002-minimax-h3-ref2va-bf16-pika`; download models before Modal deploy.
