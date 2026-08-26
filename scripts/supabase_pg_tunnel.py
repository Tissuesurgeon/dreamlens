#!/usr/bin/env python3
"""Local TCP proxy: 127.0.0.1:65432 -> Tor SOCKS -> Supabase pooler:5432.

This machine's ISP completes TCP to the pooler but drops Postgres SSLRequest.
TLS still terminates on Supabase; Tor only carries the TCP stream.
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

LISTEN = ("127.0.0.1", 65432)
SOCKS = ("127.0.0.1", 19050)
TOR_CONTAINER = "dreamlens-tor-socks"
TOR_IMAGE = "dreamlens-tor-socks:local"


def _database_url() -> str:
    raw = os.environ.get("DATABASE_URL", "")
    if raw:
        return raw
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _dest() -> tuple[str, int]:
    parsed = urlparse(_database_url())
    host = parsed.hostname or "aws-1-eu-west-1.pooler.supabase.com"
    return host, 5432


def _open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        sock = socket.create_connection((host, port), timeout)
        sock.close()
        return True
    except OSError:
        return False


def _ensure_tor() -> None:
    root = Path(__file__).resolve().parent.parent
    dockerfile_dir = root / "docker" / "tor-socks"
    inspect = subprocess.run(
        ["docker", "image", "inspect", TOR_IMAGE],
        capture_output=True,
    )
    if inspect.returncode != 0:
        subprocess.run(
            ["docker", "build", "-t", TOR_IMAGE, str(dockerfile_dir)],
            check=True,
        )
    running = subprocess.run(
        ["docker", "inspect", "-f", "{{.Config.Image}} {{.State.Running}}", TOR_CONTAINER],
        capture_output=True,
        text=True,
    )
    image_ok = running.returncode == 0 and running.stdout.strip().startswith(TOR_IMAGE)
    is_up = running.returncode == 0 and running.stdout.strip().endswith("true")
    if not (image_ok and is_up and _open(*SOCKS)):
        subprocess.run(["docker", "rm", "-f", TOR_CONTAINER], capture_output=True)
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                TOR_CONTAINER,
                "-p",
                "127.0.0.1:19050:9050",
                TOR_IMAGE,
            ],
            check=True,
            capture_output=True,
        )
    deadline = time.time() + 90
    while time.time() < deadline:
        if _open(*SOCKS, timeout=1):
            break
        time.sleep(1)
    else:
        raise SystemExit("Tor SOCKS proxy did not become ready on 127.0.0.1:19050")
    deadline = time.time() + 120
    while time.time() < deadline:
        logs = subprocess.run(
            ["docker", "logs", TOR_CONTAINER],
            capture_output=True,
            text=True,
        )
        text = (logs.stdout or "") + (logs.stderr or "")
        if "Bootstrapped 100%" in text:
            return
        time.sleep(1)
    raise SystemExit("Tor did not finish bootstrapping")


def _socks5_connect(dest_host: str, dest_port: int, timeout: float = 45) -> socket.socket:
    sock = socket.create_connection(SOCKS, timeout)
    sock.settimeout(timeout)
    sock.sendall(b"\x05\x01\x00")
    if sock.recv(2) != b"\x05\x00":
        sock.close()
        raise OSError("SOCKS5 auth rejected")
    host_b = dest_host.encode()
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + struct.pack("!H", dest_port))
    header = sock.recv(4)
    if len(header) < 4 or header[1] != 0:
        sock.close()
        raise OSError(f"SOCKS5 connect failed: {header!r}")
    atyp = header[3]
    if atyp == 1:
        sock.recv(6)
    elif atyp == 3:
        sock.recv(sock.recv(1)[0] + 2)
    elif atyp == 4:
        sock.recv(18)
    sock.settimeout(None)
    return sock


def _pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            src.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket, dest_host: str, dest_port: int) -> None:
    try:
        upstream = _socks5_connect(dest_host, dest_port)
    except OSError:
        client.close()
        return
    threading.Thread(target=_pump, args=(client, upstream), daemon=True).start()
    _pump(upstream, client)
    client.close()
    upstream.close()


def _kill_port(port: int) -> None:
    try:
        out = subprocess.check_output(["ss", "-lptn", f"sport = :{port}"], text=True)
    except (OSError, subprocess.CalledProcessError):
        return
    for token in out.replace(",", " ").split():
        if token.startswith("pid="):
            pid = token.split("=", 1)[1]
            if pid.isdigit() and int(pid) != os.getpid():
                subprocess.run(["kill", pid], capture_output=True)


def serve() -> None:
    dest_host, dest_port = _dest()
    _ensure_tor()
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(LISTEN)
    except OSError:
        _kill_port(LISTEN[1])
        time.sleep(0.3)
        server.bind(LISTEN)
    server.listen(64)
    print(f"supabase pg tunnel 127.0.0.1:{LISTEN[1]} -> {dest_host}:{dest_port}", flush=True)
    while True:
        client, _addr = server.accept()
        threading.Thread(
            target=_handle,
            args=(client, dest_host, dest_port),
            daemon=True,
        ).start()


if __name__ == "__main__":
    try:
        serve()
    except KeyboardInterrupt:
        sys.exit(0)
