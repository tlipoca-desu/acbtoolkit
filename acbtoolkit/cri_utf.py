"""
CRI @UTF table parser.

@UTF is CRIWARE's generic serialisation format, used by .acb (cue sheets),
.awb, .acf, .cpk and others.  It is a row/column database:

    offset  size  field
    0x00    4     "@UTF"
    0x04    u32be table_size - 8      (so table end = base + 8 + value)
    0x08    u16be version             (0 or 1)
    0x0a    u16be rows_offset   - 8
    0x0c    u32be strings_offset - 8
    0x10    u32be data_offset    - 8
    0x14    u32be name_offset          (table name, within string section)
    0x18    u16be column_count
    0x1a    u16be row_width            (bytes per row)
    0x1c    u32be row_count

Four sections follow, in order: schema, rows, strings, data.

The schema describes one column per entry::

    u8    info        high nibble = flags, low nibble = type
    u32be name_offset (within string section)
    ...   constant value, only when the DEFAULT flag is set

Flags: 0x10 NAME, 0x20 DEFAULT, 0x40 ROW.  A column is either DEFAULT
(one shared constant, stored inline in the schema) or ROW (a value per
row, stored in the row section) -- NAME is normally set on both.

Types: 0x00 u8, 0x01 s8, 0x02 u16, 0x03 s16, 0x04 u32, 0x05 s32,
0x06 u64, 0x07 s64, 0x08 f32, 0x09 f64, 0x0a string (offset into string
section), 0x0b vldata (u32 offset into data section + u32 size),
0x0c u128.

VLDATA columns often hold a nested @UTF table; :meth:`UtfTable.subtable`
resolves those.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from typing import Any

MAGIC = b"@UTF"

# Bytes occupied by each column type inside a row (or schema constant).
_TYPE_SIZE = {
    0x00: 1, 0x01: 1,
    0x02: 2, 0x03: 2,
    0x04: 4, 0x05: 4, 0x08: 4, 0x0A: 4,
    0x06: 8, 0x07: 8, 0x09: 8, 0x0B: 8,
    0x0C: 16,
}

TYPE_NAMES = {
    0x00: "u8", 0x01: "s8", 0x02: "u16", 0x03: "s16",
    0x04: "u32", 0x05: "s32", 0x06: "u64", 0x07: "s64",
    0x08: "f32", 0x09: "f64", 0x0A: "string", 0x0B: "vldata", 0x0C: "u128",
}

FLAG_NAME = 0x10
FLAG_DEFAULT = 0x20
FLAG_ROW = 0x40
FLAG_UNDEFINED = 0x80

# A malformed file should not be able to make us allocate unbounded memory.
MAX_COLUMNS = 256
MAX_ROWS = 1 << 20
MAX_TABLE_SIZE = 1 << 28


class UtfError(ValueError):
    """Raised when a buffer does not contain a usable @UTF table."""


class Column:
    __slots__ = ("name", "type", "flags", "_offset")

    def __init__(self, name: str | None, type_: int, flags: int, offset: int):
        self.name = name
        self.type = type_
        self.flags = flags
        self._offset = offset      # relative to schema (DEFAULT) or row (ROW)

    @property
    def is_default(self) -> bool:
        return bool(self.flags & FLAG_DEFAULT)

    @property
    def is_row(self) -> bool:
        return bool(self.flags & FLAG_ROW) and not self.is_default

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type, f"?{self.type:#x}")

    def __repr__(self) -> str:                                    # pragma: no cover
        return f"<Column {self.name!r} {self.type_name}>"


class UtfTable:
    """One parsed @UTF table.  Cheap to construct; column values are read lazily."""

    def __init__(self, data: bytes | memoryview, base: int = 0):
        if base < 0 or base + 0x20 > len(data):
            raise UtfError("truncated header")
        if bytes(data[base:base + 4]) != MAGIC:
            raise UtfError(f"no @UTF signature at {base:#x}")

        self._d = data if isinstance(data, memoryview) else memoryview(data)
        self.base = base

        u32 = lambda o: self._u32(base + o)                        # noqa: E731

        # Section offsets in the header are stored relative to offset 0x08,
        # so each needs +8 to become a real offset within the table.
        self.size = u32(0x04) + 8
        self.version = self._u16(base + 0x08)
        self._rows_off = self._u16(base + 0x0A) + 8
        self._strs_off = u32(0x0C) + 8
        self._data_off = u32(0x10) + 8
        name_off = u32(0x14)          # already relative to the string section
        self.column_count = self._u16(base + 0x18)
        self.row_width = self._u16(base + 0x1A)
        self.row_count = u32(0x1C)

        if self.size > MAX_TABLE_SIZE:
            raise UtfError("implausible table size")
        if base + self.size > len(self._d):
            raise UtfError("table extends past end of buffer")
        if self.column_count > MAX_COLUMNS or self.row_count > MAX_ROWS:
            raise UtfError("implausible column/row count")
        if not (0x20 <= self._rows_off <= self._strs_off <= self._data_off <= self.size):
            raise UtfError("sections out of order")

        self._schema_off = 0x20
        self._strs_abs = base + self._strs_off
        self._strs_size = self._data_off - self._strs_off
        self._data_abs = base + self._data_off

        self.name = self._cstr(name_off)
        self._cols: list[Column] = []
        self._by_name: dict[str, int] = {}
        self._parse_schema()

    # ---------------------------------------------------------------- header

    def _u16(self, o: int) -> int:
        return int.from_bytes(self._d[o:o + 2], "big")

    def _u32(self, o: int) -> int:
        return int.from_bytes(self._d[o:o + 4], "big")

    def _cstr(self, off: int | None) -> str | None:
        """Read a NUL-terminated string from the string section."""
        if off is None or off < 0 or off >= self._strs_size:
            return None
        blob = self._d[self._strs_abs + off:self._strs_abs + self._strs_size]
        end = bytes(blob).find(b"\x00")
        raw = bytes(blob if end < 0 else blob[:end])
        return raw.decode("utf-8", "replace")

    # ---------------------------------------------------------------- schema

    def _parse_schema(self) -> None:
        p = self.base + self._schema_off
        row_cursor = 0
        d = self._d

        for _ in range(self.column_count):
            if p + 5 > self.base + self._rows_off:
                raise UtfError("schema overruns row section")
            info = d[p]
            name_off = self._u32(p + 1)
            p += 5

            flags = info & 0xF0
            type_ = info & 0x0F
            size = _TYPE_SIZE.get(type_)
            if size is None:
                raise UtfError(f"unknown column type {type_:#x}")
            if not (flags & FLAG_NAME) or (flags & FLAG_UNDEFINED):
                raise UtfError(f"unsupported column flags {flags:#x}")

            name = self._cstr(name_off)

            if flags & FLAG_DEFAULT:
                offset = p - self.base            # absolute position of the constant
                p += size
                if p > self.base + self._rows_off:
                    raise UtfError("schema constant overruns row section")
            else:
                offset = row_cursor
                row_cursor += size
                # Checked against the row *section*, not against row_width.
                # Since HCA v3, some tables (notably the ACB Header) declare a
                # row_width smaller than the sum of their row columns and let
                # the last columns read past it into blank space.  vgmstream
                # documents the same quirk; enforcing row_width here would
                # reject otherwise readable files.
                if row_cursor > (self._strs_off - self._rows_off):
                    raise UtfError("row columns exceed row section")

            col = Column(name, type_, flags, offset)
            index = len(self._cols)
            self._cols.append(col)
            if name and name not in self._by_name:
                self._by_name[name] = index

    # ---------------------------------------------------------------- access

    @property
    def columns(self) -> list[Column]:
        return list(self._cols)

    def column_names(self) -> list[str]:
        return [c.name or "" for c in self._cols]

    def index_of(self, name: str) -> int:
        """Column index, or -1 when absent (column sets vary between ACB versions)."""
        return self._by_name.get(name, -1)

    def has(self, name: str) -> bool:
        return name in self._by_name

    def _addr(self, row: int, col: Column) -> int | None:
        """Absolute buffer position of a column's value.

        DEFAULT columns keep one constant inside the schema section, so their
        position is `base + offset`; ROW columns live in the row section.
        Getting this wrong is subtle: for the root table ``base`` is 0 and the
        two coincide, so a mistake only shows up on nested tables.
        """
        if col.is_default:
            return self.base + col._offset
        if col.is_row:
            if row < 0 or row >= self.row_count:
                return None
            return self.base + self._rows_off + row * self.row_width + col._offset
        return None

    def raw(self, row: int, name: str) -> bytes:
        """Raw bytes of a VLDATA column."""
        idx = self.index_of(name)
        if idx < 0:
            return b""
        col = self._cols[idx]
        if col.type != 0x0B:
            return b""
        addr = self._addr(row, col)
        if addr is None or addr + 8 > len(self._d):
            return b""
        off = self._u32(addr)
        size = self._u32(addr + 4)
        start = self._data_abs + off
        if off >= (self.size - self._data_off) or start + size > len(self._d):
            return b""
        return bytes(self._d[start:start + size])

    def value(self, row: int, name: str, default: Any = None) -> Any:
        """Decoded value of a column.  Returns `default` when absent."""
        idx = self.index_of(name)
        if idx < 0:
            return default
        col = self._cols[idx]
        addr = self._addr(row, col)
        if addr is None or addr + _TYPE_SIZE.get(col.type, 0) > len(self._d):
            return default

        t = col.type
        b = self._d
        if t == 0x00:
            return b[addr]
        if t == 0x01:
            return int.from_bytes(b[addr:addr + 1], "big", signed=True)
        if t == 0x02:
            return self._u16(addr)
        if t == 0x03:
            return int.from_bytes(b[addr:addr + 2], "big", signed=True)
        if t == 0x04:
            return self._u32(addr)
        if t == 0x05:
            return int.from_bytes(b[addr:addr + 4], "big", signed=True)
        if t == 0x06:
            return int.from_bytes(b[addr:addr + 8], "big")
        if t == 0x07:
            return int.from_bytes(b[addr:addr + 8], "big", signed=True)
        if t == 0x08:
            return struct.unpack(">f", b[addr:addr + 4])[0]
        if t == 0x09:
            return struct.unpack(">d", b[addr:addr + 8])[0]
        if t == 0x0A:
            return self._cstr(self._u32(addr))
        if t == 0x0B:
            return self.raw(row, col.name or "")
        return default

    def u16(self, row: int, name: str, default: int = 0) -> int:
        v = self.value(row, name, default)
        return default if v is None else int(v)

    def u32(self, row: int, name: str, default: int = 0) -> int:
        v = self.value(row, name, default)
        return default if v is None else int(v)

    def text(self, row: int, name: str, default: str = "") -> str:
        v = self.value(row, name, default)
        return default if v is None else str(v)

    def u16_array(self, row: int, name: str) -> list[int]:
        """VLDATA interpreted as a big-endian u16 array.

        Used for the index lists in this format (TrackIndex, BlockIndex,
        ReferenceItems, DestinationBlocks...).
        """
        b = self.raw(row, name)
        return [int.from_bytes(b[i:i + 2], "big")
                for i in range(0, len(b) - 1, 2)]

    def u32_array(self, row: int, name: str) -> list[int]:
        b = self.raw(row, name)
        return [int.from_bytes(b[i:i + 4], "big")
                for i in range(0, len(b) - 3, 4)]

    def subtable(self, row: int, name: str) -> UtfTable | None:
        """Resolve a VLDATA column that holds a nested @UTF table."""
        idx = self.index_of(name)
        if idx < 0:
            return None
        col = self._cols[idx]
        if col.type != 0x0B:
            return None
        addr = self._addr(row, col)
        if addr is None or addr + 8 > len(self._d):
            return None
        off = self._u32(addr)
        start = self._data_abs + off
        if start + 4 > len(self._d) or bytes(self._d[start:start + 4]) != MAGIC:
            return None
        try:
            return UtfTable(self._d, start)
        except UtfError:
            return None

    def subtables(self) -> Iterator[tuple[int, str, UtfTable]]:
        """Yield ``(row, column_name, table)`` for every nested table."""
        for col in self._cols:
            if col.type != 0x0B or not col.name:
                continue
            for row in range(self.row_count):
                sub = self.subtable(row, col.name)
                if sub is not None:
                    yield row, col.name, sub

    def walk(self) -> Iterator[tuple[int, UtfTable]]:
        """Depth-first traversal of this table and every nested table.

        Yields ``(depth, table)``.  Cyclic/duplicate references are skipped,
        which matters because the root ``Header`` table points at every other
        table, so a naive walk would revisit them repeatedly.
        """
        seen: set[int] = set()

        def rec(t: UtfTable, depth: int) -> Iterator[tuple[int, UtfTable]]:
            if t.base in seen:
                return
            seen.add(t.base)
            yield depth, t
            for _, _, sub in t.subtables():
                yield from rec(sub, depth + 1)

        yield from rec(self, 0)

    def __repr__(self) -> str:                                    # pragma: no cover
        return f"<UtfTable {self.name!r} rows={self.row_count} cols={self.column_count}>"


def open_file(path) -> UtfTable:
    """Parse the table at offset 0 of a file."""
    with open(path, "rb") as fh:
        return UtfTable(fh.read(), 0)
