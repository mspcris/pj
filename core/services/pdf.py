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


def primeira_pagina_png(caminho, resolucao=150):
    """1ª página do PDF como PNG (bytes) para a IA ENXERGAR o boleto quando
    não há camada de texto (vetorial/escaneado). b'' se não der. Usa o
    renderizador que JÁ vem com o pdfplumber (pypdfium2 + Pillow) — sem
    binário externo nem dependência nova no requirements."""
    import io
    try:
        import pdfplumber
        with pdfplumber.open(caminho) as arquivo:
            if not arquivo.pages:
                return b''
            imagem = arquivo.pages[0].to_image(resolution=resolucao)
            buf = io.BytesIO()
            imagem.original.save(buf, format='PNG')
            return buf.getvalue()
    except Exception as e:
        log.warning('render do PDF falhou em %s: %s', caminho, e)
        return b''
