"""Envio de e-mail pelo Gmail do Cristiano, sempre com registro em EmailLog.

EMAIL_MODO_TESTE=true no .env desvia TODO e-mail para EMAIL_ADMIN — para
testar o fluxo inteiro sem incomodar PJ nem a equipe de pagamento.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMessage

from ..models import EmailLog

log = logging.getLogger(__name__)


def enviar(destinatario, assunto, corpo, boleto=None, anexo_field=None):
    """Envia e registra. Retorna True/False — nunca levanta exceção."""
    dest_real = destinatario
    if settings.EMAIL_MODO_TESTE:
        assunto = f'[TESTE p/ {destinatario}] {assunto}'
        dest_real = settings.EMAIL_ADMIN

    registro = EmailLog(destinatario=dest_real, assunto=assunto,
                        corpo=corpo, boleto=boleto)
    try:
        msg = EmailMessage(subject=assunto, body=corpo,
                           from_email=settings.DEFAULT_FROM_EMAIL,
                           to=[dest_real])
        if anexo_field:
            anexo_field.open('rb')
            try:
                nome = (getattr(boleto, 'nome_original', '') or
                        anexo_field.name.rsplit('/', 1)[-1])
                msg.attach(nome, anexo_field.read(), 'application/pdf')
            finally:
                anexo_field.close()
        msg.send(fail_silently=False)
        registro.ok = True
        log.info('E-mail enviado: %s — %s', dest_real, assunto)
    except Exception as e:
        registro.ok = False
        registro.erro = str(e)[:2000]
        log.error('Falha ao enviar e-mail p/ %s: %s', dest_real, e)
    registro.save()
    return registro.ok
