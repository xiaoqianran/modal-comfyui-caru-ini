"""Output formats shared by archiving, asset access and Volume snapshots."""

VIDEOS = {".mp4", ".webm", ".mov", ".mkv"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
AUDIO = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}
MODELS = {".glb", ".gltf", ".ply", ".splat", ".ksplat", ".spz",
          ".obj", ".mtl", ".stl"}
EXTENSIONS = VIDEOS | IMAGES | AUDIO | MODELS | {".json", ".bin"}
CATEGORIES = ("3d", "3dgs", "video", "image", "audio")

def is_output_file(path, root, extensions):
    """Exclude warmup and incomplete preview/transfer files from publication."""
    relative = path.relative_to(root)
    return (path.suffix.lower() in extensions
            and "__snapshot_warmup__" not in relative.parts
            and not any(part.endswith(".part") or ".part." in part for part in relative.parts))
