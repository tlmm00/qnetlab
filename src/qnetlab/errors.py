"""Hierarquia de exceções do framework.

Todas herdam de :class:`QNetLabError` para que a CLI possa tratá-las de forma
uniforme (mensagem objetiva + código de saída != 0) sem esconder bugs reais.
"""
from __future__ import annotations


class QNetLabError(Exception):
    """Erro base do framework."""


class ConfigError(QNetLabError):
    """Topologia/configuração declarativa inválida."""


class AddressingError(QNetLabError):
    """Falha no planejamento de endereçamento IP."""


class Gns3Error(QNetLabError):
    """Falha ao conversar com o servidor GNS3."""

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class ConsoleError(QNetLabError):
    """Falha no console serial/telnet de um nó."""


class PkiError(QNetLabError):
    """Falha na geração/validação da PKI (OpenSSL)."""


class ImageBuildError(QNetLabError):
    """Falha ao construir imagens Docker."""


class RouterOsError(QNetLabError):
    """Falha ao configurar/consultar um MikroTik RouterOS."""


class TelemetryError(QNetLabError):
    """Falha na coleta ou processamento de telemetria."""
