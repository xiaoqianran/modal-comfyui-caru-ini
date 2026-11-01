"""Output formats shared by archiving, asset access and Volume snapshots."""

VIDEOS = {".mp4", ".webm", ".mov", ".mkv"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
AUDIO = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"}
MODELS = {".glb", ".gltf", ".ply", ".splat", ".ksplat", ".spz",
          ".obj", ".mtl", ".stl"}
EXTENSIONS = VIDEOS | IMAGES | AUDIO | MODELS | {".json", ".bin"}
CATEGORIES = ("3d", "3dgs", "video", "image", "audio")
