"""
TCP client that speaks the Teltonika device side of Codec 8E.
"""
from __future__ import annotations

import logging
import socket
import struct
import time
from typing import List, Optional

from codec8e import SimRecord, build_avl_packet, build_imei_handshake

log = logging.getLogger("sim.tcp")


class TeltonikaClient:
    def __init__(self, host: str, port: int, imei: str) -> None:
        self.host = host
        self.port = port
        self.imei = imei
        self._sock: Optional[socket.socket] = None

    def connect(self, timeout: float = 10.0) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(timeout)
        self._sock = sock

        sock.sendall(build_imei_handshake(self.imei))
        reply = self._recv_exact(1)
        if reply != b"\x01":
            raise RuntimeError(f"IMEI rejected by server (got {reply!r})")
        log.info("Connected to %s:%d as IMEI %s", self.host, self.port, self.imei)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "TeltonikaClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def send_records(self, records: List[SimRecord], retries: int = 2) -> int:
        if not records:
            return 0
        packet = build_avl_packet(records)
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                if self._sock is None:
                    self.connect()
                assert self._sock is not None
                self._sock.sendall(packet)
                ack = self._recv_exact(4)
                (count,) = struct.unpack(">i", ack)
                if count != len(records):
                    log.warning("ACK count %d != sent %d", count, len(records))
                return count
            except (OSError, TimeoutError, RuntimeError) as e:
                last_err = e
                log.warning("Send failed (attempt %d): %s — reconnecting", attempt + 1, e)
                self.close()
                time.sleep(0.5)
        raise RuntimeError(f"Failed to send AVL packet: {last_err}")

    def send_one(self, record: SimRecord) -> int:
        return self.send_records([record])

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise RuntimeError("connection closed by server")
            buf += chunk
        return bytes(buf)
