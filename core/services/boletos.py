"""Registro de boleto — caminho ÚNICO usado pelo upload do PJ, pelo cadastro
do admin e pelo robô da caixa pj@camim.com.br. Toda regra de substituição e
proteção contra duplicidade mora aqui.
"""
import re
import unicodedata

from ..models import Boleto, Posto, Prestador, Vale


def _norm(s):
    s = unicodedata.normalize('NFKD', s or '')
    return re.sub(r'\s+', ' ', s.encode('ascii', 'ignore').decode().upper())


def eh_nota_fiscal(texto):
    """Distingue NF de boleto: menção a nota fiscal SEM linha digitável.
    (Um PDF com linha digitável é boleto, mesmo que cite 'nota fiscal'.)
    O DANFSe nacional extrai texto sem espaços — comparo sem espaços."""
    t = _norm(texto).replace(' ', '')
    if not any(k in t for k in ('NOTAFISCAL', 'NFS-E', 'NFSE', 'DANFE',
                                'DANFSE')):
        return False
    for cand in re.findall(r'\d[\d .\-]{38,70}\d', texto or ''):
        if len(re.sub(r'\D', '', cand)) in (47, 48):
            return False
    return True


def validar_nf(texto_nf, prestador):
    """Validação determinística da NFS-e nacional: precisa parecer uma NF
    e o EMITENTE precisa ser o CNPJ do prestador (quando cadastrado).
    Retorna (ok, motivo)."""
    if not texto_nf:
        return True, 'NF sem texto legível — não validada'
    if not eh_nota_fiscal(texto_nf):
        return False, 'o anexo de nota fiscal não parece uma NFS-e'
    cnpj = re.sub(r'\D', '', prestador.cnpj or '')
    if cnpj and cnpj not in re.sub(r'\D', '', texto_nf):
        return False, ('a nota fiscal não é do prestador — o CNPJ '
                       f'{prestador.cnpj} não consta como emitente')
    return True, ''


def identificar_posto(texto):
    """Descobre o posto pelo CNPJ do sacado impresso no texto do boleto
    (match exato de dígitos — determinístico); fallback: razão social."""
    if not texto:
        return None
    digitos = re.sub(r'\D', '', texto)
    ativos = Posto.objects.filter(ativo=True, excluido_em__isnull=True)
    for p in ativos.exclude(cnpj=''):
        cnpj = re.sub(r'\D', '', p.cnpj)
        if cnpj and cnpj in digitos:
            return p
    texto_norm = _norm(texto)
    for p in ativos.exclude(razao_social=''):
        if _norm(p.razao_social) in texto_norm:
            return p
    return None


def vales_aplicaveis(prestador, posto, competencia):
    """[(vale, nº da parcela)] que abatem o boleto desta competência."""
    qs = Vale.objects.filter(prestador=prestador, ativo=True)
    if prestador.modo_boleto == Prestador.ModoBoleto.POR_POSTO:
        qs = qs.filter(posto=posto)
    achados = []
    for v in qs:
        n = v.parcela_em(competencia)
        if n is not None:
            achados.append((v, n))
    return achados


def valor_esperado_para(prestador, posto, competencia=None):
    if prestador.modo_boleto == Prestador.ModoBoleto.UNICO:
        base = prestador.valor_esperado_unico()
    else:
        vinculo = prestador.vinculos_ativos().filter(posto=posto).first()
        base = vinculo.valor_mensal if vinculo else None
    if base is None or competencia is None:
        return base
    for vale, _ in vales_aplicaveis(prestador, posto, competencia):
        base -= vale.valor_parcela
    return base


def registrar(prestador, competencia, enviado_por, posto=None, arquivo=None,
              nome_original='', linha_digitavel='', chave_pix='',
              valor_livre=False, observacao='', nota_fiscal=None,
              nota_fiscal_nome=''):
    """Cria o boleto. Substitui apenas pendências (RECEBIDO/DIVERGENTE/
    MANUAL) da mesma chave — um boleto já APROVADO ou PAGO NUNCA é
    substituído em silêncio: a duplicidade é barrada na verificação."""
    if prestador.modo_boleto == Prestador.ModoBoleto.UNICO:
        posto = None

    # Substituição só quando a chave é definida. No modo POR_POSTO com posto
    # ainda indefinido (ex.: vários PDFs no mesmo e-mail esperando
    # destinação), cada boleto é uma cobrança distinta — NÃO substitui.
    if not (prestador.modo_boleto == Prestador.ModoBoleto.POR_POSTO
            and posto is None):
        (Boleto.objects
         .filter(prestador=prestador, posto=posto, competencia=competencia,
                 status__in=[Boleto.Status.RECEBIDO,
                             Boleto.Status.DIVERGENTE,
                             Boleto.Status.MANUAL, Boleto.Status.DUPLICADO])
         .update(status=Boleto.Status.SUBSTITUIDO))

    return Boleto.objects.create(
        prestador=prestador, posto=posto, competencia=competencia,
        arquivo=arquivo, nome_original=(nome_original or '')[:255],
        enviado_por=enviado_por,
        valor_esperado=valor_esperado_para(prestador, posto, competencia),
        linha_digitavel=linha_digitavel, chave_pix=(chave_pix or '').strip(),
        valor_livre=valor_livre, observacao=(observacao or '').strip(),
        nota_fiscal=nota_fiscal,
        nota_fiscal_nome=(nota_fiscal_nome or '')[:255])


def duplicado_de(boleto):
    """Outro boleto da mesma chave já enviado p/ pagamento (ou pago)?"""
    return (Boleto.objects
            .filter(prestador=boleto.prestador, posto=boleto.posto,
                    competencia=boleto.competencia,
                    status__in=[Boleto.Status.APROVADO, Boleto.Status.PAGO])
            .exclude(pk=boleto.pk)
            .first())
