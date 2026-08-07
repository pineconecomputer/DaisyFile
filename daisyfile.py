#!/usr/bin/env python3
"""
daisyfile.py — TCP file server for DaisyOne.

Serves files to DaisyOS over WiFi. Speaks the framed binary protocol
that DaisyOS sends out via comm_messages.cpp, carried over a TCP socket
instead of a UART.

ARCHITECTURE
============

A `serve()` loop accepts TCP connections and spawns a daemon thread
per client. Each client gets its own `Client` instance with private
state:

    * file_dir   — the root directory the server is sandboxing files in
    * cwd        — per-client working directory relative to file_dir
                   (manipulated by CHDIR / MKDIR)
    * _files[]   — channel → open file handle for sequential I/O
    * _modes[]   — channel → mode byte (0=R, 1=W, 2=A)

PROTOCOL
========

Every request is a framed message:

    [SOP=0x5C] [CMD] [PAYLOAD_LEN] [PAYLOAD ...] [CHECKSUM]

where CHECKSUM is the two's complement of (SOP, CMD, PAYLOAD_LEN, PAYLOAD)
so the entire frame sums to 0 mod 256.

Command opcodes are the `CMD_*` constants below. Most commands respond
with one of:

    * "end\\r\\n"                       — silent success (CHDIR/MKDIR/
                                          DEL/REN/COPY/LOAD trailer)
    * `ACK_BYTE` (0x06) / `NAK_BYTE`   — single-byte ack for FOPEN/FCLOSE/
                                          FPRINT/FPUT/FSEEK
    * 4-byte big-endian count           — FBYTES
    * 2-byte (ok, byte) pair            — FGET (ok=0 means EOF)
    * one or more `print "..."` lines  — error/info messages that the
                                          BASIC client routes through
                                          `_BasicExecute` so they render
                                          as ?ERRORs to the user

CATALOG, LOAD, and SAVE are streaming commands with their own multi-line
formats; see the cmd_* method docstrings.

PATH SAFETY
===========

Filenames may contain path components (e.g. "subdir/file.bas") and the
client can navigate via CHDIR. `safe_path` resolves any filename against
(file_dir / cwd) and verifies the result stays inside file_dir. Paths
that resolve outside file_dir (via `..` or absolute) raise ValueError,
which handlers translate to ?FILE NOT FOUND or ?MKDIR ERROR.

USAGE
=====

    python daisyfile.py [--host HOST] [--port PORT] [--dir DIR] [--verbose]

Requires Python 3.9+.
"""

import argparse
import logging
import shutil
import socket
import threading
from pathlib import Path

# ── Protocol constants ────────────────────────────────────────────────────────

SOP          = 0x5C
ACK_BYTE     = 0x06
NAK_BYTE     = 0x15
CMD_CATALOG  = 0x0A
CMD_LOAD     = 0x0B
CMD_SAVE     = 0x0C
CMD_LOADCHAR = 0x0D
CMD_SAVECHAR = 0x0E
CMD_FOPEN    = 0x0F
CMD_FCLOSE   = 0x10
CMD_FPRINT   = 0x11
CMD_FINPUT   = 0x12
CMD_FGET     = 0x13
CMD_FPUT     = 0x14
CMD_FSEEK    = 0x15
CMD_FBYTES   = 0x16
CMD_DEL      = 0x17
CMD_REN      = 0x18
CMD_COPY     = 0x19
CMD_CHDIR    = 0x1A
CMD_MKDIR    = 0x1B
CMD_FREWIND  = 0x1C

LOAD_BATCH   = 8   # program lines per ACK on LOAD (server → DaisyOS)
SAVE_BATCH   = 1   # per-line ACK on SAVE; Zimodem buffers bursts so batching hangs

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)-21s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daisyfile")


# ── Checksum ──────────────────────────────────────────────────────────────────

def calc_checksum(data: bytes) -> int:
    """Two's-complement checksum matching DaisyOS calcchecksum()."""
    return (~sum(data) + 1) & 0xFF


# ── Client ────────────────────────────────────────────────────────────────────

class Client:
    """Handles one DaisyOS TCP connection on its own thread."""

    def __init__(self, conn: socket.socket, addr: tuple, file_dir: Path) -> None:
        self.conn     = conn
        self.addr     = addr
        self.file_dir = file_dir
        self.cwd      = Path(".")    # current directory relative to file_dir root
        self._buf     = bytearray()  # unified read buffer — all recv paths use this
        self._files: dict = {}       # open file channels: {channel_int: file_object}
        self._modes: dict = {}       # channel → mode byte (0=r, 1=w, 2=a)

    # ── Buffered receive ──────────────────────────────────────────────────

    def _fill(self, n: int) -> None:
        """Pull from socket until _buf has at least n bytes."""
        while len(self._buf) < n:
            chunk = self.conn.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed")
            self._buf += chunk

    def recv_exact(self, n: int) -> bytes:
        """Read exactly n bytes."""
        self._fill(n)
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def recv_byte(self) -> int:
        return self.recv_exact(1)[0]

    def recv_line(self) -> str:
        """Read a \\n-terminated line, strip \\r. Shares _buf with recv_exact."""
        while b"\n" not in self._buf:
            chunk = self.conn.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed")
            self._buf += chunk
        idx  = self._buf.index(b"\n")
        line = bytes(self._buf[:idx]).decode("latin-1").rstrip("\r")
        del self._buf[:idx + 1]
        return line

    def recv_ack(self) -> None:
        b = self.recv_byte()
        if b != ACK_BYTE:
            log.warning("expected ACK 0x06, got 0x%02x", b)

    # ── Send helpers ──────────────────────────────────────────────────────

    def send_line(self, text: str) -> None:
        self.conn.sendall((text + "\r\n").encode("latin-1"))

    def send_ack(self) -> None:
        self.conn.sendall(bytes([ACK_BYTE]))

    def send_ready(self) -> None:
        """'.' ready signal — DaisyOS waits for this before sending data."""
        self.conn.sendall(b".\r\n")

    # ── Frame reader ──────────────────────────────────────────────────────

    def read_frame(self) -> tuple:
        """Block until a valid, checksum-correct frame arrives."""
        while True:
            b = self.recv_byte()
            if b != SOP:
                log.debug("skipping stray byte 0x%02x", b)
                continue
            cmd     = self.recv_byte()
            plen    = self.recv_byte()
            payload = self.recv_exact(plen) if plen else b""
            cs      = self.recv_byte()
            frame   = bytes([SOP, cmd, plen]) + payload
            if calc_checksum(frame) == cs:
                log.debug("frame cmd=0x%02x plen=%d", cmd, plen)
                return cmd, payload
            log.warning("checksum mismatch cmd=0x%02x — discarding frame", cmd)

    # ── Path helpers ──────────────────────────────────────────────────────

    def safe_path(self, filename: str) -> Path:
        """Resolve `filename` against (file_dir / cwd) and verify it
        stays inside file_dir. Raises ValueError on path escape.
        """
        target = (self.file_dir / self.cwd / filename).resolve()
        root   = self.file_dir.resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise ValueError(f"path {filename!r} escapes file root")
        return target

    def cwd_abs(self) -> Path:
        """Absolute path of current working directory."""
        return (self.file_dir / self.cwd).resolve()

    def cwd_display(self) -> str:
        """Current directory as a display string relative to file root (e.g. '/' or '/games')."""
        if self.cwd == Path("."):
            return "/"
        return "/" + str(self.cwd).replace("\\", "/")

    @staticmethod
    def parse_overwrite(raw: str) -> tuple:
        """-filename → (True, filename); filename → (False, filename)."""
        if raw.startswith("-"):
            return True, raw[1:]
        return False, raw

    @staticmethod
    def is_end_line(line: str) -> bool:
        """Match DaisyOS IsEndLine(): bare 'end' (case-insensitive), no leading digit."""
        s = line.strip()
        return bool(s) and not s[0].isdigit() and s.lower() == "end"

    # ── Command handlers ──────────────────────────────────────────────────

    def cmd_catalog(self) -> None:
        """CATALOG handler. Sends a directory listing.

        Sequence:
          1. ``\\x16{cwd_display()}`` — path header (chr 22 prefix)
          2. For each subdirectory:
                ``\\x12{name}/`` (chr 18 prefix), then ``<DIR>``
          3. For each file:
                ``\\x17{name}`` (chr 23 prefix), then the size in bytes
          4. ``end``
        Each line is ACKed by the client.
        """
        log.info("CATALOG cwd=%s", self.cwd_display())
        cwd = self.cwd_abs()
        try:
            all_entries = sorted(cwd.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError as exc:
            log.error("CATALOG: %s", exc)
            all_entries = []

        dirs  = [e for e in all_entries if e.is_dir()  and not e.name.startswith("_")]
        files = [e for e in all_entries if e.is_file() and not e.name.startswith("_")]

        # Send current directory path as a header (chr(22) = 0x16 marker)
        self.send_line(f"\x16{self.cwd_display()}")
        self.recv_ack()

        # Subdirectories: chr(18) = 0x12 marker, size shown as <DIR>
        for entry in dirs:
            self.send_line(f"\x12{entry.name}/")
            self.recv_ack()
            self.send_line("<DIR>")
            self.recv_ack()

        # Files: chr(23) = 0x17 marker, size in bytes
        for entry in files:
            self.send_line(f"\x17{entry.name}")
            self.recv_ack()
            self.send_line(str(entry.stat().st_size))
            self.recv_ack()

        self.send_line("end")
        log.info("CATALOG: %d dirs, %d files", len(dirs), len(files))

    def cmd_load(self, payload: bytes) -> None:
        """LOAD handler. Streams a BASIC source file to the client.

        Sequence: ``print "FOUND"`` (or ``?FILE NOT FOUND ERROR``)
        + per-line ACK; then source lines, ACKed every LOAD_BATCH
        lines; then ``end``.
        """
        filename = payload.decode("latin-1").rstrip("\x00")
        log.info("LOAD %s", filename)
        try:
            path = self.safe_path(filename)
        except ValueError:
            self.send_line('print "?FILE NOT FOUND ERROR"')
            self.recv_ack()
            self.send_line("end")
            return

        if not path.is_file():
            log.warning("LOAD: not found — %s", filename)
            self.send_line('print "?FILE NOT FOUND ERROR"')
            self.recv_ack()
            self.send_line("end")
            return

        self.send_line('print "FOUND"')
        self.recv_ack()  # handshake ACK — always individual

        count = 0
        with open(path, "r", encoding="latin-1", newline="") as f:
            for raw in f:
                self.send_line(raw.rstrip("\r\n"))
                count += 1
                if count % LOAD_BATCH == 0:
                    self.recv_ack()  # batch ACK every LOAD_BATCH lines

        self.send_line("end")
        log.info("LOAD %s: %d lines", filename, count)

    def cmd_save(self, payload: bytes) -> None:
        """SAVE handler. Receives a BASIC source file from the client.

        Sequence: open target (rejecting if exists and no ``-`` prefix),
        send ``.`` ready, receive lines until ``end`` (with ``.`` ACK
        every SAVE_BATCH lines), write to disk, reply ``print "Saved"``.
        """
        raw = payload.decode("latin-1").rstrip("\x00")
        overwrite, filename = self.parse_overwrite(raw)
        log.info("SAVE %s (overwrite=%s)", filename, overwrite)
        try:
            path = self.safe_path(filename)
        except ValueError:
            self.send_line('print "?SAVE ERROR"')
            return

        if path.exists():
            if not overwrite:
                log.warning("SAVE: file exists — %s", filename)
                self.send_line('print "?FILE EXISTS ERROR"')
                return
            path.unlink()

        try:
            f = open(path, "w", encoding="latin-1", newline="")
        except OSError as exc:
            log.error("SAVE: open failed: %s", exc)
            self.send_line('print "?SAVE ERROR"')
            return

        self.send_ready()
        lines   = []
        success = True
        count   = 0
        try:
            while True:
                line = self.recv_line()
                if self.is_end_line(line):
                    break
                lines.append(line + "\r\n")
                count += 1
                if count % SAVE_BATCH == 0:
                    self.send_ready()    # ".\r\n" ACK every SAVE_BATCH lines
        except Exception as exc:
            log.error("SAVE: receive error: %s", exc)
            success = False

        try:
            if success:
                f.writelines(lines)      # single write pass, no per-line disk overhead
        except OSError as exc:
            log.error("SAVE: write error: %s", exc)
            success = False
        finally:
            f.close()

        count = len(lines)
        if success:
            self.send_line('print "Saved"')
            log.info("SAVE %s: %d lines", filename, count)
        else:
            self.send_line('print "?SAVE ERROR"')

    def cmd_loadchar(self, payload: bytes) -> None:
        """LOADCHAR. Streams 8-byte character bitmaps to the client.

        Payload: ``[type][startIdx][filename]``. Each 8-byte chunk is
        sent then ACKed; loops until end-of-file or 256-startIdx chars.
        """
        if len(payload) < 3:
            log.warning("LOADCHAR: payload too short (%d bytes)", len(payload))
            return
        # payload[0] = type (0=char, 1=gfx) — informational only, not used server-side
        start_idx = payload[1]
        filename  = payload[2:].decode("latin-1").rstrip("\x00")
        log.info("LOADCHAR %s start=%d", filename, start_idx)
        try:
            path = self.safe_path(filename)
        except ValueError:
            log.error("LOADCHAR: path escape — %s", filename)
            return

        if not path.is_file():
            log.error("LOADCHAR: not found — %s", filename)
            return

        sent = 0
        with open(path, "rb") as f:
            for _ in range(256 - start_idx):
                chunk = f.read(8)
                if not chunk:
                    break
                if len(chunk) < 8:
                    chunk = chunk.ljust(8, b"\x00")
                self.conn.sendall(chunk)
                self.recv_ack()
                sent += 1

        log.info("LOADCHAR %s: %d chars", filename, sent)

    def cmd_savechar(self, payload: bytes) -> None:
        """SAVECHAR. Receives 8-byte character bitmaps and writes them
        to disk. Mirrors `cmd_loadchar` in the opposite direction.
        """
        if len(payload) < 3:
            log.warning("SAVECHAR: payload too short (%d bytes)", len(payload))
            self.send_line('print "?SAVECHAR ERROR"')
            return

        # payload[0]=type, payload[1]=startIdx, payload[2:]=filename
        start_idx = payload[1]
        raw       = payload[2:].decode("latin-1").rstrip("\x00")
        overwrite, filename = self.parse_overwrite(raw)
        log.info("SAVECHAR %s start=%d (overwrite=%s)", filename, start_idx, overwrite)
        try:
            path = self.safe_path(filename)
        except ValueError:
            self.send_line('print "?SAVECHAR ERROR"')
            return

        if path.exists():
            if not overwrite:
                log.warning("SAVECHAR: file exists — %s", filename)
                self.send_line('print "?FILE EXISTS ERROR"')
                return
            path.unlink()

        try:
            f = open(path, "wb")
        except OSError as exc:
            log.error("SAVECHAR: open failed: %s", exc)
            self.send_line('print "?SAVECHAR ERROR"')
            return

        self.send_ready()
        chunks  = []
        success = True
        try:
            for _ in range(256 - start_idx):
                chunk = self.recv_exact(8)
                self.send_ack()          # ACK before disk write
                chunks.append(chunk)
        except Exception as exc:
            log.error("SAVECHAR: receive error: %s", exc)
            success = False

        try:
            if success:
                f.write(b"".join(chunks))
        except OSError as exc:
            log.error("SAVECHAR: write error: %s", exc)
            success = False
        finally:
            f.close()

        received = len(chunks)
        if success:
            self.send_line('print "Saved"')
            log.info("SAVECHAR %s: %d chars", filename, received)
        else:
            self.send_line('print "?SAVECHAR ERROR"')

    # ── Sequential file I/O handlers ─────────────────────────────────────

    def cmd_fopen(self, payload: bytes) -> None:
        """FOPEN ch, mode, filename. Open file on `ch` (0–4) in
        binary mode (rb/wb/ab). Replies ACK or NAK.
        """
        if len(payload) < 3:
            log.warning("FOPEN: payload too short")
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        channel   = payload[0]
        mode_byte = payload[1]
        filename  = payload[2:].decode("latin-1").rstrip("\x00")
        log.info("FOPEN ch=%d mode=%d file=%s", channel, mode_byte, filename)
        if channel > 4:
            log.warning("FOPEN: invalid channel %d", channel)
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        if not filename:
            log.warning("FOPEN: empty filename")
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        if channel in self._files:
            try: self._files[channel].close()
            except OSError: pass
            del self._files[channel]
            self._modes.pop(channel, None)
        try:
            path = self.safe_path(filename)
        except ValueError:
            log.warning("FOPEN: path escape — %s", filename)
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        modes = {0: "rb", 1: "wb", 2: "ab"}
        mode_str = modes.get(mode_byte, "rb")
        try:
            self._files[channel] = open(path, mode_str)
            self._modes[channel] = mode_byte
            self.send_ack()
        except OSError as exc:
            log.error("FOPEN: %s", exc)
            self.conn.sendall(bytes([NAK_BYTE]))

    def cmd_fclose(self, payload: bytes) -> None:
        """FCLOSE ch. Close a channel; idempotent. Always ACKs."""
        channel = payload[0] if payload else 0
        log.info("FCLOSE ch=%d", channel)
        if channel in self._files:
            try: self._files[channel].close()
            except OSError: pass
            del self._files[channel]
            self._modes.pop(channel, None)
        self.send_ack()

    def cmd_fprint(self, payload: bytes) -> None:
        """FPRINT ch, text. Append `text + "\\n"` to a write-mode
        channel. Returns ACK or NAK.
        """
        if not payload:
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        channel = payload[0]
        text    = payload[1:].decode("latin-1")
        if channel not in self._files:
            log.warning("FPRINT: ch=%d not open", channel)
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        if self._modes.get(channel, 1) == 0:
            log.warning("FPRINT: ch=%d opened read-only", channel)
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        try:
            self._files[channel].write((text + "\n").encode("latin-1"))
            self._files[channel].flush()
            self.send_ack()
        except OSError as exc:
            log.error("FPRINT: %s", exc)
            self.conn.sendall(bytes([NAK_BYTE]))

    def cmd_finput(self, payload: bytes) -> None:
        """FINPUT ch. Read one line from a read-mode channel and send
        it back. EOF returns an empty line.
        """
        channel = payload[0] if payload else 0
        if channel not in self._files:
            log.warning("FINPUT: ch=%d not open", channel)
            self.send_line("")
            return
        if self._modes.get(channel, 0) != 0:
            log.warning("FINPUT: ch=%d not opened for reading", channel)
            self.send_line("")
            return
        try:
            raw = self._files[channel].readline()
            if not raw:
                self.send_line("")   # EOF sentinel: empty line
            else:
                self.send_line(raw.rstrip(b"\r\n").decode("latin-1"))
        except OSError as exc:
            log.error("FINPUT: %s", exc)
            self.send_line("")

    def cmd_fget(self, payload: bytes) -> None:
        """FGET ch. Reply ``[ok, byte]`` — ok=1 with the byte on success,
        ok=0 with byte=0 on EOF.
        """
        channel = payload[0] if payload else 0
        if channel not in self._files:
            log.warning("FGET: ch=%d not open", channel)
            self.conn.sendall(bytes([0, 0]))
            return
        if self._modes.get(channel, 0) != 0:
            log.warning("FGET: ch=%d not opened for reading", channel)
            self.conn.sendall(bytes([0, 0]))
            return
        try:
            ch = self._files[channel].read(1)
            if ch:
                self.conn.sendall(bytes([1, ch[0]]))  # ok + byte
            else:
                self.conn.sendall(bytes([0, 0]))       # EOF
        except OSError as exc:
            log.error("FGET: %s", exc)
            self.conn.sendall(bytes([0, 0]))

    def cmd_fput(self, payload: bytes) -> None:
        """FPUT ch, byte. Write one byte. ACKs or NAKs."""
        if len(payload) < 2:
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        channel   = payload[0]
        byte_val  = payload[1]
        if channel not in self._files:
            log.warning("FPUT: ch=%d not open", channel)
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        if self._modes.get(channel, 1) == 0:
            log.warning("FPUT: ch=%d opened read-only", channel)
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        try:
            self._files[channel].write(bytes([byte_val]))
            self._files[channel].flush()
            self.send_ack()
        except OSError as exc:
            log.error("FPUT: %s", exc)
            self.conn.sendall(bytes([NAK_BYTE]))

    def cmd_fseek(self, payload: bytes) -> None:
        """FSEEK ch, delta. Relative signed-int32 seek (whence=current)."""
        if len(payload) < 5:
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        channel = payload[0]
        raw     = (payload[1] << 24) | (payload[2] << 16) | (payload[3] << 8) | payload[4]
        offset  = raw if raw < 0x80000000 else raw - 0x100000000  # uint32 → int32
        if channel not in self._files:
            log.warning("FSEEK: ch=%d not open", channel)
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        try:
            self._files[channel].seek(offset, 1)
            self.send_ack()
        except OSError as exc:
            log.error("FSEEK: %s", exc)
            self.conn.sendall(bytes([NAK_BYTE]))

    def cmd_frewind(self, payload: bytes) -> None:
        """FREWIND ch. Seek the channel back to byte 0."""
        if not payload:
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        channel = payload[0]
        if channel not in self._files:
            log.warning("FREWIND: ch=%d not open", channel)
            self.conn.sendall(bytes([NAK_BYTE]))
            return
        try:
            self._files[channel].seek(0)
            self.send_ack()
        except OSError as exc:
            log.error("FREWIND: %s", exc)
            self.conn.sendall(bytes([NAK_BYTE]))

    def cmd_fbytes(self, payload: bytes) -> None:
        """FBYTES ch. Reply with a 4-byte big-endian count of bytes
        remaining (read-mode channels only).
        """
        channel = payload[0] if payload else 0
        count   = 0
        if channel in self._files and self._modes.get(channel, 0) == 0:
            try:
                f   = self._files[channel]
                pos = f.tell()
                f.seek(0, 2)
                end = f.tell()
                f.seek(pos)
                count = max(0, end - pos)
            except OSError as exc:
                log.error("FBYTES: %s", exc)
        self.conn.sendall(count.to_bytes(4, "big"))

    def cmd_del(self, payload: bytes) -> None:
        """DEL filename. Unlink. Replies ``end`` on success or
        ``print "?...ERROR"`` on failure.
        """
        filename = payload.decode("latin-1").rstrip("\x00")
        log.info("DEL %s", filename)
        if not filename:
            self.send_line('print "?MISSING FILE NAME ERROR"')
            return
        try:
            path = self.safe_path(filename)
        except ValueError:
            self.send_line('print "?FILE NOT FOUND ERROR"')
            return
        if not path.is_file():
            self.send_line('print "?FILE NOT FOUND ERROR"')
            return
        try:
            path.unlink()
            self.send_line("end")
            log.info("DEL %s: deleted", filename)
        except OSError as exc:
            log.error("DEL: %s", exc)
            self.send_line('print "?DELETE ERROR"')

    def cmd_ren(self, payload: bytes) -> None:
        """REN old, new. Rename. Payload: ``[old_len][old][new]``."""
        if not payload:
            self.send_line('print "?MISSING FILE NAME ERROR"')
            return
        old_len = payload[0]
        if len(payload) < 1 + old_len:
            self.send_line('print "?RENAME ERROR"')
            return
        oldname = payload[1:1 + old_len].decode("latin-1").rstrip("\x00")
        newname = payload[1 + old_len:].decode("latin-1").rstrip("\x00")
        log.info("REN %s -> %s", oldname, newname)
        if not oldname or not newname:
            self.send_line('print "?MISSING FILE NAME ERROR"')
            return
        try:
            old_path = self.safe_path(oldname)
            new_path = self.safe_path(newname)
        except ValueError:
            self.send_line('print "?RENAME ERROR"')
            return
        if not old_path.is_file():
            self.send_line('print "?FILE NOT FOUND ERROR"')
            return
        if new_path.exists():
            self.send_line('print "?FILE EXISTS ERROR"')
            return
        try:
            old_path.rename(new_path)
            self.send_line("end")
            log.info("REN %s -> %s: done", oldname, newname)
        except OSError as exc:
            log.error("REN: %s", exc)
            self.send_line('print "?RENAME ERROR"')

    def cmd_copy(self, payload: bytes) -> None:
        """COPY src, dst. Copy via shutil.copy2 (preserves metadata).
        Payload: ``[src_len][src][dst]``.
        """
        if not payload:
            self.send_line('print "?MISSING FILE NAME ERROR"')
            return
        src_len = payload[0]
        if len(payload) < 1 + src_len:
            self.send_line('print "?COPY ERROR"')
            return
        srcname = payload[1:1 + src_len].decode("latin-1").rstrip("\x00")
        dstname = payload[1 + src_len:].decode("latin-1").rstrip("\x00")
        log.info("COPY %s -> %s", srcname, dstname)
        if not srcname or not dstname:
            self.send_line('print "?MISSING FILE NAME ERROR"')
            return
        try:
            src_path = self.safe_path(srcname)
            dst_path = self.safe_path(dstname)
        except ValueError:
            self.send_line('print "?COPY ERROR"')
            return
        if not src_path.is_file():
            self.send_line('print "?FILE NOT FOUND ERROR"')
            return
        if dst_path.exists():
            self.send_line('print "?FILE EXISTS ERROR"')
            return
        try:
            shutil.copy2(src_path, dst_path)
            self.send_line("end")
            log.info("COPY %s -> %s: done", srcname, dstname)
        except OSError as exc:
            log.error("COPY: %s", exc)
            self.send_line('print "?COPY ERROR"')

    def cmd_chdir(self, payload: bytes) -> None:
        """CHDIR path. Change per-connection working directory.

        Accepts ``/`` (root), ``/abs/path``, ``relative``, or ``..``.
        Rejects any path that resolves outside file_dir.
        """
        dirname = payload.decode("latin-1").rstrip("\x00")
        log.info("CHDIR %s (cwd=%s)", dirname, self.cwd_display())
        if not dirname:
            self.send_line('print "?MISSING FILE NAME ERROR"')
            return

        root = self.file_dir.resolve()

        # "/" means jump to the file root
        if dirname == "/":
            self.cwd = Path(".")
            self.send_line("end")
            log.info("CHDIR: now at /")
            return

        # Build candidate absolute path
        if dirname.startswith("/"):
            # Absolute path from root (strip leading slash)
            candidate = (self.file_dir / dirname[1:]).resolve()
        else:
            candidate = (self.file_dir / self.cwd / dirname).resolve()

        # Reject paths that escape the file root
        try:
            new_rel = candidate.relative_to(root)
        except ValueError:
            self.send_line('print "?DIR NOT FOUND ERROR"')
            return

        if not candidate.is_dir():
            self.send_line('print "?DIR NOT FOUND ERROR"')
            return

        self.cwd = new_rel
        self.send_line("end")
        log.info("CHDIR: now at %s", self.cwd_display())

    def cmd_mkdir(self, payload: bytes) -> None:
        """MKDIR path. Create a directory (with parent intermediates).
        Replies ``?DIR EXISTS ERROR`` if the target already exists.
        """
        dirname = payload.decode("latin-1").rstrip("\x00")
        log.info("MKDIR %s (cwd=%s)", dirname, self.cwd_display())
        if not dirname:
            self.send_line('print "?MISSING FILE NAME ERROR"')
            return
        try:
            target = self.safe_path(dirname)
        except ValueError:
            self.send_line('print "?MKDIR ERROR"')
            return
        if target.exists():
            self.send_line('print "?DIR EXISTS ERROR"')
            return
        try:
            target.mkdir(parents=True, exist_ok=False)
            self.send_line("end")
            log.info("MKDIR %s: created", dirname)
        except OSError as exc:
            log.error("MKDIR: %s", exc)
            self.send_line('print "?MKDIR ERROR"')

    # ── Dispatch loop ─────────────────────────────────────────────────────

    def run(self) -> None:
        """Per-client thread entry point. Reads frames in a loop and
        dispatches each to the matching ``cmd_*`` handler. Closes all
        open files on disconnect or error.
        """
        log.info("connected from %s:%d", *self.addr)
        try:
            while True:
                cmd, payload = self.read_frame()
                if   cmd == CMD_CATALOG:  self.cmd_catalog()
                elif cmd == CMD_LOAD:     self.cmd_load(payload)
                elif cmd == CMD_SAVE:     self.cmd_save(payload)
                elif cmd == CMD_LOADCHAR: self.cmd_loadchar(payload)
                elif cmd == CMD_SAVECHAR: self.cmd_savechar(payload)
                elif cmd == CMD_FOPEN:    self.cmd_fopen(payload)
                elif cmd == CMD_FCLOSE:   self.cmd_fclose(payload)
                elif cmd == CMD_FPRINT:   self.cmd_fprint(payload)
                elif cmd == CMD_FINPUT:   self.cmd_finput(payload)
                elif cmd == CMD_FGET:     self.cmd_fget(payload)
                elif cmd == CMD_FPUT:     self.cmd_fput(payload)
                elif cmd == CMD_FSEEK:    self.cmd_fseek(payload)
                elif cmd == CMD_FREWIND:  self.cmd_frewind(payload)
                elif cmd == CMD_FBYTES:   self.cmd_fbytes(payload)
                elif cmd == CMD_DEL:      self.cmd_del(payload)
                elif cmd == CMD_REN:      self.cmd_ren(payload)
                elif cmd == CMD_COPY:     self.cmd_copy(payload)
                elif cmd == CMD_CHDIR:    self.cmd_chdir(payload)
                elif cmd == CMD_MKDIR:    self.cmd_mkdir(payload)
                else:
                    log.warning("unknown cmd 0x%02x", cmd)
                    self.send_line('print "?IO COMMAND ERROR"')
                    self.send_line("end")
        except ConnectionError:
            log.info("disconnected from %s:%d", *self.addr)
        except Exception:
            log.exception("error on %s:%d", *self.addr)
        finally:
            self.conn.close()
            for f in self._files.values():
                try: f.close()
                except OSError: pass


# ── Server ────────────────────────────────────────────────────────────────────

def serve(host: str, port: int, file_dir: Path) -> None:
    """Top-level accept loop. Creates ``file_dir`` if missing and
    spawns a daemon thread per client connection.
    """
    file_dir.mkdir(parents=True, exist_ok=True)
    log.info("file directory : %s", file_dir.resolve())

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
        srv.listen(8)
        log.info("listening on   : %s:%d", host, port)
        log.info("press Ctrl+C to stop")

        try:
            while True:
                conn, addr = srv.accept()
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                client = Client(conn, addr, file_dir)
                t = threading.Thread(
                    target=client.run,
                    name=f"{addr[0]}:{addr[1]}",
                    daemon=True,
                )
                t.start()
        except KeyboardInterrupt:
            log.info("shutting down")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Argparse entry point: parse flags and call `serve`."""
    p = argparse.ArgumentParser(
        description="DaisyFile — DaisyOne TCP file server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host",    default="0.0.0.0",   metavar="HOST", help="bind address")
    p.add_argument("--port",    default=9000, type=int, metavar="PORT", help="TCP port")
    p.add_argument("--dir",     default="daisyfiles", metavar="DIR",  help="file storage directory")
    p.add_argument("--verbose", action="store_true",                   help="enable debug logging")
    args = p.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    serve(args.host, args.port, Path(args.dir))


if __name__ == "__main__":
    main()
