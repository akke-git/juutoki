# Run from project root: python -m PyInstaller packaging/juutoki.spec
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
browsers = Path(os.environ.get("JUUTOKI_BROWSER_BUNDLE", str(root / ".build-browsers"))).resolve()
seed = root / "seed" / "newt.sqlite3"
if not browsers.is_dir() or not any(browsers.glob("chromium*")):
    raise RuntimeError("Install Chromium into .build-browsers before packaging")
if not seed.is_file():
    raise RuntimeError("Create seed/newt.sqlite3 before packaging")

pw_data, pw_binaries, pw_imports = collect_all("playwright")
a = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root)],
    binaries=pw_binaries,
    datas=pw_data + [(str(root / "static"), "static"), (str(root / "seed"), "seed"), (str(browsers), "browsers")],
    hiddenimports=pw_imports + ["app", "scraper", "runtime_paths", "uvicorn.logging", "uvicorn.loops.asyncio",
                               "uvicorn.protocols.http.h11_impl", "uvicorn.lifespan.on"],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="juutoki", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="juutoki")
