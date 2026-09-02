"""Contratos: o que dá para ler do PDF e aproveitar no cadastro."""
import logging

from ..models import AuditLog
from . import ia, pdf

log = logging.getLogger(__name__)

# Campos do prestador que a leitura do contrato pode preencher.
CAMPOS = ('dia_pagamento', 'dia_vencimento', 'cnpj', 'endereco', 'bairro',
          'cidade', 'uf', 'cep', 'representante', 'representante_cpf')


def _em_branco(prestador, campo):
    v = getattr(prestador, campo)
    if campo == 'representante':  # backfill antigo: igual ao nome = vazio
        return not v or v.strip().lower() == (prestador.nome or '').lower()
    return v in (None, '')


def aplicar_dados(contrato):
    """Ao anexar um contrato, lê prazos, endereço, representante e CPF.
    SÓ preenche o que está em branco no prestador — o que o Cristiano já
    digitou nunca é sobrescrito. Nunca levanta exceção."""
    prestador = contrato.prestador
    faltam = [c for c in CAMPOS if _em_branco(prestador, c)]
    if not faltam:
        return {}
    try:
        texto = pdf.extrair_texto(contrato.arquivo.path)
        if not texto or not texto.strip():
            return {}
        dados = ia.extrair_dados_contrato(texto)
    except Exception as e:
        log.warning('leitura do contrato #%s falhou: %s', contrato.pk, e)
        return {}
    if prestador.representante_nome_social and 'representante' in faltam:
        pass  # nome social continua mandando na saudação; só completa
    aplicados = {}
    for campo in faltam:
        v = dados.get(campo)
        if v:
            if campo == 'cep':
                d = ''.join(ch for ch in str(v) if ch.isdigit())
                if len(d) != 8:
                    continue
                v = f'{d[:5]}-{d[5:]}'
            if campo == 'representante_cpf':
                from ..forms import validar_cpf
                try:
                    v = validar_cpf(v)
                except Exception:
                    continue
                if not v:
                    continue
            setattr(prestador, campo, v)
            aplicados[campo] = v
    if aplicados:
        prestador.save(update_fields=list(aplicados))
        AuditLog.registrar(
            AuditLog.Evento.CRUD, ator='sistema',
            detalhe=f'{prestador}: lido do contrato '
                    f'{contrato.nome_original[:40]}: {aplicados}'
                    + (f' — "{dados["trecho"][:120]}"' if dados.get('trecho')
                       else ''))
    if dados.get('regime'):
        aplicados['regime_sugerido'] = dados['regime']
    return aplicados


aplicar_prazos = aplicar_dados  # nome antigo
