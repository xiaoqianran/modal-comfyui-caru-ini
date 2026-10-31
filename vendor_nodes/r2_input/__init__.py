"""Import signed ChatGPT R2 PNG/JPEG/WebP images directly into ComfyUI."""

import hashlib
import hmac
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web
from PIL import Image, UnidentifiedImageError

import folder_paths
from server import PromptServer


_HOST = "chatgpt-file-transfer-mcp.wangran.workers.dev"
_PATH = re.compile(r"^/download/chatgpt/[0-9a-f-]{36}/[A-Za-z0-9._-]{1,100}$")
_MAX_BYTES = 25 * 1024 * 1024
_MAX_PIXELS = 36_000_000
_FORMATS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}


@PromptServer.instance.routes.post("/inputs/from_url")
async def import_r2_image(request):
    key = os.environ.get("R2_IMPORT_KEY", "")
    supplied = request.headers.get("X-Import-Key", "")
    if not key or not hmac.compare_digest(key, supplied):
        raise web.HTTPUnauthorized(text="Invalid import key")

    try:
        payload = await request.json()
        url = payload["url"]
        if not isinstance(url, str):
            raise ValueError("url must be a string")
        parsed = urlsplit(url)
    except (ValueError, TypeError, KeyError):
        raise web.HTTPBadRequest(text="Expected JSON with a valid url")

    try:
        port = parsed.port
    except ValueError:
        raise web.HTTPBadRequest(text="Invalid URL port")
    if (
        parsed.scheme != "https"
        or parsed.hostname != _HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or not _PATH.fullmatch(parsed.path)
        or not parsed.query
        or parsed.fragment
    ):
        raise web.HTTPBadRequest(text="Only signed ChatGPT R2 download URLs are allowed")

    input_dir = Path(folder_paths.get_input_directory())
    input_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=input_dir, suffix=".part", delete=False) as tmp:
            temp_path = Path(tmp.name)
            total = 0
            timeout = ClientTimeout(total=90, connect=15)
            async with ClientSession(timeout=timeout, trust_env=False) as session:
                async with session.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        raise web.HTTPBadRequest(text=f"R2 returned HTTP {response.status}")
                    async for chunk in response.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > _MAX_BYTES:
                            raise web.HTTPRequestEntityTooLarge(
                                max_size=_MAX_BYTES, actual_size=total
                            )
                        tmp.write(chunk)
                        digest.update(chunk)
        try:
            with Image.open(temp_path) as img:
                if img.format not in _FORMATS or img.width * img.height > _MAX_PIXELS:
                    raise web.HTTPBadRequest(text="Unsupported or oversized image")
                fmt = img.format
                img.verify()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise web.HTTPBadRequest(text="Invalid image content")

        name = f"r2_{digest.hexdigest()}{_FORMATS[fmt]}"
        destination = input_dir / name
        if not destination.is_file():
            os.replace(temp_path, destination)
            temp_path = None
        return web.json_response({"name": name, "subfolder": "", "type": "input"})
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


NODE_CLASS_MAPPINGS = {}
