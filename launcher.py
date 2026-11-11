"""Portable entry point: start local server, open browser, retain user data."""
import argparse
import asyncio
import json
import multiprocessing
import os
import socket
import sys
import tempfile
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

import runtime_paths


class InstanceLock:
    def __init__(self, folder):
        self.path = Path(folder) / "desktop.lock"
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.file = handle
        return True

    def release(self):
        if self.file is None:
            return
        handle, self.file = self.file, None
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def reserve_socket(preferred_port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            sock.bind(("127.0.0.1", preferred_port))
        except OSError:
            if preferred_port == 0:
                raise
            sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise


def valid_local_url(value):
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                and parsed.username is None and parsed.password is None and parsed.port is not None
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
    except (ValueError, TypeError):
        return False


def open_existing(info_path, open_browser):
    for _ in range(40):
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            url = info.get("url", "")
            if valid_local_url(url):
                with urlopen(url + "api/health", timeout=0.3) as response:
                    health = json.load(response)
                if health.get("application") == "juutoki" and health.get("run_id") == info.get("run_id"):
                    print("juutoki is already running: " + url, flush=True)
                    if open_browser:
                        webbrowser.open(url)
                    return 0
        except (OSError, ValueError, URLError):
            pass
        time.sleep(0.15)
    print("juutoki is starting or stopping. Please try again shortly.", file=sys.stderr)
    return 1


def run_async(coroutine):
    if sys.platform == "win32":
        # Playwright's Node driver needs subprocess support. Do not use the
        # SelectorEventLoop Uvicorn may select for a single Windows worker.
        return asyncio.run(coroutine, loop_factory=asyncio.ProactorEventLoop)
    return asyncio.run(coroutine)


async def serve(sock, url, open_browser):
    import app
    import uvicorn

    config = uvicorn.Config(app.app, host="127.0.0.1", port=sock.getsockname()[1],
                            access_log=False, log_level="info")
    server = uvicorn.Server(config)

    async def open_when_ready():
        while not server.started and not server.should_exit:
            await asyncio.sleep(0.05)
        if server.started:
            print("juutoki: " + url, flush=True)
            print("Data: " + str(app.DATA), flush=True)
            print("Keep this window open. Press Ctrl+C here to stop the server.", flush=True)
            if open_browser:
                await asyncio.to_thread(webbrowser.open, url)

    opener = asyncio.create_task(open_when_ready())
    try:
        await server.serve(sockets=[sock])
    finally:
        opener.cancel()
        await asyncio.gather(opener, return_exceptions=True)


async def self_test():
    """Offline packaged-runtime check: server/UI/DB plus real bundled Chromium."""
    import app
    import httpx
    import scraper

    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="http://localhost") as client:
            response = await client.get("/")
            response.raise_for_status()
            assert "juutoki" in response.text and "novel -&gt; txt" in response.text
            books = (await client.get("/api/books")).json()
            chapters = sum(book["count"] for book in books)
            manifest = runtime_paths.resource_root() / "seed" / "manifest.json"
            if runtime_paths.is_bundled():
                expected = json.loads(manifest.read_text(encoding="utf-8"))
                assert len(books) == expected["books"] and chapters == expected["chapters"], "Seed DB was not restored completely"
            if books:
                txt = await client.get(f"/api/books/{books[0]['id']}/download")
                txt.raise_for_status()
                assert txt.content.startswith(b"\xef\xbb\xbf")
        async with scraper.reader() as page:
            await page.set_content("<main><p>juutoki bundled Chromium works</p></main>")
            assert await page.locator("p").inner_text() == "juutoki bundled Chromium works"
    print(json.dumps({"ok": True, "bundled": runtime_paths.is_bundled(), "books": len(books),
                      "chapters": chapters, "chromium": "ok"}), flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="juutoki local desktop server")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the default browser")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--self-test", action="store_true", help="Check packaged runtime without accessing websites")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.self_test:
        with tempfile.TemporaryDirectory(prefix="juutoki-selftest-") as folder:
            os.environ["NEWT_DATA_DIR"] = folder
            return run_async(self_test())

    folder = runtime_paths.data_dir()
    lock = InstanceLock(folder)
    info_path = folder / "desktop.json"
    if not lock.acquire():
        return open_existing(info_path, not args.no_browser)
    sock = None
    try:
        sock = reserve_socket(args.port)
        url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
        run_id = uuid.uuid4().hex
        os.environ["NEWT_DESKTOP_RUN_ID"] = run_id
        info_path.write_text(json.dumps({"url": url, "run_id": run_id, "pid": os.getpid()}), encoding="utf-8")
        run_async(serve(sock, url, not args.no_browser))
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if sock:
            sock.close()
        info_path.unlink(missing_ok=True)
        lock.release()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
