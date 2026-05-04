"""BitTorrent download via aria2 XML-RPC (live seeders / leechers / speeds)."""

import http.client
import logging
import os
import socket
import subprocess
import tempfile
import time
import xmlrpc.client
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from xmlrpc.client import Binary

logger = logging.getLogger(__name__)

# Default xmlrpc.client has no socket timeout — RPC calls can hang forever (stuck Celery worker).
# Large .torrent over XML-RPC can be slow; BitTorrent tellStatus polls use the same client.
RPC_TIMEOUT_SEC = 180.0
RPC_PING_TIMEOUT_SEC = 10.0


class _TimeoutTransport(xmlrpc.client.Transport):
    """HTTP XML-RPC transport with a finite timeout (Python 3.11 HTTPConnection default is None)."""

    def __init__(self, timeout: float = RPC_TIMEOUT_SEC, use_datetime: bool = False):
        super().__init__(use_datetime=use_datetime)
        self._rpc_timeout = timeout

    def make_connection(self, host: str) -> http.client.HTTPConnection:
        if self._connection and host == self._connection[0]:
            return self._connection[1]
        chost, self._extra_headers, x509 = self.get_host_info(host)
        if x509:
            raise OSError("HTTPS not supported for localhost aria2 RPC in this transport")
        self._connection = host, http.client.HTTPConnection(chost, timeout=self._rpc_timeout)
        return self._connection[1]


def _rpc_proxy(url: str, timeout: float = RPC_TIMEOUT_SEC) -> xmlrpc.client.ServerProxy:
    return xmlrpc.client.ServerProxy(
        url,
        allow_none=True,
        transport=_TimeoutTransport(timeout=timeout),
    )

PollCallback = Callable[[Dict[str, Any]], None]


def _pick_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    _, port = s.getsockname()
    s.close()
    return int(port)


def _intish(v: Any) -> int:
    if v is None:
        return 0
    try:
        s = str(v).strip()
        if not s:
            return 0
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def status_to_stats(st: Dict[str, Any]) -> Dict[str, Any]:
    """Map aria2 tellStatus keys to a small JSON-serializable dict."""
    return {
        "seeders": _intish(st.get("numSeeders")),
        "leechers": _intish(st.get("numLeechers")),
        "connections": _intish(st.get("connections")),
        "download_bps": _intish(st.get("downloadSpeed")),
        "upload_bps": _intish(st.get("uploadSpeed")),
        "completed_bytes": _intish(st.get("completedLength")),
        "total_bytes": _intish(st.get("totalLength")),
        "status": str(st.get("status") or ""),
    }


def _wait_rpc_ready(
    url: str,
    proc: subprocess.Popen,
    stderr_log: Path,
    timeout: float = 30.0,
) -> xmlrpc.client.ServerProxy:
    deadline = time.time() + timeout
    last_err: Optional[BaseException] = None
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = ""
            try:
                if stderr_log.exists():
                    tail = stderr_log.read_text(encoding="utf-8", errors="replace")[-4000:]
            except OSError:
                pass
            raise RuntimeError(
                f"aria2 exited before RPC was ready (exit={proc.returncode}). stderr tail: {tail or '(empty)'}"
            )
        try:
            ping = _rpc_proxy(url, timeout=RPC_PING_TIMEOUT_SEC)
            ping.aria2.getVersion()
            return _rpc_proxy(url, timeout=RPC_TIMEOUT_SEC)
        except BaseException as e:
            last_err = e
            time.sleep(0.12)
    raise RuntimeError(f"aria2 RPC not ready: {last_err}")


def _start_aria2_foreground(save_dir: Path) -> Tuple[subprocess.Popen, xmlrpc.client.ServerProxy, Path]:
    """
    Run aria2 in the foreground as a child process (no --daemon).
    Daemon mode is unreliable under Celery/Docker (RPC never binds, connection refused).
    """
    save_dir.mkdir(parents=True, exist_ok=True)
    port = _pick_port()
    rpc_url = f"http://127.0.0.1:{port}/rpc"
    stderr_fd, log_path_str = tempfile.mkstemp(prefix="aria2-stderr-", suffix=".log")
    stderr_log = Path(log_path_str)

    cmd = [
        "aria2c",
        "--no-conf",
        "--enable-rpc",
        "--rpc-listen-port",
        str(port),
        # Localhost only; same container as XML-RPC client
        "--rpc-listen-all=false",
        "--rpc-allow-origin-all",
        "--quiet",
        # Docker / WSL: broken IPv6 often stalls trackers and DHT; cap TCP waits.
        "--disable-ipv6=true",
        "--connect-timeout=30",
        "--timeout=120",
    ]
    # Foreground child: reliable RPC bind in Docker/Celery (no --daemon).
    # Popen stderr must be an int (fd) or file object — not a path str (no fileno).
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(save_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_fd,
        )
    finally:
        os.close(stderr_fd)
    try:
        proxy = _wait_rpc_ready(rpc_url, proc, stderr_log)
        return proc, proxy, stderr_log
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        raise


def download_magnet_or_torrent(
    save_dir: Path,
    magnet: Optional[str],
    torrent_bytes: Optional[bytes],
    on_poll: PollCallback,
    poll_interval: float = 1.5,
    deadline_sec: float = 86400,
) -> None:
    """
    Start aria2 as a child process with XML-RPC, add one download, poll until complete.
    on_poll receives status_to_stats() dict on each tick.
    """
    if not magnet and not torrent_bytes:
        raise ValueError("magnet or torrent_bytes required")

    save_dir.mkdir(parents=True, exist_ok=True)
    proc: Optional[subprocess.Popen] = None
    proxy: Optional[xmlrpc.client.ServerProxy] = None
    stderr_log: Optional[Path] = None
    try:
        logger.info("aria2: starting subprocess for save_dir=%s", save_dir)
        proc, proxy, stderr_log = _start_aria2_foreground(save_dir)
        opts: Dict[str, str] = {"dir": str(save_dir)}
        gid: str
        if magnet:
            logger.info("aria2: addUri (magnet)")
            gid = proxy.aria2.addUri([magnet], opts)
        else:
            logger.info("aria2: addTorrent (%s bytes)", len(torrent_bytes or b""))
            # Must be XML-RPC base64 (Binary), not a str — aria2 bdecodes the payload.
            gid = proxy.aria2.addTorrent(Binary(torrent_bytes or b""), [], opts)

        deadline = time.time() + deadline_sec
        while time.time() < deadline:
            st = proxy.aria2.tellStatus(gid)
            on_poll(status_to_stats(st))
            status = st.get("status")
            if status == "complete":
                return
            if status == "error":
                msg = st.get("errorMessage") or "aria2 status error"
                raise RuntimeError(str(msg))
            if status == "removed":
                raise RuntimeError("aria2 download removed")
            if status == "paused":
                raise RuntimeError("aria2 download paused unexpectedly")
            # aria2 can stay "active" at 100% while hashing/seeding; do not block the pipeline forever.
            done_b = _intish(st.get("completedLength"))
            total_b = _intish(st.get("totalLength"))
            if total_b > 0 and done_b >= total_b:
                logger.info(
                    "aria2: treating as finished (status=%s completed=%s total=%s)",
                    status,
                    done_b,
                    total_b,
                )
                return
            time.sleep(poll_interval)

        raise RuntimeError("torrent download timeout")
    finally:
        if proxy is not None:
            try:
                proxy.aria2.shutdown()
            except BaseException as e:
                logger.debug("aria2 shutdown: %s", e)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if stderr_log is not None:
            try:
                stderr_log.unlink(missing_ok=True)
            except OSError:
                pass

