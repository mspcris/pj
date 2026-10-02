"""O conteúdo do e-mail que trouxe um boleto — para o painel mostrar a
ORIGEM do registro ("veio por e-mail → ver o e-mail").

Guarda só TEXTO (corpo em text/plain; se só veio HTML, as tags são
removidas) e os NOMES dos anexos. HTML de terceiros nunca é exibido no
painel. A caixa é a pessoal do Cristiano: aqui também é SOMENTE LEITURA
(select readonly + BODY.PEEK), igual ao robô importar_emails_pj.
"""
import datetime
import email
import email.utils
import html
import imaplib
import re
from email.header import decode_header, make_header

from django.conf import settings
from django.utils import timezone

MAX_CORPO = 20000


def _decodificar(valor):
    try:
        return str(make_header(decode_header(valor or '')))
    except Exception:
        return valor or ''


def _parte_texto(parte):
    try:
        return parte.get_payload(decode=True).decode(
            parte.get_content_charset() or 'utf-8', errors='replace')
    except Exception:
        return ''


def html_para_texto(fonte):
    """HTML → texto corrido (sem script/style, sem tags)."""
    fonte = re.sub(r'(?is)<(script|style|head)\b.*?</\1\s*>', ' ', fonte)
    fonte = re.sub(r'(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])\s*>', '\n', fonte)
    fonte = html.unescape(re.sub(r'(?s)<[^>]*>', '', fonte))
    linhas = [re.sub(r'[ \t\xa0]+', ' ', l).strip()
              for l in fonte.splitlines()]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(linhas)).strip()


def corpo_em_texto(msg):
    planas, htmls = [], []
    for parte in msg.walk():
        if parte.get_filename():
            continue
        tipo = parte.get_content_type()
        if tipo == 'text/plain':
            planas.append(_parte_texto(parte))
        elif tipo == 'text/html':
            htmls.append(_parte_texto(parte))
    texto = '\n'.join(planas).strip()
    if not texto:
        texto = html_para_texto('\n'.join(htmls))
    return texto.replace('\x00', '')[:MAX_CORPO]


def nomes_dos_anexos(msg):
    nomes = []
    for parte in msg.walk():
        nome = _decodificar(parte.get_filename() or '').strip()
        if nome:
            nomes.append(nome.replace('\n', ' ')[:200])
    return nomes


def conteudo(msg):
    """Campos de EmailRecebido tirados da mensagem (para create/defaults).
    Nunca levanta: guardar o texto é só para consulta e não pode impedir o
    robô de cadastrar o boleto."""
    try:
        return _conteudo(msg)
    except Exception:
        return {}


def _conteudo(msg):
    enviado_em = None
    try:
        enviado_em = email.utils.parsedate_to_datetime(msg.get('Date') or '')
        if timezone.is_naive(enviado_em):
            enviado_em = timezone.make_aware(enviado_em, datetime.timezone.utc)
    except Exception:
        enviado_em = None
    return {
        'para': _decodificar(msg.get('To'))[:255],
        'enviado_em': enviado_em,
        'corpo': corpo_em_texto(msg),
        'anexos': '\n'.join(nomes_dos_anexos(msg)),
    }


def _pasta_todos(conn):
    """Nome (como o servidor devolve) da pasta "Todos os e-mails" do Gmail
    — o e-mail pode ter sido arquivado. O nome muda com o idioma da conta;
    o atributo \\All não."""
    ok, linhas = conn.list()
    if ok != 'OK':
        return None
    for linha in linhas or []:
        txt = linha.decode('utf-8', 'replace') if isinstance(linha, bytes) \
            else str(linha)
        if '\\All' in txt:
            m = re.search(r'"((?:[^"\\]|\\.)*)"\s*$', txt)
            if m:
                return f'"{m.group(1)}"'
    return None


def _buscar_mensagem(conn, registro):
    """A mensagem do `registro` na pasta já selecionada, ou None."""
    # message_id de Outlook vem com "|data|assunto" colado (dedupe do robô)
    mid, _, resto = registro.message_id.partition('|')
    mid = mid.strip()
    if not mid or '"' in mid or '\\' in mid:
        return None
    ok, dados = conn.uid('SEARCH', 'HEADER', 'Message-ID', f'"{mid}"')
    uids = dados[0].split() if ok == 'OK' and dados and dados[0] else []
    data_pedida = resto.split('|')[0].strip() if resto else ''
    for uid in reversed(uids[-20:]):
        ok, dados = conn.uid('FETCH', uid, '(BODY.PEEK[])')
        if ok != 'OK' or not dados or dados[0] is None:
            continue
        msg = email.message_from_bytes(dados[0][1])
        if data_pedida and (msg.get('Date') or '').strip() != data_pedida:
            continue
        return msg
    return None


def buscar_na_caixa(registro):
    """Registro ANTIGO (gravado antes de guardarmos o corpo): busca o
    e-mail na caixa pelo Message-ID e completa o registro. Devolve True se
    achou. Tenta uma vez só por registro (marca `corpo_buscado_em`)."""
    achada = None
    conn = imaplib.IMAP4_SSL(settings.IMAP_HOST, timeout=20)
    try:
        conn.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
        conn.select('INBOX', readonly=True)
        achada = _buscar_mensagem(conn, registro)
        if achada is None:
            todos = _pasta_todos(conn)
            if todos and conn.select(todos, readonly=True)[0] == 'OK':
                achada = _buscar_mensagem(conn, registro)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    registro.corpo_buscado_em = timezone.now()
    campos = ['corpo_buscado_em']
    if achada is not None:
        for campo, valor in conteudo(achada).items():
            setattr(registro, campo, valor)
            campos.append(campo)
    registro.save(update_fields=campos)
    return achada is not None
