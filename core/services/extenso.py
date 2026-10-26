"""Valores e datas por extenso, em português, para os contratos gerados.

    >>> valor_extenso(Decimal('3990.00'))
    'três mil novecentos e noventa reais'
    >>> valor_extenso(Decimal('498.75'))
    'quatrocentos e noventa e oito reais e setenta e cinco centavos'
"""
from decimal import Decimal

_UNIDADES = ['zero', 'um', 'dois', 'três', 'quatro', 'cinco', 'seis', 'sete',
             'oito', 'nove', 'dez', 'onze', 'doze', 'treze', 'quatorze',
             'quinze', 'dezesseis', 'dezessete', 'dezoito', 'dezenove']
_DEZENAS = ['', '', 'vinte', 'trinta', 'quarenta', 'cinquenta', 'sessenta',
            'setenta', 'oitenta', 'noventa']
_CENTENAS = ['', 'cento', 'duzentos', 'trezentos', 'quatrocentos',
             'quinhentos', 'seiscentos', 'setecentos', 'oitocentos',
             'novecentos']
MESES = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho',
         'agosto', 'setembro', 'outubro', 'novembro', 'dezembro']


def _ate_999(n):
    if n == 0:
        return ''
    if n == 100:
        return 'cem'
    partes = []
    c, resto = divmod(n, 100)
    if c:
        partes.append(_CENTENAS[c])
    if resto:
        if resto < 20:
            partes.append(_UNIDADES[resto])
        else:
            d, u = divmod(resto, 10)
            partes.append(_DEZENAS[d] + (f' e {_UNIDADES[u]}' if u else ''))
    return ' e '.join(partes)


def numero_extenso(n):
    """Inteiro ≥ 0 por extenso (até os bilhões)."""
    n = int(n)
    if n == 0:
        return 'zero'
    grupos = [(10 ** 9, 'bilhão', 'bilhões'), (10 ** 6, 'milhão', 'milhões'),
              (1000, 'mil', 'mil')]
    partes = []
    for base, sing, plur in grupos:
        q, n = divmod(n, base)
        if q:
            if base == 1000:
                partes.append('mil' if q == 1 else f'{_ate_999(q)} mil')
            else:
                partes.append(f'{numero_extenso(q)} {sing if q == 1 else plur}')
    if n:
        # "mil E duzentos", "três mil E quatro", mas "três mil novecentos e
        # noventa" (regra: "e" antes do último grupo quando ele é < 100 ou
        # é uma centena redonda).
        ultimo = _ate_999(n)
        if not partes:
            return ultimo
        liga = ' e ' if (n < 100 or n % 100 == 0) else ' '
        return ' '.join(partes) + liga + ultimo
    return ' '.join(partes)


def valor_extenso(valor):
    """Decimal em reais → 'x reais e y centavos' (sem o 'R$' e sem parênteses)."""
    valor = Decimal(valor).quantize(Decimal('0.01'))
    reais = int(valor)
    centavos = int((valor - reais) * 100)
    partes = []
    if reais:
        partes.append(f'{numero_extenso(reais)} {"real" if reais == 1 else "reais"}')
    if centavos:
        partes.append(f'{numero_extenso(centavos)} '
                      f'{"centavo" if centavos == 1 else "centavos"}')
    return ' e '.join(partes) if partes else 'zero real'


def valor_formatado(valor):
    """Decimal → 'R$ 3.990,00'."""
    v = Decimal(valor).quantize(Decimal('0.01'))
    return 'R$ ' + f'{v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def data_extenso(d):
    """date → '26 de outubro de 2026'."""
    return f'{d.day:02d} de {MESES[d.month - 1]} de {d.year}'
