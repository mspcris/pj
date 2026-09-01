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
    ('Olá!\n\nConferi o boleto de {competencia} ({prestador} — {alvo}) e está '
     'tudo certo com o valor combinado. Já encaminhei para pagamento.\n\n'
     'Abraço,\nCristiano'),
    ('Oi!\n\nO boleto de {competencia} bateu certinho com o valor acordado. '
     'Acabei de enviar para o pagamento.\n\nAbraço,\nCristiano'),
    ('Olá, tudo bem?\n\nVerificação concluída: o boleto de {competencia} '
     '({alvo}) está correto e seguiu para pagamento.\n\nAbraço,\nCristiano'),
]

_APROVADO_PAGADOR = [
    ('Equipe,\n\nSegue em anexo o boleto de {prestador} ({alvo}), competência '
     '{competencia}, no valor de R$ {valor}. Valor conferido com o contrato — '
     'favor efetuar o pagamento.\n\nAbraço,\nCristiano'),
    ('Equipe,\n\nEncaminho para pagamento o boleto anexo: {prestador} — '
     '{alvo} — {competencia} — R$ {valor}. Já validei o valor.\n\n'
     'Abraço,\nCristiano'),
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
        'divergente': _DIVERGENTE,
        'manual_admin': _MANUAL_ADMIN,
    }
    if instrucao_ia:
        try:
            return ia.redigir_email(instrucao_ia, fatos)
        except Exception as e:
            log.warning('IA de redação falhou (%s); usando modelo pronto', e)
    return _fallback(pools[tipo], fatos)
