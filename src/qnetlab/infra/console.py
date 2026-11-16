"""Console telnet mínimo (stdlib) para o bootstrap inicial dos MikroTik CHR.

O GNS3 expõe o console serial dos nós Qemu via telnet. Só o estritamente
necessário é implementado: negociação IAC (recusa todas as opções), ``expect``
por regex e execução de comandos RouterOS com detecção de erros.
"""
from __future__ import annotations

import logging
import re
import socket
import time
from typing import List, Optional, Sequence, Tuple

from ..errors import ConsoleError

log = logging.getLogger(__name__)

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
PROMPT = re.compile(r"\[[^\]\r\n]+\] ?> ?$")
ERROR_MARKERS = ("failure:", "bad command", "syntax error", "expected end of command",
                 "no such item", "invalid value", "input does not match")


class TelnetConsole:
    def __init__(self, host: str, port: int, timeout: float = 10.0):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise ConsoleError(f"Não foi possível abrir o console {host}:{port}: {exc}") from exc
        self.sock.settimeout(0.5)
        self._pending = b""
        self._buf = ""

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "TelnetConsole":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------- baixo nível
    def _negotiate(self, data: bytes) -> bytes:
        data = self._pending + data
        self._pending = b""
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= len(data):
                self._pending = data[i:]
                break
            cmd = data[i + 1]
            if cmd == IAC:
                out.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= len(data):
                    self._pending = data[i:]
                    break
                opt = data[i + 2]
                if cmd == DO:
                    self.sock.sendall(bytes([IAC, WONT, opt]))
                elif cmd == WILL:
                    self.sock.sendall(bytes([IAC, DONT, opt]))
                i += 3
            elif cmd == SB:
                end = data.find(bytes([IAC, SE]), i + 2)
                if end < 0:
                    self._pending = data[i:]
                    break
                i = end + 2
            else:
                i += 2
        return bytes(out)

    def _fill(self) -> bool:
        try:
            chunk = self.sock.recv(4096)
        except socket.timeout:
            return False
        except OSError as exc:
            raise ConsoleError(f"Conexão de console perdida: {exc}") from exc
        if not chunk:
            raise ConsoleError("Console fechado pelo servidor.")
        self._buf += self._negotiate(chunk).decode("utf-8", errors="replace")
        return True

    def send(self, text: str) -> None:
        try:
            self.sock.sendall(text.encode())
        except OSError as exc:
            raise ConsoleError(f"Falha ao escrever no console: {exc}") from exc

    def send_line(self, text: str = "") -> None:
        self.send(text + "\r\n")

    def expect(self, patterns: Sequence["re.Pattern[str]"], timeout: float) -> Tuple[int, str]:
        """Aguarda algum padrão; devolve (índice, texto consumido até o fim do casamento)."""
        deadline = time.time() + timeout
        while True:
            for idx, pat in enumerate(patterns):
                m = pat.search(self._buf)
                if m:
                    text, self._buf = self._buf[: m.end()], self._buf[m.end():]
                    return idx, text
            if time.time() > deadline:
                tail = self._buf[-300:]
                raise ConsoleError(f"Tempo esgotado aguardando {[p.pattern for p in patterns]}. Final do buffer: {tail!r}")
            self._fill()

    def drain(self, quiet: float = 0.5) -> None:
        end = time.time() + quiet
        while time.time() < end:
            if self._fill():
                end = time.time() + quiet
        self._buf = ""


class RouterOsConsole:
    """Sessão de console RouterOS: login resiliente + execução de comandos."""

    def __init__(self, host: str, port: int, user: str, passwords: Sequence[str], boot_timeout: float = 420.0):
        self.tc = TelnetConsole(host, port)
        self.user = user
        self.passwords = list(dict.fromkeys(passwords))
        self.boot_timeout = boot_timeout

    def close(self) -> None:
        self.tc.close()

    def login(self) -> None:
        r = re.compile
        failed = r(r"(?i)(invalid user name or password|login failed|incorrect username or password)")
        pats = [
            r(r"(?i)login:\s*$"),                                  # 0
            r(r"(?i)(?<!new )(?<!repeat new )password:\s*$"),      # 1
            r(r"(?i)\[y/n\]:?\s*$"),                               # 2  licença
            r(r"(?i)new password>\s*$"),                           # 3
            r(r"(?i)repeat new password>\s*$"),                    # 4
            PROMPT,                                               # 5
        ]
        deadline = time.time() + self.boot_timeout
        failures = 0
        state = "idle"                  # idle | sent_user | sent_pw (evita responder a prompts repetidos)
        while time.time() < deadline:
            try:
                idx, text = self.tc.expect(pats, timeout=15)
            except ConsoleError:
                self.tc.send_line()     # acorda o console durante o boot
                continue
            # A mensagem de falha chega junto do novo prompt "Login:", então é detectada no texto consumido.
            if failed.search(text):
                failures += 1
                state = "idle"
                if failures >= max(1, len(self.passwords)):
                    raise ConsoleError("Credenciais rejeitadas pelo RouterOS.")
            if idx == 0:
                if state == "sent_user":
                    continue            # prompt duplicado (eco do 'nudge'): já enviamos o usuário
                # "+ct200w": sem cores, sem detecção de terminal, largura 200
                self.tc.send_line(f"{self.user}+ct200w")
                state = "sent_user"
            elif idx == 1:
                pw = self.passwords[min(failures, len(self.passwords) - 1)] if self.passwords else ""
                self.tc.send_line(pw)
                state = "sent_pw"
            elif idx == 2:
                self.tc.send_line("n")
            elif idx in (3, 4):
                self.tc.send_line(self.passwords[-1] if self.passwords else "")
            elif idx == 5:
                self.tc.drain(0.3)
                log.info("Login RouterOS concluído")
                return
        raise ConsoleError("Tempo esgotado aguardando o login do RouterOS.")

    def run(self, command: str, timeout: float = 30.0) -> str:
        self.tc.send_line(command)
        _, text = self.tc.expect([PROMPT], timeout=timeout)
        low = text.lower()
        for marker in ERROR_MARKERS:
            if marker in low:
                raise ConsoleError(f"Comando falhou: {command!r}\n{text.strip()}")
        return text

    def run_many(self, commands: Sequence[str]) -> List[str]:
        return [self.run(c) for c in commands if c.strip() and not c.lstrip().startswith("#")]
