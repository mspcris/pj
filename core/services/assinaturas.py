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


def avisar_gestor_assinado(contratos):
    """E-mail ao admin: o prestador assinou (um contrato ou um lote);
    falta (ou não) a dele. Aceita um Contrato ou uma lista."""
    from django.conf import settings

    from . import emails
    if not isinstance(contratos, (list, tuple)):
        contratos = [contratos]
    if not contratos:
        return False
    prestador = contratos[0].prestador
    alvos = [c.posto.nome if c.posto else 'contrato geral' for c in contratos]
    alvo = ', '.join(alvos)
    url = f'{SITE_URL}/painel/assinaturas/?prestador={prestador.pk}'
    if any(c.falta_gestor for c in contratos):
        pede = ('Falta agora a assinatura do gestor: no painel, "Assinar em '
                'lote" baixa todos os pendentes num ZIP; assine no gov.br e '
                'suba todos de uma vez.')
    else:
        pede = 'Com isso o contrato está assinado pelas duas partes.'
    plural = len(contratos) > 1
    corpo = ('Prezado Cristiano,\n\n'
             f'{prestador.contato} ({prestador.nome}) acabou de anexar '
             f'{"os contratos" if plural else "o contrato"} de {alvo} '
             f'assinado{"s" if plural else ""} digitalmente pelo gov.br. '
             f'{pede}\n\n----------\n'
             f'Prestador: {prestador.nome}\n'
             f'Posto{"s" if plural else ""}: {alvo}\n'
             f'Situação: {contratos[0].situacao_assinatura}\n'
             f'Painel: {url}\n')
    assunto = (f'Contratos assinados pelo prestador ({len(contratos)}) — '
               f'{prestador.nome}' if plural else
               f'Contrato assinado pelo prestador — {prestador.nome} ({alvo})')
    return emails.enviar(settings.EMAIL_ADMIN, assunto, corpo)


# --------------------------------------------------------------------------
# LOTE (23/10/2026): baixar todos os pendentes num ZIP, assinar todos no
# gov.br e subir todos de uma vez. O casamento arquivo ↔ contrato é pelo
# CONTEÚDO: o gov.br grava a assinatura como atualização incremental, então
# o PDF assinado começa com os bytes exatos do PDF que foi baixado. Nome
# de arquivo é só o plano B (o gov.br troca espaço por "_", tira acento e
# põe "_assinado" no fim).
# --------------------------------------------------------------------------
def zip_de(contratos):
    """Bytes de um ZIP com o PDF atual de cada contrato (nome_original)."""
    import io
    import zipfile
    buf = io.BytesIO()
    usados = set()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for c in contratos:
            nome = c.nome_original or f'contrato-{c.pk}.pdf'
            if nome in usados:
                nome = f'{c.pk}-{nome}'
            usados.add(nome)
            z.writestr(nome, _bytes_do_campo(c.arquivo))
    return buf.getvalue()


def _normalizar_nome(nome):
    import unicodedata
    from urllib.parse import unquote
    n = unquote(nome or '').rsplit('/', 1)[-1]
    n = n[:-4] if n.lower().endswith('.pdf') else n
    n = unicodedata.normalize('NFKD', n).encode('ascii', 'ignore').decode()
    n = re.sub(r'[-_ ]?assinad[oa]([-_ ]?(prestador|gestor))?$', '', n,
               flags=re.I)
    return re.sub(r'[^a-z0-9]', '', n.lower())


def casar(contratos, arquivos):
    """Para cada (nome, bytes) em `arquivos`, acha o contrato de `contratos`
    cujo PDF atual é prefixo do arquivo; senão, casa pelo nome. Devolve
    lista de dicts {nome, dados, contrato|None, erro}."""
    atuais = [(c, _bytes_do_campo(c.arquivo)) for c in contratos]
    por_nome = {}
    for c, _ in atuais:
        por_nome.setdefault(_normalizar_nome(c.nome_original), []).append(c)
    saida, vistos = [], set()
    for nome, dados in arquivos:
        contrato, erro = None, ''
        for c, orig in atuais:
            if orig and dados.startswith(orig):
                contrato = c
                break
        if contrato is None:
            cands = por_nome.get(_normalizar_nome(nome), [])
            if len(cands) == 1:
                contrato = cands[0]
            elif len(cands) > 1:
                erro = 'nome bate com mais de um contrato'
            else:
                erro = ('não reconheci de qual contrato é este PDF (não '
                        'começa com o PDF baixado nem o nome confere)')
        if contrato is not None and contrato.pk in vistos:
            contrato, erro = None, 'outro arquivo do lote já é deste contrato'
        if contrato is not None:
            vistos.add(contrato.pk)
        saida.append({'nome': nome, 'dados': dados, 'contrato': contrato,
                      'erro': erro})
    return saida


def registrar_lote(contratos, arquivos, papel, quem='', request=None):
    """Casa, confere a assinatura e registra cada PDF do lote. Devolve
    (assinados, erros) — erros = [(nome, motivo)]. Quem chama já validou
    forma (PDF, tamanho) de cada arquivo."""
    from django.core.files.base import ContentFile
    assinados, erros = [], []
    for item in casar(contratos, arquivos):
        c = item['contrato']
        if c is None:
            erros.append((item['nome'], item['erro']))
            continue
        try:
            conferir_pdf_assinado(c, item['dados'])
        except PdfSemAssinaturaNova:
            erros.append((item['nome'], 'sem assinatura digital nova — é o '
                                        'PDF assinado pelo gov.br?'))
            continue
        nome = nome_assinado(c, papel)
        registrar(c, ContentFile(item['dados'], name=nome), nome, papel,
                  quem=quem, request=request)
        assinados.append(c)
    return assinados, erros
