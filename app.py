import asyncio
import json
import os
import sqlite3
import uuid
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import runtime_paths

BASE = runtime_paths.resource_root()
if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime_paths.browser_dir())

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import scraper

DATA = runtime_paths.data_dir()
DB = DATA / "newt.sqlite3"
snapshots = {}
tasks = {}
stop_events = {}
collection_lock = asyncio.Lock()
preview_lock = asyncio.Lock()
discovery_lock = asyncio.Lock()
SESSION_RESTART_DELAY = 60
DEFAULT_REQUEST_DELAY = 30


@contextmanager
def connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "exports").mkdir(exist_ok=True)
    seeded = runtime_paths.is_bundled() and runtime_paths.install_seed(BASE / "seed" / "newt.sqlite3", DB)
    restored_jobs = []
    with connect() as db:
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS books (
          id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS chapters (
          id INTEGER PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id),
          number INTEGER NOT NULL, title TEXT NOT NULL, url TEXT UNIQUE NOT NULL,
          body TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        for row in db.execute("SELECT id,payload FROM jobs").fetchall():
            job = json.loads(row["payload"])
            if job["status"] in ("queued", "running", "stopping", "waiting"):
                job.update(status="interrupted", message="서버가 재시작되어 중단됐습니다. 미완료 회차를 재시도하세요.")
                job.pop("resume_at", None)
                for item in job["items"]:
                    if item["status"] in ("pending", "running", "waiting"):
                        item["status"] = "interrupted"
                db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job["id"]))
            if seeded:
                restored_jobs.append(job)
    # Historical job downloads must also work after restoring just the DB.
    for job in restored_jobs:
        export_job(job)
        save_job(job)


def save_job(job):
    job["updated_at"] = now()
    with connect() as db:
        db.execute("INSERT INTO jobs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                   (job["id"], json.dumps(job, ensure_ascii=False)))


def get_job(job_id):
    with connect() as db:
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "작업을 찾지 못했습니다.")
    return json.loads(row["payload"])


def find_chapter(url):
    with connect() as db:
        row = db.execute("SELECT * FROM chapters WHERE url=?", (url,)).fetchone()
    return dict(row) if row else None


def save_chapter(book, chapter, body):
    with connect() as db:
        db.execute("INSERT INTO books VALUES (?,?,?,?) ON CONFLICT(url) DO UPDATE SET title=excluded.title,updated_at=excluded.updated_at",
                   (book["id"], book["url"], book["title"], now()))
        db.execute("""INSERT INTO chapters(book_id,number,title,url,body,updated_at) VALUES (?,?,?,?,?,?)
          ON CONFLICT(url) DO UPDATE SET book_id=excluded.book_id,number=excluded.number,
          title=excluded.title,body=excluded.body,updated_at=excluded.updated_at""",
                   (book["id"], chapter["number"], chapter["title"], chapter["url"], body, now()))


def export_job(job):
    sections = [job["title"], f"원본 목록: {job['url']}"]
    count = 0
    for item in sorted(job["items"], key=lambda x: x["number"]):
        if item["status"] not in ("saved", "skipped"):
            continue
        chapter = find_chapter(item["url"])
        if chapter:
            sections.append(f"{'=' * 40}\n{item['number']}화 · {item['title']}\n{'=' * 40}\n\n{chapter['body']}")
            count += 1
    if count:
        (DATA / "exports" / f"{job['id']}.txt").write_text("\n\n".join(sections) + "\n", encoding="utf-8-sig", newline="\n")
    job["export_count"] = count


async def wait_for_session_restart(job, item, event, reason):
    job.update(status="waiting", message=f"{item['number']}화 수집 오류. 대기 후 새 세션에서 한 번 재시도합니다.",
               resume_at=(datetime.now(timezone.utc) + timedelta(seconds=SESSION_RESTART_DELAY)).isoformat())
    item.update(status="waiting", error=reason)
    job.setdefault("recovery_log", []).append({"number": item["number"], "at": now(),
                                              "wait_seconds": SESSION_RESTART_DELAY, "reason": reason})
    export_job(job)
    save_job(job)
    try:
        await asyncio.wait_for(event.wait(), timeout=SESSION_RESTART_DELAY)
        return False
    except TimeoutError:
        return not event.is_set()
    finally:
        job.pop("resume_at", None)


async def run_job(job):
    event = stop_events[job["id"]]
    async with collection_lock:
        if event.is_set():
            job.update(status="stopped", message="수집을 중지했습니다.")
            save_job(job)
            return
        job.update(status="running", message="브라우저를 준비하고 있습니다.")
        save_job(job)
        halt_reason = None
        try:
            async with AsyncExitStack() as sessions:
                page = await sessions.enter_async_context(scraper.reader())
                for item in job["items"]:
                    if event.is_set():
                        break
                    cached = find_chapter(item["url"]) if job["skip_existing"] else None
                    if cached:
                        # Retain current list metadata even for cached content.
                        save_chapter(job["book"], item, cached["body"])
                        item.update(status="skipped", chars=len(cached["body"]), error="")
                        save_job(job)
                        continue
                    while not event.is_set():
                        job.update(status="running", message=f"{item['number']}화 본문을 불러오고 있습니다.")
                        item.update(status="running", error="")
                        save_job(job)
                        try:
                            body = await scraper.extract(page, item, job["selectors"]["content"])
                        except Exception as exc:
                            reason = str(exc)[:700]
                            if item.get("restart_attempts", 0) == 0:
                                item["restart_attempts"] = 1
                                # Close the rejected browser before waiting; hold the
                                # job lock and resume this exact item in a new reader.
                                await sessions.aclose()
                                if not await wait_for_session_restart(job, item, event, reason):
                                    break
                                job.update(status="running", message=f"새 세션을 준비합니다. {item['number']}화부터 재개합니다.")
                                save_job(job)
                                page = await sessions.enter_async_context(scraper.reader())
                                job["recovery_count"] = job.get("recovery_count", 0) + 1
                                continue
                            item.update(status="failed", error=reason)
                            halt_reason = "새 세션에서도 같은 회차가 실패했습니다: " + reason[:500]
                            break
                        save_chapter(job["book"], item, body)
                        item.update(status="saved", chars=len(body))
                        break
                    save_job(job)
                    if halt_reason or event.is_set():
                        break
                    try:
                        await asyncio.wait_for(event.wait(), timeout=job["delay"])
                    except TimeoutError:
                        pass
            failures = sum(i["status"] == "failed" for i in job["items"])
            job.update(status="failed" if halt_reason else ("stopped" if event.is_set() else "completed"),
                       message=("추가 실패를 막기 위해 수집을 중지했습니다: " + halt_reason) if halt_reason else
                       ("수집을 중지했습니다." if event.is_set() else f"수집이 끝났습니다. 실패 {failures}회차"))
        except Exception as exc:
            job.update(status="failed", message="수집을 진행하지 못했습니다: " + str(exc)[:700])
        finally:
            for item in job["items"]:
                if item["status"] in ("running", "pending", "waiting"):
                    item["status"] = "interrupted"
            job.pop("resume_at", None)
            try:
                export_job(job)
            except Exception as exc:
                job["message"] += " / TXT 생성 실패: " + str(exc)[:200]
            save_job(job)
            tasks.pop(job["id"], None)
            stop_events.pop(job["id"], None)


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield
    for event in stop_events.values():
        event.set()
    if tasks:
        await asyncio.gather(*list(tasks.values()), return_exceptions=True)


app = FastAPI(title="juutoki · 텍스트 수집 테스트", lifespan=lifespan)


@app.middleware("http")
async def local_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        return JSONResponse({"detail": "다른 웹사이트에서의 요청은 허용하지 않습니다."}, status_code=403)
    if request.method == "POST" and "application/json" not in request.headers.get("content-type", ""):
        return JSONResponse({"detail": "JSON 요청만 지원합니다."}, status_code=415)
    return await call_next(request)


class DiscoverRequest(BaseModel):
    url: str = Field(max_length=2048)
    selectors: dict[str, str] = Field(default_factory=dict)


class StartRequest(BaseModel):
    snapshot_id: str
    start: int | None = Field(default=None, ge=1)
    end: int | None = Field(default=None, ge=1)
    skip_existing: bool = True
    delay: float = Field(default=DEFAULT_REQUEST_DELAY, ge=1, le=30)


class RetryRequest(BaseModel):
    delay: float | None = Field(default=None, ge=1, le=30)


def snapshot_for(snapshot_id):
    snapshot = snapshots.get(snapshot_id)
    if not snapshot:
        raise HTTPException(404, "목록을 다시 불러와 주세요.")
    return snapshot


@app.post("/api/discover")
async def discover(req: DiscoverRequest):
    if discovery_lock.locked():
        raise HTTPException(409, "목록을 불러오는 중입니다. 잠시 후 다시 시도하세요.")
    try:
        async with discovery_lock:
            result = await scraper.discover(req.url, req.selectors)
    except Exception as exc:
        raise HTTPException(400, str(exc)[:700]) from exc
    sid = uuid.uuid4().hex
    if len(snapshots) >= 20:
        snapshots.pop(next(iter(snapshots)))
    snapshots[sid] = result
    return {**result, "snapshot_id": sid}


@app.get("/api/preview/{snapshot_id}/{number}")
async def preview(snapshot_id: str, number: int):
    snapshot = snapshot_for(snapshot_id)
    chapter = next((c for c in snapshot["chapters"] if c["number"] == number), None)
    if not chapter:
        raise HTTPException(404, "해당 회차가 목록에 없습니다.")
    if preview_lock.locked() or collection_lock.locked():
        raise HTTPException(409, "다른 본문을 불러오는 중입니다. 잠시 후 다시 시도하세요.")
    try:
        async with preview_lock:
            async with scraper.reader() as page:
                body = await scraper.extract(page, chapter, snapshot["selectors"]["content"])
        return {**chapter, "body": body, "chars": len(body)}
    except Exception as exc:
        raise HTTPException(400, str(exc)[:700]) from exc


def launch(job):
    if tasks or preview_lock.locked():
        raise HTTPException(409, "진행 중인 작업이 있습니다. 완료 후 시작하세요.")
    save_job(job)
    stop_events[job["id"]] = asyncio.Event()
    tasks[job["id"]] = asyncio.create_task(run_job(job))
    return job


@app.post("/api/jobs")
async def start(req: StartRequest):
    snapshot = snapshot_for(req.snapshot_id)
    if req.start is not None and req.end is not None and req.start > req.end:
        raise HTTPException(400, "시작 회차는 종료 회차보다 클 수 없습니다.")
    selected = [c for c in snapshot["chapters"] if (req.start is None or c["number"] >= req.start) and (req.end is None or c["number"] <= req.end)]
    if not selected:
        raise HTTPException(400, "선택한 범위에 회차가 없습니다.")
    with connect() as db:
        existing = db.execute("SELECT id FROM books WHERE url=?", (snapshot["url"],)).fetchone()
    book = {"id": existing["id"] if existing else uuid.uuid4().hex, "url": snapshot["url"], "title": snapshot["title"]}
    return launch({"id": uuid.uuid4().hex, "title": snapshot["title"], "url": snapshot["url"],
                   "book": book, "selectors": snapshot["selectors"], "status": "queued", "message": "수집을 준비합니다.",
                   "created_at": now(), "skip_existing": req.skip_existing, "delay": req.delay, "export_count": 0,
                   "recovery_count": 0, "recovery_log": [],
                   "items": [{**c, "status": "pending", "chars": 0, "error": ""} for c in selected]})


@app.get("/api/jobs")
async def list_jobs():
    with connect() as db:
        rows = db.execute("SELECT payload FROM jobs ORDER BY rowid DESC LIMIT 20").fetchall()
    return [json.loads(row["payload"]) for row in rows]


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    return get_job(job_id)


@app.get("/api/jobs/{job_id}/source")
async def job_source(job_id: str):
    job = get_job(job_id)
    for sid, snapshot in snapshots.items():
        if snapshot["url"] == job["url"] and snapshot["selectors"] == job["selectors"]:
            return {**snapshot, "snapshot_id": sid}
    return await discover(DiscoverRequest(url=job["url"], selectors=job["selectors"]))


@app.post("/api/jobs/{job_id}/stop")
async def stop(job_id: str):
    get_job(job_id)
    if job_id in stop_events:
        stop_events[job_id].set()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
async def retry(job_id: str, req: RetryRequest | None = None):
    job = get_job(job_id)
    job["delay"] = req.delay if req is not None and req.delay is not None else DEFAULT_REQUEST_DELAY
    selected = [i for i in job["items"] if i["status"] not in ("saved", "skipped")]
    if not selected:
        raise HTTPException(400, "재시도할 회차가 없습니다.")
    job.pop("resume_at", None)
    job.update(id=uuid.uuid4().hex, status="queued", created_at=now(), message="미완료 회차를 재시도합니다.", export_count=0,
               recovery_count=0, recovery_log=[],
               items=[{**i, "status": "pending", "error": "", "chars": 0, "restart_attempts": 0} for i in selected])
    return launch(job)


@app.get("/api/jobs/{job_id}/download")
async def download(job_id: str):
    job = get_job(job_id)
    path = DATA / "exports" / f"{job_id}.txt"
    if not path.exists():
        raise HTTPException(404, "아직 TXT 파일이 없습니다. 작업이 끝난 뒤 다운로드하세요.")
    return FileResponse(path, media_type="text/plain; charset=utf-8", filename=f"{job['title']}.txt")


@app.get("/api/books")
async def books():
    with connect() as db:
        return [dict(row) for row in db.execute("""SELECT b.*,count(c.id) count,coalesce(sum(length(c.body)),0) chars
          FROM books b LEFT JOIN chapters c ON c.book_id=b.id GROUP BY b.id ORDER BY b.updated_at DESC""")]


@app.get("/api/books/{book_id}/chapters")
async def book_chapters(book_id: str):
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT id,number,title,url,length(body) chars FROM chapters WHERE book_id=? ORDER BY number", (book_id,))]


@app.get("/api/chapters/{chapter_id}")
async def chapter_detail(chapter_id: int):
    with connect() as db:
        row = db.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
    if not row:
        raise HTTPException(404, "회차를 찾지 못했습니다.")
    return dict(row)


@app.get("/api/books/{book_id}/download")
async def book_download(book_id: str):
    with connect() as db:
        book = db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        rows = db.execute("SELECT * FROM chapters WHERE book_id=? ORDER BY number", (book_id,)).fetchall()
    if not book or not rows:
        raise HTTPException(404, "저장된 본문이 없습니다.")
    body = book["title"] + "\n\n" + "\n\n".join(f"{r['number']}화 · {r['title']}\n\n{r['body']}" for r in rows)
    from fastapi.responses import Response
    return Response(content=body.encode("utf-8-sig"), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(book["title"] + ".txt", safe="")})


@app.get("/api/settings")
async def settings():
    return {"selectors": scraper.DEFAULTS}


@app.get("/api/health")
async def health():
    return {"application": "juutoki", "status": "ok", "run_id": os.environ.get("NEWT_DESKTOP_RUN_ID")}


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="web")
