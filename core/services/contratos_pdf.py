"""Gera o PDF de um contrato POR POSTO a partir de um modelo de texto
(JSON em core/modelos_contrato/) + prestador + posto + valores.

Usado pelo comando `manage.py contratos_por_posto`. O modelo traz o texto
integral das cláusulas com marcadores entre chaves — ver
docs/contratos-por-posto.md para a lista ({valor}, {valor_extenso},
{inicio}, {fim}, {primeiro_pagamento}, {dia_pagamento}, {meses}, ...).

Decisões (26/10/2026, Caio e Victória):
  * CONTRATANTE = razão social da Receita + CNPJ + sede do posto
    (Posto.razao_social_contrato / Posto.sede), "representada na forma de
    seu contrato social" — não citamos o nome do gerente;
  * CONTRATADA vem do cadastro do Prestador (razão social, CNPJ, endereço,
    representante, CPF, e-mail da empresa ou do 1º usuário ativo);
  * assinaturas em branco (o PDF é assinado pelo gov.br depois) e duas
    testemunhas em branco;
  * centavos que sobram da divisão vão, um a um, para os primeiros postos
    da lista (568,52 ×6 + 568,51 ×2 = 4.548,14 no caso da Victória).
"""
import json
from datetime import date
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

from .extenso import data_extenso, numero_extenso, valor_extenso, valor_formatado

MODELOS_DIR = Path(__file__).resolve().parent.parent / 'modelos_contrato'

CONTRATANTE_PADRAO = (
    '{posto_razao_social}, pessoa jurídica de direito privado, inscrita no '
    'CNPJ sob o nº {posto_cnpj}, com sede na {posto_sede}, neste ato '
    'representada na forma de seu contrato social.')
CONTRATADA_PADRAO = (
    '{razao_social}, pessoa jurídica de direito privado, inscrita no CNPJ sob '
    'o nº {cnpj}, com sede na {sede}, endereço eletrônico {email}, neste ato '
    'representada por {representante}, inscrito(a) no CPF sob o nº {cpf}.')


class _Seguro(dict):
    """format_map que deixa marcador desconhecido como está (e avisa)."""

    def __missing__(self, k):
        return '{' + k + '}'


def carregar_modelo(nome_ou_caminho):
    """'ti' → core/modelos_contrato/ti.json; ou um caminho para .json."""
    p = Path(nome_ou_caminho)
    if not p.suffix:
        p = MODELOS_DIR / f'{nome_ou_caminho}.json'
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def listar_modelos():
    return sorted(p.stem for p in MODELOS_DIR.glob('*.json'))


def dividir(total, n):
    """Divide `total` em n parcelas de 2 casas que SOMAM exatamente o total.
    Sobra de centavos vai para as primeiras."""
    total = Decimal(total).quantize(Decimal('0.01'))
    base = (total / n).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    sobra = int((total - base * n) * 100)
    return [base + (Decimal('0.01') if i < sobra else Decimal('0'))
            for i in range(n)]


def primeiro_pagamento(inicio, dia):
    """1º vencimento: dia `dia` do mês SEGUINTE ao início (regime
    POSTERIOR: o serviço do mês é pago no mês seguinte)."""
    ano, mes = (inicio.year + (inicio.month // 12), inicio.month % 12 + 1)
    from calendar import monthrange
    return date(ano, mes, min(dia, monthrange(ano, mes)[1]))


def contexto(prestador, posto, valor, inicio, fim, meses, dia, data_assinatura,
             total=None):
    """Dicionário de marcadores para o modelo."""
    email = prestador.email_empresa
    if not email:
        u = prestador.usuarios.filter(ativo=True).order_by('pk').first() \
            if hasattr(prestador, 'usuarios') else None
        email = u.email if u else ''
    total = Decimal(total) if total is not None else Decimal(valor)
    return _Seguro({
        'valor': valor_formatado(valor),
        'valor_extenso': valor_extenso(valor),
        'total': valor_formatado(total),
        'total_extenso': valor_extenso(total),
        'inicio': data_extenso(inicio),
        'fim': data_extenso(fim),
        'primeiro_pagamento': data_extenso(primeiro_pagamento(inicio, dia)),
        'dia_pagamento': f'{dia:02d}',
        'meses': str(meses),
        'meses_extenso': numero_extenso(meses),
        'data_assinatura': data_extenso(data_assinatura),
        'posto': posto.nome,
        'posto_razao_social': posto.razao_social_contrato or posto.razao_social
        or posto.nome,
        'posto_cnpj': posto.cnpj or '—',
        'posto_sede': posto.sede or '(sede não cadastrada)',
        'razao_social': prestador.razao_social or prestador.nome,
        'nome': prestador.nome,
        'cnpj': prestador.cnpj_formatado or prestador.cnpj or '—',
        'sede': prestador.endereco_completo.replace(' — ', ', ') or '—',
        'email': email or '—',
        'representante': prestador.representante or prestador.nome,
        'cpf': prestador.representante_cpf or '—',
    })


def _fmt(texto, ctx):
    return texto.format_map(ctx)


def gerar_pdf(modelo, ctx, caminho):
    """Escreve o PDF em `caminho` (reportlab). Retorna o caminho."""
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate,
                                    Spacer, Table, TableStyle)

    st_title = ParagraphStyle('t', fontName='Helvetica-Bold', fontSize=13,
                              leading=17, alignment=TA_CENTER, spaceAfter=14)
    st_body = ParagraphStyle('b', fontName='Helvetica', fontSize=10.5,
                             leading=14.5, alignment=TA_JUSTIFY, spaceAfter=6)
    st_head = ParagraphStyle('h', fontName='Helvetica-Bold', fontSize=10.5,
                             leading=14.5, spaceBefore=8, spaceAfter=4)
    st_right = ParagraphStyle('r', parent=st_body, alignment=TA_RIGHT,
                              spaceBefore=10, spaceAfter=16)
    st_sig = ParagraphStyle('s', fontName='Helvetica', fontSize=10,
                            leading=13.5)

    titulo = _fmt(modelo['titulo'], ctx)
    rodape = modelo.get('rodape', titulo.replace('\n', ' '))

    def _rodape(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillGray(0.4)
        canvas.drawString(2.5 * cm, 1.3 * cm, rodape)
        canvas.drawRightString(A4[0] - 2.5 * cm, 1.3 * cm, str(doc.page))
        canvas.restoreState()

    doc = SimpleDocTemplate(
        str(caminho), pagesize=A4, leftMargin=2.5 * cm, rightMargin=2.5 * cm,
        topMargin=2.5 * cm, bottomMargin=2.2 * cm,
        title=f'{titulo.replace(chr(10), " ")} — {ctx["posto_razao_social"]} × {ctx["razao_social"]}',
        author='CAMIM', subject=f'Posto {ctx["posto"]}')
    e = [Paragraph(titulo.replace('\n', '<br/>'), st_title)]
    e.append(Paragraph('<b>CONTRATANTE:</b> ' + _fmt(
        modelo.get('contratante', CONTRATANTE_PADRAO), ctx), st_body))
    e.append(Paragraph('<b>CONTRATADA:</b> ' + _fmt(
        modelo.get('contratada', CONTRATADA_PADRAO), ctx), st_body))
    for par in modelo.get('preambulo', []):
        e.append(Paragraph(_fmt(par, ctx), st_body))
    for cl in modelo['clausulas']:
        bloco = [Paragraph(_fmt(cl['titulo'], ctx), st_head)]
        bloco += [Paragraph(_fmt(t, ctx), st_body) for t in cl['itens']]
        e.append(KeepTogether(bloco[:2]))
        e.extend(bloco[2:])
    e.append(Spacer(1, 8))
    e.append(Paragraph(_fmt(modelo.get('encerramento', (
        'E, por estarem justas e contratadas, as Partes assinam o presente '
        'instrumento, juntamente com 2 (duas) testemunhas, para que produza '
        'todos os efeitos jurídicos e legais.')), ctx), st_body))
    cidade = modelo.get('cidade', 'Rio de Janeiro')
    e.append(Paragraph(f'{cidade}, {ctx["data_assinatura"]}.', st_right))

    linha = '_' * 46
    assin = [
        Paragraph('<b>CONTRATANTE</b>', st_sig), Spacer(1, 22),
        Paragraph(linha, st_sig),
        Paragraph(ctx['posto_razao_social'], st_sig),
        Paragraph(f'CNPJ nº {ctx["posto_cnpj"]}', st_sig),
        Paragraph('Representante: _________________________________', st_sig),
        Spacer(1, 20),
        Paragraph('<b>CONTRATADA</b>', st_sig), Spacer(1, 22),
        Paragraph(linha, st_sig),
        Paragraph(ctx['razao_social'], st_sig),
        Paragraph(f'CNPJ nº {ctx["cnpj"]}', st_sig),
        Paragraph(f'Representante: {ctx["representante"]} — CPF nº {ctx["cpf"]}',
                  st_sig),
        Spacer(1, 20),
        Paragraph('<b>TESTEMUNHAS</b>', st_sig), Spacer(1, 6),
    ]
    tst = Table([
        [Paragraph('1. ' + '_' * 36, st_sig), Paragraph('2. ' + '_' * 36, st_sig)],
        [Paragraph('Nome:', st_sig), Paragraph('Nome:', st_sig)],
        [Paragraph('CPF nº', st_sig), Paragraph('CPF nº', st_sig)],
    ], colWidths=[8 * cm, 8 * cm])
    tst.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'),
                             ('TOPPADDING', (0, 0), (-1, -1), 6)]))
    e.append(KeepTogether(assin + [tst]))
    doc.build(e, onFirstPage=_rodape, onLaterPages=_rodape)
    return caminho
