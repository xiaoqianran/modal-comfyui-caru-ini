"""Download a short-lived Worker URL to PC2, verify SHA-256, optionally extract ZIP."""
import argparse
import hashlib
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MAX_EXTRACT_BYTES = 250 * 1024 * 1024
MAX_ZIP_MEMBERS = 4096
WINDOWS_DEVICES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)
}


def extract_zip_safely(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        if (len(members) > MAX_ZIP_MEMBERS or
                sum(info.file_size for info in members) > MAX_EXTRACT_BYTES):
            raise ValueError("ZIP exceeds extraction limits")
        # Validate every member before writing any archive contents.
        for info in members:
            path = PurePosixPath(info.filename)
            if (path.is_absolute() or not path.parts or
                    any(part in {"..", ""} for part in path.parts) or
                    ":" in info.filename or "\\" in info.filename or
                    any(part.endswith((".", " ")) or
                        part.split(".")[0].upper() in WINDOWS_DEVICES
                        for part in path.parts)):
                raise ValueError("Unsafe ZIP member path")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("ZIP symlink not allowed")
            target = destination.joinpath(*path.parts)
            if not target.resolve().is_relative_to(root):
                raise ValueError("ZIP target escapes destination")
        for info in members:
            target = destination.joinpath(*PurePosixPath(info.filename).parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Temporary Worker GET URL")
    parser.add_argument("--sha256", required=True, help="Expected SHA-256 from upload_file")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--extract-to", type=Path)
    args = parser.parse_args()
    if len(args.sha256) != 64 or any(c not in "0123456789abcdef" for c in args.sha256.lower()):
        parser.error("--sha256 must be a 64-character hexadecimal string")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    tmp = tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False)
    temp = Path(tmp.name)
    tmp.close()
    try:
        request = urllib.request.Request(args.url, headers={
            "User-Agent": "chatgpt-file-transfer-receiver/0.5",
        })
        with temp.open("wb") as output:
            with urllib.request.urlopen(request, timeout=60) as response:
                total = 0
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("Download exceeds 25 MiB limit")
                    digest.update(block)
                    output.write(block)
        actual = digest.hexdigest()
        if actual.lower() != args.sha256.lower():
            raise ValueError(f"SHA-256 mismatch: {actual}")
        temp.replace(args.output)
    finally:
        temp.unlink(missing_ok=True)

    if args.extract_to:
        extract_zip_safely(args.output, args.extract_to)
    print(f"Verified {args.output} SHA-256={digest.hexdigest()}")


if __name__ == "__main__":
    main()
