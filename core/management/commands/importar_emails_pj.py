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
from core.services import pdf as svc_pdf
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

            # 2ª passada: respostas do FINANCEIRO aos e-mails de pagamento
            # ("recebido") → status "Recebido pelo financeiro".
            busca = (f'from:{settings.EMAIL_PAGADOR} "Pagamento" '
                     f'newer_than:{settings.IMAP_DIAS}d')
            ok, dados = conn.uid('SEARCH', 'X-GM-RAW', f'"{busca}"')
            achados = (dados[0].split()
                       if ok == 'OK' and dados and dados[0] else [])
            self.stdout.write(f'respostas do financeiro: '
                              f'{len(achados)} e-mail(s).')
            for uid in sorted(achados, key=int):
                ok, dados = conn.uid('FETCH', uid, '(BODY.PEEK[])')
                if ok != 'OK' or not dados or dados[0] is None:
                    continue
                self._processar_resposta_financeiro(
                    email.message_from_bytes(dados[0][1]), probe)
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def _processar_resposta_financeiro(self, msg, probe):
        from django.utils import timezone
        from core.models import AuditLog, Boleto
        message_id = (msg.get('Message-ID') or '').strip()[:255]
        if not message_id:
            return
        if EmailRecebido.objects.filter(message_id=message_id).exists():
            return
        remetente = (email.utils.parseaddr(msg.get('From') or '')[1]
                     .strip().lower())
        assunto = _decodificar(msg.get('Subject'))[:255]
        if probe:
            self.stdout.write(f'[probe fin] {remetente} — {assunto}')
            return
        boleto = svc_boletos.localizar_boleto_por_assunto(assunto)
        detalhe = ''
        if boleto is not None and boleto.status == Boleto.Status.APROVADO:
            boleto.status = Boleto.Status.FIN_RECEBIDO
            boleto.fin_recebido_em = timezone.now()
            boleto.save(update_fields=['status', 'fin_recebido_em'])
            AuditLog.registrar(
                AuditLog.Evento.STATUS, ator='financeiro',
                detalhe=f'Boleto #{boleto.pk} confirmado recebido pelo '
                        f'financeiro ({remetente})')
            detalhe = f'#{boleto.pk}'
            self.stdout.write(self.style.SUCCESS(
                f'  financeiro confirmou: boleto #{boleto.pk} ({assunto[:60]})'))
        else:
            self.stdout.write(f'  resposta sem boleto casável: {assunto[:70]}')
        EmailRecebido.objects.create(
            message_id=message_id, remetente=remetente, assunto=assunto,
            resultado=EmailRecebido.Resultado.FIN, detalhe=detalhe)

    def _processar_mensagem(self, msg, probe):
        message_id = (msg.get('Message-ID') or '').strip()[:255]
        if not message_id:
            return
        registro = EmailRecebido.objects.filter(message_id=message_id).first()
        # Dedupe: já processado com sucesso (ou sem conteúdo aproveitável)
        # nunca repete. SEM_PRESTADOR fica em retentativa SILENCIOSA — no
        # dia em que o remetente entrar na whitelist, os boletos entram
        # sozinhos, sem o Cristiano precisar cadastrar um a um.
        if registro and registro.resultado != \
                EmailRecebido.Resultado.SEM_PRESTADOR:
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
            if registro:
                return  # já avisado antes; segue aguardando cadastro
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

        # Separa boletos de notas fiscais (quem manda, manda os dois juntos)
        pdfs_boleto, pdfs_nf = [], []
        for nome, conteudo in _pdfs(msg):
            texto = svc_pdf.extrair_texto_bytes(conteudo)
            if svc_boletos.eh_nota_fiscal(texto):
                pdfs_nf.append((nome, conteudo, texto))
            else:
                pdfs_boleto.append((nome, conteudo))

        for nome, conteudo in pdfs_boleto:
            b = svc_boletos.registrar(
                prestador, competencia, enviado_por=remetente, posto=posto,
                arquivo=ContentFile(conteudo, name=nome), nome_original=nome)
            criados.append(b)

        # Casa cada NF com o boleto certo: pelo CNPJ do posto no texto da
        # NF; senão, com o único boleto do e-mail; senão, com o 1º sem NF.
        for nome, conteudo, texto in pdfs_nf:
            alvo = None
            p = svc_boletos.identificar_posto(texto)
            if p is not None:
                alvo = next((b for b in criados
                             if b.posto_id == p.pk and not b.nota_fiscal),
                            None)
            if alvo is None:
                alvo = next((b for b in criados if not b.nota_fiscal), None)
            if alvo is not None:
                alvo.nota_fiscal = ContentFile(conteudo, name=nome)
                alvo.nota_fiscal_nome = nome[:255]
                alvo.save()
                self.stdout.write(f'  NF "{nome[:40]}" -> boleto #{alvo.pk}')
            else:
                self.stdout.write(f'  NF "{nome[:40]}" sem boleto para '
                                  'casar — ignorada')

        if not criados:
            linha = _linha_do_texto(_corpo_texto(msg))
            if linha:
                b = svc_boletos.registrar(
                    prestador, competencia, enviado_por=remetente,
                    posto=posto, linha_digitavel=linha)
                criados.append(b)

        if not criados:
            EmailRecebido.objects.update_or_create(
                message_id=message_id,
                defaults={'remetente': remetente, 'assunto': assunto,
                          'resultado': EmailRecebido.Resultado.SEM_CONTEUDO})
            svc_emails.enviar(
                settings.EMAIL_ADMIN,
                f'⚠️ E-mail de {prestador.nome} sem boleto legível',
                f'{remetente} mandou e-mail para '
                f'{settings.EMAIL_INTAKE_ALIASES[0]} (assunto: "{assunto}") sem '
                'PDF anexo e sem linha digitável no texto. Nada cadastrado.\n'
                '\nhttps://pj.camim.com.br/painel/')
            self.stdout.write(f'  SEM_CONTEUDO: {remetente}')
            return

        EmailRecebido.objects.update_or_create(
            message_id=message_id,
            defaults={'remetente': remetente, 'assunto': assunto,
                      'resultado': EmailRecebido.Resultado.BOLETO_CRIADO,
                      'detalhe': ', '.join(f'#{b.pk}' for b in criados)})
        if len(criados) == 1:
            enviar_recebido(criados[0])
        else:
            # Vários PDFs no mesmo e-mail → UM aviso só, não um por boleto.
            from core.services import frases
            from core.services.verificacao import (competencia_extenso,
                                                   destinatarios_pj)
            fatos = {'prestador': prestador.nome, 'alvo': 'vários postos',
                     'competencia': competencia_extenso(competencia),
                     'valor': '—', 'quantidade': len(criados)}
            svc_emails.enviar(
                destinatarios_pj(criados[0]),
                f'Boletos recebidos ({len(criados)}) — '
                f'{fatos["competencia"]}',
                frases.corpo(
                    'recebido', fatos,
                    instrucao_ia=(f'Escreva confirmando que recebemos os '
                                  f'{len(criados)} boletos enviados no '
                                  'e-mail e que serão verificados um a um '
                                  'em breve.')),
                boleto=criados[0])
        for b in criados:
            processar(b.pk)
        self.stdout.write(self.style.SUCCESS(
            f'  {len(criados)} boleto(s) de {prestador.nome} '
            f'({remetente}) cadastrado(s) e verificado(s).'))
