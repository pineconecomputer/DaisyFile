# DaisyFile

Network file server for **Daisy**, a homebrew personal computer built from
several cooperating microcontrollers. DaisyFile runs on an ordinary host
machine (Linux, macOS, Windows) and stores DaisyOS's files — BASIC
programs, character sets, and sequential data files — as plain files in a
directory on the host.

The framed binary protocol is the one DaisyOS speaks in
`comm_messages.cpp`, carried over a TCP socket instead of a UART.

## Companion firmware

DaisyFile talks to DaisyOS through the ESP8266 WiFi modem.

| Unit       | Runs on     | Role                                        |
|------------|-------------|---------------------------------------------|
| DaisyFile  | Host PC     | This repo. File storage over TCP            |
| [DaisyOS](https://github.com/pineconecomputer/DaisyOS) | SAM3X (Due) | BASIC, editor, terminal, keyboard |
| [DaisyVideo](https://github.com/pineconecomputer/DaisyVideo) | ATmega2560 | 40×25 text + graphics, composite video out |
| [DaisySound](https://github.com/pineconecomputer/DaisySound) | ATmega328 | 3-voice synthesizer with envelopes and noise |
| ESP8266    | —           | WiFi modem (stock Zimodem firmware)         |

## Running

Requires Python 3.9 or newer. Standard library only; no dependencies.

```sh
git clone https://github.com/pineconecomputer/DaisyFile.git
cd DaisyFile
python3 daisyfile.py
```

| Flag        | Default      | Meaning                          |
|-------------|--------------|----------------------------------|
| `--host`    | `0.0.0.0`    | Bind address                     |
| `--port`    | `9000`       | TCP port                         |
| `--dir`     | `daisyfiles` | Directory files are stored in    |
| `--verbose` | off          | Log every frame at debug level   |

The storage directory is created on first run. Point Daisy's WiFi modem
at the host's IP and this port, and `CATALOG`, `LOAD`, and `SAVE` work
exactly as they do over the serial link.

[GETTING_STARTED.md](GETTING_STARTED.md) covers the first connection
from the Daisy side: `WIFI`, `NETCONNECT`, listing files, saving and
loading a program, and the common errors.

## Files

```
daisyfile.py       the file server — protocol, command handlers, accept loop
echo.py            minimal TCP echo server for bringing up a new WiFi link
daisyfiles/        default file storage (created at runtime, not tracked)
```

`echo.py` is a bring-up aid, not part of the server. It echoes back
whatever it receives. Its `HOST` constant is the address it binds to.

## Protocol

Every request from DaisyOS is a framed message:

```
[SOP=0x5C] [CMD] [PAYLOAD_LEN] [PAYLOAD ...] [CHECKSUM]
```

`CHECKSUM` is the two's complement of the preceding bytes, so a valid
frame sums to zero mod 256. Frames that fail the check are discarded and
the reader resynchronizes on the next `SOP`.

| Opcode | Command    | Purpose                                    |
|--------|------------|--------------------------------------------|
| `0x0A` | `CATALOG`  | Directory listing for the current directory |
| `0x0B` | `LOAD`     | Stream a BASIC source file to the client   |
| `0x0C` | `SAVE`     | Receive a BASIC source file                |
| `0x0D` | `LOADCHAR` | Stream 8-byte character bitmaps            |
| `0x0E` | `SAVECHAR` | Receive 8-byte character bitmaps           |
| `0x0F` | `FOPEN`    | Open a file on channel 0–4 (read/write/append) |
| `0x10` | `FCLOSE`   | Close a channel                            |
| `0x11` | `FPRINT`   | Write a line                               |
| `0x12` | `FINPUT`   | Read a line                                |
| `0x13` | `FGET`     | Read one byte                              |
| `0x14` | `FPUT`     | Write one byte                             |
| `0x15` | `FSEEK`    | Relative signed 32-bit seek                |
| `0x16` | `FBYTES`   | Bytes remaining on a read channel          |
| `0x17` | `DEL`      | Delete a file                              |
| `0x18` | `REN`      | Rename a file                              |
| `0x19` | `COPY`     | Copy a file                                |
| `0x1A` | `CHDIR`    | Change the per-connection working directory |
| `0x1B` | `MKDIR`    | Create a directory                         |
| `0x1C` | `FREWIND`  | Seek a channel back to byte 0              |

Responses vary by command: a bare `end` line for silent success, a
single `ACK` (`0x06`) or `NAK` (`0x15`) byte for the channel operations,
raw counts for `FBYTES` and `FGET`, or a `print "?...ERROR"` line that
DaisyBASIC executes so the message renders as a normal `?ERROR` to the
user. `CATALOG`, `LOAD`, and `SAVE` are streaming commands with their own
multi-line, ACK-paced formats; see the `cmd_*` docstrings in
`daisyfile.py`.

## Concurrency and sandboxing

The accept loop spawns a daemon thread per connection. Each client holds
its own working directory and its own set of open channels, so multiple
Daisys can be served concurrently.

All filenames, including ones with path components and anything reached
via `CHDIR`, are resolved against the storage directory and checked to
confirm they stay inside it. Paths that escape through `..` or an
absolute prefix are rejected as `?FILE NOT FOUND` or `?DIR NOT FOUND`.

DaisyFile has no authentication and no encryption.

## License

Licensed under the **GNU General Public License, version 3**. See
[LICENSE](LICENSE) for the full text.

    Copyright (C) 2026 Joe Cassara
