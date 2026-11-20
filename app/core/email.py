"""Envio de e-mail por SMTP (Mailpit em desenvolvimento; SMTP do Resend em producao).

O endereco do destinatario nunca vai para o log.
"""

import asyncio
import smtplib
from email.message import EmailMessage

from app.core.config import Configuracao

TEMPO_LIMITE_S = 15


class FalhaEnvioEmail(Exception):
    """O servidor de e-mail recusou ou nao respondeu."""


def _enviar(configuracao: Configuracao, mensagem: EmailMessage) -> None:
    try:
        with smtplib.SMTP(
            configuracao.smtp_host, configuracao.smtp_port, timeout=TEMPO_LIMITE_S
        ) as smtp:
            if configuracao.smtp_tls:
                smtp.starttls()
            if configuracao.smtp_usuario and configuracao.smtp_senha:
                smtp.login(configuracao.smtp_usuario, configuracao.smtp_senha.get_secret_value())
            smtp.send_message(mensagem)
    except (OSError, smtplib.SMTPException) as erro:
        raise FalhaEnvioEmail(type(erro).__name__) from erro


async def enviar_email(configuracao: Configuracao, para: str, assunto: str, corpo: str) -> None:
    mensagem = EmailMessage()
    mensagem["From"] = configuracao.email_remetente
    mensagem["To"] = para
    mensagem["Subject"] = assunto
    mensagem.set_content(corpo)
    await asyncio.to_thread(_enviar, configuracao, mensagem)
