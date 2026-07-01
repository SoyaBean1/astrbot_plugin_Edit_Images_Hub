"""
Yaya-decode — LSB steganography decoder (decode-only, trimmed from
https://github.com/T8mars/yaya-decode).

Only the decoding functions are kept; encoding is removed.
"""

from __future__ import annotations

import io
import os
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

WATERMARK_SKIP_W_RATIO = 0.40
WATERMARK_SKIP_H_RATIO = 0.08


@dataclass(frozen=True)
class DuckResult:
    path: Path
    filename: str
    size: int
    ext: str


def ensure_output_dir(output_dir: str | os.PathLike[str] | None) -> Path:
    target = Path(str(output_dir or ".")).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target.resolve()


def unique_path(directory: Path, stem: str, suffix: str) -> Path:
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    candidate = directory / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return candidate


# ── Main decode entry point ──────────────────────────────────


def decode_duck_to_file(
    duck_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str] | None,
    password: str = "",
    output_stem: str = "duck_recovered",
) -> DuckResult:
    source = Path(duck_path)
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Duck image not found: {source}")

    try:
        image = Image.open(source).convert("RGB")
    except Exception as exc:
        raise ValueError("Invalid duck image.") from exc

    arr = np.array(image).astype(np.uint8)
    last_error: Exception | None = None
    raw: bytes | None = None
    ext = ""
    for k in (2, 6, 8):
        try:
            header = extract_payload_with_k(arr, k)
            raw, ext = parse_header(header, password or "")
            break
        except Exception as exc:
            last_error = exc
    if raw is None:
        raise last_error or RuntimeError("Decode failed.")

    out_dir = ensure_output_dir(output_dir)
    payload, final_ext = normalize_decoded_payload(raw, ext)
    final_ext = final_ext or "bin"
    out_path = unique_path(out_dir, output_stem, final_ext)
    out_path.write_bytes(payload)
    return DuckResult(
        path=out_path.resolve(),
        filename=out_path.name,
        size=out_path.stat().st_size,
        ext=final_ext,
    )


# ── LSB extraction ───────────────────────────────────────────


def extract_payload_with_k(arr: np.ndarray, k: int) -> bytes:
    h, w, c = arr.shape
    skip_w = int(w * WATERMARK_SKIP_W_RATIO)
    skip_h = int(h * WATERMARK_SKIP_H_RATIO)
    mask2d = np.ones((h, w), dtype=bool)
    if skip_w > 0 and skip_h > 0:
        mask2d[:skip_h, :skip_w] = False
    mask3d = np.repeat(mask2d[:, :, None], c, axis=2)
    flat = arr.reshape(-1)
    idxs = np.flatnonzero(mask3d.reshape(-1))
    vals = (flat[idxs] & ((1 << k) - 1)).astype(np.uint8)
    unpacked = np.unpackbits(vals, bitorder="big").reshape(-1, 8)[:, -k:]
    bits = unpacked.reshape(-1)
    if len(bits) < 32:
        raise ValueError("Insufficient image data.")
    length_bytes = np.packbits(bits[:32], bitorder="big").tobytes()
    header_len = struct.unpack(">I", length_bytes)[0]
    total_bits = 32 + header_len * 8
    if header_len <= 0 or total_bits > len(bits):
        raise ValueError("Payload length invalid.")
    payload_bits = bits[32: 32 + header_len * 8]
    return np.packbits(payload_bits, bitorder="big").tobytes()


# ── Header parsing & password decrypt ────────────────────────


def generate_key_stream(password: str, salt: bytes, length: int) -> bytes:
    import hashlib

    key_material = (password + salt.hex()).encode("utf-8")
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(
            hashlib.sha256(key_material + str(counter).encode("utf-8")).digest()
        )
        counter += 1
    return bytes(out[:length])


def parse_header(header: bytes, password: str) -> tuple[bytes, str]:
    idx = 0
    if len(header) < 1:
        raise ValueError("Header corrupted.")
    has_password = header[0] == 1
    idx += 1

    password_hash = b""
    salt = b""
    if has_password:
        if len(header) < idx + 32 + 16:
            raise ValueError("Header corrupted.")
        password_hash = header[idx: idx + 32]
        idx += 32
        salt = header[idx: idx + 16]
        idx += 16

    if len(header) < idx + 1:
        raise ValueError("Header corrupted.")
    ext_len = header[idx]
    idx += 1
    if len(header) < idx + ext_len + 4:
        raise ValueError("Header corrupted.")
    ext = header[idx: idx + ext_len].decode("utf-8", errors="ignore")
    idx += ext_len
    data_len = struct.unpack(">I", header[idx: idx + 4])[0]
    idx += 4
    data = header[idx:]
    if len(data) != data_len:
        raise ValueError("Data length mismatch.")
    if not has_password:
        return data, ext
    if not password:
        raise ValueError("Password required.")

    import hashlib

    check_hash = hashlib.sha256((password + salt.hex()).encode("utf-8")).digest()
    if check_hash != password_hash:
        raise ValueError("Wrong password.")
    key_stream = generate_key_stream(password, salt, len(data))
    plain = bytes(a ^ b for a, b in zip(data, key_stream))
    return plain, ext


# ── Post-processing (.binpng → mp4 etc.) ─────────────────────


def normalize_decoded_payload(raw: bytes, ext: str) -> tuple[bytes, str]:
    normalized = ext.lower().lstrip(".") or "bin"
    if normalized.endswith(".binpng"):
        original_ext = normalized[: -len(".binpng")].lstrip(".") or "mp4"
        return binpng_bytes_to_bytes(raw), original_ext
    return raw, normalized


def binpng_bytes_to_bytes(raw_png: bytes) -> bytes:
    image = Image.open(io.BytesIO(raw_png)).convert("RGB")
    arr = np.array(image).astype(np.uint8)
    return arr.reshape(-1, 3).reshape(-1).tobytes().rstrip(b"\x00")
