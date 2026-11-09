import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import hmac
import json
import logging
import time

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy import inspect, text
from starlette.concurrency import run_in_threadpool

from creator_desk.db import connect
from .config import Settings
from .inference import ModelGateway
from .licensing import Licensing, digest
from .schemas import Activation, CreateLicense, ExtendLicense, Generation, Inspection, InspectedChat


class RequestLimits:
    """Hard body bound even for chunked uploads; no bodies or credentials in logs."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > 65536:
                return await JSONResponse({"detail": "body_too_large"}, status_code=413)(scope, receive, send)
            chunks.append(body)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        started = False

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, replay, tracked_send)
        except Exception as exc:
            logging.getLogger("creator_desk_server").error("request_failed type=%s", type(exc).__name__)
            if not started:
                await JSONResponse({"detail": "internal_error"}, status_code=500)(scope, receive, send)


def create_app(settings=None, gateway=None):
    settings = settings or Settings.from_env()
    settings.validate()
    engine, sessions = connect(settings.database_url)
    licenses = Licensing(sessions)
    model = gateway or ModelGateway(settings)
    capacity = asyncio.Semaphore(settings.model_parallel)
    attempts = OrderedDict()

    @asynccontextmanager
    async def lifespan(app):
        if not inspect(engine).has_table("license_audit"):
            raise RuntimeError("Run python -m creator_desk_server.manage migrate first")
        async def maintenance():
            while True:
                await asyncio.sleep(60)
                try:
                    await run_in_threadpool(licenses.maintenance)
                except Exception as exc:
                    logging.getLogger("creator_desk_server").error("maintenance_failed type=%s", type(exc).__name__)

        await run_in_threadpool(licenses.maintenance)
        cleanup = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
            engine.dispose()

    app = FastAPI(title="Creator Desk API", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.add_middleware(RequestLimits)
    app.state.licensing = licenses
    app.state.engine = engine

    @app.exception_handler(Exception)
    async def safe_error(request, exc):
        # Exception tracebacks can include prompts, SQL parameters and tokens.
        return JSONResponse({"detail": "internal_error"}, status_code=500)

    @app.exception_handler(RequestValidationError)
    async def safe_validation(request, exc):
        # FastAPI's default validation errors echo submitted values (including keys).
        return JSONResponse({"detail": "invalid_request"}, status_code=422)

    def bearer(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer ") or not 32 <= len(authorization[7:]) <= 128:
            raise HTTPException(401, "authentication_required")
        return authorization[7:]

    def admin(token=Depends(bearer), x_admin_actor: str = Header(default="admin-api", max_length=80)):
        if not hmac.compare_digest(token, settings.admin_token):
            raise HTTPException(403, "admin_required")
        return x_admin_actor

    def activation_rate(request):
        ip = request.client.host if request.client else "unknown"
        stamp = time.monotonic()
        values = [v for v in attempts.pop(ip, []) if stamp - v < 60]
        values.append(stamp)
        attempts[ip] = values[-11:]
        while len(attempts) > 10000:
            attempts.popitem(last=False)
        if len(values) > 10:
            raise HTTPException(429, "activation_rate_limit")

    @app.get("/health")
    def health():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/v1/activate")
    async def activate(data: Activation, request: Request):
        activation_rate(request)
        return await run_in_threadpool(licenses.activate, data)

    @app.get("/v1/license")
    def status(token=Depends(bearer)):
        return licenses.status(token)

    async def execute(data, token, inspect_mode=False):
        context = {"screen": data.screen} if inspect_mode else data.context.model_dump(exclude_none=True)
        if len(json.dumps(context, ensure_ascii=False)) > 30000:
            raise HTTPException(422, "context_too_large")
        if capacity.locked():
            raise HTTPException(429, "server_busy")
        # No await between checking and acquiring: one event-loop worker per instance.
        await capacity.acquire()
        job_id = None
        try:
            context_hash = digest(("inspect:" if inspect_mode else "generate:") + json.dumps(context, sort_keys=True, ensure_ascii=False))
            job_id, cached = await run_in_threadpool(licenses.reserve, token, data.request_id,
                                                    context_hash, settings.model_timeout)
            if cached is not None:
                return cached
            try:
                async with asyncio.timeout(settings.model_timeout):
                    result = await model.inspect_screen(data.screen) if inspect_mode else await model.generate(context)
                # Validate even custom gateway output before returning or persisting it.
                from .schemas import Draft
                result = (InspectedChat if inspect_mode else Draft).model_validate(result).model_dump()
                await run_in_threadpool(licenses.finish, job_id, result)
                # Recheck after generation: an admin may have blocked access during inference.
                await run_in_threadpool(licenses.status, token)
                return result
            except HTTPException:
                raise
            except asyncio.CancelledError:
                # A disconnected/stopped client must never receive a late answer.
                await asyncio.shield(run_in_threadpool(licenses.finish, job_id))
                raise
            except Exception:
                await run_in_threadpool(licenses.finish, job_id)
                raise HTTPException(502, "model_unavailable") from None
        finally:
            capacity.release()

    @app.post("/v1/generate")
    async def generate(data: Generation, token=Depends(bearer)):
        return await execute(data, token)

    @app.post("/v1/inspect")
    async def inspect_screen(data: Inspection, token=Depends(bearer)):
        return await execute(data, token, inspect_mode=True)

    @app.post("/admin/licenses")
    def create_license(data: CreateLicense, actor=Depends(admin)):
        return licenses.create(data, actor)

    @app.get("/admin/licenses", dependencies=[Depends(admin)])
    def list_licenses(offset: int = 0):
        if not 0 <= offset <= 1000000:
            raise HTTPException(422, "invalid_offset")
        return licenses.admin_list(offset)

    @app.get("/admin/licenses/{license_id}", dependencies=[Depends(admin)])
    def get_license(license_id: str):
        from .models import License
        with sessions() as s:
            item = s.get(License, license_id)
            if item is None:
                raise HTTPException(404, "license_not_found")
            return licenses.describe(s, item)

    @app.post("/admin/licenses/{license_id}/extend")
    def extend(license_id: str, data: ExtendLicense, actor=Depends(admin)):
        return licenses.admin_change(license_id, "extend", actor, data.days)

    @app.post("/admin/licenses/{license_id}/{action}")
    def change(license_id: str, action: str, actor=Depends(admin)):
        if action not in {"block", "unblock", "reset"}:
            raise HTTPException(404, "unknown_action")
        return licenses.admin_change(license_id, action, actor)

    return app
