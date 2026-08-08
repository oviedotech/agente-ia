"""Endpoint interno para recibir mensajes reenviados por el CRM.

El CRM (Next.js) recibe el webhook de Meta, normaliza el payload, y reenvía
una copia estructurada a Nea por este endpoint. La autenticación es un secreto
compartido en el header X-Internal-Secret (sin HMAC de Meta — no aplica).

El pipeline downstream (dedup, coalesce, turno, LLM) es idéntico al que usa
el webhook directo de Meta.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import canonical_identity
from app.state import AppContext, InboundMessage

logger = logging.getLogger("nea.internal")

router = APIRouter(prefix="/internal")

_bg_tasks: set[asyncio.Task[None]] = set()


class CrmInboundPayload(BaseModel):
    """Payload normalizado que envía el CRM."""

    tenant_id: str
    wa_identity: str
    profile_name: str | None = None
    type: str = "text"
    text: str | None = None
    media_id: str | None = None
    timestamp_ms: int = 0
    # Campos opcionales para multimedia y extensiones futuras
    media_mime: str | None = None
    media_filename: str | None = None
    media_caption: str | None = None
    referral_headline: str | None = None
    wa_message_id: str | None = None


@router.post("/inbound")
async def crm_inbound(request: Request) -> Any:
    """Recibe un mensaje normalizado del CRM. Responde < 1 s siempre."""
    ctx: AppContext = request.app.state.ctx

    # --- Autenticación por secreto compartido ---
    secret = ctx.settings.internal_crm_secret
    if not secret:
        logger.error("INTERNAL_CRM_SECRET no configurado — rechazando request")
        return JSONResponse({"error": "endpoint no configurado"}, status_code=503)

    provided = request.headers.get("x-internal-secret") or ""
    if provided != secret:
        logger.warning("internal/inbound: secreto inválido — 401")
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # --- Parseo del body ---
    try:
        body = await request.json()
        payload = CrmInboundPayload(**body)
    except Exception as exc:
        logger.warning("internal/inbound: payload inválido — %s", exc)
        return JSONResponse({"error": "invalid payload"}, status_code=422)

    # --- Respuesta inmediata, procesamiento en background ---
    task = asyncio.create_task(_process_crm_inbound(ctx, payload))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return {"status": "accepted"}


async def _process_crm_inbound(ctx: AppContext, payload: CrmInboundPayload) -> None:
    """Pipeline compartido: dedup → coalesce → turno."""
    try:
        identity = canonical_identity(payload.wa_identity)
        if not identity:
            logger.warning("internal/inbound: wa_identity vacío — descartado")
            return

        # Generar wa_message_id sintético si el CRM no lo envía (para dedup)
        wa_message_id = payload.wa_message_id or f"crm_{uuid.uuid4().hex}"

        msg = InboundMessage(
            wa_message_id=wa_message_id,
            identity=identity,
            type=payload.type,
            text=payload.text,
            profile_name=payload.profile_name,
            media_id=payload.media_id,
            media_mime=payload.media_mime,
            media_filename=payload.media_filename,
            media_caption=payload.media_caption,
            referral_headline=payload.referral_headline,
        )

        # Dedup
        if msg.wa_message_id:
            fresh = await ctx.store.mark_processed(msg.wa_message_id)
            if not fresh:
                logger.info("internal dedup: %s ya procesado", msg.wa_message_id)
                return

        # Coalesce (misma lógica que el webhook de Meta)
        if ctx.coalescer is None:
            logger.error("coalescer no inicializado — mensaje descartado")
            return
        ctx.coalescer.add(identity, msg)

        # Typing temprano
        task = asyncio.create_task(_early_typing(ctx, identity))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)

    except Exception:
        logger.exception("internal/inbound: fallo procesando mensaje")


async def _early_typing(ctx: AppContext, identity: str) -> None:
    """Marca 'escribiendo…' — réplica de webhook._early_typing."""
    try:
        await asyncio.sleep(ctx.settings.typing_delay_seconds)
        allowed = ctx.settings.allowed_identities
        if allowed and canonical_identity(identity) not in allowed:
            return
        conv = await ctx.store.get_or_create_conversation(identity)
        if not conv.crm_conversation_id:
            return
        await ctx.crm.post_typing(str(conv.crm_conversation_id))
    except Exception as exc:
        logger.debug("typing interno de %s falló (%s)", identity, exc)
