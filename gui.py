"""PyDeadlineCloudSubmitter GUI — Deadline Cloud submitter for Blender + Nuke.

Layout (VFX-style):

    +-------------------------------------------------------------------+
    |  Scene File: [active path......................]   [Browse...]   |
    +------------+-------------------+----------------------------------+
    |  History   |  Scene Info       |  Render Settings                 |
    |  --------  |  (read-only)      |  (editable, per-entry)           |
    |  splash    |                   |                                  |
    |  comp_010  |                   |                                  |
    |  shot011*  |                   |                                  |
    |            |                   |                                  |
    |            |                   |  [   Submit   ]                  |
    +------------+-------------------+----------------------------------+
    |  Log                                                              |
    +-------------------------------------------------------------------+

Each history entry stores both the parsed scene metadata (BlendFileInfo or
NukeFileInfo — immutable source-of-truth) AND the user's edited render
settings. Clicking an entry restores those settings; submitting uses them.

Requirements (install into the project venv):
    pip install PySide6 pyqtdarktheme

Run:
    python gui.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

import yaml

import deadline
import deadline.client.api
from deadline.client.config import config_file
from botocore.exceptions import CredentialRetrievalError
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

import blendfile
import nukescript
from blendfile import (
    BlendFileInfo,
    ExternalRef,
    available_output_formats,
)
from nukescript import NukeFileInfo


# The job bundle source dir holds the per-app templates and any worker-side
# helper scripts (e.g. nuke_init.py) — same dir as gui.py.
JOB_BUNDLE_DIR = Path(__file__).parent

# Scene-type discriminators. These match the lowercase suffix of the file
# extension and are stored on each HistoryEntry.
SCENE_TYPE_BLENDER = "blender"
SCENE_TYPE_NUKE = "nuke"


_FALLBACK_AWS_REGION = "us-east-1"


def _scene_type_for(path: str) -> Optional[str]:
    """Return SCENE_TYPE_* for a path, or None if the extension is unknown."""
    p = path.lower()
    if p.endswith(".blend"):
        return SCENE_TYPE_BLENDER
    if p.endswith(".nk"):
        return SCENE_TYPE_NUKE
    return None


def _ensure_aws_region_default() -> Optional[str]:
    """If no AWS region is resolvable, default to us-east-1 via env var.

    Fresh Linux/macOS installs commonly have the SSO cache populated but no
    ``~/.aws/config`` (Deadline Cloud Monitor is Windows-only and is what
    normally writes that file), so boto3 can't determine a region and every
    deadline API call fails with NoRegionError. Force a default so the GUI
    starts up usable, and return a warning string the caller can surface.
    """
    import botocore.session
    try:
        region = botocore.session.Session().get_config_variable("region")
    except Exception:
        region = None
    if region:
        return None
    os.environ["AWS_DEFAULT_REGION"] = _FALLBACK_AWS_REGION
    return (
        f"AWS region not configured — defaulting to {_FALLBACK_AWS_REGION}. "
        "Set [default] region in ~/.aws/config to choose a different region."
    )


def _resolve_aws_identity() -> str:
    """Best-effort sts:GetCallerIdentity for the startup log.

    Returns the principal ARN as a string, or a parenthesized error string if
    boto3 can't resolve credentials. Used to make it obvious which IAM
    identity (e.g. EC2 instance role vs. user SSO role) the GUI is about to
    talk to AWS as — a common source of "AccessDenied" surprises.
    """
    import boto3
    try:
        return boto3.client("sts").get_caller_identity().get("Arn", "(unknown)")
    except Exception as e:
        return f"(unresolved: {type(e).__name__}: {e})"


def _prepare_bundle_with_assets(
    scene_type: str,
    extra_input_files: List[Path],
    use_shared_storage: bool = False,
) -> Path:
    """Create a temp bundle dir with the right template.yaml + asset_references.yaml.

    deadline-client reads asset_references.yaml from the bundle dir at submit
    time and uploads listed files to Job Attachments alongside any PATH/IN
    parameters. We write a fresh tmp bundle per submission so concurrent
    submits and per-entry asset sets don't clash.

    The template chosen depends on `scene_type` and `use_shared_storage`.
    Shared-storage variants declare SceneFile as STRING (no upload, no
    remap) and skip the OutputDir Job Attachments OUT directory — the
    submitter and the workers see the same NAS so paths are valid as-is.

    The bundle's template file is always named ``template.yaml`` because
    that's what deadline-client expects.
    """
    bundle = Path(tempfile.mkdtemp(prefix="pysubmit-bundle-"))
    if scene_type == SCENE_TYPE_NUKE:
        template_name = (
            "template_nuke_shared.yaml" if use_shared_storage else "template_nuke.yaml"
        )
    else:
        template_name = (
            "template_blender_shared.yaml"
            if use_shared_storage
            else "template_blender.yaml"
        )
    shutil.copy2(JOB_BUNDLE_DIR / template_name, bundle / "template.yaml")

    # Shared-storage mode: no Job Attachments uploads, so don't emit
    # asset_references.yaml even if the user's external_refs walk found
    # things. The worker resolves them from the same NAS path.
    if extra_input_files and not use_shared_storage:
        ar = {
            "assetReferences": {
                "inputs": {
                    "filenames": sorted({str(p) for p in extra_input_files}),
                    "directories": [],
                },
                "outputs": {"directories": []},
                "referencedPaths": [],
            }
        }
        (bundle / "asset_references.yaml").write_text(
            yaml.safe_dump(ar, sort_keys=False), encoding="utf-8"
        )
    return bundle


def _show_error_dialog(
    parent: QWidget, title: str, message: str, details: str = ""
) -> None:
    """Show an error dialog whose body and traceback are mouse-selectable.

    QMessageBox's default label is not selectable, which makes it hard for
    users to copy server-side error text (boto3 / Deadline AccessDenied
    payloads tend to be long and useful in bug reports). This builds a
    custom dialog with a selectable summary plus a scrollable, copyable
    traceback area when ``details`` is provided.
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumWidth(720)

    layout = QVBoxLayout(dlg)

    summary = QLabel(message)
    summary.setWordWrap(True)
    summary.setTextInteractionFlags(
        Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
    )
    layout.addWidget(summary)

    if details:
        layout.addWidget(QLabel("Details:"))
        details_view = QPlainTextEdit(details)
        details_view.setReadOnly(True)
        details_view.setFont(QFont("Consolas, Menlo, monospace"))
        details_view.setMinimumHeight(260)
        layout.addWidget(details_view)

    btn_row = QHBoxLayout()
    btn_row.addStretch(1)
    copy_btn = QPushButton("Copy to clipboard")
    full_text = message + (("\n\n" + details) if details else "")
    copy_btn.clicked.connect(
        lambda: QApplication.clipboard().setText(full_text)
    )
    btn_row.addWidget(copy_btn)
    close_btn = QPushButton("Close")
    close_btn.setDefault(True)
    close_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)

    dlg.exec()


def _compute_output_path(blend_path: str, camera: str, view_layer: str) -> str:
    """Compute the default render output path (Blender RenderData.pic format).

    The naming convention is:

        camera="" view_layer=""  -> "//<stem>_####"
        camera="A" view_layer="" -> "//A/<stem>_A_####"
        camera="" view_layer="L" -> "//L/<stem>_L_####"
        camera="A" view_layer="L"-> "//A/L/<stem>_A_L_####"

    where ``<stem>`` is the .blend filename without extension. The "//" prefix
    is Blender's convention for "relative to the .blend file directory".
    """
    stem = Path(blend_path).stem
    name_tokens = [stem]
    dir_tokens: list[str] = []
    if camera:
        name_tokens.append(camera)
        dir_tokens.append(camera)
    if view_layer:
        name_tokens.append(view_layer)
        dir_tokens.append(view_layer)
    filename = "_".join(name_tokens) + "_####"
    if dir_tokens:
        return "//" + "/".join(dir_tokens) + "/" + filename
    return "//" + filename


# --------------------------------------------------------------------------
# QThread worker that performs the actual Deadline Cloud submission
# --------------------------------------------------------------------------
class SubmitWorker(QObject):
    """Run create_job_from_job_bundle off the GUI thread.

    Uploads (especially the Job Attachments .blend) can take many seconds,
    so all of it happens here. Progress and log lines are routed back to the
    GUI via signals.
    """

    log = Signal(str)
    progress = Signal(str, float)         # (stage, percent_0_to_100)
    succeeded = Signal(str)               # job_id
    failed = Signal(str, bool, str)       # (message, is_auth_error, details)

    def __init__(
        self,
        *,
        bundle_dir: str,
        job_parameters: list,
        priority: int,
        config,
        name: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._bundle_dir = bundle_dir
        self._job_parameters = job_parameters
        self._priority = priority
        self._config = config
        self._name = name

    def run(self) -> None:
        def on_print(msg: str) -> None:
            self.log.emit(msg)

        def on_hash_progress(metadata) -> bool:
            self.progress.emit("Hashing", float(metadata.progress))
            return True  # continue (False would cancel)

        def on_upload_progress(metadata) -> bool:
            self.progress.emit("Uploading", float(metadata.progress))
            return True

        try:
            response = deadline.client.api.create_job_from_job_bundle(
                job_bundle_dir=self._bundle_dir,
                job_parameters=self._job_parameters,
                priority=self._priority,
                config=self._config,
                name=self._name,
                print_function_callback=on_print,
                hashing_progress_callback=on_hash_progress,
                upload_progress_callback=on_upload_progress,
                interactive_confirmation_callback=lambda message, default: True,
            )
        except CredentialRetrievalError:
            self.failed.emit(
                "AWS credentials are expired.", True, traceback.format_exc()
            )
            return
        except Exception as e:
            self.failed.emit(
                f"{type(e).__name__}: {e}", False, traceback.format_exc()
            )
            return
        self.succeeded.emit(str(response).strip())


# --------------------------------------------------------------------------
# Per-file state held in the History list
# --------------------------------------------------------------------------
@dataclass
class HistoryEntry:
    """One entry per loaded scene file (.blend or .nk).

    The same file can be dropped multiple times → multiple independent
    entries (no dedup), so users can submit the same scene with different
    settings. Per-app fields (camera/view_layer for Blender, write_node/
    views for Nuke) coexist on the same dataclass; only the panel code that
    matches `scene_type` reads them.
    """

    path: str
    scene_type: str                                    # SCENE_TYPE_BLENDER or SCENE_TYPE_NUKE
    info: Union[BlendFileInfo, NukeFileInfo]

    # All external file refs (textures + linked .blends + sounds + Read
    # sequences + ...) reachable from this scene, computed once on file load.
    external_refs: List[ExternalRef] = field(default_factory=list)

    # Editable render settings (initialised from `info`, then user-overridable):
    start_frame: int = 0
    end_frame: int = 0
    frames_per_task: int = 1

    # Blender-only render settings (ignored for Nuke entries):
    camera: str = ""
    view_layer: str = ""
    output_path: str = ""
    output_format: str = ""

    # Nuke-only render settings (ignored for Blender entries):
    write_node: str = ""    # empty = render every enabled Write node
    views: str = ""         # empty = use script's default view set

    # Per-entry submission target (Farm / Queue):
    farm_id: str = ""
    queue_id: str = ""

    # Job priority (Deadline Cloud, 0-100; higher = higher priority).
    priority: int = 50

    # If True, skip Job Attachments entirely: SceneFile and any external
    # refs are passed by path, on the assumption the workers mount the
    # same NAS as the submitter. Selects the *_shared.yaml template variant.
    use_shared_storage: bool = False

    # Submission record:
    last_submitted_at: Optional[datetime] = None
    last_job_id: Optional[str] = None

    @classmethod
    def from_blender(cls, path: str, info: BlendFileInfo) -> "HistoryEntry":
        # camera / view_layer left empty by default — render uses the scene's
        # existing settings unless the user picks something explicitly.
        return cls(
            path=path,
            scene_type=SCENE_TYPE_BLENDER,
            info=info,
            start_frame=info.start_frame,
            end_frame=info.end_frame,
            output_path=_compute_output_path(path, "", ""),
            output_format=info.output_format,
        )

    @classmethod
    def from_nuke(cls, path: str, info: NukeFileInfo) -> "HistoryEntry":
        # write_node left empty by default — Nuke -x without -X renders every
        # enabled Write node.
        return cls(
            path=path,
            scene_type=SCENE_TYPE_NUKE,
            info=info,
            start_frame=info.start_frame,
            end_frame=info.end_frame,
        )

    @property
    def display_name(self) -> str:
        return Path(self.path).name

    @property
    def job_name(self) -> str:
        """Job name shown in DeadlineCloud Monitor.

        Always starts with the scene filename (with extension); for Blender
        entries, appends ``_<camera>`` and/or ``_<view_layer>`` when the
        user has chosen specific values, so multiple submissions of the
        same .blend with different camera/layer settings can be told
        apart in the queue.
        """
        base = Path(self.path).name
        if self.scene_type == SCENE_TYPE_BLENDER:
            suffix = "_".join(p for p in (self.camera, self.view_layer) if p)
            if suffix:
                return f"{base}_{suffix}"
        return base

    @property
    def status_marker(self) -> str:
        return "✓" if self.last_job_id else " "


# --------------------------------------------------------------------------
# Top: Farm / Queue selector
# --------------------------------------------------------------------------
class FarmQueueBar(QFrame):
    """Top bar to choose which Deadline Cloud farm/queue to submit into.

    Populated lazily via `refresh()` (which calls list_farms / list_queues).
    On auth failure the dropdowns simply stay empty; the Submit flow handles
    re-authentication.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        mono = QFont("Consolas, Menlo, monospace")

        self.farm_combo = QComboBox()
        self.farm_combo.setFont(mono)
        self.farm_combo.setMinimumWidth(280)
        self.farm_combo.currentIndexChanged.connect(self._on_farm_changed)

        self.queue_combo = QComboBox()
        self.queue_combo.setFont(mono)
        self.queue_combo.setMinimumWidth(280)

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.addWidget(QLabel("Farm:"))
        layout.addWidget(self.farm_combo, stretch=1)
        layout.addSpacing(12)
        layout.addWidget(QLabel("Queue:"))
        layout.addWidget(self.queue_combo, stretch=1)
        layout.addSpacing(12)
        layout.addWidget(self.refresh_btn)

        # farm_id -> [queue dict, ...]
        self._queues_cache: dict[str, list[dict]] = {}

    def refresh(self) -> Optional[str]:
        """Re-fetch farms (and the selected farm's queues). Returns error message or None.

        Uses `deadline.client.api.list_farms` (rather than raw boto3) because
        the high-level wrapper automatically passes `principalId=<current user>`,
        which is what the Monitor user role is permitted to call. A direct
        `client.list_farms()` would hit AccessDenied for that role.
        """
        prev_farm = self.farm_combo.currentData()
        prev_queue = self.queue_combo.currentData()
        self._queues_cache.clear()

        try:
            farms = deadline.client.api.list_farms().get("farms", [])
        except Exception as e:
            self.farm_combo.blockSignals(True)
            self.queue_combo.blockSignals(True)
            self.farm_combo.clear()
            self.queue_combo.clear()
            self.farm_combo.blockSignals(False)
            self.queue_combo.blockSignals(False)
            return f"{type(e).__name__}: {e}"

        self.farm_combo.blockSignals(True)
        self.farm_combo.clear()
        for f in farms:
            label = f.get("displayName") or f["farmId"]
            self.farm_combo.addItem(f"{label}  ({f['farmId']})", userData=f["farmId"])
        if prev_farm:
            idx = self.farm_combo.findData(prev_farm)
            if idx >= 0:
                self.farm_combo.setCurrentIndex(idx)
        self.farm_combo.blockSignals(False)

        # Populate queues for the (possibly newly selected) active farm
        self._on_farm_changed(self.farm_combo.currentIndex())
        if prev_queue:
            idx = self.queue_combo.findData(prev_queue)
            if idx >= 0:
                self.queue_combo.setCurrentIndex(idx)
        return None

    def _on_farm_changed(self, _index: int) -> None:
        farm_id = self.farm_combo.currentData()
        self.queue_combo.blockSignals(True)
        self.queue_combo.clear()
        if not farm_id:
            self.queue_combo.blockSignals(False)
            return

        if farm_id not in self._queues_cache:
            try:
                queues = deadline.client.api.list_queues(farmId=farm_id).get(
                    "queues", []
                )
            except Exception:
                queues = []
            self._queues_cache[farm_id] = queues

        for q in self._queues_cache[farm_id]:
            label = q.get("displayName") or q["queueId"]
            self.queue_combo.addItem(f"{label}  ({q['queueId']})", userData=q["queueId"])
        self.queue_combo.blockSignals(False)

    def selected_farm_id(self) -> Optional[str]:
        return self.farm_combo.currentData()

    def selected_queue_id(self) -> Optional[str]:
        return self.queue_combo.currentData()

    def set_selection(self, farm_id: str, queue_id: str) -> None:
        """Restore dropdown selections to the given farm/queue if present.

        Used when switching the active history entry — keeps the bar in sync
        with the per-entry stored values. If a value is empty or not in the
        current dropdown lists, that combo's selection is cleared.
        """
        # ---- Farm ----
        self.farm_combo.blockSignals(True)
        if farm_id:
            idx = self.farm_combo.findData(farm_id)
            self.farm_combo.setCurrentIndex(idx)  # -1 if not found is fine
        else:
            self.farm_combo.setCurrentIndex(-1)
        self.farm_combo.blockSignals(False)

        # Reload queue list for the (possibly changed) farm.
        self._on_farm_changed(self.farm_combo.currentIndex())

        # ---- Queue ----
        self.queue_combo.blockSignals(True)
        if queue_id:
            idx = self.queue_combo.findData(queue_id)
            self.queue_combo.setCurrentIndex(idx)
        else:
            self.queue_combo.setCurrentIndex(-1)
        self.queue_combo.blockSignals(False)


# --------------------------------------------------------------------------
# Top: file picker + drag/drop target
# --------------------------------------------------------------------------
class FilePickerBar(QFrame):
    fileSelected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumHeight(56)

        mono = QFont("Consolas, Menlo, monospace")
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText(
            "Drop a .blend or .nk file anywhere on this window or click Browse…"
        )
        self.path_edit.setFont(mono)
        self.path_edit.setAcceptDrops(False)  # bubble up to MainWindow

        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._on_browse)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(QLabel("Scene File:"))
        layout.addWidget(self.path_edit, stretch=1)
        layout.addWidget(self.browse_btn)

    def set_path_quiet(self, path: str) -> None:
        """Set the displayed path without re-triggering fileSelected."""
        self.path_edit.setText(path)

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select scene file",
            "",
            "Scene files (*.blend *.nk);;Blender files (*.blend);;Nuke scripts (*.nk)",
        )
        if path:
            self.path_edit.setText(path)
            self.fileSelected.emit(path)


# --------------------------------------------------------------------------
# Left: history of loaded files
# --------------------------------------------------------------------------
class HistoryPanel(QGroupBox):
    """List of loaded .blend files. Newest entry at top.

    Each entry is identified by its row in the list, which corresponds 1:1
    with `MainWindow.entries[row]`.
    """

    rowSelected = Signal(int)

    def __init__(self) -> None:
        super().__init__("History")
        mono = QFont("Consolas, Menlo, monospace")

        self.list = QListWidget()
        self.list.setFont(mono)
        self.list.currentRowChanged.connect(self.rowSelected)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list)

    def insert_top(self, entry: HistoryEntry) -> None:
        item = QListWidgetItem(self._format(entry))
        item.setToolTip(entry.path)
        self.list.insertItem(0, item)

    def update_row(self, row: int, entry: HistoryEntry) -> None:
        item = self.list.item(row)
        if item is not None:
            item.setText(self._format(entry))
            item.setToolTip(entry.path)

    def select(self, row: int) -> None:
        self.list.setCurrentRow(row)

    def current_row(self) -> int:
        return self.list.currentRow()

    @staticmethod
    def _format(entry: HistoryEntry) -> str:
        return f"{entry.status_marker}  {entry.display_name}"


# --------------------------------------------------------------------------
# Middle: read-only metadata
# --------------------------------------------------------------------------
class SceneInfoPanel(QGroupBox):
    """Read-only scene metadata. Rows swap between Blender and Nuke views."""

    def __init__(self) -> None:
        super().__init__("Scene Info")
        mono = QFont("Consolas, Menlo, monospace")

        # Common rows (visible for both Blender and Nuke)
        self.app_lbl = QLabel("—")            # "Blender 4.5" / "NukeX 15.1 v5"
        self.frames_lbl = QLabel("—")
        self.linked_files_lbl = QLabel("—")

        # Blender-only
        self.renderer_lbl = QLabel("—")
        self.output_path_lbl = QLabel("—")
        self.output_format_lbl = QLabel("—")

        # Nuke-only
        self.fps_lbl = QLabel("—")
        self.format_lbl = QLabel("—")          # "1920x1080 (HD_1080)"
        self.views_lbl = QLabel("—")           # "main, left, right"

        for lbl in (
            self.app_lbl, self.frames_lbl, self.linked_files_lbl,
            self.renderer_lbl, self.output_path_lbl, self.output_format_lbl,
            self.fps_lbl, self.format_lbl, self.views_lbl,
        ):
            lbl.setFont(mono)
        self.output_path_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)

        # Multi-line lists: cameras / view-layers (Blender) and write-nodes (Nuke).
        self.cameras_view = QPlainTextEdit()
        self.cameras_view.setReadOnly(True)
        self.cameras_view.setFont(mono)
        self.cameras_view.setMaximumHeight(80)

        self.view_layers_view = QPlainTextEdit()
        self.view_layers_view.setReadOnly(True)
        self.view_layers_view.setFont(mono)
        self.view_layers_view.setMaximumHeight(80)

        self.write_nodes_view = QPlainTextEdit()
        self.write_nodes_view.setReadOnly(True)
        self.write_nodes_view.setFont(mono)
        self.write_nodes_view.setMaximumHeight(120)

        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignRight)
        # Common
        form.addRow("Application:", self.app_lbl)
        form.addRow("Frame range:", self.frames_lbl)
        # Blender-specific
        form.addRow("Renderer:", self.renderer_lbl)
        form.addRow("Output path:", self.output_path_lbl)
        form.addRow("File format:", self.output_format_lbl)
        # Nuke-specific
        form.addRow("FPS:", self.fps_lbl)
        form.addRow("Format:", self.format_lbl)
        form.addRow("Views:", self.views_lbl)
        # Common
        form.addRow("Linked files:", self.linked_files_lbl)
        # Blender-specific lists
        form.addRow("Cameras:", self.cameras_view)
        form.addRow("View Layers:", self.view_layers_view)
        # Nuke-specific list
        form.addRow("Write nodes:", self.write_nodes_view)

        self._form = form
        self._show_for_type(None)  # start with everything hidden

    def _show_for_type(self, scene_type: Optional[str]) -> None:
        """Toggle Blender / Nuke / common rows according to `scene_type`.

        Passing None hides every type-specific row (used for the initial
        empty state).
        """
        is_blender = scene_type == SCENE_TYPE_BLENDER
        is_nuke = scene_type == SCENE_TYPE_NUKE
        for w in (self.renderer_lbl, self.output_path_lbl,
                  self.output_format_lbl, self.cameras_view,
                  self.view_layers_view):
            self._form.setRowVisible(w, is_blender)
        for w in (self.fps_lbl, self.format_lbl, self.views_lbl,
                  self.write_nodes_view):
            self._form.setRowVisible(w, is_nuke)

    def show_info(
        self,
        scene_type: Optional[str],
        info: Union[BlendFileInfo, NukeFileInfo, None],
        external_refs: Optional[List[ExternalRef]] = None,
    ) -> None:
        if info is None:
            self.app_lbl.setText("—")
            self.frames_lbl.setText("—")
            self.linked_files_lbl.setText("—")
            self.linked_files_lbl.setToolTip("")
            self._show_for_type(None)
            return

        self.frames_lbl.setText(f"{info.start_frame} – {info.end_frame}")
        self._show_for_type(scene_type)

        if scene_type == SCENE_TYPE_BLENDER:
            assert isinstance(info, BlendFileInfo)
            self.app_lbl.setText(f"Blender {info.version} (sub {info.subversion})")
            self.renderer_lbl.setText(info.renderer or "—")
            self.output_path_lbl.setText(info.output_path or "—")
            self.output_format_lbl.setText(info.output_format or "—")
            self.cameras_view.setPlainText("\n".join(info.cameras) or "(none)")
            self.view_layers_view.setPlainText(
                "\n".join(info.view_layers) or "(none)"
            )
        else:
            assert isinstance(info, NukeFileInfo)
            ver_parts = [info.version]
            if info.subversion:
                ver_parts.append(info.subversion)
            self.app_lbl.setText("NukeX " + " ".join(ver_parts) if info.version else "NukeX —")
            self.fps_lbl.setText(f"{info.fps:g}" if info.fps else "—")
            fmt = info.format_resolution or "—"
            if info.format_name:
                fmt += f"  ({info.format_name})"
            self.format_lbl.setText(fmt)
            self.views_lbl.setText(", ".join(info.views) or "(default)")
            if info.write_nodes:
                lines = [
                    f"{w.name}  [{w.file_type or '?'}]"
                    + ("  (disabled)" if w.disable else "")
                    + f"\n    {w.file or '(no file)'}"
                    for w in info.write_nodes
                ]
                self.write_nodes_view.setPlainText("\n".join(lines))
            else:
                self.write_nodes_view.setPlainText("(none)")

        # Linked files: deduplicate by resolved path so each unique asset
        # is counted once across the walk.
        if external_refs is None:
            self.linked_files_lbl.setText("—")
            self.linked_files_lbl.setToolTip("")
        else:
            unique = {r.resolved: r.exists for r in external_refs}
            total = len(unique)
            missing = sum(1 for ok in unique.values() if not ok)
            label = f"{total} total"
            if missing:
                label += f"  ⚠ {missing} missing"
            self.linked_files_lbl.setText(label)
            missing_paths = [str(p) for p, ok in unique.items() if not ok]
            if missing_paths:
                tip = "Missing files (will be skipped at upload):\n" + "\n".join(
                    sorted(missing_paths)
                )
            else:
                tip = "All linked files resolved on disk."
            self.linked_files_lbl.setToolTip(tip)


# --------------------------------------------------------------------------
# Right: editable parameters + Submit
# --------------------------------------------------------------------------
class SubmissionPanel(QGroupBox):
    """Editable per-entry render settings.

    Hosts both Blender and Nuke widgets in one form layout; rows are shown
    or hidden based on the active entry's `scene_type`.
    """

    submitRequested = Signal()

    def __init__(self) -> None:
        super().__init__("Render Settings")
        mono = QFont("Consolas, Menlo, monospace")

        # ---- Blender widgets ----
        self.camera_combo = QComboBox()
        self.view_layer_combo = QComboBox()
        # When the user changes Camera or View Layer, recompute the default
        # output path. load_from() blocks signals first so this only fires
        # for genuine user picks.
        self.camera_combo.currentIndexChanged.connect(self._refresh_output_path_default)
        self.view_layer_combo.currentIndexChanged.connect(self._refresh_output_path_default)
        self._current_scene_path: str = ""
        self._current_scene_type: Optional[str] = None

        self.output_path_edit = QLineEdit()
        self.output_path_edit.setFont(mono)
        self.output_path_edit.setPlaceholderText("//render/frame_####  (Blender path)")

        self.format_combo = QComboBox()
        self.format_combo.setEditable(True)  # allow values not in our known list
        for name in available_output_formats():
            self.format_combo.addItem(name)

        # ---- Nuke widgets ----
        self.write_node_combo = QComboBox()  # populated from info.write_nodes
        self.views_edit = QLineEdit()
        self.views_edit.setFont(mono)
        self.views_edit.setPlaceholderText("main,left,right  (empty = script default)")

        # ---- Common widgets ----
        self.start_spin = QSpinBox()
        self.start_spin.setRange(-100000, 1000000)
        self.end_spin = QSpinBox()
        self.end_spin.setRange(-100000, 1000000)
        frame_row = QHBoxLayout()
        frame_row.addWidget(self.start_spin)
        frame_row.addWidget(QLabel("–"))
        frame_row.addWidget(self.end_spin)
        frame_row.addStretch(1)
        frame_widget = QWidget()
        frame_widget.setLayout(frame_row)

        self.frames_per_task_spin = QSpinBox()
        self.frames_per_task_spin.setRange(1, 150)
        self.frames_per_task_spin.setValue(1)

        self.priority_spin = QSpinBox()
        self.priority_spin.setRange(0, 100)
        self.priority_spin.setValue(50)

        # Shared-storage toggle. When checked, SceneFile and external refs
        # are NOT uploaded — the worker is expected to see the same NAS
        # paths as the submitter. Selects the *_shared.yaml template.
        self.shared_storage_check = QCheckBox(
            "Use shared storage (skip Job Attachments)"
        )
        self.shared_storage_check.setToolTip(
            "Workers and the submitter mount the same NAS. The scene file "
            "and external references are referenced by path instead of "
            "being uploaded. Output paths are honored as-is (Blender's "
            "OutputPath / Nuke's Write nodes)."
        )

        self.last_submission_lbl = QLabel("Last submission: —")
        self.last_submission_lbl.setFont(mono)
        self.last_submission_lbl.setStyleSheet("color: #888;")

        self.submit_btn = QPushButton("Submit")
        self.submit_btn.setMinimumHeight(36)
        self.submit_btn.setEnabled(False)
        self.submit_btn.clicked.connect(self.submitRequested)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%v%")
        self.progress_bar.hide()

        # Separator between the per-app "where" section and the common
        # "how" controls. Hidden in Nuke mode where the per-app section
        # is empty (Nuke output paths live on the Write nodes).
        self._sep_blender_only = QFrame()
        self._sep_blender_only.setFrameShape(QFrame.HLine)
        self._sep_blender_only.setFrameShadow(QFrame.Sunken)

        sep_common = QFrame()
        sep_common.setFrameShape(QFrame.HLine)
        sep_common.setFrameShadow(QFrame.Sunken)

        form = QFormLayout(self)
        form.setLabelAlignment(Qt.AlignRight)
        # Per-target selection — Blender and Nuke variants share rows so
        # the visual position stays the same when switching scene types.
        form.addRow("Camera:", self.camera_combo)
        form.addRow("Write Node:", self.write_node_combo)
        form.addRow("View Layer:", self.view_layer_combo)
        form.addRow("Views:", self.views_edit)
        form.addRow("Frames:", frame_widget)
        # Blender-only "where" section.
        form.addRow(self._sep_blender_only)
        form.addRow("Output Path:", self.output_path_edit)
        form.addRow("File Format:", self.format_combo)
        # Common "how" section.
        form.addRow(sep_common)
        form.addRow("Frames per task:", self.frames_per_task_spin)
        form.addRow("Priority:", self.priority_spin)
        form.addRow(self.shared_storage_check)
        form.addRow(self.last_submission_lbl)
        form.addRow(self.submit_btn)
        form.addRow(self.progress_bar)

        self._form = form
        self._show_for_type(None)

    def _show_for_type(self, scene_type: Optional[str]) -> None:
        """Toggle Blender / Nuke / common rows according to `scene_type`."""
        is_blender = scene_type == SCENE_TYPE_BLENDER
        is_nuke = scene_type == SCENE_TYPE_NUKE
        for w in (self.camera_combo, self.view_layer_combo,
                  self.output_path_edit, self.format_combo,
                  self._sep_blender_only):
            self._form.setRowVisible(w, is_blender)
        for w in (self.write_node_combo, self.views_edit):
            self._form.setRowVisible(w, is_nuke)

    # --- per-entry sync ----------------------------------------------------
    _DEFAULT_LABEL = "(default)"
    _ALL_WRITES_LABEL = "(all enabled)"

    def load_from(self, entry: HistoryEntry) -> None:
        """Populate all widgets from `entry` (called when it becomes active).

        For Blender entries: Camera and View Layer combos start with a
        "(default)" item carrying an empty userData; selecting it lets the
        worker use the scene's stored settings.

        For Nuke entries: Write Node combo starts with "(all enabled)"
        which translates to Nuke running every enabled Write node (no -X
        flag).
        """
        self._current_scene_path = entry.path
        self._current_scene_type = entry.scene_type
        self._show_for_type(entry.scene_type)

        if entry.scene_type == SCENE_TYPE_BLENDER:
            self._load_blender(entry)
        else:
            self._load_nuke(entry)

        self.start_spin.setValue(entry.start_frame)
        self.end_spin.setValue(entry.end_frame)
        self.frames_per_task_spin.setValue(entry.frames_per_task)
        self.priority_spin.setValue(entry.priority)
        self.shared_storage_check.setChecked(entry.use_shared_storage)

        if entry.last_job_id and entry.last_submitted_at:
            self.last_submission_lbl.setText(
                f"Last: {entry.last_submitted_at:%Y-%m-%d %H:%M}  "
                f"({entry.last_job_id})"
            )
        else:
            self.last_submission_lbl.setText("Last submission: —")

        self.submit_btn.setEnabled(True)

    def _load_blender(self, entry: HistoryEntry) -> None:
        info: BlendFileInfo = entry.info  # type: ignore[assignment]

        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        self.camera_combo.addItem(self._DEFAULT_LABEL, userData="")
        for name in info.cameras:
            self.camera_combo.addItem(name, userData=name)
        idx = self.camera_combo.findData(entry.camera)
        self.camera_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.camera_combo.blockSignals(False)

        self.view_layer_combo.blockSignals(True)
        self.view_layer_combo.clear()
        self.view_layer_combo.addItem(self._DEFAULT_LABEL, userData="")
        for name in info.view_layers:
            self.view_layer_combo.addItem(name, userData=name)
        idx = self.view_layer_combo.findData(entry.view_layer)
        self.view_layer_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.view_layer_combo.blockSignals(False)

        self.output_path_edit.setText(entry.output_path)
        self.format_combo.blockSignals(True)
        if entry.output_format:
            idx = self.format_combo.findText(entry.output_format)
            if idx < 0:
                self.format_combo.addItem(entry.output_format)
                idx = self.format_combo.findText(entry.output_format)
            self.format_combo.setCurrentIndex(idx)
        self.format_combo.blockSignals(False)

    def _load_nuke(self, entry: HistoryEntry) -> None:
        info: NukeFileInfo = entry.info  # type: ignore[assignment]

        self.write_node_combo.blockSignals(True)
        self.write_node_combo.clear()
        self.write_node_combo.addItem(self._ALL_WRITES_LABEL, userData="")
        for w in info.write_nodes:
            label = w.name + (" (disabled)" if w.disable else "")
            self.write_node_combo.addItem(label, userData=w.name)
        idx = self.write_node_combo.findData(entry.write_node)
        self.write_node_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.write_node_combo.blockSignals(False)

        self.views_edit.setText(entry.views)

    def _refresh_output_path_default(self) -> None:
        """Recompute the default output path from the current camera/view_layer.

        Blender-only. Called when the user picks a Camera or View Layer in
        the combos. Any manual edit the user made to the path is
        overwritten — to keep a custom path, edit it AFTER setting
        camera/view_layer.
        """
        if not self._current_scene_path or self._current_scene_type != SCENE_TYPE_BLENDER:
            return
        cam = self.camera_combo.currentData() or ""
        vl = self.view_layer_combo.currentData() or ""
        self.output_path_edit.setText(_compute_output_path(self._current_scene_path, cam, vl))

    def save_to(self, entry: HistoryEntry) -> None:
        """Copy current widget values back into `entry`."""
        entry.start_frame = self.start_spin.value()
        entry.end_frame = self.end_spin.value()
        entry.frames_per_task = self.frames_per_task_spin.value()
        entry.priority = self.priority_spin.value()
        entry.use_shared_storage = self.shared_storage_check.isChecked()
        if entry.scene_type == SCENE_TYPE_BLENDER:
            entry.camera = self.camera_combo.currentData() or ""
            entry.view_layer = self.view_layer_combo.currentData() or ""
            entry.output_path = self.output_path_edit.text()
            entry.output_format = self.format_combo.currentText().strip()
        else:
            entry.write_node = self.write_node_combo.currentData() or ""
            entry.views = self.views_edit.text().strip()

    def clear(self) -> None:
        self.camera_combo.clear()
        self.view_layer_combo.clear()
        self.write_node_combo.clear()
        self.views_edit.clear()
        self.start_spin.setValue(0)
        self.end_spin.setValue(0)
        self.output_path_edit.clear()
        self.format_combo.setCurrentIndex(-1)
        self.frames_per_task_spin.setValue(1)
        self.priority_spin.setValue(50)
        self.shared_storage_check.setChecked(False)
        self.last_submission_lbl.setText("Last submission: —")
        self.submit_btn.setEnabled(False)
        self.progress_bar.hide()
        self._show_for_type(None)

    # --- progress feedback -------------------------------------------------
    def begin_progress(self) -> None:
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("Starting…")
        self.progress_bar.show()

    def update_progress(self, stage: str, percent: float) -> None:
        self.progress_bar.setValue(int(round(percent)))
        self.progress_bar.setFormat(f"{stage}: %v%")

    def end_progress(self) -> None:
        self.progress_bar.hide()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("PyDeadlineCloudSubmitter")
        self.resize(1200, 720)
        self.setAcceptDrops(True)  # entire window is a drop target

        self.entries: list[HistoryEntry] = []
        self.active_index: Optional[int] = None

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        self.farm_queue_bar = FarmQueueBar()
        outer.addWidget(self.farm_queue_bar)

        self.file_bar = FilePickerBar()
        outer.addWidget(self.file_bar)

        # 3-way splitter: history | scene info | submission
        splitter = QSplitter(Qt.Horizontal)
        self.history = HistoryPanel()
        self.scene_info = SceneInfoPanel()
        self.submission = SubmissionPanel()
        splitter.addWidget(self.history)
        splitter.addWidget(self.scene_info)
        splitter.addWidget(self.submission)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 4)
        splitter.setStretchFactor(2, 6)
        splitter.setSizes([220, 360, 540])
        outer.addWidget(splitter, stretch=1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setFont(QFont("Consolas, Menlo, monospace"))
        self.log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.log.setMinimumHeight(140)
        outer.addWidget(self.log)

        # Stop child text widgets from swallowing URL drops.
        for w in (
            self.log,
            self.scene_info.cameras_view,
            self.scene_info.view_layers_view,
        ):
            w.setAcceptDrops(False)

        # Wiring
        self.file_bar.fileSelected.connect(self._on_file_selected)
        self.history.rowSelected.connect(self._on_history_selected)
        self.submission.submitRequested.connect(self._on_submit)

        self._log(
            "Ready. Drop one or more .blend or .nk files anywhere on this window."
        )

        region_warning = _ensure_aws_region_default()
        if region_warning:
            self._log(f"  WARNING: {region_warning}")

        # Surface the AWS principal up front. If submit later fails with
        # AccessDenied, this line tells the user which role/user was actually
        # in use (often an EC2 instance role they didn't expect).
        self._log(f"  AWS identity: {_resolve_aws_identity()}")

        # Best-effort initial load of farms/queues; relies on whatever
        # credentials boto3's default chain resolved (env vars, ~/.aws/*,
        # EC2 instance metadata). If the call fails the user is expected to
        # fix their credentials externally and click Refresh.
        err = self.farm_queue_bar.refresh()
        if err:
            self._log(f"Could not load farms/queues: {err}")
            self._log(
                "  Fix your AWS credentials (env vars, ~/.aws/credentials, "
                "or EC2 instance role), then click Refresh."
            )
        else:
            self._log(
                f"Loaded {self.farm_queue_bar.farm_combo.count()} farm(s)."
            )

    # ---- Window-wide drag & drop -----------------------------------------
    @staticmethod
    def _scene_paths(e: QDropEvent | QDragEnterEvent) -> list[str]:
        """Return the dropped local paths whose extension is supported."""
        if not e.mimeData().hasUrls():
            return []
        return [
            u.toLocalFile()
            for u in e.mimeData().urls()
            if _scene_type_for(u.toLocalFile()) is not None
        ]

    def dragEnterEvent(self, e: QDragEnterEvent) -> None:
        if self._scene_paths(e):
            e.acceptProposedAction()

    def dragMoveEvent(self, e: QDragEnterEvent) -> None:
        if self._scene_paths(e):
            e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent) -> None:
        paths = self._scene_paths(e)
        if not paths:
            return
        e.acceptProposedAction()
        for path in paths:
            self._on_file_selected(path)

    # ----------------------------------------------------------------------
    def _on_file_selected(self, path: str) -> None:
        # Same file dropped twice? We add a fresh entry instead of switching to
        # the existing one, so the user can submit the same scene multiple times
        # with different settings without overwriting their previous edits.

        # Save current entry's edits before swapping focus.
        self._snapshot_active()

        scene_type = _scene_type_for(path)
        if scene_type is None:
            self._log(f"  ERROR: unsupported file type: {path}")
            return

        self._log(f"Loading {path}")
        try:
            if scene_type == SCENE_TYPE_BLENDER:
                info = blendfile.read_blendfile_info(path)
                entry = HistoryEntry.from_blender(path, info)
            else:
                info = nukescript.read_nukescript_info(path)
                entry = HistoryEntry.from_nuke(path, info)
        except Exception as e:
            self._log(f"  ERROR: {e}")
            return

        # External references: Blender walks linked .blends recursively;
        # Nuke expands Read sequence patterns into actual files. Either may
        # be slow on large projects but only happens once per file load.
        try:
            if scene_type == SCENE_TYPE_BLENDER:
                entry.external_refs = blendfile.collect_external_refs(path)
            else:
                entry.external_refs = nukescript.collect_external_refs(path)
        except Exception as e:
            self._log(f"  WARN: external-refs walk failed: {e}")
            entry.external_refs = []

        # New entries inherit the currently visible Farm / Queue selection
        # and the shared-storage toggle so that dropping multiple files in
        # a row defaults to the same target without re-clicking each time.
        entry.farm_id = self.farm_queue_bar.selected_farm_id() or ""
        entry.queue_id = self.farm_queue_bar.selected_queue_id() or ""
        entry.use_shared_storage = self.submission.shared_storage_check.isChecked()
        self.entries.insert(0, entry)
        # Existing active_index shifts down by 1 because we inserted at top.
        if self.active_index is not None:
            self.active_index += 1
        self.history.insert_top(entry)
        self.history.select(0)  # triggers _on_history_selected

        if scene_type == SCENE_TYPE_BLENDER:
            self._log(
                f"  Blender {info.version} (sub {info.subversion}) | "
                f"{info.renderer or '?'} | frames {info.start_frame}-{info.end_frame}"
            )
            self._log(f"  cameras={info.cameras}")
            self._log(f"  view_layers={info.view_layers}")
        else:
            ver = f"{info.version} {info.subversion}".strip() or "?"
            self._log(
                f"  NukeX {ver} | {info.format_resolution or '?'} "
                f"({info.format_name or '-'}) | "
                f"frames {info.start_frame}-{info.end_frame} @ {info.fps or '?'}fps"
            )
            self._log(
                f"  write_nodes={[w.name for w in info.write_nodes]}"
            )
            self._log(f"  views={info.views}")
        if entry.external_refs:
            unique_present = {r.resolved for r in entry.external_refs if r.exists}
            unique_missing = {r.resolved for r in entry.external_refs if not r.exists}
            self._log(
                f"  external_refs: {len(unique_present)} present, "
                f"{len(unique_missing)} missing"
            )
            for missing in sorted(unique_missing):
                self._log(f"    MISSING: {missing}")

    def _on_history_selected(self, row: int) -> None:
        if row < 0 or row == self.active_index:
            return
        self._snapshot_active()
        self._activate(row)

    def _on_submit(self) -> None:
        if self.active_index is None:
            return
        # Persist current widget state (incl. Farm/Queue) into the active entry.
        self._snapshot_active()
        entry = self.entries[self.active_index]

        problem = self._validate(entry)
        if problem:
            self._log(f"  Invalid: {problem}")
            QMessageBox.warning(self, "Cannot submit", problem)
            return

        if not entry.farm_id or not entry.queue_id:
            msg = (
                "Select a Farm and Queue at the top of the window before "
                "submitting. Press Refresh if the dropdowns are empty."
            )
            self._log(f"  Invalid: {msg}")
            QMessageBox.warning(self, "Cannot submit", msg)
            return

        if entry.scene_type == SCENE_TYPE_BLENDER:
            job_parameters = [
                {"name": "SceneFile", "value": entry.path},
                {"name": "Frames", "value": f"{entry.start_frame}-{entry.end_frame}"},
                {"name": "FramesPerTask", "value": str(entry.frames_per_task)},
                {"name": "Camera", "value": entry.camera},
                {"name": "ViewLayer", "value": entry.view_layer},
                {"name": "OutputFormat", "value": entry.output_format},
                {"name": "OutputPath", "value": entry.output_path},
            ]
        else:
            job_parameters = [
                {"name": "SceneFile", "value": entry.path},
                {"name": "Frames", "value": f"{entry.start_frame}-{entry.end_frame}"},
                {"name": "FramesPerTask", "value": str(entry.frames_per_task)},
                {"name": "WriteNode", "value": entry.write_node},
                {"name": "Views", "value": entry.views},
            ]
            # Job-Attachments-mode-only parameters: the worker-side init.py
            # (delivered as a PATH/IN file and staged into NUKE_PATH on the
            # worker) and SceneFileOriginal (used by init.py to derive a
            # submitter→worker prefix for in-script Read paths). In shared
            # storage mode the script's paths are already valid on the
            # worker so neither is needed.
            if not entry.use_shared_storage:
                job_parameters.extend(
                    [
                        {"name": "SceneFileOriginal", "value": entry.path},
                        {
                            "name": "NukeInit",
                            "value": str(JOB_BUNDLE_DIR / "nuke_init.py"),
                        },
                    ]
                )

        # In-memory config override so we submit to the chosen farm/queue
        # without touching the user's persisted defaults.
        config = config_file.read_config()
        config_file.set_setting("defaults.farm_id", entry.farm_id, config=config)
        config_file.set_setting("defaults.queue_id", entry.queue_id, config=config)

        # In Job-Attachments mode, build a list of every referenced file that
        # exists on disk so they get uploaded alongside the scene. The main
        # scene goes through the SceneFile PATH/IN parameter, so we exclude
        # it from the asset list to avoid a duplicate input.
        # In shared-storage mode we skip this list entirely — the worker
        # resolves references via the same NAS path.
        scene_path = Path(entry.path).resolve()
        if entry.use_shared_storage:
            extra_inputs: list[Path] = []
        else:
            extra_inputs = sorted(
                {
                    r.resolved
                    for r in entry.external_refs
                    if r.exists and r.resolved != scene_path
                }
            )
        bundle_dir = _prepare_bundle_with_assets(
            entry.scene_type, extra_inputs, use_shared_storage=entry.use_shared_storage
        )

        self.submission.submit_btn.setEnabled(False)
        self.submission.begin_progress()
        self._log(f"Submitting {entry.display_name}…")
        self._log(f"  job name: {entry.job_name}")
        self._log(f"  farm:     {entry.farm_id}")
        self._log(f"  queue:    {entry.queue_id}")
        self._log(f"  priority: {entry.priority}")
        self._log(f"  bundle:   {bundle_dir}")
        if entry.use_shared_storage:
            self._log("  storage:  shared (no Job Attachments upload)")
        elif extra_inputs:
            self._log(
                f"  extra inputs (asset_references.yaml): {len(extra_inputs)} files"
            )
        for p in job_parameters:
            self._log(f"  {p['name']}: {p['value']}")

        # ---- Spin up a worker thread so the upload doesn't freeze the GUI ----
        thread = QThread(self)
        worker = SubmitWorker(
            bundle_dir=str(bundle_dir),
            job_parameters=job_parameters,
            priority=entry.priority,
            config=config,
            name=entry.job_name,
        )
        # Stash the temp bundle path so we can clean it up after the thread
        # finishes (whether successful or not).
        thread.finished.connect(lambda d=bundle_dir: shutil.rmtree(d, ignore_errors=True))
        worker.moveToThread(thread)
        # Capture the entry index at submit time so a switch during upload
        # still updates the right entry's submission record.
        target_index = self.active_index

        worker.log.connect(self._log)
        worker.progress.connect(self.submission.update_progress)
        worker.succeeded.connect(
            lambda job_id: self._on_submit_succeeded(target_index, job_id)
        )
        worker.failed.connect(self._on_submit_failed)

        # Cleanup: when worker reports either outcome, quit the thread; when
        # the thread finishes, delete both objects.
        worker.succeeded.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        thread.started.connect(worker.run)
        thread.start()

        # Hold a reference so neither thread nor worker is GC'd mid-flight.
        self._submit_thread = thread
        self._submit_worker = worker

    def _on_submit_succeeded(self, entry_index: int, job_id: str) -> None:
        if 0 <= entry_index < len(self.entries):
            entry = self.entries[entry_index]
            entry.last_submitted_at = datetime.now()
            entry.last_job_id = job_id
            self.history.update_row(entry_index, entry)
            # Refresh the panel only if this entry is still the active one.
            if entry_index == self.active_index:
                self.submission.load_from(entry)
        self._log(f"  -> {job_id}")
        self.submission.end_progress()
        self.submission.submit_btn.setEnabled(True)

    def _on_submit_failed(
        self, message: str, is_auth_error: bool, details: str
    ) -> None:
        self.submission.end_progress()
        self.submission.submit_btn.setEnabled(True)
        title = "Authentication failed" if is_auth_error else "Submission failed"
        self._log(f"  {'Auth failed' if is_auth_error else 'ERROR'}: {message}")
        if details:
            for line in details.rstrip().splitlines():
                self._log(f"    {line}")
        if is_auth_error:
            self._log(
                "  Refresh credentials externally (e.g. swap the IAM role on this "
                "host, run `aws sso login`, or update ~/.aws/credentials), then "
                "click Refresh and try again."
            )
        _show_error_dialog(self, title, message, details)

    @staticmethod
    def _validate(entry: HistoryEntry) -> Optional[str]:
        if not Path(entry.path).is_file():
            return f"Scene file no longer exists locally: {entry.path}"
        if entry.start_frame > entry.end_frame:
            return (
                f"Start frame ({entry.start_frame}) must be <= "
                f"end frame ({entry.end_frame})."
            )
        if entry.frames_per_task < 1:
            return f"Frames per task must be >= 1, got {entry.frames_per_task}."
        return None

    # ----------------------------------------------------------------------
    def _snapshot_active(self) -> None:
        """Persist current widget state into the active entry."""
        if self.active_index is not None and 0 <= self.active_index < len(self.entries):
            entry = self.entries[self.active_index]
            self.submission.save_to(entry)
            entry.farm_id = self.farm_queue_bar.selected_farm_id() or ""
            entry.queue_id = self.farm_queue_bar.selected_queue_id() or ""

    def _activate(self, row: int) -> None:
        self.active_index = row
        entry = self.entries[row]
        self.file_bar.set_path_quiet(entry.path)
        self.scene_info.show_info(entry.scene_type, entry.info, entry.external_refs)
        self.submission.load_from(entry)
        self.farm_queue_bar.set_selection(entry.farm_id, entry.queue_id)

    def _log(self, msg: str) -> None:
        self.log.appendPlainText(msg)


def _apply_dark_theme(app: QApplication) -> None:
    """Apply a dark theme via pyqtdarktheme if available, supporting both APIs."""
    try:
        import qdarktheme  # type: ignore
    except ImportError:
        return
    if hasattr(qdarktheme, "setup_theme"):  # pyqtdarktheme 2.x
        qdarktheme.setup_theme("dark")
    elif hasattr(qdarktheme, "load_stylesheet"):  # pyqtdarktheme 0.1.x
        app.setStyleSheet(qdarktheme.load_stylesheet("dark"))


def main() -> int:
    app = QApplication(sys.argv)
    _apply_dark_theme(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
