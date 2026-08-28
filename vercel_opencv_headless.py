"""Vercel has no libGL. MediaPipe pulls GUI OpenCV; swap it for the headless wheel."""

from __future__ import annotations

import shutil
import subprocess
import sys

GUI_PACKAGES = ("opencv-python", "opencv-contrib-python")
HEADLESS = "opencv-contrib-python-headless==4.11.0.86"


def _run(cmd: list[str], check: bool = True) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=check)


def main() -> None:
    uv = shutil.which("uv")
    if uv:
        _run([uv, "pip", "uninstall", *GUI_PACKAGES], check=False)
        # Uninstalling the GUI wheel also removes `cv2` if both packages shared
        # the namespace, so always rewrite the headless install afterwards.
        _run(
            [
                uv,
                "pip",
                "install",
                "--reinstall",
                "--no-cache-dir",
                HEADLESS,
                "numpy<2",
            ]
        )
        return
    _run([sys.executable, "-m", "pip", "uninstall", "-y", *GUI_PACKAGES], check=False)
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            "--no-cache-dir",
            HEADLESS,
            "numpy<2",
        ]
    )


if __name__ == "__main__":
    main()
