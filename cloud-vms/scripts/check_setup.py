"""Check every project-level prerequisite and install only what is missing.

Run it with the Python inside the project's virtual environment (the launchers
RUN_VMS.bat / run_vms.sh do this for you):

    .venv\\Scripts\\python scripts\\check_setup.py        (Windows)
    .venv/bin/python scripts/check_setup.py              (Linux / macOS)

What it checks, in order - each item is installed ONLY when it is missing:
  1. Python packages from backend/requirements.txt (name AND version range)
  2. PyTorch / OpenCV actually load (catches missing Windows runtime DLLs)
  3. Default detector weights  data/weights/yolo26n.pt
  4. Optional IISc UVH-26 vehicle model (tried once; never blocks start-up)
     + OSNet re-identification weights for unique counting (never blocks start-up)
  5. Demo video               data/uploads/demo_gate.mp4
  6. Settings file            .env  (copied from .env.example)
  7. Web interface            frontend/dist/index.html (built only if missing; needs Node.js)

Options:
  --check-only   report what is missing, install nothing (exit code 1 if something required is missing)
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQS = ROOT / "backend" / "requirements.txt"
WEIGHTS = ROOT / "data" / "weights"
YOLO = WEIGHTS / "yolo26n.pt"
UVH = WEIGHTS / "UVH-26-MV-YOLOv11-S.pt"
UVH_SKIP = WEIGHTS / ".uvh26_skipped"
OSNET = WEIGHTS / "osnet_x0_25_msmt17.onnx"
DEMO = ROOT / "data" / "uploads" / "demo_gate.mp4"
ENV, ENV_EXAMPLE = ROOT / ".env", ROOT / ".env.example"
DIST = ROOT / "frontend" / "dist" / "index.html"

# consistent, readable output even when the console code page is not UTF-8
try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def ok(msg: str) -> None:
    print(f"  [ OK ]      {msg}", flush=True)


def todo(msg: str) -> None:
    print(f"  [INSTALL]   {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  [WARNING]   {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  [ERROR]     {msg}", flush=True)


def run(cmd: list[str], cwd: Path = ROOT) -> int:
    print("              > " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=str(cwd))


# ----------------------------------------------------------------------------- packages
def _packaging():
    """`packaging` is not always installed, but pip always ships a copy."""
    try:
        from packaging.markers import default_environment
        from packaging.requirements import Requirement
    except ImportError:  # pragma: no cover - depends on the environment
        from pip._vendor.packaging.markers import default_environment  # type: ignore
        from pip._vendor.packaging.requirements import Requirement  # type: ignore
    return Requirement, default_environment


def read_requirements() -> list[str]:
    lines = []
    for raw in REQS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            lines.append(line)
    return lines


def _unsatisfied(spec: str, seen: set[str]) -> list[str]:
    """Return the requirement strings (spec itself and, for extras, their deps) that are not satisfied."""
    from importlib import metadata
    Requirement, default_environment = _packaging()
    req = Requirement(spec)
    key = re.sub(r"[-_.]+", "-", req.name).lower() + str(sorted(req.extras))
    if key in seen:
        return []
    seen.add(key)
    if req.marker is not None and not req.marker.evaluate():
        return []
    try:
        dist = metadata.distribution(req.name)
    except metadata.PackageNotFoundError:
        return [spec]
    if req.specifier and not req.specifier.contains(dist.version, prereleases=True):
        return [spec]
    missing: list[str] = []
    for extra in req.extras:  # e.g. uvicorn[standard] -> uvloop/httptools/watchfiles ...
        for dep in dist.requires or []:
            r = Requirement(dep)
            if r.marker is None:
                continue
            env = dict(default_environment())
            env["extra"] = extra
            if r.marker.evaluate(env):
                r.marker = None
                if _unsatisfied(str(r), seen):
                    missing.append(spec)
                    break
    return missing


def check_packages(check_only: bool) -> bool:
    missing: list[str] = []
    seen: set[str] = set()
    for spec in read_requirements():
        missing += _unsatisfied(spec, seen)
    if not missing:
        ok(f"Python packages ({len(read_requirements())} requirements satisfied)")
        return True
    todo("Python packages missing or too old: " + ", ".join(missing))
    if check_only:
        return False
    if any(m.lower().startswith("ultralytics") for m in missing):
        print("              (the first install downloads about 1 GB - PyTorch, OpenCV ... - so it takes a while)")
    run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"])
    if run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing]) != 0:
        fail("pip could not install the packages above. Check the internet connection and run the launcher again.")
        return False
    still = [s for s in read_requirements() if _unsatisfied(s, set())]
    if still:
        fail("still missing after install: " + ", ".join(still))
        return False
    ok("Python packages installed")
    return True


def check_imports() -> bool:
    """Import the heavy native libraries in a child process so a DLL failure is reported cleanly."""
    code = "import torch, cv2, numpy; print(torch.__version__, cv2.__version__)"
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    if p.returncode == 0:
        ok(f"PyTorch / OpenCV load correctly ({p.stdout.strip()})")
        return True
    err = (p.stderr or p.stdout).strip().splitlines()
    fail("PyTorch or OpenCV failed to load: " + (err[-1] if err else "unknown error"))
    if os.name == "nt":
        print("              This almost always means the Microsoft Visual C++ Redistributable is missing.\n"
              "              Install it from https://aka.ms/vs/17/release/vc_redist.x64.exe and run the launcher again.")
    return False


# ----------------------------------------------------------------------------- data files
def check_weights(check_only: bool) -> bool:
    if YOLO.is_file() and YOLO.stat().st_size > 1_000_000:
        ok(f"Detector weights ({YOLO.relative_to(ROOT)})")
        return True
    todo("Detector weights yolo26n.pt (about 5 MB)")
    if check_only:
        return False
    YOLO.unlink(missing_ok=True)  # remove a half-downloaded file
    if run([sys.executable, str(ROOT / "ml" / "datasets" / "download_models.py"), "--yolo26", "n"]) != 0 \
            or not YOLO.is_file():
        fail("could not download yolo26n.pt (needs access to github.com). Run the launcher again when online.")
        return False
    ok("Detector weights downloaded")
    return True


def check_uvh26(check_only: bool) -> None:
    """Optional model - attempted once, never blocks the launch."""
    if UVH.is_file():
        ok(f"Optional UVH-26 vehicle model ({UVH.relative_to(ROOT)})")
        return
    if UVH_SKIP.exists():
        ok("Optional UVH-26 vehicle model: skipped earlier (delete data/weights/.uvh26_skipped to retry)")
        return
    todo("Optional UVH-26 Indian-vehicle model (Hugging Face)")
    if check_only:
        return
    cmd = [sys.executable, str(ROOT / "ml" / "datasets" / "download_models.py"), "--uvh26"]
    print("              > " + " ".join(cmd), flush=True)
    try:  # output captured: a failure here is expected when Hugging Face is unreachable
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
        code, tail = p.returncode, ((p.stderr or p.stdout).strip().splitlines() or [""])[-1]
    except subprocess.TimeoutExpired:
        code, tail = 1, "timed out"
    if code == 0 and UVH.is_file():
        ok("UVH-26 model downloaded")
    else:
        WEIGHTS.mkdir(parents=True, exist_ok=True)
        UVH_SKIP.write_text("UVH-26 download failed; the app uses the COCO model instead.\n"
                            "Retry: python ml/datasets/download_models.py --uvh26\n")
        warn(f"UVH-26 not downloaded (optional; {tail[:120]}) - the app uses the COCO model instead.")


def check_demo(check_only: bool) -> bool:
    if DEMO.is_file() and DEMO.stat().st_size > 10_000:
        ok(f"Demo video ({DEMO.relative_to(ROOT)})")
        return True
    todo("Demo video demo_gate.mp4")
    if check_only:
        return False
    if run([sys.executable, str(ROOT / "scripts" / "make_demo_video.py")]) != 0 or not DEMO.is_file():
        fail("could not create the demo video")
        return False
    ok("Demo video created")
    return True


def check_env(check_only: bool) -> bool:
    if ENV.is_file():
        ok("Settings file (.env)")
        return True
    todo("Settings file .env (copied from .env.example)")
    if not check_only:
        shutil.copyfile(ENV_EXAMPLE, ENV)
        ok(".env created")
    return True


def check_frontend(check_only: bool) -> bool:
    if DIST.is_file():
        ok("Web interface (frontend/dist, pre-built)")
        return True
    todo("Web interface build (frontend/dist is missing)")
    if check_only:
        return False
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        fail("Node.js (npm) is needed to build the web interface. Install Node.js 18+ and run the launcher again.")
        return False
    fe = ROOT / "frontend"
    if not (fe / "node_modules").is_dir():
        if run([npm, "ci" if (fe / "package-lock.json").is_file() else "install", "--no-audit", "--no-fund"], fe) != 0:
            fail("npm install failed")
            return False
    if run([npm, "run", "build"], fe) != 0 or not DIST.is_file():
        fail("npm run build failed")
        return False
    ok("Web interface built")
    return True


def check_osnet(check_only: bool) -> None:
    """Re-identification weights (unique counting). Optional: without them the app falls back to
    colour histograms, and the server retries the download when a camera starts."""
    if OSNET.is_file() and OSNET.stat().st_size > 100_000:
        ok(f"Re-ID model for unique counting ({OSNET.relative_to(ROOT)})")
        return
    todo("Re-ID model OSNet x0.25 for unique people / vehicle counts (about 1 MB, Hugging Face)")
    if check_only:
        return
    try:
        import shutil
        from huggingface_hub import hf_hub_download
        src = hf_hub_download("anriha/osnet_x0_25_msmt17", OSNET.name)
        WEIGHTS.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, OSNET)
        ok("Re-ID model downloaded")
    except Exception as exc:
        warn(f"Re-ID model not downloaded ({str(exc)[:120]}); retried when a camera starts.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args()
    v = sys.version_info
    if not ((3, 10) <= v[:2] <= (3, 12)):
        warn(f"Python {v.major}.{v.minor} is outside the tested range 3.10 - 3.12")
    if sys.prefix == sys.base_prefix:
        warn("not running inside a virtual environment - packages go into the system Python")

    results = []
    pkgs = check_packages(a.check_only)
    results.append(pkgs)
    if pkgs:  # the rest needs the packages
        results.append(check_imports())
        results.append(check_weights(a.check_only))
        check_uvh26(a.check_only)
        check_osnet(a.check_only)
        results.append(check_demo(a.check_only))
    results.append(check_env(a.check_only))
    results.append(check_frontend(a.check_only))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
