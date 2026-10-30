"""Run a storyboard from story.json using the existing ComfyUI batch client."""
import json
from pathlib import Path

from batch_run import generate

ROOT = Path(__file__).resolve().parent


def main():
    story = json.loads((ROOT / "story.json").read_text(encoding="utf-8-sig"))
    shots = story["shots"]
    if not isinstance(shots, list) or not shots:
        raise ValueError("story.json must contain a nonempty shots list")
    prompts = [shot["prompt"] for shot in shots]
    # Separate output directory per story. Clear it or choose a new name if
    # changing prompts: DONE markers intentionally prevent regenerating shots.
    generate(
        url="https://zhiyuqqq--comfyui-1.us-east.modal.run",
        workflow=ROOT / "workflows/comfyui-qwen-image-2-1/image_qwen_image_2_1_t2i.json",
        prompts=prompts,
        output=ROOT / "results" / "story_demo",
    )


if __name__ == "__main__":
    main()
