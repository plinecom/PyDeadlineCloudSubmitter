"""Minimal Blender .blend file parser.

Extracts the Blender version (from the file header) and the render frame
range (Scene.r.sfra / Scene.r.efra) without requiring Blender or any
third-party packages — only the standard library.

Usage:
    from blendfile import read_blender_version, read_frame_range
    version = read_blender_version("path/to/file.blend")  # e.g. "4.5"
    start, end = read_frame_range("path/to/file.blend")

Or as a CLI smoke test:
    python blendfile.py path/to/file.blend
"""

from __future__ import annotations

import gzip
import re
import struct
import sys
from pathlib import Path
from typing import List, NamedTuple, Tuple


# Blender's Object.type enum value for cameras (DNA_object_types.h, OB_CAMERA).
_OB_CAMERA = 11


# Maps R_IMF_IMTYPE_* enum int (DNA_scene_types.h) to the corresponding
# `bpy.types.ImageFormatSettings.file_format` enum identifier. Using the bpy
# names (rather than the raw R_IMF_IMTYPE_ macro names) means the value can be
# round-tripped: read here, displayed in the GUI, then assigned back to
# `scene.render.image_settings.file_format` on the worker without translation.
_IMTYPE_NAMES = {
    0: "TARGA",
    1: "IRIS",
    4: "JPEG",                  # R_IMF_IMTYPE_JPEG90
    7: "IRIZ",
    14: "TARGA_RAW",            # R_IMF_IMTYPE_RAWTGA
    15: "AVI_RAW",              # R_IMF_IMTYPE_AVIRAW
    16: "AVI_JPEG",             # R_IMF_IMTYPE_AVIJPEG
    17: "PNG",
    20: "BMP",
    21: "HDR",                  # R_IMF_IMTYPE_RADHDR
    22: "TIFF",
    23: "OPEN_EXR",             # R_IMF_IMTYPE_OPENEXR
    24: "FFMPEG",
    26: "CINEON",
    27: "DPX",
    28: "OPEN_EXR_MULTILAYER",  # R_IMF_IMTYPE_OPENEXR_MULTILAYER
    29: "DDS",
    30: "JPEG2000",             # R_IMF_IMTYPE_JP2
    31: "H264",
    32: "XVID",
    33: "THEORA",
    34: "PSD",
    35: "WEBP",
    36: "AV1",
    255: "INVALID",
}


def _imtype_name(value: int) -> str:
    return _IMTYPE_NAMES.get(value, f"UNKNOWN({value})")


def available_output_formats() -> List[str]:
    """Return all known R_IMF_IMTYPE_* names (sorted, INVALID excluded)."""
    return sorted(set(_IMTYPE_NAMES.values()) - {"INVALID"})


class BlendFileInfo(NamedTuple):
    version: str            # e.g. "4.5"
    subversion: int         # internal SDNA file-format subversion (e.g. 87)
    start_frame: int
    end_frame: int
    renderer: str           # RenderData.engine, e.g. "CYCLES" / "BLENDER_EEVEE_NEXT"
    output_path: str        # RenderData.pic, e.g. "//render/frame_####" (Blender path)
    output_format: str      # mapped from RenderData.im_format.imtype, e.g. "PNG"
    cameras: List[str]      # camera object names (Object.id.name[2:] where type==CAMERA)
    view_layers: List[str]  # ViewLayer names from the first Scene's view_layers ListBase


# Standard C ABI alignment for the primitive types Blender uses in SDNA.
_PRIMITIVE_ALIGN = {
    "char": 1, "uchar": 1, "int8_t": 1, "uint8_t": 1,
    "short": 2, "ushort": 2, "int16_t": 2, "uint16_t": 2,
    "int": 4, "uint": 4, "long": 4, "ulong": 4, "float": 4,
    "int32_t": 4, "uint32_t": 4,
    "double": 8, "int64_t": 8, "uint64_t": 8,
    "void": 1,
}


def _parse_field_name(name: str):
    """Parse an SDNA field name into (base, pointer_levels, [array_dims], is_func_ptr)."""
    # Function pointer:  "(*name)(args)" with optional trailing array dims.
    m = re.match(r"^\(\*+\s*(\w+)\s*\)\s*\([^)]*\)((?:\[\d+\])*)$", name)
    if m:
        dims = [int(d) for d in re.findall(r"\[(\d+)\]", m.group(2) or "")]
        return m.group(1), 1, dims, True
    n_ptr = 0
    rest = name
    while rest.startswith("*"):
        n_ptr += 1
        rest = rest[1:]
    dims = []
    arr_match = re.search(r"((?:\[\d+\])+)$", rest)
    if arr_match:
        dims = [int(d) for d in re.findall(r"\[(\d+)\]", arr_match.group(1))]
        rest = rest[: arr_match.start()]
    return rest, n_ptr, dims, False


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _open_decompressed(path: Path):
    """Return a binary file-like object yielding the (decompressed) .blend bytes.

    Caller is responsible for closing the returned object.
    """
    f = open(path, "rb")
    magic = f.read(4)
    f.seek(0)
    if magic[:2] == b"\x1f\x8b":
        return gzip.open(f, "rb")
    if magic == _ZSTD_MAGIC:
        try:
            import zstandard  # PyPI; works on Python 3.12+
        except ImportError as e:  # pragma: no cover
            f.close()
            raise RuntimeError(
                "This .blend file is Zstd-compressed; the `zstandard` "
                "package is required to read it. "
                "Install with `pip install zstandard`."
            ) from e
        return zstandard.open(f, "rb")
    return f


def _read_blend_bytes(path: Path) -> bytes:
    with _open_decompressed(path) as f:
        return f.read()


def _format_version(raw: str) -> str:
    """Convert a Blender file-version code (3 or 4 ASCII digits) to a display string.

    Blender encodes the version as ``major * 100 + minor``, so the digits
    in the file header convert to a "X.Y" form by integer split.

    Examples (3-digit, legacy header):
        "405" -> "4.5"   (Blender 4.5)
        "281" -> "2.81"  (Blender 2.81)
        "300" -> "3.0"   (Blender 3.0)

    Examples (4-digit, new 5.0+ header):
        "0501" -> "5.1"  (Blender 5.1)
        "0500" -> "5.0"
        "1000" -> "10.0" (hypothetical)
    """
    if len(raw) not in (3, 4) or not raw.isdigit():
        raise ValueError(f"Unexpected Blender version code: {raw!r}")
    major, minor = divmod(int(raw), 100)
    return f"{major}.{minor}"


class _FileFormat(NamedTuple):
    """Layout parameters for parsing a .blend file.

    `header_size` is where the first file-block begins. `block_header_size`
    + the offset/format fields together describe the BHead struct used by
    the file's Blender version (the layout grew from 24→32 bytes in 5.0
    when block lengths were widened to 64-bit).
    """
    ptr_size: int           # 4 or 8
    endian: str             # struct format prefix: "<" or ">"
    version: str            # display string e.g. "4.5", "5.1"
    header_size: int        # 12 (legacy) or 17 (5.0+)
    block_header_size: int  # 20 / 24 (legacy) or 32 (5.0+)
    length_offset: int      # within block header, where the data length lives
    length_fmt_char: str    # "i" (32-bit length) or "q" (64-bit, 5.0+)
    oldaddr_offset: int     # within block header, where the original ptr lives
    sdna_idx_offset: int    # within block header, where the SDNA index lives


def _parse_file_format(data: bytes) -> _FileFormat:
    """Parse the .blend file header and return layout parameters.

    Supports both header generations:

    Legacy (12 bytes, Blender ≤ 4.x):
        BLENDER<ptr><endian><3-digit version>           ptr ∈ {'_','-'}
        Block header: 16 + ptr_size bytes, length is 32-bit.

    New (17 bytes, Blender 5.0+):
        BLENDER17<ptr>01<endian><4-digit version>       ptr ∈ {'_','-'}
        Block header: 32 bytes, length is 64-bit (supports >2 GB blocks).
        Big-endian was dropped in 5.0; we still accept 'V' for forward
        compatibility but the file header itself only carries 'v' today.
    """
    if len(data) < 12 or data[:7] != b"BLENDER":
        raise ValueError("Not a Blender file (magic mismatch)")

    b7 = data[7:8]

    if b7 in (b"_", b"-"):
        ptr_size = 4 if b7 == b"_" else 8
        endian = _decode_endian_flag(data[8:9])
        version = _format_version(data[9:12].decode("ascii"))
        return _FileFormat(
            ptr_size=ptr_size,
            endian=endian,
            version=version,
            header_size=12,
            block_header_size=16 + ptr_size,
            length_offset=4,
            length_fmt_char="i",
            oldaddr_offset=8,
            sdna_idx_offset=8 + ptr_size,
        )

    if data[7:9] == b"17":
        if len(data) < 17:
            raise ValueError(
                f"Truncated Blender 5.0+ header (need 17 bytes, got {len(data)})"
            )
        ptr_byte = data[9:10]
        if ptr_byte == b"_":
            ptr_size = 4
        elif ptr_byte == b"-":
            ptr_size = 8
        else:
            raise ValueError(f"Unknown pointer-size flag {ptr_byte!r}")
        if data[10:12] != b"01":
            raise ValueError(
                f"Unsupported new-header schema version {data[10:12]!r}"
            )
        endian = _decode_endian_flag(data[12:13])
        version = _format_version(data[13:17].decode("ascii"))
        return _FileFormat(
            ptr_size=ptr_size,
            endian=endian,
            version=version,
            header_size=17,
            block_header_size=32,
            length_offset=16,
            length_fmt_char="q",
            oldaddr_offset=8,
            sdna_idx_offset=24,
        )

    raise ValueError(
        f"Unrecognised Blender header byte at offset 7: {bytes(b7)!r}"
    )


def _decode_endian_flag(b: bytes) -> str:
    if b == b"v":
        return "<"
    if b == b"V":
        return ">"
    raise ValueError(f"Unknown endianness flag {b!r}")


def read_blender_version(blend_path) -> str:
    """Return the Blender version that wrote the file, e.g. ``"4.5"`` or ``"5.1"``.

    Reads only the file header (after decompression), so this is cheap even
    for very large .blend files. 17 bytes is enough for both legacy and
    5.0+ headers.
    """
    blend_path = Path(blend_path)
    with _open_decompressed(blend_path) as f:
        header = f.read(17)
    return _parse_file_format(header).version


def read_blendfile_info(blend_path) -> BlendFileInfo:
    """Return version, subversion, and frame range for the first Scene.

    Single-pass: decompresses the .blend once and reads everything from it.
    Raises ValueError if the file is not a recognizable .blend file or the
    expected DNA structures cannot be located.
    """
    blend_path = Path(blend_path)
    data = _read_blend_bytes(blend_path)

    fmt = _parse_file_format(data)
    ptr_size = fmt.ptr_size
    endian = fmt.endian
    version = fmt.version

    # ---- Walk file blocks ----
    block_header_size = fmt.block_header_size
    addr_fmt = endian + ("Q" if ptr_size == 8 else "I")
    length_fmt = endian + fmt.length_fmt_char
    sdna_fmt = endian + "i"
    pos = fmt.header_size
    sdna_block: bytes | None = None
    scene_blocks: list[tuple[int, bytes]] = []
    glob_blocks: list[tuple[int, bytes]] = []
    object_blocks: list[tuple[int, bytes]] = []
    addr_to_block: dict[int, tuple[int, bytes]] = {}

    while pos + block_header_size <= len(data):
        code = data[pos:pos + 4].rstrip(b"\x00").decode("ascii", errors="replace")
        length = struct.unpack_from(length_fmt, data, pos + fmt.length_offset)[0]
        oldaddr = struct.unpack_from(addr_fmt, data, pos + fmt.oldaddr_offset)[0]
        sdna_idx = struct.unpack_from(sdna_fmt, data, pos + fmt.sdna_idx_offset)[0]
        body_start = pos + block_header_size
        body = data[body_start:body_start + length]
        if code == "ENDB":
            break
        if code == "DNA1":
            sdna_block = body
        else:
            if oldaddr:
                addr_to_block[oldaddr] = (sdna_idx, body)
            if code == "SC":
                scene_blocks.append((sdna_idx, body))
            elif code == "GLOB":
                glob_blocks.append((sdna_idx, body))
            elif code == "OB":
                object_blocks.append((sdna_idx, body))
        pos = body_start + length

    if sdna_block is None:
        raise ValueError("DNA1 (SDNA) block not found")
    if not scene_blocks:
        raise ValueError("No Scene (SC) block found")

    # ---- Parse SDNA ----
    sd = sdna_block
    spos = 0

    def _expect(tag: bytes) -> None:
        nonlocal spos
        if sd[spos:spos + 4] != tag:
            raise ValueError(
                f"Expected {tag!r} at SDNA offset {spos}, got {sd[spos:spos + 4]!r}"
            )
        spos += 4

    def _read_int() -> int:
        nonlocal spos
        v = struct.unpack_from(endian + "i", sd, spos)[0]
        spos += 4
        return v

    def _align4() -> None:
        nonlocal spos
        spos = (spos + 3) & ~3

    _expect(b"SDNA")
    _expect(b"NAME")
    n_names = _read_int()
    names: list[str] = []
    for _ in range(n_names):
        end = sd.index(b"\x00", spos)
        names.append(sd[spos:end].decode("ascii", errors="replace"))
        spos = end + 1
    _align4()

    _expect(b"TYPE")
    n_types = _read_int()
    types: list[str] = []
    for _ in range(n_types):
        end = sd.index(b"\x00", spos)
        types.append(sd[spos:end].decode("ascii", errors="replace"))
        spos = end + 1
    _align4()

    _expect(b"TLEN")
    type_sizes = list(struct.unpack_from(endian + "h" * n_types, sd, spos))
    spos += 2 * n_types
    _align4()

    _expect(b"STRC")
    n_structs = _read_int()
    structs: list[tuple[int, list[tuple[int, int]]]] = []
    for _ in range(n_structs):
        type_idx = struct.unpack_from(endian + "h", sd, spos)[0]
        n_fields = struct.unpack_from(endian + "h", sd, spos + 2)[0]
        spos += 4
        fields: list[tuple[int, int]] = []
        for _ in range(n_fields):
            ft = struct.unpack_from(endian + "h", sd, spos)[0]
            fn = struct.unpack_from(endian + "h", sd, spos + 2)[0]
            spos += 4
            fields.append((ft, fn))
        structs.append((type_idx, fields))

    struct_index_by_type_name = {types[t]: i for i, (t, _) in enumerate(structs)}

    align_cache: dict[int, int] = {}

    def type_alignment(type_idx: int) -> int:
        tname = types[type_idx]
        if tname in _PRIMITIVE_ALIGN:
            return _PRIMITIVE_ALIGN[tname]
        if tname not in struct_index_by_type_name:
            return max(1, type_sizes[type_idx])
        s_idx = struct_index_by_type_name[tname]
        if s_idx in align_cache:
            return align_cache[s_idx]
        align_cache[s_idx] = 1  # break recursion
        max_a = 1
        _, fs = structs[s_idx]
        for ft, fn in fs:
            _, n_ptr, _, is_func = _parse_field_name(names[fn])
            a = ptr_size if (n_ptr > 0 or is_func) else type_alignment(ft)
            if a > max_a:
                max_a = a
        align_cache[s_idx] = max_a
        return max_a

    def compute_field_offsets(struct_idx: int) -> dict[str, int]:
        _, fs = structs[struct_idx]
        offset = 0
        offsets: dict[str, int] = {}
        for ft, fn in fs:
            base, n_ptr, dims, is_func = _parse_field_name(names[fn])
            if n_ptr > 0 or is_func:
                size = ptr_size
                align = ptr_size
            else:
                size = type_sizes[ft]
                align = type_alignment(ft)
            if dims:
                count = 1
                for d in dims:
                    count *= d
                size *= count
            offset = (offset + align - 1) & ~(align - 1)
            offsets[base] = offset
            offset += size
        return offsets

    if "Scene" not in struct_index_by_type_name:
        raise ValueError("Scene struct not found in SDNA")
    if "RenderData" not in struct_index_by_type_name:
        raise ValueError("RenderData struct not found in SDNA")

    scene_struct_idx = struct_index_by_type_name["Scene"]
    render_struct_idx = struct_index_by_type_name["RenderData"]

    scene_offsets = compute_field_offsets(scene_struct_idx)
    render_offsets = compute_field_offsets(render_struct_idx)

    if "r" not in scene_offsets:
        raise ValueError("Scene.r (RenderData) field not found")
    if "sfra" not in render_offsets or "efra" not in render_offsets:
        raise ValueError("RenderData.sfra / RenderData.efra not found")

    sfra_off = scene_offsets["r"] + render_offsets["sfra"]
    efra_off = scene_offsets["r"] + render_offsets["efra"]
    engine_off = (
        scene_offsets["r"] + render_offsets["engine"]
        if "engine" in render_offsets
        else None
    )
    pic_off = (
        scene_offsets["r"] + render_offsets["pic"]
        if "pic" in render_offsets
        else None
    )

    # ImageFormatData.imtype lives inside RenderData.im_format (nested struct).
    imtype_off = None
    if "im_format" in render_offsets:
        if_idx = struct_index_by_type_name.get("ImageFormatData")
        if if_idx is not None:
            if_offsets = compute_field_offsets(if_idx)
            if "imtype" in if_offsets:
                imtype_off = (
                    scene_offsets["r"]
                    + render_offsets["im_format"]
                    + if_offsets["imtype"]
                )

    # The block code (SC / GLOB / OB) already identifies the struct type.
    # Through Blender 4.x, sdna_idx on these typed blocks also pointed at the
    # same struct, but Blender 5.0+ writes a sentinel (1) for typed blocks and
    # only stores meaningful sdna_idx on anonymous DATA blocks. Trust the code.
    sfra = efra = None
    renderer = ""
    output_path = ""
    output_format = ""
    for _sdna_idx, body in scene_blocks:
        sfra = struct.unpack_from(endian + "i", body, sfra_off)[0]
        efra = struct.unpack_from(endian + "i", body, efra_off)[0]
        if engine_off is not None:
            # RenderData.engine is a fixed-size char array (null-terminated).
            raw = body[engine_off : engine_off + 32]
            renderer = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        if pic_off is not None:
            # RenderData.pic is FILE_MAX (1024) bytes, null-terminated.
            raw = body[pic_off : pic_off + 1024]
            output_path = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        if imtype_off is not None:
            # ImageFormatData.imtype is a single signed/unsigned char.
            imtype = struct.unpack_from(endian + "B", body, imtype_off)[0]
            output_format = _imtype_name(imtype)
        break
    if sfra is None:
        raise ValueError("No Scene (SC) block found")

    # ---- FileGlobal.subversion (from GLOB block) ----
    subversion = -1
    fg_idx = struct_index_by_type_name.get("FileGlobal")
    if fg_idx is not None and glob_blocks:
        fg_offsets = compute_field_offsets(fg_idx)
        if "subversion" in fg_offsets:
            _sdna_idx, body = glob_blocks[0]
            subversion = struct.unpack_from(
                endian + "h", body, fg_offsets["subversion"]
            )[0]

    # ---- Camera object names (Object.id.name where Object.type == OB_CAMERA) ----
    cameras: list[str] = []
    object_struct_idx = struct_index_by_type_name.get("Object")
    id_struct_idx = struct_index_by_type_name.get("ID")
    if object_struct_idx is not None and id_struct_idx is not None:
        object_offsets = compute_field_offsets(object_struct_idx)
        id_offsets = compute_field_offsets(id_struct_idx)
        # ID.name was char[66] through Blender 4.x; Blender 5.0 widened it to
        # char[258] (release notes: "max ID name length increased to 255").
        # Read the actual array size from SDNA so the same code handles both.
        id_name_size = 66
        for ft, fn in structs[id_struct_idx][1]:
            base, n_ptr, dims, is_func = _parse_field_name(names[fn])
            if base == "name" and not n_ptr and not is_func and dims:
                id_name_size = dims[0]
                break
        if (
            "id" in object_offsets
            and "type" in object_offsets
            and "name" in id_offsets
        ):
            type_off = object_offsets["type"]
            name_off = object_offsets["id"] + id_offsets["name"]
            for _sdna_idx, body in object_blocks:
                ob_type = struct.unpack_from(endian + "h", body, type_off)[0]
                if ob_type != _OB_CAMERA:
                    continue
                # id.name format: 2-char ID type prefix ("OB"), then null-terminated name.
                raw = body[name_off + 2 : name_off + id_name_size]
                cameras.append(raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace"))

    # ---- View layer names (walk Scene.view_layers ListBase) ----
    view_layers: list[str] = []
    vl_struct_idx = struct_index_by_type_name.get("ViewLayer")
    if (
        vl_struct_idx is not None
        and "view_layers" in scene_offsets
    ):
        vl_offsets = compute_field_offsets(vl_struct_idx)
        # ViewLayer.name is char[64] through 5.1; read the actual array size
        # from SDNA so the slice keeps working if Blender widens it later.
        vl_name_size = 64
        for ft, fn in structs[vl_struct_idx][1]:
            base, n_ptr, dims, is_func = _parse_field_name(names[fn])
            if base == "name" and not n_ptr and not is_func and dims:
                vl_name_size = dims[0]
                break
        if "next" in vl_offsets and "name" in vl_offsets and scene_blocks:
            # ListBase = (first*, last*) — read first pointer and follow ViewLayer.next.
            _sdna_idx, body = scene_blocks[0]
            addr = struct.unpack_from(addr_fmt, body, scene_offsets["view_layers"])[0]
            while addr:
                block = addr_to_block.get(addr)
                if block is None:
                    break
                _, vl_body = block
                name_bytes = vl_body[vl_offsets["name"] : vl_offsets["name"] + vl_name_size]
                view_layers.append(
                    name_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
                )
                addr = struct.unpack_from(addr_fmt, vl_body, vl_offsets["next"])[0]

    return BlendFileInfo(
        version=version,
        subversion=subversion,
        start_frame=sfra,
        end_frame=efra,
        renderer=renderer,
        output_path=output_path,
        output_format=output_format,
        cameras=cameras,
        view_layers=view_layers,
    )


def read_frame_range(blend_path) -> Tuple[int, int]:
    """Return (start_frame, end_frame) for the first Scene in the .blend file."""
    info = read_blendfile_info(blend_path)
    return info.start_frame, info.end_frame


def read_blender_subversion(blend_path) -> int:
    """Return the .blend file's internal SDNA subversion (e.g. 87)."""
    return read_blendfile_info(blend_path).subversion


# --------------------------------------------------------------------------
# External file references (textures, linked .blends, sounds, etc.)
# --------------------------------------------------------------------------
class ExternalRef(NamedTuple):
    referrer: Path      # which .blend referenced this
    type: str           # "Image" / "Library" / "bSound" / ...
    raw: str            # raw path as stored in the .blend (may start with "//")
    resolved: Path      # absolute path resolved against `referrer`
    exists: bool        # whether `resolved` actually exists on disk


# (datablock_code, struct_name, field-name candidates) — try filepath first,
# fall back to the legacy `name` field which older datablocks (Image, Library)
# still use as their on-disk path.
_EXTERNAL_RECIPES = [
    ("IM", "Image",     ("filepath", "name")),
    ("LI", "Library",   ("filepath", "name")),
    ("SO", "bSound",    ("filepath", "name")),
    ("CF", "CacheFile", ("filepath", "name")),
    ("MC", "MovieClip", ("filepath", "name")),
    ("VF", "VFont",     ("filepath", "name")),
]


# Fields that indicate the asset is packed *inside* the .blend file rather
# than living on disk: Blender's splash scenes (and any user "File → External
# Data → Pack Resources" workflow) keep the original on-disk path on the
# datablock for reference, but the bytes are embedded — so the path should
# NOT be reported as a missing external ref.
#
# `packedfile` is a `PackedFile *` (legacy / single-file datablocks).
# `packedfiles` is a `ListBase` for UDIM/tiled images; its first 8/4 bytes
# are the `first*` pointer, so reading a single ptr_size at the field offset
# tells us whether the list is non-empty — same check as a plain pointer.
_PACKED_INDICATOR_FIELDS = {
    "Image":  ("packedfile", "packedfiles"),
    "bSound": ("packedfile",),
    "VFont":  ("packedfile",),
}


def _resolve_blend_path(raw: str, referrer: Path) -> Path:
    """Resolve a Blender path. `//` prefix means relative to the .blend dir."""
    if raw.startswith("//"):
        return (referrer.parent / raw[2:]).resolve()
    return Path(raw).resolve()


def _read_external_refs_from(blend_path: Path) -> List[Tuple[str, str]]:
    """Return [(struct_name, raw_path), ...] for one .blend file. Empty paths skipped."""
    data = _read_blend_bytes(blend_path)
    fmt = _parse_file_format(data)
    ptr_size = fmt.ptr_size
    endian = fmt.endian
    block_header_size = fmt.block_header_size
    length_fmt = endian + fmt.length_fmt_char
    sdna_fmt = endian + "i"

    target_codes = {c for c, _, _ in _EXTERNAL_RECIPES}
    per_code: dict[str, list[tuple[int, bytes]]] = {c: [] for c in target_codes}
    sdna_block: bytes | None = None
    pos = fmt.header_size
    while pos + block_header_size <= len(data):
        code = data[pos:pos + 4].rstrip(b"\x00").decode("ascii", errors="replace")
        length = struct.unpack_from(length_fmt, data, pos + fmt.length_offset)[0]
        sdna_idx = struct.unpack_from(sdna_fmt, data, pos + fmt.sdna_idx_offset)[0]
        body_start = pos + block_header_size
        body = data[body_start:body_start + length]
        if code == "ENDB":
            break
        if code == "DNA1":
            sdna_block = body
        elif code in target_codes:
            per_code[code].append((sdna_idx, body))
        pos = body_start + length

    if sdna_block is None:
        return []

    sd = sdna_block
    spos = [0]

    def expect(t: bytes) -> None:
        if sd[spos[0]:spos[0] + 4] != t:
            raise ValueError(
                f"Expected {t!r} at SDNA offset {spos[0]}, got {sd[spos[0]:spos[0] + 4]!r}"
            )
        spos[0] += 4

    def rint() -> int:
        v = struct.unpack_from(endian + "i", sd, spos[0])[0]
        spos[0] += 4
        return v

    def a4() -> None:
        spos[0] = (spos[0] + 3) & ~3

    expect(b"SDNA"); expect(b"NAME"); n_names = rint()
    names: list[str] = []
    for _ in range(n_names):
        e = sd.index(b"\x00", spos[0])
        names.append(sd[spos[0]:e].decode("ascii", errors="replace"))
        spos[0] = e + 1
    a4()
    expect(b"TYPE"); n_types = rint()
    types: list[str] = []
    for _ in range(n_types):
        e = sd.index(b"\x00", spos[0])
        types.append(sd[spos[0]:e].decode("ascii", errors="replace"))
        spos[0] = e + 1
    a4()
    expect(b"TLEN")
    type_sizes = list(struct.unpack_from(endian + "h" * n_types, sd, spos[0]))
    spos[0] += 2 * n_types
    a4()
    expect(b"STRC"); n_structs = rint()
    structs: list[tuple[int, list[tuple[int, int]]]] = []
    for _ in range(n_structs):
        type_idx = struct.unpack_from(endian + "h", sd, spos[0])[0]
        n_fields = struct.unpack_from(endian + "h", sd, spos[0] + 2)[0]
        spos[0] += 4
        fs: list[tuple[int, int]] = []
        for _ in range(n_fields):
            ft = struct.unpack_from(endian + "h", sd, spos[0])[0]
            fn = struct.unpack_from(endian + "h", sd, spos[0] + 2)[0]
            spos[0] += 4
            fs.append((ft, fn))
        structs.append((type_idx, fs))

    sname_to_sidx = {types[t]: i for i, (t, _) in enumerate(structs)}
    align_cache: dict[int, int] = {}

    def type_alignment(ti: int) -> int:
        tname = types[ti]
        if tname in _PRIMITIVE_ALIGN:
            return _PRIMITIVE_ALIGN[tname]
        if tname not in sname_to_sidx:
            return max(1, type_sizes[ti])
        sidx = sname_to_sidx[tname]
        if sidx in align_cache:
            return align_cache[sidx]
        align_cache[sidx] = 1
        mx = 1
        for ft, fn in structs[sidx][1]:
            _, n_ptr, _, isf = _parse_field_name(names[fn])
            a = ptr_size if (n_ptr > 0 or isf) else type_alignment(ft)
            if a > mx:
                mx = a
        align_cache[sidx] = mx
        return mx

    def field_offsets(struct_idx: int) -> dict[str, tuple[int, int]]:
        o = 0
        out: dict[str, tuple[int, int]] = {}
        for ft, fn in structs[struct_idx][1]:
            base, n_ptr, dims, isf = _parse_field_name(names[fn])
            if n_ptr > 0 or isf:
                size, align = ptr_size, ptr_size
            else:
                size = type_sizes[ft]
                align = type_alignment(ft)
            if dims:
                c = 1
                for d in dims:
                    c *= d
                size *= c
            o = (o + align - 1) & ~(align - 1)
            out[base] = (o, size)
            o += size
        return out

    addr_fmt = endian + ("Q" if ptr_size == 8 else "I")

    refs: list[tuple[str, str]] = []
    for code, struct_name, field_candidates in _EXTERNAL_RECIPES:
        blocks = per_code.get(code, [])
        if not blocks or struct_name not in sname_to_sidx:
            continue
        offs = field_offsets(sname_to_sidx[struct_name])
        field = next((f for f in field_candidates if f in offs), None)
        if field is None:
            continue
        fp_off, fp_size = offs[field]
        packed_ptr_offs = [
            offs[pf][0]
            for pf in _PACKED_INDICATOR_FIELDS.get(struct_name, ())
            if pf in offs
        ]
        for _, body in blocks:
            if any(
                struct.unpack_from(addr_fmt, body, off)[0]
                for off in packed_ptr_offs
            ):
                continue
            raw = body[fp_off:fp_off + fp_size]
            path = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            if path:
                refs.append((struct_name, path))
    return refs


def collect_external_refs(root_blend_path) -> List[ExternalRef]:
    """Recursively collect all external file refs reachable from `root_blend_path`.

    Walks `Library` (linked .blend) refs and recurses into them, deduplicating
    by absolute resolved path. Each returned `ExternalRef` carries an `exists`
    flag — refs whose resolved path isn't on disk are still included so the
    caller can warn about them.
    """
    root = Path(root_blend_path).resolve()
    visited: set[Path] = {root}
    queue: list[Path] = [root]
    refs: list[ExternalRef] = []

    while queue:
        current = queue.pop()
        if not current.is_file():
            # Linked .blend missing — record the link itself as missing later;
            # we can't recurse into it.
            continue
        try:
            local = _read_external_refs_from(current)
        except Exception:
            # Unreadable .blend (corrupt, unsupported compression, etc.).
            continue
        for struct_name, raw in local:
            resolved = _resolve_blend_path(raw, current)
            refs.append(
                ExternalRef(
                    referrer=current,
                    type=struct_name,
                    raw=raw,
                    resolved=resolved,
                    exists=resolved.is_file(),
                )
            )
            if struct_name == "Library" and resolved.suffix.lower() == ".blend":
                if resolved not in visited:
                    visited.add(resolved)
                    queue.append(resolved)
    return refs


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} path/to/file.blend [--refs]", file=sys.stderr)
        sys.exit(2)
    info = read_blendfile_info(sys.argv[1])
    print(
        f"version={info.version} subversion={info.subversion} "
        f"start={info.start_frame} end={info.end_frame}"
    )
    print(f"renderer={info.renderer}")
    print(f"output_path={info.output_path!r}")
    print(f"output_format={info.output_format}")
    print(f"cameras={info.cameras}")
    print(f"view_layers={info.view_layers}")
    if "--refs" in sys.argv[2:]:
        refs = collect_external_refs(sys.argv[1])
        present = sum(1 for r in refs if r.exists)
        missing = len(refs) - present
        print(f"external_refs: {len(refs)} total / {missing} missing")
        for r in sorted(set((r.type, r.resolved, r.exists) for r in refs)):
            mark = "OK" if r[2] else "MISSING"
            print(f"  [{mark:7}] {r[0]:11} {r[1]}")
