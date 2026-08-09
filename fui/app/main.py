"""FUI - a small web console for the Factorio headless server.

Authentication is handled by this app: a single password, no username. A
successful login sets a signed, expiring cookie. The app only ever listens on
127.0.0.1 and is reached through nginx.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from enum import Enum
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .auth import LoginThrottle, issue_token, verify_password, verify_token
from .config import get_settings
from .logs import read_clean_logs
from .mods import ModSyncError, read_mod_inventory, run_privileged_sync
from .players import read_players
from .systemd import ServiceError, control, delete_upload, read_logs, read_status, reset_map
from .uploads import (
    SaveUploadError,
    install_save,
    update_server_settings,
    validate_server_description,
    validate_server_name,
)

logger = logging.getLogger(__name__)

settings = get_settings()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
throttle = LoginThrottle()
upload_lock = threading.Lock()

app = FastAPI(
    title="Factorio Server Control",
    version=__version__,
    root_path=settings.root_path,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)


class Action(str, Enum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"


def _url(path: str) -> str:
    """Build a browser-facing URL, accounting for the /fui prefix nginx strips."""
    return f"{settings.root_path}{path}"


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _authenticated(request: Request) -> bool:
    return verify_token(settings.secret_key, request.cookies.get(settings.cookie_name))


def _instance_settings(instance_id: str):
    try:
        instance = settings.instance(instance_id)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown server instance") from exc
    return instance, replace(
        settings,
        service_name=instance.service_name,
        reset_script_path=instance.reset_script_path,
    )


def _slot_in_use(instance) -> bool:
    return any(Path(instance.saves_dir).glob("*.zip"))


def _instance_display_name(instance) -> str:
    if not instance.upload_slot or not _slot_in_use(instance):
        return instance.name
    try:
        data = json.loads(
            Path(instance.server_settings_path).read_text(encoding="utf-8")
        )
        name = data.get("name")
        return name if isinstance(name, str) and name else instance.name
    except (OSError, ValueError, AttributeError):
        return instance.name


def require_session(request: Request) -> None:
    if not _authenticated(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )


def require_ajax_header(request: Request) -> None:
    """Block cross-site requests that ride on the browser's session cookie.

    A custom header cannot be set by a simple cross-origin form post, and the
    preflight it triggers has no CORS policy to satisfy.
    """
    if request.headers.get("x-requested-with") != "fui":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing X-Requested-With header",
        )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> Response:
    if _authenticated(request):
        return RedirectResponse(_url("/"), status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request, "login.html", {"error": None, "version": __version__}
    )


@app.post("/login")
def login(request: Request, password: str = Form(...)) -> Response:
    client = _client_key(request)
    wait = throttle.retry_after(client)
    if wait > 0:
        logger.warning("Login blocked for %s, %.0fs remaining", client, wait)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": f"Too many attempts. Try again in {int(wait) + 1}s.",
                "version": __version__,
            },
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if not verify_password(settings.password, password):
        throttle.record_failure(client)
        logger.warning("Failed login from %s", client)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect password.", "version": __version__},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    throttle.reset(client)
    response = RedirectResponse(_url("/"), status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key=settings.cookie_name,
        value=issue_token(settings.secret_key, settings.session_ttl),
        max_age=settings.session_ttl,
        path=settings.root_path or "/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse(_url("/login"), status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(settings.cookie_name, path=settings.root_path or "/")
    return response


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> Response:
    if not _authenticated(request):
        return RedirectResponse(_url("/login"), status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"logout_url": _url("/logout"), "version": __version__},
    )


@app.get("/api/servers", dependencies=[Depends(require_session)])
def api_servers() -> dict[str, Any]:
    servers = []
    for instance in settings.instances:
        if instance.upload_slot and not _slot_in_use(instance):
            continue
        _, instance_settings = _instance_settings(instance.id)
        try:
            server_status = read_status(instance_settings)
        except ServiceError:
            server_status = {"running": False, "active_state": "unavailable"}
        try:
            player_status = read_players(
                instance_settings,
                running=bool(server_status.get("running")),
            )
        except ServiceError:
            player_status = {"online_count": 0, "last_join": None}
        servers.append(
            {
                "id": instance.id,
                "name": _instance_display_name(instance),
                "service": instance.service_name,
                "upload_slot": instance.upload_slot,
                "mod_sync_enabled": instance.mod_sync_enabled,
                "reset_enabled": instance.reset_enabled,
                "status": server_status,
                "players": {
                    "online_count": player_status["online_count"],
                    "last_join": player_status["last_join"],
                },
            }
        )
    return {"servers": servers}


@app.get("/api/upload-slots", dependencies=[Depends(require_session)])
def api_upload_slots() -> dict[str, Any]:
    return {
        "slots": [
            {
                "id": instance.id,
                "name": instance.name,
                "port": instance.port,
                "available": not _slot_in_use(instance),
            }
            for instance in settings.instances
            if instance.upload_slot
        ]
    }


@app.post(
    "/api/upload-slots/{instance_id}",
    dependencies=[Depends(require_session), Depends(require_ajax_header)],
)
def api_upload_save(
    instance_id: str,
    name: str = Form(...),
    description: str = Form(...),
    public: bool = Form(default=True),
    save: UploadFile = File(...),
) -> dict[str, Any]:
    instance, instance_settings = _instance_settings(instance_id)
    if not instance.upload_slot:
        raise HTTPException(status.HTTP_409_CONFLICT, "Instance is not an upload slot")
    if not save.filename or not save.filename.lower().endswith(".zip"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Save must be a ZIP file")

    with upload_lock:
        target = None
        original_settings = None
        try:
            clean_name = validate_server_name(name)
            clean_description = validate_server_description(description)
            if read_status(instance_settings)["running"]:
                raise SaveUploadError("Upload slot is already running")
            target = install_save(save.file, Path(instance.saves_dir))
            original_settings = update_server_settings(
                Path(instance.server_settings_path),
                clean_name,
                clean_description,
                public,
            )
            sync_result = run_privileged_sync(
                instance.mod_sync_script_path,
                settings.sudo_path,
                settings.mod_sync_timeout,
            )
            control(instance_settings, "start")
        except (SaveUploadError, ModSyncError, ServiceError) as exc:
            if target is not None:
                target.unlink(missing_ok=True)
            if original_settings is not None:
                Path(instance.server_settings_path).write_bytes(original_settings)
            logger.warning("Save upload for %s failed: %s", instance_id, exc)
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        finally:
            save.file.close()

    return {
        "server": {
            "id": instance.id,
            "name": clean_name,
            "description": clean_description,
            "port": instance.port,
            "public": public,
        },
        "mods": sync_result.get("mods", []),
    }


@app.delete(
    "/api/upload-slots/{instance_id}",
    dependencies=[Depends(require_session), Depends(require_ajax_header)],
)
def api_delete_upload(instance_id: str) -> dict[str, str]:
    instance, _ = _instance_settings(instance_id)
    if not instance.upload_slot:
        raise HTTPException(status.HTTP_409_CONFLICT, "Instance is not an upload slot")

    with upload_lock:
        if not _slot_in_use(instance):
            raise HTTPException(status.HTTP_409_CONFLICT, "Upload slot is already empty")
        delete_script = str(
            Path(instance.mod_sync_script_path).with_name("factorio-delete-upload.sh")
        )
        try:
            delete_upload(delete_script, settings.sudo_path, settings.reset_timeout)
        except ServiceError as exc:
            logger.warning("Upload slot deletion for %s failed: %s", instance_id, exc)
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    logger.warning("Uploaded server %s deleted", instance_id)
    return {"id": instance.id, "status": "available"}


@app.get("/api/servers/{instance_id}/status", dependencies=[Depends(require_session)])
def api_instance_status(instance_id: str) -> dict[str, Any]:
    _, instance_settings = _instance_settings(instance_id)
    try:
        return read_status(instance_settings)
    except ServiceError as exc:
        logger.warning("Status query for %s failed: %s", instance_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.get("/api/servers/{instance_id}/logs", dependencies=[Depends(require_session)])
def api_instance_logs(
    instance_id: str,
    lines: int = Query(default=100, ge=1, le=500),
    current_run: bool = Query(default=True),
    clean: bool = Query(default=False),
) -> dict[str, Any]:
    _, instance_settings = _instance_settings(instance_id)
    try:
        reader = read_clean_logs if clean else read_logs
        return {"lines": reader(instance_settings, lines, current_run)}
    except ServiceError as exc:
        logger.warning("Log query for %s failed: %s", instance_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.get("/api/servers/{instance_id}/players", dependencies=[Depends(require_session)])
def api_instance_players(instance_id: str) -> dict[str, Any]:
    _, instance_settings = _instance_settings(instance_id)
    try:
        return read_players(instance_settings)
    except ServiceError as exc:
        logger.warning("Player query for %s failed: %s", instance_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.get("/api/servers/{instance_id}/mods", dependencies=[Depends(require_session)])
def api_instance_mods(instance_id: str) -> dict[str, Any]:
    instance, _ = _instance_settings(instance_id)
    try:
        return {"mods": read_mod_inventory(Path(instance.mods_dir))}
    except ModSyncError as exc:
        logger.warning("Mod inventory query for %s failed: %s", instance_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.post(
    "/api/servers/{instance_id}/service/{action}",
    dependencies=[Depends(require_session), Depends(require_ajax_header)],
)
def api_instance_control(instance_id: str, action: Action) -> dict[str, Any]:
    _, instance_settings = _instance_settings(instance_id)
    try:
        control(instance_settings, action.value)
        return read_status(instance_settings)
    except ServiceError as exc:
        logger.warning("Action %s for %s failed: %s", action.value, instance_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.post(
    "/api/servers/{instance_id}/reset",
    dependencies=[Depends(require_session), Depends(require_ajax_header)],
)
def api_instance_reset(instance_id: str) -> dict[str, Any]:
    instance, instance_settings = _instance_settings(instance_id)
    if not instance.reset_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Map reset is disabled for uploaded servers")
    logger.warning("Map reset requested for %s", instance_id)
    try:
        reset_map(instance_settings)
        return read_status(instance_settings)
    except ServiceError as exc:
        logger.warning("Map reset for %s failed: %s", instance_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.post(
    "/api/servers/{instance_id}/mods/sync",
    dependencies=[Depends(require_session), Depends(require_ajax_header)],
)
def api_instance_sync_mods(instance_id: str) -> dict[str, object]:
    instance, _ = _instance_settings(instance_id)
    if not instance.mod_sync_enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Save-driven mod synchronization is disabled for this instance",
        )
    logger.warning("Save mod synchronization requested for %s", instance_id)
    try:
        return run_privileged_sync(
            instance.mod_sync_script_path,
            settings.sudo_path,
            settings.mod_sync_timeout,
        )
    except ModSyncError as exc:
        logger.warning("Mod synchronization for %s failed: %s", instance_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.get("/api/status", dependencies=[Depends(require_session)])
def api_status() -> dict[str, Any]:
    try:
        return read_status(settings)
    except ServiceError as exc:
        logger.warning("Status query failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.get("/api/logs", dependencies=[Depends(require_session)])
def api_logs(
    lines: int = Query(default=100, ge=1, le=500),
    current_run: bool = Query(default=True),
    clean: bool = Query(default=False),
) -> dict[str, Any]:
    try:
        reader = read_clean_logs if clean else read_logs
        return {"lines": reader(settings, lines, current_run)}
    except ServiceError as exc:
        logger.warning("Log query failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.get("/api/players", dependencies=[Depends(require_session)])
def api_players() -> dict[str, Any]:
    try:
        return read_players(settings)
    except ServiceError as exc:
        logger.warning("Player query failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.post(
    "/api/service/{action}",
    dependencies=[Depends(require_session), Depends(require_ajax_header)],
)
def api_control(action: Action) -> dict[str, Any]:
    try:
        control(settings, action.value)
        return read_status(settings)
    except ServiceError as exc:
        logger.warning("Action %s failed: %s", action.value, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.post(
    "/api/reset",
    dependencies=[Depends(require_session), Depends(require_ajax_header)],
)
def api_reset() -> dict[str, Any]:
    logger.warning("Map reset requested")
    try:
        reset_map(settings)
        return read_status(settings)
    except ServiceError as exc:
        logger.warning("Map reset failed: %s", exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
