"""Export an explicit source allowlist, including only the approved seed DB."""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["app.py", "scraper.py", "runtime_paths.py", "launcher.py", "requirements.txt",
         "requirements-build.txt", "README.md", ".gitignore"]
FOLDERS = ["static", "tests", "scripts", "packaging", "seed", ".github"]
DOCS = ["docs/windows-build.md", "docs/windows-quick-start.txt"]


def create_archive(root=ROOT, destination=None):
    root = Path(root)
    destination = Path(destination) if destination else root / "release" / "juutoki-source.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    selected = [root / name for name in FILES + DOCS]
    for folder in FOLDERS:
        selected.extend((root / folder).rglob("*"))
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(set(selected)):
            if not file.is_file() or file.is_symlink() or "__pycache__" in file.parts or file.suffix == ".pyc":
                continue
            if not file.resolve().is_relative_to(root.resolve()):
                raise ValueError("Source path escaped project root")
            archive.write(file, file.relative_to(root))
    return destination


if __name__ == "__main__":
    print(create_archive())
