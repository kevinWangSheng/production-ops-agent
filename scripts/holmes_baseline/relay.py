"""The baseline container's only two ways out (U5-a), stdlib-only so it runs
unchanged in the ``python:3.11-alpine`` base image the Holmes build pulled.

``connect``: an HTTP CONNECT proxy that tunnels only to the allowlisted
``host:port`` pairs (the model API) and refuses every other method, host and
port. ``forward``: plain TCP forwards from the internal network to the kind
node's NodePorts (readonly Prometheus proxy, Jaeger query). Each connection
is one JSON line on stdout: who, where, allowed or not, byte counts; never
any payload (the tunnels are TLS anyway).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

MAX_HEADER_BYTES = 8192


def log(**fields: object) -> None:
    print(json.dumps({"ts": time.time(), **fields}, sort_keys=True), flush=True)


def parse_connect(head: bytes, allow: frozenset[str]) -> tuple[str | None, str]:
    """``(target, reason)``: the allowlisted ``host:port`` of a CONNECT
    request line, or ``None`` with the refusal reason."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("ascii")
        method, target, _version = line.split(" ")
    except (UnicodeDecodeError, ValueError):
        return None, "malformed"
    if method != "CONNECT":
        return None, f"method {method}"
    if target.lower() not in allow:
        return None, f"target {target}"
    return target.lower(), "allowlisted"


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    total = 0
    try:
        while chunk := await reader.read(65536):
            total += len(chunk)
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - closing a dead socket
            pass
    return total


async def _splice(
    client: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter],
) -> tuple[int, int]:
    up, down = await asyncio.gather(
        _pipe(client[0], upstream[1]), _pipe(upstream[0], client[1])
    )
    return up, down


async def handle_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    allow: frozenset[str],
) -> None:
    peer = writer.get_extra_info("peername")
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        log(mode="connect", client=str(peer), allowed=False, reason="no header")
        return
    target, reason = parse_connect(head, allow)
    if target is None:
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        log(mode="connect", client=str(peer), allowed=False, reason=reason)
        return
    host, port = target.rsplit(":", 1)
    try:
        upstream = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 15)
    except (OSError, asyncio.TimeoutError) as exc:
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        log(
            mode="connect",
            client=str(peer),
            target=target,
            allowed=True,
            reason=f"upstream {type(exc).__name__}",
        )
        return
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    started = time.monotonic()
    up, down = await _splice((reader, writer), upstream)
    log(
        mode="connect",
        client=str(peer),
        target=target,
        allowed=True,
        reason=reason,
        bytes_up=up,
        bytes_down=down,
        seconds=round(time.monotonic() - started, 3),
    )


async def handle_forward(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, target: str
) -> None:
    peer = writer.get_extra_info("peername")
    host, port = target.rsplit(":", 1)
    try:
        upstream = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 15)
    except (OSError, asyncio.TimeoutError) as exc:
        writer.close()
        log(
            mode="forward",
            client=str(peer),
            target=target,
            ok=False,
            reason=type(exc).__name__,
        )
        return
    up, down = await _splice((reader, writer), upstream)
    log(
        mode="forward",
        client=str(peer),
        target=target,
        ok=True,
        bytes_up=up,
        bytes_down=down,
    )


async def serve(args: argparse.Namespace) -> None:
    servers = []
    if args.mode == "connect":
        allow = frozenset(a.lower() for a in args.allow)
        servers.append(
            await asyncio.start_server(
                lambda r, w: handle_connect(r, w, allow),
                "0.0.0.0",
                args.listen,
                limit=MAX_HEADER_BYTES,
            )
        )
        log(mode="connect", listening=args.listen, allow=sorted(allow))
    else:
        for spec in args.route:
            port, target = spec.split("=", 1)
            servers.append(
                await asyncio.start_server(
                    lambda r, w, t=target: handle_forward(r, w, t), "0.0.0.0", int(port)
                )
            )
            log(mode="forward", listening=int(port), target=target)
    await asyncio.gather(*(s.serve_forever() for s in servers))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    connect = sub.add_parser("connect")
    connect.add_argument("--listen", type=int, default=3128)
    connect.add_argument("--allow", action="append", required=True, help="host:port")
    forward = sub.add_parser("forward")
    forward.add_argument(
        "--route", action="append", required=True, help="port=host:port"
    )
    try:
        asyncio.run(serve(parser.parse_args(argv)))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
