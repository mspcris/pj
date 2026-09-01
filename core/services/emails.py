"""Envio de e-mail pelo Gmail do Cristiano, sempre com registro em EmailLog.

EMAIL_MODO_TESTE=true no .env desvia TODO e-mail para EMAIL_ADMIN — para
testar o fluxo inteiro sem incomodar PJ nem a equipe de pagamento.
"""
import logging
import mimetypes

from django.conf import settings
from django.core.mail import EmailMessage

from ..models import EmailLog

log = logging.getLogger(__name__)


def enviar(destinatario, assunto, corpo, boleto=None, anexo_field=None,
           de=None):
    """Envia e registra. Retorna True/False — nunca levanta exceção.

    `de` troca o remetente (ex.: e-mail p/ o pagador sai do cristiano@;
    o padrão pj@ fica para os e-mails aos PJs). `destinatario` pode ser
    um endereço ou uma lista."""
    if isinstance(destinatario, str):
        dests = [destinatario]
    else:
        dests = [d for d in destinatario if d]
    if not dests:
        dests = [settings.EMAIL_ADMIN]
    if settings.EMAIL_MODO_TESTE:
        assunto = f'[TESTE p/ {", ".join(dests)}] {assunto}'
        dests = [settings.EMAIL_ADMIN]

    registro = EmailLog(destinatario=', '.join(dests)[:255], assunto=assunto,
                        corpo=corpo, boleto=boleto)
    try:
        msg = EmailMessage(subject=assunto, body=corpo,
                           from_email=de or settings.DEFAULT_FROM_EMAIL,
                           to=dests)
        if anexo_field:
            anexo_field.open('rb')
            try:
                nome = (getattr(boleto, 'nome_original', '') or
                        anexo_field.name.rsplit('/', 1)[-1])
                tipo = (mimetypes.guess_type(nome)[0]
                        or 'application/octet-stream')
                msg.attach(nome, anexo_field.read(), tipo)
            finally:
                anexo_field.close()
        msg.send(fail_silently=False)
        registro.ok = True
        log.info('E-mail enviado: %s — %s', ', '.join(dests), assunto)
    except Exception as e:
        registro.ok = False
        registro.erro = str(e)[:2000]
        log.error('Falha ao enviar e-mail p/ %s: %s', ', '.join(dests), e)
    registro.save()
    return registro.ok
