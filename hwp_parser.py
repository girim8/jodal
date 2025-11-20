"""HWP/HWPX 파일에서 텍스트를 뽑아내는 도우미 함수 모음."""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from xml.etree import ElementTree

import olefile


def _maybe_decompress(data: bytes) -> bytes:
    """BodyText 스트림에서 자주 쓰이는 압축 방식을 순차적으로 해제한다."""
    # RAW zlib → 표준 zlib → 무압축 순서로 시도한다.
    for mode in (-zlib.MAX_WBITS, zlib.MAX_WBITS, None):
        try:
            if mode is None:
                return data
            return zlib.decompress(data, mode)
        except zlib.error:
            continue
    return data


def _clean_text(text: str) -> str:
    """인쇄 가능한 문자만 남기고 줄바꿈을 정리한다."""
    filtered = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    lines = [line.strip() for line in filtered.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _parse_body_records(data: bytes) -> str:
    """BodyText 레코드를 순회하면서 사람이 읽을 수 있는 텍스트만 모은다."""
    text_chunks: list[str] = []
    offset = 0
    length = len(data)

    while offset + 4 <= length:
        header = struct.unpack("<I", data[offset : offset + 4])[0]
        tag_id = header & 0x3FF
        size = (header >> 20) & 0xFFF
        offset += 4

        if offset + size > length:
            break

        payload = data[offset : offset + size]
        offset += size

        if tag_id in (66, 67, 68, 80):
            # 본문에 해당하는 레코드만 압축 해제 후 디코딩한다.
            payload = _maybe_decompress(payload)
            try:
                decoded = payload.decode("utf-16le", errors="ignore")
            except UnicodeDecodeError:
                continue

            cleaned = _clean_text(decoded)
            if cleaned:
                text_chunks.append(cleaned)

    return "\n".join(text_chunks)


def extract_text_from_hwp(data: bytes) -> str:
    """OLE 스트림을 직접 읽어 바이너리 HWP 문서에서 텍스트를 뽑는다."""
    text_parts: list[str] = []

    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        for entry in ole.listdir():
            if not entry or entry[0] != "BodyText":
                continue

            stream_name = "/".join(entry)
            try:
                raw_stream = ole.openstream(entry).read()
            except OSError:
                continue

            # BodyText 스트림은 또 한 번 압축돼 있을 수 있다.
            parsed = _parse_body_records(_maybe_decompress(raw_stream))
            if parsed:
                text_parts.append(f"[{stream_name}]\n{parsed}")

    return _clean_text("\n\n".join(text_parts))


def extract_text_from_hwpx(data: bytes) -> str:
    """ZIP 안의 XML 파일들을 합쳐 HWPX 문서의 텍스트를 만든다."""
    text_chunks: list[str] = []

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml"):
                continue

            try:
                xml_data = archive.read(name)
            except KeyError:
                continue

            try:
                root = ElementTree.fromstring(xml_data)
            except ElementTree.ParseError:
                continue

            # itertext()로 태그 사이의 문자만 빠르게 추출한다.
            text_chunks.append("".join(root.itertext()))

    return _clean_text("\n".join(text_chunks))


def convert_to_text(data: bytes, filename: str | None = None) -> tuple[str, str]:
    """파일 포맷을 판별하고 텍스트와 판별 결과를 함께 돌려준다."""
    name = (filename or "").lower()
    is_hwpx = name.endswith("hwpx") or data[:2] == b"PK"

    if is_hwpx:
        text = extract_text_from_hwpx(data)
        fmt = "HWPX"
    else:
        text = extract_text_from_hwp(data)
        fmt = "HWP"

    if not text:
        raise ValueError("Unable to extract text from the provided file.")

    return text, fmt


__all__ = [
    "convert_to_text",
    "extract_text_from_hwp",
    "extract_text_from_hwpx",
]
