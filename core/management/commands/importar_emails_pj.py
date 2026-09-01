"""Robô das caixas de boleto — prestadores@camim.com.br (endereço oficial
comunicado aos PJs em 21/08/2026) e pj@camim.com.br. Quem não usar a
plataforma manda boleto por e-mail; este robô monitora e cadastra sozinho.

REGRAS DE OURO (mesmas do import_email_pjs.py do relatorio_h_t — a caixa é
a PESSOAL do Cristiano, não é caixa de robô):
  * SOMENTE LEITURA: select(readonly=True) + BODY.PEEK — nunca marca como
    lido, nunca move, nunca aplica marcador.
  * Seleção por X-GM-RAW deliveredto:<alias>, para cada alias em
    EMAIL_INTAKE_ALIASES (últimos IMAP_DIAS).
  * Dedupe por Message-ID (EmailRecebido.message_id é UNIQUE) — reprocessar
    a caixa inteira é seguro por construção.

O que faz com cada e-mail novo:
  * Remetente precisa estar na whitelist (UsuarioPermitido ativo com
    prestador) — senão avisa o admin e registra SEM_PRESTADOR.
  * Anexos PDF → um boleto por PDF, competência do mês atual.
  * Sem PDF → procura linha digitável no corpo (47/48 dígitos) → boleto sem
    arquivo. Sem nada → avisa o admin (SEM_CONTEUDO).
  * Cada boleto entra no MESMO fluxo do upload: "recebemos" + verificação
    (valor × código de barras × combinado, duplicidade, mês).

Cron sugerido (a cada 10 min):
    */10 * * * * cd /opt/pj && .venv/bin/python manage.py importar_emails_pj
"""
import email
import email.utils
import imaplib
import re
from email.header import decode_header, make_header

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import EmailRecebido, UsuarioPermitido
from core.services import boletos as svc_boletos
from core.services import emails as svc_emails
from core.services.verificacao import enviar_recebido, processar

RE_LINHA = re.compile(r'\d[\d .\-]{38,70}\d')


def _decodificar(valor):
    try:
        return str(make_header(decode_header(valor or '')))
    except Exception:
        return valor or ''


def _linha_do_texto(texto):
    for candidato in RE_LINHA.findall(texto or ''):
        digitos = re.sub(r'\D', '', candidato)
        if len(digitos) in (47, 48):
            return digitos
    return ''


def _corpo_texto(msg):
    partes = []
    for parte in msg.walk():
        if parte.get_content_type() == 'text/plain':
            try:
                partes.append(parte.get_payload(decode=True).decode(
                    parte.get_content_charset() or 'utf-8', errors='replace'))
            except Exception:
                pass
    return '\n'.join(partes)


def _pdfs(msg):
    achados = []
    for parte in msg.walk():
        nome = _decodificar(parte.get_filename() or '')
        if nome.lower().endswith('.pdf'):
            conteudo = parte.get_payload(decode=True)
            if conteudo and conteudo[:5] == b'%PDF-':
                achados.append((nome, conteudo))
    return achados


class Command(BaseCommand):
    help = 'Lê a caixa pj@camim.com.br (somente leitura) e cadastra boletos.'

    def add_arguments(self, parser):
        parser.add_argument('--probe', action='store_true',
                            help='Só lista o que faria, sem gravar nada.')

    def handle(self, *args, **opts):
        probe = opts['probe']
        conn = imaplib.IMAP4_SSL(settings.IMAP_HOST)
        conn.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
        try:
            conn.select('INBOX', readonly=True)
            uids = set()
            for alias in settings.EMAIL_INTAKE_ALIASES:
                busca = (f'deliveredto:{alias} '
                         f'newer_than:{settings.IMAP_DIAS}d')
                ok, dados = conn.uid('SEARCH', 'X-GM-RAW', f'"{busca}"')
                achados = (dados[0].split()
                           if ok == 'OK' and dados and dados[0] else [])
                self.stdout.write(f'{alias}: {len(achados)} e-mail(s).')
                uids.update(achados)
            for uid in sorted(uids, key=int):
                ok, dados = conn.uid('FETCH', uid, '(BODY.PEEK[])')
                if ok != 'OK' or not dados or dados[0] is None:
                    continue
                msg = email.message_from_bytes(dados[0][1])
                self._processar_mensagem(msg, probe)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _processar_mensagem(self, msg, probe):
        message_id = (msg.get('Message-ID') or '').strip()[:255]
        if not message_id:
            return
        if EmailRecebido.objects.filter(message_id=message_id).exists():
            return
        remetente = (email.utils.parseaddr(msg.get('From') or '')[1]
                     .strip().lower())
        assunto = _decodificar(msg.get('Subject'))[:255]

        if probe:
            self.stdout.write(f'[probe] {remetente} — {assunto}')
            return

        up = (UsuarioPermitido.objects
              .filter(email=remetente, ativo=True, prestador__isnull=False,
                      prestador__ativo=True)
              .select_related('prestador').first())
        if up is None:
            EmailRecebido.objects.create(
                message_id=message_id, remetente=remetente, assunto=assunto,
                resultado=EmailRecebido.Resultado.SEM_PRESTADOR)
            svc_emails.enviar(
                settings.EMAIL_ADMIN,
                f'⚠️ Boleto por e-mail de remetente NÃO cadastrado',
                f'Chegou e-mail em {settings.EMAIL_INTAKE_ALIASES[0]} de '
                f'{remetente} (assunto: "{assunto}"), mas esse endereço não '
                'está na whitelist de nenhum prestador. Nada foi cadastrado.\n'
                'Se for legítimo, cadastre o e-mail no painel e o robô pega '
                'na próxima passada.\n\nhttps://pj.camim.com.br/painel/')
            self.stdout.write(f'  SEM_PRESTADOR: {remetente}')
            return

        prestador = up.prestador
        vinculos = list(prestador.vinculos_ativos())
        posto = vinculos[0].posto if len(vinculos) == 1 else None
        competencia = timezone.localdate().replace(day=1)
        criados = []

        for nome, conteudo in _pdfs(msg):
            b = svc_boletos.registrar(
                prestador, competencia, enviado_por=remetente, posto=posto,
                arquivo=ContentFile(conteudo, name=nome), nome_original=nome)
            criados.append(b)

        if not criados:
            linha = _linha_do_texto(_corpo_texto(msg))
            if linha:
                b = svc_boletos.registrar(
                    prestador, competencia, enviado_por=remetente,
                    posto=posto, linha_digitavel=linha)
                criados.append(b)

        if not criados:
            EmailRecebido.objects.create(
                message_id=message_id, remetente=remetente, assunto=assunto,
                resultado=EmailRecebido.Resultado.SEM_CONTEUDO)
            svc_emails.enviar(
                settings.EMAIL_ADMIN,
                f'⚠️ E-mail de {prestador.nome} sem boleto legível',
                f'{remetente} mandou e-mail para '
                f'{settings.EMAIL_INTAKE_ALIASES[0]} (assunto: "{assunto}") sem '
                'PDF anexo e sem linha digitável no texto. Nada cadastrado.\n'
                '\nhttps://pj.camim.com.br/painel/')
            self.stdout.write(f'  SEM_CONTEUDO: {remetente}')
            return

        EmailRecebido.objects.create(
            message_id=message_id, remetente=remetente, assunto=assunto,
            resultado=EmailRecebido.Resultado.BOLETO_CRIADO,
            detalhe=', '.join(f'#{b.pk}' for b in criados))
        for b in criados:
            enviar_recebido(b)
            processar(b.pk)
        self.stdout.write(self.style.SUCCESS(
            f'  {len(criados)} boleto(s) de {prestador.nome} '
            f'({remetente}) cadastrado(s) e verificado(s).'))
