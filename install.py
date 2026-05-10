"""WhisperVoice Installer v2.0 - install.py

Brain of the two-file installer. The bootstrap (install.bat) installs Python
and runs this script for the actual install work. See
docs/INSTALLER_V2_ARCHITECTURE.md for the design.

Phases:
  1. Pre-flight: hardware detect, network/dependency checks
  2. User input: install path, model selection
  3. Install:    download files, venv, deps, model, configs, shortcut
  4. Final:      summary, persist log, wait for user

The installer never auto-closes. Every error is logged in full to install.log
and a friendly one-liner goes to the console.
"""

import ctypes
import datetime
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from ctypes import POINTER, byref, c_int, c_ubyte, c_ulong, c_ushort, c_void_p, c_wchar_p
from pathlib import Path

try:
    import winreg  # Windows-only stdlib module
except ImportError:
    winreg = None  # py_compile / non-Windows dev hosts

try:
    import pynvml  # bootstrap-installed; may be missing on weird envs
except ImportError:
    pynvml = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INSTALLER_VERSION = "2.0.0"
SERVER_URL = "http://193.233.19.237:8080"
DEFAULT_INSTALL_PATH = r"C:\WhisperVoice"

PROJECT_FILES = ["main.py", "live_preview.py", "requirements.txt", "config.yaml.example"]
ASSET_FILES = ["assets/whispervoice.ico"]

GLOSSARY = (
    "MediaMTX, NVR, Docker, Keycloak, PostgreSQL, MaMaison, DEV1, DEV2, "
    "RAID6, EPYC, Supermicro, Claude, Obsidian, PCIe, NVMe, SSD, HDD, "
    "SSH, CUDA, Whisper, TESLATEL, Cloud."
)

MODEL_CHOICES = ["small", "medium", "large-v3"]
MODEL_DOWNLOAD_GB = {"small": 0.25, "medium": 0.8, "large-v3": 3.0}
RUNTIME_OVERHEAD_GB = {"cuda": 4.5, "cpu": 1.5}

NETWORK_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 60
PIP_CHECK_URL = "https://pypi.org/simple/"
HF_CHECK_URL = "https://huggingface.co/"

TEMP_LOG_PATH = Path(tempfile.gettempdir()) / "wv-install.log"


# Must be set before any faster-whisper / huggingface_hub activity.
# Without this the model download segfaults on fresh Windows (CHANGELOG v1.1.0).
os.environ["HF_HUB_DISABLE_XET"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("wv-installer")
_file_handler: logging.FileHandler | None = None


def setup_logging(log_path: Path) -> None:
    global _file_handler
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    _file_handler = fh


def relocate_log(new_path: Path) -> None:
    """Move accumulated log to its final home and continue logging there."""
    global _file_handler
    if _file_handler is not None:
        _file_handler.close()
        logger.removeHandler(_file_handler)
        _file_handler = None
    new_path.parent.mkdir(parents=True, exist_ok=True)
    if TEMP_LOG_PATH.exists() and TEMP_LOG_PATH.resolve() != new_path.resolve():
        shutil.copy2(TEMP_LOG_PATH, new_path)
    setup_logging(new_path)


def info(msg: str, console: str | None = None) -> None:
    logger.info(msg)
    print(console if console is not None else msg)


def warn(msg: str, console: str | None = None) -> None:
    logger.warning(msg)
    print(console if console is not None else f"[WARN] {msg}")


def error(msg: str, console: str | None = None) -> None:
    logger.error(msg)
    print(console if console is not None else f"[FAIL] {msg}")


def debug(msg: str) -> None:
    logger.debug(msg)


def section(title: str) -> None:
    bar = "=" * 60
    logger.info(bar)
    logger.info(title)
    logger.info(bar)
    print()
    print(bar)
    print(f"  {title}")
    print(bar)


def log_exception(prefix: str, exc: BaseException) -> None:
    logger.error("%s: %s", prefix, exc)
    logger.error(traceback.format_exc())


# ---------------------------------------------------------------------------
# User prompts
# ---------------------------------------------------------------------------

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            answer = input(f"{prompt}{suffix}: ").strip()
        except EOFError:
            return default
        if answer:
            return answer
        if default:
            return default


def ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            answer = input(f"{prompt} ({hint}): ").strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def pause_exit(message: str = "Press Enter to exit...") -> None:
    try:
        input(message)
    except EOFError:
        pass


def bail(msg: str, exit_code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    error(msg, console=f"\n[ABORT] {msg}")
    print(f"\nFull log: {_current_log_path()}")
    pause_exit()
    sys.exit(exit_code)


def _current_log_path() -> Path:
    if _file_handler is not None:
        return Path(_file_handler.baseFilename)
    return TEMP_LOG_PATH


# ---------------------------------------------------------------------------
# Step runner
# ---------------------------------------------------------------------------

def run_step(name: str, fn, *, critical: bool = True, console_label: str | None = None):
    """Run fn() with full error logging.

    Critical failures bail. Non-critical failures ask the user whether to
    continue. The exception (if any) is always logged with traceback.
    """
    label = console_label or name
    info(f"step '{name}' start", console=f"  -> {label}")
    try:
        result = fn()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log_exception(f"step '{name}' failed", exc)
        short = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        print(f"     [FAIL] {short}")
        print(f"            see {_current_log_path()} for details")
        if critical:
            bail(f"cannot continue without '{name}'")
        if not ask_yes_no("Continue anyway?", default=False):
            bail("user cancelled after step failure")
        return None
    info(f"step '{name}' ok")
    return result


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def http_reachable(url: str, timeout: int = NETWORK_TIMEOUT) -> bool:
    """HEAD-style probe via GET with a tiny read. Treats any 2xx/3xx as up."""
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": f"wv-installer/{INSTALLER_VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(1)
            return 200 <= resp.status < 400
    except Exception as exc:
        debug(f"http_reachable({url}) failed: {exc}")
        return False


def download(url: str, dest: Path, timeout: int = DOWNLOAD_TIMEOUT) -> int:
    """Download url to dest. Returns bytes written. Overwrites existing file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": f"wv-installer/{INSTALLER_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as out:
        total = 0
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
        return total


# ---------------------------------------------------------------------------
# Phase 1 - preflight
# ---------------------------------------------------------------------------

def detect_os() -> dict:
    win_ver = sys.getwindowsversion() if hasattr(sys, "getwindowsversion") else None
    arch = platform.machine().lower()
    release = platform.release()
    build = win_ver.build if win_ver else 0
    is_supported = (win_ver is not None and win_ver.major >= 10 and arch in ("amd64", "x86_64"))
    return {
        "release": release,
        "build": build,
        "arch": arch,
        "supported": is_supported,
        "summary": f"Windows {release} build {build} {arch}",
    }


def detect_ram_gb() -> float:
    """Total physical RAM in GB via GlobalMemoryStatusEx."""
    if not hasattr(ctypes, "windll"):
        return 0.0

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

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return 0.0
    return stat.ullTotalPhys / (1024 ** 3)


def detect_disk_free_gb(path: Path) -> float:
    try:
        usage = shutil.disk_usage(str(path))
        return usage.free / (1024 ** 3)
    except OSError:
        return 0.0


def detect_gpu() -> dict:
    """Returns {has_gpu, name, vram_gb, error}. Treats AMD/Intel as no GPU."""
    if pynvml is None:
        return {"has_gpu": False, "name": None, "vram_gb": 0.0, "error": "pynvml not available"}
    try:
        pynvml.nvmlInit()
    except Exception as exc:
        return {"has_gpu": False, "name": None, "vram_gb": 0.0, "error": f"nvmlInit: {exc}"}
    try:
        count = pynvml.nvmlDeviceGetCount()
        if count == 0:
            return {"has_gpu": False, "name": None, "vram_gb": 0.0, "error": "no NVIDIA devices"}
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name_raw = pynvml.nvmlDeviceGetName(handle)
        name = name_raw.decode("utf-8") if isinstance(name_raw, bytes) else str(name_raw)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        vram_gb = mem.total / (1024 ** 3)
        return {"has_gpu": True, "name": name, "vram_gb": vram_gb, "error": None}
    except Exception as exc:
        return {"has_gpu": False, "name": None, "vram_gb": 0.0, "error": f"query: {exc}"}
    finally:
        try:
            pynvml.nvmlShutdown()
        except Exception:
            pass


# Standard registry locations for Visual C++ 2015-2022 Redist x64.
# Microsoft documents these under VisualStudio\14.0\VC\Runtimes\X64.
_VC_REGISTRY_PATHS = [
    r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64",
    r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X64",
]


def detect_vc_redist() -> dict:
    if winreg is None:
        return {"installed": False, "version": None, "error": "winreg unavailable"}
    for sub in _VC_REGISTRY_PATHS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as key:
                installed, _ = winreg.QueryValueEx(key, "Installed")
                version, _ = winreg.QueryValueEx(key, "Version")
                if int(installed) == 1:
                    return {"installed": True, "version": str(version), "error": None}
        except FileNotFoundError:
            continue
        except OSError as exc:
            debug(f"VC redist probe at {sub} failed: {exc}")
            continue
    return {"installed": False, "version": None, "error": "registry keys not present"}


def phase1_preflight() -> dict:
    section("Phase 1 - Pre-flight checks")
    facts: dict = {}

    osinfo = detect_os()
    facts["os"] = osinfo
    info(f"OS: {osinfo['summary']}")
    if not osinfo["supported"]:
        bail("WhisperVoice requires 64-bit Windows 10 or newer")

    ram_gb = detect_ram_gb()
    disk_root = Path(DEFAULT_INSTALL_PATH).anchor or "C:\\"
    free_gb = detect_disk_free_gb(Path(disk_root))
    facts["ram_gb"] = ram_gb
    facts["disk_free_gb"] = free_gb
    info(f"RAM: {ram_gb:.1f} GB, free disk on {disk_root.rstrip(chr(92))}: {free_gb:.0f} GB")
    if ram_gb and ram_gb < 4:
        warn(f"only {ram_gb:.1f} GB RAM detected - performance may be poor")
    if free_gb and free_gb < 4:
        warn(f"only {free_gb:.0f} GB free on {disk_root} - install may not fit")

    gpu = detect_gpu()
    facts["gpu"] = gpu
    if gpu["has_gpu"]:
        info(f"GPU: {gpu['name']}, {gpu['vram_gb']:.1f} GB VRAM")
    else:
        reason = gpu["error"] or "no NVIDIA GPU"
        info(f"GPU: none ({reason}) - CPU mode will be used")

    vc = detect_vc_redist()
    facts["vc_redist"] = vc
    if vc["installed"]:
        info(f"VC++ Redist 2015-2022: found ({vc['version']})")
    else:
        warn("VC++ Redist 2015-2022 not detected")
        print("       Download: https://aka.ms/vs/17/release/vc_redist.x64.exe")
        if not ask_yes_no("VC++ Redist may already be present via another product. Continue anyway?", default=True):
            bail("install aborted - install VC++ Redist first")

    info("Checking pypi.org connectivity...")
    pip_ok = http_reachable(PIP_CHECK_URL, timeout=NETWORK_TIMEOUT)
    facts["pip_reachable"] = pip_ok
    if pip_ok:
        info("pypi.org: reachable")
    else:
        warn("pypi.org: unreachable")
        if not ask_yes_no("Dependency install will fail without pypi.org. Continue anyway?", default=False):
            bail("install aborted - check internet access")

    info("Checking huggingface.co connectivity...")
    hf_ok = http_reachable(HF_CHECK_URL, timeout=NETWORK_TIMEOUT)
    facts["hf_reachable"] = hf_ok
    if hf_ok:
        info("huggingface.co: reachable")
    else:
        warn("huggingface.co: unreachable - model download will fail")
        if not ask_yes_no("Continue anyway?", default=False):
            bail("install aborted - check internet access")

    return facts


# ---------------------------------------------------------------------------
# Phase 2 - user input
# ---------------------------------------------------------------------------

def recommend_model(facts: dict) -> str:
    gpu = facts.get("gpu", {})
    if not gpu.get("has_gpu"):
        return "small"
    vram = gpu.get("vram_gb", 0.0)
    if vram >= 6.0:
        return "large-v3"
    if vram >= 4.0:
        return "medium"
    return "small"


def prompt_install_path() -> Path:
    while True:
        raw = ask("Install path", DEFAULT_INSTALL_PATH)
        try:
            path = Path(raw).expanduser()
        except Exception as exc:
            print(f"  invalid path: {exc}")
            continue
        if path.exists():
            try:
                non_empty = any(path.iterdir())
            except OSError:
                non_empty = True
            if non_empty:
                print(f"  Folder {path} already exists and is not empty.")
                choice = ask("[O]verwrite, [N]ew path, [C]ancel", "C").lower()
                if choice.startswith("o"):
                    return path
                if choice.startswith("n"):
                    continue
                bail("install cancelled by user")
        return path


def prompt_model(recommended: str) -> str:
    print()
    print("Choose Whisper model:")
    sizes = {"small": "244 MB", "medium": "769 MB", "large-v3": "3 GB"}
    for idx, name in enumerate(MODEL_CHOICES, start=1):
        marker = "  (recommended)" if name == recommended else ""
        print(f"  [{idx}] {name:<10} {sizes[name]}{marker}")
    default_idx = MODEL_CHOICES.index(recommended) + 1
    while True:
        raw = ask("Enter 1, 2, or 3", str(default_idx))
        if raw in ("1", "2", "3"):
            return MODEL_CHOICES[int(raw) - 1]
        if raw in MODEL_CHOICES:
            return raw
        print("  invalid choice")


def derive_runtime(model: str, gpu_present: bool) -> dict:
    device = "cuda" if gpu_present else "cpu"
    compute_type = "float16" if gpu_present else "int8"
    beam_size = 5 if gpu_present else 1
    disk = MODEL_DOWNLOAD_GB[model] + RUNTIME_OVERHEAD_GB[device]
    disk_rounded = round(disk * 2) / 2
    return {
        "model": model,
        "device": device,
        "compute_type": compute_type,
        "beam_size": beam_size,
        "disk_gb": disk_rounded,
    }


def phase2_user_input(facts: dict) -> dict:
    section("Phase 2 - Choose install options")
    install_path = prompt_install_path()
    recommended = recommend_model(facts)
    model = prompt_model(recommended)
    runtime = derive_runtime(model, facts["gpu"].get("has_gpu", False))
    runtime["install_path"] = install_path

    print()
    print(
        f"Will install {runtime['model']} on {runtime['device']} "
        f"at {install_path}. Disk needed: ~{runtime['disk_gb']:.1f} GB."
    )
    if not ask_yes_no("Continue?", default=True):
        bail("install cancelled by user")

    info(f"User chose: {runtime['model']} on {runtime['device']} at {install_path}")
    return runtime


# ---------------------------------------------------------------------------
# Phase 3 - install steps
# ---------------------------------------------------------------------------

def step_create_install_dir(install_path: Path) -> None:
    install_path.mkdir(parents=True, exist_ok=True)
    (install_path / "assets").mkdir(parents=True, exist_ok=True)


def step_download_project_files(install_path: Path) -> None:
    for rel in PROJECT_FILES:
        url = f"{SERVER_URL}/{rel}"
        dest = install_path / rel
        size = download(url, dest)
        info(f"  GET {url} - {size} bytes - OK")


def step_download_assets(install_path: Path) -> None:
    """Non-critical: ico download. If 404, continue without icon."""
    for rel in ASSET_FILES:
        url = f"{SERVER_URL}/{rel}"
        dest = install_path / rel
        try:
            size = download(url, dest)
            info(f"  GET {url} - {size} bytes - OK")
        except Exception as exc:
            log_exception(f"asset download {rel}", exc)
            warn(f"asset {rel} unavailable - continuing without it")


def _find_uv_executable() -> str:
    """Locate uv on PATH or in known per-user install locations."""
    found = shutil.which("uv") or shutil.which("uv.exe")
    if found:
        return found
    candidates = [
        Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "uv.exe",
        Path(os.environ.get("APPDATA", "")) / "uv" / "bin" / "uv.exe",
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    raise FileNotFoundError("uv executable not found - bootstrap should have installed it")


def step_create_venv(install_path: Path) -> None:
    uv = _find_uv_executable()
    venv_path = install_path / ".venv"
    cmd = [uv, "venv", "--python", "3.12", str(venv_path)]
    debug(f"running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _write_cpu_requirements(source: Path, dest: Path) -> int:
    """Copy `source` to `dest` with all nvidia-* package lines stripped.

    Returns the number of lines removed. Comments and blank lines pass through.
    """
    lines = source.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if not ln.lstrip().lower().startswith("nvidia-")]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return len(lines) - len(kept)


def step_install_dependencies(install_path: Path, device: str) -> None:
    uv = _find_uv_executable()
    venv_python = install_path / ".venv" / "Scripts" / "python.exe"
    requirements = install_path / "requirements.txt"
    if device == "cpu":
        # Strip nvidia-* lines before handing to uv. Installing those wheels
        # on a CPU-only machine wastes ~2 GB AND ships DLLs that crash
        # faster_whisper at import (exit 0xC0000005) when no NVIDIA driver
        # is present. Original repo requirements.txt is left untouched -
        # cuda installs still need it as-is.
        cpu_requirements = Path(tempfile.gettempdir()) / "wv-bootstrap" / "requirements_cpu.txt"
        removed = _write_cpu_requirements(requirements, cpu_requirements)
        info(f"  filtered nvidia-* lines for cpu install ({removed} removed) -> {cpu_requirements}")
        requirements = cpu_requirements
    cmd = [
        uv, "pip", "install",
        "-r", str(requirements),
        "--python", str(venv_python),
    ]
    debug(f"running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def step_copy_cuda_dlls(install_path: Path, device: str) -> None:
    """Mirror .venv\\Lib\\site-packages\\nvidia\\*\\bin\\*.dll into .venv\\Scripts\\.

    ctranslate2 4.7.1 stopped declaring nvidia-cublas-cu12 / nvidia-cudnn-cu12
    as deps, and its DLL search path is python.exe's directory + system PATH
    only - the per-package nvidia\\<lib>\\bin folders are on neither, so
    transcription crashes with 'cublas64_12.dll is not found' otherwise.

    No-op when device == "cpu". Even if nvidia-* wheels somehow ended up in
    the venv on a cpu install, copying their DLLs next to python.exe would
    crash faster_whisper at import (0xC0000005) on a driver-less machine.
    """
    venv = install_path / ".venv"
    scripts = venv / "Scripts"
    if device != "cpu" and device != "cuda":
        warn(f"unexpected device '{device}' - skipping CUDA DLL copy")
        return
    if device == "cpu":
        # Defensive sweep: log any stray CUDA DLLs that slipped through filter (b).
        stray: list[Path] = []
        if scripts.is_dir():
            for pattern in ("cublas*.dll", "cudnn*.dll", "cuda*.dll", "nvrtc*.dll"):
                stray.extend(sorted(scripts.glob(pattern)))
        if stray:
            warn(
                "cpu install but found CUDA DLLs in .venv\\Scripts: "
                + ", ".join(p.name for p in stray)
                + " - app may crash on launch"
            )
        else:
            info("  skipped: device='cpu', no CUDA DLLs needed (.venv\\Scripts verified clean)")
        return
    nvidia_root = venv / "Lib" / "site-packages" / "nvidia"
    if not nvidia_root.is_dir():
        info("  nvidia\\ tree not present (cpu-only deps); nothing to copy")
        return
    scripts.mkdir(parents=True, exist_ok=True)
    copied = 0
    total_bytes = 0
    for sub in sorted(nvidia_root.iterdir()):
        bin_dir = sub / "bin"
        if not bin_dir.is_dir():
            continue
        for dll in sorted(bin_dir.glob("*.dll")):
            shutil.copy2(dll, scripts / dll.name)
            copied += 1
            total_bytes += dll.stat().st_size
    if copied == 0:
        info("  no DLLs found under nvidia\\<lib>\\bin\\ - nothing copied")
        return
    info(f"  copied {copied} CUDA runtime DLL(s), {total_bytes / (1024 ** 2):.1f} MB total")


def _find_uv_python_root() -> Path | None:
    """Locate the portable Python tree uv unpacked.

    Prefers the dotted version folder (cpython-3.12.X-windows-x86_64-none)
    over the junction (cpython-3.12-windows-x86_64-none). File stats through
    the junction are unreliable on Windows (Defender / reparse-point quirks),
    which can break shutil.copytree mid-copy. Falls back to whatever is there
    if no dotted folder exists.
    """
    candidate_roots = [
        Path(os.environ.get("APPDATA", "")) / "uv" / "python",
        Path(os.environ.get("USERPROFILE", "")) / ".local" / "share" / "uv" / "python",
        Path(os.environ.get("LOCALAPPDATA", "")) / "uv" / "python",
    ]
    fallback: Path | None = None
    for root in candidate_roots:
        if not root.exists():
            continue
        for entry in root.iterdir():
            if not (entry.is_dir() and entry.name.startswith("cpython-3.12")):
                continue
            # Real folder name has "3.12." (dotted version, e.g. 3.12.13);
            # junction name has "3.12-" (no dot after major.minor).
            if "3.12." in entry.name:
                return entry
            if fallback is None:
                fallback = entry
    return fallback


def step_copy_runtime(install_path: Path) -> None:
    runtime_dir = install_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    src = _find_uv_python_root()
    if src is not None:
        info(f"  copying portable Python from {src}")
        for entry in src.iterdir():
            target = runtime_dir / entry.name
            if entry.is_dir():
                shutil.copytree(entry, target, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, target)
        return
    # Fallback: copy interpreter from the venv. main.py needs python.exe to launch.
    warn("uv portable Python tree not found - falling back to venv interpreter")
    venv_scripts = install_path / ".venv" / "Scripts"
    for name in ("python.exe", "pythonw.exe", "python312.dll"):
        candidate = venv_scripts / name
        if candidate.exists():
            shutil.copy2(candidate, runtime_dir / name)


def step_download_model(install_path: Path, model: str, device: str) -> None:
    venv_python = install_path / ".venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        raise FileNotFoundError(f"venv python missing at {venv_python}")
    # Always download with cpu/int8 - that path doesn't need CUDA DLLs and
    # produces the same on-disk model files regardless of inference device.
    snippet = (
        "import os; "
        "os.environ['HF_HUB_DISABLE_XET'] = '1'; "
        "os.environ['HF_HUB_DISABLE_SYMLINKS_WARNING'] = '1'; "
        "from faster_whisper import WhisperModel; "
        f"WhisperModel({model!r}, device='cpu', compute_type='int8'); "
        "print('model download ok')"
    )
    cmd = [str(venv_python), "-X", "utf8", "-c", snippet]
    debug(f"running: {' '.join(cmd)}")
    env = dict(os.environ)
    env["HF_HUB_DISABLE_XET"] = "1"
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    subprocess.run(cmd, check=True, env=env)


def step_write_config(install_path: Path, runtime: dict) -> None:
    """Generate config.yaml with the v1.3 glossary as initial_prompt."""
    cfg = (
        "# WhisperVoice configuration\n"
        "model:\n"
        f"  size: {runtime['model']}\n"
        f"  device: {runtime['device']}\n"
        f"  compute_type: {runtime['compute_type']}\n"
        "  language: ru\n"
        f"  initial_prompt: '{GLOSSARY}'\n"
        f"  beam_size: {runtime['beam_size']}\n"
        "\n"
        "hotkey: alt_gr\n"
        "\n"
        "audio:\n"
        "  sample_rate: 16000\n"
        "  channels: 1\n"
        "  device: null\n"
        "  block_size: 1024\n"
        "\n"
        "post_processing:\n"
        "  apply_dictionary: true\n"
        "  normalize_audio: true\n"
        "  target_peak: 0.7\n"
        "  add_trailing_space: true\n"
        "  auto_paste: true\n"
        "  paste_delay_ms: 80\n"
        "\n"
        "ui:\n"
        "  sound_feedback: true\n"
    )
    (install_path / "config.yaml").write_text(cfg, encoding="utf-8")
    (install_path / "dictionary.json").write_text('{"rules": []}\n', encoding="utf-8")


def step_write_start_bat(install_path: Path) -> None:
    """Generate start.bat byte-identical to v1.1's `(echo ...) > start.bat` output.

    cmd's `echo` writes each line as <text>\\r\\n with no leading/trailing
    whitespace fixups. The v1.1 block expands %INSTALL_DIR% on the first line
    and emits %BASE% literally on the rest.
    """
    base = str(install_path)
    lines = [
        "@echo off",
        f"set BASE={base}",
        "set PYTHONPATH=%BASE%\\.venv\\Lib\\site-packages",
        "set PYTHONHOME=",
        "set VIRTUAL_ENV=%BASE%\\.venv",
        "set HF_HUB_DISABLE_XET=1",
        "set HF_HUB_DISABLE_SYMLINKS_WARNING=1",
        "%BASE%\\runtime\\python.exe -X utf8 \"%BASE%\\main.py\"",
    ]
    content = "\r\n".join(lines) + "\r\n"
    (install_path / "start.bat").write_bytes(content.encode("utf-8"))


def step_create_shortcut(install_path: Path) -> None:
    desktop = _user_desktop_path()
    if desktop is None:
        warn("desktop path not found - skipping shortcut")
        return
    lnk_path = desktop / "WhisperVoice.lnk"
    target = install_path / "start.bat"
    icon_path = install_path / "assets" / "whispervoice.ico"
    icon_arg = str(icon_path) if icon_path.exists() else "imageres.dll,101"
    create_shortcut(
        lnk_path=str(lnk_path),
        target=str(target),
        working_dir=str(install_path),
        icon=icon_arg,
        description="WhisperVoice - Local Voice Dictation",
    )
    info(f"  shortcut created at {lnk_path}")


def step_finalize_log(install_path: Path) -> None:
    final_log = install_path / "install.log"
    relocate_log(final_log)
    info(f"log finalized at {final_log}")


def phase3_install(facts: dict, runtime: dict) -> None:
    section("Phase 3 - Install")
    install_path: Path = runtime["install_path"]
    model = runtime["model"]
    device = runtime["device"]

    info(f"target: {install_path}")
    print()

    run_step("create install dir", lambda: step_create_install_dir(install_path), critical=True,
             console_label="create install dir")
    run_step("download project files", lambda: step_download_project_files(install_path), critical=True,
             console_label="download project files")
    run_step("download assets", lambda: step_download_assets(install_path), critical=False,
             console_label="download assets")
    run_step("create .venv", lambda: step_create_venv(install_path), critical=True,
             console_label="create .venv (Python 3.12)")
    run_step("install dependencies", lambda: step_install_dependencies(install_path, device), critical=True,
             console_label="install dependencies")
    run_step("copy CUDA runtime DLLs", lambda: step_copy_cuda_dlls(install_path, device),
             critical=(device == "cuda"),
             console_label="copy CUDA runtime DLLs")
    run_step("copy portable runtime", lambda: step_copy_runtime(install_path), critical=True,
             console_label="copy portable Python runtime")
    run_step("download model", lambda: step_download_model(install_path, model, device), critical=True,
             console_label=f"download Whisper model '{model}'")
    run_step("write config.yaml", lambda: step_write_config(install_path, runtime), critical=True,
             console_label="write config.yaml")
    run_step("write start.bat", lambda: step_write_start_bat(install_path), critical=True,
             console_label="write start.bat")
    run_step("create desktop shortcut", lambda: step_create_shortcut(install_path), critical=False,
             console_label="create desktop shortcut")
    run_step("finalize log", lambda: step_finalize_log(install_path), critical=False,
             console_label="finalize install.log")


# ---------------------------------------------------------------------------
# Phase 4 - final
# ---------------------------------------------------------------------------

def phase4_final(runtime: dict) -> None:
    section("Phase 4 - Done")
    install_path = runtime["install_path"]
    info(f"install path: {install_path}")
    info(f"model:        {runtime['model']}")
    info(f"device:       {runtime['device']}")
    info(f"installer:    v{INSTALLER_VERSION}")
    print()
    print("To start WhisperVoice: double-click 'WhisperVoice' on your Desktop")
    print("                       or run start.bat from the install folder.")
    print()
    print(f"Hotkey: Right Alt (AltGr) - press to start/stop recording.")
    print(f"Log:    {install_path / 'install.log'}")
    print()


# ---------------------------------------------------------------------------
# ctypes IShellLinkW shortcut creation (no PowerShell, no pywin32)
# ---------------------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", c_ulong),
        ("Data2", c_ushort),
        ("Data3", c_ushort),
        ("Data4", c_ubyte * 8),
    ]


_CLSID_SHELLLINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELLLINKW = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSISTFILE = "{0000010B-0000-0000-C000-000000000046}"

_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2

# IShellLinkW vtable indices (after IUnknown's QI/AddRef/Release at 0/1/2)
_VT_RELEASE = 2
_VT_QI = 0
_VT_SET_DESCRIPTION = 7
_VT_SET_WORKING_DIRECTORY = 9
_VT_SET_ICON_LOCATION = 17
_VT_SET_PATH = 20

# IPersistFile vtable indices
_VT_PF_RELEASE = 2
_VT_PF_SAVE = 6


def _guid_from_string(s: str) -> _GUID:
    g = _GUID()
    if not hasattr(ctypes, "windll"):
        return g
    hr = ctypes.windll.ole32.CLSIDFromString(c_wchar_p(s), byref(g))
    if hr != 0:
        raise OSError(f"CLSIDFromString({s}) -> 0x{hr & 0xFFFFFFFF:08X}")
    return g


def _vmethod(interface_ptr: c_void_p, index: int, *argtypes):
    """Resolve a virtual method pointer at `index` on a COM interface."""
    vtbl_pp = ctypes.cast(interface_ptr, POINTER(POINTER(c_void_p)))
    vtbl = vtbl_pp.contents
    fn_addr = vtbl[index]
    proto = ctypes.WINFUNCTYPE(c_int, c_void_p, *argtypes)
    return proto(fn_addr)


def create_shortcut(
    lnk_path: str,
    target: str,
    working_dir: str | None = None,
    icon: str | None = None,
    description: str | None = None,
) -> None:
    """Create a .lnk shortcut via IShellLinkW + IPersistFile (no PowerShell)."""
    if not hasattr(ctypes, "windll"):
        raise RuntimeError("Windows COM not available on this platform")
    ole32 = ctypes.windll.ole32

    hr = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    # S_OK==0, S_FALSE==1 (already initialized) - both fine.
    if hr & 0xFFFFFFFF not in (0, 1):
        raise OSError(f"CoInitializeEx -> 0x{hr & 0xFFFFFFFF:08X}")

    psl = c_void_p()
    ppf = c_void_p()
    try:
        clsid = _guid_from_string(_CLSID_SHELLLINK)
        iid_sl = _guid_from_string(_IID_ISHELLLINKW)
        iid_pf = _guid_from_string(_IID_IPERSISTFILE)

        hr = ole32.CoCreateInstance(byref(clsid), None, _CLSCTX_INPROC_SERVER, byref(iid_sl), byref(psl))
        if hr & 0xFFFFFFFF != 0:
            raise OSError(f"CoCreateInstance(ShellLink) -> 0x{hr & 0xFFFFFFFF:08X}")

        set_path = _vmethod(psl, _VT_SET_PATH, c_wchar_p)
        hr = set_path(psl, target)
        if hr & 0xFFFFFFFF != 0:
            raise OSError(f"SetPath -> 0x{hr & 0xFFFFFFFF:08X}")

        if working_dir:
            set_wd = _vmethod(psl, _VT_SET_WORKING_DIRECTORY, c_wchar_p)
            set_wd(psl, working_dir)
        if description:
            set_desc = _vmethod(psl, _VT_SET_DESCRIPTION, c_wchar_p)
            set_desc(psl, description)
        if icon:
            set_icon = _vmethod(psl, _VT_SET_ICON_LOCATION, c_wchar_p, c_int)
            set_icon(psl, icon, 0)

        qi = _vmethod(psl, _VT_QI, POINTER(_GUID), POINTER(c_void_p))
        hr = qi(psl, byref(iid_pf), byref(ppf))
        if hr & 0xFFFFFFFF != 0:
            raise OSError(f"QueryInterface(IPersistFile) -> 0x{hr & 0xFFFFFFFF:08X}")

        save = _vmethod(ppf, _VT_PF_SAVE, c_wchar_p, c_int)
        hr = save(ppf, lnk_path, 1)  # fRemember = TRUE
        if hr & 0xFFFFFFFF != 0:
            raise OSError(f"IPersistFile::Save -> 0x{hr & 0xFFFFFFFF:08X}")
    finally:
        if ppf:
            try:
                _vmethod(ppf, _VT_PF_RELEASE)(ppf)
            except Exception:
                pass
        if psl:
            try:
                _vmethod(psl, _VT_RELEASE)(psl)
            except Exception:
                pass
        ole32.CoUninitialize()


def _user_desktop_path() -> Path | None:
    """Locate the current user's Desktop via SHGetFolderPathW (CSIDL_DESKTOPDIRECTORY=0x10)."""
    if not hasattr(ctypes, "windll"):
        return None
    CSIDL_DESKTOPDIRECTORY = 0x10
    SHGFP_TYPE_CURRENT = 0
    buf = ctypes.create_unicode_buffer(260)
    hr = ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_DESKTOPDIRECTORY, None, SHGFP_TYPE_CURRENT, buf)
    if hr & 0xFFFFFFFF != 0:
        # Fallback to USERPROFILE\Desktop
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            return Path(userprofile) / "Desktop"
        return None
    return Path(buf.value)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    runtime: dict = {}
    try:
        TEMP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        setup_logging(TEMP_LOG_PATH)
        info(f"=== WhisperVoice Installer v{INSTALLER_VERSION} ===")
        info(f"started at {datetime.datetime.now().isoformat(timespec='seconds')}")
        info(f"python: {sys.version.split()[0]} ({sys.executable})")
        info(f"log:    {TEMP_LOG_PATH}")
        # Diagnostic: which python is *really* running. Useful for spotting
        # uv junction-vs-real-folder mismatches (Defender / reparse-point quirk).
        debug(f"sys.executable realpath: {os.path.realpath(sys.executable)}")
        debug(f"os.__file__: {os.__file__}")

        facts = phase1_preflight()
        runtime = phase2_user_input(facts)
        phase3_install(facts, runtime)
        phase4_final(runtime)
        return 0
    except KeyboardInterrupt:
        print()
        warn("install cancelled by user (Ctrl+C)")
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        log_exception("unhandled exception in main", exc)
        error(f"unhandled error: {exc}")
        print(f"\nFull log: {_current_log_path()}")
        return 1
    finally:
        # step_finalize_log already relocated the log to <install>/install.log via
        # relocate_log(). Re-copying _current_log_path() onto itself here zero-fills
        # the file on Windows (open-source-handle starts at 0, dest truncates first).
        # If finalize failed, the temp log path was printed for the user.
        pause_exit()


if __name__ == "__main__":
    sys.exit(main())
