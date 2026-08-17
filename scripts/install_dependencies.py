"""ONE fail-closed dependency bootstrap, for a fresh machine and for CI.

WHAT THIS REPLACES, AND WHY THE GATE WAS LYING.

The CI install step was three lines in a `run:` block:

    python -m pip install --upgrade pip
    pip install -r requirements.txt
    pip install pytest

On `windows-latest` that block runs under pwsh, where a failing NATIVE
command does not stop the script — only the LAST command's exit code
decides whether the step passed. So on Windows:

    pip install -r requirements.txt   FAILS   (order_book needs the MSVC
                                               patch; see vendor/patches)
    pip install pytest                SUCCEEDS
    -> step reports SUCCESS

and pytest then started against a half-installed environment. The release
gate was reporting on a machine that had never finished being built.

The remedy is not to drop cryptofeed from the Windows matrix. Windows IS
the deployment platform, and a gate that skips the platform it ships on is
not a gate. This script performs the documented patch procedure the same
way locally and in CI, and every step is checked.

    python scripts/install_dependencies.py
    python scripts/install_dependencies.py --with-test
    python scripts/install_dependencies.py --check-only

Any failure exits non-zero immediately. There is no partial success.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATCH = REPO / "vendor" / "patches" / "order_book-1.0.1-msvc.patch"

# The exact source the patch was written against. A patch applied to a
# different version is not a fix; it is an unreviewed edit.
ORDER_BOOK_SPEC = "order-book==1.0.1"
ORDER_BOOK_DIR = "order_book-1.0.1"

# Imported after installation to prove the environment is actually usable.
# An install that "succeeded" while leaving an unimportable extension is
# the failure this whole script exists to catch.
SMOKE_IMPORTS = ("order_book", "cryptofeed")


class StepFailed(RuntimeError):
    pass


def run(cmd: list[str], *, cwd: Path | None = None, what: str) -> None:
    """Run a command and RAISE on failure. Never warn-and-continue."""
    print(f"\n>>> {what}\n    $ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
    if proc.returncode != 0:
        raise StepFailed(f"{what} failed with exit code {proc.returncode}")


def pip(*args: str, what: str) -> None:
    run([sys.executable, "-m", "pip", *args], what=what)


def is_windows() -> bool:
    return platform.system() == "Windows"


def order_book_importable() -> bool:
    return subprocess.run(
        [sys.executable, "-c", "import order_book"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def build_patched_order_book(workdir: Path) -> None:
    """Fetch the exact source, verify it, patch it, build it.

    Upstream's architecture gates recognise only GCC/Clang macros and end
    in `#error`, and orderbook.c calls GCC's `__builtin_expect`, so MSVC
    cannot compile it unmodified. See vendor/patches/README.md.
    """
    if not PATCH.exists():
        raise StepFailed(f"patch not found: {PATCH}")

    print(f"    patch sha256: {hashlib.sha256(PATCH.read_bytes()).hexdigest()[:16]}")

    pip("download", ORDER_BOOK_SPEC, "--no-binary", ":all:", "--no-deps",
        "-d", str(workdir), what=f"fetch {ORDER_BOOK_SPEC} source")

    tarballs = list(workdir.glob("order_book-*.tar.gz"))
    if len(tarballs) != 1:
        raise StepFailed(
            f"expected exactly one order_book sdist, found {tarballs}")
    with tarfile.open(tarballs[0]) as tf:
        tf.extractall(workdir)

    src = workdir / ORDER_BOOK_DIR
    if not src.is_dir():
        # The patch targets specific files at specific offsets. A different
        # layout means a different version, and applying it anyway would be
        # an unreviewed source edit rather than the documented fix.
        raise StepFailed(
            f"expected {ORDER_BOOK_DIR}/ in the sdist — the patch was written "
            f"against that exact version and must not be applied to another")

    # `git apply` is used because the patch is a git diff and git is present
    # on every GitHub runner and on the operator's machine.
    run(["git", "apply", "--verbose", str(PATCH)], cwd=src,
        what="apply MSVC patch to order_book")
    pip("install", ".", what="build and install patched order_book")


def ensure_order_book() -> None:
    if order_book_importable():
        print("\n>>> order_book already importable — skipping build")
        return
    if not is_windows():
        # Linux/macOS build it from upstream without help.
        pip("install", ORDER_BOOK_SPEC, what=f"install {ORDER_BOOK_SPEC}")
        return
    with tempfile.TemporaryDirectory(prefix="jarvis-orderbook-") as tmp:
        build_patched_order_book(Path(tmp))


def smoke() -> None:
    """Prove the environment is usable, not merely that pip exited zero."""
    for mod in SMOKE_IMPORTS:
        run([sys.executable, "-c", f"import {mod}; print('{mod}', 'ok')"],
            what=f"smoke import {mod}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--with-test", action="store_true",
                    help="also install the test runner")
    ap.add_argument("--with-inference", action="store_true",
                    help="CPU predictive inference (OpenVINO). Part of the supported runtime.")
    ap.add_argument("--with-research", action="store_true",
                    help="offline model research (scipy, scikit-learn, CPU torch). No GPU.")
    ap.add_argument("--with-cuda-training", action="store_true",
                    help="OPTIONAL GPU training. Downloads ~2 GB of CUDA wheels. Not needed to run JARVIS.")
    # DEPRECATED. "ML" is the wrong axis: JARVIS already does machine
    # learning — calibration, expectancy, feature attribution, OpenVINO
    # inference — with no CUDA anywhere. This flag used to mean "install the
    # CUDA training stack", so a name that sounds like a normal capability
    # pulled 2 GB of GPU wheels onto machines that had no GPU.
    ap.add_argument("--with-ml", action="store_true",
                    help="DEPRECATED alias for --with-inference --with-research (CPU only). "
                         "For GPU training use --with-cuda-training.")
    ap.add_argument("--check-only", action="store_true",
                    help="verify the current environment without installing")
    args = ap.parse_args()

    print(f"JARVIS dependency bootstrap — {platform.system()} "
          f"{platform.machine()}, Python {platform.python_version()}")

    try:
        if args.check_only:
            smoke()
            pip("check", what="pip dependency consistency check")
            print("\nEnvironment OK.")
            return 0

        pip("install", "--upgrade", "pip", what="upgrade pip")

        # order_book FIRST. requirements.txt pulls cryptofeed, which
        # depends on it, so building it up front is what turns the Windows
        # failure into a success rather than something to be tolerated.
        ensure_order_book()

        pip("install", "-r", str(REPO / "requirements.txt"),
            what="install project requirements")

        # THREE SEPARATE THINGS, PREVIOUSLY ONE FLAG.
        #
        #   inference  CPU predictive runtime (OpenVINO). Supported runtime.
        #   research   offline harness only (scipy, scikit-learn, CPU torch).
        #   cuda       optional GPU training. Nothing needs it to run.
        #
        # Established by tracing imports, not by reading file names: nothing
        # under app/, jobs/ or lib/ imports scipy, scikit-learn or torch.
        # The learning pipeline is stdlib and SQLAlchemy; the predictive
        # runtime is numpy and OpenVINO. JARVIS learns from its trades with
        # neither research nor CUDA installed.
        if args.with_ml:
            print("\n  NOTE: --with-ml is deprecated and now means "
                  "--with-inference --with-research (CPU only).\n"
                  "        It used to install the CUDA training stack. If that "
                  "is genuinely what you want,\n"
                  "        pass --with-cuda-training explicitly.\n")

        # Without OpenVINO, 11 predictive-runtime tests SKIP rather than run,
        # and they cover abstention semantics — schema mismatch, stale
        # features, a device that raises. Coverage that silently disappears
        # is not coverage.
        if args.with_inference or args.with_ml:
            pip("install", "-r", str(REPO / "requirements-inference.txt"),
                what="install CPU inference extras (OpenVINO)")

        if args.with_research or args.with_ml:
            pip("install", "-r", str(REPO / "requirements-research.txt"),
                what="install offline research extras (CPU)")

        if args.with_cuda_training:
            # --extra-index-url, NOT --index-url: the CUDA index is partial
            # and carries no numpy, scipy or scikit-learn, so replacing PyPI
            # with it makes everything else unresolvable.
            pip("install", "-r", str(REPO / "requirements-cuda.txt"),
                "--extra-index-url", "https://download.pytorch.org/whl/cu128",
                what="install OPTIONAL GPU training extras (CUDA wheels)")

        if args.with_test:
            pip("install", "pytest", what="install test runner")

        # A consistent resolution is part of "installed", and pip check is
        # the only step that notices a conflict pip install did not fail on.
        pip("check", what="pip dependency consistency check")
        smoke()

        print("\nAll dependencies installed and verified.")
        return 0
    except StepFailed as e:
        print(f"\nBOOTSTRAP FAILED: {e}", file=sys.stderr)
        print("Nothing further will run — a half-installed environment must "
              "not be tested against.", file=sys.stderr)
        return 1
    except Exception as e:                                    # pragma: no cover
        print(f"\nBOOTSTRAP FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
