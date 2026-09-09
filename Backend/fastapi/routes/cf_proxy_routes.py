"""
Cloudflare proxy integration for the `cf-worker-ts` worker.

Copy this file to:  Backend/fastapi/routes/cf_proxy_routes.py
See PATCHES.md next to this file for the other integration points
(main.py, settings_manager.py, settings.html, stremio_routes.py).

Flow (once per playback, when a player actually opens the stream):

    GET/HEAD /cf/{token}/{id}/{name}
        -> decode id -> (chat_id, msg_id) or parts[] (plain split files)
        -> resolve each file (get_file_ids) -> media_id/access_hash/file_ref/dc/size
        -> mint a FRESH session for the bot that owns the file, export its
           primary auth key, retire it
        -> build payload + HMAC signature -> POST /bootstrap on the worker
        -> 307 redirect to https://<worker>/stream/<playbackId>

The one-writer rule: the minted session is never used again after export.
The long-lived StreamBot keeps its own session untouched, so there is no
MSG_SEQNO conflict.

Zip-split archives and Global Search streams are not supported here — they
keep using the server-side /dl streamer.
"""

import asyncio
import base64
import hashlib
import hmac
import mimetypes
import secrets
import time
from typing import Dict, Tuple
from urllib.parse import unquote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pyrogram import Client

from Backend.config import Telegram
from Backend.fastapi.routes.stream_routes import select_best_client
from Backend.fastapi.security.tokens import verify_token
from Backend.helper.analytics import client_ip_from, record_stream_start
from Backend.helper.encrypt import decode_string
from Backend.helper.pyro import get_file_ids
from Backend.logger import LOGGER
from Backend.pyrofork.bot import multi_clients

router = APIRouter(tags=["Cloudflare Proxy"])

# HEAD + GET usually arrive back-to-back for the same file. Reuse the bootstrap
# result for a short window so the player does not trigger two logins.
_PLAYBACK_TTL = 30.0
_playback_cache: Dict[str, Tuple[float, str]] = {}


def _canonical_string(payload: dict) -> str:
    """Must match canonicalString() in the worker's src/hmac.ts exactly."""
    keys = sorted(payload["keys"], key=lambda k: k["dc"])
    keys_part = ",".join(f'{k["dc"]}:{k["key"]}' for k in keys)

    if payload.get("parts"):
        parts_part = ",".join(
            ":".join(
                [
                    str(p["id"]),
                    str(p["hash"]),
                    str(p["ref"]),
                    str(p["dc"]),
                    str(p["size"]),
                ]
            )
            for p in payload["parts"]
        )
        return ".".join(
            [
                "MP",
                str(payload["mime"]),
                str(payload["name"]),
                str(payload["primaryDc"]),
                keys_part,
                parts_part,
            ]
        )

    return ".".join(
        [
            str(payload["id"]),
            str(payload["hash"]),
            str(payload["ref"]),
            str(payload["dc"]),
            str(payload["size"]),
            str(payload["mime"]),
            str(payload["name"]),
            str(payload["primaryDc"]),
            keys_part,
        ]
    )


def _sign(payload: dict, secret: str) -> str:
    digest = hmac.new(
        secret.encode(), _canonical_string(payload).encode(), hashlib.sha256
    ).digest()
    # base64url without padding — same encoding the worker verifies.
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def _mint_session(bot_token, api_id, api_hash):
    """Create a fresh bot session, return (auth_key_bytes, primary_dc), retire it."""
    client = Client(
        name=f"cf_boot_{secrets.token_hex(4)}",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=bot_token,
        in_memory=True,
        no_updates=True,
    )
    await client.start()
    try:
        auth_key = await client.storage.auth_key()
        primary_dc = await client.storage.dc_id()
        if not auth_key or len(auth_key) != 256:
            raise RuntimeError("Fresh session produced an unexpected auth key")
        return auth_key, primary_dc
    finally:
        # Retire: never touch this session again (single-writer rule).
        try:
            await client.stop()
        except Exception:
            pass


def _client_credentials(client):
    """Bot credentials of a live client; falls back to the global config."""
    token = getattr(client, "bot_token", None) or Telegram.BOT_TOKEN
    api_id = getattr(client, "api_id", None) or Telegram.API_ID
    api_hash = getattr(client, "api_hash", None) or Telegram.API_HASH
    return token, api_id, api_hash


def _client_order() -> list:
    """Client indices in preferred order: load-aware best first, then the rest."""
    order: list = []
    try:
        best = select_best_client(0)
    except Exception:
        best = None
    if best in multi_clients:
        order.append(best)
    order.extend(idx for idx in multi_clients if idx != best)
    return order


async def _resolve_file_id(chat_id: int, msg_id: int):
    """Resolve a file through the best available bot. Returns (client, file_id)."""
    for idx in _client_order():
        client = multi_clients[idx]
        try:
            fid = await get_file_ids(client, chat_id, msg_id)
        except Exception:
            continue
        if fid is not None:
            return client, fid
    raise HTTPException(status_code=404, detail="File not found")


async def _resolve_parts(parts_payload):
    """Resolve every split part with a SINGLE client.

    The minted session belongs to one bot account and access_hash is
    account-scoped, so all parts must come from the same client. If a client
    cannot resolve every part, the whole set is retried with the next client.
    """
    last_error = None
    for idx in _client_order():
        client = multi_clients[idx]
        fids = []
        ok = True
        for p in parts_payload:
            raw_chat = int(p["chat_id"])
            chat_id = int(f"-100{raw_chat}") if raw_chat > 0 else raw_chat
            msg_id = int(p["msg_id"])
            try:
                fid = await get_file_ids(client, chat_id, msg_id)
            except Exception as e:
                last_error = e
                ok = False
                break
            if fid is None or not getattr(fid, "file_size", 0):
                ok = False
                break
            fids.append(fid)
        if ok:
            return client, fids
    LOGGER.warning(
        "[CF-PROXY] no single client could resolve all parts: %s", last_error
    )
    raise HTTPException(status_code=404, detail="Split parts not found")


async def _mint_key_entry(client):
    """Mint a fresh session with `client`'s own credentials.

    Returns (key_entry, primary_dc) where key_entry is the
    {"dc", "key"} dict embedded in the bootstrap payload.
    """
    token, api_id, api_hash = _client_credentials(client)
    try:
        auth_key, primary_dc = await _mint_session(token, api_id, api_hash)
    except Exception as e:
        LOGGER.error("[CF-PROXY] session mint failed: %s", e)
        raise HTTPException(status_code=502, detail=f"CF session mint failed: {e}")
    key_entry = {"dc": int(primary_dc), "key": base64.b64encode(auth_key).decode()}
    return key_entry, primary_dc


def _part_dict(file_id) -> dict:
    """Map a Pyrogram FileId to the worker's PartInfo shape."""
    return {
        "id": str(file_id.media_id),
        "hash": str(file_id.access_hash),
        "ref": base64.b64encode(file_id.file_reference).decode(),
        "dc": int(file_id.dc_id),
        "size": int(file_id.file_size),
    }


async def _bootstrap(payload: dict, worker_url: str, secret: str) -> str:
    # The worker re-derives file-DC auth keys via exportAuthorization, so the
    # primary key alone is sufficient for every part.
    payload["sig"] = _sign(payload, secret)

    async with httpx.AsyncClient(timeout=20.0) as http:
        resp = await http.post(f"{worker_url.rstrip('/')}/bootstrap", json=payload)
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or not body.get("playbackId"):
        LOGGER.error("[CF-PROXY] bootstrap failed: %s %s", resp.status_code, body)
        raise HTTPException(
            status_code=502,
            detail=f"CF bootstrap failed: {body.get('error') or resp.status_code}",
        )
    return body["playbackId"]


@router.get("/cf/{token}/{id}/{name}")
@router.head("/cf/{token}/{id}/{name}")
async def cf_stream_handler(
    request: Request,
    token: str,
    id: str,
    name: str,
    token_data: dict = Depends(verify_token),
):
    # Configured via config.env only: CF_PROXY_URL + CF_BOOTSTRAP_SECRET.
    worker_url = Telegram.CF_PROXY_URL
    secret = Telegram.CF_BOOTSTRAP_SECRET
    if not worker_url or not secret:
        raise HTTPException(status_code=503, detail="Cloudflare proxy is not configured")

    if request.method != "HEAD":
        asyncio.create_task(
            record_stream_start(
                token,
                token_data.get("name") if token_data else None,
                client_ip_from(request),
                request.headers.get("user-agent", ""),
            )
        )

    # Reuse a fresh bootstrap result (players send HEAD then GET back-to-back).
    now = time.time()
    cached = _playback_cache.get(id)
    if cached and now < cached[0]:
        return RedirectResponse(
            url=f"{worker_url.rstrip('/')}/stream/{cached[1]}", status_code=307
        )

    try:
        decoded = await decode_string(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid stream id")

    # Global Search streams and zip archives keep the server-side /dl streamer.
    if decoded.get("global"):
        raise HTTPException(
            status_code=400,
            detail="Global Search streams are not supported via CF proxy",
        )
    if decoded.get("zip"):
        raise HTTPException(
            status_code=400,
            detail="Zip archives are not supported via CF proxy",
        )

    if not multi_clients:
        raise HTTPException(status_code=503, detail="No Telegram client available")

    decoded_name = unquote(request.path_params.get("name", "") or "") or "video.mkv"

    parts_payload = decoded.get("parts")
    if parts_payload:
        # Plain split file: all parts must be resolved by ONE client — the
        # minted session belongs to one account and access_hash is
        # account-scoped.
        try:
            chosen_client, fids = await _resolve_parts(parts_payload)
        except HTTPException:
            raise
        except Exception as e:
            LOGGER.warning("[CF-PROXY] parts resolve failed: %s", e)
            raise HTTPException(status_code=404, detail="Split part not found")
        first_fid = fids[0]
        resolved = [_part_dict(f) for f in fids]

        key_entry, primary_dc = await _mint_key_entry(chosen_client)

        mime = (
            getattr(first_fid, "mime_type", "")
            or mimetypes.guess_type(decoded_name)[0]
            or "application/octet-stream"
        )
        payload = {
            "parts": resolved,
            "mime": mime,
            "name": decoded_name,
            "primaryDc": int(primary_dc),
            "keys": [key_entry],
        }
    else:
        msg_id = decoded.get("msg_id")
        chat_id = decoded.get("chat_id")
        if not msg_id or not chat_id:
            raise HTTPException(status_code=400, detail="Missing id")

        chat_id = int(f"-100{str(chat_id).replace('-100', '')}")
        msg_id = int(msg_id)

        try:
            chosen_client, file_id = await _resolve_file_id(chat_id, msg_id)
        except HTTPException as e:
            LOGGER.warning("[CF-PROXY] resolve failed chat=%s msg=%s: %s", chat_id, msg_id, e)
            raise HTTPException(status_code=404, detail="File not found")

        if not file_id.file_size or file_id.file_size <= 0:
            raise HTTPException(status_code=400, detail="File has no size")

        key_entry, primary_dc = await _mint_key_entry(chosen_client)

        file_name = getattr(file_id, "file_name", "") or "video.mkv"
        mime_type = (
            getattr(file_id, "mime_type", "")
            or mimetypes.guess_type(file_name)[0]
            or "application/octet-stream"
        )
        payload = {
            "id": str(file_id.media_id),
            "hash": str(file_id.access_hash),
            "ref": base64.b64encode(file_id.file_reference).decode(),
            "dc": int(file_id.dc_id),
            "size": int(file_id.file_size),
            "mime": mime_type,
            "name": file_name,
            "primaryDc": int(primary_dc),
            "keys": [key_entry],
        }

    try:
        playback_id = await _bootstrap(payload, worker_url, secret)
    except HTTPException:
        raise
    except Exception as e:
        LOGGER.error("[CF-PROXY] bootstrap error: %s", e)
        raise HTTPException(status_code=502, detail=f"CF bootstrap error: {e}")

    _playback_cache[id] = (time.time() + _PLAYBACK_TTL, playback_id)
    return RedirectResponse(
        url=f"{worker_url.rstrip('/')}/stream/{playback_id}", status_code=307
    )
