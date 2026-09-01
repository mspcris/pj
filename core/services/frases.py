"""Frases dos e-mails — a IA redige variando; se a IA cair, sorteamos um
modelo pronto (o fluxo de pagamento nunca pode travar por causa de frase)."""
import logging
import random

from . import ia

log = logging.getLogger(__name__)

_RECEBIDO = [
    ('Olá!\n\nRecebemos o boleto de {prestador} ({alvo}) referente a '
     '{competencia}. Ele já entrou na fila de verificação e em breve dou '
     'retorno.\n\nAbraço,\nCristiano'),
    ('Oi, tudo bem?\n\nSó confirmando: o boleto de {competencia} '
     '({prestador} — {alvo}) chegou aqui e será verificado.\n\n'
     'Qualquer coisa te aviso.\n\nAbraço,\nCristiano'),
    ('Olá!\n\nBoleto de {competencia} recebido com sucesso ({prestador} — '
     '{alvo}). Vou conferir os dados e te retorno em seguida.\n\n'
     'Abraço,\nCristiano'),
]

_APROVADO_PJ = [
    ('Prezados,\n\nInformamos que o boleto referente à competência de '
     '{competencia} ({prestador} — {alvo}) foi conferido, está de acordo '
     'com o valor contratado e já foi encaminhado ao setor financeiro para '
     'pagamento. Os dados da cobrança seguem abaixo.\n\n'
     'Atenciosamente,\nCristiano — CAMIM'),
    ('Prezado(a) {prestador},\n\nApós conferência, o boleto da competência '
     'de {competencia} apresentou o valor acordado em contrato e foi '
     'encaminhado para pagamento. Seguem abaixo os dados do documento.\n\n'
     'Atenciosamente,\nCristiano — CAMIM'),
]

_APROVADO_PAGADOR = [
    ('Equipe,\n\nSegue em anexo o boleto de {prestador} ({alvo}), competência '
     '{competencia}, no valor de R$ {valor}. Valor conferido com o contrato — '
     'favor efetuar o pagamento.\n\nAbraço,\nCristiano'),
    ('Equipe,\n\nEncaminho para pagamento o boleto anexo: {prestador} — '
     '{alvo} — {competencia} — R$ {valor}. Já validei o valor.\n\n'
     'Abraço,\nCristiano'),
]

_FIN_RECEBIDO = [
    ('Prezados,\n\nBoas notícias: o boleto da competência de {competencia} '
     '({prestador} — {alvo}) já está com o nosso setor financeiro para '
     'processamento do pagamento. Nenhuma ação é necessária da sua parte — '
     'é só aguardar a compensação. Os dados do documento seguem abaixo.\n\n'
     'Atenciosamente,\nCristiano — CAMIM'),
    ('Prezado(a) {prestador},\n\nInformamos que o setor financeiro da CAMIM '
     'confirmou o recebimento do boleto da competência de {competencia} e o '
     'pagamento está em processamento. Não é preciso fazer nada — '
     'avisaremos se houver qualquer novidade.\n\n'
     'Atenciosamente,\nCristiano — CAMIM'),
]

_DIVERGENTE = [
    ('Olá!\n\nRecebi o boleto de {competencia} ({prestador} — {alvo}), mas o '
     'valor que consta nele não bate com o que combinamos. Pode me ligar para '
     'entendermos essa diferença antes de eu enviar para pagamento?\n\n'
     'Abraço,\nCristiano'),
    ('Oi, tudo bem?\n\nNo boleto de {competencia} ({alvo}) o valor está '
     'diferente do acordado. Me liga quando puder para alinharmos? Enquanto '
     'isso o pagamento fica em espera.\n\nAbraço,\nCristiano'),
]

_MANUAL_ADMIN = [
    ('Cristiano,\n\nNão consegui ler automaticamente o boleto de {prestador} '
     '({alvo}, {competencia}). Verifique manualmente no painel: '
     'https://pj.camim.com.br/painel/\n\n— Controle dos PJs'),
]


def _fallback(pool, fatos):
    return random.choice(pool).format(**fatos)


def corpo(tipo, fatos, instrucao_ia=None):
    """Corpo do e-mail: tenta a IA (frases sempre novas); cai no modelo."""
    pools = {
        'recebido': _RECEBIDO,
        'aprovado_pj': _APROVADO_PJ,
        'aprovado_pagador': _APROVADO_PAGADOR,
        'fin_recebido': _FIN_RECEBIDO,
        'divergente': _DIVERGENTE,
        'manual_admin': _MANUAL_ADMIN,
    }
    if instrucao_ia:
        try:
            return ia.redigir_email(instrucao_ia, fatos)
        except Exception as e:
            log.warning('IA de redação falhou (%s); usando modelo pronto', e)
    return _fallback(pools[tipo], fatos)
