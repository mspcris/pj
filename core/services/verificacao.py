"""Orquestração da verificação de boleto.

Fluxo: upload → e-mail "recebemos" → extrai texto do PDF → IA extrai o valor
→ compara com o valor acordado:
  * bate      → APROVADO: e-mail p/ pagador (equipe@) com o boleto anexo
                + e-mail p/ o PJ avisando que foi para pagamento
  * não bate  → DIVERGENTE: e-mail p/ o PJ pedindo para ligar
  * não leu   → MANUAL: e-mail p/ o Cristiano verificar no painel

A decisão de comparação é 100% nossa (Decimal, tolerância de 1 centavo).
A IA só extrai número e redige frase — nunca decide pagamento.
"""
import json
import logging
import re
import threading
from decimal import Decimal

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from ..models import AuditLog, Boleto
from . import emails, frases, ia, pdf

log = logging.getLogger(__name__)

TOLERANCIA = Decimal('0.01')
MAX_TENTATIVAS = 3

_MESES = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho',
          'agosto', 'setembro', 'outubro', 'novembro', 'dezembro']


def competencia_extenso(d):
    return f'{_MESES[d.month - 1]}/{d.year}'


def _moeda(v):
    if v is None:
        return '—'
    s = f'{v:,.2f}'
    return s.replace(',', '_').replace('.', ',').replace('_', '.')


def _fatos(boleto):
    posto = boleto.posto_efetivo
    return {
        'prestador': boleto.prestador.nome,
        'alvo': posto.nome if posto else 'boleto único',
        'competencia': competencia_extenso(boleto.competencia),
        'valor': _moeda(boleto.valor_extraido or boleto.valor_esperado),
    }


def dados_pagamento(boleto, fatos):
    """Bloco determinístico com os dados de pagamento — anexado ao corpo do
    e-mail do pagador DEPOIS da redação (a IA nunca toca nesses dados)."""
    partes = ['', '-' * 40,
              f'Prestador: {fatos["prestador"]} — {fatos["alvo"]}',
              f'Competência: {fatos["competencia"]}',
              f'Valor: R$ {fatos["valor"]}']
    if boleto.linha_digitavel:
        partes.append(f'Linha digitável: {boleto.linha_digitavel}')
    if boleto.chave_pix:
        partes.append(f'Chave PIX: {boleto.chave_pix}')
    return '\n'.join(partes)


def enviar_recebido(boleto):
    fatos = _fatos(boleto)
    corpo = frases.corpo(
        'recebido', fatos,
        instrucao_ia=('Escreva confirmando que recebemos o boleto do '
                      'prestador e que ele será verificado em breve.'))
    emails.enviar(boleto.enviado_por or settings.EMAIL_ADMIN,
                  f'Boleto recebido — {fatos["competencia"]}',
                  corpo, boleto=boleto)


def _marcar(boleto, status):
    boleto.status = status
    boleto.verificado_em = timezone.now()
    boleto.save()
    AuditLog.registrar(AuditLog.Evento.STATUS, ator='sistema',
                       detalhe=f'Boleto #{boleto.pk} → {status}')


def _para_manual(boleto, motivo):
    boleto.ia_resposta = (boleto.ia_resposta + f'\n[manual] {motivo}').strip()
    _marcar(boleto, Boleto.Status.MANUAL)
    fatos = _fatos(boleto)
    emails.enviar(settings.EMAIL_ADMIN,
                  f'⚠️ Verificar boleto manualmente — {fatos["prestador"]} — '
                  f'{fatos["competencia"]}',
                  frases.corpo('manual_admin', fatos) + f'\n\nMotivo: {motivo}',
                  boleto=boleto)


def processar(boleto_pk):
    """Verifica um boleto RECEBIDO. Seguro para rodar por thread E por cron:
    a claim atômica em `tentativas` garante que só um processa."""
    try:
        boleto = (Boleto.objects.select_related('prestador', 'posto',
                                                'prestador__posto_cobranca')
                  .get(pk=boleto_pk))
    except Boleto.DoesNotExist:
        return

    if boleto.status != Boleto.Status.RECEBIDO:
        return
    claimed = Boleto.objects.filter(
        pk=boleto_pk, status=Boleto.Status.RECEBIDO,
        tentativas=boleto.tentativas,
    ).update(tentativas=boleto.tentativas + 1)
    if not claimed:
        return
    boleto.tentativas += 1

    fatos = _fatos(boleto)

    if not boleto.arquivo.name.lower().endswith('.pdf'):
        _para_manual(boleto, 'arquivo não é PDF (imagem/foto)')
        return

    texto = pdf.extrair_texto(boleto.arquivo.path)
    if not texto:
        _para_manual(boleto, 'PDF sem texto legível (escaneado?)')
        return

    try:
        valor, bruto = ia.extrair_valor(texto)
        boleto.ia_resposta = bruto[:4000]
    except Exception as e:
        log.error('IA falhou no boleto #%s: %s', boleto_pk, e)
        if boleto.tentativas >= MAX_TENTATIVAS:
            _para_manual(boleto, f'IA indisponível após '
                                 f'{MAX_TENTATIVAS} tentativas: {e}')
        else:
            boleto.save()  # continua RECEBIDO; o cron tenta de novo
        return

    if valor is None:
        _para_manual(boleto, 'IA não identificou o valor no boleto')
        return

    boleto.valor_extraido = valor
    fatos['valor'] = _moeda(valor)

    if not boleto.linha_digitavel:
        try:
            ld = re.sub(r'\D', '', str(json.loads(bruto)
                                       .get('linha_digitavel') or ''))
            if 40 <= len(ld) <= 48:
                boleto.linha_digitavel = ld
        except Exception:
            pass

    if boleto.valor_esperado is None:
        _para_manual(boleto, 'sem valor acordado cadastrado no painel')
        return

    if abs(valor - boleto.valor_esperado) <= TOLERANCIA:
        _marcar(boleto, Boleto.Status.APROVADO)
        emails.enviar(
            settings.EMAIL_PAGADOR,
            f'Pagamento — {fatos["prestador"]} — {fatos["alvo"]} — '
            f'{fatos["competencia"]} — R$ {fatos["valor"]}',
            frases.corpo(
                'aprovado_pagador', fatos,
                instrucao_ia=('Escreva para a equipe de pagamento pedindo '
                              'para pagar o boleto em anexo, informando que '
                              'o valor já foi conferido e que os dados de '
                              'pagamento seguem abaixo da assinatura.'))
            + dados_pagamento(boleto, fatos),
            boleto=boleto, anexo_field=boleto.arquivo)
        emails.enviar(
            boleto.enviado_por or settings.EMAIL_ADMIN,
            f'Boleto aprovado e enviado p/ pagamento — {fatos["competencia"]}',
            frases.corpo(
                'aprovado_pj', fatos,
                instrucao_ia=('Escreva para o prestador avisando que o boleto '
                              'foi conferido, o valor está correto e já foi '
                              'encaminhado para pagamento.')),
            boleto=boleto)
    else:
        fatos['valor_esperado'] = _moeda(boleto.valor_esperado)
        _marcar(boleto, Boleto.Status.DIVERGENTE)
        emails.enviar(
            boleto.enviado_por or settings.EMAIL_ADMIN,
            f'Boleto {fatos["competencia"]} — valor a confirmar',
            frases.corpo(
                'divergente', fatos,
                instrucao_ia=('Escreva para o prestador dizendo que o valor '
                              'combinado não consta no boleto enviado e peça '
                              'para ligar para o Cristiano para entenderem a '
                              'diferença. NÃO cite números.')),
            boleto=boleto)


def fluxo_completo_async(boleto_pk):
    """Dispara em thread: e-mail de recebimento + verificação. O cron
    processar_boletos é a rede de segurança se a thread morrer."""
    def _run():
        close_old_connections()
        try:
            b = Boleto.objects.get(pk=boleto_pk)
            enviar_recebido(b)
            processar(boleto_pk)
        except Exception:
            log.exception('fluxo do boleto #%s falhou (cron retenta)',
                          boleto_pk)
        finally:
            close_old_connections()
    threading.Thread(target=_run, daemon=True).start()
