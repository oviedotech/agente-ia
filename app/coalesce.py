"""Coalescencia de ráfagas: debounce por identidad.

La gente manda 3 mensajitos seguidos — se agrupan en UN turno. Cada mensaje
nuevo reinicia el timer (`COALESCE_SECONDS`, default 6 s). Al vencer, se
dispara `on_flush(identity, items)` con la ráfaga completa.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("nea.coalesce")

FlushCallback = Callable[[str, list[Any]], Awaitable[None]]


class Coalescer:
    def __init__(self, seconds: float, on_flush: FlushCallback) -> None:
        self._seconds = seconds
        self._on_flush = on_flush
        self._buffers: dict[str, list[Any]] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}

    def add(self, identity: str, item: Any) -> None:
        """Acumula un mensaje y (re)inicia el timer de la identidad."""
        self._buffers.setdefault(identity, []).append(item)
        buf_len = len(self._buffers[identity])
        timer = self._timers.get(identity)
        if timer is not None and not timer.done():
            timer.cancel()  # mensaje nuevo → el reloj vuelve a empezar
            logger.info(
                "DIAG-COALESCE: %s msg #%d — timer reiniciado (%.1fs)",
                identity, buf_len, self._seconds,
            )
        else:
            logger.info(
                "DIAG-COALESCE: %s msg #%d — timer NUEVO armado (%.1fs)",
                identity, buf_len, self._seconds,
            )
        self._timers[identity] = asyncio.create_task(self._fire(identity))

    async def _fire(self, identity: str) -> None:
        try:
            await asyncio.sleep(self._seconds)
        except asyncio.CancelledError:
            logger.info("DIAG-COALESCE: %s timer CANCELADO (ráfaga continúa)", identity)
            return  # reiniciado por un mensaje nuevo
        items = self._buffers.pop(identity, [])
        self._timers.pop(identity, None)
        if not items:
            logger.warning("DIAG-COALESCE: %s timer venció pero buffer VACÍO — no-op", identity)
            return
        logger.info(
            "DIAG-COALESCE: %s timer DISPARADO — flushing %d mensaje(s) a run_turn",
            identity, len(items),
        )
        try:
            await self._on_flush(identity, items)
            logger.info("DIAG-COALESCE: %s flush COMPLETADO exitosamente", identity)
        except Exception:
            # El turno jamás debe tumbar el loop (Constitución IV).
            logger.exception("coalesce: el turno de %s falló", identity)

    async def aclose(self) -> None:
        for timer in self._timers.values():
            timer.cancel()
        if self._timers:
            await asyncio.gather(*self._timers.values(), return_exceptions=True)
        self._timers.clear()
        self._buffers.clear()
