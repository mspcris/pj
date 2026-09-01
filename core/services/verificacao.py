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
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from ..models import AuditLog, Boleto
from . import boletos as svc_boletos
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


def valor_da_linha(linha):
    """Valor embutido no código de barras — conferência 100% determinística.

    Linha digitável de boleto bancário (47 díg.): valor = últimos 10 dígitos,
    em centavos. Guia de arrecadação (48 díg.): remove o DV de cada bloco de
    12 e lê o valor nas posições 5-15 do código de barras resultante.
    """
    ld = re.sub(r'\D', '', linha or '')
    try:
        if len(ld) == 47:
            v = Decimal(ld[-10:]) / 100
        elif len(ld) == 48:
            cb = ''.join(d for i, d in enumerate(ld) if (i + 1) % 12 != 0)
            v = Decimal(cb[4:15]) / 100
        else:
            return None
        return v.quantize(Decimal('0.01')) if v > 0 else None
    except Exception:
        return None


def dados_pagamento(boleto, fatos):
    """Bloco determinístico com os dados de pagamento — anexado ao corpo do
    e-mail do pagador DEPOIS da redação (a IA nunca toca nesses dados)."""
    partes = ['', '-' * 40,
              f'Prestador: {fatos["prestador"]} — {fatos["alvo"]}',
              f'Competência: {fatos["competencia"]}',
              f'Valor: R$ {fatos["valor"]}']
    if boleto.vencimento:
        partes.append(f'Vencimento: {boleto.vencimento:%d/%m/%Y}')
    if boleto.linha_digitavel:
        partes.append(f'Linha digitável: {boleto.linha_digitavel}')
    if boleto.chave_pix:
        partes.append(f'Chave PIX: {boleto.chave_pix}')
    if (boleto.valor_esperado is not None
            and boleto.valor_extraido is not None
            and boleto.valor_esperado - boleto.valor_extraido > TOLERANCIA):
        partes.append(f'Obs.: valor abaixo do combinado '
                      f'(R$ {_moeda(boleto.valor_esperado)}) — acordo com o '
                      'prestador.')
    return '\n'.join(partes)


def _mes_seguinte_fim(competencia):
    """Último dia do mês seguinte ao da competência."""
    m = competencia.month + 2
    ano = competencia.year + (m - 1) // 12
    mes = (m - 1) % 12 + 1
    return date(ano, mes, 1)  # exclusivo: vencimento < este dia


def destinatarios_pj(boleto):
    """Para quem vão os avisos do prestador: TODOS os usuários ativos do PJ
    (não quem apertou o botão — se o admin cadastrar, o PJ é avisado do
    mesmo jeito). Quem enviou também entra, se for outro endereço."""
    ems = [u.email for u in boleto.prestador.usuarios.filter(ativo=True)]
    extra = (boleto.enviado_por or '').lower()
    if extra and extra != settings.EMAIL_ADMIN.lower() and extra not in ems:
        ems.append(extra)
    return ems or [settings.EMAIL_ADMIN]


def enviar_recebido(boleto):
    fatos = _fatos(boleto)
    corpo = frases.corpo(
        'recebido', fatos,
        instrucao_ia=('Escreva confirmando que recebemos o boleto do '
                      'prestador e que ele será verificado em breve.'))
    emails.enviar(destinatarios_pj(boleto),
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

    # 1) Valor do PDF (via IA) — quando há PDF.
    valor_pdf = None
    if boleto.arquivo:
        if not boleto.arquivo.name.lower().endswith('.pdf'):
            _para_manual(boleto, 'arquivo não é PDF (imagem/foto)')
            return
        texto = pdf.extrair_texto(boleto.arquivo.path)
        if not texto:
            _para_manual(boleto, 'PDF sem texto legível (escaneado?)')
            return
        try:
            valor_pdf, bruto = ia.extrair_valor(texto)
            boleto.ia_resposta = bruto[:4000]
        except Exception as e:
            log.error('IA falhou no boleto #%s: %s', boleto_pk, e)
            if boleto.tentativas >= MAX_TENTATIVAS:
                _para_manual(boleto, f'IA indisponível após '
                                     f'{MAX_TENTATIVAS} tentativas: {e}')
            else:
                boleto.save()  # continua RECEBIDO; o cron tenta de novo
            return
        if valor_pdf is None:
            _para_manual(boleto, 'IA não identificou o valor no PDF')
            return
        try:
            dados = json.loads(bruto)
        except Exception:
            dados = {}
        if not boleto.linha_digitavel:
            ld = re.sub(r'\D', '', str(dados.get('linha_digitavel') or ''))
            if 40 <= len(ld) <= 48:
                boleto.linha_digitavel = ld
        if boleto.vencimento is None:
            try:
                boleto.vencimento = datetime.strptime(
                    str(dados.get('vencimento') or ''), '%d/%m/%Y').date()
            except ValueError:
                pass

    # 2) Valor embutido no código de barras (determinístico, sem IA).
    valor_linha = valor_da_linha(boleto.linha_digitavel)

    if valor_pdf is None and valor_linha is None:
        _para_manual(boleto, 'sem PDF legível e sem linha digitável com '
                             'valor — nada para conferir')
        return

    # 3) O CÓDIGO TEM DE BATER COM O VALOR: PDF × código de barras.
    if (valor_pdf is not None and valor_linha is not None
            and abs(valor_pdf - valor_linha) > TOLERANCIA):
        _para_manual(boleto,
                     f'código de barras diz R$ {_moeda(valor_linha)}, mas o '
                     f'PDF diz R$ {_moeda(valor_pdf)} — documento '
                     'inconsistente, NÃO enviado para pagamento')
        return

    valor = valor_pdf if valor_pdf is not None else valor_linha
    boleto.valor_extraido = valor
    fatos['valor'] = _moeda(valor)

    # 4) NÃO DUPLICIDADE: nunca aprovar duas vezes a mesma competência.
    dup = svc_boletos.duplicado_de(boleto)
    if dup is not None:
        _para_manual(boleto,
                     f'possível DUPLICIDADE: o boleto #{dup.pk} desta mesma '
                     f'competência já está "{dup.get_status_display()}" — '
                     'nada foi enviado para pagamento')
        return

    # 5) O MÊS TEM DE BATER: vencimento dentro da janela da competência
    # (do dia 1 da competência até o fim do mês seguinte).
    if (boleto.vencimento is not None
            and not (boleto.competencia <= boleto.vencimento
                     < _mes_seguinte_fim(boleto.competencia))):
        _para_manual(boleto,
                     f'vencimento {boleto.vencimento:%d/%m/%Y} não bate com '
                     f'a competência {fatos["competencia"]}')
        return

    if boleto.valor_esperado is None and not boleto.valor_livre:
        _para_manual(boleto, 'sem valor acordado cadastrado no painel')
        return

    # 6) Valor × combinado. Igual ou MENOR (pode haver acordo) → aprova.
    # MAIOR: NUNCA aprova sozinho — só o admin, cadastrando direto na
    # plataforma com "aceitar este valor" (valor_livre).
    if boleto.valor_livre or (valor - boleto.valor_esperado) <= TOLERANCIA:
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
            boleto=boleto,
            anexo_field=boleto.arquivo if boleto.arquivo else None,
            de=settings.EMAIL_FROM_PAGADOR)
        emails.enviar(
            destinatarios_pj(boleto),
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
            destinatarios_pj(boleto),
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
