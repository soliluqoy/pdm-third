"""
PREDICT — Teltonika TCP listener (runs inside the app process).

One asyncio server handles every tracker connection:
  IMEI handshake → accept → AVL packet loop → hand parsed records to the
  ingest pipeline (in-process call — no broker, no serialization).

ACK policy: a packet is ACKed only after its records were fully processed.
On CRC/codec errors the frame is dropped byte-by-byte to resync and NOT
ACKed, so the store-and-forward device retransmits (at-least-once).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, List, Optional

from server.config import settings
from server.teltonika.codec8e import (
    AvlRecord,
    CodecError,
    IncompletePacket,
    build_ack,
    build_imei_reply,
    parse_avl_packet,
    parse_imei,
)

logger = logging.getLogger("predict.teltonika")

# handler(imei, records) — provided by the ingest pipeline
RecordsHandler = Callable[[str, List[AvlRecord]], Awaitable[None]]


class TeltonikaListener:
    def __init__(self, handler: RecordsHandler):
        self._handler = handler
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_device, settings.TELTONIKA_HOST, settings.TELTONIKA_PORT
        )
        logger.info(
            "Teltonika listener on %s:%d (Codec 8/8E)",
            settings.TELTONIKA_HOST, settings.TELTONIKA_PORT,
        )

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ── Per-device connection ─────────────────────────────────────────────────
    async def _handle_device(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        imei: Optional[str] = None
        try:
            # Phase 1: IMEI handshake
            buf = b""
            while True:
                chunk = await asyncio.wait_for(reader.read(256), timeout=30)
                if not chunk:
                    return
                buf += chunk
                try:
                    imei, consumed = parse_imei(buf)
                    break
                except IncompletePacket:
                    continue

            # Accept every IMEI: unregistered devices get their records dropped
            # downstream (logged once). Rejecting would make the device retry
            # forever and block its buffer.
            writer.write(build_imei_reply(True))
            await writer.drain()
            logger.info("Device connected: IMEI %s from %s", imei, peer)
            buf = buf[consumed:]

            # Phase 2: AVL packet loop
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        reader.read(4096), timeout=settings.TELTONIKA_IDLE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.info("Idle timeout, closing IMEI %s", imei)
                    return
                if not chunk:
                    return
                buf += chunk

                # Drain as many complete packets as the buffer holds
                while buf:
                    try:
                        records, pkt_len = parse_avl_packet(buf)
                    except IncompletePacket:
                        break
                    except CodecError as e:
                        logger.warning(
                            "Bad packet from IMEI %s: %s — dropping 1 byte", imei, e
                        )
                        buf = buf[1:]
                        continue

                    buf = buf[pkt_len:]
                    try:
                        await self._handler(imei, records)
                    except Exception:
                        # Do not ACK: the device keeps these records buffered
                        # and retransmits instead of losing them.
                        logger.exception(
                            "Ingest failed for IMEI %s (%d records) — not ACKing",
                            imei, len(records),
                        )
                        return

                    writer.write(build_ack(len(records)))
                    await writer.drain()
                    logger.debug("IMEI %s: %d record(s), ACK sent", imei, len(records))

        except (ConnectionResetError, BrokenPipeError):
            logger.info("Connection lost: IMEI %s (%s)", imei or "?", peer)
        except Exception:
            logger.exception("Unexpected error for IMEI %s (%s)", imei or "?", peer)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            if imei:
                logger.info("Device disconnected: IMEI %s", imei)
