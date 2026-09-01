"""Extração de texto de boleto PDF. Sem texto legível → verificação MANUAL."""
import logging

log = logging.getLogger(__name__)

MAX_CHARS = 12000  # boleto é 1-2 páginas; corta lixo de PDFs gigantes


def extrair_texto_bytes(dados):
    """Como extrair_texto, mas a partir dos bytes (anexo de e-mail)."""
    import io
    try:
        import pdfplumber
        partes = []
        with pdfplumber.open(io.BytesIO(dados)) as arquivo:
            for page in arquivo.pages[:4]:
                partes.append(page.extract_text() or '')
        return '\n'.join(partes).strip()[:MAX_CHARS]
    except Exception as e:
        log.warning('pdfplumber falhou em bytes: %s', e)
        return ''


def extrair_texto(caminho):
    """Retorna o texto do PDF ou '' se não der (imagem escaneada, corrompido)."""
    try:
        import pdfplumber
        partes = []
        with pdfplumber.open(caminho) as pdf:
            for page in pdf.pages[:4]:
                partes.append(page.extract_text() or '')
        texto = '\n'.join(partes).strip()
        return texto[:MAX_CHARS]
    except Exception as e:
        log.warning('pdfplumber falhou em %s: %s', caminho, e)
        return ''
