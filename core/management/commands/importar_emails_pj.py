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
  * Remetente precisa ser conhecido: UsuarioPermitido ativo com prestador
    OU e-mail em Prestador.emails_aviso (gente sem idCamim) — senão avisa
    o admin e registra SEM_PRESTADOR.
  * Competência: SEMPRE o mês atual (mês do pagamento), mesmo que o
    assunto cite o mês do serviço ("NF Guido Agosto" chega em setembro).
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
import urllib.parse
import urllib.request
from email.header import decode_header, make_header

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Boleto, EmailRecebido, UsuarioPermitido
from core.services import boletos as svc_boletos
from core.services import email_recebido as svc_recebido
from core.services import emails as svc_emails
from core.services import pdf as svc_pdf
from core.services.verificacao import enviar_recebido, processar

RE_LINHA = re.compile(r'\d[\d .\-]{38,70}\d')


def _decodificar(valor):
    try:
        return str(make_header(decode_header(valor or '')))
    except Exception:
        return valor or ''


def prestador_do_remetente(remetente):
    """Quem pode mandar boleto por e-mail: usuário ativo com login OU
    e-mail cadastrado em "e-mails do prestador sem login" (Guido, Rosana —
    gente sem idCamim). Qualquer outro remetente → aviso ao admin."""
    from core.models import Prestador
    up = (UsuarioPermitido.objects
          .filter(email=remetente, ativo=True, prestador__isnull=False,
                  prestador__ativo=True)
          .select_related('prestador').first())
    if up is not None:
        return up.prestador
    for p in Prestador.objects.filter(ativo=True, excluido_em__isnull=True,
                                      emails_aviso__icontains='@'):
        if remetente in p.lista_emails_aviso():
            return p
    return None


def classificar_pdf(nome, texto):
    """'nf' ou 'boleto'. Pelo texto (NFS-e) e, quando o PDF é escaneado
    (sem texto), pelo nome do arquivo ("NF Agosto Guido.pdf")."""
    if svc_boletos.eh_nota_fiscal(texto):
        return 'nf'
    if not (texto or '').strip() and re.search(
            r'\b(nf|nfs|nfe|nfs-e|nota)\b', nome or '', re.I):
        return 'nf'
    return 'boleto'


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


# PJ que manda o boleto por LINK do Adobe (acrobat.adobe.com/id/...) em vez de
# anexar: a página de visualização traz, no próprio HTML, a URL assinada (S3)
# do PDF. Baixa de lá e trata exatamente como um anexo.
RE_ADOBE = re.compile(
    r'https://acrobat\.adobe\.com/id/urn:aaid:sc:[^\s"\'<>)\]]+')
_UA = {'User-Agent': 'Mozilla/5.0'}


def _corpo_para_links(msg):
    """Texto p/ caçar links — junta text/plain E text/html (o link pode vir só
    no HTML). Separado de _corpo_texto (que alimenta a busca de linha
    digitável e não deve pegar tags)."""
    partes = []
    for parte in msg.walk():
        if parte.get_content_type() in ('text/plain', 'text/html'):
            try:
                partes.append(parte.get_payload(decode=True).decode(
                    parte.get_content_charset() or 'utf-8', errors='replace'))
            except Exception:
                pass
    return '\n'.join(partes)


def _baixar_pdf_adobe(link):
    """(nome, bytes) do PDF por trás de um link do Adobe, ou (None, None)."""
    req = urllib.request.Request(link, headers=_UA)
    html = urllib.request.urlopen(req, timeout=30).read().decode(
        'utf-8', 'ignore')
    m = re.search(r'https://acp-aep-cs-blobstore[^\s"\'\\<>]+?'
                  r'response-content-type=application%2Fpdf[^\s"\'\\<>]*', html)
    if not m:
        return None, None
    url = m.group(0)
    fn = re.search(r'filename%3D%22([^&]*?)%22', url)
    nome = (urllib.parse.unquote(urllib.parse.unquote(fn.group(1)))
            if fn else 'boleto-adobe.pdf')
    conteudo = urllib.request.urlopen(
        urllib.request.Request(url, headers=_UA), timeout=30).read()
    return nome, conteudo


def _pdfs_de_links_adobe(corpo):
    """PDFs baixados de links do Adobe no corpo — mesmo formato de _pdfs."""
    achados, vistos = [], set()
    for link in RE_ADOBE.findall(corpo or ''):
        if link in vistos:
            continue
        vistos.add(link)
        try:
            nome, conteudo = _baixar_pdf_adobe(link)
        except Exception:
            continue
        if conteudo and conteudo[:5] == b'%PDF-':
            achados.append(((nome or 'boleto-adobe.pdf')[:255], conteudo))
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
            # sem aspas internas: a query inteira já vai entre aspas no UID
            # SEARCH e aspas aninhadas quebram o parse do IMAP
            busca = (f'from:{settings.EMAIL_PAGADOR} subject:Pagamento '
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
            # Avisa o PJ: está com o financeiro, é só aguardar.
            from core.services import frases
            from core.services.verificacao import (_fatos, _instrucao_parcial,
                                                   _moeda, assunto_parcial,
                                                   cc_gerente, dados_pj,
                                                   destinatarios_pj)
            fatos = _fatos(boleto)
            if not boleto.parcial:
                fatos['valor'] = _moeda(boleto.valor_extraido
                                        or boleto.valor_esperado)
            svc_emails.enviar(
                destinatarios_pj(boleto),
                assunto_parcial(
                    fatos,
                    f'Boleto com o financeiro — {fatos["competencia"]}'),
                frases.corpo(
                    'fin_recebido', fatos,
                    instrucao_ia=('Escreva em tom FORMAL e positivo '
                                  'informando ao PRESTADOR que o setor '
                                  'financeiro da CAMIM confirmou o '
                                  'recebimento do boleto dele e que o '
                                  'pagamento (da CAMIM para o prestador) '
                                  'está em processamento — nenhuma ação é '
                                  'necessária, é só aguardar. NÃO cite '
                                  'valores no texto. Diga que os dados '
                                  'seguem abaixo da assinatura.'
                                  + _instrucao_parcial(fatos)))
                + dados_pj(boleto, fatos),
                boleto=boleto, cc=cc_gerente(boleto))
            self.stdout.write(self.style.SUCCESS(
                f'  financeiro confirmou: boleto #{boleto.pk} ({assunto[:60]})'))
        else:
            self.stdout.write(f'  resposta sem boleto casável: {assunto[:70]}')
        EmailRecebido.objects.create(
            message_id=message_id, remetente=remetente, assunto=assunto,
            resultado=EmailRecebido.Resultado.FIN, detalhe=detalhe,
            **svc_recebido.conteudo(msg))

    @staticmethod
    def _dedup_id(msg):
        """Chave de dedupe tirada do Message-ID. Outlook/Exchange emite
        Message-ID que COLIDE entre e-mails distintos (prefixo "<!&!AAAA..."):
        o do Caio (propagacaodigital) se repetia todo mês, então o robô
        jogava fora o boleto novo achando que já tinha visto. Para esses,
        o Message-ID sozinho não identifica o e-mail — junta Data + assunto
        para desempatar. E-mails normais seguem com o Message-ID puro, então
        nada do histórico é reprocessado."""
        mid = (msg.get('Message-ID') or '').strip()
        if not mid:
            return ''
        if mid.startswith('<!&!'):
            extra = ((msg.get('Date') or '').strip() + '|' +
                     (msg.get('Subject') or '').strip())
            mid = f'{mid}|{extra}'
        return mid[:255]

    def _processar_mensagem(self, msg, probe):
        message_id = self._dedup_id(msg)
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

        prestador = prestador_do_remetente(remetente)
        if prestador is None:
            if registro:
                return  # já avisado antes; segue aguardando cadastro
            EmailRecebido.objects.create(
                message_id=message_id, remetente=remetente, assunto=assunto,
                resultado=EmailRecebido.Resultado.SEM_PRESTADOR,
                **svc_recebido.conteudo(msg))
            svc_emails.enviar(
                settings.EMAIL_ADMIN,
                f'⚠️ Boleto por e-mail de remetente NÃO cadastrado',
                f'Chegou e-mail em {settings.EMAIL_INTAKE_ALIASES[0]} de '
                f'{remetente} (assunto: "{assunto}"), mas esse endereço não '
                'está na whitelist de nenhum prestador. Nada foi cadastrado.\n'
                'Se for legítimo, cadastre o e-mail no prestador (aba '
                'Prestadores) e o robô pega na próxima passada.\n\n'
                'Todos os e-mails não reconhecidos do mês:\n'
                'https://pj.camim.com.br/painel/emails/nao-reconhecidos/')
            self.stdout.write(f'  SEM_PRESTADOR: {remetente}')
            return

        vinculos = list(prestador.vinculos_ativos())
        posto = vinculos[0].posto if len(vinculos) == 1 else None
        # Competência = mês em que o boleto chega (é o mês do PAGAMENTO —
        # "NF Guido Agosto" que chega em setembro é a régua de setembro).
        competencia = timezone.localdate().replace(day=1)
        criados = []

        # Separa boletos de notas fiscais (quem manda, manda os dois juntos)
        pdfs_boleto, pdfs_nf = [], []
        anexos = _pdfs(msg)
        if not anexos:
            # Sem PDF anexo: o PJ pode ter mandado LINK do Adobe no corpo.
            anexos = _pdfs_de_links_adobe(_corpo_para_links(msg))
            if anexos:
                self.stdout.write(f'  {len(anexos)} PDF(s) baixado(s) de link '
                                  'do Adobe no corpo do e-mail')
        for nome, conteudo in anexos:
            texto = svc_pdf.extrair_texto_bytes(conteudo)
            if classificar_pdf(nome, texto) == 'nf':
                pdfs_nf.append((nome, conteudo, texto))
            else:
                pdfs_boleto.append((nome, conteudo, texto.strip()))
        # PDF escaneado (sem texto) junto de um boleto legível: é a NF
        # (Guido manda "NF Agosto.pdf" escaneada + boleto).
        if pdfs_boleto and len(pdfs_boleto) > 1:
            legiveis = [x for x in pdfs_boleto if x[2]]
            if legiveis:
                for x in [x for x in pdfs_boleto if not x[2]]:
                    pdfs_nf.append((x[0], x[1], ''))
                pdfs_boleto = legiveis
        # Vários postos: destina cada boleto JÁ AQUI pelo CNPJ do sacado
        # no PDF (04/09/2026: sem isso os boletos ficavam sem posto e as
        # NFs eram casadas na ordem dos anexos — 7 de 8 da JRA trocadas).
        for nome, conteudo, texto in pdfs_boleto:
            p = posto or svc_boletos.posto_do_boleto(prestador, texto)
            b = svc_boletos.registrar(
                prestador, competencia, enviado_por=remetente, posto=p,
                arquivo=ContentFile(conteudo, name=nome), nome_original=nome,
                origem=Boleto.Origem.EMAIL)
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
                    posto=posto, linha_digitavel=linha,
                    origem=Boleto.Origem.EMAIL)
                criados.append(b)

        if not criados:
            EmailRecebido.objects.update_or_create(
                message_id=message_id,
                defaults={'remetente': remetente, 'assunto': assunto,
                          'resultado': EmailRecebido.Resultado.SEM_CONTEUDO,
                          **svc_recebido.conteudo(msg)})
            svc_emails.enviar(
                settings.EMAIL_ADMIN,
                f'⚠️ E-mail de {prestador.nome} sem boleto legível',
                f'{remetente} mandou e-mail para '
                f'{settings.EMAIL_INTAKE_ALIASES[0]} (assunto: "{assunto}") sem '
                'PDF anexo e sem linha digitável no texto. Nada cadastrado.\n'
                '\nhttps://pj.camim.com.br/painel/')
            self.stdout.write(f'  SEM_CONTEUDO: {remetente}')
            return

        registro, _ = EmailRecebido.objects.update_or_create(
            message_id=message_id,
            defaults={'remetente': remetente, 'assunto': assunto,
                      'resultado': EmailRecebido.Resultado.BOLETO_CRIADO,
                      'detalhe': ', '.join(f'#{b.pk}' for b in criados),
                      **svc_recebido.conteudo(msg)})
        # Origem do registro: cada boleto aponta para o e-mail que o trouxe
        # (no objeto em memória também — o fluxo abaixo ainda o salva).
        for b in criados:
            b.email_origem = registro
            b.save(update_fields=['email_origem'])
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
