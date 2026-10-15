#!/usr/bin/env python3
"""The participant-code rule for provider-visible text, defined once (2.6.5).

Two stages used to disagree about what a participant code is. ``run_coordinator.py`` refused any
provider input containing a code-shaped token (``SUBJ_P12``, or a ``P12`` that opens a data row),
while Phase-1 output admission (``phase1_runtime.check_output_hygiene``) only refused an
identifier printed beside a date. So Phase 1 accepted, and paid for, reports that the coordinator
was then bound to refuse. On audit A20 all five auditors
quoted two of the deposit's SPSS column names (rating variables),
which match the code shape. The reports are
hash-bound to their invocation records, and the coordinator refused them before any provider
call. The audit could not continue.

This module is the single definition both stages now call:

* ``RAW_PARTICIPANT_CODE`` and ``ROW_PARTICIPANT_CODE`` are the shapes ``run_coordinator`` has
  always refused. They are unchanged.
* A token is exempt only when it is demonstrably a variable name of the deposit: it is a column
  header or dictionary name in one of the deposit's own data files, and it never appears as a
  data value anywhere in the deposit. A real code lives in an identifier column's cells, so it
  stays refused even when some column happens to share its spelling.
* Code, syntax, documentation, media and analysis-container files contribute nothing to the
  exemption, in either direction. A code can appear in a filter or a comment there, and the data
  dictionary already names every stored variable.
* A header that looks like one column per participant is never exempt. A file (or workbook sheet)
  is read that way when at least three of its non-empty header cells are code-shaped or begin
  with a code-shaped token followed by a separator (``P01_SD``, ``SUBJ_P01_L``), and those make
  up more than half of its non-empty header cells. Real variable dictionaries are far below that
  line: 8 of 138 SPSS variables in the A20 deposit, 12 of 162 to 534 columns in two
  other deposits. The test is a heuristic: a table with fewer than three such headers,
  or with more ordinary columns than participant columns, has its headers read as names.
* Every other file is read for data values, and the exemption fails closed: if any of them
  cannot be scanned in full, no token is exempt.

  - A data dictionary the module parses has its body scanned: the cells of a delimited table or
    workbook sheet, the data section of an SPSS file (after undoing bytecode or zlib compression)
    together with its value-label texts, and everything after a Stata file's ``<data>`` tag. When
    the reader fails on an SPSS, Stata or Excel file, that file is unscannable. Its raw bytes are
    no fallback: an SPSS string can be split across bytecode command blocks.
  - Any other file has its raw bytes scanned, after gzip, bzip2, xz, zip and UTF-16 decoding. The
    byte scan reads text, and R's serialization format (``.rds``, ``.RData``), which stores each
    string whole. An SPSS, Stata or Excel file inside a zip archive is parsed as well, and every
    token in it, header included, counts as a value. Binary data sections are also searched for
    each candidate name as a raw substring, because a fixed-width field can hold a code flush
    against the next field.
  - A file whose bytes hide their text is unscannable: Parquet, Arrow and Feather, zstd, lz4,
    snappy, raw zlib, 7z, RAR, HDF5, MATLAB, NumPy, pickle, SQLite, OLE2 (``.xls``), SPSS portable
    files, an R file in no known serialization, and any other binary whose bytes do not decode
    as text. So a format the module does not know can only make the rule stricter, never looser.
  - A directory that cannot be listed, or a file that cannot be examined or read, also makes the
    deposit unscannable.
  - Folder metadata written by macOS and Windows (``.DS_Store``, AppleDouble ``._`` files,
    ``Thumbs.db``, recognised by content) holds file names and display settings, not data. It is
    byte-scanned without the binary test.

Readable dictionaries are delimited text tables (``.csv``, ``.tsv``, ``.tab``, ``.dat``, or a
``.txt`` that parses as a table), Excel workbooks (``.xlsx``, ``.xlsm``), SPSS system files
(``.sav``, ``.zsav``) and Stata files in formats 117 to 119 (``.dta``).
"""
from __future__ import annotations

import bz2
import csv
import dataclasses
import functools
import gzip
import io
import lzma
import os
import pathlib
import re
import stat
import struct
import threading
import zipfile
import zlib
from typing import Iterable

RAW_PARTICIPANT_CODE = re.compile(
    r"(?<![A-Za-z0-9])[A-Z]{2,}(?:_[A-Z]{1,8})*_?P\d+(?![A-Za-z0-9])"
)
# The code a row signature opens is captured so the exemption can be applied to it.
ROW_PARTICIPANT_CODE = re.compile(
    r"(?:^|[,\t|])\s*(P\d{1,4})\s*[,\t|]\s*[-+]?\d+(?:\.\d+)?", re.MULTILINE
)
_CODE_SHAPED = re.compile(r"\A(?:[A-Z]{2,}(?:_[A-Z]{1,8})*_?P\d+|P\d{1,4})\Z")
_CODE_BODY = re.compile(r"(?:[A-Z]{2,}(?:_[A-Z]{1,8})*_?P\d+|P\d{1,4})")
_WORD_RUN = re.compile(rb"[A-Za-z0-9_]+")
_COMPANION = re.compile(r"\A(.+?)[_.\- ][A-Za-z0-9]{1,12}\Z")

DELIMITED_SUFFIXES = frozenset({".csv", ".tsv", ".tab", ".dat", ".txt"})
SPREADSHEET_SUFFIXES = frozenset({".xlsx", ".xlsm"})
SPSS_SUFFIXES = frozenset({".sav", ".zsav"})
STATA_SUFFIXES = frozenset({".dta"})
DATA_SUFFIXES = DELIMITED_SUFFIXES | SPREADSHEET_SUFFIXES | SPSS_SUFFIXES | STATA_SUFFIXES
# Formats read by a structured reader. When the reader fails, the file is unscannable.
STRUCTURED_SUFFIXES = SPREADSHEET_SUFFIXES | SPSS_SUFFIXES | STATA_SUFFIXES
# Files that never hold data values for this rule. Everything else is scanned for values.
CODE_SUFFIXES = frozenset({
    ".r", ".rmd", ".qmd", ".py", ".ipynb", ".do", ".ado", ".sps", ".syn", ".sas", ".m", ".jl",
    ".js", ".ts", ".sh", ".bat", ".ps1", ".pl", ".c", ".cpp", ".h", ".java", ".stan", ".bug",
    ".mplus", ".inp", ".lss",
})
DOCUMENT_SUFFIXES = frozenset({
    ".md", ".markdown", ".rst", ".pdf", ".doc", ".docx", ".odt", ".rtf", ".html", ".htm", ".tex",
    ".bib", ".ppt", ".pptx", ".key", ".epub", ".log",
})
MEDIA_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".svg", ".eps", ".ico", ".webp",
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".m4v", ".mp3", ".wav", ".flac", ".ogg",
    ".m4a", ".aac",
})
# Analysis files whose embedded specifications name the variables they analyse.
ANALYSIS_CONTAINER_SUFFIXES = frozenset({".jasp", ".omv", ".spv", ".jmp", ".mplus"})
IGNORED_SUFFIXES = CODE_SUFFIXES | DOCUMENT_SUFFIXES | MEDIA_SUFFIXES | ANALYSIS_CONTAINER_SUFFIXES
# Value-bearing formats a byte scan cannot read, whatever their first bytes say. SPSS portable
# files break every 80 characters, so a code can straddle two lines.
OPAQUE_SUFFIXES = frozenset({
    ".parquet", ".feather", ".arrow", ".h5", ".hdf5", ".mat", ".npy", ".pkl", ".pickle",
    ".sqlite", ".sqlite3", ".7z", ".rar", ".xls", ".por", ".sas7bdat", ".fst", ".qs",
})
R_SUFFIXES = frozenset({".rds", ".rdata", ".rda"})
_COMPRESSION_SUFFIXES = frozenset({".gz", ".gzip", ".bz2", ".xz"})

MAX_FILE_BYTES = 512 * 1024 * 1024      # on disk, per file
MAX_INFLATED_BYTES = 512 * 1024 * 1024  # after decompression, per file
MAX_ZIP_DEPTH = 2
MAX_NESTING = 4                         # containers of any kind, one inside another
PARTICIPANT_COLUMNS_MIN = 3
PARTICIPANT_COLUMNS_SHARE = 0.5
_DELIMITERS = (",", ";", "\t", "|")

# Signatures of formats whose bytes hide their text (compressed, columnar or paged storage).
_OPAQUE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x28\xb5\x2f\xfd", "zstd-compressed"),
    (b"\x04\x22\x4d\x18", "lz4-compressed"),
    (b"\xff\x06\x00\x00sNaPpY", "snappy-compressed"),
    (b"\x1f\x9d", "compress(1)-compressed"),
    (b"LZIP\x01", "lzip-compressed"),
    (b"7z\xbc\xaf\x27\x1c", "a 7z archive"),
    (b"Rar!\x1a\x07", "a RAR archive"),
    (b"SQLite format 3\x00", "an SQLite database"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "an OLE2 compound file (.xls)"),
    (b"\x93NUMPY", "a NumPy array"),
)
# ASCII signatures that open AND close the file, so a text table whose first column happens to be
# called PAR1 is not mistaken for Parquet.
_FRAMED_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"PAR1", "a Parquet file"),
    (b"ARROW1", "an Arrow/Feather file"),
    (b"FEA1", "a Feather file"),
)
_HDF5_MAGIC = b"\x89HDF\r\n\x1a\n"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_R_WRAPPERS = (b"RDX2\n", b"RDX3\n", b"RDA2\n", b"RDA3\n", b"RDB2\n", b"RDB3\n")
_WINDOWS_THUMBNAILS = frozenset({"thumbs.db", "ehthumbs.db", "ehthumbs_vista.db"})
# Bytes that do not occur in text. Tab, newline, vertical tab, form feed, carriage return,
# backspace, SUB (a DOS end-of-file mark) and ESC do.
_CONTROL_BYTES = bytes(b for b in range(32) if b not in (8, 9, 10, 11, 12, 13, 26, 27)) + b"\x7f"
_HIGH_BYTES = bytes(range(128, 256))
_TEXT_WINDOW = 64 * 1024


class _Unscannable(ValueError):
    """A file whose values cannot be read in full; the exemption then fails closed."""


@dataclasses.dataclass(frozen=True)
class DepositVocabulary:
    """What a deposit's files say about code-shaped tokens."""

    names: frozenset[str]
    values: frozenset[str]
    unreadable: tuple[str, ...]
    participant_columns: frozenset[str] = frozenset()
    unscannable: tuple[str, ...] = ()

    @property
    def exempt(self) -> frozenset[str]:
        if self.unscannable:
            return frozenset()
        return self.names - self.values - self.participant_columns


@dataclasses.dataclass(frozen=True)
class CodeHit:
    line: int
    kind: str  # "raw" or "row"


@dataclasses.dataclass
class _Parsed:
    names: set[str]
    values: set[str]
    participant_columns: set[str]
    binary: list[bytes]


def _code_shaped(token: str) -> bool:
    return bool(_CODE_SHAPED.fullmatch(token))


def _participant_like(header: str) -> bool:
    """A header cell that names a participant: a code, or a code with a short suffix."""
    if _code_shaped(header):
        return True
    companion = _COMPANION.fullmatch(header)
    return bool(companion and _code_shaped(companion.group(1)))


def _value_tokens(data: bytes) -> set[str]:
    """Every token the report patterns would treat as a code, found in linear time.

    A code is a maximal run of letters, digits and underscores, or an underscore-delimited part
    of one (the patterns' lookarounds exclude letters and digits, not underscores), so each run
    is split on underscores and every contiguous window of up to ten parts is tested.
    """
    tokens: set[str] = set()
    for run in _WORD_RUN.findall(data):
        text = run.decode("ascii")
        if "P" not in text:
            continue
        parts = text.split("_")
        for start in range(len(parts)):
            for end in range(start + 1, min(len(parts), start + 10) + 1):
                candidate = "_".join(parts[start:end])
                if candidate and _CODE_SHAPED.fullmatch(candidate):
                    tokens.add(candidate)
    return tokens


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


# ------------------------------------------------------------------------ what a byte scan reads
def _zlib_stream(data: bytes) -> bool:
    """A raw zlib stream: a valid two-byte header, and data that inflates to its end or to 64 KiB.

    Text can open with a valid header (``H,`` is one), but it does not inflate that far.
    """
    if len(data) < 2 or data[0] & 0x0F != 8 or data[0] >> 4 > 7 or ((data[0] << 8) | data[1]) % 31:
        return False
    decompressor = zlib.decompressobj()
    try:
        inflated = decompressor.decompress(data[:1 << 20], 1 << 16)
    except zlib.error:
        return False
    return decompressor.eof or len(inflated) >= 1 << 16


def _opaque_kind(data: bytes, suffix: str) -> str | None:
    """Why a byte scan cannot read this file, or None."""
    if suffix in OPAQUE_SUFFIXES:
        return f"a {suffix} file"
    for magic, kind in _OPAQUE_MAGIC:
        if data.startswith(magic):
            return kind
    for magic, kind in _FRAMED_MAGIC:
        if data.startswith(magic) and data.endswith(magic):
            return kind
    # HDF5 (MATLAB 7.3 and netCDF-4 too) may sit behind a user block of 512 * 2^n bytes.
    if any(data[offset:offset + 8] == _HDF5_MAGIC for offset in (0, 512, 1024, 2048, 4096, 8192)):
        return "an HDF5 file"
    if data.startswith(b"MATLAB") and b"MAT-file" in data[:128]:
        return "a MATLAB MAT-file"
    if len(data) >= 2 and data[0] == 0x80 and 2 <= data[1] <= 5:
        return "a Python pickle"
    if _zlib_stream(data):
        return "a raw zlib stream"
    return None


def _r_serialized(data: bytes) -> bool:
    """R's serialization format (saveRDS, save), in which every string is stored whole."""
    body = data[5:] if data[:5] in _R_WRAPPERS else data
    if body[:2] == b"X\n":
        return body[2:6] in (b"\0\0\0\x02", b"\0\0\0\x03")
    if body[:2] == b"B\n":
        return body[2:6] in (b"\x02\0\0\0", b"\x03\0\0\0")
    if body[:2] == b"A\n":
        return body[2:4] in (b"2\n", b"3\n")
    return False


def _folder_metadata(data: bytes, name: str) -> bool:
    """Finder and Explorer metadata: file names and display settings, never data values."""
    if data.startswith((b"\x00\x05\x16\x07", b"\x00\x05\x16\x00", b"\x00\x00\x00\x01Bud1")):
        return True  # AppleDouble, AppleSingle, .DS_Store
    return name.lower() in _WINDOWS_THUMBNAILS and data.startswith(_OLE2_MAGIC)


def _undecodable(data: bytes) -> bool:
    """Binary a byte scan cannot read as text.

    Control bytes above 1 % of any 64 KiB window mark binary (compressed or packed data carries
    about 11 %), as does a file that is mostly non-ASCII bytes and not UTF-8.
    """
    for start in range(0, len(data), _TEXT_WINDOW):
        window = data[start:start + _TEXT_WINDOW]
        controls = len(window) - len(window.translate(None, _CONTROL_BYTES))
        if controls * 100 > len(window):
            return True
    high = len(data) - len(data.translate(None, _HIGH_BYTES))
    if high * 10 > len(data) * 3:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            return not (exc.reason == "unexpected end of data" and exc.start >= len(data) - 3)
    return False


def _textual(data: bytes, label: str, suffix: str = "") -> bytes:
    """The bytes a text scan reads (UTF-16 decoded), or _Unscannable for data that is not text."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) and suffix not in OPAQUE_SUFFIXES:
        text = data.decode("utf-16", errors="replace")
        if text.count("\ufffd") * 100 > len(text):
            raise _Unscannable(f"{label} does not decode as UTF-16 text")
        data = text.encode("utf-8", errors="replace")
    else:
        kind = _opaque_kind(data, suffix)
        if kind:
            raise _Unscannable(f"{label} is {kind}, which a byte scan cannot read")
    if _undecodable(data):
        raise _Unscannable(f"{label} is binary data that does not decode as text")
    return data


# ---------------------------------------------------------------------------- data dictionaries
def _clean_header(cell: str) -> str:
    return cell.strip().strip('"').strip("'").strip().lstrip("﻿")


def _participant_columns(headers: list[str]) -> set[str]:
    cells = [header for header in headers if header]
    flagged = [header for header in cells if _participant_like(header)]
    if len(flagged) >= PARTICIPANT_COLUMNS_MIN and len(flagged) > PARTICIPANT_COLUMNS_SHARE * len(cells):
        names = set()
        for header in flagged:
            companion = _COMPANION.fullmatch(header)
            names.add(header if _code_shaped(header) else companion.group(1))
        return names
    return set()


def _delimited(data: bytes, label: str = "", suffix: str = "") -> _Parsed | None:
    """Header names and value tokens of a delimited table, or None for text that is no table.

    Bytes that are not text are _Unscannable: a binary ``.dat`` must not pass as a table.
    """
    _textual(data, label or "delimited file", suffix)
    text = _decode_text(data)
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    first = lines[0]
    delimiter = max(_DELIMITERS, key=first.count)
    if first.count(delimiter) == 0:
        return None
    try:
        header = [_clean_header(cell) for cell in next(csv.reader([first], delimiter=delimiter))]
        widths = {len(row) for row in csv.reader(lines[1:6], delimiter=delimiter)}
    except csv.Error:
        return None
    # Prose split on commas does not keep a constant field count; a table does.
    if len(header) < 2 or len(header) not in widths:
        return None
    body = text.split(first, 1)[1] if first in text else "\n".join(lines[1:])
    return _Parsed(
        {name for name in header if _code_shaped(name)},
        _value_tokens(body.encode("utf-8", errors="replace")),
        _participant_columns(header),
        [],
    )


class _FormatError(ValueError):
    pass


def _spreadsheet(source) -> _Parsed:
    """A workbook's first non-empty row per sheet as names, every later row as values.

    ``source`` is a path or a binary file object. A workbook openpyxl cannot open is a failed
    structured read, and so is a missing openpyxl: both make the file unscannable.
    """
    try:
        import openpyxl  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise _Unscannable("openpyxl is not installed, so the workbook cannot be read") from exc
    try:
        workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    except Exception as exc:  # corrupt or password-protected workbook
        raise _FormatError(f"workbook cannot be opened: {type(exc).__name__}: {exc}") from exc
    parsed = _Parsed(set(), set(), set(), [])
    try:
        for sheet in workbook.worksheets:
            header: list[str] | None = None
            for row in sheet.iter_rows(values_only=True):
                cells = [str(cell) if cell is not None else "" for cell in row]
                if not any(cell.strip() for cell in cells):
                    continue
                if header is None:
                    header = [_clean_header(cell) for cell in cells]
                    parsed.names.update(name for name in header if _code_shaped(name))
                    parsed.participant_columns |= _participant_columns(header)
                else:
                    parsed.values |= _value_tokens(" ".join(cells).encode("utf-8", errors="replace"))
    finally:
        workbook.close()
    return parsed


def _spss_bytecode(stream: bytes) -> bytes:
    """Undo SPSS bytecode compression into string cells, with one separator per number.

    A string value longer than eight bytes is stored as consecutive raw cells, and a command block
    can fall between them, so the stream must be decoded before it is searched. Numbers carry no
    text, so each costs one separator byte rather than eight.
    """
    out = bytearray()
    position = 0
    size = len(stream)
    while position + 8 <= size:
        commands = stream[position:position + 8]
        position += 8
        for code in commands:
            if code == 0:
                continue
            if code == 252:
                return bytes(out)
            if code == 253:
                if position + 8 > size:
                    return bytes(out)
                out += stream[position:position + 8]
                position += 8
            elif code == 254:
                out += b" " * 8
            else:  # 1-251 compressed numbers and 255 system-missing carry no text
                out += b"\n"
            if len(out) > MAX_INFLATED_BYTES:
                raise _Unscannable("SPSS data section inflates beyond the scan limit")
    return bytes(out)


def _spss_long_string_labels(payload: bytes, endian: str) -> list[bytes]:
    """Values and label texts of an extension record 7 subtype 21 (long string value labels).

    Per variable: int32 name length, name, int32 width, int32 label count, then per label an
    int32-prefixed value and an int32-prefixed label. The variable names are not collected.
    """
    texts: list[bytes] = []
    position, size = 0, len(payload)

    def integer() -> int:
        nonlocal position
        if position + 4 > size:
            raise _FormatError("truncated long string value labels")
        value = struct.unpack_from(endian + "i", payload, position)[0]
        position += 4
        return value

    def chunk(length: int) -> bytes:
        nonlocal position
        if length < 0 or position + length > size:
            raise _FormatError("long string value label runs past its record")
        piece = payload[position:position + length]
        position += length
        return piece

    while position < size:
        chunk(integer())  # variable name
        integer()         # variable width
        count = integer()
        if count < 0:
            raise _FormatError("negative long string value label count")
        for _ in range(count):
            texts.append(chunk(integer()))  # value
            texts.append(chunk(integer()))  # label
    return texts


def _spss(data: bytes) -> _Parsed:
    if data[:4] not in (b"$FL2", b"$FL3") or len(data) < 176:
        raise _FormatError("not an SPSS system file")
    layout_le = struct.unpack("<i", data[64:68])[0]
    endian = "<" if layout_le in (2, 3) else ">"
    compression = struct.unpack(endian + "i", data[72:76])[0]
    size = len(data)

    def integer(offset: int) -> int:
        if offset < 0 or offset + 4 > size:
            raise _FormatError("truncated dictionary")
        return struct.unpack(endian + "i", data[offset:offset + 4])[0]

    def advance(offset: int, amount: int) -> int:
        if amount < 0 or offset + amount > size:
            raise _FormatError("dictionary record length is negative or runs past the file")
        return offset + amount

    names: set[str] = set()
    header: list[str] = []
    var_types: list[int] = []  # one per dictionary index; a long string's continuations are -1
    labels: list[bytes] = []   # value-label texts, and the values of labelled string variables
    labelled: list[tuple[list[bytes], tuple[int, ...]]] = []
    offset = 176
    while True:
        start = offset
        record = integer(offset)
        offset += 4
        if record == 2:
            var_type, has_label, n_missing = integer(offset), integer(offset + 4), integer(offset + 8)
            offset = advance(offset, 20)
            short = data[offset:offset + 8]
            offset = advance(offset, 8)
            var_types.append(var_type)
            if var_type != -1:
                name = _decode_text(short).strip()
                names.add(name)
                header.append(name)
            if has_label not in (0, 1) or abs(n_missing) > 3:
                raise _FormatError("variable record has an invalid label flag or missing count")
            if has_label:
                length = integer(offset)
                offset = advance(offset + 4, ((length + 3) // 4) * 4) if length >= 0 else advance(offset, -1)
            offset = advance(offset, abs(n_missing) * 8)
        elif record == 3:
            # Value labels: int32 count; per label an 8-byte value, a 1-byte label length and the
            # label, padded so that 1 + length is a multiple of 8. Record 4, which names the
            # variables the labels belong to, must follow at once.
            count = integer(offset)
            offset = advance(offset, 4)
            if count < 0:
                raise _FormatError("negative value-label count")
            values: list[bytes] = []
            for _ in range(count):
                if offset + 9 > size:
                    raise _FormatError("truncated value labels")
                values.append(data[offset:offset + 8])
                length = data[offset + 8]
                labels.append(data[offset + 9:offset + 9 + length])
                offset = advance(offset, 8 + ((length + 1 + 7) // 8) * 8)
            if integer(offset) != 4:
                raise _FormatError("value labels are not followed by their variable record")
            n_vars = integer(offset + 4)
            offset = advance(offset, 8)
            if n_vars < 0:
                raise _FormatError("negative value-label variable count")
            first = offset
            offset = advance(offset, n_vars * 4)
            labelled.append((values, struct.unpack_from(f"{endian}{n_vars}i", data, first)))
        elif record == 4:
            raise _FormatError("value-label variable record without its value labels")
        elif record == 6:
            count = integer(offset)
            offset = advance(offset + 4, count * 80) if count >= 0 else advance(offset, -1)
        elif record == 7:
            subtype, item, count = integer(offset), integer(offset + 4), integer(offset + 8)
            if item < 0 or count < 0:
                raise _FormatError("negative extension record size")
            offset = advance(offset, 12)
            payload = data[offset:offset + item * count]
            offset = advance(offset, item * count)
            if subtype == 13:  # long variable names: SHORT=Long<TAB>SHORT2=Long2
                for pair in _decode_text(payload).split("\t"):
                    short_name, _, long_name = pair.partition("=")
                    for part in (short_name.strip(), long_name.strip()):
                        if part:
                            names.add(part)
                    if long_name.strip() and short_name.strip() in header:
                        header[header.index(short_name.strip())] = long_name.strip()
            elif subtype == 21:  # value labels of string variables wider than eight bytes
                labels.extend(_spss_long_string_labels(payload, endian))
        elif record == 999:
            offset = advance(offset, 4)
            break
        else:
            raise _FormatError(f"unknown dictionary record type {record}")
        if offset <= start:
            raise _FormatError("dictionary parsing did not advance")
    for values, indices in labelled:
        if any(not 1 <= index <= len(var_types) for index in indices):
            raise _FormatError("value labels name a variable that is not in the dictionary")
        if any(var_types[index - 1] > 0 for index in indices):
            labels.extend(values)  # a string variable's labelled values are stored text
    body = data[offset:]
    if data[:4] == b"$FL3" or compression == 2:
        if len(body) < 24:
            raise _FormatError("truncated zsav header")
        ztrailer = struct.unpack(endian + "q", body[8:16])[0] - offset
        if not 24 <= ztrailer <= len(body):
            raise _FormatError("zsav trailer offset is outside the file")
        position, inflated = 24, bytearray()
        while position < ztrailer:
            decompressor = zlib.decompressobj()
            chunk = decompressor.decompress(body[position:ztrailer], MAX_INFLATED_BYTES - len(inflated) + 1)
            if decompressor.unconsumed_tail:
                raise _Unscannable("zsav data inflates beyond the scan limit")
            inflated += chunk
            used = len(body[position:ztrailer]) - len(decompressor.unused_data)
            if used <= 0:
                raise _FormatError("zsav block did not decompress")
            position += used
        cells = _spss_bytecode(bytes(inflated))
    elif compression == 1:
        cells = _spss_bytecode(body)
    else:
        cells = body
    # Label texts are decoded like every other SPSS string, then read for tokens.
    label_text = "\n".join(_decode_text(text) for text in labels)
    return _Parsed({name for name in names if _code_shaped(name)},
                   _value_tokens(cells) | _value_tokens(label_text.encode("utf-8", errors="replace")),
                   _participant_columns(header), [cells])


def _stata(data: bytes) -> _Parsed:
    if not data.startswith(b"<stata_dta>"):
        raise _FormatError("only Stata formats 117-119 are read")
    release = re.search(rb"<release>(\d+)</release>", data[:200])
    order = re.search(rb"<byteorder>(LSF|MSF)</byteorder>", data[:200])
    if not release or not order:
        raise _FormatError("Stata header lacks release or byte order")
    version = int(release.group(1))
    endian = "<" if order.group(1) == b"LSF" else ">"
    k_match = re.search(rb"<K>(.{2,4}?)</K>", data[:300], re.S)
    start, end = data.find(b"<varnames>"), data.find(b"</varnames>")
    if not k_match or start < 0 or end < 0:
        raise _FormatError("Stata file lacks variable names")
    raw_k = k_match.group(1)
    nvar = struct.unpack(endian + ("I" if len(raw_k) == 4 else "H"), raw_k)[0]
    width = 33 if version == 117 else 129
    block = data[start + len(b"<varnames>"):end]
    if nvar <= 0 or len(block) != nvar * width:
        raise _FormatError("Stata variable-name block has an unexpected size")
    header = [
        _decode_text(block[index * width:(index + 1) * width].split(b"\0", 1)[0]).strip()
        for index in range(nvar)
    ]
    data_start = data.find(b"<data>")
    if data_start < 0:
        raise _FormatError("Stata file lacks a data section")
    body = data[data_start:]  # data, strLs and value labels
    return _Parsed({name for name in header if _code_shaped(name)}, _value_tokens(body),
                   _participant_columns(header), [body])


def _structured(data_or_path, suffix: str) -> _Parsed:
    if suffix in SPREADSHEET_SUFFIXES:
        source = data_or_path if isinstance(data_or_path, pathlib.Path) else io.BytesIO(data_or_path)
        return _spreadsheet(source)
    data = data_or_path.read_bytes() if isinstance(data_or_path, pathlib.Path) else data_or_path
    if suffix in SPSS_SUFFIXES:
        return _spss(data)
    return _stata(data)


# ------------------------------------------------------------------------------ the byte backstop
def _inflate(data: bytes, label: str) -> bytes:
    """Undo a whole-file gzip, bzip2 or xz wrapper (an .rds or .RData file is gzip by default)."""
    try:
        if data[:2] == b"\x1f\x8b":
            return _bounded(gzip.GzipFile(fileobj=io.BytesIO(data)), label)
        if _bzip2(data):
            return _bounded(bz2.BZ2File(io.BytesIO(data)), label)
        if data[:6] == b"\xfd7zXZ\x00":
            return _bounded(lzma.LZMAFile(io.BytesIO(data)), label)
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise _Unscannable(f"{label} does not decompress: {exc}") from exc
    return data


def _bzip2(data: bytes) -> bool:
    # "BZh", a block-size digit and the block magic: a text file that opens with "BZh" is not one.
    return data[:3] == b"BZh" and data[3:4].isdigit() and data[4:10] in (b"1AY&SY", b"\x17rE8P\x90")


def _compressed(data: bytes) -> bool:
    return data[:2] == b"\x1f\x8b" or _bzip2(data) or data[:6] == b"\xfd7zXZ\x00"


def _bounded(handle, label: str) -> bytes:
    data = handle.read(MAX_INFLATED_BYTES + 1)
    if len(data) > MAX_INFLATED_BYTES:
        raise _Unscannable(f"{label} inflates beyond the scan limit")
    return data


def _member_values(data: bytes, label: str, suffix: str, candidates: frozenset[str]) -> set[str]:
    """Every token of a data file stored inside an archive, header included.

    The file is parsed as it would be on disk, and a failed read is unscannable. Its names count as
    values too, as they did when archive members were only byte-scanned, so a zipped copy of a
    data file still revokes its own names; SPSS and Stata bytes are also scanned raw.
    """
    try:
        parsed = _structured(data, suffix)
    except _Unscannable:
        raise
    except Exception as exc:  # the reader failed: the member is unscannable, as on disk
        raise _Unscannable(f"{label} cannot be read: {type(exc).__name__}: {exc}") from exc
    values = parsed.names | parsed.values
    if suffix not in SPREADSHEET_SUFFIXES:
        values |= _value_tokens(data)
    return values | {candidate for candidate in candidates
                     if any(candidate.encode("ascii") in blob for blob in parsed.binary)}


def _raw_values(data: bytes, label: str, candidates: frozenset[str] = frozenset(),
                name: str | None = None, depth: int = 0, zip_depth: int = 0) -> set[str]:
    """Value tokens in a file's raw bytes, or _Unscannable when the bytes hide their text.

    ``name`` is the file's own name (a zip member's, or the name without its .gz); ``label``
    says where it sits, for messages. Archives are opened (zip) or decompressed (gzip, bzip2, xz)
    and their contents read the same way; a data file inside a zip is also parsed. R
    serialization is read raw and also searched for each candidate name as a substring. Folder
    metadata is read raw. Anything else must be text.
    """
    name = pathlib.PurePosixPath(label).name if name is None else name
    suffix = pathlib.PurePosixPath(name).suffix.lower()
    if depth > MAX_NESTING:
        raise _Unscannable(f"{label} nests containers deeper than {MAX_NESTING}")
    if data[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        if zip_depth >= MAX_ZIP_DEPTH:
            raise _Unscannable(f"{label} nests archives deeper than {MAX_ZIP_DEPTH}")
        values: set[str] = set()
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for member in archive.infolist():
                    member_name = pathlib.PurePosixPath(member.filename).name
                    member_suffix = pathlib.PurePosixPath(member_name).suffix.lower()
                    if member.is_dir() or member_suffix in IGNORED_SUFFIXES:
                        continue
                    if member.file_size > MAX_INFLATED_BYTES:
                        raise _Unscannable(f"{label}:{member.filename} is beyond the scan limit")
                    content = archive.read(member)
                    member_label = f"{label}:{member.filename}"
                    if member_suffix in STRUCTURED_SUFFIXES:
                        values |= _member_values(content, member_label, member_suffix, candidates)
                    else:
                        values |= _raw_values(content, member_label, candidates, member_name,
                                              depth + 1, zip_depth + 1)
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError) as exc:
            raise _Unscannable(f"{label} is an archive that cannot be read: {exc}") from exc
        return values
    if _compressed(data):
        inner = name[:-len(suffix)] if suffix in _COMPRESSION_SUFFIXES else name
        return _raw_values(_inflate(data, label), label, candidates, inner, depth + 1, zip_depth)
    if _folder_metadata(data, name):
        return _value_tokens(data)
    if _r_serialized(data):
        return _value_tokens(data) | {candidate for candidate in candidates
                                      if candidate.encode("ascii") in data}
    if suffix in R_SUFFIXES:
        raise _Unscannable(f"{label} is an R file in no serialization format this module reads")
    return _value_tokens(_textual(data, label, suffix))


# ------------------------------------------------------------------------------- the deposit walk
def _walk(pkg_dir: pathlib.Path) -> tuple[list[tuple[str, pathlib.Path]], list[str]]:
    """Every regular file, and every internal symlink to one, as Phase 1 copies them (source_files).

    Directory symlinks are not descended; dangling, looping and escaping symlinks are skipped, as
    Phase 1 skips (or refuses) them. A directory that cannot be listed, or an entry that cannot be
    examined, is returned as an error: its files would otherwise drop out of the scan silently.
    """
    root = pkg_dir.resolve()
    rows: list[tuple[str, pathlib.Path]] = []
    errors: list[str] = []

    def relative(path) -> str:
        try:
            return pathlib.Path(path).relative_to(pkg_dir).as_posix()
        except ValueError:
            return str(path)

    def unlistable(exc: OSError) -> None:
        errors.append(f"{relative(exc.filename)}: directory cannot be listed: {exc.strerror or exc}")

    for current, dirnames, filenames in os.walk(pkg_dir, onerror=unlistable):
        for entry_name in dirnames + filenames:
            path = pathlib.Path(current) / entry_name
            try:
                mode = os.lstat(path).st_mode
            except OSError as exc:
                errors.append(f"{relative(path)}: cannot be examined: {exc.strerror or exc}")
                continue
            if stat.S_ISLNK(mode):
                try:
                    target = path.resolve(strict=True)
                    target.relative_to(root)
                except (OSError, RuntimeError, ValueError):
                    continue  # dangling, looping or escaping: Phase 1 skips or refuses it too
                try:
                    target_mode = os.stat(target).st_mode
                except OSError as exc:
                    errors.append(f"{relative(path)}: symlink target cannot be examined: {exc.strerror or exc}")
                    continue
                if stat.S_ISREG(target_mode):
                    rows.append((relative(path), target))
            elif stat.S_ISREG(mode):
                rows.append((relative(path), path))
            # Real directories are walked by os.walk; special files hold no values.
    rows.sort(key=lambda row: row[0])
    return rows, errors


def _fingerprint(pkg_dir: pathlib.Path) -> tuple[tuple, ...]:
    """The cache key: every file's identity, then every walk error keyed None (a relative path is
    never None), so a deposit whose error is fixed is computed afresh."""
    rows, errors = _walk(pkg_dir)
    keyed: list[tuple] = []
    for relative, path in rows:
        try:
            status = path.stat()
        except OSError as exc:
            errors.append(f"{relative}: cannot be examined: {exc.strerror or exc}")
            continue
        keyed.append((relative, status.st_size, status.st_mtime_ns, status.st_ctime_ns, status.st_ino))
    return tuple(keyed) + tuple((None, message) for message in errors)


def _compute(pkg_dir: pathlib.Path, fingerprint: tuple[tuple, ...]) -> DepositVocabulary:
    names: set[str] = set()
    values: set[str] = set()
    participant_columns: set[str] = set()
    binary: list[bytes] = []
    unreadable: list[str] = []
    unscannable: list[str] = [row[1] for row in fingerprint if row[0] is None]
    files = dict(_walk(pkg_dir)[0])
    backstop: list[tuple[str, pathlib.Path]] = []
    # Pass 1: the data dictionaries. Their bodies are scanned for values as they are parsed.
    for relative, size, *_ in fingerprint:
        if relative is None:
            continue
        suffix = pathlib.PurePosixPath(relative).suffix.lower()
        if suffix in IGNORED_SUFFIXES:
            continue
        path = files.get(relative)
        if path is None:
            unscannable.append(f"{relative}: disappeared while the deposit was read")
            continue
        if size > MAX_FILE_BYTES:
            unscannable.append(f"{relative}: larger than the scan limit")
            continue
        if suffix not in DATA_SUFFIXES:
            backstop.append((relative, path))
            continue
        try:
            if suffix in STRUCTURED_SUFFIXES:
                parsed = _structured(path, suffix)
            else:
                parsed = _delimited(path.read_bytes(), relative, suffix)
        except _Unscannable as exc:
            unscannable.append(f"{relative}: {exc}")
            continue
        except Exception as exc:
            # A failed read is a failed scan. There is no byte fallback for a structured format:
            # an SPSS string can be split across bytecode command blocks.
            unreadable.append(relative)
            unscannable.append(f"{relative}: cannot be read: {type(exc).__name__}: {exc}")
            continue
        if parsed is None:  # text that is no table: its bytes are scanned below
            unreadable.append(relative)
            backstop.append((relative, path))
            continue
        names |= parsed.names
        values |= parsed.values
        participant_columns |= parsed.participant_columns
        binary.extend(parsed.binary)
    # Pass 2: every other file, for values only -- needed only when some name could be exempt.
    candidates = frozenset(names - values - participant_columns)
    if candidates:
        for relative, path in backstop:
            try:
                values |= _raw_values(path.read_bytes(), relative, candidates)
            except (_Unscannable, OSError) as exc:
                unscannable.append(f"{relative}: {exc}")
    # A fixed-width binary field can hold a code flush against the next field's bytes, where the
    # token boundary the patterns need is missing. Search each candidate name as raw bytes too.
    for name in list(names - values):
        needle = name.encode("ascii")
        if any(needle in blob for blob in binary):
            values.add(name)
    return DepositVocabulary(frozenset(names), frozenset(values), tuple(unreadable),
                             frozenset(participant_columns), tuple(unscannable))


_LOCK = threading.Lock()


@functools.lru_cache(maxsize=32)
def _vocabulary(root: str, fingerprint: tuple[tuple, ...]) -> DepositVocabulary:
    return _compute(pathlib.Path(root), fingerprint)


def deposit_vocabulary(pkg_dir: pathlib.Path | str | None) -> DepositVocabulary:
    """Code-shaped variable names and data values found in a deposit's own files.

    Concurrent callers (Phase 1 starts five roles at once) compute a deposit once: the lock makes
    the first caller fill the cache and the others read it.
    """
    if pkg_dir is None:
        return DepositVocabulary(frozenset(), frozenset(), ())
    path = pathlib.Path(pkg_dir)
    if not path.is_dir():
        return DepositVocabulary(frozenset(), frozenset(), ())
    resolved = path.resolve()
    with _LOCK:
        return _vocabulary(str(resolved), _fingerprint(resolved))


def deposit_exemptions(pkg_dir: pathlib.Path | str | None) -> frozenset[str]:
    """Tokens a provider may see although they have the participant-code shape."""
    return deposit_vocabulary(pkg_dir).exempt


def first_participant_code(text: str, exempt: Iterable[str] = frozenset()) -> CodeHit | None:
    """The first code-shaped token in ``text`` that is not an exempt deposit variable name."""
    allowed = frozenset(exempt)
    hits: list[tuple[int, str]] = []
    for match in RAW_PARTICIPANT_CODE.finditer(text):
        if match.group(0) not in allowed:
            hits.append((match.start(), "raw"))
            break
    for match in ROW_PARTICIPANT_CODE.finditer(text):
        if match.group(1) not in allowed:
            hits.append((match.start(1), "row"))
            break
    if not hits:
        return None
    position, kind = min(hits)
    return CodeHit(line=text.count("\n", 0, position) + 1, kind=kind)


def has_participant_code(text: str, exempt: Iterable[str] = frozenset()) -> bool:
    return first_participant_code(text, exempt) is not None


def exempt_spans(text: str, exempt: Iterable[str]) -> list[tuple[int, int]]:
    """Character spans of exempt deposit names in ``text`` (for detectors with other patterns)."""
    allowed = frozenset(exempt)
    return [match.span() for match in RAW_PARTICIPANT_CODE.finditer(text) if match.group(0) in allowed]


RULE_STATEMENT = (
    "Never write a token shaped like a participant code anywhere in the report OR in result.json: "
    "two or more capital letters, optionally with underscore-joined capital segments, followed by P "
    "and digits (such as SUBJ_P12 or EMG_P01), or P plus digits opening a comma-, tab- or "
    "pipe-separated row followed by a number (such as P12, 3.4). The rule is mechanical: it covers "
    "file names, variables created only in syntax, labels and quotations too, so describe such "
    "things in words instead (for example 'the participant-12 file') and describe participants by "
    "counts or row positions. It covers source joins and evidence paths as well: a deposit file whose "
    "name contains such a token must not be cited in a source join or an evidence path, even though "
    "the manifest lists it; bind that evidence to another manifest entry instead (for example the "
    "script that reads the file) and describe the file in words. The one exception: a column or "
    "variable name that is a header in one of the deposit's data files and never a data value (for "
    "example INT_P05) is not an identifier and may be quoted."
)
