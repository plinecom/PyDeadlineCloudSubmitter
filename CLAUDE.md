# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
# GUI (primary entry point)
python gui.py

# CLI (debug only — most submission logic now lives in gui.py)
python main.py
```

The project targets **Python 3.12 or newer** (PyCharm config and `.venv/` both point at 3.12; boto3 has dropped support for older Python versions, so 3.12 is the floor). Annotated modules still use `from __future__ import annotations` because PEP 563 was deferred — the future import is still meaningful in 3.12+. Expects a local venv at `.venv/`. There is no test suite or lint config.

Runtime dependencies are listed in [`requirements.txt`](requirements.txt) — install with `.venv\Scripts\pip install -r requirements.txt`. Summary:

- `deadline` (AWS Deadline Cloud client — `deadline.client.api`, transitively pulls `boto3` / `botocore`)
- `PySide6` ≥ 6.4 (GUI toolkit; the 6.4 floor is required by `QFormLayout.setRowVisible`, used to swap Blender/Nuke rows)
- `pyqtdarktheme` (optional dark theme; the GUI falls back gracefully if missing)
- `PyYAML` (already installed transitively via `deadline`; used to write `asset_references.yaml`)
- `zstandard` (PyPI) for reading modern Zstd-compressed `.blend` files. Python 3.14 also has `compression.zstd` in the stdlib, but we use `zstandard` unconditionally so a single code path covers 3.12 → 3.14+.

## Architecture overview

A **Deadline Cloud job submitter for Blender and NukeX renders.** A single GUI dispatches per scene-type, with separate parsers and OpenJD templates per app:

| File | Role |
|---|---|
| [`gui.py`](gui.py) | PySide6 GUI: file picker, history, per-file render settings, Submit. Holds the bulk of submission logic; dispatches to the right parser / template based on file extension. |
| [`main.py`](main.py) | Minimal CLI entry kept for debugging. |
| [`blendfile.py`](blendfile.py) | Pure-Python `.blend` parser (no Blender dep). Reads scene metadata + recursively walks linked-file references. |
| [`nukescript.py`](nukescript.py) | Pure-Python `.nk` script parser (no Nuke dep). Reads version/frame range/format/Write nodes + expands Read sequence patterns. |
| [`template_blender.yaml`](template_blender.yaml) | OpenJD `jobtemplate-2023-09` for Blender renders. |
| [`template_nuke.yaml`](template_nuke.yaml) | OpenJD `jobtemplate-2023-09` for NukeX renders. |
| [`nuke_init.py`](nuke_init.py) | Worker-side Nuke `init.py` (delivered as a `PATH/IN` parameter, staged into `NUKE_PATH`). Retargets every Write node's `file` knob to live under the job's `OutputDir` so Job Attachments can sync renders back. |

### `gui.py` — main flow

VFX-style layout (Farm/Queue bar at top, file picker, three-pane split: History | Scene Info | Render Settings, log at bottom):

```
+-------------------------------------------------------+
| Farm: [...▼]   Queue: [...▼]              [Refresh]   |
| Scene File: [path]                       [Browse...]  |
+----------+----------------+--------------------------+
| History  | Scene Info     | Render Settings          |
| (queue)  | (read-only)    | (editable, per-entry)    |
|          |                | [   Submit   ] [Progress]|
+----------+----------------+--------------------------+
| Log                                                   |
+-------------------------------------------------------+
```

Key concepts:

- **`HistoryEntry` (dataclass)** — one entry per dropped scene file (`.blend` or `.nk`). Holds:
  - `scene_type` discriminator (`SCENE_TYPE_BLENDER` / `SCENE_TYPE_NUKE`)
  - the parsed `info` (`BlendFileInfo` or `NukeFileInfo` — read-only metadata)
  - `external_refs: List[ExternalRef]` (Blender: recursively walked Library/Image/sound refs; Nuke: Read patterns expanded into actual files)
  - **common editable settings**: `start_frame`, `end_frame`, `frames_per_task`, `farm_id`, `queue_id`, `priority`
  - **Blender-only**: `camera`, `view_layer`, `output_path`, `output_format`
  - **Nuke-only**: `write_node` (empty = render all enabled), `views` (comma-separated, empty = script default)
  - **submission record**: `last_submitted_at`, `last_job_id`
  - The same file can be dropped multiple times → multiple independent entries (no dedup), so users can submit the same scene with different settings.
  - Constructed via `HistoryEntry.from_blender(path, info)` or `HistoryEntry.from_nuke(path, info)`.

- **`SubmitWorker(QObject)`** — runs `create_job_from_job_bundle()` on a `QThread`. Signals (`log`, `progress(stage, %)`, `succeeded(job_id)`, `failed(message, is_auth_error)`) bridge back to the GUI. Hashing/upload progress comes from `hashing_progress_callback` / `upload_progress_callback`.

- **`_prepare_bundle_with_assets(scene_type, extra_files)`** — *every submit creates a fresh temp bundle dir* under `tempfile.mkdtemp()`. It contains a copy of the per-app template (`template_blender.yaml` or `template_nuke.yaml`) renamed to `template.yaml` (the name deadline-client expects), plus a freshly-generated `asset_references.yaml` listing the entry's external refs. Cleaned up via `thread.finished` → `shutil.rmtree`. Never modifies the source files.

- **`_compute_output_path(blend_path, camera, view_layer)`** — Blender-only. Generates the default `OutputPath` from the file stem + selected camera/layer. Updated on Camera/View Layer combo changes (handler: `_refresh_output_path_default`). Nuke entries don't use this — Write paths are retargeted at render time by `nuke_init.py`.

- **Panel visibility** — `SceneInfoPanel` and `SubmissionPanel` host both Blender and Nuke widgets; rows are toggled with `QFormLayout.setRowVisible()` based on the active entry's `scene_type`. Requires PySide6 ≥ 6.4.

### `blendfile.py` — `.blend` parsing

Pure-Python implementation, no Blender dep. Two public entry points:

- **`read_blendfile_info(path) -> BlendFileInfo`** — reads version/subversion, frame range, renderer, default output path/format, camera names, view-layer names. Single-pass; decompresses Zstd/gzip on the fly.

- **`collect_external_refs(path) -> List[ExternalRef]`** — recursively walks Library refs, deduplicating by resolved path. Each `ExternalRef` has `referrer / type / raw / resolved / exists`. Used to build `asset_references.yaml`.

Internal mechanics:
- Walks file blocks (`SC` / `GLOB` / `OB` / `IM` / `LI` / `SO` / `CF` / `MC` / `VF` codes), maps `oldaddr → block` for pointer following.
- Parses the `DNA1` block to learn struct layouts at runtime; field offsets are computed dynamically using the SDNA + standard C ABI alignment, so the parser tracks Blender version drift automatically.
- For Blender's image-format enum (`R_IMF_IMTYPE_*`), the int-to-name map (`_IMTYPE_NAMES`) deliberately uses **`bpy.types.ImageFormatSettings.file_format` enum identifiers** (e.g. `OPEN_EXR`, not the SDNA macro `OPENEXR`) so the value round-trips between display and `scene.render.image_settings.file_format = ...`.
- For `Image` / `Library` etc., the path field is sometimes named `name` (legacy SDNA) rather than `filepath`. The parser tries both.

### `nukescript.py` — `.nk` parsing

Pure-Python implementation, no Nuke dep. `.nk` is plain text in a Tcl-like syntax — a brace-balanced parser walks every block.

- **`read_nukescript_info(path) -> NukeFileInfo`** — extracts the `version` line, the first `Root { ... }` block (frame range, fps, format, views), and every `Write { ... }` block (name, file, file_type, channels, views, disable). Recurses through brace nesting so Writes inside Groups are found.

- **`collect_external_refs(path) -> List[ExternalRef]`** — walks Read-like nodes (`Read`, `DeepRead`, `ReadGeo*`, `Camera*`, `Axis*`, `Light`, `OCIOFileTransform`, `Vectorfield`, `AudioRead`) and resolves their `file` knob against the script's parent directory. Sequence patterns (`%04d` / `####`) are expanded by globbing the parent dir and matching with a regex; each matched file becomes one `ExternalRef`. Unresolvable values (Tcl `[python ...]` expressions, `$ENV` vars) are recorded as `exists=False`. Reuses the `ExternalRef` NamedTuple from `blendfile.py` so the bundle prep code is shared.

### `template_blender.yaml` — Blender OpenJD job template

`jobtemplate-2023-09` with `TASK_CHUNKING` extension. **Job parameters** are wired from `gui.py`:

| Parameter | Type | Source |
|---|---|---|
| `SceneFile` | `PATH` / `objectType: FILE` / `dataFlow: IN` | `entry.path` (the local .blend; uploaded via Job Attachments) |
| `OutputDir` | `PATH` / `objectType: DIRECTORY` / `dataFlow: OUT` | template default `./outputs`; the worker writes here, Job Attachments syncs it back |
| `Frames` | `STRING` | `f"{start}-{end}"` |
| `FramesPerTask` | `INT` | per-entry value (passed into `chunks.defaultTaskCount` via `{{Param.X}}` substitution) |
| `Camera` | `STRING` | empty = scene default; otherwise applied via `--python-expr` |
| `ViewLayer` | `STRING` | empty = render all layers; otherwise applied via `--python-expr` |
| `OutputFormat` | `STRING` | bpy enum identifier (`PNG` / `OPEN_EXR` / ...) |
| `OutputPath` | `STRING` | full Blender pic value (e.g. `//CAM-A/Layer1/splash_CAM-A_Layer1_####`) |

Worker bash flow (inside the embedded `Run` script):

1. Strip `//` (or `/`) prefix from `OutputPath`, re-root under `OutputDir` so Job Attachments sees the rendered files. `mkdir -p $(dirname FULL_OUT)` so subdirectories exist.
2. If any of `Camera` / `ViewLayer` / `OutputFormat` is non-empty, build a single multi-line `--python-expr` (heredoc’d into `EXTRA_ARGS`) that sets `scene.camera` / `scene.view_layers` active layer / `scene.render.image_settings.file_format`. Wrapped in `try/except` so a failure here only warns.
3. `blender -b "$SCENE_FILE" --enable-autoexec "${EXTRA_ARGS[@]}" -o "$FULL_OUT" -s "$START" -e "$END" -a`

`--enable-autoexec` is required because production scenes (e.g. Blender Studio splashes) drive properties via PyDriver expressions and load addons (e.g. cloudrig.py). Without it, `BPY_driver_exec: restricted access disallows ...` errors break camera animation.

### `template_nuke.yaml` + `nuke_init.py` — NukeX OpenJD job template

`jobtemplate-2023-09` with `TASK_CHUNKING` extension. **Job parameters**:

| Parameter | Type | Source |
|---|---|---|
| `SceneFile` | `PATH` / `objectType: FILE` / `dataFlow: IN` | `entry.path` (the local .nk; uploaded via Job Attachments) |
| `NukeInit` | `PATH` / `objectType: FILE` / `dataFlow: IN` | `JOB_BUNDLE_DIR / "nuke_init.py"` (uploaded so the worker can stage it as `init.py` under `NUKE_PATH`) |
| `OutputDir` | `PATH` / `objectType: DIRECTORY` / `dataFlow: OUT` | template default `./outputs` |
| `Frames` | `STRING` | `f"{start}-{end}"` |
| `FramesPerTask` | `INT` | per-entry value |
| `WriteNode` | `STRING` | empty = render every enabled Write; otherwise passed as `-X <name>` |
| `Views` | `STRING` | comma-separated list, empty = script default; passed as `--view <list>` |

Worker bash flow:

1. `mktemp -d` a fresh dir, `cp $NukeInit $TMPDIR/init.py`, prepend `$TMPDIR` to `NUKE_PATH`. Nuke loads files literally named `init.py`, so the file (delivered as `nuke_init.py` in the project root to keep imports clean) must be staged under that name.
2. Export `PYSUBMIT_OUTPUT_DIR=$OutputDir`. The init.py registers a single `addOnScriptLoad` callback that runs two passes once Nuke finishes loading the user's script:
   - **Inputs** — read the OpenJD/Deadline path-mapping rules JSON (env var `DEADLINE_PATH_MAPPING_RULES_FILE` or `OPENJD_PATH_MAPPING_RULES_FILE`) and rewrite every Read-like node's `file` knob (Read, DeepRead, ReadGeo*, Camera*, Axis*, Light, OCIOFileTransform, AudioRead, Vectorfield) so the absolute submitter-side path becomes the worker-side session path. Case-insensitive prefix match, longest rule first.
   - **Outputs** — rewrite every Write node's `file` knob to `<PYSUBMIT_OUTPUT_DIR>/<WriteNodeName>/<original_basename>` (mirrors the Blender side which subdivides by camera / view-layer).
   The user's `.nk` is never edited — only the in-memory state is mutated.
3. `nuke --nukex -x -F "$START-$END" [-X $WriteNode] [--view $Views] "$SCENE_FILE"`

`-x` is non-interactive render mode (no GUI); `--nukex` requests a NukeX render license. The submitter is Windows; the worker bash runs on **Linux** fleets in the configured queue.

## Auth

The GUI does **not** ship its own login flow. It relies entirely on boto3's default credential provider chain (env vars → `~/.aws/credentials` → `~/.aws/config` profile → SSO cache → container metadata → EC2 instance metadata, in that order). Pick whichever path fits the host:

- **Workstation / Windows with Deadline Cloud Monitor** — DCM writes a profile with `credential_process` pointing at itself; boto3 resolves through DCM transparently.
- **Linux on EC2** — attach an IAM instance profile with `AWSDeadlineCloudUserAccessFarms` / `Queues` / `Jobs` (or equivalent) to the instance. The default credential chain finds it. Region defaults to `us-east-1` if `~/.aws/config` doesn't set one (see `_ensure_aws_region_default`).
- **Anywhere else** — set `AWS_PROFILE`, run `aws sso login --profile <name>`, or paste keys into `~/.aws/credentials`. Same chain applies.

Conventions:

- `gui.py` calls `deadline.client.api.list_farms()` / `list_queues()` (the **high-level wrappers**, not raw `client.list_farms()`) so the `principalId` filter is added — required for some Deadline Cloud user roles to be authorized.
- Farm / Queue selection is per-`HistoryEntry`. Submission overrides via an in-memory `config_file.read_config()` ConfigParser passed to `create_job_from_job_bundle(..., config=config)`. The user's persisted `~/.deadline/config` is never written to.
- On startup the GUI logs `boto3.client("sts").get_caller_identity()` (`_resolve_aws_identity`) so it's obvious which IAM principal is in use — this catches surprises like an EC2 worker role being picked instead of an expected SSO role.
- On `CredentialRetrievalError` (or any submit-time AccessDenied), the GUI shows a copyable error dialog with the full traceback. The user is expected to fix credentials externally and click Refresh.

## Known platform / farm gotchas

- **TASK_CHUNKING is declared in the templates but currently doesn't function on this farm** — every frame becomes its own task regardless of `defaultTaskCount`. The chunks block is left in place so it'll work once the farm is fixed.
- **Deadline Cloud Monitor's bundled CLI 0.52.1 has a bug** where downloads return "no output files available" even when files exist. Workaround: tell users to update Monitor (newer client 0.55.1+ works), or use the venv's CLI directly (`.venv\Scripts\deadline.exe job download-output ...`).
- **`bpy.context.window` is `None` in `--background` mode** on some Blender versions — use `bpy.context.window_manager.windows[0]` instead. The view-layer setup python-expr does both as a fallback.
- **Blender has no `--scene-camera` CLI flag** despite some out-of-date docs — set the active camera via `scene.camera = bpy.data.objects.get(name)` in a `--python-expr`.
- **Nuke `init.py` filename is fixed** — Nuke loads only files literally named `init.py` from each `NUKE_PATH` entry. We ship the script as `nuke_init.py` in the project root (so it doesn't shadow anything at import time) and the worker bash stages it as `init.py` in a fresh `mktemp -d` dir before invoking Nuke.
- **Nuke Read sequence expansion is glob-based** — `nukescript.collect_external_refs` resolves `%04d` / `####` patterns by listing the parent directory and regex-matching filenames. If the parent dir has unrelated files matching the same pattern they'll be uploaded too. Patterns containing Tcl/Python expressions (`[python ...]`, `[getenv ...]`) or shell vars (`$FOO`) can't be statically resolved and are recorded as missing.
- Workers may be RAM-constrained; large Blender Studio scenes (`gold-splash_screen.blend` reaches ~12GB peak) can OOM-kill the render. That's a fleet config issue, not a submitter bug.

## Conventions

- **Pull request titles and bodies are written in Japanese.** (See user feedback memory.)
- Add new project files freely — `JOB_BUNDLE_DIR = Path(__file__).parent` is *no longer* uploaded directly. The temp bundle prep (`_prepare_bundle_with_assets`) only copies the chosen `template_*.yaml`, so other files in the project root are not part of the Job Attachments payload. Asset uploads are driven entirely by the dynamically generated `asset_references.yaml`, plus the explicit PATH/IN parameters (`SceneFile`, and `NukeInit` for Nuke jobs).
- `.gitignore` exists and already excludes `*.blend`, `__pycache__/`, `.venv/`, `.idea/`, `outputs/`, `.claude/settings.local.json`. Adding more locally-only files? Update `.gitignore`.
