"""Server mode: sign-in, per-user isolation, limits, page allowances, the audit log and the admin API."""

import io
import sqlite3
import time
import zipfile
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from pdf2epub.web.auth import cookie_name
from pdf2epub.web.quota import Quota
from pdf2epub.web.security import RateLimiter
from pdf2epub.web.server import create_app
from pdf2epub.web.settings import Limits, Settings

BASE = "https://pdf2epub.test"


def settings_for(tmp: Path, **limits) -> Settings:
    return Settings(
        mode="server", data_dir=tmp, base_url=BASE, session_secret="s" * 48,
        google_client_id="client", google_client_secret="secret", admin_emails=frozenset({"admin@example.com"}),
        trust_proxy=True, worker_in_process=False, limits=Limits(**limits),
    )


def make_pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for n in range(pages):
        doc.new_page().insert_text((72, 72), f"Page {n + 1} of a small test book.")
    return doc.tobytes()


def make_epub() -> bytes:
    from test_enrich import epub3_hebrew

    return epub3_hebrew("he")


class Site:
    """A server-mode app with helpers to act as different signed-in users."""

    def __init__(self, tmp: Path, start_worker: bool = False, **limits):
        self.settings = settings_for(tmp, **limits)
        self.app = create_app(settings=self.settings, start_worker=start_worker)
        self.db = self.app.state.db

    def client(self, email: str | None = None) -> TestClient:
        c = TestClient(self.app, base_url=BASE, headers={"Origin": BASE})
        if email:
            user = self.db.upsert_google_user(f"sub-{email}", email, email.split("@")[0], "", email == "admin@example.com", "203.0.113.5", "IL")
            token = f"token-{email}-{time.time_ns()}"
            self.db.create_session(token, user["id"], 30, "203.0.113.5", "IL", "pytest")
            c.cookies.set(cookie_name(self.settings), token)
            c.user = user
        return c

    def upload(self, c: TestClient, name: str, data: bytes, mime: str = "application/pdf"):
        return c.post("/api/jobs", files={"file": (name, data, mime)})


def wait_status(c: TestClient, job_id: str, statuses: set[str], timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.2)
    raise AssertionError(f"job stuck in {job['status']}")


# -- sign-in and cross-site protection -----------------------------------------------------------------


def test_requires_sign_in_and_refuses_cross_site(tmp_path: Path):
    site = Site(tmp_path)
    anon = site.client()
    assert anon.get("/api/jobs").status_code == 401
    assert anon.get("/api/me").status_code == 401
    assert anon.get("/").status_code == 200  # the page itself loads, to show the sign-in button
    alice = site.client("alice@example.com")
    assert alice.get("/api/me").json()["email"] == "alice@example.com"
    # the same signed-in browser, but the request comes from another site (CSRF)
    res = alice.post("/api/jobs", files={"file": ("a.pdf", make_pdf(1), "application/pdf")}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    res = alice.post("/api/jobs", files={"file": ("a.pdf", make_pdf(1), "application/pdf")}, headers={"Origin": ""})
    assert res.status_code == 403  # no Origin/Referer at all is refused on the public server


def test_security_headers(tmp_path: Path):
    res = Site(tmp_path).client().get("/")
    h = res.headers
    assert "default-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["strict-transport-security"].startswith("max-age=")
    assert h["x-request-id"]


def test_google_callback_creates_session_and_audits(tmp_path: Path, monkeypatch):
    site = Site(tmp_path)
    from pdf2epub.web import auth as auth_module

    infos = iter([
        {"userinfo": {"sub": "g-1", "email": "Admin@Example.com", "email_verified": True, "name": "Admin"}},
        {"userinfo": {"sub": "g-2", "email": "x@example.com", "email_verified": False}},
    ])

    async def fake_token(self, request, **kw):  # noqa: ARG001
        return next(infos)

    from authlib.integrations.starlette_client import StarletteOAuth2App

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", fake_token)
    c = site.client()
    res = c.get("/auth/callback", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/"
    assert cookie_name(site.settings) in res.cookies
    me = c.get("/api/me").json()
    assert me["email"] == "admin@example.com" and me["role"] == "admin"
    res = site.client().get("/auth/callback", follow_redirects=False)
    assert res.headers["location"] == "/?signin=unverified"
    actions = [(r["action"], r["outcome"]) for r in site.db.audit_rows()]
    assert ("auth.login", "ok") in actions and ("auth.login", "denied") in actions
    assert auth_module.LOCAL_USER["id"] == "local"


def test_client_address_from_proxy_headers(tmp_path: Path):
    site = Site(tmp_path)
    alice = site.client("alice@example.com")
    alice.post("/api/jobs", files={"file": ("x.txt", b"not a book", "text/plain")},
               headers={"CF-Connecting-IP": "198.51.100.7", "CF-IPCountry": "FR"})
    row = site.db.audit_rows(action="job.upload")[0]
    assert row["outcome"] == "denied" and row["details"]["reason"] == "type"
    assert (row["ip"], row["country"]) == ("198.51.100.7", "FR")
    # without a trusted proxy the same headers are ignored (a client could forge them)
    from starlette.requests import Request

    from pdf2epub.web.security import client_of

    scope = {"type": "http", "headers": [(b"cf-connecting-ip", b"198.51.100.7")], "client": ("10.0.0.9", 1234)}
    untrusted = Settings(**{**site.settings.__dict__, "trust_proxy": False})
    assert client_of(Request(scope), untrusted).ip == "10.0.0.9"


# -- isolation between users ------------------------------------------------------------------------


def test_users_cannot_see_each_others_books(tmp_path: Path):
    site = Site(tmp_path)
    alice, bob, admin = site.client("alice@example.com"), site.client("bob@example.com"), site.client("admin@example.com")
    job = site.upload(alice, "alice.epub", make_epub(), "application/epub+zip").json()
    assert [j["id"] for j in alice.get("/api/jobs").json()] == [job["id"]]
    assert bob.get("/api/jobs").json() == []
    for path in ("", "/draft", "/cover", "/epub", "/chapters/0"):
        assert bob.get(f"/api/jobs/{job['id']}{path}").status_code == 404
        assert admin.get(f"/api/jobs/{job['id']}{path}").status_code == 404  # admins don't read people's books
    assert bob.delete(f"/api/jobs/{job['id']}").status_code == 404
    assert bob.post(f"/api/jobs/{job['id']}/build", json={}).status_code == 404
    assert "owner_id" not in job and "worker" not in job


# -- limits ------------------------------------------------------------------------------------------


def test_upload_limits(tmp_path: Path):
    site = Site(tmp_path, max_waiting_jobs=2, max_jobs_per_day=3, max_upload_mb=1, max_storage_mb=2)
    alice = site.client("alice@example.com")
    assert site.upload(alice, "big.pdf", make_pdf(1) + b"0" * (2 * 1024 * 1024)).status_code == 413
    assert site.upload(alice, "fake.pdf", b"MZ\x90\x00 an executable").status_code == 400
    assert site.upload(alice, "1.pdf", make_pdf(1)).status_code == 200
    assert site.upload(alice, "2.pdf", make_pdf(1)).status_code == 200
    res = site.upload(alice, "3.pdf", make_pdf(1))
    assert res.status_code == 429 and "waiting" in res.json()["detail"]  # nobody processes jobs in this test
    for job in alice.get("/api/jobs").json():
        site.db.update_job(job["id"], status="done", pending=None)
    assert site.upload(alice, "3.pdf", make_pdf(1)).status_code == 200
    res = site.upload(alice, "4.pdf", make_pdf(1))
    assert res.status_code == 429 and "today" in res.json()["detail"]
    reasons = {r["details"].get("reason") for r in site.db.audit_rows(action="job.upload", outcome="denied")}
    assert {"content", "type", "waiting", "daily"} <= reasons


def _zip(entries: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        for name, data in entries.items():
            z.writestr(name, data)
    return out.getvalue()


def test_hostile_epubs_rejected(tmp_path: Path):
    site = Site(tmp_path)
    alice = site.client("alice@example.com")
    bomb = _zip({"META-INF/container.xml": b"<container/>", "OEBPS/bomb.xhtml": b"\0" * 50_000_000})
    escape = _zip({"META-INF/container.xml": b"<container/>", "../../etc/cron.d/evil": b"* * * * * root rm -rf /"})
    not_epub = _zip({"hello.txt": b"hi"})
    for data, expected in ((bomb, "compression bomb"), (escape, "unsafe file name"), (not_epub, "not an EPUB")):
        res = site.upload(alice, "x.epub", data, "application/epub+zip")
        assert res.status_code == 400 and expected in res.json()["detail"], res.json()
    assert alice.get("/api/jobs").json() == []
    assert not any((tmp_path / "jobs").iterdir())  # rejected uploads leave nothing behind


def test_rate_limit(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(RateLimiter.RULES, "read", (5, 60))
    site = Site(tmp_path)
    alice = site.client("alice@example.com")
    codes = [alice.get("/api/jobs").status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    assert site.db.audit_rows(action="rate.limited")


# -- page allowance ----------------------------------------------------------------------------------


def test_quota_charge_refund_and_grant(tmp_path: Path):
    site = Site(tmp_path)
    user = site.db.upsert_google_user("s", "q@example.com", "Q", "", False, "", "")
    quota = Quota(site.db, default_free_pages=10)
    assert quota.balance(user["id"]).available == 10
    assert quota.charge(user["id"], "job1", 8)
    assert not quota.charge(user["id"], "job2", 5)  # only 2 left: nothing taken
    assert quota.balance(user["id"]).free_left == 2
    quota.grant(user["id"], 20)
    assert quota.charge(user["id"], "job3", 7)  # 2 free + 5 paid
    b = quota.balance(user["id"])
    assert (b.free_left, b.paid) == (0, 15)
    assert quota.refund(user["id"], "job3") == 7 and quota.refund(user["id"], "job3") == 0  # refunded once
    b = quota.balance(user["id"])
    assert (b.free_left, b.paid) == (2, 20)


def test_book_larger_than_allowance_fails_without_charge(tmp_path: Path):
    site = Site(tmp_path, start_worker=True, free_pages_per_month=2)
    alice = site.client("alice@example.com")
    job = site.upload(alice, "three.pdf", make_pdf(3)).json()
    done = wait_status(alice, job["id"], {"failed"})
    assert "3 pages" in done["error"] and "2 pages left" in done["error"]
    assert alice.get("/api/me").json()["balance"]["available"] == 2


def test_failed_conversion_is_refunded(tmp_path: Path, monkeypatch):
    from pdf2epub.web import worker as worker_module

    def broken(*a, **kw):
        raise RuntimeError("internal details that must not reach the user")

    monkeypatch.setattr(worker_module, "prepare", broken)
    site = Site(tmp_path, start_worker=True, free_pages_per_month=10)
    alice = site.client("alice@example.com")
    job = site.upload(alice, "two.pdf", make_pdf(2)).json()
    done = wait_status(alice, job["id"], {"failed"})
    assert "internal details" not in done["error"] and done["error"].startswith("The conversion failed")
    assert alice.get("/api/me").json()["balance"]["available"] == 10


# -- admin, blocking, audit -----------------------------------------------------------------------


def test_admin_api_and_blocking(tmp_path: Path):
    site = Site(tmp_path)
    alice, admin = site.client("alice@example.com"), site.client("admin@example.com")
    assert alice.get("/api/admin/overview").status_code == 404  # not even revealed to non-admins
    overview = admin.get("/api/admin/overview").json()
    assert overview["stats"]["users_total"] == 2 and len(overview["days"]) == 30
    users = {u["email"]: u for u in admin.get("/api/admin/users").json()}
    assert "google_sub" not in users["alice@example.com"]
    alice_id = users["alice@example.com"]["id"]
    assert admin.post(f"/api/admin/users/{alice_id}/grant", json={"pages": 50}).json()["balance"]["paid"] == 50
    assert admin.post(f"/api/admin/users/{alice_id}/block").json()["status"] == "blocked"
    assert alice.get("/api/jobs").status_code == 401  # sessions of a blocked user stop working at once
    assert admin.post(f"/api/admin/users/{site.client('admin@example.com').user['id']}/block").status_code == 400
    actions = [r["action"] for r in admin.get("/api/admin/audit").json()]
    assert "admin.block" in actions and "admin.grant" in actions and "admin.denied" in actions


def test_audit_log_is_append_only_and_tamper_evident(tmp_path: Path):
    site = Site(tmp_path)
    admin = site.client("admin@example.com")
    site.upload(admin, "x.txt", b"nope", "text/plain")
    site.upload(admin, "y.txt", b"nope", "text/plain")
    assert admin.get("/api/admin/audit/verify").json()["intact"]
    with pytest.raises(sqlite3.DatabaseError):
        site.db.execute("UPDATE audit SET outcome='ok'")
    with pytest.raises(sqlite3.DatabaseError):
        site.db.execute("DELETE FROM audit")
    # Someone with raw access drops the triggers and edits a row: the hash chain shows it.
    site.db.execute("DROP TRIGGER audit_no_update")
    site.db.execute("UPDATE audit SET target='edited' WHERE id=1")
    assert not admin.get("/api/admin/audit/verify").json()["intact"]


def test_draft_json_round_trip():
    from pdf2epub.draftio import draft_from_dict, draft_to_dict
    from pdf2epub.model import Block, Book, Chapter, Kind
    from pdf2epub.pipeline import Draft

    block = Block(Kind.IMAGE, page=3, image=b"\x89PNG...", image_ext="png", box=(0.1, 0.2, 0.3, 0.4), pages=[3, 4])
    book = Book(title="T", authors=["A"], language="he", direction="rtl", cover=b"\xff\xd8\xff",
                chapters=[Chapter(title="C", blocks=[Block(Kind.HEADING, text="C", page=1, level=2), block], level=2)])
    draft = Draft(book=book, pages=10, scanned_pages=2, ocr_lang="heb", ocr_fixes=1, seconds=1.5, warnings=["w"])
    again = draft_from_dict(draft_to_dict(draft))
    assert again == draft
    with pytest.raises(ValueError):
        draft_from_dict({"format": 99})


# -- the isolated worker ----------------------------------------------------------------------------


def _isolated_worker(site: Site):
    import threading

    from pdf2epub.web.worker import Worker

    worker = Worker(site.settings, site.db, site.app.state.library, isolate=True, name="test-isolated")
    threading.Thread(target=worker.run, daemon=True).start()
    return worker


def test_isolated_worker_runs_jobs_in_a_child_process(tmp_path: Path):
    site = Site(tmp_path)
    alice = site.client("alice@example.com")
    job = site.upload(alice, "book.epub", make_epub(), "application/epub+zip").json()
    worker = _isolated_worker(site)
    try:
        done = wait_status(alice, job["id"], {"review", "failed"}, timeout=120)
        assert done["status"] == "review", done["error"]
        assert site.db.job(job["id"])["worker"] == "test-isolated"
    finally:
        worker.stop.set()


def test_isolated_worker_enforces_the_time_limit(tmp_path: Path):
    site = Site(tmp_path, job_timeout_minutes=0)
    alice = site.client("alice@example.com")
    job = site.upload(alice, "book.epub", make_epub(), "application/epub+zip").json()
    worker = _isolated_worker(site)
    try:
        done = wait_status(alice, job["id"], {"failed", "review"}, timeout=60)
        assert done["status"] == "failed" and "longer than 0 minutes" in done["error"]
    finally:
        worker.stop.set()


def test_job_processes_get_no_secrets(tmp_path: Path, monkeypatch):
    from pdf2epub.web.worker import child_environment

    for name in ("PDF2EPUB_SESSION_SECRET", "PDF2EPUB_GOOGLE_CLIENT_SECRET", "PDF2EPUB_GOOGLE_CLIENT_ID", "OPENAI_API_KEY", "GITHUB_TOKEN", "PDF2EPUB_POSTHOG_KEY"):
        monkeypatch.setenv(name, "do-not-leak")
    env = child_environment(settings_for(tmp_path))
    assert "do-not-leak" not in env.values()
    assert env["PDF2EPUB_MODE"] == "server" and env["PDF2EPUB_DATA"] == str(tmp_path)


def test_backup_is_a_consistent_rotated_copy(tmp_path: Path):
    import gzip

    from pdf2epub.web.backup import backup

    site = Site(tmp_path)
    site.client("reader@example.com")
    made = [backup(tmp_path, keep=2) for _ in range(3)]
    kept = sorted((tmp_path / "backups").glob("*.db.gz"))
    assert kept == sorted(made[1:])
    restored = tmp_path / "restored.db"
    restored.write_bytes(gzip.decompress(kept[-1].read_bytes()))
    with sqlite3.connect(restored) as conn:
        assert conn.execute("SELECT email FROM users").fetchall() == [("reader@example.com",)]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
