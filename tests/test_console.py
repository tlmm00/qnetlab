"""Testa o login/execução no console RouterOS contra um servidor telnet falso."""
import re
import socket
import threading

import pytest

from qnetlab.errors import ConsoleError
from qnetlab.infra.console import IAC, DO, WILL, RouterOsConsole


class FakeRouter(threading.Thread):
    """Emula o console serial de um CHR: IAC, login, licença, prompt e eco de comandos."""

    def __init__(self, good_password: str = "", reject_first: bool = False, fail_on: str = ""):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.good_password, self.reject_first, self.fail_on = good_password, reject_first, fail_on
        self.commands: list = []

    def _readline(self, c: socket.socket) -> str:
        buf = b""
        while not buf.endswith(b"\n"):
            d = c.recv(1)
            if not d:
                raise EOFError
            buf += d
        return re.sub(rb"[^\x20-\x7e]", b"", buf).decode()

    def run(self) -> None:
        c, _ = self.srv.accept()
        try:
            c.sendall(bytes([IAC, DO, 1, IAC, WILL, 3]))                 # negociação a ser recusada
            c.sendall(b"\r\nMikroTik 7.15 (c) 1999-2024\r\nMikroTik Login: ")
            while True:
                user = self._readline(c)
                if not user:                                              # linha vazia: reexibe o login
                    c.sendall(b"MikroTik Login: ")
                    continue
                assert user.startswith("admin+ct"), user
                c.sendall(b"Password: ")
                pw = self._readline(c)
                if self.reject_first and pw == "":
                    c.sendall(b"\r\nlogin failed, incorrect username or password\r\n\r\nMikroTik Login: ")
                    self.reject_first = False
                    continue
                if pw != self.good_password:
                    c.sendall(b"\r\nlogin failed, incorrect username or password\r\n\r\nMikroTik Login: ")
                    continue
                break
            c.sendall(b"\r\nDo you want to see the software license? [Y/n]: ")
            assert self._readline(c) == "n"
            c.sendall(b"\r\n[admin@MikroTik] > ")
            while True:
                cmd = self._readline(c)
                self.commands.append(cmd)
                if self.fail_on and self.fail_on in cmd:
                    c.sendall(f"{cmd}\r\nfailure: item already exists\r\n[admin@MikroTik] > ".encode())
                else:
                    c.sendall(f"{cmd}\r\n[admin@MikroTik] > ".encode())
        except (EOFError, OSError):
            pass
        finally:
            c.close()
            self.srv.close()


def test_login_and_commands():
    fake = FakeRouter()
    fake.start()
    con = RouterOsConsole("127.0.0.1", fake.port, "admin", ["", "pw"], boot_timeout=10)
    try:
        con.login()
        out = con.run_many(["# comentário", "/ip address print", "", "/system identity print"])
        assert len(out) == 2
    finally:
        con.close()
    assert fake.commands == ["/ip address print", "/system identity print"]


def test_login_falls_back_to_configured_password():
    fake = FakeRouter(good_password="configured", reject_first=False)
    fake.start()
    con = RouterOsConsole("127.0.0.1", fake.port, "admin", ["", "configured"], boot_timeout=10)
    try:
        con.login()
    finally:
        con.close()


def test_failure_marker_raises():
    fake = FakeRouter(fail_on="duplicate")
    fake.start()
    con = RouterOsConsole("127.0.0.1", fake.port, "admin", [""], boot_timeout=10)
    try:
        con.login()
        with pytest.raises(ConsoleError, match="item already exists"):
            con.run("/ip address add duplicate")
    finally:
        con.close()


def test_connection_refused():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(ConsoleError):
        RouterOsConsole("127.0.0.1", port, "admin", [""])
