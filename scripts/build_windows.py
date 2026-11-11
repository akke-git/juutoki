"""Build and test the Windows folder distribution on the Windows runner."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.platform != "win32":
        raise SystemExit("Run this Windows build on the GitHub Windows runner.")
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "packaging/juutoki.spec"], cwd=ROOT, check=True)
    executable = ROOT / "dist" / "juutoki" / "juutoki.exe"
    # Do not allow developer paths to hide missing bundled resources.
    env = dict(os.environ)
    env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    env.pop("NEWT_DATA_DIR", None)
    unrelated = ROOT / "build" / "smoke-cwd"
    unrelated.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(executable), "--self-test"], cwd=unrelated, env=env, check=True, timeout=180)
    shutil.copyfile(ROOT / "docs" / "windows-quick-start.txt", executable.parent / "START-HERE.txt")
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    shutil.make_archive(str(release / "juutoki-windows-x64"), "zip", ROOT / "dist", "juutoki")
    print("Windows build and packaged Chromium self-test completed.")


if __name__ == "__main__":
    main()
