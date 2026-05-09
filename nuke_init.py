"""PyDeadlineCloudSubmitter-supplied Nuke init.py — runs at Nuke startup on the worker.

Loaded via the worker's `NUKE_PATH` environment variable (set by the OpenJD
job script in `template_nuke.yaml`). It registers an `addOnScriptLoad`
callback that does two things after the user's .nk script finishes loading:

1. **Remap Read-like nodes' `file` knobs** using the OpenJD / Deadline
   Cloud path-mapping rules file. Job Attachments uploads the original
   Windows / submitter-host paths but the worker sees them under
   ``$OPENJD_SESSION_DIR/files/...`` — without remapping, every Read
   would fail with a "missing input" error. The rules file location is
   passed via ``DEADLINE_PATH_MAPPING_RULES_FILE`` (the variable used by
   Deadline Cloud) or ``OPENJD_PATH_MAPPING_RULES_FILE`` (the OpenJD
   spec name). Either form is accepted.

2. **Retarget Write nodes' `file` knobs** to live under the Job
   Attachments output directory, so rendered files end up where they
   get synced back to S3. Mapping rule:

       <PYSUBMIT_OUTPUT_DIR>/<WriteNodeName>/<original_basename>

   The original directory portion of the Write path is dropped — only the
   basename (which contains the frame pattern, e.g. ``comp.%04d.exr``) is
   preserved. This mirrors the Blender side, which subdivides outputs by
   camera / view-layer name.

Both rewrites are best-effort: a single bad knob logs a warning and the
render continues with the rest.
"""
from __future__ import annotations

import json
import os

import nuke


# Read-like node classes whose `file` knob points at an input on disk.
# Kept in sync with `nukescript._FILE_REF_KNOBS` on the submitter side.
_INPUT_FILE_NODE_CLASSES = (
    "Read", "DeepRead", "ReadGeo", "ReadGeo2",
    "Camera", "Camera2", "Camera3",
    "Axis", "Axis2", "Axis3",
    "Light",
    "OCIOFileTransform",
    "AudioRead",
)
# Vectorfield uses a different knob name.
_INPUT_FILE_NODE_CLASSES_VFIELD = ("Vectorfield",)


# Env var names known to point at the OpenJD / Deadline path-mapping rules
# JSON file. We probe in order; the first one that exists wins. Different
# Deadline / openjd-sessions versions have used different names over time,
# so we cast a wide net.
_PATH_MAPPING_ENV_VARS = (
    "DEADLINE_PATH_MAPPING_RULES_FILE",
    "OPENJD_PATH_MAPPING_RULES_FILE",
    "OPENJD_PATH_MAPPING_FILE",
)


def _derive_mapping_from_scene_file():
    """Derive a (source_prefix, destination_prefix) rule from the SceneFile pair.

    The worker bash exports both:
      * ``PYSUBMIT_SCENE_FILE_SOURCE`` — the original submitter path (e.g.
        ``C:\\projects\\shot010\\comp.nk``)
      * ``PYSUBMIT_SCENE_FILE_DEST``   — the worker-side remapped path
        (e.g. ``/sessions/.../assetroot-XXX/projects/shot010/comp.nk``)

    Their longest common path-token suffix is the relative path inside the
    asset root; everything before it on each side is the prefix. The
    resulting rule remaps any Read knob value that begins with the source
    prefix into the asset root.

    Comparison is case-insensitive (the source is a Windows path).
    Returns ``None`` if either env var is missing or the paths share no
    suffix at all (which would mean Job Attachments uploaded to a layout
    that this heuristic can't reverse-engineer).
    """
    src = os.environ.get("PYSUBMIT_SCENE_FILE_SOURCE")
    dst = os.environ.get("PYSUBMIT_SCENE_FILE_DEST")
    if not src or not dst:
        return None
    src_parts = [p for p in src.replace("\\", "/").split("/") if p]
    dst_parts = [p for p in dst.split("/") if p]
    common = 0
    while (
        common < len(src_parts)
        and common < len(dst_parts)
        and src_parts[-(common + 1)].lower() == dst_parts[-(common + 1)].lower()
    ):
        common += 1
    if common == 0:
        print(
            "[PySubmit] WARNING: cannot derive mapping — no common suffix "
            "between {!r} and {!r}".format(src, dst)
        )
        return None
    # Source prefix preserves the leading drive letter (e.g. "C:") if
    # present. Destination prefix gets a leading "/" because dst is
    # absolute on the worker (Linux).
    src_prefix = "/".join(src_parts[: len(src_parts) - common])
    dst_prefix = "/" + "/".join(dst_parts[: len(dst_parts) - common])
    return src_prefix, dst_prefix


def _load_path_mapping_rules():
    """Return a list of (source_path, destination_path) tuples or [].

    Tries three sources in order:
    1. An explicit OpenJD / Deadline path-mapping rules JSON pointed at
       by one of ``_PATH_MAPPING_ENV_VARS`` (only if the file exists).
    2. A JSON discovered by scanning ``$OPENJD_SESSION_DIR``.
    3. The SceneFile-derived fallback (see ``_derive_mapping_from_scene_file``).

    Sources 1 & 2 may yield multiple rules; source 3 yields at most one.
    All sources are merged into a single rule list, sorted longest-source-
    first so more specific rules win.
    """
    rules = []
    rules_file = None
    for var in _PATH_MAPPING_ENV_VARS:
        val = os.environ.get(var)
        if val and os.path.isfile(val):
            rules_file = val
            print("[PySubmit] using path-mapping rules from ${} = {}".format(var, val))
            break

    if rules_file is None:
        # Fallback: scan the session dir for anything that looks like a
        # path-mapping JSON. Useful when the env var isn't set but the
        # worker still drops the rules file somewhere predictable.
        session_dir = os.environ.get("OPENJD_SESSION_DIR")
        if session_dir and os.path.isdir(session_dir):
            for root, _dirs, files in os.walk(session_dir):
                for name in files:
                    lower = name.lower()
                    if lower.endswith(".json") and "path" in lower and "mapping" in lower:
                        rules_file = os.path.join(root, name)
                        print(
                            "[PySubmit] discovered path-mapping rules at {} "
                            "(no explicit env var set)".format(rules_file)
                        )
                        break
                if rules_file:
                    break

    if rules_file is not None:
        try:
            with open(rules_file) as f:
                data = json.load(f)
            raw_rules = data.get("path_mapping_rules") or data.get("rules") or []
            for r in raw_rules:
                src = r.get("source_path") or r.get("source")
                dst = r.get("destination_path") or r.get("destination")
                if not src or not dst:
                    continue
                src_norm = src.replace("\\", "/").rstrip("/")
                dst_norm = dst.rstrip("/").rstrip("\\")
                rules.append((src_norm, dst_norm))
        except (OSError, ValueError) as exc:
            print(
                "[PySubmit] WARNING: failed to read path mapping rules: {!r}".format(
                    exc
                )
            )

    derived = _derive_mapping_from_scene_file()
    if derived is not None:
        # Only add the derived rule if no explicit rule already covers it.
        if not any(s.lower() == derived[0].lower() for s, _ in rules):
            rules.append(derived)
            print(
                "[PySubmit] derived SceneFile-based rule: {!r} -> {!r}".format(
                    *derived
                )
            )

    if not rules:
        candidates = sorted(
            "{}={}".format(k, v)
            for k, v in os.environ.items()
            if "PATH_MAPPING" in k.upper()
            or "OPENJD" in k.upper()
            or "DEADLINE" in k.upper()
            or k.startswith("PYSUBMIT_")
        )
        print(
            "[PySubmit] WARNING: no path-mapping rules and SceneFile "
            "derivation failed. Read paths will NOT be remapped. "
            "Relevant env: {}".format(candidates or "(none)")
        )
        return []

    rules.sort(key=lambda t: len(t[0]), reverse=True)
    print("[PySubmit] active path-mapping rule(s): {}".format(len(rules)))
    for src, dst in rules:
        print("    {!r} -> {!r}".format(src, dst))
    return rules


def _apply_path_mapping(value, rules):
    """Apply path-mapping rules to a Nuke file knob value.

    Source paths are matched case-insensitively (Windows source roots are
    case-insensitive on disk). Returns the remapped value, or the original
    if no rule matched.
    """
    if not value or not rules:
        return value
    norm = value.replace("\\", "/")
    norm_lower = norm.lower()
    for src, dst in rules:
        src_lower = src.lower()
        if norm_lower == src_lower or norm_lower.startswith(src_lower + "/"):
            return dst + norm[len(src):]
    return value


def _retarget_writes_for_pysubmit():
    out_dir = os.environ.get("PYSUBMIT_OUTPUT_DIR")
    if not out_dir:
        return
    for node in nuke.allNodes("Write", recurseGroups=True):
        try:
            file_knob = node.knob("file")
            if file_knob is None:
                continue
            # value() resolves Tcl/Python expressions to a final string;
            # if the knob is empty there's nothing to retarget.
            original = file_knob.value()
            if not original:
                continue
            basename = os.path.basename(original)
            target = os.path.join(out_dir, node.name(), basename).replace("\\", "/")
            target_dir = os.path.dirname(target)
            if target_dir:
                try:
                    os.makedirs(target_dir)
                except OSError:
                    pass  # already exists, or a sibling task got there first
            file_knob.setValue(target)
            print(
                "[PySubmit] retargeted Write '{}': {} -> {}".format(
                    node.name(), original, target
                )
            )
        except Exception as exc:
            print(
                "[PySubmit] WARNING: failed to retarget Write '{}': {!r}".format(
                    node.name(), exc
                )
            )


def _remap_inputs_for_pysubmit():
    rules = _load_path_mapping_rules()
    targets = []  # (node, knob_name) pairs we'll inspect
    for cls in _INPUT_FILE_NODE_CLASSES:
        for node in nuke.allNodes(cls, recurseGroups=True):
            targets.append((node, "file"))
    for cls in _INPUT_FILE_NODE_CLASSES_VFIELD:
        for node in nuke.allNodes(cls, recurseGroups=True):
            targets.append((node, "vfield_file"))
    print(
        "[PySubmit] inspecting {} input-file knob(s) for remapping".format(len(targets))
    )
    for node, knob_name in targets:
        _remap_node_knob(node, knob_name, rules)


def _remap_node_knob(node, knob_name, rules):
    try:
        knob = node.knob(knob_name)
        if knob is None:
            return
        original = knob.value()
        if not original:
            return
        remapped = _apply_path_mapping(original, rules) if rules else original
        if remapped != original:
            knob.setValue(remapped)
            print(
                "[PySubmit] remapped {} '{}' {}: {} -> {}".format(
                    node.Class(), node.name(), knob_name, original, remapped
                )
            )
        else:
            # Logged so the user can see WHY a Read failed: was it because
            # no rule matched, or because there were no rules at all?
            print(
                "[PySubmit] kept {} '{}' {} as-is: {}".format(
                    node.Class(), node.name(), knob_name, original
                )
            )
    except Exception as exc:
        print(
            "[PySubmit] WARNING: failed to remap {} '{}' {}: {!r}".format(
                node.Class(), node.name(), knob_name, exc
            )
        )


def _on_script_load():
    # Inputs first so the script is in a valid state before we change
    # outputs. Failures in either step are non-fatal (logged, not raised)
    # so a single bad node doesn't kill the whole render.
    _remap_inputs_for_pysubmit()
    _retarget_writes_for_pysubmit()


nuke.addOnScriptLoad(_on_script_load)
