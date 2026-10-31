"""YuE2 instrumental ABC voice transfer, based on the official YuE2 instrumental skill."""
from .instrumentalize import convert_score


class YuE2InstrumentalizeABC:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"abc": ("STRING", {"forceInput": True})}}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("instrumental_abc", "verification")
    FUNCTION = "convert"
    CATEGORY = "audio/YuE2"

    def convert(self, abc):
        converted, report = convert_score(abc)
        if report["vocal_notes_after"] or not report["output_ins_notes"]:
            raise ValueError("YuE2 instrumental conversion did not remove all Vocal notes")
        import json
        return (converted, json.dumps(report, ensure_ascii=False, indent=2))


NODE_CLASS_MAPPINGS = {"YuE2InstrumentalizeABC": YuE2InstrumentalizeABC}
NODE_DISPLAY_NAME_MAPPINGS = {"YuE2InstrumentalizeABC": "YuE2 Instrumental: Vocal → Ins"}
