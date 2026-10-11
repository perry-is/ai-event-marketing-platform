"""Private, bounded source-file validation and storage helpers."""

import os
import re
import tempfile
import warnings
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_EVENT_BYTES = 100 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
MAX_PDF_PAGES = 50


def safe_display_name(filename: str | None) -> str:
    original = (filename or "").replace("\\", "/")
    name = PurePosixPath(original).name
    name = "".join(char for char in name if char.isprintable() and char not in '<>:"|?*')
    name = name.strip().rstrip(".")[:240]
    if not name or name in {".", ".."}:
        raise HTTPException(422, "INVALID_FILENAME")
    return name


def _check_declared_type(upload: UploadFile, expected: set[str]) -> None:
    declared = (upload.content_type or "").lower().split(";", 1)[0].strip()
    if declared and declared not in expected and declared != "application/octet-stream":
        raise HTTPException(415, "FILE_TYPE_NOT_ALLOWED")


def read_and_validate_upload(upload: UploadFile) -> tuple[bytes, str, str]:
    name = safe_display_name(upload.filename)
    suffix = Path(name).suffix.lower()
    limits = {
        ".txt": MAX_TEXT_BYTES,
        ".md": MAX_TEXT_BYTES,
        ".pdf": MAX_PDF_BYTES,
        ".jpg": MAX_IMAGE_BYTES,
        ".jpeg": MAX_IMAGE_BYTES,
        ".png": MAX_IMAGE_BYTES,
        ".webp": MAX_IMAGE_BYTES,
    }
    if suffix not in limits:
        raise HTTPException(415, "FILE_TYPE_NOT_ALLOWED")
    limit = limits[suffix]
    content_length = upload.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > limit:
        raise HTTPException(413, "FILE_TOO_LARGE")
    data = upload.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, "FILE_TOO_LARGE")
    if not data:
        raise HTTPException(422, "INVALID_FILE")

    if suffix in {".txt", ".md"}:
        expected = {"text/plain", "text/markdown", "text/x-markdown"}
        _check_declared_type(upload, expected)
        if b"\x00" in data:
            raise HTTPException(415, "FILE_TYPE_NOT_ALLOWED")
        try:
            data.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError:
            raise HTTPException(422, "INVALID_FILE") from None
        return data, name, "text/markdown" if suffix == ".md" else "text/plain"

    if suffix == ".pdf":
        _check_declared_type(upload, {"application/pdf"})
        if not data.startswith(b"%PDF-"):
            raise HTTPException(422, "INVALID_FILE")
        try:
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted or len(reader.pages) > MAX_PDF_PAGES:
                raise HTTPException(422, "INVALID_FILE")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422, "INVALID_FILE") from None
        return data, name, "application/pdf"

    image_types = {
        ".jpg": ("image/jpeg", "JPEG"),
        ".jpeg": ("image/jpeg", "JPEG"),
        ".png": ("image/png", "PNG"),
        ".webp": ("image/webp", "WEBP"),
    }
    detected, image_format = image_types[suffix]
    _check_declared_type(upload, {detected})
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(data), formats=(image_format,))
            if image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) > 1:
                raise HTTPException(422, "INVALID_FILE")
            image.verify()
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, UnidentifiedImageError):
        raise HTTPException(422, "INVALID_FILE") from None
    except Exception:
        raise HTTPException(422, "INVALID_FILE") from None
    return data, name, detected


def storage_key() -> str:
    value = uuid4().hex
    return f"{value[:2]}/{value}"


def resolve_storage_path(storage_dir: Path, key: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{2}/[a-f0-9]{32}", key):
        raise HTTPException(404, "SOURCE_NOT_FOUND")
    base = storage_dir.resolve()
    path = (base / key).resolve()
    if base not in path.parents:
        raise HTTPException(404, "SOURCE_NOT_FOUND")
    return path


def write_private(storage_dir: Path, key: str, data: bytes) -> Path:
    path = resolve_storage_path(storage_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".incoming-", delete=False
        ) as file:
            temporary = Path(file.name)
            os.chmod(temporary, 0o600)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink(missing_ok=True)
    return path
