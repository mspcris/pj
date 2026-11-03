"""Postos e valores do prestador com VIGÊNCIA (03/11/2026): toda mudança
vale "a partir de um mês" e fica na auditoria com quem mudou; os meses
anteriores continuam como estavam ("sempre que altero algo o legado fica
errado — uma vez existindo, pronto"). Caminho ÚNICO do painel e de
qualquer acerto feito à mão.
"""
from datetime import date

from django.db import transaction

from ..models import AuditLog, PrestadorPosto
from .verificacao import _moeda


def mes_anterior(mes):
    m = mes.month - 1 or 12
    return date(mes.year - (1 if mes.month == 1 else 0), m, 1)


def salvar_valores(prestador, a_partir_de, dados, quem='', request=None):
    """`dados`: {posto: (atende: bool, valor: Decimal|None)}. Aplica as
    mudanças a partir do mês `a_partir_de`, registra UMA linha de
    auditoria com tudo que mudou e devolve a lista de mudanças (texto)."""
    a = a_partir_de.replace(day=1)
    anterior = mes_anterior(a)
    mudancas = []
    with transaction.atomic():
        for posto, (atende, valor) in dados.items():
            v = PrestadorPosto.objects.filter(prestador=prestador,
                                              posto=posto).first()
            if not atende:
                if v is None or not v.vigente_em(a):
                    continue
                if v.desde is not None and anterior < v.desde:
                    v.ativo = False          # nunca chegou a contar
                    v.save(update_fields=['ativo'])
                else:
                    v.ate = anterior
                    v.save(update_fields=['ate'])
                mudancas.append(f'{posto.nome}: deixa de contar a partir de '
                                f'{a:%m/%Y}')
                continue
            if v is None:
                PrestadorPosto.objects.create(prestador=prestador, posto=posto,
                                              valor_mensal=valor, desde=a)
                mudancas.append(f'{posto.nome}: novo, R$ {_moeda(valor)} a '
                                f'partir de {a:%m/%Y}')
                continue
            if not v.vigente_em(a):
                campos = []
                if not v.ativo:
                    v.ativo, v.desde, v.ate = True, a, None
                    campos += ['ativo', 'desde', 'ate']
                if v.ate is not None and v.ate < a:
                    v.ate = None
                    campos.append('ate')
                if v.desde is not None and v.desde > a:
                    v.desde = a
                    campos.append('desde')
                if campos:
                    v.save(update_fields=sorted(set(campos)))
                antigo = v.valor_em(a)
                if valor != antigo:
                    v.registrar_valor(valor, a, quem)
                mudancas.append(f'{posto.nome}: volta a contar a partir de '
                                f'{a:%m/%Y}, R$ {_moeda(valor)}')
                continue
            antigo = v.valor_em(a)
            if valor != antigo:
                v.registrar_valor(valor, a, quem)
                mudancas.append(f'{posto.nome}: R$ {_moeda(antigo)} → '
                                f'R$ {_moeda(valor)} a partir de {a:%m/%Y}')
    if mudancas:
        AuditLog.registrar(
            AuditLog.Evento.CRUD, request, ator=quem,
            detalhe=f'Valores de {prestador} (a partir de {a:%m/%Y}): '
                    + '; '.join(mudancas))
    return mudancas
