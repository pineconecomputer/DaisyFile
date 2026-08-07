# Getting Started with DaisyFile

Connecting a Daisy to the file server for the first time: listing files,
saving a program, loading it back, and organizing files into directories.

Commands shown for the Daisy are typed at the `READY.` prompt in
immediate mode, without line numbers. For the full definition of any
command used here, see the
[DaisyBASIC Programmer's Reference](https://github.com/pineconecomputer/DaisyOS/blob/main/DAISYBASIC_REFERENCE.md).

## 1. Start the server

On the host machine — the PC, Mac, or Raspberry Pi that will hold the
files:

```sh
python3 daisyfile.py
```

```
14:22:01 [MainThread          ] INFO file directory : /home/joe/DaisyFile/daisyfiles
14:22:01 [MainThread          ] INFO listening on   : 0.0.0.0:9000
14:22:01 [MainThread          ] INFO press Ctrl+C to stop
```

`daisyfiles/` is created on first run and holds everything the Daisy
saves. The server logs every request to this window.

The Daisy needs the host's IP address on the LAN:

| Host       | Command                                    |
|------------|--------------------------------------------|
| Linux      | `hostname -I`                              |
| macOS      | `ipconfig getifaddr en0`                   |
| Windows    | `ipconfig` — look for IPv4 Address         |

Inbound TCP on port 9000 must be open in the host firewall.

## 2. Get the Daisy on WiFi

```basic
WIFI "MyNetwork", "secret"
```

To confirm the connection:

```basic
PRINT WIFI$(0)        ← SSID of the connected network
PRINT WIFI$(1)        ← the Daisy's own IP address
PRINT WIFI$(2)        ← "OK" if the internet is reachable
```

`WIFI$(1)` returning an address on the same subnet as the host confirms
both machines are on the network. `?WIFI CONNECT FAILED` means the SSID
or password was rejected.

The Daisy remembers the network between sessions.

## 3. Connect to the server

```basic
NETCONNECT "192.168.1.10", 9000
```

On success the server logs the connection:

```
14:23:40 [192.168.1.42:51070  ] INFO connected from 192.168.1.42:51070
```

`?NETCONNECT FAILED` means the Daisy could not reach the host: wrong
address, server not running, or a blocked port. Nothing appears in the
server log in that case.

To test for an open session:

```basic
IF NETCONNECTED() THEN PRINT "ONLINE"
```

`WIFI` and `WIFI$()` are only meaningful when no session is open; the
modem is in pass-through mode while connected.

## 4. List files

```basic
CAT
```

`CATALOG` is the same command spelled out. The listing shows the current
directory path, then subdirectories marked `<DIR>`, then files with their
sizes in bytes:

```
/
GAMES/           <DIR>
UTILS/           <DIR>
HELLO.BAS        84
README.TXT       1450
```

Files and directories whose names start with `_` are hidden from the
catalog.

## 5. Save a program

With a program in memory:

```basic
10 FOR I = 1 TO 5
20 PRINT "HELLO FROM DAISY"; I
30 NEXT I
```

Then:

```basic
SAVE "hello.bas"
```

The Daisy prints `Saved` once the server has written the file.
`?FILE EXISTS ERROR` means the name is taken; prefix it with `-` to
overwrite:

```basic
SAVE "-hello.bas"
```

The file is plain text in `daisyfiles/hello.bas`.

## 6. Load a program back

```basic
NEW                   ← clear the current program first
LOAD "hello.bas"
RUN
```

`LOAD` prints `FOUND` and then transfers the program. `?FILE NOT FOUND
ERROR` means no such name in the current directory.

To page through a text file instead of loading it as a program:

```basic
MORE "readme.txt"
```

Space pages forward, Backspace back, `:` searches, BREAK quits.

## 7. Organize with directories

```basic
MKDIR "games"
CHDIR "games"
CAT                   ← now shows /games
SAVE "invaders.bas"
CHDIR ".."            ← back up one level
CHDIR "/"             ← jump to the root
```

The current directory belongs to the connection, not the server: it
resets to `/` on reconnect, and each concurrent client has its own. Paths
that would leave the storage directory, including `CHDIR ".."` at the
root, return `?DIR NOT FOUND ERROR`.

## 8. Housekeeping

```basic
COPY "hello.bas", "hello_bak.bas"
REN "hello_bak.bas", "backup.bas"
DEL "backup.bas"
```

Filenames may be quoted or bare (`DEL hello.bas`). All three act on the
current directory.

## 9. Data files

Beyond whole programs, a running program can read and write files on the
host through numbered channels 0–3:

```basic
10 FOPEN 0, "scores.txt", "W"
20 FPRINT 0, "ALICE,4200"; CHR$(10)
30 FPRINT 0, "BOB,3150"; CHR$(10)
40 FCLOSE 0
50 FOPEN 0, "scores.txt", "R"
60 FINPUT 0, A$
70 FINPUT 0, B$
80 FCLOSE 0
90 PRINT A$ : PRINT B$
```

Modes are `"R"` read, `"W"` write (truncates an existing file), and
`"A"` append. `FPRINT` appends no newline, hence the explicit `CHR$(10)`.
The remaining channel operations are `FGET` and `FPUT` for single bytes,
`FSEEK` to move the cursor, `FREWIND` to return to the start, and
`FBYTES(channel)` for the bytes remaining.

## 10. Disconnect

```basic
NETDISCONNECT
```

This takes about two seconds; the modem requires a guard-time pause
before accepting the hangup. Channels still open are closed server-side
when the connection drops.

## Troubleshooting

The server logs every request, showing whether a command arrived.

```sh
python3 daisyfile.py --verbose
```

adds a line per protocol frame, including checksum failures.

| Symptom                          | Likely cause                                       |
|----------------------------------|----------------------------------------------------|
| `?WIFI CONNECT FAILED`           | Wrong SSID or password                             |
| `?NETCONNECT FAILED`, nothing logged | Wrong host IP, server not running, or firewall |
| `?FILE NOT FOUND ERROR`          | Name doesn't match — check `CAT` and the directory |
| `?FILE EXISTS ERROR`             | Target exists; use `SAVE "-name"` to overwrite     |
| `?DIR NOT FOUND ERROR`           | No such directory, or the path escapes the root    |
| Commands hang after a few lines  | Link dropped mid-transfer; `NETDISCONNECT` and reconnect |

`echo.py` tests the link without the file protocol involved. Set its
`HOST` constant to the host's address, run it, and from the Daisy:

```basic
NETCONNECT "192.168.1.10", 6502
NETPRINT "TEST"; CHR$(13)
NETINPUT A$;
PRINT A$
```

`TEST` coming back confirms the modem, the network, and the host.
`echo.py` serves one connection and then exits.

## Where to go next

- [DaisyBASIC Programmer's Reference](https://github.com/pineconecomputer/DaisyOS/blob/main/DAISYBASIC_REFERENCE.md) — every command in full, including the `NETPRINT` / `NETGET` / `NETINPUT` raw-socket calls
- [README](README.md) — server options, the wire protocol, and the opcode table
- [DaisyOS](https://github.com/pineconecomputer/DaisyOS) — the firmware on the other end of the link
