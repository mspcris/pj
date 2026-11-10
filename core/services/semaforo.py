"""Cores do financeiro por vencimento (10/11/2026, regra do Cristiano):
faltam 2 ou mais dias ÚTEIS (sem sábado e domingo) → verde; falta 1 dia
útil → amarelo; no dia ou vencido → vermelho, 1% mais escuro a cada dia
vencido. Boleto que vence no sábado ou domingo conta como vencendo na
sexta ("se vence no domingo, considerar sexta já vermelho")."""
from datetime import timedelta

from django.utils import timezone

CORES = {'verde': 'hsl(158, 76%, 36%)', 'amarelo': 'hsl(42, 90%, 46%)'}
_LUZ_VERMELHO, _LUZ_MIN = 48, 18


def dia_efetivo(vencimento):
    """Fim de semana recua para a sexta."""
    while vencimento.weekday() >= 5:
        vencimento -= timedelta(days=1)
    return vencimento


def dias_uteis_ate(hoje, data):
    """Dias úteis em (hoje, data]."""
    n, d = 0, hoje
    while d < data:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def semaforo_vencimento(vencimento, hoje=None):
    """{classe, cor, dias_vencido, dias_uteis, texto} ou None sem data."""
    if not vencimento:
        return None
    hoje = hoje or timezone.localdate()
    efetivo = dia_efetivo(vencimento)
    if efetivo <= hoje:
        vencido = (hoje - efetivo).days
        luz = max(_LUZ_VERMELHO - vencido, _LUZ_MIN)
        texto = ('vence hoje' if vencido == 0 else
                 f'vencido há {vencido} dia{"s" if vencido > 1 else ""}')
        if efetivo != vencimento and vencido == 0:
            texto = f'vence {vencimento:%d/%m} (fim de semana) — conta hoje'
        return {'classe': 'vermelho', 'cor': f'hsl(4, 78%, {luz}%)',
                'dias_vencido': vencido, 'dias_uteis': 0, 'texto': texto}
    uteis = dias_uteis_ate(hoje, efetivo)
    classe = 'verde' if uteis >= 2 else 'amarelo'
    return {'classe': classe, 'cor': CORES[classe], 'dias_vencido': 0,
            'dias_uteis': uteis,
            'texto': f'faltam {uteis} dia{"s" if uteis > 1 else ""} úte'
                     f'{"is" if uteis > 1 else "l"}'}


PESO = {'verde': 0, 'amarelo': 1, 'vermelho': 2}


def pior(semaforos):
    """O mais urgente de uma lista (None ignorados): vermelho mais vencido
    > amarelo > verde."""
    validos = [s for s in semaforos if s]
    if not validos:
        return None
    return max(validos, key=lambda s: (PESO[s['classe']], s['dias_vencido']))
