"""Download a short-lived Worker URL to PC2, verify SHA-256, optionally extract ZIP."""
import argparse
import hashlib
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath


def extract_zip_safely(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            path = PurePosixPath(info.filename)
            if (path.is_absolute() or not path.parts or
                    any(part in {"..", ""} for part in path.parts) or
                    ":" in info.filename or "\\" in info.filename):
                raise ValueError("Unsafe ZIP member path")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("ZIP symlink not allowed")
            target = destination.joinpath(*path.parts)
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
    with tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False) as tmp:
        temp = Path(tmp.name)
        try:
            with urllib.request.urlopen(args.url, timeout=60) as response:
                while block := response.read(1024 * 1024):
                    digest.update(block)
                    tmp.write(block)
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
