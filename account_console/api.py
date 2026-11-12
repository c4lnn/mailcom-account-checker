from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .mocker import MockAccount, MockMailService, MockMailState
from .mocker_scenarios import OPERATIONS, SCENARIOS, build_plan

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


class SettingBody(BaseModel):
    ipinfo_key: str = ""


class ProxyBody(BaseModel):
    proxies: str = Field(max_length=200_000)


class VerifyCandidateBody(BaseModel):
    applied: bool
class MockerConfigBody(BaseModel):
    mode: Literal["scenario", "random"] = "scenario"
    scenario: str = "success"
    seed: str = Field(default="", max_length=200)
    operations: list[str] = Field(default_factory=lambda: ["login", "oauth", "imap_select"])
    probability: float = Field(default=0.1, ge=0, le=1)


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

def _apply_mocker_plan(state: MockMailState, config: dict[str, Any],
                       plan: dict[str, Any], emails: set[str]) -> None:
    state.faults.clear()
    state.rejected_logins.difference_update(emails)
    scenario = config.get("scenario") if config.get("mode") == "scenario" else ""
    if scenario == "login_rejected":
        state.rejected_logins.update(emails)
    if scenario == "password_rejected":
        state.password_change_mode = {email: "reject" for email in emails}
    elif scenario == "password_uncertain":
        state.password_change_mode = {email: "commit_then_drop" for email in emails}
    else:
        state.password_change_mode = "success"
    state.configure_fault_plan(plan["faults"])


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
    mocker_service: MockMailService | None = None
    children: dict[str, subprocess.Popen] = {}
    child_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        mocker_service: MockMailService | None = None
        mocker_env_keys = (
            "MAIL_API_ENDPOINT_OVERRIDES", "MAIL_MOCKER_IMAP_HOST",
            "MAIL_MOCKER_IMAP_PORT", "MAIL_MOCKER_CERT",
        )
        prior_mocker_env = {key: os.environ.get(key) for key in mocker_env_keys}
        db.initialize()
        store.remove_job_defaults()
        if mark_orphans_on_start:
            store.mark_orphans_interrupted()
        _app.state.mocker_service = None
        _app.state.mocker_initial_passwords = {}
        _app.state.mocker_initial_accounts = {}
        if os.environ.get("MAIL_MOCKER_MODE") == "1":
            mocker_service = MockMailService(MockMailState({
                "good@example.test": MockAccount(
                    "GoodPassword!", aliases=[{"address": "good@example.test", "state": "ACTIVE"},
                                               {"address": "alias@example.test", "state": "ACTIVE"}],
                    folders={"Drafts": 1, "INBOX": 2, "OUTBOX": 0, "Sent": 0, "Spam": 0, "Trash": 0},
                ),
                "bad@example.test": MockAccount("ServerPassword!"),
            })).start()
            _app.state.mocker_service = mocker_service
            _app.state.mocker_initial_passwords = {
                "good@example.test": "GoodPassword!", "bad@example.test": "ServerPassword!",
            }
            _app.state.mocker_initial_accounts = copy.deepcopy(mocker_service.state.accounts)
            os.environ["MAIL_API_ENDPOINT_OVERRIDES"] = json.dumps(mocker_service.endpoint_overrides)
            os.environ["MAIL_MOCKER_IMAP_HOST"] = "127.0.0.1"
            os.environ["MAIL_MOCKER_IMAP_PORT"] = str(mocker_service.imap_port)
            os.environ["MAIL_MOCKER_CERT"] = str(mocker_service._server_certificate)
            with db.connect() as connection:
                existing = {row[0] for row in connection.execute(
                    "SELECT email FROM imported_accounts"
                ).fetchall()}
            store.import_accounts([
                (email, password) for email, password in [
                    ("good@example.test", "GoodPassword!"),
                    ("bad@example.test", "WrongPassword!"),
                ] if email not in existing
            ])
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
            if mocker_service is not None:
                mocker_service.close()
            if mark_orphans_on_start:
                store.mark_orphans_interrupted()
            for key, value in prior_mocker_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    app = FastAPI(title="Account Checker Console API", lifespan=lifespan)
    app.state.store = store
    app.state.mocker_service = None
    app.state.mocker_initial_accounts = {}
    app.state.mocker_initial_passwords = {}

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

    def schedule_mocker_api_restart(job_id: str) -> None:
        def restart_when_worker_runs() -> None:
            for _ in range(200):
                try:
                    if store.get_job(job_id)["status"] == "running":
                        service = app.state.mocker_service
                        if service is not None:
                            service.close()
                        os._exit(70)
                except KeyError:
                    return
                time.sleep(0.05)
        threading.Thread(target=restart_when_worker_runs, daemon=True).start()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/runtime-config")
    def runtime_config():
        return {"mocker_enabled": os.environ.get("MAIL_MOCKER_MODE") == "1"}

    @app.get("/api/mocker/config")
    def read_mocker_config(_user: str = Depends(require_user)):
        if os.environ.get("MAIL_MOCKER_MODE") != "1":
            raise HTTPException(status_code=404, detail="mocker_mode_disabled")
        raw = store.get_setting("mocker_config")
        config = json.loads(raw) if raw else {"mode": "scenario", "scenario": "success"}
        return {**config, "scenarios": SCENARIOS, "available_operations": list(OPERATIONS)}

    @app.put("/api/mocker/config")
    def write_mocker_config(body: MockerConfigBody, request: Request,
                            _user: str = Depends(require_user)):
        require_same_origin(request)
        if os.environ.get("MAIL_MOCKER_MODE") != "1":
            raise HTTPException(status_code=404, detail="mocker_mode_disabled")
        config = body.model_dump()
        try:
            build_plan(config, ["probe@example.test"])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.set_setting("mocker_config", json.dumps(config, ensure_ascii=False))
        return {**config, "scenarios": SCENARIOS, "available_operations": list(OPERATIONS)}

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
            if os.environ.get("MAIL_MOCKER_MODE") == "1" and not email.strip().lower().endswith("@example.test"):
                errors.append({"line": line_no, "error": "mocker_requires_synthetic_account"})
                continue
            valid.append((email, password))
        if os.environ.get("MAIL_MOCKER_MODE") == "1" and app.state.mocker_service:
            for email, password in valid:
                normalized = email.strip().lower()
                app.state.mocker_initial_passwords.setdefault(normalized, password)
                app.state.mocker_initial_accounts.setdefault(
                    normalized,
                    copy.deepcopy(app.state.mocker_service.state.accounts[normalized]),
                )
                app.state.mocker_service.state.accounts.setdefault(
                    normalized,
                    MockAccount(password, aliases=[
                        {"address": normalized, "state": "ACTIVE"},
                        {"address": "alias@example.test", "state": "ACTIVE"},
                    ], folders={"Drafts": 1, "INBOX": 2, "OUTBOX": 0, "Sent": 0, "Spam": 0, "Trash": 0}),
                )
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
        try:
            mocker_mode = os.environ.get("MAIL_MOCKER_MODE") == "1"
            mocker_plan = None
            mocker_config = None
            remove_aliases = body.remove_aliases
            change_password = body.change_password
            if mocker_mode:
                service = app.state.mocker_service
                if service is None:
                    raise HTTPException(status_code=503, detail="mocker_unavailable")
                accounts = store.selected_accounts(body.account_ids)
                if any(not row["email"].endswith("@example.test") for row in accounts):
                    raise ValueError("mocker_requires_synthetic_account")
                proxies = [f"http://127.0.0.1:{port}" for port in (1, 2, 3, 4)]
                raw_config = store.get_setting("mocker_config")
                mocker_config = json.loads(raw_config) if raw_config else {
                    "mode": "scenario", "scenario": "success",
                }
                random_operations = set(mocker_config.get("operations", [])) if mocker_config.get("mode") == "random" else set()
                scenario = mocker_config.get("scenario") if mocker_config.get("mode") == "scenario" else ""
                if (scenario == "alias_delete_failure" or "alias_remove" in random_operations) and not body.remove_aliases:
                    raise HTTPException(status_code=422, detail="mocker_scenario_requires_alias_removal_option")
                if (scenario in {"password_rejected", "password_uncertain"}
                        or "password_change" in random_operations) and not body.change_password:
                    raise HTTPException(status_code=422, detail="mocker_scenario_requires_password_change_option")
                mocker_plan = build_plan(mocker_config, [row["email"] for row in accounts])
                if mocker_plan.get("lifecycle") == "api_restart" and launcher is not None:
                    raise HTTPException(status_code=422, detail="mocker_lifecycle_requires_subprocess")
            else:
                proxies = _normalize_proxies(store.get_setting("proxies") or "")
            options = {
                "workers": body.workers,
                "proxies": proxies,
                "verbose": body.verbose,
                "remove_aliases": remove_aliases,
                "change_password": change_password,
                "ipinfo_key": "" if mocker_mode else store.get_setting("ipinfo_key") or "",
            }
            if mocker_mode:
                options["mocker_config"] = mocker_config
                options["mocker_plan"] = mocker_plan
            job_id = store.create_job(body.account_ids, options)
            if mocker_mode:
                state = service.state
                emails = {row["email"].lower() for row in accounts}
                for email in emails:
                    account = state.accounts.get(email)
                    initial_account = app.state.mocker_initial_accounts.get(email)
                    if account is not None and initial_account is not None:
                        account.password = initial_account.password
                        account.aliases = copy.deepcopy(initial_account.aliases)
                        account.folders = copy.deepcopy(initial_account.folders)
                _apply_mocker_plan(state, mocker_config, mocker_plan, emails)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            start_worker(job_id)
        except OSError as exc:
            store.set_job_status(job_id, "failed", "worker_start_failed")
            raise HTTPException(status_code=503, detail="worker_start_failed") from exc
        if mocker_mode and mocker_plan.get("lifecycle") == "api_restart":
            schedule_mocker_api_restart(job_id)
        return {"id": job_id, "status": "queued"}

    @app.post("/api/jobs/{job_id}/replay", status_code=202)
    def replay_job(job_id: str, request: Request, _user: str = Depends(require_user)):
        require_same_origin(request)
        if os.environ.get("MAIL_MOCKER_MODE") != "1":
            raise HTTPException(status_code=404, detail="mocker_mode_disabled")
        try:
            original = store.get_job(job_id)
            options = store.job_options(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job_not_found") from exc
        if original["status"] not in {"completed", "failed", "interrupted"}:
            raise HTTPException(status_code=409, detail="replay_job_not_finished")
        plan = options.get("mocker_plan")
        config = options.get("mocker_config")
        if not isinstance(plan, dict) or plan.get("version") != 1 or not isinstance(plan.get("faults"), list):
            raise HTTPException(status_code=422, detail="mocker_plan_unavailable")
        if plan.get("lifecycle") == "api_restart" and launcher is not None:
            raise HTTPException(status_code=422, detail="mocker_lifecycle_requires_subprocess")
        service = app.state.mocker_service
        if service is None:
            raise HTTPException(status_code=503, detail="mocker_unavailable")
        account_ids = options.get("account_ids")
        accounts = store.selected_accounts(account_ids) if isinstance(account_ids, list) else []
        if not accounts or any(not row["email"].endswith("@example.test") for row in accounts):
            raise HTTPException(status_code=422, detail="mocker_requires_synthetic_account")
        emails = {row["email"].lower() for row in accounts}
        for email in emails:
            account = service.state.accounts.get(email)
            initial_account = app.state.mocker_initial_accounts.get(email)
            if account is not None and initial_account is not None:
                account.password = initial_account.password
                account.aliases = copy.deepcopy(initial_account.aliases)
                account.folders = copy.deepcopy(initial_account.folders)
        _apply_mocker_plan(service.state, config if isinstance(config, dict) else {}, plan, emails)
        try:
            new_job_id = store.create_job(account_ids, options)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            start_worker(new_job_id)
        except OSError as exc:
            store.set_job_status(new_job_id, "failed", "worker_start_failed")
            raise HTTPException(status_code=503, detail="worker_start_failed") from exc
        if plan.get("lifecycle") == "api_restart":
            if launcher is not None:
                raise HTTPException(status_code=422, detail="mocker_lifecycle_requires_subprocess")
            schedule_mocker_api_restart(new_job_id)
        return {"id": new_job_id, "status": "queued", "replayed_from": job_id}

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
            job = store.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="job_not_found") from exc
        verbose = bool(job["options"].get("verbose"))

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
                    yield f"id: {cursor}\ndata: {json.dumps({**item, 'verbose': verbose}, ensure_ascii=False)}\n\n"
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
