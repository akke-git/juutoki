"""Read-only application resources and persistent per-user data locations."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


def is_bundled():
    return bool(getattr(sys, "frozen", False))


def resource_root():
    return Path(__file__).resolve().parent


def data_dir():
    override = os.environ.get("NEWT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if not is_bundled():
        return resource_root() / "data"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "juutoki"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "juutoki"


def browser_dir():
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override)
    return resource_root() / ("browsers" if is_bundled() else ".browsers")


def install_seed(seed, destination):
    """Restore once using SQLite backup; never replace a user's existing DB."""
    seed, destination = Path(seed), Path(destination)
    if destination.exists() or not seed.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(seed.resolve().as_uri() + "?mode=ro", uri=True)
    temporary = None
    try:
        if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise sqlite3.DatabaseError("배포용 초기 DB 검사에 실패했습니다.")
        fd, name = tempfile.mkstemp(prefix=".juutoki-seed-", suffix=".sqlite3", dir=destination.parent)
        os.close(fd)
        temporary = Path(name)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("복원된 초기 DB 검사에 실패했습니다.")
        finally:
            target.close()
        try:
            if sys.platform == "win32":
                # Windows rename is atomic and refuses an existing target.
                os.rename(temporary, destination)
            else:
                # POSIX rename would overwrite. A hard link publishes the
                # finished sibling DB atomically without replacing anything.
                os.link(temporary, destination)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
        source.close()
