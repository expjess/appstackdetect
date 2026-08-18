"""Minimal reader for Android binary XML (AXML), e.g. AndroidManifest.xml inside an APK.

Only what this tool needs: element names, attribute names and values.
Format reference: Android ResourceTypes.h chunk layout.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

RES_STRING_POOL = 0x0001
RES_XML_START_ELEMENT = 0x0102
RES_XML_END_ELEMENT = 0x0103
RES_XML_RESOURCE_MAP = 0x0180

TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_FLOAT = 0x04
TYPE_INT_DEC = 0x10
TYPE_INT_HEX = 0x11
TYPE_INT_BOOLEAN = 0x12

UTF8_FLAG = 1 << 8

# Fallback for manifests whose attribute names are empty strings in the pool.
# Keys are android framework attribute resource ids.
ATTR_RES_IDS = {
    0x01010001: "label",
    0x01010003: "name",
    0x01010024: "value",
    0x01010025: "resource",
    0x0101021B: "versionCode",
    0x0101021C: "versionName",
    0x0101020C: "minSdkVersion",
    0x01010270: "targetSdkVersion",
}


@dataclass
class Element:
    name: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Element"] = field(default_factory=list)


class AxmlError(Exception):
    pass


def _read_string_pool(data: bytes, off: int) -> list[str]:
    chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", data, off)
    if chunk_type != RES_STRING_POOL:
        raise AxmlError(f"expected string pool at {off}, got type {chunk_type:#x}")
    count, style_count, flags, strings_start, _styles_start = struct.unpack_from("<IIIII", data, off + 8)
    utf8 = bool(flags & UTF8_FLAG)
    offsets = struct.unpack_from(f"<{count}I", data, off + header_size)
    base = off + strings_start
    out: list[str] = []
    for rel in offsets:
        p = base + rel
        if p >= off + chunk_size:
            out.append("")
            continue
        if utf8:
            n_chars, p = _decode_len8(data, p)
            n_bytes, p = _decode_len8(data, p)
            out.append(data[p : p + n_bytes].decode("utf-8", "replace"))
        else:
            n_chars, p = _decode_len16(data, p)
            out.append(data[p : p + n_chars * 2].decode("utf-16-le", "replace"))
    return out


def _decode_len8(data: bytes, p: int) -> tuple[int, int]:
    v = data[p]
    if v & 0x80:
        return ((v & 0x7F) << 8) | data[p + 1], p + 2
    return v, p + 1


def _decode_len16(data: bytes, p: int) -> tuple[int, int]:
    v = struct.unpack_from("<H", data, p)[0]
    if v & 0x8000:
        hi = v & 0x7FFF
        lo = struct.unpack_from("<H", data, p + 2)[0]
        return (hi << 16) | lo, p + 4
    return v, p + 2


def _fmt_value(strings: list[str], raw_index: int, data_type: int, value: int) -> str:
    if data_type == TYPE_STRING:
        return strings[raw_index] if 0 <= raw_index < len(strings) else ""
    if data_type == TYPE_INT_BOOLEAN:
        return "true" if value else "false"
    if data_type == TYPE_REFERENCE:
        return f"@{value:#010x}"
    if data_type == TYPE_INT_HEX:
        return f"{value:#x}"
    if data_type == TYPE_FLOAT:
        return str(struct.unpack("<f", struct.pack("<I", value))[0])
    if value >= 0x80000000:
        value -= 0x100000000
    return str(value)


def parse(data: bytes) -> Element:
    """Parse AXML bytes into an element tree. Raises AxmlError on malformed input."""
    if len(data) < 8:
        raise AxmlError("too short")
    off = 8  # skip the outer RES_XML header
    strings: list[str] = []
    res_map: list[int] = []
    root = Element("__root__")
    stack: list[Element] = [root]

    while off + 8 <= len(data):
        chunk_type, header_size, chunk_size = struct.unpack_from("<HHI", data, off)
        if chunk_size <= 0 or off + chunk_size > len(data):
            break
        if chunk_type == RES_STRING_POOL:
            strings = _read_string_pool(data, off)
        elif chunk_type == RES_XML_RESOURCE_MAP:
            n = (chunk_size - header_size) // 4
            res_map = list(struct.unpack_from(f"<{n}I", data, off + header_size))
        elif chunk_type == RES_XML_START_ELEMENT:
            p = off + header_size
            _ns, name_idx = struct.unpack_from("<II", data, p)
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", data, p + 8)
            el = Element(strings[name_idx] if name_idx < len(strings) else "")
            ap = p + attr_start
            for _ in range(attr_count):
                _ans, a_name_idx, a_raw = struct.unpack_from("<III", data, ap)
                _size, _res0, a_type, a_val = struct.unpack_from("<HBBI", data, ap + 12)
                key = strings[a_name_idx] if a_name_idx < len(strings) else ""
                if not key and a_name_idx < len(res_map):
                    key = ATTR_RES_IDS.get(res_map[a_name_idx], f"attr_{res_map[a_name_idx]:#x}")
                el.attrs[key] = _fmt_value(strings, a_raw, a_type, a_val)
                ap += attr_size
            stack[-1].children.append(el)
            stack.append(el)
        elif chunk_type == RES_XML_END_ELEMENT:
            if len(stack) > 1:
                stack.pop()
        off += chunk_size

    if not root.children:
        raise AxmlError("no elements found")
    return root.children[0]


def iter_elements(el: Element):
    yield el
    for child in el.children:
        yield from iter_elements(child)


def meta_data(manifest: Element) -> dict[str, str]:
    """Collect every <meta-data android:name=... android:value=.../> in the manifest."""
    out: dict[str, str] = {}
    for el in iter_elements(manifest):
        if el.name == "meta-data":
            key = el.attrs.get("name")
            if key:
                out[key] = el.attrs.get("value", el.attrs.get("resource", ""))
    return out
