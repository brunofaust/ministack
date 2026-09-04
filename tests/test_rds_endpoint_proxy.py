"""The in-process RDS endpoint proxy forwards a listen port to the sidecar (unit, no endpoint)."""

import socket
import threading
import time

import ministack.services.rds as rds_mod


def _echo_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def run():
        conn, _ = srv.accept()
        with conn:
            data = conn.recv(1024)
            conn.sendall(b"echo:" + data)

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_proxy_forwards_bytes_both_ways():
    upstream_port = _echo_server()
    listen_port = _free_port()
    rds_mod._ensure_endpoint_proxy("db-1", "127.0.0.1", upstream_port, [listen_port])
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", listen_port), timeout=0.5) as c:
                c.sendall(b"ping")
                assert c.recv(1024) == b"echo:ping"
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("proxy never accepted a connection")


def test_a_port_already_owned_is_skipped_not_fought_over():
    holder = socket.socket()
    holder.bind(("0.0.0.0", 0))
    holder.listen()
    port = holder.getsockname()[1]
    try:
        rds_mod._ensure_endpoint_proxy("db-2", "127.0.0.1", 1, [port])
        time.sleep(0.3)
        # The bind failed, the registry forgot the port, the holder still owns it.
        assert port not in rds_mod._endpoint_proxies
    finally:
        holder.close()
