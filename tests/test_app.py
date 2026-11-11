import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import app as service
import scraper

FIXTURE = """<h1>사이트 이름</h1><div class="page-title"><h2>테스트 작품</h2></div>
<div class="serial-list"><ul>
<li class="list-item"><span class="wr-num">2</span><a class="item-subject" href="/novel/10/abc">소제목(1)</a></li>
<li class="list-item"><span class="wr-num">1</span><a class="item-subject" href="/novel/10/xyz">시작(7)</a></li>
</ul></div><div class="pg_wrap"><a href="?epage=2">2</a><a href="https://evil.example/novel/10">외부</a></div>"""


class ParseTests(unittest.TestCase):
    def test_list_uses_real_numbers_and_same_origin_pages(self):
        title, chapters, pages = scraper.parse_list(FIXTURE, "https://example.com/novel/10", scraper.DEFAULTS)
        self.assertEqual(title, "테스트 작품")
        self.assertEqual([c["number"] for c in chapters], [2, 1])
        self.assertEqual(chapters[0]["title"], "소제목(1)")
        self.assertEqual(pages, ["https://example.com/novel/10?epage=2"])

    def test_bad_schemes_and_credentials_rejected(self):
        for url in ("file:///etc/passwd", "ftp://example.com", "https://user:pass@example.com", "http://example.com:8001"):
            with self.assertRaises(ValueError):
                scraper.normalize_url(url)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_patch = patch.multiple(service, DATA=Path(self.temp.name), DB=Path(self.temp.name) / "test.db")
        self.data_patch.start()
        self.delay_patch = patch.object(service, "SESSION_RESTART_DELAY", 0.01)
        self.delay_patch.start()
        service.snapshots.clear()
        service.tasks.clear()
        service.stop_events.clear()
        service.collection_lock = asyncio.Lock()
        service.preview_lock = asyncio.Lock()
        service.discovery_lock = asyncio.Lock()
        service.initialize()
        title, chapters, _ = scraper.parse_list(FIXTURE, "https://example.com/novel/10", scraper.DEFAULTS)
        service.snapshots["test"] = {"title": title, "url": "https://example.com/novel/10", "selectors": scraper.DEFAULTS,
                                      "chapters": sorted(chapters, key=lambda c: c["number"])}
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=service.app), base_url="http://testserver")

    async def asyncTearDown(self):
        for task in list(service.tasks.values()):
            task.cancel()
        await asyncio.gather(*list(service.tasks.values()), return_exceptions=True)
        await self.client.aclose()
        self.data_patch.stop()
        self.delay_patch.stop()
        self.temp.cleanup()

    async def finish(self):
        await asyncio.gather(*list(service.tasks.values()))

    async def test_job_source_restores_entire_cached_list_not_only_finished_range(self):
        snapshot = service.snapshots["test"]
        service.save_job({"id": "finished-range", "status": "completed", "url": snapshot["url"],
                          "selectors": snapshot["selectors"], "items": snapshot["chapters"][:1]})
        with patch.object(scraper, "discover", side_effect=AssertionError("Cached list must be reused")):
            response = await self.client.get("/api/jobs/finished-range/source")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["snapshot_id"], "test")
        self.assertEqual([c["number"] for c in response.json()["chapters"]], [1, 2])

    async def test_job_source_recovers_expired_list_with_original_site_selectors(self):
        snapshot = service.snapshots.pop("test")
        selectors = {**snapshot["selectors"], "content": ".different-site-content"}
        service.save_job({"id": "expired-list", "url": snapshot["url"], "selectors": selectors})
        async def discover(url, actual_selectors):
            self.assertEqual(url, snapshot["url"])
            self.assertEqual(actual_selectors, selectors)
            return {**snapshot, "selectors": selectors}
        with patch.object(scraper, "discover", discover):
            response = await self.client.get("/api/jobs/expired-list/source")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["selectors"], selectors)
        self.assertIn(response.json()["snapshot_id"], service.snapshots)

    async def test_retry_without_interval_uses_current_default_instead_of_old_job_interval(self):
        service.save_job({"id": "old-delay", "delay": 2, "items": [
            {"number": 1, "status": "saved"},
            {"number": 2, "status": "failed"},
        ]})
        with patch.object(service, "launch", side_effect=lambda job: job):
            for options in ({"content": b"", "headers": {"Content-Type": "application/json"}}, {"json": {}}, {"json": {"delay": 20}}):
                response = await self.client.post("/api/jobs/old-delay/retry", **options)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["delay"], options.get("json", {}).get("delay", 30))
                self.assertEqual([i["number"] for i in response.json()["items"]], [2])

    async def test_save_failure_retry_dedup_and_download(self):
        @asynccontextmanager
        async def reader():
            yield None

        async def partial(page, chapter, selector):
            if chapter["number"] == 2:
                raise ValueError("본문을 표시하지 못했습니다")
            return "첫 문단\n\n둘째 문단"

        async def success(page, chapter, selector):
            return "재시도한 둘째 회차 본문"

        payload = {"snapshot_id": "test", "start": 1, "end": 2, "delay": 1}
        with patch.object(scraper, "reader", reader), patch.object(scraper, "extract", partial):
            res = await self.client.post("/api/jobs", json=payload)
            self.assertEqual(res.status_code, 200, res.text)
            job_id = res.json()["id"]
            await self.finish()
        job = (await self.client.get(f"/api/jobs/{job_id}")).json()
        self.assertEqual([i["status"] for i in job["items"]], ["saved", "failed"])
        self.assertEqual(job["export_count"], 1)
        download = await self.client.get(f"/api/jobs/{job_id}/download")
        self.assertTrue(download.content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("첫 문단\n\n둘째 문단", download.text)
        self.assertNotIn("본문을 표시하지 못했습니다", download.text)
        with patch.object(scraper, "reader", reader), patch.object(scraper, "extract", success):
            retry = await self.client.post(f"/api/jobs/{job_id}/retry", json={"delay": 1})
            self.assertEqual(retry.status_code, 200)
            self.assertEqual(len(retry.json()["items"]), 1)
            await self.finish()
            again = await self.client.post("/api/jobs", json=payload)
            await self.finish()
        repeat = (await self.client.get(f"/api/jobs/{again.json()['id']}")).json()
        self.assertEqual([i["status"] for i in repeat["items"]], ["skipped", "skipped"])
        books = (await self.client.get("/api/books")).json()
        self.assertEqual(books[0]["count"], 2)
        txt = await self.client.get(f"/api/books/{books[0]['id']}/download")
        self.assertLess(txt.text.index("1화"), txt.text.index("2화"))
        self.assertIn("재시도한 둘째 회차 본문", txt.text)
        chapters = (await self.client.get(f"/api/books/{books[0]['id']}/chapters")).json()
        detail = (await self.client.get(f"/api/chapters/{chapters[0]['id']}")).json()
        self.assertEqual(detail["body"], "첫 문단\n\n둘째 문단")

    async def test_preview_does_not_save_and_ranges_validate(self):
        @asynccontextmanager
        async def reader():
            yield None

        async def extract(*args):
            return "미리보기 본문"

        with patch.object(scraper, "reader", reader), patch.object(scraper, "extract", extract):
            response = await self.client.get("/api/preview/test/1")
        self.assertEqual(response.json()["body"], "미리보기 본문")
        self.assertEqual((await self.client.get("/api/books")).json(), [])
        response = await self.client.post("/api/jobs", json={"snapshot_id": "test", "start": 2, "end": 1})
        self.assertEqual(response.status_code, 400)
        response = await self.client.post("/api/jobs", json={"snapshot_id": "test", "start": 8, "end": 9})
        self.assertEqual(response.status_code, 400)

    async def test_restart_marks_unfinished_jobs_and_cross_origin_rejected(self):
        service.save_job({"id": "restart", "status": "running", "items": [{"status": "running"}, {"status": "pending"}]})
        service.initialize()
        job = service.get_job("restart")
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual([i["status"] for i in job["items"]], ["interrupted", "interrupted"])
        response = await self.client.post("/api/jobs", json={"snapshot_id": "test"}, headers={"Origin": "https://other.example"})
        self.assertEqual(response.status_code, 403)

    async def test_private_address_rejected(self):
        with self.assertRaises(ValueError):
            await scraper.validate_public("http://127.0.0.1/")

    async def test_repeated_load_failure_stops_after_one_new_session(self):
        @asynccontextmanager
        async def reader():
            yield None

        async def fail(*args):
            raise ValueError("본문 로딩 실패")

        service.snapshots["test"]["chapters"] = [
            {"number": n, "title": str(n), "url": f"https://example.com/chapter/{n}"} for n in range(1, 6)]
        with patch.object(scraper, "reader", reader), patch.object(scraper, "extract", fail):
            result = await self.client.post("/api/jobs", json={"snapshot_id": "test", "delay": 1})
            await self.finish()
        job = service.get_job(result.json()["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual([i["status"] for i in job["items"]], ["failed", "interrupted", "interrupted", "interrupted", "interrupted"])
        self.assertEqual(job.get("recovery_count", 0), 1)

    async def test_access_limit_restarts_session_and_resumes_at_failed_chapter(self):
        sessions = []
        closed = []
        requests = []

        @asynccontextmanager
        async def reader():
            session = len(sessions) + 1
            sessions.append(session)
            try:
                yield session
            finally:
                closed.append(session)

        async def extract(session, chapter, selector):
            requests.append((session, chapter["number"]))
            if chapter["number"] == 2 and session == 1:
                raise scraper.SiteAccessError("HTTP 403", status_code=403)
            return f"{chapter['number']}화 본문"

        with patch.object(scraper, "reader", reader), patch.object(scraper, "extract", extract), patch.object(service, "SESSION_RESTART_DELAY", 0.01, create=True):
            res = await self.client.post("/api/jobs", json={"snapshot_id": "test", "delay": 1, "skip_existing": False})
            await self.finish()
        job = service.get_job(res.json()["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual([i["status"] for i in job["items"]], ["saved", "saved"])
        self.assertEqual(requests, [(1, 1), (1, 2), (2, 2)])
        self.assertEqual(closed, [1, 2])
        self.assertEqual(job["recovery_count"], 1)
        self.assertEqual(job["export_count"], 2)
        self.assertEqual(find_bodies(), ["1화 본문", "2화 본문"])

    async def test_same_chapter_still_forbidden_in_new_session_stops(self):
        sessions = []

        @asynccontextmanager
        async def reader():
            sessions.append(object())
            yield sessions[-1]

        async def extract(session, chapter, selector):
            raise scraper.SiteAccessError("HTTP 403", status_code=403)

        with patch.object(scraper, "reader", reader), patch.object(scraper, "extract", extract), patch.object(service, "SESSION_RESTART_DELAY", 0.01, create=True):
            res = await self.client.post("/api/jobs", json={"snapshot_id": "test", "delay": 1})
            await self.finish()
        job = service.get_job(res.json()["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual([i["status"] for i in job["items"]], ["failed", "interrupted"])
        self.assertEqual(len(sessions), 2)

    async def test_stop_during_cooldown_does_not_open_new_session(self):
        sessions = []
        closed = []

        @asynccontextmanager
        async def reader():
            sessions.append(object())
            try:
                yield sessions[-1]
            finally:
                closed.append(True)

        async def extract(*args):
            raise scraper.SiteAccessError("HTTP 403", status_code=403)

        with patch.object(scraper, "reader", reader), patch.object(scraper, "extract", extract), patch.object(service, "SESSION_RESTART_DELAY", 60):
            res = await self.client.post("/api/jobs", json={"snapshot_id": "test", "delay": 1})
            job_id = res.json()["id"]
            for _ in range(100):
                await asyncio.sleep(0.001)
                if service.get_job(job_id)["status"] == "waiting":
                    break
            waiting = service.get_job(job_id)
            self.assertEqual(waiting["status"], "waiting")
            self.assertEqual(len(closed), 1)
            self.assertIn("resume_at", waiting)
            await self.client.post(f"/api/jobs/{job_id}/stop", json={})
            await self.finish()
        self.assertEqual(service.get_job(job_id)["status"], "stopped")
        self.assertEqual(len(sessions), 1)

    async def test_later_chapter_can_recover_again_without_refetching_saved_chapters(self):
        sessions = []
        seen = set()

        @asynccontextmanager
        async def reader():
            sessions.append(object())
            yield sessions[-1]

        async def extract(session, chapter, selector):
            number = chapter["number"]
            if number not in seen:
                seen.add(number)
                raise scraper.SiteAccessError("HTTP 403", status_code=403)
            return f"{number}화 본문"

        with patch.object(scraper, "reader", reader), patch.object(scraper, "extract", extract), patch.object(service, "SESSION_RESTART_DELAY", 0.01):
            res = await self.client.post("/api/jobs", json={"snapshot_id": "test", "delay": 1})
            await self.finish()
        job = service.get_job(res.json()["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["recovery_count"], 2)
        self.assertEqual([e["number"] for e in job["recovery_log"]], [1, 2])
        self.assertEqual(find_bodies(), ["1화 본문", "2화 본문"])

    async def test_verification_error_gets_one_new_session_then_stops_if_repeated(self):
        sessions = []

        @asynccontextmanager
        async def reader():
            sessions.append(object())
            yield sessions[-1]

        async def extract(*args):
            raise scraper.SiteAccessError("보안 인증 필요", status_code=403, requires_verification=True)

        with patch.object(scraper, "reader", reader), patch.object(scraper, "extract", extract):
            res = await self.client.post("/api/jobs", json={"snapshot_id": "test", "delay": 1})
            await self.finish()
        job = service.get_job(res.json()["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(len(sessions), 2)
        self.assertEqual(job.get("recovery_count", 0), 1)

    async def test_network_error_can_recover_in_new_session(self):
        sessions = []

        @asynccontextmanager
        async def reader():
            sessions.append(object())
            yield len(sessions)

        async def extract(session, chapter, selector):
            if session == 1:
                raise ConnectionError("연결이 끊겼습니다")
            return f"{chapter['number']}화 정상 본문"

        with patch.object(scraper, "reader", reader), patch.object(scraper, "extract", extract):
            res = await self.client.post("/api/jobs", json={"snapshot_id": "test", "delay": 1})
            await self.finish()
        job = service.get_job(res.json()["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["recovery_count"], 1)
        self.assertEqual(find_bodies(), ["1화 정상 본문", "2화 정상 본문"])

    async def test_restart_during_wait_requires_manual_resume(self):
        service.save_job({"id": "waiting", "status": "waiting", "resume_at": "2099-01-01T00:00:00+00:00",
                          "items": [{"status": "saved"}, {"status": "waiting"}, {"status": "pending"}]})
        service.initialize()
        job = service.get_job("waiting")
        self.assertEqual(job["status"], "interrupted")
        self.assertNotIn("resume_at", job)
        self.assertEqual([i["status"] for i in job["items"]], ["saved", "interrupted", "interrupted"])
        self.assertFalse(service.tasks)


def find_bodies():
    with service.connect() as db:
        return [row["body"] for row in db.execute("SELECT body FROM chapters ORDER BY number")]


if __name__ == "__main__":
    unittest.main()
