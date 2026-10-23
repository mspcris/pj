"""Assinatura digital de contratos pelo gov.br.

O fluxo é o mesmo para prestador e gestor: baixa o PDF ATUAL do contrato,
assina no assinador do gov.br (assinador.iti.br) e anexa o PDF assinado de
volta. O arquivo do contrato passa a ser a versão assinada; a anterior vai
para o histórico (ContratoVersao) — nada é apagado.

Guarda contra o erro mais provável (anexar o PDF SEM assinar): o PDF novo
precisa ter MAIS assinaturas digitais que o atual. A contagem é
estrutural (campos /Sig com /ByteRange no PDF — é assim que o PAdES do
gov.br fica gravado), não uma validação criptográfica: quem garante a
autenticidade é o próprio gov.br (https://validar.iti.gov.br).
"""
import logging
import re

from django.db import transaction
from django.utils import timezone

from ..models import AuditLog, ContratoVersao

log = logging.getLogger(__name__)

URL_ASSINADOR = 'https://assinador.iti.br'
URL_VALIDAR = 'https://validar.iti.gov.br'
SITE_URL = 'https://pj.camim.com.br'

PAPEIS = ('prestador', 'gestor')

_RE_BYTERANGE = re.compile(rb'/ByteRange\s*\[')
_RE_SIG = re.compile(rb'/Type\s*/Sig\b')


def contar_assinaturas(dados):
    """Quantas assinaturas digitais o PDF (bytes) tem. 0 se nenhuma ou se
    não der para ler. Nunca levanta exceção."""
    if not dados:
        return 0
    try:
        n = _contar_via_pdfminer(dados)
        if n:
            return n
    except Exception as e:  # pragma: no cover - depende do PDF
        log.debug('pdfminer não contou assinaturas: %s', e)
    # Fallback: o dicionário de assinatura PAdES vai em atualização
    # incremental, em texto claro — /ByteRange aparece uma vez por
    # assinatura.
    try:
        return max(len(_RE_BYTERANGE.findall(dados)),
                   len(_RE_SIG.findall(dados)))
    except Exception:  # pragma: no cover
        return 0


def _contar_via_pdfminer(dados):
    import io

    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdftypes import resolve1

    doc = PDFDocument(PDFParser(io.BytesIO(dados)))
    catalogo = doc.catalog
    acro = resolve1(catalogo.get('AcroForm')) if catalogo else None
    if not acro:
        return 0
    n = 0
    for ref in resolve1(acro.get('Fields')) or []:
        campo = resolve1(ref)
        if not isinstance(campo, dict):
            continue
        ft = resolve1(campo.get('FT'))
        if getattr(ft, 'name', ft) == 'Sig' and campo.get('V') is not None:
            n += 1
    return n


def _bytes_do_campo(campo):
    try:
        with campo.open('rb') as f:
            return f.read()
    except Exception as e:
        log.warning('não li o arquivo atual do contrato: %s', e)
        return b''


class PdfSemAssinaturaNova(Exception):
    """O PDF anexado não tem assinatura digital a mais que o atual."""


def conferir_pdf_assinado(contrato, dados_novo):
    """Levanta PdfSemAssinaturaNova se o PDF novo não trouxer assinatura
    digital além das que o contrato já tem. Devolve (atuais, novas)."""
    atuais = contar_assinaturas(_bytes_do_campo(contrato.arquivo))
    novas = contar_assinaturas(dados_novo)
    if novas <= atuais:
        raise PdfSemAssinaturaNova(atuais, novas)
    return atuais, novas


def registrar(contrato, arquivo, nome, papel, quem='', request=None):
    """Guarda a versão atual no histórico, põe `arquivo` como o PDF do
    contrato e marca a assinatura de `papel` ('prestador' | 'gestor').
    Quem chama já validou o arquivo (forma e assinatura)."""
    assert papel in PAPEIS, papel
    agora = timezone.now()
    if contrato.assinado_gestor_em and contrato.assinado_prestador_em:
        etapa = ContratoVersao.Etapa.GESTOR
    elif contrato.assinado_gestor_em:
        etapa = ContratoVersao.Etapa.GESTOR
    elif contrato.assinado_prestador_em:
        etapa = ContratoVersao.Etapa.PRESTADOR
    else:
        etapa = ContratoVersao.Etapa.ORIGINAL
    with transaction.atomic():
        if contrato.arquivo:
            v = ContratoVersao(contrato=contrato, nome=contrato.nome_original,
                               etapa=etapa, substituida_por=(quem or '')[:254])
            v.arquivo.name = contrato.arquivo.name
            v.save()
        contrato.arquivo = arquivo
        contrato.nome_original = (nome or '')[:255]
        setattr(contrato, f'assinado_{papel}_em', agora)
        setattr(contrato, f'assinado_{papel}_por', (quem or '')[:254])
        contrato.save(update_fields=['arquivo', 'nome_original',
                                     f'assinado_{papel}_em',
                                     f'assinado_{papel}_por'])
    AuditLog.registrar(
        AuditLog.Evento.ASSINATURA_CONTRATO, request, ator=quem,
        detalhe=f'Contrato #{contrato.pk} {contrato} assinado pelo {papel} '
                f'({contrato.situacao_assinatura})')
    return contrato


def nome_assinado(contrato, papel):
    """Nome de arquivo da versão assinada (o gov.br devolve nomes
    genéricos; aqui fica claro de quem é e em que etapa está)."""
    base = (contrato.nome_original or 'contrato.pdf').rsplit('.', 1)[0]
    base = re.sub(r'[-_ ]?assinad[oa][-_a-z]*$', '', base, flags=re.I)
    return f'{base}-assinado-{papel}.pdf'[:255]


# --------------------------------------------------------------------------
# Avisos por e-mail — texto determinístico e formal; sem dados sensíveis.
# --------------------------------------------------------------------------
def avisar_prestador_para_assinar(contratos, de=None):
    """E-mail ao PJ: há contrato(s) esperando a assinatura dele. Um e-mail
    só, mesmo que sejam vários contratos (um por posto)."""
    from . import emails, frases
    contratos = [c for c in contratos if c.falta_prestador]
    if not contratos:
        return False
    prestador = contratos[0].prestador
    dests = ([u.email for u in prestador.usuarios.filter(ativo=True)]
             + prestador.lista_emails_aviso())
    if not dests:
        return False
    postos = [c.posto.nome for c in contratos if c.posto]
    fatos = {'contato': prestador.contato, 'prestador': prestador.nome,
             'quantos': len(contratos),
             'postos': ', '.join(postos) if postos else 'a empresa',
             'url': SITE_URL + '/contratos/',
             'assinador': URL_ASSINADOR}
    corpo = frases.corpo('contrato_assinar', fatos)
    corpo += ('\n\n----------\n'
              f'Prestador: {prestador.nome}\n'
              f'Contratos a assinar: {len(contratos)}\n'
              f'Postos: {fatos["postos"]}\n'
              f'Onde assinar: {fatos["url"]}\n')
    assunto = (f'Contrato para assinatura — {prestador.nome}'
               if len(contratos) == 1 else
               f'Contratos para assinatura ({len(contratos)}) — '
               f'{prestador.nome}')
    return emails.enviar(dests, assunto, corpo, de=de)


def avisar_gestor_assinado(contrato):
    """E-mail ao admin: o prestador assinou; falta (ou não) a dele."""
    from django.conf import settings

    from . import emails
    prestador = contrato.prestador
    alvo = contrato.posto.nome if contrato.posto else 'contrato geral'
    url = f'{SITE_URL}/painel/prestadores/{prestador.pk}/#contratos'
    if contrato.falta_gestor:
        pede = ('Falta agora a assinatura do gestor: baixe o PDF já '
                'assinado pelo prestador no painel, assine no gov.br e '
                'anexe de volta.')
    else:
        pede = 'Com isso o contrato está assinado pelas duas partes.'
    corpo = ('Prezado Cristiano,\n\n'
             f'{prestador.contato} ({prestador.nome}) acabou de anexar o '
             f'contrato de {alvo} assinado digitalmente pelo gov.br. {pede}'
             '\n\n----------\n'
             f'Prestador: {prestador.nome}\n'
             f'Posto: {alvo}\n'
             f'Situação: {contrato.situacao_assinatura}\n'
             f'Painel: {url}\n')
    return emails.enviar(settings.EMAIL_ADMIN,
                         f'Contrato assinado pelo prestador — '
                         f'{prestador.nome} ({alvo})', corpo)
