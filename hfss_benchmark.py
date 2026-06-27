"""
hfss_benchmark_runner_rating_speedup.py

HFSS embedded FilterSolutions model benchmark runner for:
- Model: embedded FilterSolutions filter geometry
- Design: FilterSolutions_Embedded_Benchmark
- Keep original solution type of the example project; do not change solver type
- Target: Setup1 : Sweep
- GUI mode is kept enabled for debugging and observation

Main workflow:
1. Locate AnsysEM automatically.
2. Create a fresh temporary AEDT project for each run.
3. Build the filter geometry directly in AEDT through COM.
4. Open the generated AEDT project through PyAEDT.
5. Keep the original solution type, normally HFSS with Hybrid and Arrays for this example.
6. Always write Tasks/Cores by generating an ACF configuration file directly.
7. Keep usual distribution types but exclude Iterative Solver Excitations and Direct Solver Memory.
8. Solve using PyAEDT hfss.analyze_setup(...).
9. Close AEDT and clean only the Ansys processes newly created by this script.
10. Print point-throughput and throughput-speedup metrics.
11. Save results only when the user clicks the export button.

Dependency:
    pip install pyaedt

Syntax check:
    python -m py_compile hfss_benchmark_runner_rating_speedup.py

Run:
    python hfss_benchmark_runner_rating_speedup.py
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import struct
import platform
import ctypes
import contextlib
import io
import base64
import zlib
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


UI_BG = "#ffffff"
MODEL_PHOTO_FILENAMES = ("photo.jpg", "photo.jpeg", "photo.png")


def get_program_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path.cwd()


def get_bundle_resource_directory() -> Path | None:
    """Return PyInstaller/auto-py-to-exe temporary resource dir when bundled.

    If photo.jpg is added through auto-py-to-exe's "Additional Files", one-file
    mode extracts it into sys._MEIPASS. Searching this path lets the exe display
    the bundled image without keeping photo.jpg beside the exe.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    try:
        p = Path(meipass).resolve()
        if p.exists() and p.is_dir():
            return p
    except Exception:
        pass
    return None


def find_model_photo_path() -> Path | None:
    candidates: list[Path] = []
    resource_dirs: list[Path] = []

    bundle_dir = get_bundle_resource_directory()
    if bundle_dir is not None:
        resource_dirs.append(bundle_dir)

    resource_dirs.append(get_program_directory())
    resource_dirs.append(Path.cwd())

    # Deduplicate while preserving priority: bundled resource first, then exe
    # folder/source folder, then current working directory.
    seen: set[str] = set()
    unique_dirs: list[Path] = []
    for d in resource_dirs:
        key = str(d).lower()
        if key not in seen:
            unique_dirs.append(d)
            seen.add(key)

    for d in unique_dirs:
        for name in MODEL_PHOTO_FILENAMES:
            candidates.append(d / name)
            candidates.append(d / "assets" / name)
            candidates.append(d / "resources" / name)

    for path in candidates:
        try:
            if path.exists() and path.is_file():
                return path
        except Exception:
            pass
    return None


def read_png_size(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as f:
            header = f.read(24)
        if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = struct.unpack(">II", header[16:24])
            return int(width), int(height)
    except Exception:
        pass
    return None


def load_model_photo_source_image():
    """Load photo.jpg/photo.jpeg/photo.png as a Pillow image for responsive preview.

    The GUI keeps a source image and regenerates a resized preview whenever the
    window changes size. Pillow is required for smooth responsive scaling.
    """
    path = find_model_photo_path()
    if path is None:
        return None, None

    try:
        from PIL import Image, ImageChops, ImageOps  # type: ignore

        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)

            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                white.alpha_composite(rgba)
                im = white.convert("RGB")
            else:
                im = im.convert("RGB")

            # Trim only very large near-white margins. This keeps the model visible
            # while preventing a 6000x4000 screenshot canvas from dominating the UI.
            bg = Image.new("RGB", im.size, (255, 255, 255))
            diff = ImageChops.difference(im, bg).convert("L")
            diff = diff.point(lambda px: 0 if px < 12 else 255)
            bbox = diff.getbbox()
            if bbox:
                pad = 40
                left = max(0, bbox[0] - pad)
                top = max(0, bbox[1] - pad)
                right = min(im.size[0], bbox[2] + pad)
                bottom = min(im.size[1], bbox[3] + pad)
                # Avoid over-cropping if the whole image is already meaningful.
                cropped_area = (right - left) * (bottom - top)
                full_area = im.size[0] * im.size[1]
                if 0.08 * full_area <= cropped_area <= 0.95 * full_area:
                    im = im.crop((left, top, right, bottom))

            return im.copy(), path
    except Exception:
        return None, path


def resize_model_photo_for_panel(source_image, max_width: int, max_height: int):
    try:
        from PIL import Image, ImageTk  # type: ignore

        if source_image is None:
            return None
        max_width = max(80, int(max_width))
        max_height = max(60, int(max_height))

        im = source_image.copy()
        im.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", im.size, (255, 255, 255))
        canvas.paste(im, (0, 0))
        return ImageTk.PhotoImage(canvas)
    except Exception:
        return None


def render_model_photo_at_size(source_image, target_width: int, target_height: int):
    try:
        from PIL import Image, ImageTk  # type: ignore

        if source_image is None:
            return None
        target_width = max(20, int(target_width))
        target_height = max(20, int(target_height))

        im = source_image.copy()
        im = im.resize((target_width, target_height), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", im.size, (255, 255, 255))
        canvas.paste(im, (0, 0))
        return ImageTk.PhotoImage(canvas)
    except Exception:
        return None


def setup_utf8_stdio() -> None:
    """Reduce Chinese mojibake after PyInstaller / auto-py-to-exe packaging."""
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


setup_utf8_stdio()


PROGRAM_NAME = "HFSS Embedded Filter Benchmark"
PROGRAM_RELEASE = "PyAEDT"
PROGRAM_VERSION_TAG = "embedded_filter"

DEFAULT_VERSION_ID = "232"
DEFAULT_AEDT_VERSION = "2023.2"
EXAMPLE_RELATIVE_PATH = Path("Examples") / "HFSS" / "Antennas" / "5G_SIW_Aperture_Antenna.aedt"
DESIGN_NAME = "FilterSolutions_Embedded_Benchmark"
DESIGN_NAME_25R1 = DESIGN_NAME

# Pure PyAEDT combined version.
# The selected AEDT version is detected at runtime and written into exported data.
SETUP_NAME = "Setup1"
SWEEP_NAME = "Sweep"
BENCHMARK_SETUP_NAME = "Setup1"
BENCHMARK_SWEEP_NAME = "Sweep"

NATIVE_ANALYZE_TARGET = f"{BENCHMARK_SETUP_NAME} : {BENCHMARK_SWEEP_NAME}"

BENCHMARK_SETUP_FREQUENCY = "60GHz"
BENCHMARK_SWEEP_START = "56GHz"
BENCHMARK_SWEEP_END = "67GHz"
BENCHMARK_BASELINE_LINEAR_COUNT = 10
BENCHMARK_INTERPOLATING_RANGE_COUNT = BENCHMARK_BASELINE_LINEAR_COUNT  # compatibility alias; not used by embedded model

# Do not change the solution type. The original example uses HFSS with Hybrid and Arrays,
# and after fixing Job Distribution, the hybrid solution type can run frequency points in parallel.
CHANGE_SOLUTION_TYPE_TO_HFSS = False
HFSS_MODAL_SOLUTION_TYPE = "HFSS Modal Network"
HFSS_MODAL_OPTIONS = ["NAME:Options", "EnableAutoOpen:=", False]
VALIDATE_AFTER_SOLUTION_TYPE_CHANGE = False

# The script always writes HPC settings by creating an AEDT ACF file directly.
# This avoids PyAEDT set_custom_hpc_options() copying pyaedt_local_config.acf, which
# may fail after packaging with PyInstaller / auto-py-to-exe if package data is missing.
#
# ACF internal names from PyAEDT's default pyaedt_local_config.acf are:
# "Variations", "Frequencies", "Mesh Assembly", "Mesher",
# "Transient Excitations", "Domain Solver", "Solver",
# "Iterative Solver", "Direct Solver".
#
# User's normal setting: enable all usual types except:
# - Iterative Solver Excitations  -> ACF internal name: "Iterative Solver"
# - Direct Solver Memory          -> ACF internal name: "Direct Solver"
HPC_ALLOWED_DISTRIBUTION_TYPES = [
    "Variations",
    "Frequencies",
    "Mesh Assembly",
    "Mesher",
    "Transient Excitations",
    "Domain Solver",
    "Solver",
]
HPC_CONFIG_NAME = "pyaedt_config"
HPC_USE_AUTO_SETTINGS = False
HPC_RAM_PERCENT = 90
HPC_NUM_JOB_CORES = 0


# Keep AEDT GUI open during debugging so that the running process is visible.
NON_GRAPHICAL = False
NEW_DESKTOP = True
KEEP_TEMP_ON_FAIL = False
RESULT_CSV = "hfss_embedded_filter_benchmark_results.csv"

# Clean only Ansys processes created during this script run.
FORCE_CLOSE_NEW_ANSYS_PROCESSES = True
BENCH_TEMP_PREFIX = "hfss_filter_bench_"
ANSYS_PROCESS_NAMES_TO_CLEAN = (
    "ansysedt.exe",
    "ansyscl.exe",
    "hf3d.exe",
    "hfsscomengine.exe",
    "mpiexec.exe",
    "hydra_service.exe",
    "pmi_proxy.exe",
)

@dataclass
class ResultDirInfo:
    exists: bool
    file_count: int
    total_bytes: int
    sweep_name_seen: bool



@dataclass
class ProcessCleanupInfo:
    enabled: bool
    killed_pids: list[int]
    errors: list[str]


@dataclass
class SolutionTypeInfo:
    changed: bool
    validation_passed: bool
    validate_return: str
    error: str


@dataclass(frozen=True)
class AedtInstall:
    win64_path: Path
    ansysedt_exe: Path
    version_id: str
    pyaedt_version: str
    display_name: str
    source: str


@dataclass
class OneRunResult:
    case_id: str
    case_name: str
    case_short_name: str
    design_name: str
    setup_name: str
    sweep_name: str
    tasks: int
    sweep_points: int
    cores: int
    hpc_control: str
    solution_type_changed: bool
    validation_passed: bool
    validate_return: str
    native_analyze_target: str
    status: str
    benchmark_valid: bool
    analyze_call: str
    result_dir_exists: bool
    result_file_count: int
    result_total_mb: float
    sweep_name_seen: bool
    killed_processes: str
    open_seconds: float
    solve_seconds: float
    total_seconds: float
    error_summary: str
    round_index: int = 0
    repeat_rounds: int = 1
    result_kind: str = "final"


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    display_name: str
    short_name: str
    example_relative_path: Path
    design_name: str
    setup_name: str
    sweep_name: str
    recommended_tasks: int
    effective_frequency_points: int

    @property
    def native_analyze_target(self) -> str:
        return f"{self.setup_name} : {self.sweep_name}"


BENCHMARK_CASES: dict[str, BenchmarkCase] = {
    "embedded_filtersolutions_filter": BenchmarkCase(
        case_id="embedded_filtersolutions_filter",
        display_name="Embedded FilterSolutions Filter / HFSS Modal Network",
        short_name="HFSS-MN / Embedded Filter",
        example_relative_path=Path("."),
        design_name=DESIGN_NAME,
        setup_name=SETUP_NAME,
        sweep_name=SWEEP_NAME,
        recommended_tasks=10,
        effective_frequency_points=10,
    ),
}

# This version creates the benchmark model directly from embedded FilterSolutions data.
FIXED_CASE_ID = "embedded_filtersolutions_filter"


def get_case(case_id: str) -> BenchmarkCase:
    try:
        return BENCHMARK_CASES[case_id]
    except KeyError as exc:
        raise RuntimeError(f"未知 benchmark 例程：{case_id}") from exc


def get_max_available_tasks() -> int:
    """Return the maximum Tasks count this machine should attempt automatically.

    For local runs, Tasks is capped by the logical CPU count. This is a practical
    default for this benchmark tool because the script also sets Cores to at least
    the selected Tasks count.
    """
    return max(os.cpu_count() or 1, 1)


def get_target_sweep_points() -> int:
    """Use the machine logical thread count as both sweep-point count and full-load Tasks."""
    return max(os.cpu_count() or 1, 1)


def choose_auto_best_tasks(case: BenchmarkCase, max_tasks: int | None = None) -> tuple[int, str]:
    """Choose full-load Tasks.

    This single-project stress mode intentionally ignores memory risk for now:
    full-load Tasks equals the machine logical thread count, capped by the sweep
    point count, which is also set to the logical thread count.
    """
    logical = get_max_available_tasks() if max_tasks is None else max(1, int(max_tasks))
    points = get_target_sweep_points()
    full_tasks = max(1, min(logical, points))
    return full_tasks, f"满载模式：扫频点数={points}，Tasks={full_tasks}，不按内存容量限速"


def get_auto_tasks_for_case(case: BenchmarkCase) -> tuple[list[int], str]:
    full_tasks, reason = choose_auto_best_tasks(case)
    tasks = sorted(set([1, full_tasks]))
    return tasks, reason

def import_hfss_class():
    try:
        from ansys.aedt.core import Hfss  # type: ignore
        return Hfss
    except Exception:
        try:
            from pyaedt import Hfss  # type: ignore
            return Hfss
        except Exception as exc:
            raise RuntimeError("无法导入 PyAEDT。请先执行：pip install pyaedt") from exc


def run_cmd(args: list[str]) -> subprocess.CompletedProcess:
    creationflags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        errors="ignore",
        creationflags=creationflags,
    )


def get_process_snapshot(process_names: Iterable[str]) -> dict[str, set[int]]:
    snapshot: dict[str, set[int]] = {name.lower(): set() for name in process_names}
    if os.name != "nt":
        return snapshot

    for name in process_names:
        name_low = name.lower()
        try:
            cp = run_cmd(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"])
            text = cp.stdout.strip()
            if not text or "No tasks" in text or "INFO:" in text:
                continue
            for row in csv.reader(text.splitlines()):
                if len(row) >= 2 and row[0].strip('"').lower() == name_low:
                    try:
                        snapshot[name_low].add(int(row[1].strip('"')))
                    except Exception:
                        pass
        except Exception:
            pass
    return snapshot



def kill_pid_tree(pid: int) -> tuple[bool, str]:
    if os.name != "nt":
        return False, "not windows"
    try:
        cp = run_cmd(["taskkill", "/PID", str(pid), "/T", "/F"])
        ok = cp.returncode == 0
        msg = (cp.stdout or cp.stderr or "").strip()
        return ok, msg
    except Exception as exc:
        return False, str(exc)


def cleanup_new_ansys_processes(before_snapshot: dict[str, set[int]]) -> ProcessCleanupInfo:
    if not FORCE_CLOSE_NEW_ANSYS_PROCESSES:
        return ProcessCleanupInfo(False, [], [])

    time.sleep(2.0)
    after_snapshot = get_process_snapshot(ANSYS_PROCESS_NAMES_TO_CLEAN)
    killed: list[int] = []
    errors: list[str] = []

    ordered_names = ["ansysedt.exe"] + [
        n for n in ANSYS_PROCESS_NAMES_TO_CLEAN if n.lower() != "ansysedt.exe"
    ]
    for name in ordered_names:
        name_low = name.lower()
        before = before_snapshot.get(name_low, set())
        after = after_snapshot.get(name_low, set())
        new_pids = sorted(after - before)
        for pid in new_pids:
            ok, msg = kill_pid_tree(pid)
            if ok:
                killed.append(pid)
            else:
                errors.append(f"taskkill {name} PID={pid} failed: {msg}")

    return ProcessCleanupInfo(True, killed, errors)




def cleanup_benchmark_temp_dirs() -> tuple[int, list[str]]:
    """Remove temp folders created by this benchmark tool."""
    removed = 0
    errors: list[str] = []
    temp_root = Path(tempfile.gettempdir())

    try:
        candidates = list(temp_root.glob(BENCH_TEMP_PREFIX + "*"))
    except Exception as exc:
        return 0, [str(exc)]

    for path in candidates:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=False)
                removed += 1
        except Exception as exc:
            errors.append(f"{path}: {exc}")

    return removed, errors

def existing_paths(paths: Iterable[Path]) -> list[Path]:
    out = []
    seen = set()
    for p in paths:
        try:
            rp = p.expanduser().resolve()
        except Exception:
            continue
        key = str(rp).lower()
        if key not in seen and rp.exists():
            out.append(rp)
            seen.add(key)
    return out


def clean_path_like_value(value: str) -> str:
    """Clean environment/registry values that may contain quotes, commas, or exe arguments."""
    s = str(value or "").strip().strip('"')
    if not s:
        return ""

    # Registry DisplayIcon often looks like: "C:\...\ansysedt.exe,0"
    if ".exe," in s.lower():
        s = s[: s.lower().find(".exe,") + 4]

    # Remove simple command-line arguments after ansysedt.exe.
    m = re.search(r"(?i)(.*?ansysedt\.exe)", s)
    if m:
        return m.group(1).strip().strip('"')

    return s.strip().strip('"')


def _append_unique_path(items: list[Path], path: Path) -> None:
    try:
        key = str(path).lower()
    except Exception:
        return
    if not key:
        return
    if not any(str(x).lower() == key for x in items):
        items.append(path)


def _candidate_exe_dirs_from_base(base: Path) -> list[Path]:
    """Generate likely directories containing ansysedt.exe from many AEDT layouts.

    Supported examples include:
        ...\AnsysEM\v232\Win64
        ...\v232\Win64
        ...\v251\AnsysEM
        ...\v251\AnsysEM\Win64
        direct path to ansysedt.exe
        an ANSYSEM_ROOTxxx value that points to any of the above or their parents
    """
    candidates: list[Path] = []
    try:
        p = Path(clean_path_like_value(str(base))).expanduser()
    except Exception:
        return candidates

    if not str(p):
        return candidates

    # Direct executable path.
    if p.name.lower() == "ansysedt.exe":
        _append_unique_path(candidates, p.parent)
        return candidates

    # Direct directory possibilities.
    for item in (
        p,
        p / "Win64",
        p / "AnsysEM",
        p / "AnsysEM" / "Win64",
        p / "ANSYS Inc",
        p / "ANSYS Inc" / "Win64",
    ):
        _append_unique_path(candidates, item)

    # Version root: ...\v251
    if re.fullmatch(r"v\d{3}", p.name.lower()):
        for item in (
            p / "Win64",
            p / "AnsysEM",
            p / "AnsysEM" / "Win64",
            p / "ANSYS Inc",
            p / "ANSYS Inc" / "Win64",
        ):
            _append_unique_path(candidates, item)

    # Known version directories under a base folder. Do not hard-code versions.
    for prefix in (p, p / "AnsysEM", p / "ANSYS Inc"):
        try:
            for vdir in prefix.glob("v*"):
                if not vdir.is_dir():
                    continue
                for item in (
                    vdir,
                    vdir / "Win64",
                    vdir / "AnsysEM",
                    vdir / "AnsysEM" / "Win64",
                    vdir / "ANSYS Inc",
                    vdir / "ANSYS Inc" / "Win64",
                ):
                    _append_unique_path(candidates, item)
        except Exception:
            pass

    # Bounded fallback search for custom layouts. This is intentionally shallow
    # to avoid scanning an entire disk from roots like C:\.
    try:
        name_low = p.name.lower()
        path_low = str(p).lower()
        allow_shallow_search = (
            "ansys" in path_low
            or "ansysem" in path_low
            or re.fullmatch(r"v\d{3}", name_low) is not None
            or p.name.lower() in ("win64", "ansysem", "ansys inc")
        )
        # Avoid expensive searches on very broad roots.
        broad_names = {"", "\\", "/", "program files", "program files (x86)"}
        if allow_shallow_search and p.exists() and p.is_dir() and p.name.lower() not in broad_names:
            base_depth = len(p.parts)
            for exe in p.glob("**/ansysedt.exe"):
                try:
                    if len(exe.parts) - base_depth <= 5:
                        _append_unique_path(candidates, exe.parent)
                except Exception:
                    pass
    except Exception:
        pass

    return candidates


def normalize_win64_path(path: Path) -> Path | None:
    """Return the AEDT executable directory if ansysedt.exe can be found.

    The historic variable name says Win64, but newer AEDT layouts may use a
    directory such as ...\v251\AnsysEM instead of ...\v251\Win64. The returned
    directory is therefore the directory containing ansysedt.exe, not necessarily
    a folder literally named Win64.
    """
    for c in _candidate_exe_dirs_from_base(path):
        try:
            c_resolved = c.resolve()
        except Exception:
            c_resolved = c
        if (c_resolved / "ansysedt.exe").exists():
            return c_resolved
    return None


def version_id_from_win64(win64: Path) -> str:
    parts = [p.lower() for p in win64.parts]
    for part in reversed(parts):
        m = re.fullmatch(r"v(\d{3})", part)
        if m:
            return m.group(1)

    # Fallback: search the whole path.
    m = re.search(r"[\\/ ]v(\d{3})(?:[\\/ ]|$)", str(win64), re.IGNORECASE)
    if m:
        return m.group(1)

    return ""


def pyaedt_version_from_version_id(version_id: str) -> str:
    """Convert AnsysEM folder id 232 -> PyAEDT version string 2023.2."""
    m = re.fullmatch(r"(\d{2})(\d)", version_id or "")
    if not m:
        return DEFAULT_AEDT_VERSION
    year = 2000 + int(m.group(1))
    release = int(m.group(2))
    return f"{year}.{release}"


def aedt_install_from_win64(win64: Path, source: str = "auto") -> AedtInstall | None:
    exe_dir = normalize_win64_path(win64)
    if exe_dir is None:
        return None

    version_id = version_id_from_win64(exe_dir)
    pyaedt_version = pyaedt_version_from_version_id(version_id)
    layout_note = "Win64" if exe_dir.name.lower() == "win64" else exe_dir.name
    display = (
        f"AnsysEM v{version_id} / AEDT {pyaedt_version} [{layout_note}]"
        if version_id
        else f"AEDT {pyaedt_version} [{layout_note}]"
    )
    return AedtInstall(
        win64_path=exe_dir,
        ansysedt_exe=exe_dir / "ansysedt.exe",
        version_id=version_id,
        pyaedt_version=pyaedt_version,
        display_name=display,
        source=source,
    )


def sort_aedt_installs(installs: list[AedtInstall]) -> list[AedtInstall]:
    def key(item: AedtInstall) -> tuple[int, str]:
        try:
            version_num = int(item.version_id)
        except Exception:
            version_num = 0
        return (version_num, str(item.win64_path).lower())

    seen = set()
    unique: list[AedtInstall] = []
    for item in installs:
        k = str(item.win64_path).lower()
        if k not in seen:
            unique.append(item)
            seen.add(k)
    return sorted(unique, key=key, reverse=True)


def win64_candidates_from_base(base: Path) -> list[Path]:
    """Compatibility name: return candidate AEDT executable directories.

    The candidates are later normalized by checking ansysedt.exe. They may be
    Win64 folders, AnsysEM folders, version roots, or custom install folders.
    """
    return _candidate_exe_dirs_from_base(base)


def env_candidates() -> list[Path]:
    candidates: list[Path] = []

    # Explicit override from this benchmark GUI.
    for name in ("HFSS_BENCHMARK_AEDT_WIN64", "HFSS_BENCHMARK_ANSYSEDT_EXE"):
        value = os.environ.get(name)
        if value:
            candidates.extend(win64_candidates_from_base(Path(clean_path_like_value(value))))

    # Official/typical ANSYS environment variables, all versions.
    for name, value in os.environ.items():
        lname = name.upper()
        if not value:
            continue
        if lname.startswith("ANSYSEM_ROOT") or lname.startswith("ANSYS_ROOT") or "ANSYSEM" in lname:
            candidates.extend(win64_candidates_from_base(Path(clean_path_like_value(value))))

    # Some custom environments only put the path in the value.
    for value in os.environ.values():
        low = str(value).lower()
        if "ansys" in low or "ansysem" in low:
            candidates.extend(win64_candidates_from_base(Path(clean_path_like_value(value))))

    return candidates


def registry_candidates() -> list[Path]:
    if os.name != "nt":
        return []

    try:
        import winreg  # type: ignore
    except Exception:
        return []

    candidates: list[Path] = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\ANSYS, Inc."),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\ANSYS, Inc."),
    ]

    def read_value(key, value_name: str) -> Optional[str]:
        try:
            value, _ = winreg.QueryValueEx(key, value_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        except Exception:
            return None
        return None

    def scan_key(root, subkey: str, depth: int = 0) -> None:
        if depth > 4:
            return
        try:
            with winreg.OpenKey(root, subkey) as key:
                display = read_value(key, "DisplayName") or ""
                install = read_value(key, "InstallLocation") or ""
                path_val = read_value(key, "Path") or ""
                exe_val = read_value(key, "DisplayIcon") or ""

                joined = " ".join([display, install, path_val, exe_val]).lower()
                if "ansys" in joined or "electronics desktop" in joined or "ansysem" in joined:
                    for v in (install, path_val, exe_val):
                        if v:
                            cleaned = v.split(",")[0].strip('"')
                            candidates.extend(win64_candidates_from_base(Path(clean_path_like_value(cleaned))))

                try:
                    count, _, _ = winreg.QueryInfoKey(key)
                    for i in range(count):
                        try:
                            child = winreg.EnumKey(key, i)
                            scan_key(root, subkey + "\\" + child, depth + 1)
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass

    for root, subkey in roots:
        scan_key(root, subkey)

    return candidates


def common_path_candidates() -> list[Path]:
    candidates: list[Path] = []
    roots = [
        Path(r"C:\Program Files\AnsysEM"),
        Path(r"C:\Program Files\ANSYS Inc"),
        Path(r"C:\Program Files"),
        Path(r"D:\Program Files\AnsysEM"),
        Path(r"D:\Program Files\ANSYS Inc"),
        Path(r"D:\Program Files"),
        Path(r"D:\AnsysEM"),
        Path(r"C:\AnsysEM"),
    ]

    for drive in "CDEFGHIJ":
        roots.extend(
            [
                Path(f"{drive}:\\AnsysEM"),
                Path(f"{drive}:\\ANSYS Inc"),
                Path(f"{drive}:\\Program Files\\AnsysEM"),
                Path(f"{drive}:\\Program Files\\ANSYS Inc"),
            ]
        )

    for root in roots:
        candidates.extend(win64_candidates_from_base(root))

    return candidates


def discover_aedt_installs() -> list[AedtInstall]:
    candidates: list[Path] = []
    candidates.extend(env_candidates())
    candidates.extend(registry_candidates())
    candidates.extend(common_path_candidates())

    installs: list[AedtInstall] = []
    for root in existing_paths(candidates):
        install = aedt_install_from_win64(root, "auto")
        if install is not None:
            installs.append(install)

    return sort_aedt_installs(installs)


def get_manual_aedt_install_from_env() -> AedtInstall | None:
    # Explicit benchmark override has the highest priority.
    for name in ("HFSS_BENCHMARK_AEDT_WIN64", "HFSS_BENCHMARK_ANSYSEDT_EXE"):
        value = os.environ.get(name)
        if not value:
            continue
        install = aedt_install_from_win64(Path(clean_path_like_value(value)), "manual")
        if install is not None:
            return install

    official: list[AedtInstall] = []
    for name, value in os.environ.items():
        if not value:
            continue
        if name.upper().startswith("ANSYSEM_ROOT"):
            install = aedt_install_from_win64(Path(clean_path_like_value(value)), f"env:{name}")
            if install is not None:
                official.append(install)
    official = sort_aedt_installs(official)
    if len(official) == 1:
        return official[0]
    return None


def get_preferred_aedt_install() -> AedtInstall:
    manual = get_manual_aedt_install_from_env()
    if manual is not None:
        return manual

    installs = discover_aedt_installs()
    if installs:
        return installs[0]

    raise FileNotFoundError(
        "没有自动找到 Ansys Electronics Desktop 的 Win64 目录。\n"
        "请检查 AEDT 安装，或设置环境变量 HFSS_BENCHMARK_AEDT_WIN64 指向 Win64 目录。"
    )


def find_ansysem_win64() -> Path:
    """Compatibility helper."""
    return get_preferred_aedt_install().win64_path


def apply_aedt_install_environment(install: AedtInstall) -> None:
    """Help PyAEDT and AEDT subprocesses locate the selected installation."""
    os.environ["HFSS_BENCHMARK_AEDT_WIN64"] = str(install.win64_path)
    os.environ["HFSS_BENCHMARK_ANSYSEDT_EXE"] = str(install.ansysedt_exe)
    if install.version_id:
        os.environ[f"ANSYSEM_ROOT{install.version_id}"] = str(install.win64_path)

    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    win64_text = str(install.win64_path)
    if win64_text not in path_parts:
        os.environ["PATH"] = win64_text + os.pathsep + os.environ.get("PATH", "")


def aedt_install_to_dict(install: AedtInstall) -> dict:
    return {
        "win64_path": str(install.win64_path),
        "ansysedt_exe": str(install.ansysedt_exe),
        "version_id": install.version_id,
        "pyaedt_version": install.pyaedt_version,
        "display_name": install.display_name,
        "source": install.source,
    }


def aedt_install_from_dict(data: dict) -> AedtInstall | None:
    try:
        raw_dir = Path(str(data.get("win64_path", "")))
        exe_dir = normalize_win64_path(raw_dir)
        if exe_dir is None:
            exe = Path(str(data.get("ansysedt_exe", raw_dir / "ansysedt.exe")))
            exe_dir = normalize_win64_path(exe)
        if exe_dir is None:
            return None

        version_id = str(data.get("version_id", "")) or version_id_from_win64(exe_dir)
        pyaedt_version = str(data.get("pyaedt_version", "")) or pyaedt_version_from_version_id(version_id)
        return AedtInstall(
            win64_path=exe_dir,
            ansysedt_exe=exe_dir / "ansysedt.exe",
            version_id=version_id,
            pyaedt_version=pyaedt_version or DEFAULT_AEDT_VERSION,
            display_name=str(data.get("display_name", "")) or f"AEDT {pyaedt_version or DEFAULT_AEDT_VERSION}",
            source=str(data.get("source", "auto")),
        )
    except Exception:
        return None


def make_aedt_choice_label(install: AedtInstall) -> str:
    version = install.display_name
    return f"{version}    |    {install.win64_path}"


def design_name_for_aedt_version(aedt_version: str | None) -> str:
    return DESIGN_NAME


def benchmark_release_from_install(install: AedtInstall | None) -> str:
    if install is None:
        return PROGRAM_RELEASE
    version_id = str(getattr(install, "version_id", "") or "")
    version_map = {
        "232": "23R2",
        "241": "24R1",
        "242": "24R2",
        "251": "25R1",
        "252": "25R2",
    }
    if version_id in version_map:
        return version_map[version_id]
    pyaedt_version = str(getattr(install, "pyaedt_version", "") or "")
    if pyaedt_version:
        return "AEDT " + pyaedt_version
    return PROGRAM_RELEASE


def benchmark_program_from_install(install: AedtInstall | None) -> str:
    release = benchmark_release_from_install(install)
    return f"HFSS Benchmark {release} PyAEDT"


def benchmark_version_tag_from_install(install: AedtInstall | None) -> str:
    release = benchmark_release_from_install(install).lower()
    return re.sub(r"[^a-z0-9]+", "", release) + "_pyaedt"


def add_aedt_info_to_host_config(info: dict[str, str], install: AedtInstall | None) -> dict[str, str]:
    if install is None:
        info["AEDT版本"] = "未找到"
        info["AnsysEM"] = "未找到 Ansys Electronics Desktop"
        info["_aedt_win64"] = ""
        info["_aedt_version"] = ""
        info["_aedt_version_id"] = ""
        info["_aedt_source"] = ""
    else:
        info["AEDT版本"] = install.display_name
        info["AnsysEM"] = str(install.win64_path)
        info["_aedt_win64"] = str(install.win64_path)
        info["_aedt_version"] = install.pyaedt_version
        info["_aedt_version_id"] = install.version_id
        info["_aedt_source"] = install.source
    return info



def ask_tasks_list() -> list[int]:
    prompt = (
        "请输入要测试的 Tasks 数目，可输入单个值或列表，例如 1 或 1,2,4,8,11,16,20,24；"
        "直接回车默认为 1："
    )

    while True:
        text = input(prompt).strip()
        if not text:
            return [1]
        try:
            values = []
            for item in re.split(r"[,，\s]+", text):
                if not item:
                    continue
                v = int(item)
                if v < 1:
                    raise ValueError
                values.append(v)
            values = sorted(set(values))
            if values:
                return values
        except Exception:
            pass
        print("Tasks 列表格式不正确。示例：1,2,4,8,11,16,20,24")


def parse_tasks_text(text: str) -> list[int]:
    """Parse GUI tasks input such as 1,4,8,11,16,20,24."""
    if not text.strip():
        return [1]

    values: list[int] = []
    for item in re.split(r"[,，\s]+", text.strip()):
        if not item:
            continue
        v = int(item)
        if v < 1:
            raise ValueError("Tasks 必须是大于等于 1 的整数。")
        values.append(v)

    values = sorted(set(values))
    if not values:
        raise ValueError("Tasks 列表为空。")
    return values


def get_total_memory_gb() -> str:
    """Return total physical memory in GB on Windows without external dependencies."""
    if os.name != "nt":
        return "未知"

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return f"{stat.ullTotalPhys / 1024**3:.1f} GB"
    except Exception:
        return "未知"


def run_powershell_text(command: str, timeout: int = 8) -> str:
    """Run a PowerShell command and return stdout text.

    This helper is used only for reading host hardware information.
    It does not affect the HFSS benchmark workflow.
    """
    if os.name != "nt":
        return ""

    try:
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW

        cp = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=timeout,
            creationflags=creationflags,
        )
        out = (cp.stdout or "").strip()
        err = (cp.stderr or "").strip()
        return out if out else err
    except Exception:
        return ""


def get_cpu_name() -> str:
    """Read CPU model name."""
    text = run_powershell_text(
        "(Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name)"
    )
    if text:
        return text.splitlines()[0].strip()
    return platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "未知")


def get_physical_core_count() -> str:
    """Read total physical core count across all CPU packages."""
    text = run_powershell_text(
        "(Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfCores -Sum).Sum"
    )
    if text:
        return text.splitlines()[0].strip()
    return "未知"


def get_windows_display_name() -> str:
    """Return a user-friendly Windows version string.

    Windows 11 still reports NT version 10.0 internally. Some APIs and registry
    fields may also keep returning strings containing "Windows 10". Therefore,
    the reliable rule used here is:
        Build >= 22000  -> Windows 11
        Build <  22000  -> Windows 10 or earlier
    """
    if os.name != "nt":
        return platform.platform()

    # Query both Win32_OperatingSystem and the CurrentVersion registry key.
    # On some machines Caption is correct, while ProductName may still say Win10.
    ps = (
        "$os=Get-CimInstance Win32_OperatingSystem; "
        "$cv=Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion'; "
        "[PSCustomObject]@{"
        "Caption=$os.Caption;"
        "Version=$os.Version;"
        "BuildNumber=$os.BuildNumber;"
        "ProductName=$cv.ProductName;"
        "DisplayVersion=$cv.DisplayVersion;"
        "CurrentBuildNumber=$cv.CurrentBuildNumber;"
        "UBR=$cv.UBR;"
        "EditionID=$cv.EditionID"
        "} | ConvertTo-Json -Compress"
    )

    raw = run_powershell_text(ps, timeout=8).strip()
    if not raw:
        return platform.platform()

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return platform.platform()

        caption = str(data.get("Caption") or "").strip()
        product = str(data.get("ProductName") or "").strip()
        display = str(data.get("DisplayVersion") or "").strip()
        edition = str(data.get("EditionID") or "").strip()

        build_text = str(data.get("BuildNumber") or data.get("CurrentBuildNumber") or "").strip()
        ubr_text = str(data.get("UBR") or "").strip()

        try:
            build = int(build_text)
        except Exception:
            build = 0

        if build >= 22000:
            # Force Windows 11 even when ProductName / platform says Windows 10.
            if caption and "Windows 11" in caption:
                base = caption.replace("Microsoft ", "").strip()
            else:
                # Turn EditionID such as Professional into a readable label.
                edition_map = {
                    "Professional": "Pro",
                    "ProfessionalWorkstation": "Pro for Workstations",
                    "Enterprise": "Enterprise",
                    "Education": "Education",
                    "Core": "Home",
                    "CoreSingleLanguage": "Home Single Language",
                    "ServerStandard": "Server Standard",
                    "ServerDatacenter": "Server Datacenter",
                }
                edition_name = edition_map.get(edition, edition)
                base = "Windows 11" + (f" {edition_name}" if edition_name else "")
        else:
            base = caption.replace("Microsoft ", "").strip() if caption else product
            if not base:
                base = platform.platform()

        parts = [base]
        if display:
            parts.append(display)
        if build_text:
            parts.append(f"Build {build_text}.{ubr_text}" if ubr_text else f"Build {build_text}")

        return ", ".join(parts)

    except Exception:
        return platform.platform()


def get_memory_modules_info() -> list[dict]:
    """Read physical memory module information from Windows CIM/WMI."""
    if os.name != "nt":
        return []

    # InterleavePosition / InterleaveDataDepth are useful when firmware exposes
    # memory interleaving information. BankLabel / DeviceLocator often contains
    # channel names on workstation/server boards.
    ps = (
        "$m=Get-CimInstance Win32_PhysicalMemory | "
        "Select-Object Capacity,DataWidth,TotalWidth,Speed,ConfiguredClockSpeed,"
        "Manufacturer,PartNumber,BankLabel,DeviceLocator,InterleavePosition,InterleaveDataDepth; "
        "$m | ConvertTo-Json -Compress"
    )
    raw = run_powershell_text(ps, timeout=10).strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        return []
    return []


def get_memory_array_info() -> dict:
    """Read physical memory array information from Windows CIM/WMI."""
    if os.name != "nt":
        return {}
    ps = (
        "$a=Get-CimInstance Win32_PhysicalMemoryArray | "
        "Select-Object MemoryDevices,MaxCapacityEx,MaxCapacity; "
        "$a | Select-Object -First 1 | ConvertTo-Json -Compress"
    )
    raw = run_powershell_text(ps, timeout=10).strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except Exception:
        return default


def _memory_module_capacity_gb(module: dict) -> float:
    return _safe_int(module.get("Capacity"), 0) / 1024**3


def _memory_module_speed_mts(module: dict) -> int:
    configured = _safe_int(module.get("ConfiguredClockSpeed"), 0)
    if configured > 0:
        return configured
    return _safe_int(module.get("Speed"), 0)


def _extract_channel_from_labels(label_text: str) -> str | None:
    """Best-effort channel extraction from BankLabel/DeviceLocator strings."""
    s = (label_text or "").upper()

    patterns = [
        r"CHANNEL\s*([A-Z0-9]+)",
        r"CHAN\s*([A-Z0-9]+)",
        r"\bCH\s*([A-Z0-9]+)\b",
        r"\bCHA\b",
        r"\bCHB\b",
        r"\bCHC\b",
        r"\bCHD\b",
        r"\bCHE\b",
        r"\bCHF\b",
        r"\bCHG\b",
        r"\bCHH\b",
        r"CPU\d+_DIMM_([A-Z])",
        r"DIMM_([A-Z])\d*",
        r"DIMM\s*([A-Z])\d*",
    ]

    for pat in patterns:
        m = re.search(pat, s)
        if not m:
            continue
        if pat.startswith(r"\bCH") and pat.endswith(r"\b"):
            return m.group(0).replace(" ", "")
        return m.group(1).strip()

    return None


def get_memory_channel_info(modules: list[dict]) -> tuple[str, int | None, str]:
    """Return display text, channel count for bandwidth, and source description.

    True active memory-channel mode is not universally exposed by Windows WMI.
    The function therefore uses a hierarchy:
    1) WMI InterleavePosition if firmware exposes meaningful values.
    2) Channel-like names in BankLabel / DeviceLocator.
    3) Populated DIMM count as a fallback estimate.
    """
    populated = [m for m in modules if _safe_int(m.get("Capacity"), 0) > 0]
    if not populated:
        return "未知", None, "未读取到内存条信息"

    interleave_positions = []
    for m in populated:
        pos = _safe_int(m.get("InterleavePosition"), 0)
        if pos > 0:
            interleave_positions.append(pos)

    unique_interleave = sorted(set(interleave_positions))
    if len(unique_interleave) > 1:
        count = len(unique_interleave)
        return f"{count} 通道", count, f"WMI InterleavePosition={unique_interleave}"

    label_channels = []
    for m in populated:
        text = f"{m.get('BankLabel') or ''} {m.get('DeviceLocator') or ''}"
        ch = _extract_channel_from_labels(text)
        if ch:
            label_channels.append(ch)

    unique_label_channels = sorted(set(label_channels))
    if len(unique_label_channels) > 1:
        count = len(unique_label_channels)
        return f"{count} 通道", count, f"BankLabel/DeviceLocator={unique_label_channels}"

    # Fallback. This is not necessarily true on platforms with two DIMMs per channel
    # or partially populated server boards, so mark it as an estimate.
    count = len(populated)
    return f"约 {count} 通道", count, "按已安装内存条数量估算"


def get_memory_config_dict() -> dict[str, str]:
    modules = get_memory_modules_info()
    populated = [m for m in modules if _safe_int(m.get("Capacity"), 0) > 0]

    if populated:
        total_gb = sum(_memory_module_capacity_gb(m) for m in populated)
        total_text = f"{total_gb:.1f} GB"
        module_count = len(populated)
    else:
        total_text = get_total_memory_gb()
        module_count = 0

    speeds = [_memory_module_speed_mts(m) for m in populated]
    speeds = [s for s in speeds if s > 0]
    if speeds:
        speed_mts = min(speeds)
        speed_text = f"{speed_mts} MT/s"
    else:
        speed_mts = 0
        speed_text = "未知"

    data_widths = [_safe_int(m.get("DataWidth"), 0) for m in populated]
    data_widths = [w for w in data_widths if w > 0]
    data_width_bits = min(data_widths) if data_widths else 64

    channel_text, channel_count, channel_source = get_memory_channel_info(modules)

    if channel_count and speed_mts:
        bandwidth_gbs = channel_count * speed_mts * (data_width_bits / 8) / 1000
        bandwidth_text = f"{bandwidth_gbs:.1f} GB/s"
    else:
        bandwidth_text = "未知"

    module_desc = []
    for i, m in enumerate(populated, 1):
        cap_text = f"{_memory_module_capacity_gb(m):.0f}GB"
        spd = _memory_module_speed_mts(m)
        speed_part = f"{spd}MT/s" if spd else "未知频率"
        locator = str(m.get("DeviceLocator") or m.get("BankLabel") or f"DIMM{i}").strip()
        part = str(m.get("PartNumber") or "").strip()
        item = f"{locator}: {cap_text} {speed_part}"
        if part:
            item += f" {part}"
        module_desc.append(item)

    array = get_memory_array_info()
    slot_count = _safe_int(array.get("MemoryDevices"), 0)
    slot_text = str(slot_count) if slot_count > 0 else "未知"

    return {
        "内存总量": total_text,
        "内存条数量": str(module_count) if module_count else "未知",
        "内存插槽数": slot_text,
        "内存频率": speed_text,
        "内存通道": channel_text,
        "通道来源": channel_source,
        "数据位宽": f"{data_width_bits} bit" if data_width_bits else "未知",
        "理论内存带宽": bandwidth_text,
        "内存条信息": "\n".join(module_desc) if module_desc else "未知",
    }


def get_system_config_dict() -> dict[str, str]:
    logical = os.cpu_count() or 1

    info: dict[str, str] = {
        "操作系统": get_windows_display_name(),
        "CPU": get_cpu_name(),
        "物理核心数": get_physical_core_count(),
        "逻辑线程数": str(logical),
    }

    info.update(get_memory_config_dict())
    return info


def get_host_config_dict() -> dict[str, str]:
    info = get_system_config_dict()

    try:
        install = get_preferred_aedt_install()
        add_aedt_info_to_host_config(info, install)
    except Exception as exc:
        info["AEDT版本"] = "未找到"
        info["AnsysEM"] = f"未找到：{exc}"
        info["_aedt_win64"] = ""
        info["_aedt_version"] = ""
        info["_aedt_version_id"] = ""
        info["_aedt_source"] = ""

    return info


def get_host_config_text() -> str:
    """Compatibility helper for console/debug use."""
    info = get_host_config_dict()
    return "\n".join(f"{k}：{v}" for k, v in info.items())



def decide_cores(tasks: int) -> int:
    logical = os.cpu_count() or 1
    return max(logical, tasks, 1)


def get_desktop_dir() -> Path:
    candidates = []
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / "Desktop")
        candidates.append(Path(userprofile) / "OneDrive" / "Desktop")
        candidates.append(Path(userprofile) / "OneDrive" / "桌面")
    candidates.append(Path.home() / "Desktop")
    candidates.append(Path.home() / "桌面")

    for p in candidates:
        if p.exists():
            return p

    p = Path.home() / "Desktop"
    p.mkdir(parents=True, exist_ok=True)
    return p


def copy_example_to_temp(example_path: Path) -> tuple[Path, Path]:
    temp_dir = Path(tempfile.mkdtemp(prefix=BENCH_TEMP_PREFIX))
    temp_project = temp_dir / example_path.name
    shutil.copy2(example_path, temp_project)

    result_dir = temp_project.with_suffix(".aedtresults")
    if result_dir.exists():
        shutil.rmtree(result_dir, ignore_errors=True)

    return temp_dir, temp_project


def inspect_result_dir(temp_project: Path) -> ResultDirInfo:
    result_dir = temp_project.with_suffix(".aedtresults")
    if not result_dir.exists():
        return ResultDirInfo(False, 0, 0, False)

    file_count = 0
    total_bytes = 0
    sweep_seen = False
    for p in result_dir.rglob("*"):
        try:
            name_low = str(p).lower()
            if SWEEP_NAME.lower() in name_low:
                sweep_seen = True
            if p.is_file():
                file_count += 1
                total_bytes += p.stat().st_size
        except Exception:
            pass

    return ResultDirInfo(True, file_count, total_bytes, sweep_seen)


def close_project_without_saving(hfss) -> None:
    try:
        hfss.close_project(save=False)
        return
    except Exception:
        pass

    try:
        hfss.close_project(save_project=False)
        return
    except Exception:
        pass

    try:
        hfss.close_project(hfss.project_name, save_project=False)
        return
    except Exception:
        pass

    try:
        hfss.odesktop.CloseProject(hfss.project_name)
    except Exception:
        pass


def flatten_messages(obj) -> list[str]:
    if obj is None:
        return []
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, (list, tuple, set)):
        out: list[str] = []
        for x in obj:
            out.extend(flatten_messages(x))
        return out

    out: list[str] = []
    for attr in (
        "global_level",
        "project_level",
        "design_level",
        "info_messages",
        "warning_messages",
        "error_messages",
        "aedt_info_messages",
        "aedt_warning_messages",
        "aedt_error_messages",
        "messages",
    ):
        if hasattr(obj, attr):
            try:
                out.extend(flatten_messages(getattr(obj, attr)))
            except Exception:
                pass
    return out or [str(obj)]


def get_aedt_messages(hfss) -> list[str]:
    try:
        msg_obj = hfss.logger.get_messages(
            project_name=getattr(hfss, "project_name", None),
            design_name=getattr(hfss, "design_name", None),
            level=0,
            aedt_messages=True,
        )
        return flatten_messages(msg_obj)
    except Exception:
        return []


def split_problem_messages(messages: list[str]) -> tuple[list[str], list[str]]:
    errors = []
    warnings = []
    for m in messages:
        s = str(m).strip()
        low = s.lower()
        if not s:
            continue
        if "error" in low or "failed" in low or "failure" in low:
            errors.append(s)
        elif "warning" in low or "warn" in low:
            warnings.append(s)
    return errors, warnings


def has_real_solve_error(messages: list[str]) -> bool:
    text = "\n".join(str(x) for x in messages).lower()
    bad_patterns = [
        "simulation completed with execution error",
        "has failed with execution error",
        "error in solving setup",
        "failed with execution error",
        "solver process",
    ]
    return any(p in text for p in bad_patterns)


def looks_solved_from_messages(messages: list[str]) -> bool:
    text = "\n".join(str(x) for x in messages).lower()
    if has_real_solve_error(messages):
        return False
    if "solved correctly" in text:
        return True
    if "normal completion" in text:
        return True
    if re.search(r"solv(?:e|ed|ing).*correct", text):
        return True
    return False


def activate_project_design_like_recorded(hfss, design_name: str = DESIGN_NAME):
    """Try to mimic RestoreWindow, SetActiveProject, and SetActiveDesign from AEDT recording."""
    try:
        hfss.odesktop.RestoreWindow()
    except Exception:
        pass

    try:
        project_name = getattr(hfss, "project_name", None)
        if project_name:
            project = hfss.odesktop.SetActiveProject(project_name)
        else:
            project = hfss.oproject
    except Exception:
        project = getattr(hfss, "oproject", None)

    try:
        if project is not None:
            return project.SetActiveDesign(design_name)
    except Exception:
        pass

    return hfss.odesign


def change_solution_type_to_hfss_and_validate(
    hfss,
    design_name: str = DESIGN_NAME,
) -> SolutionTypeInfo:
    if not CHANGE_SOLUTION_TYPE_TO_HFSS:
        return SolutionTypeInfo(False, True, "skipped", "")

    try:
        odesign = activate_project_design_like_recorded(hfss, design_name)
        print(f"切换求解器：{HFSS_MODAL_SOLUTION_TYPE}")
        odesign.SetSolutionType(HFSS_MODAL_SOLUTION_TYPE, HFSS_MODAL_OPTIONS)
        time.sleep(0.5)

        if not VALIDATE_AFTER_SOLUTION_TYPE_CHANGE:
            return SolutionTypeInfo(True, True, "skipped", "")

        validate_ret = odesign.ValidateDesign()
        try:
            passed = int(validate_ret) == 1
        except Exception:
            passed = bool(validate_ret)

        print(f"设计验证 ValidateDesign 返回：{validate_ret}，{'通过' if passed else '失败'}")
        return SolutionTypeInfo(True, passed, str(validate_ret), "")
    except Exception as exc:
        print(f"切换求解器或验证失败：{exc}")
        return SolutionTypeInfo(False, False, "exception", str(exc))


def _get_prop_ci(props: dict, names: list[str], default=None):
    if not isinstance(props, dict):
        return default
    low_map = {str(k).lower(): v for k, v in props.items()}
    for name in names:
        key = name.lower()
        if key in low_map and low_map[key] not in (None, ""):
            return low_map[key]
    return default


def configure_sweep_to_linear_count(
    hfss,
    setup_name: str,
    sweep_name: str,
    point_count: int,
    design_name: str = DESIGN_NAME,
) -> str:
    """Configure the selected sweep as a non-interpolating linear-count sweep.

    The preferred path uses PyAEDT's sweep object so that the original start/end
    frequency can be preserved. A COM fallback is kept for older PyAEDT versions.
    """
    point_count = max(1, int(point_count))
    fallback_start = "24GHz"
    fallback_end = "30GHz"

    # Preferred PyAEDT path: preserve original range and edit only sweep type/count.
    try:
        setup_obj = hfss.get_setup(setup_name)
        sweep_obj = None

        get_sweep = getattr(setup_obj, "get_sweep", None)
        if callable(get_sweep):
            try:
                sweep_obj = get_sweep(sweep_name)
            except Exception:
                sweep_obj = None

        if sweep_obj is None:
            for item in getattr(setup_obj, "sweeps", []) or []:
                if str(getattr(item, "name", "")) == sweep_name:
                    sweep_obj = item
                    break

        if sweep_obj is not None:
            props = getattr(sweep_obj, "props", {})
            start_freq = _get_prop_ci(props, ["RangeStart", "Start", "StartFrequency"], fallback_start)
            end_freq = _get_prop_ci(props, ["RangeEnd", "Stop", "StopFrequency", "End"], fallback_end)

            props["Type"] = "Discrete"
            props["RangeType"] = "LinearCount"
            props["RangeStart"] = str(start_freq)
            props["RangeEnd"] = str(end_freq)
            props["RangeCount"] = int(point_count)
            props["SaveFields"] = False
            props["SaveRadFields"] = False
            props["GenerateFieldsForAllFreqs"] = False

            update = getattr(sweep_obj, "update", None)
            if callable(update):
                update()
                return f"PyAEDT sweep update ok: Type=Discrete, RangeType=LinearCount, RangeCount={point_count}, Range={start_freq}~{end_freq}"
            print("PyAEDT sweep object 没有 update() 方法，尝试 COM fallback。")
    except Exception as exc:
        print(f"PyAEDT sweep 设置失败，尝试 COM fallback：{exc}")

    # COM fallback. If the original range cannot be read, use a safe SIW/5G range.
    try:
        odesign = activate_project_design_like_recorded(hfss, design_name)
        oanalysis = odesign.GetModule("AnalysisSetup")
        sweep_props = [
            f"NAME:{sweep_name}",
            "IsEnabled:=", True,
            "RangeType:=", "LinearCount",
            "RangeStart:=", fallback_start,
            "RangeEnd:=", fallback_end,
            "RangeCount:=", int(point_count),
            "Type:=", "Discrete",
            "SaveFields:=", False,
            "SaveRadFields:=", False,
            "GenerateFieldsForAllFreqs:=", False,
        ]
        try:
            oanalysis.EditFrequencySweep(setup_name, sweep_name, sweep_props)
            return f"COM EditFrequencySweep ok: Type=Discrete, LinearCount={point_count}, Range={fallback_start}~{fallback_end}"
        except Exception:
            oanalysis.EditSweep(setup_name, sweep_name, sweep_props)
            return f"COM EditSweep ok: Type=Discrete, LinearCount={point_count}, Range={fallback_start}~{fallback_end}"
    except Exception as exc:
        return f"sweep update failed: {exc}"



def _safe_update_pyaedt_object(obj) -> str:
    update = getattr(obj, "update", None)
    if callable(update):
        try:
            ok = update()
            return f"update={ok}"
        except Exception as exc:
            raise RuntimeError(f"PyAEDT object update failed: {exc}") from exc
    return "no update method"


def _set_props_best_effort(obj, props: dict) -> str:
    target = getattr(obj, "props", None)
    if not isinstance(target, dict):
        return "props unavailable"

    changed = []
    for key, value in props.items():
        try:
            target[key] = value
            changed.append(key)
        except Exception:
            pass
    update_msg = _safe_update_pyaedt_object(obj)
    return f"props set: {','.join(changed)}; {update_msg}"


def _try_delete_setup_pyaedt(hfss, setup_name: str) -> str:
    """Delete an old benchmark setup through PyAEDT, without COM recorded calls."""
    try:
        setup_names = [str(x) for x in list(getattr(hfss, "setup_names", []) or [])]
    except Exception:
        setup_names = []

    if setup_name not in setup_names:
        return f"delete skipped: {setup_name} not found"

    delete_setup = getattr(hfss, "delete_setup", None)
    if callable(delete_setup):
        ok = delete_setup(setup_name)
        if ok is False:
            raise RuntimeError(f"delete_setup({setup_name!r}) returned False")
        return f"deleted old {setup_name}"

    setup = hfss.get_setup(setup_name)
    delete = getattr(setup, "delete", None)
    if callable(delete):
        ok = delete()
        if ok is False:
            raise RuntimeError(f"setup.delete({setup_name!r}) returned False")
        return f"deleted old {setup_name} by setup.delete()"

    raise RuntimeError("no PyAEDT delete setup API is available")


def _looks_like_interpolating_sweep_not_supported(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = (
        "interpolating sweeps are not supported",
        "problems with no ports",
        "no ports",
    )
    return any(marker in message for marker in markers)


def _create_one_linear_count_sweep_pyaedt(
    hfss,
    setup_name: str,
    sweep_name: str,
    point_for_sweep: int,
    sweep_type: str,
):
    start_ghz = float(str(BENCHMARK_SWEEP_START).lower().replace("ghz", ""))
    end_ghz = float(str(BENCHMARK_SWEEP_END).lower().replace("ghz", ""))

    create_sweep = getattr(hfss, "create_linear_count_sweep", None)
    if not callable(create_sweep):
        raise RuntimeError("hfss.create_linear_count_sweep is not available")

    try:
        return create_sweep(
            setup=setup_name,
            unit="GHz",
            start_frequency=start_ghz,
            stop_frequency=end_ghz,
            num_of_freq_points=point_for_sweep,
            name=sweep_name,
            sweep_type=sweep_type,
            save_fields=False,
            save_rad_fields=False,
        )
    except TypeError:
        # Signature fallback only. If AEDT itself rejects the sweep type, the
        # non-TypeError exception is intentionally allowed to propagate.
        return create_sweep(
            setup=setup_name,
            unit="GHz",
            start_frequency=start_ghz,
            stop_frequency=end_ghz,
            num_of_freq_points=point_for_sweep,
            name=sweep_name,
        )


def _create_linear_count_sweep_pyaedt(
    hfss,
    setup_name: str,
    sweep_name: str,
    point_count: int,
    interpolating: bool,
):
    """Create a sweep and return (sweep, actual_interpolating, point_count, note).

    Some AEDT 2025 example designs are seen by AEDT as "no ports" when a new
    PyAEDT HFSSDriven setup is created. AEDT rejects Interpolating sweeps in
    that condition, while Discrete sweeps are still allowed. Therefore Tasks=1
    first tries Interpolating and automatically falls back to an 11-point
    Discrete baseline when AEDT reports that specific limitation.
    """
    if not interpolating:
        point_for_sweep = int(point_count)
        sweep = _create_one_linear_count_sweep_pyaedt(
            hfss,
            setup_name,
            sweep_name,
            point_for_sweep,
            "Discrete",
        )
        return sweep, False, point_for_sweep, ""

    try:
        point_for_sweep = int(BENCHMARK_INTERPOLATING_RANGE_COUNT)
        sweep = _create_one_linear_count_sweep_pyaedt(
            hfss,
            setup_name,
            sweep_name,
            point_for_sweep,
            "Interpolating",
        )
        return sweep, True, point_for_sweep, ""
    except Exception as exc:
        if not _looks_like_interpolating_sweep_not_supported(exc):
            raise

        fallback_points = int(point_count)
        fallback_name = sweep_name
        print(
            "Interpolating sweep 创建失败，AEDT提示当前问题不支持插值扫频；"
            f"自动回退为 Discrete LinearCount，点数={fallback_points}。原始错误：{exc}"
        )
        sweep = _create_one_linear_count_sweep_pyaedt(
            hfss,
            setup_name,
            fallback_name,
            fallback_points,
            "Discrete",
        )
        note = (
            "Interpolating rejected by AEDT, fallback to Discrete LinearCount "
            f"with points={fallback_points}; original_error={exc}"
        )
        return sweep, False, fallback_points, note


def create_benchmark_setup_and_sweep(
    hfss,
    setup_name: str,
    sweep_name: str,
    point_count: int,
    design_name: str = DESIGN_NAME,
    interpolating: bool = False,
) -> tuple[str, str, str]:
    """Create the benchmark setup/sweep with PyAEDT only."""
    point_count = max(1, int(point_count))
    delete_msg = _try_delete_setup_pyaedt(hfss, setup_name)

    setup = hfss.create_setup(
        name=setup_name,
        setup_type="HFSSDriven",
        Frequency=BENCHMARK_SETUP_FREQUENCY,
    )
    setup_name = str(getattr(setup, "name", setup_name))

    setup_msg = _set_props_best_effort(
        setup,
        {
            "SolveType": "Single",
            "Frequency": BENCHMARK_SETUP_FREQUENCY,
            "MaxDeltaS": 0.02,
            "MaximumPasses": 6,
            "MinimumPasses": 1,
            "MinimumConvergedPasses": 1,
            "PercentRefinement": 30,
            "BasisOrder": 1,
            "DoLambdaRefine": True,
            "DoMaterialLambda": True,
            "SetLambdaTarget": False,
            "Target": 0.3333,
            "DrivenSolverType": "Direct Solver",
            "SaveFields": True,
            "SaveRadFieldsOnly": False,
            "SaveAnyFields": True,
        },
    )

    sweep, actual_interpolating, point_for_sweep, fallback_note = _create_linear_count_sweep_pyaedt(
        hfss,
        setup_name,
        sweep_name,
        point_count,
        interpolating,
    )
    sweep_name = str(getattr(sweep, "name", sweep_name))

    sweep_type = "Interpolating" if actual_interpolating else "Discrete"
    sweep_props = {
        "Type": sweep_type,
        "RangeType": "LinearCount",
        "RangeStart": BENCHMARK_SWEEP_START,
        "RangeEnd": BENCHMARK_SWEEP_END,
        "RangeCount": point_for_sweep,
        "SaveFields": False,
        "SaveRadFields": False,
        "GenerateFieldsForAllFreqs": False,
    }
    if actual_interpolating:
        sweep_props.update(
            {
                "InterpTolerance": 0.5,
                "InterpMaxSolns": 250,
                "InterpMinSolns": 0,
                "InterpMinSubranges": 1,
                "InterpUseS": True,
                "InterpUsePortImped": False,
                "InterpUsePropConst": True,
                "UseDerivativeConvergence": False,
                "InterpDerivTolerance": 0.2,
                "UseFullBasis": True,
                "EnforcePassivity": True,
                "PassivityErrorTolerance": 0.0001,
            }
        )
    sweep_msg = _set_props_best_effort(sweep, sweep_props)

    if actual_interpolating:
        desc = (
            f"PyAEDT Interpolating LinearCount {BENCHMARK_SWEEP_START}~{BENCHMARK_SWEEP_END}, "
            f"RangeCount={BENCHMARK_INTERPOLATING_RANGE_COUNT}, reported_effective_points={point_count}"
        )
    elif interpolating:
        desc = (
            f"PyAEDT Discrete LinearCount fallback for Tasks=1 baseline "
            f"{BENCHMARK_SWEEP_START}~{BENCHMARK_SWEEP_END}, points={point_for_sweep}, "
            f"reported_effective_points={point_count}"
        )
    else:
        desc = (
            f"PyAEDT Discrete LinearCount {BENCHMARK_SWEEP_START}~{BENCHMARK_SWEEP_END}, "
            f"points={point_count}"
        )

    note_text = f"; {fallback_note}" if fallback_note else ""
    return (
        setup_name,
        sweep_name,
        f"created {setup_name} : {sweep_name}; {desc}; {delete_msg}; {setup_msg}; {sweep_msg}{note_text}"
    )



# ---------------------------------------------------------------------------
# Embedded FilterSolutions model builder
# ---------------------------------------------------------------------------

FILTER_PROJECT_NAME = "Embedded_FilterSolutions_Benchmark"
FILTER_DESIGN_NAME = DESIGN_NAME
FILTERSOLUTIONS_TXT_ZLIB_B64 = (
    "eNrtXVtzG7mxfpZ+xdTmxQpHCu4XJXxYX9Zxlb3lshzbOqdcWzQ1tllLkQpJra1T+fGnMTfODDCYAW+ykmwqu9QMGmg0uhv9oQHMn+jT6NX8KplGz5NZshitkqvo0130y2S6ShYX8+ntajKfLf8a/Txb3i3j6MVsfBY9+vbt29nIPDgbz69Pjv/083QaXU2uk9nSFI5GiySazKLb2WS1jOafo7//XxxdJ1AfVHCVfImj+ddr+LmcJIYkjj6PFqMr+O9odhV9TWaLSbI8Pn48/35+fPRhNV+NpkN0hqgSSFONJcOSMnF8dFm+IohgjJDCQhHFMFCNk+n0/eTKvETwmlOhMeIIaUIMZfFaninNNOFIcEm5Vs9OEc+pf729HmKl8sLmL8KB9MOn28+fk0VaMTVvK39zSo6PL1aj8e+3N9PRHXR3SKALb+c3T+Z/mDLHRy/N4//FH+Hx0SuQ9WICPXidLD4n41X0ZD67uh2v5gt4+RZkYQiOni2G0KGjvyeTL19XwLAAHpV5Pk3+GJmxGZ6WD7PaSb32fyyTRVT8VdYM7GKRVU/ONFu3kEpMiloLho9XiRH2k/nNTWL4e/N1nv1Oucs5n/wxWd0NuULpP6atr5Px70N6xlPBHr+Gcc70YMjY+fGL2VVyk8C/Zqto/QqYL//IJfUr/DWE8YKfT5PleDG5SdmCJ6uvRr/eJMv5bASCW4twCWX/GE1vk7RDhHAYZcYRJpIj09vV7SwZfh5Nl0m1ObJu7jnubu0iuQFS87bWGiYMYy4lJ0IQTYvWVotbaOzoejJ7VxZFApRTKiUFpZgJad6Pvq/fE6YIxZwb3eLKvH6ezIHXxR2wAXqXzMaJkRiI+u4mOT/6+dcn+Pzol8liCSKdT0CwEzDH2fjrfJFcnUXR01zgYzCxaDof/w7WvppHy2QMgotuDIWp7OlkAQoJ/To/+vD0xZvzo7SuZXQNemws+wNYe14ims+md4bm1zn0bDQ9P3KL4KjkN2cMp2yDiD9PwE+kbb2eT+++gKj50btksUq+p7pliqyZPod2ZomrPtJeHynqI3Z96ZOi5HmE0qITI9WIxLTxFlfeohjHUCJmMY9FoxxplGvWQ+16GiVYW0uxjFWsYwwP8b/buGPpGaiNBh55NKk5ZqgmcntICO4cNUL6DluViPbVqoZSVD0XrXgusrnnEuCSmCAYwWSLiNdzGS8PvglxRuGXbnouSsxTwgiW8Is9HM9liSBEg1Whb2g3Clx6QnpPnovs03dVaHiTpvFedLYqH6jH3ErfMPMoyCYKt3bBqFPjcFXkriHBqnPUsN7EY6JuT/wjO2tWcdZ0C2ctkZKYIgkhpGZ+Z03BTXPBKSdKC2U5awqgihGBzez9kMJMSwRBxoN3HGeq+44z781bW7Xu0F9XaFRHXK27/Q16qNPEbjR9R4H1et7pDqwx6wiscbc6YbGBtmDZXe9/ZyjnDMXXMxTMQy+TWWOWgidfsmnqyfz2ZgpKX05X9ZUXSTGWnApQX6SR9E1SXDHNEKBGKpA0y3T1OUpJyTBiDKIliJVU5xx1cfnq/OjxHLjMbe2VsbWLu2tDMxmPptO7dvv9ZOhS6102zPfSZb6XXeZri2Gj9ZCweaVFnzeYW+x4fP/zis/32/zs0vNXibC31Zprc7w+hGOzm605H8frH83xFAzsBxfXu0J9kcw9xDDUp3CO190RH6abaBTzNyv9rw8zi9LqDCXWM9Tb0U1jeno6Wa5GRpHAlT9JZlDeTFVZuQqA4iYxoBSH0EoJ7ZubMCZaEGkyRVRy0pybOKAmDPhKI64pofeAnzablmwJhExLZNdBZVEf29M0l7uif6sxQLvOldD+o9DmyLsmhXQc1qYs16b8Mvm+ag01397OJrMv0cXq9lPNjsE6KcSEWihNFVWi1ZCbZouZIJRDcEYZwgp7zPbZP2/TBZgo5fIoZ+EH0yTJCMeCCco1k0xU+T4/ysL4QS7g+zRzsuHc/R8/Djs3ddR/rbc2FdcmYrW23tXoxpOlh+k3Gq2iZzNjX69zaVes2GynqOblj5927Q7Q66bfvW80++7Fz9Gb0dXkdmnG+OVklkQZJ2/MQFRapry24QCt61wmX14mn1e/NXcC/JIkVxfJl+QaOPtQ1nQKncCaIiY0V+DLhayJCeNaxcCLt94Xs4zdesCiGSdEK8YVobLmQ3Fl68IiWb6d3/yGGtWvF5ThLQzAclJbUj5NF4IFB+aFouAWGa93gNY6YFrwdeCyrLhgZ5Cp/inowWkhgb+QM6No4/n1za3ZeQRlb6erPORSGBhCHFihWtZYYY1BmnXJMptEasI06lapktfU2FWl0V9HPdiEg5WKRG0gnAq0Hgnz2h6KUvEGRe8GBU+D0aflo0J6p5nBnbhEmKojYaAyQiCIW1lDz2WNzYvkxsOlIwGRbo9xj1tzC0ClUVVr1GUD60br6p/5FWeD1uaeSoPaHgwSOBjlIA4KlgcVgTnljpTGAAWolBgjzmu6S5AleBIkeNIq+GYmsdIotgRP9ip4QmzB0w0FTwrBk0FFYG7BcwBgkoPTUFoCKquxRC3B0yDB03bBN9bmK40yS/B0v4LntuDZhoKnheDpoCIwt+DrALlm9URYgmf7F7y0BM/2K3hlC55vKHhWCJ4NKgJzc6TA5rUCJwNhB6tzpC258717GoosufO9yp1iW+5iQ7nzQu58UBGYkyMhwNFoTYRWFNVmVkossYu9z6yUWmIX+xU7s8UuNxS7KMQuBhWBuTjCRvUICMGEv6oudm5JQO5XAsKKGEl3xFiJLd1jXI8mqbRQg6sRiHSvDU6q96sCM9xttcMJqqxgn9xTsE+1jchIL0RW6mWhX7IIo0k1jCa+MNooHedtoI4hC4iQICBSQS9OudRBCqv4utfzxQr/9qHRmnka4ehD9HI+HrkxRStYaMeujDTbvXS3e+lsN4WKgx5aQDCnAkstBcO8bt6M1lkg7q4TX9fJoDJGzpH2iIA12790t98uAlKIgASI4Pj5fDRdDik0bn4VR0eS0fJ2YU7PrIY/PX386F8Xj0iMT/518tNxthg2/Olv5uf74kyHmcd+WST/vE1m47uL1WixGgqzcZeaCOqMUqVFvcT8ZigkRppR+OcMZYcm0pXTjNpoDF8/guKnJHuUskl2xibnWlMYFCrPOMdKW2xyafWjyiexGa3wSdv4xB4+sYNL2SVM7uUS6zOGKQbUqBjDiNYZtt4e5+vUk2Q5JBy6YPQ7fpNy9+jU9lmx/egkO5kUP56vVvPrjAoxqUwpaAYzgnjseGbo0mkmzg8MPTJns8B5V85mpXSay8qZLGzoitV10GKYjMer0ezL1CxaGh+ZLtHB7/QMU7bCmR6lSi0uPXyUuq635sxXZHW3brixQwQnJXG11/bckzFPMRGEEgW6R2SV2Oo6uOjYMZmuKf5hTknFL+ffoFuPstNVOpMsyooZSWQ9As7c/XGwWVKGcFQKvb6iavFQThZx4b5PylL19ooJLC7cuymYLZhnCaQPl2a0Q3plSIiHxDE2hoR+bFX9NhLmIXEwli2oNySX969FYnlX7LeDqrwK9u11v/byzF/+pGJtwEG+jr+xfTUE5Fhc3J99IVVV4y2NLVdN2VTNfhpQU83+elaqJgWWA1WzLuWUhFBWiRFYQcJ9JLWwoiQRHhKshYtEevqyqc041dc2nh5W4baicvXcb07lSq9d7NRac4cYrip4D22lpHCX9FUue/YoFXYh5jTXloo+2xxTugJan3g3cwYNZTm1gbnbGWQZJik0UVIhwhjRJHZknXzeoAHIY0e+dcup19G7JtOeubcnf30n4rWyFMD61GSO22bjIkMRV7LMvgm5Z19rbs/hKhzjV3N7deXwkbAWkjbOfO7FJ7u8Qy1FBg3xFZ2xU0HddKwHXd120yEdvk9z8o+T1bckmUVZ7n2SbehEteLZioLZJvPXdGtMvoemNHl2nybvGGonqAi0+MZ2vN1afIt6Bhi8g73NDT5Xqk6TL37kc0dPk/fYos/iHePXZfEtJCzMSYQZfFV2rSZfWG1DgD2M3kPJelFWo3SIIp5MFuOGkb6bjJ59X5mFibXJpkpetds4Sp+nap+dKpxd/QYm6LFnpBCXUpnUEcnjecS0RloLrTTXst2eiRIcUyk5VKGIzAaXgYJgrhHQIuUzZ7NTFlQBQzGwmzxurD/zmjMAAPM+E1W+uTfvFIYoWlFzToQh0aZAOWG2NSi/7UQjSeH/HDpBBFsXaqhZo017eGH8napXb/Pd+z9XNj/kdopJ01Drw9HLUJFA3NyTwySg5tzqEAf7BTeECZFC2oaqNPRaEyqEIjojoSA+mGoFMv8jlqHaYrZHv4ZHMNgzKBwgHfCL2NdGiUewhkCJKm12x0pvT0o80lBpW14lhFGeVigxMpREc9DHkkJ7ukK0xKCWEglmoE9BglG7vBoWVJJgz6i0NeMbfKszpbMROwgKGrtc8iCtnhjFPiTgiAu2RQL1Y1bbxQXu/m0XGTgYDI0MSOliOkICEq9dUEs00LuLVTdjEfWAAE216IMBLJrTLWKChtQawQBpixiqAcB6H1JbaeYvvVWY/3x0k+/IKArna3pxhegRGPg5j1OBnLOTOP1b5H83lgjWuKHiF+QP4BeaSYjTHTiGnQIGdwcd8e1hIcNa0U4L1SsCUp+TaJTdq6c47YEd3GbfgR725CssQbb6jWrJgYOqw5F0kbP+5NVpXh0QUyAF4QwGiUO4ihnNrV5rJRREPhywnZQ+q6/FkBm1I046PKxAEFNqRgTEfUwoRFtVa/fIglRVoT6BtUEK4oEUzdHoZ8MQdhrmqTmXL0rDd4bJpQlDpyFcB1sk0JjOB9MdwJYW7BRzS6TMSweDFZOAImCkNQTn3pZEScQkI0RApAwscuLrkfzoUe4WySlvOy0wRrt7lI9RC1wqMYZbeG5YVqIM5yi1teRXB7tTpQvSu4goatu6S4WqbX3lDxlpOPt370iD9kUatAfS6NvFmu9pEvVBGg216IU0mjTbRA/UizRoH6Sx3njfB2k4Sm+PNGgfpKEaSEMHIA3ju+7TMTyEzERf/T8szqCBqQlaQAzaATECrN3rIboAhstDdOGLnl4lzEF40hO0LclAW0BFu89wUbK+3mZtr/iQWIJJgpRgSkHspwnLHTKGqMNsNiEK/o0fXoYCAf9gjMAW6AzR6nA5ClpVgqAcBfUBisaI9LJYSpi5zFwoTCguLNafpUCUUxNEEwjpkVasT5rCKWt/ogKqBHepoS0IvwXulamAYBibLwhoc8Emy7e+dqQqLO3ukatwtuNPVji705WtcIqtK1/hHJ+uhIVTEdozFoZu+3ihMf35p+yHhyOa3fsxcATriyNYDxzRt4s1n+Madg+OCCRhLSTboAjmRRGsD4pYnyLtgyIcpQ+EIsAz1WEEJiE4gt6/X/jxMxZO1bz3jAWzFrBZd8aCxY2ye/UUXRmLUJr9+gpLjK1+o1py4KDqcCRd5Kw/eW2SZ4cEGY2gmsauhw8tZYEOmJ9g1aHvl59gLXDCOR5dFpt9QUkxZc7GQ7SJmerMT2RXTWnzRTCTmITQk9LOBAXql40IqrdEECE9KCFEkKyUv6mulIS7W76cBOpMQITX6x9wTwYC8y0jhLb1n8ba0wMFDiHr34fFDbwvbuB+3BDSw6prCcgkFJ4lYAmTtZBsEwlwL2rgfVDD+g6UPqjBUfpAu5wwa6IGHoIaxH36hB899xCg/IcFDDww9cALrMDbsUKgpfucQwdOCMhVsDB/EuYbPGkH3pY84C3YoN1duChZX0ezNlR5IETQtpzdWKt9WEkHe0FXHi7nwKsaEJRz4B6Q0L12bplqwBJ9YaoBmYDCVF1ybs83BGY1REHSP3kiP7YrtSfZ0HtlvsQIoQkA5BGYL9MQmtLwDb8nz6B2EBm4drs39sI+YLTQd6/tYdGC6IsWRDda6NvDqpcJOOFQeJmAgxSshWQbtCC8aEH0QQvrq/v6oAVH6e3RAu6FFmQTLagQtKDv3Sf80CmGkF37hwUMwlpyFt0ZBhE3yu7RS/TJL4Scotivn7CE2OozqiUHDqoOJ9JFzvqT125CQgdEE83d9tY2cqIfXHLhXo9DiKom9Es3CA+SaIxFL/Otb+nHfZINIUcUCusNPQsReOaiQBP1vfysK/EQeoRE+ZrxJB1CzyYgn9Q6chDO0elKQTi1wJOBIHj7+MFxP0avG9h++EuXAm77OeydSzLoziXZ486lgK5WnE7A9Um5zwm45IW5Kba5b0l237ckw+5bWl/gHHbfUhvd9qBD9gEdBDVAB8FdoMN9hRMh9+c/HkCuoqeBHBZ5yMBUhSxAh/SCjgBn4HEffsARcOETC3I4Yd7Dk6aQbckG2QIxHJ7AQ8l6UdZmeHo4YOG4Gahx+c2DS1Lc19VNsjr6QSkK2Q4sum8isky0/3VHhYn2v1OpMNGQa5vCboYSBUXv66fkxzZN7khN9LrlqAokQq5SQq2S6khLhFwL5Rny9qQEYVtd4ez20Ae7IL12gbPc/QXO4fc3h1/fHH57c/jlzeF3N4df3bybm5tDv6xS3FJMnJN5aCXldzT8M31gJS2TvlX+1Kqz5SpoXyUtd0K7SHzNyQ06XvMrfIcfYmj57ET9Bv8H8xmG/k5l719hIHXzaf8KA4mrSt0CIA7hKft/tYG1U2x4mzxxOhv7rdOL2J8K8jsMT/mT4+Nn0/SbMhCrnh8fm+8DLYfmc8w38MuwbKDBYjaaQn3Prr4k5pGprPIZ8hezq/xzz2nP139+uPyf4nNq7R/KaEwcubJDFJOJcuGQ5PBR/l2pOP/O00nOLrHZNYsSRv0sfgmvM0ybDO+aX5LzSwy//w/PiKdW"
)

MODEL_TRANSPARENCY = 0.7
COPPER_COLOR = "(255 255 128)"
VIA_COLOR = "(0 0 255)"
SUBSTRATE_COLOR = "(132 160 132)"
PORT_COLOR = "(255 0 0)"
AIRBOX_COLOR = "(128 192 255)"


def get_embedded_filtersolutions_text() -> str:
    return zlib.decompress(base64.b64decode(FILTERSOLUTIONS_TXT_ZLIB_B64)).decode("utf-8", errors="ignore")


def fs_mm_from_m(value_m: float) -> float:
    return value_m * 1000.0


def fs_mm(value_m: float) -> str:
    return f"{fs_mm_from_m(value_m):.12g}mm"


def fs_ghz(value_hz: float) -> str:
    return f"{value_hz / 1e9:.12g}GHz"


@dataclass
class FsLayer:
    index: int
    material: str = ""
    tand: float | None = None
    er: float | None = None
    height: float | None = None
    elevation: float | None = None
    metal: str | None = None
    conductivity: float | None = None
    thick: float | None = None


@dataclass
class FsGeometry:
    index: int
    kind: str
    layer: int | None = None
    points: list[tuple[float, float]] = field(default_factory=list)
    via_extent: bool = False
    circle_center: tuple[float, float] | None = None
    circle_radius: float | None = None
    z_upper: float | None = None
    z_lower: float | None = None

    @property
    def xmin(self) -> float:
        return min(x for x, _ in self.points)

    @property
    def xmax(self) -> float:
        return max(x for x, _ in self.points)

    @property
    def ymin(self) -> float:
        return min(y for _, y in self.points)

    @property
    def ymax(self) -> float:
        return max(y for _, y in self.points)


@dataclass
class FsPort:
    index: int
    geometry_index: int | None = None
    point_index: int | None = None
    position: tuple[float, float, float] | None = None


@dataclass
class FsModel:
    box: dict[str, float]
    layers: dict[int, FsLayer]
    geometries: dict[int, FsGeometry]
    ports: list[FsPort]
    goal_freqs_hz: list[float]

    @property
    def substrate_layer(self) -> FsLayer:
        candidates = [
            layer for layer in self.layers.values()
            if (layer.height or 0) > 1e-9 and (layer.er or 1) > 1.0001
        ]
        if not candidates:
            raise ValueError("No dielectric substrate layer was found in the embedded FilterSolutions data.")
        return sorted(candidates, key=lambda x: x.index)[0]

    @property
    def top_metal_layer(self) -> FsLayer:
        candidates = [layer for layer in self.layers.values() if layer.metal or layer.conductivity or layer.thick]
        if not candidates:
            return self.substrate_layer
        return sorted(candidates, key=lambda x: x.index)[-1]


def fs_parse_float(s: str) -> float:
    return float(s.strip())


def fs_parse_key_value_block(text_value: str, header_regex: str) -> dict[str, float]:
    lines = text_value.splitlines()
    out: dict[str, float] = {}
    in_block = False
    for line in lines:
        if re.match(header_regex, line):
            in_block = True
            continue
        if in_block and line and not line.startswith("\t") and not line.startswith(" "):
            break
        if in_block:
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(.+)", line)
            if m:
                try:
                    out[m.group(1)] = fs_parse_float(m.group(2))
                except ValueError:
                    pass
    return out


def fs_parse_layers(text_value: str) -> dict[int, FsLayer]:
    layers: dict[int, FsLayer] = {}
    current: FsLayer | None = None
    in_stack = False
    for line in text_value.splitlines():
        if line.startswith("Stackuplayers="):
            in_stack = True
            continue
        if in_stack and line.startswith("Parameters="):
            break
        if not in_stack:
            continue
        m = re.match(r"\s*Layer\[(\d+)\]:", line)
        if m:
            current = FsLayer(index=int(m.group(1)))
            layers[current.index] = current
            continue
        if current is None:
            continue
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(.+)", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "Material":
            current.material = val
        elif key == "Metal":
            current.metal = val
        elif key == "Tand":
            current.tand = fs_parse_float(val)
        elif key == "Er":
            current.er = fs_parse_float(val)
        elif key == "Height":
            current.height = fs_parse_float(val)
        elif key == "Elevation":
            current.elevation = fs_parse_float(val)
        elif key == "Conductivity":
            current.conductivity = fs_parse_float(val)
        elif key == "Thick":
            current.thick = fs_parse_float(val)
    return layers


def fs_parse_geometries(text_value: str) -> dict[int, FsGeometry]:
    geometries: dict[int, FsGeometry] = {}
    current: FsGeometry | None = None
    seen_point_indices: set[int] = set()
    for line in text_value.splitlines():
        m = re.match(r"\s*Geometry\[(\d+)\]=(\w+)", line)
        if m:
            current = FsGeometry(index=int(m.group(1)), kind=m.group(2))
            geometries[current.index] = current
            seen_point_indices = set()
            continue
        if current is None:
            continue
        if line.startswith("Elements=") or line.startswith("Ports="):
            current = None
            continue
        m = re.match(r"\s*Layer=(\d+)", line)
        if m:
            current.layer = int(m.group(1))
            continue
        m = re.match(r"\s*ViaExtent=(\d+)", line)
        if m:
            current.via_extent = int(m.group(1)) != 0
            continue
        m = re.match(r"\s*Circle Center=\(([-+0-9.Ee]+),([-+0-9.Ee]+)\)", line)
        if m:
            current.circle_center = (float(m.group(1)), float(m.group(2)))
            continue
        m = re.match(r"\s*Circle Radius=([-+0-9.Ee]+)", line)
        if m:
            current.circle_radius = float(m.group(1))
            continue
        m = re.match(r"\s*Total Upper,Lower=\(([-+0-9.Ee]+),([-+0-9.Ee]+)\)", line)
        if m:
            current.z_upper = float(m.group(1))
            current.z_lower = float(m.group(2))
            continue
        m = re.match(r"\s*XY\[(\d+)\]=\(([-+0-9.Ee]+),([-+0-9.Ee]+)\)", line)
        if m:
            point_index = int(m.group(1))
            if point_index not in seen_point_indices:
                current.points.append((float(m.group(2)), float(m.group(3))))
                seen_point_indices.add(point_index)
    return geometries


def fs_parse_ports(text_value: str) -> list[FsPort]:
    ports: list[FsPort] = []
    current: FsPort | None = None
    in_ports = False
    for line in text_value.splitlines():
        if line.startswith("Ports="):
            in_ports = True
            continue
        if not in_ports:
            continue
        m = re.match(r"\s*port\[(\d+)\]=", line)
        if m:
            current = FsPort(index=int(m.group(1)))
            ports.append(current)
            continue
        if current is None:
            continue
        m = re.match(r"\s*GeometryIndex=(\d+)", line)
        if m:
            current.geometry_index = int(m.group(1))
            continue
        m = re.match(r"\s*PointIndex=(\d+)", line)
        if m:
            current.point_index = int(m.group(1))
            continue
        m = re.match(r"\s*XYZPosition=\(([-+0-9.Ee]+),([-+0-9.Ee]+),([-+0-9.Ee]+)\)", line)
        if m:
            current.position = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
            continue
    return ports


def fs_parse_goal_freqs(text_value: str) -> list[float]:
    freqs: list[float] = []
    for line in text_value.splitlines():
        m = re.match(r"\s*Frequency(?:Start|Stop)=([-+0-9.Ee]+)", line)
        if m:
            freqs.append(float(m.group(1)))
    return freqs


def parse_embedded_filtersolutions_model() -> FsModel:
    text_value = get_embedded_filtersolutions_text()
    box = fs_parse_key_value_block(text_value, r"^Box:")
    layers = fs_parse_layers(text_value)
    geometries = fs_parse_geometries(text_value)
    ports = fs_parse_ports(text_value)
    goal_freqs_hz = fs_parse_goal_freqs(text_value)
    if not box:
        raise ValueError("Box section was not found in embedded FilterSolutions data.")
    if not geometries:
        raise ValueError("Geometries section was not found in embedded FilterSolutions data.")
    return FsModel(box=box, layers=layers, geometries=geometries, ports=ports, goal_freqs_hz=goal_freqs_hz)


def fs_q(name: str) -> str:
    return f'"{name}"'


class EmbeddedFilterHfssBuilder:
    def __init__(self, model: FsModel, version: str | None = None, visible: bool = False):
        try:
            import win32com.client as win32
        except ImportError as exc:
            raise RuntimeError(
                "pywin32 is required for embedded COM model construction. Install it with: pip install pywin32"
            ) from exc

        progids = []
        if version:
            progids.append(f"Ansoft.ElectronicsDesktop.{version}")
        progids.append("Ansoft.ElectronicsDesktop")

        app = None
        last_error = None
        for progid in progids:
            try:
                app = win32.Dispatch(progid)
                break
            except Exception as err:
                last_error = err
        if app is None:
            raise RuntimeError(f"Cannot start AEDT COM server: {last_error}")

        self.model = model
        self.app = app
        self.desktop = app.GetAppDesktop()
        if visible:
            try:
                self.desktop.RestoreWindow()
            except Exception:
                pass
        self.project = None
        self.design = None
        self.editor = None
        self.def_manager = None
        self.boundary = None
        self.analysis = None
        self.ports_ok = False
        self.metal_objects: list[str] = []
        self.port_sheet_names: list[str] = []
        self.via_objects: list[str] = []
        self.substrate_mat_name = "FS_Alumina"
        self.metal_mat_name = "FS_Metal"

    @staticmethod
    def attributes(name: str, material: str, color: str, transparency: float, solve_inside: bool):
        return [
            "NAME:Attributes",
            "Name:=", name,
            "Flags:=", "",
            "Color:=", color,
            "Transparency:=", transparency,
            "PartCoordinateSystem:=", "Global",
            "UDMId:=", "",
            "MaterialValue:=", fs_q(material),
            "SurfaceMaterialValue:=", fs_q(""),
            "SolveInside:=", solve_inside,
            "ShellElement:=", False,
            "ShellElementThickness:=", "0mm",
            "ReferenceTemperature:=", "20cel",
            "IsMaterialEditable:=", True,
            "UseMaterialAppearance:=", False,
            "IsLightweight:=", False,
        ]

    def new_design(self):
        self.desktop.NewProject()
        self.project = self.desktop.GetActiveProject()

        insert_errors = []
        used_solution_name = None
        for solution_name in ("HFSS Modal Network", "Modal Network", "HFSS", "DrivenModal"):
            try:
                self.project.InsertDesign("HFSS", FILTER_DESIGN_NAME, solution_name, "")
                used_solution_name = solution_name
                print(f"INFO: InsertDesign used solution string: {solution_name}")
                break
            except Exception as err:
                insert_errors.append((solution_name, err))
        if used_solution_name is None:
            raise RuntimeError(f"InsertDesign failed for all attempted HFSS solution strings: {insert_errors}")
        if used_solution_name != "HFSS Modal Network":
            print("WARNING: AEDT rejected 'HFSS Modal Network' and used a fallback.")

        self.design = self.project.SetActiveDesign(FILTER_DESIGN_NAME)
        self.force_hfss_solution_type()
        self.editor = self.design.SetActiveEditor("3D Modeler")
        self.def_manager = self.project.GetDefinitionManager()
        self.boundary = self.design.GetModule("BoundarySetup")
        self.set_model_units()
        self.add_materials()

    def force_hfss_solution_type(self):
        used_args = None
        for args in [("HFSS Modal Network",), ("Modal Network",), ("HFSS",), ("DrivenModal",), ("Modal",), ("HFSSDrivenModal",)]:
            try:
                self.design.SetSolutionType(*args)
                used_args = args
                print(f"INFO: requested solution type with SetSolutionType{args}.")
                break
            except Exception as err:
                print(f"INFO: SetSolutionType{args} was rejected: {err}")
        if used_args != ("HFSS Modal Network",):
            print("WARNING: SetSolutionType did not accept the exact 'HFSS Modal Network' string first.")
        try:
            print(f"INFO: AEDT reports solution type: {self.design.GetSolutionType()}")
        except Exception:
            pass

    def set_model_units(self):
        self.editor.SetModelUnits(["NAME:Units Parameter", "Units:=", "mm", "Rescale:=", False])

    def add_materials(self):
        sub = self.model.substrate_layer
        try:
            self.def_manager.AddMaterial(
                [
                    "NAME:" + self.substrate_mat_name,
                    "CoordinateSystemType:=", "Cartesian",
                    ["NAME:PhysicsTypes", "set:=", ["Electromagnetic"]],
                    "permittivity:=", str(sub.er if sub.er is not None else 1.0),
                    "dielectric_loss_tangent:=", str(sub.tand if sub.tand is not None else 0.0),
                ]
            )
        except Exception:
            pass

        metal = self.model.top_metal_layer
        try:
            self.def_manager.AddMaterial(
                [
                    "NAME:" + self.metal_mat_name,
                    "CoordinateSystemType:=", "Cartesian",
                    ["NAME:PhysicsTypes", "set:=", ["Electromagnetic"]],
                    "conductivity:=", str(metal.conductivity if metal.conductivity is not None else 5.8e7),
                ]
            )
        except Exception:
            pass

    def create_box(self, name: str, x: str, y: str, z: str, dx: str, dy: str, dz: str, material: str, color: str, transparency: float, solve_inside: bool):
        self.editor.CreateBox(
            ["NAME:BoxParameters", "XPosition:=", x, "YPosition:=", y, "ZPosition:=", z, "XSize:=", dx, "YSize:=", dy, "ZSize:=", dz],
            self.attributes(name, material, color, transparency, solve_inside),
        )

    def create_rectangle(self, name: str, x: str, y: str, z: str, width: str, height: str, axis: str, material: str, color: str, transparency: float):
        self.editor.CreateRectangle(
            [
                "NAME:RectangleParameters",
                "IsCovered:=", True,
                "XStart:=", x,
                "YStart:=", y,
                "ZStart:=", z,
                "Width:=", width,
                "Height:=", height,
                "WhichAxis:=", axis,
            ],
            self.attributes(name, material, color, transparency, True),
        )

    def create_cylinder(self, name: str, x: str, y: str, z: str, radius: str, height: str, material: str, color: str, transparency: float, solve_inside: bool = False):
        self.editor.CreateCylinder(
            [
                "NAME:CylinderParameters",
                "XCenter:=", x,
                "YCenter:=", y,
                "ZCenter:=", z,
                "Radius:=", radius,
                "Height:=", height,
                "WhichAxis:=", "Z",
                "NumSides:=", 0,
            ],
            self.attributes(name, material, color, transparency, solve_inside),
        )

    def create_via(self, name: str, center_xy_m: tuple[float, float], radius_m: float, z_lower_m: float, z_upper_m: float):
        z0 = min(z_lower_m, z_upper_m)
        z1 = max(z_lower_m, z_upper_m)
        if z1 <= z0:
            z1 = z0 + (self.model.substrate_layer.height or 0.001)
        self.create_cylinder(name, fs_mm(center_xy_m[0]), fs_mm(center_xy_m[1]), fs_mm(z0), fs_mm(radius_m), fs_mm(z1 - z0), self.metal_mat_name, VIA_COLOR, MODEL_TRANSPARENCY, False)
        self.via_objects.append(name)

    def create_poly_sheet(self, name: str, points_xy_m: list[tuple[float, float]], z_m: float):
        points = [["NAME:PLPoint", "X:=", fs_mm(x), "Y:=", fs_mm(y), "Z:=", fs_mm(z_m)] for x, y in points_xy_m]
        segments = [["NAME:PLSegment", "SegmentType:=", "Line", "StartIndex:=", i, "NoOfPoints:=", 2] for i in range(len(points_xy_m))]
        self.editor.CreatePolyline(
            [
                "NAME:PolylineParameters",
                "IsPolylineCovered:=", True,
                "IsPolylineClosed:=", True,
                ["NAME:PolylinePoints", *points],
                ["NAME:PolylineSegments", *segments],
                ["NAME:PolylineXSection", "XSectionType:=", "None", "XSectionOrient:=", "Auto", "XSectionWidth:=", "0mm", "XSectionTopWidth:=", "0mm", "XSectionHeight:=", "0mm", "XSectionNumSegments:=", "0", "XSectionBendType:=", "Corner"],
            ],
            self.attributes(name, "vacuum", COPPER_COLOR, MODEL_TRANSPARENCY, True),
        )
        self.metal_objects.append(name)

    def build_geometry(self, add_airbox: bool = True):
        box = self.model.box
        sub = self.model.substrate_layer
        h = sub.height or 0.00127
        elev = sub.elevation or 0.0
        x_total = box["Xtotal"]
        y_total = box["Ytotal"]
        x0 = -x_total / 2.0
        y0 = -y_total / 2.0

        self.create_box("Substrate", fs_mm(x0), fs_mm(y0), fs_mm(elev), fs_mm(x_total), fs_mm(y_total), fs_mm(h), self.substrate_mat_name, SUBSTRATE_COLOR, MODEL_TRANSPARENCY, True)
        self.create_rectangle("Ground", fs_mm(x0), fs_mm(y0), fs_mm(elev), fs_mm(x_total), fs_mm(y_total), "Z", "vacuum", SUBSTRATE_COLOR, MODEL_TRANSPARENCY)

        z_top = elev + h
        for idx in sorted(self.model.geometries):
            geo = self.model.geometries[idx]
            if geo.kind.lower() == "circle" and geo.via_extent and geo.circle_center and geo.circle_radius:
                z_lower = geo.z_lower if geo.z_lower is not None else elev
                z_upper = geo.z_upper if geo.z_upper is not None else z_top
                self.create_via(f"Via_{idx:02d}", geo.circle_center, geo.circle_radius, z_lower, z_upper)
                continue
            if len(geo.points) < 3:
                continue
            self.create_poly_sheet(f"Metal_{idx:02d}", geo.points, z_top)

        if self.via_objects:
            print(f"INFO: created {len(self.via_objects)} plated via cylinders from Circle/ViaExtent entries.")

        if add_airbox:
            xpad = max(box.get("Xbuffer", 0.00635), 0.00635)
            ypad = max(box.get("Ybuffer", 0.00889), 0.00889)
            z_above = max(4 * h, 0.010)
            z_below = max(1 * h, 0.003)
            self.create_box("AirBox", fs_mm(x0 - xpad), fs_mm(y0 - ypad), fs_mm(elev - z_below), fs_mm(x_total + 2 * xpad), fs_mm(y_total + 2 * ypad), fs_mm(h + z_above + z_below), "vacuum", AIRBOX_COLOR, MODEL_TRANSPARENCY, True)
            try:
                self.editor.ChangeProperty(
                    [
                        "NAME:AllTabs",
                        ["NAME:Geometry3DAttributeTab", ["NAME:PropServers", "AirBox"], ["NAME:ChangedProps", ["NAME:Display Wireframe", "Value:=", True]]],
                    ]
                )
            except Exception:
                pass

    def assign_boundaries(self, add_airbox: bool = True):
        try:
            self.boundary.AssignPerfectE(["NAME:Ground_PEC", "Objects:=", ["Ground"], "InfGroundPlane:=", False])
        except Exception as err:
            print(f"WARNING: AssignPerfectE on Ground failed: {err}")

        if self.metal_objects:
            try:
                self.boundary.AssignFiniteCond(
                    [
                        "NAME:TopMetal_FiniteCond",
                        "Objects:=", self.metal_objects,
                        "UseMaterial:=", True,
                        "Material:=", self.metal_mat_name,
                        "Roughness:=", "0um",
                        "InfGroundPlane:=", False,
                        "IsTwoSided:=", False,
                    ]
                )
                print(f"INFO: assigned finite-conductivity boundary to {len(self.metal_objects)} top metal sheets.")
            except Exception as err:
                print(f"WARNING: AssignFiniteCond failed, using PerfectE fallback: {err}")
                try:
                    self.boundary.AssignPerfectE(["NAME:TopMetal_PEC", "Objects:=", self.metal_objects, "InfGroundPlane:=", False])
                except Exception as err2:
                    print(f"WARNING: PerfectE fallback on top metal failed: {err2}")

        if add_airbox:
            try:
                self.boundary.AssignRadiation(["NAME:Rad1", "Objects:=", ["AirBox"], "IsIncidentField:=", False, "IsEnforcedField:=", False, "IsFssReference:=", False, "IsForPML:=", False])
                print("INFO: assigned Radiation boundary to AirBox.")
            except Exception as err:
                print(f"WARNING: Radiation boundary assignment failed: {err}")

    def create_lumped_ports(self):
        sub = self.model.substrate_layer
        h = sub.height or 0.00127
        elev = sub.elevation or 0.0
        z0 = elev
        z1 = elev + h
        ok_count = 0

        for port in self.model.ports:
            if port.position is None:
                print(f"WARNING: Port{port.index} has no XYZPosition in embedded TXT; skipped.")
                continue
            if port.geometry_index is None or port.geometry_index not in self.model.geometries:
                print(f"WARNING: Port{port.index} has no valid GeometryIndex; skipped.")
                continue
            geo = self.model.geometries[port.geometry_index]
            x = port.position[0]
            y0 = geo.ymin
            y1 = geo.ymax
            yc = 0.5 * (y0 + y1)
            sheet = f"Port{port.index}_Sheet"

            self.create_rectangle(sheet, fs_mm(x), fs_mm(y0), fs_mm(z0), fs_mm(y1 - y0), fs_mm(z1 - z0), "X", "vacuum", PORT_COLOR, MODEL_TRANSPARENCY)
            self.port_sheet_names.append(sheet)

            try:
                self.boundary.AssignLumpedPort(
                    [
                        "NAME:" + f"Port{port.index}",
                        "Objects:=", [sheet],
                        "RenormalizeAllTerminals:=", True,
                        "DoDeembed:=", False,
                        [
                            "NAME:Modes",
                            [
                                "NAME:Mode1",
                                "ModeNum:=", 1,
                                "UseIntLine:=", True,
                                ["NAME:IntLine", "Coordinate System:=", "Global", "Start:=", [fs_mm(x), fs_mm(yc), fs_mm(z1)], "End:=", [fs_mm(x), fs_mm(yc), fs_mm(z0)]],
                                "AlignmentGroup:=", 0,
                                "CharImp:=", "Zpi",
                            ],
                        ],
                        "ShowReporterFilter:=", False,
                        "ReporterFilter:=", [True],
                    ]
                )
                ok_count += 1
                print(f"INFO: assigned Lumped Port{port.index} at x={fs_mm(x)}, y={fs_mm(yc)}.")
            except Exception as err:
                print(f"WARNING: Lumped Port{port.index} failed: {err}")

        self.ports_ok = ok_count >= 2
        if not self.ports_ok:
            raise RuntimeError("fewer than two ports were assigned; cannot run benchmark.")

    def add_analysis_setup(self, sweep_points: int):
        if not self.ports_ok:
            raise RuntimeError("analysis setup skipped because ports are not valid.")
        self.analysis = self.design.GetModule("AnalysisSetup")

        fcenter = 6.13e9
        fstart = 4.0e9
        fend = 8.0e9
        sweep_points = max(1, int(sweep_points))

        setup_args = [
            "NAME:" + SETUP_NAME,
            "SolveType:=", "Single",
            "Frequency:=", fs_ghz(fcenter),
            "MaxDeltaS:=", 0.02,
            "UseMatrixConv:=", False,
            "MaximumPasses:=", 50,
            "MinimumPasses:=", 1,
            "MinimumConvergedPasses:=", 1,
            "PercentRefinement:=", 30,
            "IsEnabled:=", True,
            ["NAME:MeshLink", "ImportMesh:=", False],
            "BasisOrder:=", 1,
            "DoLambdaRefine:=", True,
            "DoMaterialLambda:=", True,
            "SetLambdaTarget:=", False,
            "Target:=", 0.3333,
            "UseMaxTetIncrease:=", False,
            "PortAccuracy:=", 2,
            "UseABCOnPort:=", False,
            "SetPortMinMaxTri:=", False,
            "DrivenSolverType:=", "Direct Solver",
            "SaveRadFieldsOnly:=", False,
            "SaveAnyFields:=", True,
        ]

        self.analysis.InsertSetup("HfssDriven", setup_args)
        print("INFO: InsertSetup used setup type: HfssDriven")
        print("INFO: sweep is intentionally not inserted during model build; adaptive setup will be solved first, then Sweep will be inserted and timed separately.")

    def save(self, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"{FILTER_PROJECT_NAME}.aedt"
        self.project.SaveAs(str(out_path), True)
        print(f"INFO: saved embedded filter project: {out_path}")
        return out_path

    def close(self):
        try:
            if self.project is not None:
                self.project.Save()
        except Exception:
            pass
        try:
            if self.project is not None:
                self.desktop.CloseProject(self.project.GetName())
        except Exception:
            pass
        try:
            self.desktop.QuitApplication()
        except Exception:
            pass


def print_embedded_filter_summary(model: FsModel):
    sub = model.substrate_layer
    via_count = sum(1 for g in model.geometries.values() if g.kind.lower() == "circle" and g.via_extent)
    print("测试模型：7阶 interdigital 滤波器，中心频率 6.13 GHz，带宽 800 MHz。")
    print(
        f"模型概要：尺寸 {fs_mm(model.box['Xtotal'])} × {fs_mm(model.box['Ytotal'])}，"
        f"介质 er={sub.er}, h={fs_mm(sub.height or 0.0)}，"
        f"金属图形 {len(model.geometries)} 个，过孔 {via_count} 个，端口 {len(model.ports)} 个。"
    )


def build_embedded_filter_project(output_dir: Path, sweep_points: int, aedt_version: str | None) -> Path:
    model = parse_embedded_filtersolutions_model()

    # Only print concise status lines in normal runs. The detailed COM build
    # messages are captured and printed only when model creation fails.
    print_embedded_filter_summary(model)
    builder = EmbeddedFilterHfssBuilder(model=model, version=aedt_version, visible=NON_GRAPHICAL)
    build_log = io.StringIO()
    try:
        with contextlib.redirect_stdout(build_log):
            builder.new_design()
            builder.build_geometry(add_airbox=True)
            builder.assign_boundaries(add_airbox=True)
            builder.create_lumped_ports()
            builder.add_analysis_setup(sweep_points=sweep_points)
            out_path = builder.save(output_dir)
        print("建模状态：几何、材料、边界、端口、Setup 创建完成。")
        return out_path
    except Exception:
        captured = build_log.getvalue().strip()
        if captured:
            print("建模阶段诊断日志：")
            for line in captured.splitlines()[-30:]:
                print(f"  {line}")
        raise
    finally:
        try:
            builder.close()
        finally:
            time.sleep(1.0)


def check_sweep_exists(
    hfss,
    setup_name: str = SETUP_NAME,
    sweep_name: str = SWEEP_NAME,
) -> bool:
    try:
        setup_obj = hfss.get_setup(setup_name)
        names = setup_obj.get_sweep_names()
        print(f"Setup {setup_name!r} 中检测到的 Sweep：{names}")
        return sweep_name in names
    except Exception as exc:
        print(f"Sweep 名称检查失败：{exc}")
        return False


def run_pyaedt_setup_analyze(hfss, setup_name: str) -> str:
    """Run setup using PyAEDT analyze_setup only."""
    analyze_setup = getattr(hfss, "analyze_setup", None)
    if not callable(analyze_setup):
        return "hfss.analyze_setup exception: method not available"

    attempts = [
        ("name+blocking", lambda: analyze_setup(name=setup_name, blocking=True)),
        ("name", lambda: analyze_setup(name=setup_name)),
        ("setup_name+blocking", lambda: analyze_setup(setup_name=setup_name, blocking=True)),
        ("setup_name", lambda: analyze_setup(setup_name=setup_name)),
        ("positional", lambda: analyze_setup(setup_name)),
    ]

    errors = []
    for label, caller in attempts:
        try:
            ok = caller()
            if ok is False:
                errors.append(f"{label}: returned False")
                continue
            return f"hfss.analyze_setup completed ({label}, return={ok})"
        except TypeError as exc:
            errors.append(f"{label}: TypeError {exc}")
            continue
        except Exception as exc:
            return f"hfss.analyze_setup exception ({label}): {exc}"

    return "hfss.analyze_setup exception: " + " | ".join(errors)


def insert_discrete_linear_count_sweep_after_adaptive(
    hfss,
    setup_name: str,
    sweep_name: str,
    point_count: int,
    design_name: str = DESIGN_NAME,
) -> str:
    """Insert the benchmark sweep after adaptive setup has been solved.

    The sweep is inserted only after the adaptive solution is complete, so the
    timed call can target "Setup1 : Sweep" and exclude adaptive meshing time.
    """
    point_count = max(1, int(point_count))
    odesign = activate_project_design_like_recorded(hfss, design_name)
    oanalysis = odesign.GetModule("AnalysisSetup")

    try:
        setup_obj = hfss.get_setup(setup_name)
        names = setup_obj.get_sweep_names()
        if sweep_name in names:
            try:
                oanalysis.DeleteSweep(setup_name, sweep_name)
            except Exception:
                try:
                    setup_obj.delete_sweep(sweep_name)
                except Exception:
                    pass
    except Exception:
        pass

    sweep_props = [
        "NAME:" + sweep_name,
        "IsEnabled:=", True,
        "RangeType:=", "LinearCount",
        "RangeStart:=", "4GHz",
        "RangeEnd:=", "8GHz",
        "RangeCount:=", point_count,
        "Type:=", "Discrete",
        "SaveFields:=", False,
        "SaveRadFields:=", False,
    ]

    oanalysis.InsertFrequencySweep(setup_name, sweep_props)
    return f"Inserted {setup_name} : {sweep_name}, Discrete LinearCount 4GHz~8GHz, points={point_count}"


def run_native_analyze_target(
    hfss,
    analyze_target: str,
    design_name: str = DESIGN_NAME,
) -> str:
    """Run a specific AEDT target, for example 'Setup1 : Sweep'."""
    try:
        odesign = activate_project_design_like_recorded(hfss, design_name)
        odesign.Analyze(analyze_target)
        return f"oDesign.Analyze completed ({analyze_target})"
    except Exception as exc:
        return f"oDesign.Analyze exception ({analyze_target}): {exc}"


def make_allowed_distribution_string(allowed_types: list[str]) -> str:
    quoted = "', '".join(allowed_types)
    return f"[{len(allowed_types)}: '{quoted}']"


def create_hpc_acf_text(hfss, tasks: int, cores: int, gpus: int = 0) -> str:
    design_type = getattr(hfss, "design_type", "HFSS") or "HFSS"
    allowed = make_allowed_distribution_string(HPC_ALLOWED_DISTRIBUTION_TYPES)
    use_auto = "true" if HPC_USE_AUTO_SETTINGS else "false"

    return f"""$begin 'Configs'
$begin 'Configs'
$begin 'DSOConfig'
ConfigName='{HPC_CONFIG_NAME}'
DesignType='{design_type}'
$begin 'DSOMachineList'
$begin 'DSOMachineInfo'
MachineName='localhost'
NumEngines={tasks}
NumCores={cores}
IsEnabled=true
RAMPercent={HPC_RAM_PERCENT}
NumJobCores={HPC_NUM_JOB_CORES}
NumGPUs={gpus}
$end 'DSOMachineInfo'
$end 'DSOMachineList'
UseAutoSettings={use_auto}
NumVariationsToDistribute=1
$begin 'DSOJobDistributionInfo'
AllowedDistributionTypes{allowed}
Enable2LevelDistribution=false
NumL1Engines=1
UseDefaultsForDistributionTypes=false
Context()
$end 'DSOJobDistributionInfo'
$begin 'DSOMachineOptionsInfo'
MenuValues()
IntValues()
BoolValues(AllowOffCore=true)
DoubleValues()
$end 'DSOMachineOptionsInfo'
$end 'DSOConfig'
$end 'Configs'
$end 'Configs'
"""


def apply_hpc_settings(hfss, tasks: int, cores: int) -> str:
    """
    Write HPC settings by generating an ACF file directly.

    Why not use hfss.set_custom_hpc_options()?
    In normal Python it works, but in PyInstaller / auto-py-to-exe onefile mode,
    PyAEDT may fail while copying its bundled pyaedt_local_config.acf template.
    Direct ACF generation avoids that packaging-sensitive copy step.
    """
    try:
        working_dir = Path(getattr(hfss, "working_directory", Path.cwd()))
        working_dir.mkdir(parents=True, exist_ok=True)
        acf_path = working_dir / f"{HPC_CONFIG_NAME}.acf"

        acf_text = create_hpc_acf_text(hfss, tasks=tasks, cores=cores, gpus=0)
        acf_path.write_text(acf_text, encoding="utf-8")

        print(f"自定义 HPC ACF 已生成：{acf_path}")
        print(
            f"脚本写入配置：Tasks={tasks}, Cores={cores}, GPUs=0, "
            f"use_auto_settings={HPC_USE_AUTO_SETTINGS}, allowed_distribution_types={HPC_ALLOWED_DISTRIBUTION_TYPES}"
        )
        print("Job Distribution 设置：允许常规分发类型，但不允许 Iterative Solver Excitations / Direct Solver Memory。")

        ok = hfss.set_hpc_from_file(acf_file=str(acf_path))
        msg = "script set_hpc_from_file ok" if ok else "script set_hpc_from_file returned False"
        print(f"自定义 HPC 配置写入：{'成功' if ok else '失败/未确认'}")
        return msg

    except Exception as exc:
        msg = f"script direct ACF HPC write exception: {exc}"
        print(f"自定义 HPC 配置写入失败，仍继续求解：{exc}")
        return msg


def run_one(
    example_path: Path,
    tasks: int,
    Hfss,
    case: BenchmarkCase | None = None,
    aedt_version: str = DEFAULT_AEDT_VERSION,
    sweep_points: int | None = None,
    configure_linear_sweep: bool = True,
) -> OneRunResult:
    if case is None:
        case = BENCHMARK_CASES[FIXED_CASE_ID]

    # The embedded benchmark always uses a direct Discrete LinearCount sweep.
    sweep_points = max(1, int(sweep_points or case.effective_frequency_points))
    cores = decide_cores(tasks)
    hpc_control = "script"
    before_processes = get_process_snapshot(ANSYS_PROCESS_NAMES_TO_CLEAN)

    design_name = FILTER_DESIGN_NAME
    setup_name = SETUP_NAME
    sweep_name = SWEEP_NAME
    native_analyze_target = f"{setup_name} : {sweep_name}"

    hfss = None
    temp_dir: Optional[Path] = None
    temp_project: Optional[Path] = None
    benchmark_valid = False
    t0 = time.perf_counter()
    open_seconds = 0.0
    solve_seconds = 0.0
    total_seconds = 0.0
    analyze_call = "not called"
    status = "失败/未确认成功"
    error_summary = ""
    result_info = ResultDirInfo(False, 0, 0, False)
    solution_type_info = SolutionTypeInfo(False, True, "skipped", "")
    cleanup_info = ProcessCleanupInfo(FORCE_CLOSE_NEW_ANSYS_PROCESSES, [], [])

    try:
        temp_dir = Path(tempfile.mkdtemp(prefix=BENCH_TEMP_PREFIX))
        print("\n" + "-" * 72)
        print(f"Benchmark 模型：{case.display_name}")
        print(f"开始测试 Tasks={tasks}, Cores={cores}, SweepPoints={sweep_points}, SweepMode=嵌入式建模+Discrete LinearCount")
        print(f"HPC控制方式：{hpc_control}")
        print(f"Design：{design_name}")
        print(f"Setup：{setup_name}")
        print(f"Sweep：{sweep_name}")
        print(f"临时工程目录：{temp_dir}")

        t_open0 = time.perf_counter()
        print("建模状态：开始创建 HFSS Modal Network 测试模型...")
        temp_project = build_embedded_filter_project(temp_dir, sweep_points=sweep_points, aedt_version=aedt_version)
        print(f"建模状态：项目已创建，路径：{temp_project}")

        hfss = Hfss(
            project=str(temp_project),
            design=design_name,
            version=aedt_version,
            non_graphical=NON_GRAPHICAL,
            new_desktop=NEW_DESKTOP,
            close_on_exit=False,
            remove_lock=True,
        )
        open_seconds = time.perf_counter() - t_open0

        try:
            hfss.autosave_disable()
        except Exception:
            pass

        activate_project_design_like_recorded(hfss, design_name)

        hpc_msg = apply_hpc_settings(hfss, tasks, cores)

        try:
            hfss.save_project()
        except Exception:
            pass

        print(f"开始自适应求解：hfss.analyze_setup({setup_name!r})，该阶段不计入点吞吐耗时。")
        t_adapt0 = time.perf_counter()
        adaptive_call = run_pyaedt_setup_analyze(hfss, setup_name)
        adaptive_seconds = time.perf_counter() - t_adapt0
        print(f"自适应求解调用：{adaptive_call}")
        print(f"自适应求解耗时：{adaptive_seconds:.2f} s（不计入点吞吐）")
        if not adaptive_call.startswith("hfss.analyze_setup completed"):
            raise RuntimeError(f"自适应求解失败，未进入 Sweep 计时阶段：{adaptive_call}")

        sweep_cfg_msg = insert_discrete_linear_count_sweep_after_adaptive(
            hfss,
            setup_name,
            sweep_name,
            sweep_points,
            design_name,
        )
        print(f"Benchmark Sweep：{sweep_cfg_msg}")
        try:
            hfss.save_project()
        except Exception:
            pass

        print(f"开始执行 Sweep-only：oDesign.Analyze({native_analyze_target!r})")
        t_solve0 = time.perf_counter()
        analyze_call = run_native_analyze_target(hfss, native_analyze_target, design_name)
        solve_seconds = time.perf_counter() - t_solve0

        messages = get_aedt_messages(hfss)
        errors, warnings = split_problem_messages(messages)
        real_solve_error = has_real_solve_error(messages)
        solved_by_message = looks_solved_from_messages(messages)
        analyze_completed = analyze_call.startswith("oDesign.Analyze completed")

        if temp_project is not None:
            result_info = inspect_result_dir(temp_project)

        benchmark_valid = (
            analyze_completed
            and not real_solve_error
            and (solved_by_message or result_info.exists)
            and result_info.file_count > 0
        )

        if benchmark_valid:
            status = "成功"
        elif not analyze_completed:
            status = "PyAEDT Analyze调用异常"
        elif real_solve_error:
            status = "HFSS求解失败"
        elif not result_info.exists or result_info.file_count == 0:
            status = "未发现有效.aedtresults结果文件"
        else:
            status = "失败/未确认成功"

        all_probs = errors[:6] + warnings[:3]
        if hpc_msg and "exception" in hpc_msg.lower():
            all_probs.insert(0, hpc_msg)
        error_summary = " | ".join(all_probs)

        total_seconds = time.perf_counter() - t0
        total_mb = result_info.total_bytes / 1024 / 1024

        print(f"结果：{status}")
        print(f"Sweep-only Analyze调用：{analyze_call}")
        print(f"结果目录存在：{result_info.exists}，文件数：{result_info.file_count}，大小：{total_mb:.2f} MB")
        print(
            f"建模+打开耗时：{open_seconds:.2f} s，Sweep-only求解耗时：{solve_seconds:.2f} s，"
            f"总耗时：{total_seconds:.2f} s"
        )
        if all_probs:
            print("错误/警告摘要：")
            for item in all_probs[:10]:
                print(f"  {item}")

    except Exception as exc:
        total_seconds = time.perf_counter() - t0
        status = "脚本异常"
        error_summary = str(exc)
        print(f"运行异常：{exc}")

    finally:
        if hfss is not None:
            try:
                close_project_without_saving(hfss)
            except Exception:
                pass
            try:
                hfss.release_desktop(close_projects=True, close_desktop=True)
            except Exception:
                pass

        cleanup_info = cleanup_new_ansys_processes(before_processes)
        if cleanup_info.enabled:
            if cleanup_info.killed_pids:
                print(f"已强制关闭本轮残留 Ansys 进程 PID：{cleanup_info.killed_pids}")
            else:
                print("未发现本轮新增的残留 Ansys 进程。")
            if cleanup_info.errors:
                print("进程清理警告：")
                for item in cleanup_info.errors[:8]:
                    print(f"  {item}")

        if temp_dir is not None and temp_dir.exists():
            if KEEP_TEMP_ON_FAIL and not benchmark_valid:
                print(f"调试模式：保留临时目录：{temp_dir}")
            else:
                for _ in range(5):
                    try:
                        shutil.rmtree(temp_dir, ignore_errors=False)
                        print(f"已删除临时目录：{temp_dir}")
                        break
                    except Exception:
                        time.sleep(1)
                else:
                    print(f"临时目录暂时无法删除，可能被 AEDT 占用：{temp_dir}")

    killed_processes = ",".join(str(p) for p in cleanup_info.killed_pids)
    return OneRunResult(
        case_id=case.case_id,
        case_name=case.display_name,
        case_short_name=case.short_name,
        design_name=design_name,
        setup_name=setup_name,
        sweep_name=sweep_name,
        tasks=tasks,
        sweep_points=sweep_points,
        cores=cores,
        hpc_control=hpc_control,
        solution_type_changed=solution_type_info.changed,
        validation_passed=solution_type_info.validation_passed,
        validate_return=solution_type_info.validate_return,
        native_analyze_target=native_analyze_target,
        status=status,
        benchmark_valid=benchmark_valid,
        analyze_call=analyze_call,
        result_dir_exists=result_info.exists,
        result_file_count=result_info.file_count,
        result_total_mb=result_info.total_bytes / 1024 / 1024,
        sweep_name_seen=result_info.sweep_name_seen,
        killed_processes=killed_processes,
        open_seconds=open_seconds,
        solve_seconds=solve_seconds,
        total_seconds=total_seconds,
        error_summary=error_summary,
    )


def compute_rating(seconds: float, valid: bool) -> float:
    if not valid or seconds <= 0:
        return 0.0
    return 86400.0 / seconds


def compute_point_throughput_score(r: OneRunResult) -> float:
    """Frequency-point throughput in points/day.

    This is the chart metric for the current single-project benchmark, because
    Tasks=1 uses the default sweep while the full-load run uses a different
    number of linear-count frequency points.
    """
    if not r.benchmark_valid or r.solve_seconds <= 0:
        return 0.0
    return get_points_for_result(r) * 86400.0 / r.solve_seconds


def compute_speedup(seconds: float, base_seconds: float | None, valid: bool) -> float:
    if not valid or not base_seconds or base_seconds <= 0 or seconds <= 0:
        return 0.0
    return base_seconds / seconds


def get_points_for_result(r: OneRunResult) -> int:
    return max(1, int(getattr(r, "sweep_points", 0) or get_target_sweep_points()))


def get_effective_tasks_for_result(r: OneRunResult) -> int:
    points = get_points_for_result(r)
    tasks = max(1, int(getattr(r, "tasks", 1) or 1))
    return max(1, min(tasks, points))


def compute_task_normalized_time(r: OneRunResult) -> float:
    """Approximate time per frequency point per effective task.

    Formula:
        TaskNormalizedTime = Time * EffectiveTasks / Nfreq
    """
    if not r.benchmark_valid or r.solve_seconds <= 0:
        return 0.0
    points = get_points_for_result(r)
    return r.solve_seconds * get_effective_tasks_for_result(r) / points


def compute_point_throughput_speedup(r: OneRunResult, base: OneRunResult | None) -> float:
    """Speedup based on solved frequency-point throughput.

    This is used because Tasks=1 keeps the default SIW sweep, while the full-load
    run uses a different number of linear-count sweep points.
    """
    if base is None:
        return 0.0
    if not r.benchmark_valid or not base.benchmark_valid:
        return 0.0
    if r.solve_seconds <= 0 or base.solve_seconds <= 0:
        return 0.0

    current_throughput = get_points_for_result(r) / r.solve_seconds
    base_throughput = get_points_for_result(base) / base.solve_seconds
    if base_throughput <= 0:
        return 0.0
    return current_throughput / base_throughput


def compute_parallel_efficiency(r: OneRunResult, base: OneRunResult | None) -> float:
    speedup = compute_point_throughput_speedup(r, base)
    effective_tasks = get_effective_tasks_for_result(r)
    if speedup <= 0 or effective_tasks <= 0:
        return 0.0
    return speedup / effective_tasks


def get_speedup_base_info(results: list[OneRunResult]) -> tuple[float | None, int | None]:
    for r in results:
        if r.benchmark_valid and r.tasks == 1 and r.solve_seconds > 0:
            return r.solve_seconds, r.tasks
    for r in results:
        if r.benchmark_valid and r.solve_seconds > 0:
            return r.solve_seconds, r.tasks
    return None, None


def get_speedup_base_info_for_result(
    results: list[OneRunResult],
    target: OneRunResult,
) -> tuple[float | None, int | None]:
    """Use Tasks=1 of the same benchmark case as the Speedup baseline."""
    target_case_id = getattr(target, "case_id", "")

    same_case = [r for r in results if getattr(r, "case_id", "") == target_case_id]
    for r in same_case:
        if r.benchmark_valid and r.tasks == 1 and r.solve_seconds > 0:
            return r.solve_seconds, r.tasks
    for r in same_case:
        if r.benchmark_valid and r.solve_seconds > 0:
            return r.solve_seconds, r.tasks

    # Fallback for old result objects without case_id.
    return get_speedup_base_info(results)


def get_speedup_base_result_for_result(
    results: list[OneRunResult],
    target: OneRunResult,
) -> OneRunResult | None:
    """Use Tasks=1 of the same benchmark case as throughput-speedup baseline."""
    target_case_id = getattr(target, "case_id", "")
    same_case = [r for r in results if getattr(r, "case_id", "") == target_case_id]

    for r in same_case:
        if r.benchmark_valid and r.tasks == 1 and r.solve_seconds > 0:
            return r
    for r in same_case:
        if r.benchmark_valid and r.solve_seconds > 0:
            return r
    return None


EXPORT_FORMAT_NAME = "HFSS_BENCHMARK_GUI_RESULT_PYAEDT"
EXPORT_FORMAT_VERSION = 64


def benchmark_case_to_dict(case: BenchmarkCase) -> dict:
    return {
        "case_id": case.case_id,
        "display_name": case.display_name,
        "short_name": case.short_name,
        "example_relative_path": str(case.example_relative_path),
        "design_name": case.design_name,
        "setup_name": BENCHMARK_SETUP_NAME,
        "sweep_name": BENCHMARK_SWEEP_NAME,
        "recommended_tasks": case.recommended_tasks,
        "effective_frequency_points": case.effective_frequency_points,
    }


def one_run_result_from_dict(data: dict) -> OneRunResult:
    """Load OneRunResult from exported JSON, tolerating older/missing fields."""
    defaults = {
        "case_id": "",
        "case_name": "",
        "case_short_name": "",
        "design_name": "",
        "setup_name": "",
        "sweep_name": "",
        "tasks": 0,
        "sweep_points": 0,
        "cores": 0,
        "hpc_control": "",
        "solution_type_changed": False,
        "validation_passed": False,
        "validate_return": "",
        "native_analyze_target": "",
        "status": "",
        "benchmark_valid": False,
        "analyze_call": "",
        "result_dir_exists": False,
        "result_file_count": 0,
        "result_total_mb": 0.0,
        "sweep_name_seen": False,
        "killed_processes": "",
        "open_seconds": 0.0,
        "solve_seconds": 0.0,
        "total_seconds": 0.0,
        "error_summary": "",
        "round_index": 0,
        "repeat_rounds": 1,
        "result_kind": "final",
    }
    for key in defaults:
        if key in data:
            defaults[key] = data[key]

    # Try to infer a readable case label for older exports.
    if not defaults["case_short_name"]:
        defaults["case_short_name"] = defaults["case_id"] or defaults["case_name"] or "Imported Case"
    if not defaults["case_name"]:
        defaults["case_name"] = defaults["case_short_name"]

    return OneRunResult(**defaults)


def shorten_text(text: str, max_len: int = 46) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def get_benchmark_label_from_host_config(host_config: dict, fallback: str = "未知版本") -> str:
    if not isinstance(host_config, dict):
        return fallback
    return str(
        host_config.get("Benchmark版本")
        or host_config.get("_benchmark_program")
        or host_config.get("_benchmark_release")
        or fallback
    )


def make_machine_summary(host_config: dict, fallback: str = "未知机器") -> str:
    """Build a full machine summary without ellipsis.

    The chart legend wraps long text automatically, and the dataset dropdown should
    show the complete machine name/configuration rather than replacing it with "...".
    """
    cpu = " ".join(str(host_config.get("CPU") or fallback).split())
    mem = " ".join(str(host_config.get("内存总量") or "").split())
    channel = " ".join(str(host_config.get("内存通道") or "").split())
    freq = " ".join(str(host_config.get("内存频率") or "").split())
    bandwidth = " ".join(str(host_config.get("理论内存带宽") or "").split())

    parts = [cpu]
    memory_parts = [x for x in [mem, channel, freq, bandwidth] if x]
    if memory_parts:
        parts.append(" ".join(memory_parts))
    return " / ".join(parts)


def _host_config_for_export(host_config: dict) -> dict:
    cleaned = dict(host_config)
    for key in list(cleaned.keys()):
        if key == "Benchmark版本" or str(key).startswith("_benchmark"):
            cleaned.pop(key, None)
    return cleaned


def make_export_payload(results: list[OneRunResult], host_config: dict, round_results: list[OneRunResult] | None = None) -> dict:
    return {
        "format": EXPORT_FORMAT_NAME,
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "benchmark_mode": "embedded_filtersolutions_hfss_modal_network_discrete_linear_count_sweep_only",
        "fixed_case_id": FIXED_CASE_ID,
        "chart_metric": "point_throughput_points_per_day",
        "metric_definitions": {
            "point_throughput_points_per_day": "sweep_points / sweep_only_solve_seconds * 86400; adaptive setup time is excluded",
            "throughput_speedup": "(points/second of current run) / (points/second of Tasks=1 baseline)",
        },
        "host_config": _host_config_for_export(host_config),
        "benchmark_cases": {
            case_id: benchmark_case_to_dict(case)
            for case_id, case in BENCHMARK_CASES.items()
        },
        "results": [asdict(r) for r in results],
        "round_results": [asdict(r) for r in (round_results or [])],
    }


def save_results_csv(results: list[OneRunResult], path: Path) -> None:
    need_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "timestamp",
                "case_id",
                "case_name",
                "case_short_name",
                "design",
                "setup",
                "sweep",
                "native_analyze_target",
                "tasks_label",
                "sweep_points",
                "effective_tasks",
                "cores_set_by_script",
                "hpc_control",
                "solution_type_changed",
                "validation_passed",
                "validate_return",
                "status",
                "benchmark_valid",
                "analyze_call",
                "result_dir_exists",
                "result_file_count",
                "result_total_mb",
                "sweep_name_seen",
                "killed_processes",
                "open_seconds",
                "solve_seconds",
                "rating_runs_per_day",
                "point_throughput_points_per_day",
                "throughput_speedup_vs_tasks1",
                "speedup_vs_tasks1",
                "total_seconds",
                "error_summary",
                "round_index",
                "repeat_rounds",
                "result_kind",
            ],
        )
        if need_header:
            writer.writeheader()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in results:
            base_result = get_speedup_base_result_for_result(results, r)
            rating = compute_rating(r.solve_seconds, r.benchmark_valid)
            throughput = compute_point_throughput_score(r)
            speedup = compute_point_throughput_speedup(r, base_result)
            writer.writerow({
                "timestamp": ts,
                "case_id": getattr(r, "case_id", ""),
                "case_name": getattr(r, "case_name", ""),
                "case_short_name": getattr(r, "case_short_name", ""),
                "design": getattr(r, "design_name", DESIGN_NAME),
                "setup": getattr(r, "setup_name", SETUP_NAME),
                "sweep": getattr(r, "sweep_name", SWEEP_NAME),
                "native_analyze_target": r.native_analyze_target,
                "tasks_label": r.tasks,
                "sweep_points": getattr(r, "sweep_points", ""),
                "effective_tasks": get_effective_tasks_for_result(r),
                "cores_set_by_script": r.cores,
                "hpc_control": r.hpc_control,
                "solution_type_changed": r.solution_type_changed,
                "validation_passed": r.validation_passed,
                "validate_return": r.validate_return,
                "status": r.status,
                "benchmark_valid": r.benchmark_valid,
                "analyze_call": r.analyze_call,
                "result_dir_exists": r.result_dir_exists,
                "result_file_count": r.result_file_count,
                "result_total_mb": f"{r.result_total_mb:.3f}",
                "sweep_name_seen": r.sweep_name_seen,
                "killed_processes": r.killed_processes,
                "open_seconds": f"{r.open_seconds:.3f}",
                "solve_seconds": f"{r.solve_seconds:.3f}",
                "rating_runs_per_day": f"{rating:.3f}" if rating else "",
                "point_throughput_points_per_day": f"{throughput:.3f}" if throughput else "",
                "throughput_speedup_vs_tasks1": f"{speedup:.3f}" if speedup else "",
                "speedup_vs_tasks1": f"{speedup:.3f}" if speedup else "",
                "total_seconds": f"{r.total_seconds:.3f}",
                "error_summary": r.error_summary,
                "round_index": getattr(r, "round_index", 0),
                "repeat_rounds": getattr(r, "repeat_rounds", 1),
                "result_kind": getattr(r, "result_kind", "final"),
            })


def print_summary(results: list[OneRunResult]) -> None:
    print("\n" + "=" * 150)
    print("HFSS Embedded Filter Benchmark Summary")
    print(f"PyAEDT Analyze target: {NATIVE_ANALYZE_TARGET}")
    print("Tasks=1: adaptive excluded, Discrete LinearCount 10-point Sweep; Full-load: adaptive excluded, Discrete LinearCount logical-thread-point Sweep")
    print("=" * 150)
    print(
        f"{'Tasks':>7} {'FreqPts':>7} {'有效':>6} {'Sweep耗时/s':>12} "
        f"{'点吞吐/day':>14} {'吞吐加速':>10} {'结果MB':>10}  状态"
    )
    print("-" * 150)

    best_score = 0.0
    best_tasks = None
    for r in results:
        score = compute_point_throughput_score(r)
        if score > best_score:
            best_score = score
            best_tasks = r.tasks

    for r in results:
        base_result = get_speedup_base_result_for_result(results, r)
        throughput = compute_point_throughput_score(r)
        speedup = compute_point_throughput_speedup(r, base_result)
        throughput_text = f"{throughput:.0f}" if throughput else "-"
        speedup_text = f"{speedup:.2f}x" if speedup else "-"
        valid_text = "是" if r.benchmark_valid else "否"
        print(
            f"{r.tasks:7d} {getattr(r, 'sweep_points', 0):7d} {valid_text:>6} "
            f"{r.solve_seconds:12.2f} {throughput_text:>14} {speedup_text:>10} "
            f"{r.result_total_mb:10.2f}  {r.status}"
        )
    print("-" * 150)
    summary_base = None
    for r in results:
        if r.benchmark_valid and r.tasks == 1 and r.solve_seconds > 0:
            summary_base = r
            break
    if summary_base:
        print(f"吞吐加速基准：Tasks=1，Benchmark Sweep，点数={get_points_for_result(summary_base)}，有效时间 {summary_base.solve_seconds:.2f} s。")
        print("PointThroughput = FreqPts / SweepTime * 86400")
        print("ThroughputSpeedup = 当前点吞吐 / Tasks=1点吞吐")
    else:
        print("吞吐加速基准：无有效 Tasks=1 结果。")
    if best_tasks is not None:
        print(f"Best PointThroughput：{best_score:.0f} points/day @ Tasks={best_tasks}。")
    print("=" * 150)





WORKER_RESULT_PREFIX = "__HFSS_RESULT_JSON__ "
WORKER_ROUND_RESULT_PREFIX = "__HFSS_ROUND_RESULT_JSON__ "


def get_self_command(extra_args: list[str]) -> list[str]:
    """Return command for launching this program again as a worker process."""
    if getattr(sys, "frozen", False):
        return [sys.executable] + extra_args
    return [sys.executable, str(Path(__file__).resolve())] + extra_args


def average_continuous_results_for_display(
    raw_results: list[OneRunResult],
    tasks_list: list[int],
    repeat_rounds: int,
) -> list[OneRunResult]:
    """Return one averaged display result per Tasks value.

    Only valid runs are used in the numeric average. If a Tasks value has no
    valid run, the first invalid result is returned as the representative
    failure row.
    """
    if repeat_rounds <= 1:
        return raw_results

    averaged: list[OneRunResult] = []
    for tasks in tasks_list:
        group = [r for r in raw_results if int(getattr(r, "tasks", 0)) == int(tasks)]
        if not group:
            continue

        valid = [r for r in group if r.benchmark_valid and r.solve_seconds > 0]
        source = valid if valid else group
        t = source[0]
        n_total = len(group)
        n_valid = len(valid)

        def avg_float(attr: str) -> float:
            vals = [float(getattr(r, attr, 0.0) or 0.0) for r in source]
            return sum(vals) / len(vals) if vals else 0.0

        def avg_int(attr: str) -> int:
            vals = [int(getattr(r, attr, 0) or 0) for r in source]
            return int(round(sum(vals) / len(vals))) if vals else 0

        if n_valid > 0:
            status = f"平均值({n_valid}/{n_total}有效)" if n_valid != n_total else f"平均值({n_valid}轮)"
            benchmark_valid = True
            error_summary = ""
            if n_valid != n_total:
                error_summary = f"连续测试中有 {n_total - n_valid} 轮无效，平均值仅统计有效轮次。"
        else:
            status = "连续测试均无效"
            benchmark_valid = False
            error_summary = " | ".join([str(r.error_summary) for r in group if str(r.error_summary).strip()][:4])

        averaged.append(
            OneRunResult(
                case_id=t.case_id,
                case_name=t.case_name,
                case_short_name=t.case_short_name,
                design_name=t.design_name,
                setup_name=t.setup_name,
                sweep_name=t.sweep_name,
                tasks=t.tasks,
                sweep_points=t.sweep_points,
                cores=t.cores,
                hpc_control=t.hpc_control,
                solution_type_changed=any(r.solution_type_changed for r in group),
                validation_passed=all(r.validation_passed for r in source),
                validate_return=f"continuous average, valid={n_valid}/{n_total}",
                native_analyze_target=t.native_analyze_target,
                status=status,
                benchmark_valid=benchmark_valid,
                analyze_call=f"continuous average over {n_valid}/{n_total} valid runs",
                result_dir_exists=any(r.result_dir_exists for r in source),
                result_file_count=avg_int("result_file_count"),
                result_total_mb=avg_float("result_total_mb"),
                sweep_name_seen=any(r.sweep_name_seen for r in source),
                killed_processes="",
                open_seconds=avg_float("open_seconds"),
                solve_seconds=avg_float("solve_seconds"),
                total_seconds=avg_float("total_seconds"),
                error_summary=error_summary,
                round_index=0,
                repeat_rounds=repeat_rounds,
                result_kind="average",
            )
        )

    return averaged


def worker_main(case_ids_text: str, repeat_rounds: int = 1) -> int:
    """Run the embedded FilterSolutions benchmark in a separate process so the Tk GUI remains responsive."""
    setup_utf8_stdio()
    try:
        repeat_rounds = int(repeat_rounds)
        if repeat_rounds <= 0:
            raise ValueError
    except Exception:
        print("WARNING: worker 收到的连续测试轮次非法，按 1 轮执行。", flush=True)
        repeat_rounds = 1

    print("\n正在查找 Ansys Electronics Desktop 安装目录...", flush=True)
    install = get_preferred_aedt_install()
    apply_aedt_install_environment(install)
    win64_root = install.win64_path
    print(f"找到安装目录：{win64_root}", flush=True)
    print(f"Benchmark程序：{benchmark_program_from_install(install)}", flush=True)
    print(f"AEDT版本：{install.display_name}", flush=True)
    print(f"PyAEDT version参数：{install.pyaedt_version}", flush=True)
    print("HPC控制方式：脚本固定写入 Tasks/Cores", flush=True)

    Hfss = import_hfss_class()
    all_results: list[OneRunResult] = []

    case = get_case(FIXED_CASE_ID)
    tasks_list, reason = get_auto_tasks_for_case(case)
    full_sweep_points = get_target_sweep_points()

    print("\n" + "#" * 72, flush=True)
    print(f"Benchmark 模型：{case.display_name}", flush=True)
    print(f"Design：{case.design_name}", flush=True)
    print(f"Setup：{SETUP_NAME}", flush=True)
    print(f"Sweep：{SWEEP_NAME}", flush=True)
    print("测试流程：先自适应求解但不计入跑分，再执行 Sweep-only 计时。", flush=True)
    print(f"Tasks=1 基准：线性离散 {case.effective_frequency_points} 个频点。", flush=True)
    print(f"满载测试：线性离散 {full_sweep_points} 个频点，Tasks={max(tasks_list)}。", flush=True)
    print(f"测试 Tasks：{tasks_list}", flush=True)
    print(f"自动策略：{reason}", flush=True)

    if repeat_rounds > 1:
        print(f"连续测试：共 {repeat_rounds} 轮。", flush=True)

    results: list[OneRunResult] = []
    for round_index in range(1, repeat_rounds + 1):
        if repeat_rounds > 1:
            print("\n" + "=" * 72, flush=True)
            print(f"连续测试第 {round_index}/{repeat_rounds} 轮", flush=True)

        for tasks in tasks_list:
            is_full_load = tasks != 1
            run_points = full_sweep_points if is_full_load else case.effective_frequency_points
            if is_full_load:
                print(f"本轮为满载测试：Tasks={tasks}，线性离散扫频点数={run_points}", flush=True)
            else:
                print(f"本轮为单 Tasks 基准：Tasks=1，线性离散扫频点数={run_points}", flush=True)

            r = run_one(
                Path("."),
                tasks,
                Hfss,
                case,
                install.pyaedt_version,
                sweep_points=run_points,
                configure_linear_sweep=True,
            )
            r.round_index = round_index
            r.repeat_rounds = repeat_rounds
            r.result_kind = "round" if repeat_rounds > 1 else "final"

            results.append(r)
            all_results.append(r)

            if repeat_rounds > 1:
                print(WORKER_ROUND_RESULT_PREFIX + json.dumps(asdict(r), ensure_ascii=False), flush=True)
            else:
                print(WORKER_RESULT_PREFIX + json.dumps(asdict(r), ensure_ascii=False), flush=True)

            if not r.benchmark_valid:
                print("本轮结果无效或失败，worker 将继续测试后续 Tasks。", flush=True)

    display_results = average_continuous_results_for_display(results, tasks_list, repeat_rounds)

    if repeat_rounds > 1:
        print("\n" + "=" * 72, flush=True)
        print("连续测试平均结果：结果表仅显示以下平均值。", flush=True)
        for r in display_results:
            print(WORKER_RESULT_PREFIX + json.dumps(asdict(r), ensure_ascii=False), flush=True)

    print_summary(display_results)

    print("本次运行未自动导出 CSV；需要保存结果时请使用界面中的导出功能。", flush=True)

    return 0 if any(r.benchmark_valid for r in all_results) else 1


class QueueWriter:
    """Redirect stdout/stderr from worker thread to Tkinter log box."""

    def __init__(self, q: "queue.Queue[tuple[str, object]]"):
        self.q = q

    def write(self, data):
        if data:
            self.q.put(("log", str(data)))

    def flush(self):
        pass


class HfssBenchmarkApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("HFSS Benchmark PyAEDT")
        self.root.geometry("1200x820")
        self.root.minsize(1000, 720)

        self.queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.results: list[OneRunResult] = []
        self.round_results: list[OneRunResult] = []
        self.current_host_config: dict[str, str] = {}
        self.imported_datasets: list[dict] = []
        self.dataset_visibility_vars: dict[str, tk.BooleanVar] = {}
        self.aedt_installs: list[AedtInstall] = []
        self.selected_aedt_win64: str = os.environ.get("HFSS_BENCHMARK_AEDT_WIN64", "")
        self.selected_aedt_choice_var: tk.StringVar | None = None
        self.case_checkbuttons_by_id: dict[str, ttk.Checkbutton] = {}
        self.case_base_texts: dict[str, str] = {}
        self.worker_proc: subprocess.Popen | None = None
        self.stop_requested = False
        self.run_start_ansys_snapshot: dict[str, set[int]] | None = None
        self.running = False
        self.model_photo_source_image = None
        self.model_photo_image = None
        self.model_photo_path: Path | None = None
        self.model_photo_label = None
        self.model_photo_canvas = None
        self.model_photo_frame = None
        self.model_photo_zoom = 1.0
        self._model_photo_resize_after_id = None
        self._last_model_photo_canvas_height = 0
        self._model_photo_wheel_resizing = False
        self.fixed_case_status_label = None
        self.model_intro_text_label = None

        self._configure_white_theme()
        self._build_ui()
        self._load_host_config_async()
        self.root.after(100, self._poll_queue)

    def _configure_white_theme(self) -> None:
        try:
            self.root.configure(bg=UI_BG)
        except Exception:
            pass

        try:
            style = ttk.Style(self.root)
            # Keep the current theme, but force the main application surfaces to white.
            style.configure(".", background=UI_BG)
            style.configure("TFrame", background=UI_BG)
            style.configure("TLabel", background=UI_BG)
            style.configure("TCheckbutton", background=UI_BG)
            style.configure("TLabelframe", background=UI_BG)
            style.configure("TLabelframe.Label", background=UI_BG)
            style.configure("TNotebook", background=UI_BG)
            style.configure("TNotebook.Tab", background=UI_BG)
            style.configure("Treeview", background=UI_BG, fieldbackground=UI_BG)
            style.configure("TEntry", fieldbackground=UI_BG)
        except Exception:
            pass

    def _queue_model_photo_resize(self, event=None) -> None:
        if self._model_photo_resize_after_id is not None:
            try:
                self.root.after_cancel(self._model_photo_resize_after_id)
            except Exception:
                pass
        self._model_photo_resize_after_id = self.root.after(120, self._update_model_photo_preview)

    def _on_model_photo_wheel(self, event) -> str:
        if self.model_photo_source_image is None:
            return "break"

        # Windows/macOS use MouseWheel delta; Linux may use Button-4/Button-5.
        direction = 0
        if getattr(event, "num", None) == 4:
            direction = 1
        elif getattr(event, "num", None) == 5:
            direction = -1
        else:
            delta = getattr(event, "delta", 0)
            direction = 1 if delta > 0 else -1 if delta < 0 else 0

        if direction:
            factor = 1.12 if direction > 0 else 1 / 1.12
            self.model_photo_zoom = max(0.55, min(4.00, self.model_photo_zoom * factor))
            self._model_photo_wheel_resizing = True
            try:
                self._update_model_photo_preview()
            finally:
                self._model_photo_wheel_resizing = False

        # Keep the wheel operation local to the model picture.
        return "break"

    def _update_model_photo_preview(self) -> None:
        self._model_photo_resize_after_id = None
        if self.model_photo_source_image is None or self.model_photo_canvas is None:
            return

        try:
            frame_width = self.model_photo_frame.winfo_width() if self.model_photo_frame is not None else 0
            root_height = self.root.winfo_height()
        except Exception:
            frame_width = 0
            root_height = 0

        # The picture is always fully visible. Wheel zoom grows/shrinks the
        # image height and pushes only the content below the picture downward.
        viewport_width = max(180, min(560, frame_width - 20 if frame_width > 40 else 560))
        base_view_height = max(95, min(145, int(root_height * 0.14) if root_height else 125))

        try:
            source_w, source_h = self.model_photo_source_image.size
            if source_w <= 0 or source_h <= 0:
                return

            # Start with a compact height. When zooming in, the width is capped
            # by the model column width, so the full image remains visible.
            base_scale = max(base_view_height / source_h, 1e-6)
            target_w = int(source_w * base_scale * self.model_photo_zoom)
            target_h = int(source_h * base_scale * self.model_photo_zoom)

            if target_w > viewport_width:
                fit_scale = viewport_width / source_w
                target_w = int(source_w * fit_scale)
                target_h = int(source_h * fit_scale)

            target_w = max(20, target_w)
            target_h = max(20, target_h)
            canvas_h = target_h

            old_h = int(getattr(self, "_last_model_photo_canvas_height", 0) or 0)

            try:
                self.model_photo_canvas.configure(width=viewport_width, height=canvas_h)
            except Exception:
                pass

            # During explicit wheel zoom, move the right-pane divider by the
            # picture height delta. The intro text above does not move, while
            # the text below the picture has room to move downward/upward.
            if self._model_photo_wheel_resizing and old_h > 0 and canvas_h != old_h:
                try:
                    h_total = self.right_paned.winfo_height()
                    current = self.right_paned.sashpos(0)
                    new_pos = current + (canvas_h - old_h)
                    new_pos = max(170, min(new_pos, max(180, h_total - 220)))
                    self.right_paned.sashpos(0, new_pos)
                except Exception:
                    pass

            self._last_model_photo_canvas_height = canvas_h

            img = render_model_photo_at_size(self.model_photo_source_image, target_w, target_h)
            if img is None:
                return

            self.model_photo_image = img
            self.model_photo_canvas.delete("all")
            self.model_photo_canvas.create_rectangle(
                0,
                0,
                viewport_width,
                canvas_h,
                fill=UI_BG,
                outline=UI_BG,
            )
            self.model_photo_canvas.create_image(
                viewport_width // 2,
                canvas_h // 2,
                image=self.model_photo_image,
                anchor="center",
            )
        except Exception:
            return

        # Keep the text below the picture adapted to the current model column
        # width. This stays inside the model-introduction column only.
        try:
            wrap = max(220, viewport_width)
            if self.fixed_case_status_label is not None:
                self.fixed_case_status_label.configure(wraplength=wrap)
            if self.model_intro_text_label is not None:
                self.model_intro_text_label.configure(wraplength=wrap)
        except Exception:
            pass

    def _build_ui(self) -> None:
        self.root.geometry("1240x860")
        self.root.minsize(1060, 760)

        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill=tk.BOTH, expand=True)

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        cases_tab = ttk.Frame(self.notebook, padding=10)
        results_tab = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(cases_tab, text="基本信息")
        self.notebook.add(results_tab, text="跑分结果")

        # ------------------------------------------------------------------
        # 基本信息页：左侧主机配置，右侧固定测试工程
        # ------------------------------------------------------------------
        selection_paned = ttk.PanedWindow(cases_tab, orient=tk.HORIZONTAL)
        selection_paned.pack(fill=tk.BOTH, expand=True)

        host_area = ttk.Frame(selection_paned)
        case_area = ttk.Frame(selection_paned)

        selection_paned.add(host_area, weight=3)
        selection_paned.add(case_area, weight=4)

        # -------------------- 左侧：主机配置 --------------------
        host_frame = ttk.LabelFrame(host_area, text="主机配置")
        host_frame.pack(fill=tk.BOTH, expand=True, padx=(0, 8))

        self.host_fields = [
            "操作系统",
            "CPU",
            "物理核心数",
            "逻辑线程数",
            "内存总量",
            "内存条数量",
            "内存插槽数",
            "内存频率",
            "内存通道",
            "通道来源",
            "数据位宽",
            "理论内存带宽",
            "内存条信息",
            "AEDT版本",
            "AnsysEM",
        ]
        self.host_vars: dict[str, tk.StringVar] = {}

        for row, field in enumerate(self.host_fields):
            name_label = ttk.Label(host_frame, text=field + "：", width=15, anchor="e")
            name_label.grid(row=row, column=0, sticky="ne", padx=(8, 4), pady=3)

            value_var = tk.StringVar(value="读取中...")
            value_label = ttk.Label(
                host_frame,
                textvariable=value_var,
                anchor="w",
                justify=tk.LEFT,
                wraplength=420,
            )
            value_label.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=3)
            self.host_vars[field] = value_var

        host_frame.columnconfigure(1, weight=1)

        host_buttons = ttk.Frame(host_area)
        host_buttons.pack(fill=tk.X, pady=(8, 0), padx=(0, 8))

        ttk.Label(host_buttons, text="HFSS版本：").pack(side=tk.LEFT)

        self.selected_aedt_choice_var = tk.StringVar(value="正在扫描...")
        self.aedt_combo = ttk.Combobox(
            host_buttons,
            textvariable=self.selected_aedt_choice_var,
            state="readonly",
            width=42,
        )
        self.aedt_combo.pack(side=tk.LEFT, padx=(4, 8), fill=tk.X, expand=True)
        self.aedt_combo.bind("<<ComboboxSelected>>", self.on_aedt_combo_selected)

        self.refresh_button = ttk.Button(host_buttons, text="刷新HFSS版本", command=self._load_host_config_async)
        self.refresh_button.pack(side=tk.LEFT)

        # -------------------- 右侧：测试模型与跑分入口 --------------------
        case = get_case(FIXED_CASE_ID)

        self.right_paned = ttk.PanedWindow(case_area, orient=tk.VERTICAL)
        self.right_paned.pack(fill=tk.BOTH, expand=True, padx=(8, 0))

        case_frame = ttk.LabelFrame(self.right_paned, text="测试模型介绍")
        run_frame = ttk.LabelFrame(self.right_paned, text="开始跑分")
        self.right_paned.add(case_frame, weight=1)
        self.right_paned.add(run_frame, weight=1)

        def _set_initial_right_sash():
            try:
                h = self.right_paned.winfo_height()
                if h > 500:
                    # ttk.PanedWindow keeps the same subtle divider style as the left/right split.
                    self.right_paned.sashpos(0, h // 2)
            except Exception:
                pass

        self.root.after(300, _set_initial_right_sash)

        self.fixed_case_available = False
        self.fixed_case_status_var = tk.StringVar(value="正在检查 HFSS/AEDT 环境...")
        self.case_vars: dict[str, tk.BooleanVar] = {case.case_id: tk.BooleanVar(value=True)}
        self.case_checkbuttons: list[ttk.Checkbutton] = []

        temp_root_text = r"%LOCALAPPDATA%\Temp"
        self.model_intro_text_label = ttk.Label(
            case_frame,
            text=(
                "测试采用 7 阶 interdigital 滤波器。理论中心频率为 6.13 GHz，"
                "理论带宽为 800 MHz，具体几何尺寸由 FilterSolutions 生成，并已内置在程序中。\n\n"
                f"HFSS Design：{case.design_name}\n"
                f"Benchmark：{SETUP_NAME} : {SWEEP_NAME}\n"
                f"临时工程位置：{temp_root_text}\\{BENCH_TEMP_PREFIX}*\n\n"
                "经验内存占用：该例程运行时约 1 Task 消耗 1 GB 内存。"
                "每轮临时工程默认在求解结束后删除；若调试模式保留失败工程，则可在上述临时目录中查看。"
            ),
            justify=tk.LEFT,
            wraplength=590,
        )
        self.model_intro_text_label.pack(fill=tk.X, padx=10, pady=(10, 6))

        self.model_photo_source_image, self.model_photo_path = load_model_photo_source_image()
        if self.model_photo_source_image is not None:
            self.model_photo_frame = ttk.Frame(case_frame)
            self.model_photo_frame.pack(fill=tk.X, padx=10, pady=(0, 6))
            self.model_photo_canvas = tk.Canvas(
                self.model_photo_frame,
                height=125,
                background=UI_BG,
                highlightthickness=0,
                takefocus=True,
            )
            self.model_photo_canvas.pack(fill=tk.X)
            self.model_photo_canvas.bind("<Configure>", self._queue_model_photo_resize)
            self.model_photo_canvas.bind("<Enter>", lambda event: self.model_photo_canvas.focus_set())
            self.model_photo_canvas.bind("<MouseWheel>", self._on_model_photo_wheel)
            self.model_photo_canvas.bind("<Button-4>", self._on_model_photo_wheel)
            self.model_photo_canvas.bind("<Button-5>", self._on_model_photo_wheel)
            self.root.bind("<Configure>", self._queue_model_photo_resize, add="+")
            self.root.after(200, self._update_model_photo_preview)

        self.fixed_case_status_label = ttk.Label(
            case_frame,
            textvariable=self.fixed_case_status_var,
            justify=tk.LEFT,
            wraplength=590,
        )
        self.fixed_case_status_label.pack(fill=tk.X, padx=10, pady=(0, 10))

        preview_frame = ttk.LabelFrame(run_frame, text="测试策略")
        preview_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 6))

        self.auto_tasks_label_var = tk.StringVar(value=self._build_auto_tasks_preview())
        ttk.Label(
            preview_frame,
            textvariable=self.auto_tasks_label_var,
            justify=tk.LEFT,
            wraplength=560,
        ).pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        action_frame = ttk.Frame(run_frame)
        action_frame.pack(fill=tk.X, padx=10, pady=(4, 10))

        self.start_button = ttk.Button(action_frame, text="开始跑分", command=self.start_benchmark)
        self.start_button.pack(side=tk.LEFT)

        self.continuous_test_var = tk.BooleanVar(value=False)
        self.continuous_rounds_var = tk.StringVar(value="")
        self.continuous_check = ttk.Checkbutton(
            action_frame,
            text="连续测试",
            variable=self.continuous_test_var,
            command=self._on_continuous_test_changed,
        )
        self.continuous_check.pack(side=tk.LEFT, padx=(8, 0))

        self.continuous_rounds_entry = ttk.Entry(
            action_frame,
            textvariable=self.continuous_rounds_var,
            width=6,
            state=tk.DISABLED,
        )
        self.continuous_rounds_entry.pack(side=tk.LEFT, padx=(4, 0))

        self.stop_button = ttk.Button(action_frame, text="终止跑分", command=self.stop_benchmark, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            action_frame,
            text=(
                "点击开始后，固定运行 Tasks=1 和满载 Tasks 两轮。"
                "终止跑分按钮按下后，需要等待几秒才能完全生效。"
            ),
            justify=tk.LEFT,
            wraplength=360,
        ).pack(side=tk.LEFT, padx=(10, 0), fill=tk.X, expand=True)

        # ------------------------------------------------------------------
        # 跑分结果页：结果表 + 白底柱状图 + 日志
        # ------------------------------------------------------------------
        result_paned = ttk.PanedWindow(results_tab, orient=tk.VERTICAL)
        result_paned.pack(fill=tk.BOTH, expand=True)

        result_top = ttk.Frame(result_paned)
        result_bottom = ttk.Frame(result_paned)
        result_paned.add(result_top, weight=5)
        result_paned.add(result_bottom, weight=2)

        result_actions = ttk.Frame(result_top)
        result_actions.pack(fill=tk.X, pady=(0, 6))

        self.export_button = ttk.Button(result_actions, text="导出跑分结果", command=self.export_results)
        self.export_button.pack(side=tk.LEFT)

        self.import_button = ttk.Button(result_actions, text="导入对比结果", command=self.import_results)
        self.import_button.pack(side=tk.LEFT, padx=(8, 0))

        self.delete_import_button = ttk.Button(result_actions, text="删除导入数据", command=self.delete_imported_results)
        self.delete_import_button.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(
            result_actions,
            text="导入后会在当前白底柱状图中叠加其他机器结果。",
        ).pack(side=tk.LEFT, padx=(10, 0))

        self.dataset_select_frame = ttk.LabelFrame(result_top, text="图中显示的数据")
        self.dataset_select_frame.pack(fill=tk.X, pady=(0, 6))

        self.dataset_menu_button = ttk.Menubutton(self.dataset_select_frame, text="选择显示数据")
        self.dataset_menu_button.pack(side=tk.LEFT, padx=8, pady=4)

        self.dataset_menu = tk.Menu(self.dataset_menu_button, tearoff=False)
        self.dataset_menu_button.configure(menu=self.dataset_menu)

        self.dataset_visible_summary_var = tk.StringVar(value="")
        ttk.Label(
            self.dataset_select_frame,
            textvariable=self.dataset_visible_summary_var,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, padx=(8, 0), pady=4)

        self._refresh_dataset_visibility_panel()

        chart_and_table = ttk.PanedWindow(result_top, orient=tk.HORIZONTAL)
        chart_and_table.pack(fill=tk.BOTH, expand=True)

        table_frame = ttk.LabelFrame(chart_and_table, text="结果表", width=430)
        chart_frame = ttk.LabelFrame(chart_and_table, text="图表")

        chart_and_table.add(table_frame, weight=0)
        chart_and_table.add(chart_frame, weight=1)

        columns = ("tasks", "points", "time", "rating", "speedup", "status")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=6)
        self.tree.heading("tasks", text="Tasks")
        self.tree.heading("points", text="点数")
        self.tree.heading("time", text="耗时")
        self.tree.heading("rating", text="点吞吐")
        self.tree.heading("speedup", text="吞吐加速")
        self.tree.heading("status", text="状态")

        self.tree.column("tasks", width=42, minwidth=38, anchor=tk.CENTER)
        self.tree.column("points", width=42, minwidth=38, anchor=tk.CENTER)
        self.tree.column("time", width=56, minwidth=50, anchor=tk.CENTER)
        self.tree.column("rating", width=68, minwidth=58, anchor=tk.CENTER)
        self.tree.column("speedup", width=68, minwidth=58, anchor=tk.CENTER)
        self.tree.column("status", width=78, minwidth=62, anchor=tk.W)

        yscroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=6)
        yscroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=6)

        self.chart_notebook = ttk.Notebook(chart_frame)
        self.chart_notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        bar_chart_tab = ttk.Frame(self.chart_notebook)
        round_chart_tab = ttk.Frame(self.chart_notebook)
        self.chart_notebook.add(bar_chart_tab, text="频点吞吐柱状图")
        self.chart_notebook.add(round_chart_tab, text="多轮跑分折线图")

        self.chart_canvas = tk.Canvas(bar_chart_tab, height=460, background=UI_BG, highlightthickness=1)
        self.chart_canvas.pack(fill=tk.BOTH, expand=True)
        self.chart_canvas.bind("<Configure>", lambda event: self._draw_rating_chart())

        self.round_chart_canvas = tk.Canvas(round_chart_tab, height=460, background=UI_BG, highlightthickness=1)
        self.round_chart_canvas.pack(fill=tk.BOTH, expand=True)
        self.round_chart_canvas.bind("<Configure>", lambda event: self._draw_round_chart())

        log_frame = ttk.LabelFrame(result_bottom, text="运行日志")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, height=9, wrap=tk.WORD, background=UI_BG)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=6)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=6)

    def _set_host_config(self, info: dict[str, str]) -> None:
        self.current_host_config = dict(info)

        installs_json = info.get("_aedt_installs_json", "")
        if installs_json:
            try:
                raw = json.loads(installs_json)
                installs = [aedt_install_from_dict(x) for x in raw if isinstance(x, dict)]
                self.aedt_installs = [x for x in installs if x is not None]
            except Exception:
                self.aedt_installs = []

        if info.get("_aedt_win64"):
            self.selected_aedt_win64 = info.get("_aedt_win64", "")

        self._refresh_aedt_combo()

        for field in self.host_fields:
            self.host_vars[field].set(info.get(field, "未知"))
        self._refresh_case_availability()
        self._refresh_dataset_visibility_panel()
        self._draw_rating_chart()
        self._draw_round_chart()

    def _set_host_loading(self) -> None:
        for field in self.host_fields:
            self.host_vars[field].set("读取中...")
        if self.selected_aedt_choice_var is not None:
            self.selected_aedt_choice_var.set("正在扫描 HFSS/AEDT 版本...")
        if hasattr(self, "aedt_combo"):
            self.aedt_combo.configure(state=tk.DISABLED)

    def _load_host_config_async(self) -> None:
        self._set_host_loading()
        thread = threading.Thread(target=self._host_worker, daemon=True)
        thread.start()

    def _host_worker(self) -> None:
        try:
            info = get_system_config_dict()

            installs = discover_aedt_installs()
            selected_install: AedtInstall | None = None

            if self.selected_aedt_win64:
                selected_norm = str(Path(self.selected_aedt_win64)).lower()
                for install in installs:
                    if str(install.win64_path).lower() == selected_norm:
                        selected_install = install
                        break

            if selected_install is None and installs:
                selected_install = installs[0]

            add_aedt_info_to_host_config(info, selected_install)
            info["_aedt_installs_json"] = json.dumps(
                [aedt_install_to_dict(x) for x in installs],
                ensure_ascii=False,
            )
            self.queue.put(("host", info))
        except Exception:
            self.queue.put(("host", {"CPU": "读取失败", "操作系统": traceback.format_exc()}))

    def _refresh_aedt_combo(self) -> None:
        combo = getattr(self, "aedt_combo", None)
        var = self.selected_aedt_choice_var
        if combo is None or var is None:
            return

        labels = [make_aedt_choice_label(x) for x in self.aedt_installs]
        combo.configure(values=labels)

        if not labels:
            var.set("未找到 HFSS/AEDT")
            combo.configure(state=tk.DISABLED)
            return

        selected_index = 0
        if self.selected_aedt_win64:
            target = str(Path(self.selected_aedt_win64)).lower()
            for i, install in enumerate(self.aedt_installs):
                if str(install.win64_path).lower() == target:
                    selected_index = i
                    break

        self.selected_aedt_win64 = str(self.aedt_installs[selected_index].win64_path)
        var.set(labels[selected_index])
        combo.configure(state=tk.NORMAL if not self.running else tk.DISABLED)

    def on_aedt_combo_selected(self, event=None) -> None:
        combo = getattr(self, "aedt_combo", None)
        if combo is None or not self.aedt_installs:
            return

        idx = combo.current()
        if idx < 0 or idx >= len(self.aedt_installs):
            return

        install = self.aedt_installs[idx]
        apply_aedt_install_environment(install)
        self.selected_aedt_win64 = str(install.win64_path)

        info = dict(self.current_host_config)
        add_aedt_info_to_host_config(info, install)
        self.current_host_config = info

        for field in self.host_fields:
            self.host_vars[field].set(info.get(field, "未知"))

        self._refresh_case_availability()
        self._refresh_dataset_visibility_panel()
        self._draw_rating_chart()
        self._draw_round_chart()
        self._append_log(f"已选择 HFSS/AEDT 版本：{install.display_name}，{install.win64_path}\n")

    def _refresh_case_availability(self) -> None:
        win64_text = self.current_host_config.get("_aedt_win64") or self.selected_aedt_win64
        available = bool(win64_text)
        self.fixed_case_available = available

        if hasattr(self, "fixed_case_status_var"):
            if available:
                self.fixed_case_status_var.set("状态：已识别到可用 HFSS/AEDT，可开始测试。")
            else:
                self.fixed_case_status_var.set("状态：未找到可用 HFSS/AEDT 安装。")

        if hasattr(self, "auto_tasks_label_var"):
            self.auto_tasks_label_var.set(self._build_auto_tasks_preview())

        if hasattr(self, "start_button") and not self.running:
            self.start_button.configure(state=tk.NORMAL if available else tk.DISABLED)

    def _get_selected_case_ids(self) -> list[str]:
        return [FIXED_CASE_ID] if getattr(self, "fixed_case_available", False) else []

    def _build_auto_tasks_preview(self) -> str:
        case = get_case(FIXED_CASE_ID)
        full_points = get_target_sweep_points()
        tasks, reason = get_auto_tasks_for_case(case)
        full_tasks = max(tasks)
        estimated_mem_gb = full_tasks
        lines = [
            "当前测试流程：",
            "1) 每轮自动创建测试模型，并创建 HFSS Modal Network 的 Setup1。",
            "2) 先执行自适应求解；该阶段不计入点吞吐跑分。",
            f"3) Tasks=1：插入 Discrete LinearCount Sweep，频点数={case.effective_frequency_points}，只统计 Sweep 耗时。",
            f"4) 满载测试：Tasks={full_tasks}，Sweep 频点数={full_points}，只统计 Sweep 耗时。",
            f"5) 满载内存粗略估算约 {estimated_mem_gb} GB（按 1 Task ≈ 1 GB 估计）。",
            "警告：如果预估满载时内存余量不足，不建议开始跑分，否则仿真可能明显变慢甚至卡住。",
            f"自动策略：{reason}",
        ]
        return "\n".join(lines)

    def _get_current_machine_label(self) -> str:
        if self.current_host_config:
            return "本机: " + make_machine_summary(self.current_host_config, "本机")
        return "本机"

    def _get_imported_dataset_id(self, index: int) -> str:
        return f"imported_{index + 1}"

    def _get_imported_machine_label(self, index: int, dataset: dict) -> str:
        host_config = dataset.get("host_config", {})
        return f"{index + 1}: " + make_machine_summary(host_config, f"机器{index + 1}")

    def _is_dataset_visible(self, dataset_id: str, default: bool = True) -> bool:
        var = self.dataset_visibility_vars.get(dataset_id)
        if var is None:
            var = tk.BooleanVar(value=default)
            self.dataset_visibility_vars[dataset_id] = var
        return bool(var.get())

    def _refresh_dataset_visibility_panel(self) -> None:
        """Refresh the dropdown menu controlling which machines are plotted."""
        menu = getattr(self, "dataset_menu", None)
        if menu is None:
            return

        menu.delete(0, tk.END)

        # 本机数据源
        current_id = "current"
        if current_id not in self.dataset_visibility_vars:
            self.dataset_visibility_vars[current_id] = tk.BooleanVar(value=True)

        def on_toggle() -> None:
            self._update_dataset_visible_summary()
            self._draw_rating_chart()
            self._draw_round_chart()

        menu.add_checkbutton(
            label=self._get_current_machine_label(),
            variable=self.dataset_visibility_vars[current_id],
            command=on_toggle,
        )

        if self.imported_datasets:
            menu.add_separator()

        valid_ids = {"current"}
        for i, dataset in enumerate(self.imported_datasets):
            dataset_id = self._get_imported_dataset_id(i)
            valid_ids.add(dataset_id)
            if dataset_id not in self.dataset_visibility_vars:
                self.dataset_visibility_vars[dataset_id] = tk.BooleanVar(value=True)

            menu.add_checkbutton(
                label=self._get_imported_machine_label(i, dataset),
                variable=self.dataset_visibility_vars[dataset_id],
                command=on_toggle,
            )

        # Remove stale variables left after deleting imported datasets.
        for key in list(self.dataset_visibility_vars.keys()):
            if key not in valid_ids:
                self.dataset_visibility_vars.pop(key, None)

        self._update_dataset_visible_summary()

    def _update_dataset_visible_summary(self) -> None:
        """Show a compact summary beside the dropdown button."""
        total = 1 + len(self.imported_datasets)
        visible = 0
        if self.dataset_visibility_vars.get("current", tk.BooleanVar(value=True)).get():
            visible += 1
        for i in range(len(self.imported_datasets)):
            dataset_id = self._get_imported_dataset_id(i)
            var = self.dataset_visibility_vars.get(dataset_id)
            if var is not None and var.get():
                visible += 1

        summary = f"已显示 {visible}/{total} 组数据；下拉菜单中显示完整机器配置"
        var = getattr(self, "dataset_visible_summary_var", None)
        if var is not None:
            var.set(summary)

    def _get_chart_datasets(self) -> list[dict]:
        datasets: list[dict] = []

        if self.results and self._is_dataset_visible("current", True):
            datasets.append(
                {
                    "dataset_id": "current",
                    "label": self._get_current_machine_label(),
                    "host_config": self.current_host_config,
                    "results": self.results,
                    "round_results": self.round_results,
                    "source": "current",
                }
            )

        for i, ds in enumerate(self.imported_datasets):
            dataset_id = self._get_imported_dataset_id(i)
            if not self._is_dataset_visible(dataset_id, True):
                continue
            label = self._get_imported_machine_label(i, ds)
            datasets.append(
                {
                    "dataset_id": dataset_id,
                    "label": label,
                    "host_config": ds.get("host_config", {}),
                    "results": ds.get("results", []),
                    "round_results": ds.get("round_results", []),
                    "source": "imported",
                }
            )
        return datasets

    def delete_imported_results(self) -> None:
        if not self.imported_datasets:
            messagebox.showinfo("没有导入数据", "当前没有可删除的导入数据。")
            return

        win = tk.Toplevel(self.root)
        win.title("删除导入数据")
        win.geometry("620x360")
        win.transient(self.root)
        win.grab_set()

        ttk.Label(win, text="请选择要删除的导入数据：").pack(anchor="w", padx=10, pady=(10, 4))

        list_frame = ttk.Frame(win)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)

        listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        for i, dataset in enumerate(self.imported_datasets):
            results_count = len(dataset.get("results", []))
            round_count = len(dataset.get("round_results", []))
            if round_count:
                count_text = f"{results_count} 条最终结果，{round_count} 条多轮记录"
            else:
                count_text = f"{results_count} 条结果"
            listbox.insert(tk.END, f"{i + 1}. {self._get_imported_machine_label(i, dataset)}    ({count_text})")

        button_frame = ttk.Frame(win)
        button_frame.pack(fill=tk.X, padx=10, pady=(4, 10))

        def do_delete() -> None:
            selected = list(listbox.curselection())
            if not selected:
                messagebox.showwarning("未选择", "请至少选择一条导入数据。", parent=win)
                return

            # Delete from back to front so indices remain valid.
            for idx in sorted(selected, reverse=True):
                if 0 <= idx < len(self.imported_datasets):
                    self.imported_datasets.pop(idx)

            # Rebuild visibility variables because imported dataset numbers changed.
            current_var = self.dataset_visibility_vars.get("current", tk.BooleanVar(value=True))
            self.dataset_visibility_vars = {"current": current_var}

            self._refresh_dataset_visibility_panel()
            self._draw_rating_chart()
            self._append_log(f"已删除 {len(selected)} 条导入数据。\\n")
            win.destroy()

        ttk.Button(button_frame, text="删除选中数据", command=do_delete).pack(side=tk.LEFT)
        ttk.Button(button_frame, text="取消", command=win.destroy).pack(side=tk.RIGHT)

    def export_results(self) -> None:
        if not self.results:
            messagebox.showwarning("没有可导出的结果", "当前还没有跑分结果。")
            return

        default_name = "hfss_pyaedt_benchmark_results_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
        path = filedialog.asksaveasfilename(
            title="导出跑分结果",
            defaultextension=".json",
            initialfile=default_name,
            filetypes=[("HFSS Benchmark Result", "*.json"), ("All Files", "*.*")],
        )
        if not path:
            return

        try:
            payload = make_export_payload(self.results, self.current_host_config, self.round_results)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            self._append_log(f"已导出跑分结果：{path}\n")
            messagebox.showinfo("导出完成", f"跑分结果已导出：\n{path}")
        except Exception:
            messagebox.showerror("导出失败", traceback.format_exc())

    def import_results(self) -> None:
        paths = filedialog.askopenfilenames(
            title="导入跑分结果",
            filetypes=[("HFSS Benchmark Result", "*.json"), ("All Files", "*.*")],
        )
        if not paths:
            return

        imported_count = 0
        try:
            for path in paths:
                with open(path, "r", encoding="utf-8") as f:
                    payload = json.load(f)

                if not isinstance(payload, dict):
                    raise RuntimeError(f"{path} 不是有效的 JSON 对象。")

                results_raw = payload.get("results", [])
                if not isinstance(results_raw, list):
                    raise RuntimeError(f"{path} 中没有有效 results 列表。")

                results = [one_run_result_from_dict(x) for x in results_raw if isinstance(x, dict)]

                round_results_raw = payload.get("round_results", [])
                if not isinstance(round_results_raw, list):
                    round_results_raw = []
                round_results = [one_run_result_from_dict(x) for x in round_results_raw if isinstance(x, dict)]

                host_config = payload.get("host_config", {})
                if not isinstance(host_config, dict):
                    host_config = {}

                for key in list(host_config.keys()):
                    if key == "Benchmark版本" or str(key).startswith("_benchmark"):
                        host_config.pop(key, None)

                self.imported_datasets.append(
                    {
                        "path": path,
                        "host_config": host_config,
                        "results": results,
                        "round_results": round_results,
                    }
                )
                imported_count += 1
                if round_results:
                    self._append_log(f"已导入对比结果：{path}，最终记录 {len(results)} 条，多轮原始记录 {len(round_results)} 条\n")
                else:
                    self._append_log(f"已导入对比结果：{path}，有效记录 {len(results)} 条\n")

            self._refresh_dataset_visibility_panel()
            self._draw_rating_chart()
            self._draw_round_chart()
            messagebox.showinfo("导入完成", f"已导入 {imported_count} 个结果文件。")
        except Exception:
            messagebox.showerror("导入失败", traceback.format_exc())

    def _on_continuous_test_changed(self) -> None:
        if not hasattr(self, "continuous_rounds_entry"):
            return
        enabled = bool(self.continuous_test_var.get()) and not self.running
        self.continuous_rounds_entry.configure(state=tk.NORMAL if enabled else tk.DISABLED)

    def _get_continuous_rounds_for_run(self) -> int:
        if not hasattr(self, "continuous_test_var") or not self.continuous_test_var.get():
            return 1

        raw = self.continuous_rounds_var.get().strip()
        try:
            rounds = int(raw)
            if rounds <= 0:
                raise ValueError("rounds must be positive")
            return rounds
        except Exception:
            self._append_log("WARNING: 已勾选连续测试，但连续轮次为空或非法，按 1 轮执行。\n")
            return 1

    def _set_running_controls(self, running: bool) -> None:
        self.start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)

        state = tk.DISABLED if running else tk.NORMAL
        self.export_button.configure(state=state)
        self.import_button.configure(state=state)
        self.delete_import_button.configure(state=state)
        self.refresh_button.configure(state=state)
        if hasattr(self, "aedt_combo"):
            self.aedt_combo.configure(state=tk.DISABLED if running or not self.aedt_installs else tk.NORMAL)
        if hasattr(self, "continuous_check"):
            self.continuous_check.configure(state=tk.DISABLED if running else tk.NORMAL)
        if hasattr(self, "continuous_rounds_entry"):
            entry_enabled = (not running) and bool(getattr(self, "continuous_test_var", tk.BooleanVar(value=False)).get())
            self.continuous_rounds_entry.configure(state=tk.NORMAL if entry_enabled else tk.DISABLED)

        if running:
            for cb in getattr(self, "case_checkbuttons", []):
                cb.configure(state=tk.DISABLED)
        else:
            self._refresh_case_availability()

    def _discard_current_run_results(self) -> None:
        self.results.clear()
        self.round_results.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._refresh_result_table_speedup()
        self._draw_rating_chart()
        self._draw_round_chart()

    def _cleanup_after_abort(self) -> None:
        snapshot = self.run_start_ansys_snapshot
        if snapshot is not None:
            cleanup_info = cleanup_new_ansys_processes(snapshot)
            if cleanup_info.killed_pids:
                self._append_log("已终止本次新开的 Ansys 进程 PID：" + ", ".join(map(str, cleanup_info.killed_pids)) + "\n")
            if cleanup_info.errors:
                self._append_log("终止 Ansys 进程时出现警告：\n" + "\n".join(cleanup_info.errors[:8]) + "\n")

        removed, errors = cleanup_benchmark_temp_dirs()
        self._append_log(f"已清理 benchmark 临时目录：{removed} 个。\n")
        if errors:
            self._append_log("部分临时目录清理失败，可能仍被系统占用：\n" + "\n".join(errors[:8]) + "\n")

    def stop_benchmark(self) -> None:
        if not self.running:
            return

        self.stop_requested = True
        self.stop_button.configure(state=tk.DISABLED)
        self._append_log("\n========== 用户请求终止跑分 ==========\n")
        self._append_log("正在停止 worker、终止本次新开的 Ansys 进程，并作废本次已跑结果。\n")

        proc = self.worker_proc
        if proc is not None and proc.poll() is None:
            ok, msg = kill_pid_tree(proc.pid)
            if ok:
                self._append_log(f"已终止 worker 进程 PID={proc.pid}。\n")
            else:
                self._append_log(f"终止 worker 进程 PID={proc.pid} 时出现警告：{msg}\n")

        self._discard_current_run_results()
        self._cleanup_after_abort()

    def start_benchmark(self) -> None:
        if self.running:
            return

        selected_case_ids = self._get_selected_case_ids()
        if not selected_case_ids:
            messagebox.showerror("工程不可用", "当前 HFSS/AEDT 版本下没有找到固定 SIW benchmark 工程。")
            return

        self.running = True
        self.stop_requested = False
        self.worker_proc = None
        self.run_start_ansys_snapshot = get_process_snapshot(ANSYS_PROCESS_NAMES_TO_CLEAN)
        self._discard_current_run_results()
        self._set_running_controls(True)
        repeat_rounds = self._get_continuous_rounds_for_run()

        self._append_log("\n========== 开始跑分 ==========\n")
        self._append_log("固定工程：" + get_case(FIXED_CASE_ID).display_name + "\n")
        self._append_log("使用 HFSS/AEDT： " + (self.selected_aedt_win64 or "未选择") + "\n")
        if repeat_rounds > 1:
            self._append_log(f"连续测试：共 {repeat_rounds} 轮，结果表显示各 Tasks 的平均值；多轮折线图显示每轮实际分数。\n")
        self._append_log(self._build_auto_tasks_preview() + "\n")

        thread = threading.Thread(target=self._benchmark_worker, args=(selected_case_ids, repeat_rounds), daemon=True)
        thread.start()

    def _benchmark_worker(self, selected_case_ids: list[str], repeat_rounds: int) -> None:
        try:
            case_ids_text = ",".join(selected_case_ids)
            cmd = get_self_command(["--worker", case_ids_text, str(max(1, int(repeat_rounds)))])

            self.queue.put(("log", "启动独立 worker 进程：\n" + " ".join(cmd) + "\n"))
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONLEGACYWINDOWSSTDIO"] = "0"
            if self.selected_aedt_win64:
                env["HFSS_BENCHMARK_AEDT_WIN64"] = self.selected_aedt_win64

            creationflags = 0
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                creationflags=creationflags,
            )
            self.worker_proc = proc

            results: list[OneRunResult] = []
            round_results: list[OneRunResult] = []

            assert proc.stdout is not None
            for line in proc.stdout:
                if line.startswith(WORKER_ROUND_RESULT_PREFIX):
                    payload = line[len(WORKER_ROUND_RESULT_PREFIX):].strip()
                    try:
                        data = json.loads(payload)
                        r = OneRunResult(**data)
                        if not self.stop_requested:
                            round_results.append(r)
                            self.queue.put(("round_result", r))
                    except Exception:
                        self.queue.put(("log", "解析 worker 多轮原始结果失败：\n" + traceback.format_exc() + "\n"))
                        self.queue.put(("log", line))
                elif line.startswith(WORKER_RESULT_PREFIX):
                    payload = line[len(WORKER_RESULT_PREFIX):].strip()
                    try:
                        data = json.loads(payload)
                        r = OneRunResult(**data)
                        if not self.stop_requested:
                            results.append(r)
                            self.queue.put(("result", r))
                    except Exception:
                        self.queue.put(("log", "解析 worker 结果失败：\n" + traceback.format_exc() + "\n"))
                        self.queue.put(("log", line))
                else:
                    self.queue.put(("log", line))

            ret = proc.wait()
            self.worker_proc = None
            self.queue.put(("log", f"\nworker 进程退出，返回码：{ret}\n"))

            if self.stop_requested:
                self.queue.put(("stopped", None))
            elif ret != 0 and not results and not round_results:
                self.queue.put(("error", f"worker 进程失败，返回码：{ret}。请查看运行日志。"))
            else:
                self.queue.put(("done", results))

        except Exception:
            self.worker_proc = None
            if self.stop_requested:
                self.queue.put(("stopped", None))
            else:
                self.queue.put(("error", traceback.format_exc()))

    def _append_log(self, text: str) -> None:
        self.log_text.insert(tk.END, text)
        self.log_text.see(tk.END)

    def _add_round_result(self, r: OneRunResult) -> None:
        self.round_results.append(r)
        self._draw_round_chart()

    def _add_result_row(self, r: OneRunResult) -> None:
        self.results.append(r)
        base_result = get_speedup_base_result_for_result(self.results, r)
        throughput = compute_point_throughput_score(r)
        speedup = compute_point_throughput_speedup(r, base_result)

        rating_text = f"{throughput:.0f}" if throughput else "-"
        speedup_text = f"{speedup:.2f}x" if speedup else "-"
        time_text = f"{r.solve_seconds:.2f}" if r.solve_seconds else "-"
        self.tree.insert(
            "",
            tk.END,
            values=(r.tasks, getattr(r, "sweep_points", 0), time_text, rating_text, speedup_text, r.status),
        )

        self._refresh_result_table_speedup()

    def _draw_rating_chart(self) -> None:
        canvas = getattr(self, "chart_canvas", None)
        if canvas is None:
            return

        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()

        if width < 260 or height < 220:
            canvas.configure(background="#ffffff")
            canvas.create_text(
                max(width, 1) // 2,
                max(height, 1) // 2,
                text="窗口过小，放大窗口后显示柱状图",
                anchor="center",
                fill="#4b5563",
                font=("Segoe UI", 9),
            )
            return

        bg = "#ffffff"
        panel = "#f6f8fb"
        grid = "#d9e0ea"
        axis = "#596579"
        text_color = "#1f2937"
        sub_text = "#4b5563"
        gold = "#f59e0b"
        machine_colors = [
            "#2563eb",
            "#dc2626",
            "#059669",
            "#7c3aed",
            "#ea580c",
            "#0891b2",
            "#db2777",
            "#65a30d",
        ]

        canvas.configure(background=bg)

        left_margin = 72 if width >= 520 else 54
        right_margin = 16 if width >= 520 else 8

        datasets = self._get_chart_datasets()
        valid_items = []
        for dataset_index, dataset in enumerate(datasets):
            for r in dataset.get("results", []):
                score = compute_point_throughput_score(r)
                if score and score > 0:
                    valid_items.append((dataset_index, dataset, r, score))

        canvas.create_text(
            width // 2,
            10,
            text="HFSS 频点吞吐量对比（points/day，越高越好）",
            anchor="n",
            fill=text_color,
            font=("Segoe UI", 11 if width >= 520 else 9, "bold"),
        )

        if not valid_items:
            canvas.create_text(
                width // 2,
                height // 2,
                text="暂无有效频点吞吐数据；可先跑分，或导入已导出的结果文件",
                anchor="center",
                fill=sub_text,
                font=("Segoe UI", 10),
                width=max(width - 40, 120),
            )
            return

        # Top machine legend, wrapped.
        label_font = ("Segoe UI", 8 if width >= 520 else 7)
        legend_start_y = 38
        legend_left = left_margin
        legend_right = right_margin

        if width >= 1180 and len(datasets) >= 3:
            legend_cols = 3
        elif width >= 760 and len(datasets) >= 2:
            legend_cols = 2
        else:
            legend_cols = 1

        legend_total_w = max(width - legend_left - legend_right, 100)
        legend_col_w = max(120, legend_total_w / legend_cols)
        label_text_w = max(80, int(legend_col_w - 26))
        labels = [" ".join(str(dataset.get("label") or f"机器{i + 1}").split()) for i, dataset in enumerate(datasets)]

        def estimate_lines(label: str, text_w: int) -> int:
            avg_px = 6.0 if width >= 520 else 5.3
            return max(1, int((len(label) * avg_px) // max(text_w, 60)) + 1)

        row_heights: list[int] = []
        for row_start in range(0, len(labels), legend_cols):
            row_labels = labels[row_start: row_start + legend_cols]
            max_lines = max(estimate_lines(label, label_text_w) for label in row_labels)
            row_heights.append(max(18, 12 * max_lines + 6))

        y = legend_start_y
        for row_index, row_start in enumerate(range(0, len(labels), legend_cols)):
            row_labels = labels[row_start: row_start + legend_cols]
            row_h = row_heights[row_index]
            for col_index, label in enumerate(row_labels):
                dataset_index = row_start + col_index
                color = machine_colors[dataset_index % len(machine_colors)]
                x = legend_left + col_index * legend_col_w
                canvas.create_rectangle(x, y - 5, x + 12, y + 7, fill=color, outline=color)
                canvas.create_text(
                    x + 18,
                    y,
                    text=label,
                    anchor="nw",
                    fill=sub_text,
                    font=label_font,
                    width=label_text_w,
                )
            y += row_h

        top_margin = max(72, y + 8)
        bottom_margin = 112 if width < 680 else 92
        plot_w = width - left_margin - right_margin
        plot_h = height - top_margin - bottom_margin

        if plot_w < 140 or plot_h < 90:
            canvas.create_text(
                width // 2,
                max(top_margin + 20, height // 2),
                text="当前窗口给柱状图留下的空间太小，请稍微放大窗口或拖动分隔栏",
                anchor="center",
                fill=sub_text,
                font=("Segoe UI", 9),
                width=max(width - 40, 120),
            )
            return

        max_score = max(score for _di, _ds, _r, score in valid_items)
        chart_max = max_score * 1.08 if max_score > 0 else 1.0

        # Group by machine. Each machine normally has two bars: T1 default and Tmax full-load.
        grouped: dict[int, list[tuple[OneRunResult, float]]] = {}
        for dataset_index, _dataset, r, score in valid_items:
            grouped.setdefault(dataset_index, []).append((r, score))

        groups = []
        for dataset_index in sorted(grouped.keys()):
            runs = sorted(grouped[dataset_index], key=lambda item: (item[0].tasks != 1, item[0].tasks))
            groups.append((dataset_index, runs))

        # Wrap machine groups into multiple chart rows so many imported datasets do not overflow.
        base_bar_w = 24.0
        pair_gap = 8.0
        machine_gap = 32.0
        usable_w = max(plot_w - 16, 60)

        rows: list[list[tuple[int, list[tuple[OneRunResult, float]], float]]] = []
        current_row: list[tuple[int, list[tuple[OneRunResult, float]], float]] = []
        current_w = 0.0
        for dataset_index, runs in groups:
            group_w = max(1, len(runs)) * base_bar_w + max(0, len(runs) - 1) * pair_gap
            extra_gap = machine_gap if current_row else 0.0
            if current_row and current_w + extra_gap + group_w > usable_w:
                rows.append(current_row)
                current_row = [(dataset_index, runs, group_w)]
                current_w = group_w
            else:
                current_row.append((dataset_index, runs, group_w))
                current_w += extra_gap + group_w
        if current_row:
            rows.append(current_row)

        row_gap = 20.0
        available_plot_h = plot_h - row_gap * max(0, len(rows) - 1)
        row_plot_h = available_plot_h / max(1, len(rows))
        if row_plot_h < 48:
            canvas.create_text(
                width // 2,
                height // 2,
                text="导入数据较多，当前窗口高度不足；请放大窗口，或在“选择显示数据”中隐藏部分机器。",
                anchor="center",
                fill=sub_text,
                font=("Segoe UI", 9),
                width=max(width - 40, 120),
            )
            return

        x0 = left_margin
        x1 = width - right_margin

        tick_count = 4 if len(rows) <= 2 else 3
        bar_font = ("Segoe UI", 7 if width >= 520 else 6)
        show_bar_values = row_plot_h >= 90
        show_task_labels = row_plot_h >= 60

        for row_idx, row_groups in enumerate(rows):
            row_y1 = top_margin + row_idx * (row_plot_h + row_gap)
            row_y0 = row_y1 + row_plot_h
            canvas.create_rectangle(x0, row_y1, x1, row_y0, fill=panel, outline="#cfd8e3")

            for i in range(tick_count + 1):
                value = chart_max * i / tick_count
                yy = row_y0 - row_plot_h * i / tick_count
                canvas.create_line(x0 - 5, yy, x0, yy, fill=axis)
                canvas.create_text(
                    x0 - 8,
                    yy,
                    text=f"{value:.0f}",
                    anchor="e",
                    fill=sub_text,
                    font=("Segoe UI", 8 if width >= 520 else 7),
                )
                if i > 0:
                    canvas.create_line(x0, yy, x1, yy, fill=grid, dash=(3, 4))

            canvas.create_line(x0, row_y0, x1, row_y0, fill=axis, width=1)
            canvas.create_line(x0, row_y0, x0, row_y1, fill=axis, width=1)

            # Center the bars of this row inside the plot area instead of
            # always starting from the left edge. This matters when imported
            # datasets wrap into multiple rows and the last row has fewer bars.
            row_content_w = 0.0
            for _dataset_index, runs, _group_w in row_groups:
                row_content_w += max(1, len(runs)) * base_bar_w
                row_content_w += max(0, len(runs) - 1) * pair_gap
            row_content_w += max(0, len(row_groups) - 1) * machine_gap

            row_start_x = x0 + max(8.0, (plot_w - row_content_w) / 2.0)
            x_cursor = row_start_x

            for dataset_index, runs, _group_w in row_groups:
                color = machine_colors[dataset_index % len(machine_colors)]
                best_score = max(score for _r, score in runs)

                for r, score in runs:
                    bar_h = (score / chart_max) * row_plot_h
                    xl = x_cursor
                    xr = x_cursor + base_bar_w
                    yt = row_y0 - bar_h

                    canvas.create_rectangle(xl + 2, yt + 3, xr + 2, row_y0, fill="#e5e7eb", outline="")
                    outline = gold if abs(score - best_score) < 1e-9 else "#344054"
                    outline_width = 2 if outline == gold else 1
                    canvas.create_rectangle(xl, yt, xr, row_y0, fill=color, outline=outline, width=outline_width)

                    if r.tasks == 1:
                        canvas.create_rectangle(xl, yt, xr, row_y0, fill="#ffffff", outline="", stipple="gray50")
                        # Re-draw the outline because stipple overlay hides it on some Tk themes.
                        canvas.create_rectangle(xl, yt, xr, row_y0, outline=outline, width=outline_width)

                    if show_bar_values:
                        canvas.create_text(
                            (xl + xr) / 2,
                            yt - 4,
                            text=f"{score:.0f}",
                            anchor="s",
                            fill=text_color,
                            font=bar_font,
                        )

                    if show_task_labels:
                        label = "T1\n单任务" if r.tasks == 1 else f"T{r.tasks}\n满载"
                        canvas.create_text(
                            (xl + xr) / 2,
                            row_y0 + 4,
                            text=label,
                            anchor="n",
                            fill=sub_text,
                            font=bar_font,
                        )

                    x_cursor += base_bar_w + pair_gap

                x_cursor += machine_gap

        # Bottom legend.
        legend_y = height - (58 if width < 680 else 34)

        canvas.create_rectangle(
            left_margin,
            legend_y - 7,
            left_margin + 14,
            legend_y + 7,
            fill="#2563eb",
            outline="#344054",
        )
        canvas.create_rectangle(
            left_margin,
            legend_y - 7,
            left_margin + 14,
            legend_y + 7,
            fill="#ffffff",
            outline="",
            stipple="gray50",
        )
        canvas.create_text(
            left_margin + 20,
            legend_y,
            text="T1：建模基准 10点",
            anchor="w",
            fill=sub_text,
            font=("Segoe UI", 8),
        )

        if width >= 680:
            x2 = left_margin + 160
            y2 = legend_y
            x3 = left_margin + 320
            y3 = legend_y
        else:
            x2 = left_margin
            y2 = legend_y + 22
            x3 = left_margin
            y3 = legend_y + 44

        canvas.create_rectangle(
            x2,
            y2 - 7,
            x2 + 14,
            y2 + 7,
            fill="#2563eb",
            outline="#344054",
        )
        canvas.create_text(
            x2 + 20,
            y2,
            text="满载：建模满载线性",
            anchor="w",
            fill=sub_text,
            font=("Segoe UI", 8),
        )

        canvas.create_rectangle(
            x3,
            y3 - 7,
            x3 + 14,
            y3 + 7,
            fill="#ffffff",
            outline=gold,
            width=2,
        )
        canvas.create_text(
            x3 + 20,
            y3,
            text="金色边框：该机器点吞吐最高",
            anchor="w",
            fill=sub_text,
            font=("Segoe UI", 8),
            width=max(width - x3 - right_margin - 24, 120),
        )

    def _draw_round_chart(self) -> None:
        canvas = getattr(self, "round_chart_canvas", None)
        if canvas is None:
            return

        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        canvas.configure(background=UI_BG)

        if width < 260 or height < 220:
            canvas.create_text(
                max(width, 1) // 2,
                max(height, 1) // 2,
                text="窗口过小，放大窗口后显示多轮折线图",
                anchor="center",
                fill="#4b5563",
                font=("Segoe UI", 9),
                width=max(width - 40, 120),
            )
            return

        panel = "#f6f8fb"
        grid = "#d9e0ea"
        axis = "#596579"
        text_color = "#1f2937"
        sub_text = "#4b5563"
        palette = [
            "#2563eb",
            "#dc2626",
            "#059669",
            "#7c3aed",
            "#ea580c",
            "#0891b2",
            "#db2777",
            "#65a30d",
            "#b45309",
            "#0f766e",
        ]

        datasets = self._get_chart_datasets()
        line_items = []

        for dataset_index, dataset in enumerate(datasets):
            raw_results = dataset.get("round_results", [])
            if not raw_results:
                continue

            valid = [
                r for r in raw_results
                if getattr(r, "benchmark_valid", False)
                and float(getattr(r, "solve_seconds", 0.0) or 0.0) > 0
                and int(getattr(r, "round_index", 0) or 0) > 0
                and int(getattr(r, "repeat_rounds", 1) or 1) > 1
            ]
            if not valid:
                continue

            grouped: dict[int, list[tuple[int, float]]] = {}
            for r in valid:
                score = compute_point_throughput_score(r)
                if not score or score <= 0:
                    continue
                grouped.setdefault(int(r.tasks), []).append((int(getattr(r, "round_index", 0) or 0), score))

            for tasks, pts in sorted(grouped.items(), key=lambda item: (item[0] != 1, item[0])):
                pts = sorted(pts, key=lambda item: item[0])
                if not pts:
                    continue
                task_label = "T1 单任务" if tasks == 1 else f"T{tasks} 满载"
                label = f"{dataset.get('label', '当前机器')} / {task_label}"
                line_items.append(
                    {
                        "dataset_index": dataset_index,
                        "tasks": tasks,
                        "label": label,
                        "points": pts,
                    }
                )

        canvas.create_text(
            width // 2,
            10,
            text="多轮跑分实际频点吞吐折线图（points/day）",
            anchor="n",
            fill=text_color,
            font=("Segoe UI", 11 if width >= 520 else 9, "bold"),
        )

        if not line_items:
            canvas.create_text(
                width // 2,
                height // 2,
                text="暂无多轮跑分原始数据；单轮跑分不会在此页显示。",
                anchor="center",
                fill=sub_text,
                font=("Segoe UI", 10),
                width=max(width - 40, 120),
            )
            return

        left_margin = 74 if width >= 520 else 58
        right_margin = 18 if width >= 520 else 10

        legend_top = 38
        legend_left = left_margin
        legend_right = right_margin
        legend_w = max(width - legend_left - legend_right, 120)
        legend_font = ("Segoe UI", 8 if width >= 520 else 7)
        legend_cols = 2 if width >= 900 and len(line_items) > 1 else 1
        col_w = legend_w / legend_cols
        line_h = 18
        visible_legend_count = min(len(line_items), max(1, legend_cols * 5))
        legend_rows = (visible_legend_count + legend_cols - 1) // legend_cols
        legend_h = legend_rows * line_h + 8

        for i, item in enumerate(line_items[:visible_legend_count]):
            col = i % legend_cols
            row = i // legend_cols
            x = legend_left + col * col_w
            y = legend_top + row * line_h
            color = palette[i % len(palette)]
            canvas.create_line(x, y + 7, x + 16, y + 7, fill=color, width=2)
            canvas.create_oval(x + 6, y + 3, x + 10, y + 11, fill=color, outline=color)
            canvas.create_text(
                x + 22,
                y,
                text=item["label"],
                anchor="nw",
                fill=sub_text,
                font=legend_font,
                width=max(90, int(col_w - 28)),
            )

        if len(line_items) > visible_legend_count:
            canvas.create_text(
                legend_left,
                legend_top + legend_rows * line_h,
                text=f"还有 {len(line_items) - visible_legend_count} 条曲线未列入图例，但仍绘制在图中。",
                anchor="nw",
                fill=sub_text,
                font=legend_font,
                width=max(width - left_margin - right_margin, 120),
            )
            legend_h += 20

        top_margin = max(82, legend_top + legend_h + 12)
        bottom_margin = 50
        plot_w = width - left_margin - right_margin
        plot_h = height - top_margin - bottom_margin

        if plot_w < 160 or plot_h < 100:
            canvas.create_text(
                width // 2,
                height // 2,
                text="当前窗口给折线图留下的空间太小，请放大窗口或拖动分隔栏。",
                anchor="center",
                fill=sub_text,
                font=("Segoe UI", 9),
                width=max(width - 40, 120),
            )
            return

        all_points = [pt for item in line_items for pt in item["points"]]
        max_round = max(round_idx for round_idx, _score in all_points)
        min_round = min(round_idx for round_idx, _score in all_points)
        if max_round == min_round:
            max_round = min_round + 1

        max_score = max(score for _round_idx, score in all_points)
        chart_max = max(max_score * 1.08, 1.0)

        x0 = left_margin
        y1 = top_margin
        x1 = width - right_margin
        y0 = top_margin + plot_h

        canvas.create_rectangle(x0, y1, x1, y0, fill=panel, outline="#cfd8e3")

        tick_count = 4
        for i in range(tick_count + 1):
            value = chart_max * i / tick_count
            yy = y0 - plot_h * i / tick_count
            canvas.create_line(x0 - 5, yy, x0, yy, fill=axis)
            canvas.create_text(
                x0 - 8,
                yy,
                text=f"{value:.0f}",
                anchor="e",
                fill=sub_text,
                font=("Segoe UI", 8 if width >= 520 else 7),
            )
            if i > 0:
                canvas.create_line(x0, yy, x1, yy, fill=grid, dash=(3, 4))

        round_span = max_round - min_round
        if round_span <= 10:
            x_ticks = list(range(min_round, max_round + 1))
        else:
            x_ticks = sorted(set(round(min_round + round_span * i / 5) for i in range(6)))

        def x_for_round(round_idx: int) -> float:
            return x0 + (round_idx - min_round) / (max_round - min_round) * plot_w

        def y_for_score(score: float) -> float:
            return y0 - score / chart_max * plot_h

        for r_idx in x_ticks:
            xx = x_for_round(int(r_idx))
            canvas.create_line(xx, y0, xx, y0 + 5, fill=axis)
            canvas.create_text(
                xx,
                y0 + 8,
                text=str(int(r_idx)),
                anchor="n",
                fill=sub_text,
                font=("Segoe UI", 8 if width >= 520 else 7),
            )
            if r_idx != min_round:
                canvas.create_line(xx, y1, xx, y0, fill=grid, dash=(2, 6))

        canvas.create_line(x0, y0, x1, y0, fill=axis, width=1)
        canvas.create_line(x0, y0, x0, y1, fill=axis, width=1)
        canvas.create_text(
            (x0 + x1) / 2,
            height - 16,
            text="轮次",
            anchor="center",
            fill=sub_text,
            font=("Segoe UI", 8),
        )

        for i, item in enumerate(line_items):
            color = palette[i % len(palette)]
            pts_xy = [(x_for_round(round_idx), y_for_score(score)) for round_idx, score in item["points"]]
            if len(pts_xy) >= 2:
                flat = []
                for x, y in pts_xy:
                    flat.extend([x, y])
                canvas.create_line(*flat, fill=color, width=2, smooth=False)
            for x, y in pts_xy:
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=color, outline=UI_BG)

            if len(line_items) <= 6 and pts_xy:
                last_x, last_y = pts_xy[-1]
                last_score = item["points"][-1][1]
                canvas.create_text(
                    min(last_x + 6, x1 - 4),
                    last_y,
                    text=f"{last_score:.0f}",
                    anchor="w" if last_x < x1 - 30 else "e",
                    fill=color,
                    font=("Segoe UI", 7),
                )

    def _refresh_result_table_speedup(self) -> None:
        items = self.tree.get_children()
        for item, r in zip(items, self.results):
            base_result = get_speedup_base_result_for_result(self.results, r)
            throughput = compute_point_throughput_score(r)
            speedup = compute_point_throughput_speedup(r, base_result)
            rating_text = f"{throughput:.0f}" if throughput else "-"
            speedup_text = f"{speedup:.2f}x" if speedup else "-"
            time_text = f"{r.solve_seconds:.2f}" if r.solve_seconds else "-"
            self.tree.item(
                item,
                values=(r.tasks, getattr(r, "sweep_points", 0), time_text, rating_text, speedup_text, r.status),
            )
        self._draw_rating_chart()
        self._draw_round_chart()

    def _finish_run(self, results: list[OneRunResult]) -> None:
        self.running = False
        self.stop_requested = False
        self.worker_proc = None
        self.run_start_ansys_snapshot = None
        self._set_running_controls(False)

        valid_results = [r for r in results if r.benchmark_valid]
        if valid_results:
            best = max(valid_results, key=compute_point_throughput_score)
            best_score = compute_point_throughput_score(best)
            messagebox.showinfo(
                "跑分完成",
                f"Best Point Throughput: {best_score:.0f} points/day\n"
                f"Tasks: {best.tasks}\n"
                f"Sweep Points: {get_points_for_result(best)}\n"
                f"Best Time: {best.solve_seconds:.2f} s"
            )
        else:
            messagebox.showwarning("跑分结束", "没有得到有效结果，请查看日志。")

    def _finish_aborted(self) -> None:
        self._discard_current_run_results()
        self._cleanup_after_abort()

        self.running = False
        self.stop_requested = False
        self.worker_proc = None
        self.run_start_ansys_snapshot = None
        self._set_running_controls(False)

        self._append_log("本次跑分已终止，本次已跑结果已作废。\n")
        messagebox.showinfo("跑分已终止", "当前跑分已终止，本次已跑结果已作废，临时目录已尝试清理。")

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "host":
                    self._set_host_config(payload)  # type: ignore[arg-type]
                elif kind == "result":
                    if not self.stop_requested:
                        self._add_result_row(payload)  # type: ignore[arg-type]
                elif kind == "round_result":
                    if not self.stop_requested:
                        self._add_round_result(payload)  # type: ignore[arg-type]
                elif kind == "done":
                    if self.stop_requested:
                        self._finish_aborted()
                    else:
                        self._finish_run(payload)  # type: ignore[arg-type]
                elif kind == "stopped":
                    self._finish_aborted()
                elif kind == "error":
                    self.running = False
                    self.stop_requested = False
                    self.worker_proc = None
                    self.run_start_ansys_snapshot = None
                    self._set_running_controls(False)
                    messagebox.showerror("运行错误", str(payload)[:3000])
        except queue.Empty:
            pass

        self.root.after(100, self._poll_queue)


def main() -> int:
    setup_utf8_stdio()
    if os.name != "nt":
        print("这个程序面向 Windows 版 HFSS/AEDT。", file=sys.stderr)
        return 2

    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        repeat_rounds = 1
        if len(sys.argv) >= 4:
            try:
                repeat_rounds = int(sys.argv[3])
                if repeat_rounds <= 0:
                    raise ValueError
            except Exception:
                print("WARNING: 连续测试轮次参数非法，按 1 轮执行。", file=sys.stderr)
                repeat_rounds = 1
        return worker_main(sys.argv[2], repeat_rounds)

    root = tk.Tk()
    app = HfssBenchmarkApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
