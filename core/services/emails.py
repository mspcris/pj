"""Envio de e-mail pelo Gmail do Cristiano, sempre com registro em EmailLog.

EMAIL_MODO_TESTE=true no .env desvia TODO e-mail para EMAIL_ADMIN — para
testar o fluxo inteiro sem incomodar PJ nem a equipe de pagamento.
"""
import html as html_mod
import logging
import mimetypes

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from ..models import EmailLog

log = logging.getLogger(__name__)


def _render_html(corpo):
    """Versão HTML do e-mail: cartão branco com cabeçalho CAMIM; o bloco
    de dados (após a linha de traços) vira tabela. O texto puro segue
    junto como alternativa."""
    esc = html_mod.escape
    paragrafos, tabela = [], []
    em_tabela = False
    for ln in corpo.split('\n'):
        s = ln.strip()
        if s and set(s) == {'-'} and len(s) >= 10:
            em_tabela = True
            continue
        if em_tabela and ': ' in s:
            chave, _, valor = s.partition(': ')
            tabela.append(
                '<tr><td style="padding:6px 14px 6px 0;color:#5c6b66;'
                'font-weight:bold;white-space:nowrap;vertical-align:top">'
                f'{esc(chave)}</td>'
                f'<td style="padding:6px 0">{esc(valor)}</td></tr>')
            continue
        if s:
            paragrafos.append(f'<p style="margin:0 0 10px">{esc(s)}</p>')
    tabela_html = ''
    if tabela:
        tabela_html = ('<table cellspacing="0" style="border-top:1px solid '
                       '#dfe9e5;margin-top:10px;font-size:14px;width:100%">'
                       + ''.join(tabela) + '</table>')
    return (
        '<div style="background:#f4f8f6;padding:24px 12px;font-family:Arial,'
        'Helvetica,sans-serif;color:#15221e;font-size:15px;line-height:1.5">'
        '<div style="max-width:640px;margin:0 auto;background:#ffffff;'
        'border-radius:12px;overflow:hidden;border:1px solid #dfe9e5">'
        '<div style="background:#0b7a5e;color:#ffffff;padding:14px 22px;'
        'font-weight:bold;font-size:16px">CAMIM '
        '<span style="font-weight:normal;opacity:.85;font-size:13px">'
        '&middot; Controle de Prestadores</span></div>'
        f'<div style="padding:22px">{"".join(paragrafos)}{tabela_html}</div>'
        '<div style="padding:12px 22px;background:#f4f8f6;color:#5c6b66;'
        'font-size:12px">Mensagem autom&aacute;tica do sistema de controle '
        'de prestadores da CAMIM &middot; pj.camim.com.br</div>'
        '</div></div>')


def enviar(destinatario, assunto, corpo, boleto=None, anexo_field=None,
           de=None, anexos=None, cc=None):
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
    copias = [c for c in (cc or []) if c and c not in dests]
    if settings.EMAIL_MODO_TESTE:
        assunto = f'[TESTE p/ {", ".join(dests)}] {assunto}'
        dests = [settings.EMAIL_ADMIN]
        copias = []

    rotulo = ', '.join(dests) + (f' +cc: {", ".join(copias)}' if copias
                                 else '')
    registro = EmailLog(destinatario=rotulo[:255], assunto=assunto,
                        corpo=corpo, boleto=boleto)
    try:
        msg = EmailMultiAlternatives(
            subject=assunto, body=corpo,
            from_email=de or settings.DEFAULT_FROM_EMAIL, to=dests,
            cc=copias or None)
        msg.attach_alternative(_render_html(corpo), 'text/html')
        lista = []
        if anexo_field:
            lista.append((anexo_field,
                          getattr(boleto, 'nome_original', '') or ''))
        lista.extend(anexos or [])
        for campo, nome in lista:
            if not campo:
                continue
            campo.open('rb')
            try:
                nome = nome or campo.name.rsplit('/', 1)[-1]
                tipo = (mimetypes.guess_type(nome)[0]
                        or 'application/octet-stream')
                msg.attach(nome, campo.read(), tipo)
            finally:
                campo.close()
        msg.send(fail_silently=False)
        registro.ok = True
        log.info('E-mail enviado: %s — %s', ', '.join(dests), assunto)
    except Exception as e:
        registro.ok = False
        registro.erro = str(e)[:2000]
        log.error('Falha ao enviar e-mail p/ %s: %s', ', '.join(dests), e)
    registro.save()
    return registro.ok
