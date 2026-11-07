from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .db import Database
from .security import SecretBox, SessionSigner, password_matches
from .store import Store


class LoginBody(BaseModel):
    password: str


class ImportBody(BaseModel):
    content: str = Field(max_length=2_000_000)


class JobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_ids: list[int] = Field(min_length=1, max_length=10000)
    workers: int = Field(default=8, ge=1, le=256)
    verbose: bool = False
    remove_aliases: bool = False
    change_password: bool = False
    confirm_mailbox_cleanup: bool = False
    confirm_remove_aliases: bool = False
    confirm_change_password: bool = False


class SettingBody(BaseModel):
    ipinfo_key: str = ""


class ProxyBody(BaseModel):
    proxies: str = Field(max_length=200_000)


class VerifyCandidateBody(BaseModel):
    applied: bool


def _normalize_proxies(text: str) -> list[str]:
    rows: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "://" not in line:
            parts = line.split(":", 3)
            if len(parts) == 2:
                line = f"http://{parts[0]}:{parts[1]}"
            elif len(parts) == 4:
                from urllib.parse import quote

                host, port, username, password = parts
                line = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
            else:
                raise ValueError("proxy_format")
        parsed = urlsplit(line)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("proxy_format")
        if parsed.port is None or not 1 <= parsed.port <= 65535:
            raise ValueError("proxy_format")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("proxy_format")
        rows.append(line)
    if not rows:
        raise ValueError("proxy_file_empty")
    return list(dict.fromkeys(rows))


def create_app(
    database: Database | None = None,
    *,
    secrets_box: SecretBox | None = None,
    signer: SessionSigner | None = None,
    launcher: Callable[[str], Any] | None = None,
    mark_orphans_on_start: bool = True,
) -> FastAPI:
    db = database or Database()
    box = secrets_box or SecretBox()
    sessions = signer or SessionSigner()
    store = Store(db, box)
    children: dict[str, subprocess.Popen] = {}
    child_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.initialize()
        store.remove_job_defaults()
        if mark_orphans_on_start:
            store.mark_orphans_interrupted()
        try:
            yield
        finally:
            with child_lock:
                processes = list(children.values())
            for process in processes:
                if process.stdin:
                    process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if mark_orphans_on_start:
                store.mark_orphans_interrupted()

    app = FastAPI(title="Account Checker Console API", lifespan=lifespan)
    app.state.store = store

    def require_user(request: Request, session: str | None = Cookie(default=None)) -> str:
        if not session or not sessions.verify(session):
            raise HTTPException(status_code=401, detail="unauthorized")
        return "operator"

    def require_same_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin:
            raise HTTPException(status_code=403, detail="origin_required")
        parsed = urlsplit(origin)
        if parsed.netloc.lower() != request.headers.get("host", "").lower():
            raise HTTPException(status_code=403, detail="cross_origin_request")

    def start_worker(job_id: str) -> None:
        if launcher is not None:
            launcher(job_id)
            return
        root = Path(__file__).resolve().parents[1]
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            [sys.executable, "-m", "account_console.worker", job_id, "--watch-parent"],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        with child_lock:
            children[job_id] = process

        def reap_worker():
            process.wait()
            # 异常退出也立即结束任务和恢复候选，不必等 API 再次启动。
            store.mark_orphans_interrupted(job_id)
            if process.stdin:
                process.stdin.close()
            with child_lock:
                children.pop(job_id, None)

        threading.Thread(target=reap_worker, daemon=True).start()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        require_same_origin(request)
        expected = os.environ.get("CONSOLE_PASSWORD", "")
        if not expected or not password_matches(body.password, expected):
            raise HTTPException(status_code=401, detail="invalid_credentials")
        response.set_cookie(
            "session", sessions.issue(), httponly=True,
            secure=os.environ.get("CONSOLE_COOKIE_SECURE", "1") != "0",
            samesite="lax", max_age=sessions.ttl_seconds, path="/",
        )
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, _user: str = Depends(require_user)):
        require_same_origin(request)
        response.delete_cookie("session", path="/")
        return {"ok": True}

    @app.get("/api/auth/session")
    def current_session(_user: str = Depends(require_user)):
        return {"authenticated": True}

    @app.get("/api/dashboard")
    def dashboard(_user: str = Depends(require_user)):
        return store.dashboard()

    @app.post("/api/accounts/import")
    def import_accounts(body: ImportBody, request: Request, _user: str = Depends(require_user)):
        require_same_origin(request)
        valid: list[tuple[str, str]] = []
        errors: list[dict[str, Any]] = []
        for line_no, raw in enumerate(body.content.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "----" not in line:
                errors.append({"line": line_no, "error": "account_format"})
                continue
            email, password = line.split("----", 1)
            if not email.strip() or not password.strip() or "@" not in email:
                errors.append({"line": line_no, "error": "account_format"})
                continue
            valid.append((email, password))
        imported, _ = store.import_accounts(valid)
        return {"imported": imported, "errors": errors}

    @app.get("/api/accounts")
    def accounts(status: str = "pending", _user: str = Depends(require_user)):
        if status not in {"pending", "successful", "failed"}:
            raise HTTPException(status_code=422, detail="status_invalid")
        return {"items": store.accounts(status)}

    @app.get("/api/password-candidates")
    def candidates(email: str = "", job_id: str = "", _user: str = Depends(require_user)):
        return {"items": store.candidates(email=email, job_id=job_id)}

    @app.post("/api/password-candidates/{candidate_id}/verify")
    def verify_candidate(candidate_id: int, body: VerifyCandidateBody, request: Request,
                         _user: str = Depends(require_user)):
        require_same_origin(request)
        try:
            store.update_candidate(candidate_id, "confirmed" if body.applied else "unapplied",
                                   require_uncertain=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/jobs")
    def jobs(_user: str = Depends(require_user)):
        return {"items": store.list_jobs()}

    @app.post("/api/jobs", status_code=202)
    def create_job(body: JobBody, request: Request, _user: str = Depends(require_user)):
        require_same_origin(request)
        if not body.confirm_mailbox_cleanup:
            raise HTTPException(status_code=422, detail="mailbox_cleanup_confirmation_required")
        if body.remove_aliases and not body.confirm_remove_aliases:
            raise HTTPException(status_code=422, detail="alias_removal_confirmation_required")
        if body.change_password and not body.confirm_change_password:
            raise HTTPException(status_code=422, detail="password_change_confirmation_required")
        try:
            proxies = _normalize_proxies(store.get_setting("proxies") or "")
            options = {
                "workers": body.workers,
                "proxies": proxies,
                "verbose": body.verbose,
                "remove_aliases": body.remove_aliases,
                "change_password": body.change_password,
                "ipinfo_key": store.get_setting("ipinfo_key") or "",
            }
            job_id = store.create_job(body.account_ids, options)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            start_worker(job_id)
        except OSError as exc:
            store.set_job_status(job_id, "failed", "worker_start_failed")
            raise HTTPException(status_code=503, detail="worker_start_failed") from exc
        return {"id": job_id, "status": "queued"}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, _user: str = Depends(require_user)):
        try:
            return store.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job_not_found") from exc

    @app.get("/api/jobs/{job_id}/logs")
    def logs(job_id: str, after_id: int = 0, limit: int = 500,
             _user: str = Depends(require_user)):
        try:
            store.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job_not_found") from exc
        return {"items": store.read_logs(job_id, after_id=max(0, after_id),
                                           limit=max(1, min(limit, 1000)))}

    @app.get("/api/jobs/{job_id}/events")
    def events(job_id: str, request: Request, after_id: int = 0,
               _user: str = Depends(require_user)):
        try:
            store.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job_not_found") from exc

        try:
            header_cursor = int(request.headers.get("last-event-id", "0") or 0)
        except ValueError:
            raise HTTPException(status_code=400, detail="event_cursor_invalid") from None

        async def stream():
            cursor = max(0, header_cursor or after_id)
            while True:
                if await request.is_disconnected():
                    break
                items = store.read_logs(job_id, after_id=cursor, limit=250)
                for item in items:
                    cursor = item["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(item['payload'], ensure_ascii=False)}\n\n"
                state = store.get_job(job_id)
                if state["status"] in {"completed", "failed", "interrupted"}:
                    # 状态变成终态前可能又写入了日志，确认游标之后确实为空再结束。
                    if items or store.read_logs(job_id, after_id=cursor, limit=1):
                        continue
                    yield f"event: complete\ndata: {json.dumps({'status': state['status']})}\n\n"
                    break
                if not items:
                    yield ": keep-alive\n\n"
                    await __import__("asyncio").sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/proxies")
    def read_proxies(_user: str = Depends(require_user)):
        text = store.get_setting("proxies") or ""
        proxies = _normalize_proxies(text) if text else []
        return {"configured": bool(proxies), "count": len(proxies)}

    @app.put("/api/proxies")
    def write_proxies(body: ProxyBody, request: Request,
                      _user: str = Depends(require_user)):
        require_same_origin(request)
        try:
            proxies = _normalize_proxies(body.proxies)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.set_setting("proxies", "\n".join(proxies), is_secret=True)
        return {"ok": True}

    @app.get("/api/settings")
    def read_settings(_user: str = Depends(require_user)):
        return {"ipinfo_key_configured": bool(store.get_setting("ipinfo_key"))}

    @app.get("/api/settings/ipinfo-key")
    def reveal_ipinfo_key(response: Response, _user: str = Depends(require_user)):
        key = store.get_setting("ipinfo_key")
        if not key:
            raise HTTPException(status_code=404, detail="ipinfo_key_not_configured")
        response.headers["Cache-Control"] = "no-store, private"
        return {"ipinfo_key": key}

    @app.put("/api/settings")
    def write_settings(body: SettingBody, request: Request,
                       _user: str = Depends(require_user)):
        require_same_origin(request)
        if body.ipinfo_key:
            store.set_setting("ipinfo_key", body.ipinfo_key, is_secret=True)
        return {"ok": True}

    return app


app = create_app()
