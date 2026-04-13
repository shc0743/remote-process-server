from __future__ import annotations

import os
import struct
from typing import Optional, Tuple

from rmpsm_protocol import HEADER, MAX_LEN, VERSION, bytes_to_u64

MAGIC = 0x961F132BDDDC19B9
MAGIC_BYTES = struct.pack("<Q", MAGIC)


class ServerPacketReader:
    def __init__(self, fd: int):
        self.fd = fd
        self.buf = bytearray()

    def _fill(self, n: int) -> bool:
        while len(self.buf) < n:
            try:
                chunk = os.read(self.fd, 65536)
            except InterruptedError:
                continue
            except OSError:
                return False

            if not chunk:
                return False
            self.buf.extend(chunk)
        return True

    def _resync(self) -> None:
        idx = self.buf.find(MAGIC_BYTES, 1)
        if idx >= 0:
            if idx > 0:
                del self.buf[:idx]
            return

        keep = len(MAGIC_BYTES) - 1
        if len(self.buf) > keep:
            del self.buf[:-keep]

    def read_packet(self) -> Optional[Tuple[int, int, int, int, int, bytes]]:
        while True:
            if not self._fill(HEADER.size):
                return None

            if self.buf[:8] != MAGIC_BYTES or bytes_to_u64(self.buf[8:16]) != VERSION:
                self._resync()
                continue

            header = bytes(self.buf[:HEADER.size])
            del self.buf[:HEADER.size]

            magic, version, ptype, flags, request_id, task_id, seq, ack, length = HEADER.unpack(header)
            if magic != MAGIC or version != VERSION:
                self._resync()
                continue
            if length > MAX_LEN:
                self._resync()
                continue

            if not self._fill(int(length)):
                return None

            payload = bytes(self.buf[:length])
            del self.buf[:length]
            return int(ptype), int(request_id), int(task_id), int(seq), int(ack), payload
