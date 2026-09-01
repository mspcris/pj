"""Registro de boleto — caminho ÚNICO usado pelo upload do PJ, pelo cadastro
do admin e pelo robô da caixa pj@camim.com.br. Toda regra de substituição e
proteção contra duplicidade mora aqui.
"""
from ..models import Boleto, Prestador


def valor_esperado_para(prestador, posto):
    if prestador.modo_boleto == Prestador.ModoBoleto.UNICO:
        return prestador.valor_esperado_unico()
    vinculo = prestador.vinculos_ativos().filter(posto=posto).first()
    return vinculo.valor_mensal if vinculo else None


def registrar(prestador, competencia, enviado_por, posto=None, arquivo=None,
              nome_original='', linha_digitavel='', chave_pix='',
              valor_livre=False, observacao=''):
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
        valor_esperado=valor_esperado_para(prestador, posto),
        linha_digitavel=linha_digitavel, chave_pix=(chave_pix or '').strip(),
        valor_livre=valor_livre, observacao=(observacao or '').strip())


def duplicado_de(boleto):
    """Outro boleto da mesma chave já enviado p/ pagamento (ou pago)?"""
    return (Boleto.objects
            .filter(prestador=boleto.prestador, posto=boleto.posto,
                    competencia=boleto.competencia,
                    status__in=[Boleto.Status.APROVADO, Boleto.Status.PAGO])
            .exclude(pk=boleto.pk)
            .first())
