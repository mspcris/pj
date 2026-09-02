"""Contratos: o que dá para ler do PDF e aproveitar no cadastro."""
import logging

from ..models import AuditLog
from . import ia, pdf

log = logging.getLogger(__name__)


def aplicar_prazos(contrato):
    """Ao anexar um contrato, tenta ler dia de pagamento, vencimento e
    regime. SÓ preenche o que está em branco no prestador — o que o
    Cristiano já digitou nunca é sobrescrito. Nunca levanta exceção."""
    prestador = contrato.prestador
    faltam = [c for c in ('dia_pagamento', 'dia_vencimento')
              if getattr(prestador, c) is None]
    if not faltam and prestador.regime_pagamento:
        return {}
    try:
        texto = pdf.extrair_texto(contrato.arquivo.path)
        if not texto or not texto.strip():
            return {}
        prazos = ia.extrair_prazos_contrato(texto)
    except Exception as e:
        log.warning('leitura de prazos do contrato #%s falhou: %s',
                    contrato.pk, e)
        return {}
    aplicados = {}
    for campo in faltam:
        if prazos.get(campo):
            setattr(prestador, campo, prazos[campo])
            aplicados[campo] = prazos[campo]
    if aplicados:
        prestador.save(update_fields=list(aplicados))
        AuditLog.registrar(
            AuditLog.Evento.CRUD, ator='sistema',
            detalhe=f'{prestador}: prazos lidos do contrato '
                    f'{contrato.nome_original[:40]}: {aplicados} '
                    f'— "{prazos.get("trecho", "")[:120]}"')
    if prazos.get('regime'):
        aplicados['regime_sugerido'] = prazos['regime']
    return aplicados
