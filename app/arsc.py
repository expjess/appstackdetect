"""Minimal resources.arsc reader.

Android manifests often store a value such as the expo-updates URL as a
reference (@0x7f1300d4) into the resource table instead of as a literal string.
This module resolves such a reference back to its string, so the report can show
the real value.

Only string-valued, non-complex entries are handled. Anything else returns None.
"""

from __future__ import annotations

import struct

from .axml import _read_string_pool  # same string pool layout

RES_STRING_POOL = 0x0001
RES_TABLE = 0x0002
RES_TABLE_PACKAGE = 0x0200
RES_TABLE_TYPE = 0x0201
RES_TABLE_TYPE_SPEC = 0x0202

FLAG_SPARSE = 0x01
FLAG_OFFSET16 = 0x02
ENTRY_COMPLEX = 0x0001
TYPE_STRING = 0x03


class ResourceTable:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.global_strings: list[str] = []
        # package id -> list of (type_id, entry_id) -> value string
        self._values: dict[int, str] = {}
        self._parsed = False

    def _parse(self) -> None:
        if self._parsed:
            return
        self._parsed = True
        data = self.data
        if len(data) < 12 or struct.unpack_from("<H", data, 0)[0] != RES_TABLE:
            return
        header_size = struct.unpack_from("<H", data, 2)[0]
        off = header_size
        while off + 8 <= len(data):
            chunk_type, chunk_header, chunk_size = struct.unpack_from("<HHI", data, off)
            if chunk_size <= 0 or off + chunk_size > len(data):
                break
            if chunk_type == RES_STRING_POOL and not self.global_strings:
                try:
                    self.global_strings = _read_string_pool(data, off)
                except Exception:  # noqa: BLE001 - a damaged pool must not break the scan
                    self.global_strings = []
            elif chunk_type == RES_TABLE_PACKAGE:
                self._parse_package(off, chunk_header, chunk_size)
            off += chunk_size

    def _parse_package(self, pkg_off: int, header_size: int, chunk_size: int) -> None:
        data = self.data
        package_id = struct.unpack_from("<I", data, pkg_off + 8)[0]
        off = pkg_off + header_size
        end = pkg_off + chunk_size
        best_specificity: dict[tuple[int, int], int] = {}

        while off + 8 <= end:
            chunk_type, chunk_header, size = struct.unpack_from("<HHI", data, off)
            if size <= 0 or off + size > end:
                break
            if chunk_type == RES_TABLE_TYPE:
                self._parse_type(off, chunk_header, size, package_id, best_specificity)
            off += size

    def _parse_type(
        self,
        off: int,
        header_size: int,
        size: int,
        package_id: int,
        best_specificity: dict[tuple[int, int], int],
    ) -> None:
        data = self.data
        type_id = data[off + 8]
        flags = data[off + 9]
        entry_count, entries_start = struct.unpack_from("<II", data, off + 12)
        config_off = off + 20
        config_size = struct.unpack_from("<I", data, config_off)[0] if config_off + 4 <= len(data) else 0
        config = data[config_off : config_off + min(config_size, 64)]
        specificity = sum(1 for b in config[4:] if b)  # 0 for the default configuration

        table_off = off + header_size
        entries_base = off + entries_start
        offsets: list[tuple[int, int]] = []  # (entry index, offset)

        if flags & FLAG_SPARSE:
            for i in range(entry_count):
                p = table_off + i * 4
                if p + 4 > len(data):
                    break
                idx, rel = struct.unpack_from("<HH", data, p)
                offsets.append((idx, rel * 4))
        elif flags & FLAG_OFFSET16:
            for i in range(entry_count):
                p = table_off + i * 2
                if p + 2 > len(data):
                    break
                rel = struct.unpack_from("<H", data, p)[0]
                if rel != 0xFFFF:
                    offsets.append((i, rel * 4))
        else:
            for i in range(entry_count):
                p = table_off + i * 4
                if p + 4 > len(data):
                    break
                rel = struct.unpack_from("<I", data, p)[0]
                if rel != 0xFFFFFFFF:
                    offsets.append((i, rel))

        for index, rel in offsets:
            entry_off = entries_base + rel
            if entry_off + 8 > len(data):
                continue
            entry_size, entry_flags = struct.unpack_from("<HH", data, entry_off)
            if entry_flags & ENTRY_COMPLEX:
                continue
            value_off = entry_off + max(entry_size, 8)
            if value_off + 8 > len(data):
                continue
            _vsize, _res0, data_type, value = struct.unpack_from("<HBBI", data, value_off)
            if data_type != TYPE_STRING or value >= len(self.global_strings):
                continue
            key = (type_id, index)
            if key in best_specificity and best_specificity[key] <= specificity:
                continue
            best_specificity[key] = specificity
            res_id = (package_id << 24) | (type_id << 16) | index
            self._values[res_id] = self.global_strings[value]

    def resolve(self, res_id: int) -> str | None:
        self._parse()
        return self._values.get(res_id)


def resolve_reference(arsc_bytes: bytes | None, value: str) -> str | None:
    """Turn '@0x7f1300d4' into its string, when resources.arsc holds one."""
    if not arsc_bytes or not value.startswith("@0x"):
        return None
    try:
        res_id = int(value[1:], 16)
    except ValueError:
        return None
    try:
        return ResourceTable(arsc_bytes).resolve(res_id)
    except Exception:  # noqa: BLE001 - resolution is best effort
        return None
