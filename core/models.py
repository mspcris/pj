"""
Modelos do Controle dos PJs.

Regra de ouro do fluxo de boleto: NADA é pago sem passar pelo dashboard —
o status conta a história inteira (recebido → aprovado/divergente → pago),
para o Cristiano nunca "perder a mão" e esquecer boleto.
"""
import uuid
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import models


def _upload_boleto(instance, filename):
    ext = Path(filename).suffix.lower() or '.pdf'
    return (f'boletos/{instance.competencia:%Y/%m}/'
            f'{uuid.uuid4().hex[:8]}-{instance.prestador_id}{ext}')


def _upload_contrato(instance, filename):
    ext = Path(filename).suffix.lower() or '.pdf'
    return f'contratos/{instance.prestador_id}/{uuid.uuid4().hex[:8]}{ext}'


class Posto(models.Model):
    """Postos da Camim — mesma régua do legado (Cad_Endereco): letra +
    id_endereco. Seed na migração 0002 com os 13 postos canônicos."""
    nome = models.CharField(max_length=120, unique=True)
    codigo = models.CharField(max_length=5, blank=True,
                              help_text='Letra do legado (A, B, C...)')
    id_endereco_legado = models.IntegerField(null=True, blank=True, unique=True)
    # Entidade jurídica do posto — usada para destinar boleto pelo CNPJ do
    # sacado impresso no PDF (determinístico, sem IA).
    razao_social = models.CharField(max_length=200, blank=True)
    cnpj = models.CharField(max_length=20, blank=True)
    # Gerente do posto — ESPELHO do cadastro de gestores do CRM (fonte
    # única, decisão de 10/08/2026). Editar no CRM; o sync_gerentes espelha.
    gerente_nome = models.CharField(max_length=120, blank=True)
    gerente_email = models.EmailField(blank=True)
    # Exceção ao espelho: quando o CRM tem mais de um gestor ativo e o
    # "menor id" não é o gerente de verdade (Anchieta: Elisangela é
    # subgerente, o gerente é o Júlio). Fixo = o sync NÃO mexe.
    gerente_fixo = models.BooleanField(default=False)
    ativo = models.BooleanField(default=True)
    # Soft delete — só para postos criados à mão; os 13 canônicos do legado
    # (com id_endereco_legado) nunca são excluídos, no máximo inativados.
    excluido_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['nome']

    def __str__(self):
        return self.nome


class Prestador(models.Model):
    """Empresa PJ que presta serviço em um ou mais postos."""

    class ModoBoleto(models.TextChoices):
        POR_POSTO = 'POR_POSTO', 'Um boleto por posto'
        UNICO = 'UNICO', 'Um boleto único (contra um posto)'

    nome = models.CharField(max_length=160)
    cnpj = models.CharField(max_length=20, blank=True)
    modo_boleto = models.CharField(max_length=10, choices=ModoBoleto.choices,
                                   default=ModoBoleto.POR_POSTO)
    # Modo UNICO: contra qual posto o boleto único é emitido e, se preenchido,
    # o valor combinado (vazio = soma dos valores por posto).
    posto_cobranca = models.ForeignKey(Posto, null=True, blank=True,
                                       on_delete=models.SET_NULL,
                                       related_name='+')
    valor_unico = models.DecimalField(max_digits=12, decimal_places=2,
                                      null=True, blank=True)
    # Só aceita boleto acompanhado de nota fiscal (modelo nacional NFS-e).
    exige_nf = models.BooleanField(
        default=False, verbose_name='Exigir nota fiscal anexa')
    ativo = models.BooleanField(default=True)
    observacao = models.TextField(blank=True)
    # Quem recebe os avisos (recebido/aprovado/financeiro) SEM ter login —
    # ex.: Rosana, que manda boleto pelo zap e só quer o e-mail de retorno.
    # Vários: separados por vírgula. Somam-se aos usuários ativos.
    emails_aviso = models.CharField(
        max_length=300, blank=True,
        help_text='Separe vários por vírgula')

    def lista_emails_aviso(self):
        return [e.strip().lower() for e in (self.emails_aviso or '')
                .replace(';', ',').split(',') if '@' in e]
    criado_em = models.DateTimeField(auto_now_add=True)
    # Soft delete (regra do projeto: NUNCA delete físico — histórico de
    # pagamento é auditável para sempre). Excluído some das listas, mas
    # boletos, contratos e usuários continuam no banco.
    excluido_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['nome']

    def __str__(self):
        return self.nome

    def vinculos_ativos(self):
        return self.vinculos.filter(ativo=True, posto__ativo=True)

    def valor_esperado_unico(self):
        if self.valor_unico is not None:
            return self.valor_unico
        total = sum((v.valor_mensal for v in self.vinculos_ativos()),
                    Decimal('0'))
        return total or None

    def boletos_esperados(self):
        """Lista [(posto|None, valor_esperado)] do mês — a régua do dashboard."""
        if self.modo_boleto == self.ModoBoleto.UNICO:
            return [(self.posto_cobranca, self.valor_esperado_unico())]
        return [(v.posto, v.valor_mensal) for v in
                self.vinculos_ativos().select_related('posto')]


class PrestadorPosto(models.Model):
    """Vínculo prestador × posto com o valor mensal combinado."""
    prestador = models.ForeignKey(Prestador, on_delete=models.CASCADE,
                                  related_name='vinculos')
    posto = models.ForeignKey(Posto, on_delete=models.CASCADE,
                              related_name='vinculos')
    valor_mensal = models.DecimalField(max_digits=12, decimal_places=2)
    ativo = models.BooleanField(default=True)

    class Meta:
        unique_together = [('prestador', 'posto')]
        ordering = ['posto__nome']

    def __str__(self):
        return f'{self.prestador} @ {self.posto}'


class Vale(models.Model):
    """Adiantamento/empréstimo descontado em parcelas do boleto mensal.
    Ex.: notebook que a Camim pagou pela GP5, descontado em 7× de R$ 600.
    O valor esperado do boleto do posto (ou do boleto único) já sai com a
    parcela do mês abatida, e o e-mail do financeiro cita o desconto."""
    prestador = models.ForeignKey(Prestador, on_delete=models.CASCADE,
                                  related_name='vales')
    # No modo POR_POSTO, de qual boleto o desconto sai; no UNICO, ignore.
    posto = models.ForeignKey(Posto, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name='+')
    descricao = models.CharField(max_length=160)
    valor_parcela = models.DecimalField(max_digits=12, decimal_places=2)
    parcelas_total = models.PositiveSmallIntegerField()
    primeira_competencia = models.DateField(
        help_text='Mês da parcela 1 (dia 1)')
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return (f'{self.descricao} — {self.parcelas_total}× '
                f'R$ {self.valor_parcela} ({self.prestador})')

    def parcela_em(self, competencia):
        """Nº da parcela nesta competência, ou None se fora do período."""
        n = ((competencia.year - self.primeira_competencia.year) * 12
             + competencia.month - self.primeira_competencia.month + 1)
        return n if 1 <= n <= self.parcelas_total else None

    def competencia_da(self, n):
        """Competência (dia 1) da parcela nº n."""
        from datetime import date
        m = self.primeira_competencia.month - 1 + (n - 1)
        return date(self.primeira_competencia.year + m // 12, m % 12 + 1, 1)

    def parcelas_pendentes(self):
        """Parcelas ainda NÃO consumidas por um boleto aprovado/pago da
        competência — a parcela some daqui quando o boleto com o desconto
        passa. Cada item: {n, competencia, atual, atrasada}."""
        from django.utils import timezone
        mes_atual = timezone.localdate().replace(day=1)
        consumo = Boleto.objects.filter(
            prestador=self.prestador, extra=False,
            status__in=[Boleto.Status.APROVADO, Boleto.Status.PAGO])
        if (self.prestador.modo_boleto == Prestador.ModoBoleto.POR_POSTO
                and self.posto_id):
            consumo = consumo.filter(posto_id=self.posto_id)
        meses_pagos = set(consumo.values_list('competencia', flat=True))
        pendentes = []
        for n in range(1, self.parcelas_total + 1):
            comp = self.competencia_da(n)
            if comp in meses_pagos:
                continue
            pendentes.append({'n': n, 'competencia': comp,
                              'atual': comp == mes_atual,
                              'atrasada': comp < mes_atual})
        return pendentes


class UsuarioPermitido(models.Model):
    """Whitelist de login. Quem não tem linha aqui (ativo=True) NÃO entra."""
    email = models.EmailField(unique=True)
    nome = models.CharField(max_length=120, blank=True)
    prestador = models.ForeignKey(Prestador, null=True, blank=True,
                                  on_delete=models.CASCADE,
                                  related_name='usuarios')
    is_admin = models.BooleanField(default=False)
    ativo = models.BooleanField(default=True)
    # Token da API (Authorization: Bearer ...) — gerado no painel Usuários,
    # exibido uma única vez. Para revogar, gere outro ou bloqueie o usuário.
    api_token = models.CharField(max_length=64, null=True, blank=True,
                                 unique=True)
    api_token_criado_em = models.DateTimeField(null=True, blank=True)
    ultimo_login = models.DateTimeField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['email']
        verbose_name = 'usuário permitido'
        verbose_name_plural = 'usuários permitidos'

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        self.email = (self.email or '').strip().lower()
        super().save(*args, **kwargs)


class Contrato(models.Model):
    prestador = models.ForeignKey(Prestador, on_delete=models.CASCADE,
                                  related_name='contratos')
    # None = contrato geral da empresa (ou razão social sem posto mapeado)
    posto = models.ForeignKey(Posto, null=True, blank=True,
                              on_delete=models.SET_NULL,
                              related_name='contratos')
    arquivo = models.FileField(upload_to=_upload_contrato)
    nome_original = models.CharField(max_length=255, blank=True)
    vigencia_inicio = models.DateField(null=True, blank=True)
    vigencia_fim = models.DateField(null=True, blank=True)
    enviado_por = models.EmailField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-vigencia_inicio']

    def __str__(self):
        return f'Contrato {self.prestador} @ {self.posto}'

    @property
    def vigente(self):
        from django.utils import timezone
        hoje = timezone.localdate()
        if self.vigencia_inicio and self.vigencia_inicio > hoje:
            return False
        return self.vigencia_fim is None or self.vigencia_fim >= hoje


class Boleto(models.Model):
    class Status(models.TextChoices):
        RECEBIDO = 'RECEBIDO', 'Recebido — aguardando verificação'
        APROVADO = 'APROVADO', 'Valor confere — enviado p/ pagamento'
        FIN_RECEBIDO = 'FIN_RECEBIDO', 'Recebido pelo financeiro'
        DIVERGENTE = 'DIVERGENTE', 'Valor divergente — aguardando contato'
        MANUAL = 'MANUAL', 'Verificar manualmente'
        PAGO = 'PAGO', 'Pago'
        SUBSTITUIDO = 'SUBSTITUIDO', 'Substituído por novo arquivo'
        DUPLICADO = 'DUPLICADO', 'Duplicado — competência já aprovada/paga'
        DESCARTADO = 'DESCARTADO', 'Descartado pelo admin'

    prestador = models.ForeignKey(Prestador, on_delete=models.CASCADE,
                                  related_name='boletos')
    # None quando o prestador emite boleto único (vale posto_cobranca).
    posto = models.ForeignKey(Posto, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name='boletos')
    competencia = models.DateField(help_text='Sempre dia 1 do mês')
    # Pode não ter PDF (ex.: veio só a linha digitável pelo zap/e-mail) —
    # nesse caso o valor é conferido pelo próprio código de barras.
    arquivo = models.FileField(upload_to=_upload_boleto, null=True, blank=True)
    nome_original = models.CharField(max_length=255, blank=True)
    enviado_por = models.EmailField(blank=True)

    # Dados de pagamento — preenchidos pelo admin (boleto que chegou pelo
    # zap) ou extraídos do PDF pela IA. Vão no e-mail para o pagador.
    linha_digitavel = models.CharField(max_length=60, blank=True)
    chave_pix = models.CharField(max_length=140, blank=True)
    # Nota fiscal que acompanha o boleto — vai junto no e-mail do financeiro.
    nota_fiscal = models.FileField(upload_to=_upload_boleto, null=True,
                                   blank=True)
    nota_fiscal_nome = models.CharField(max_length=255, blank=True)
    vencimento = models.DateField(null=True, blank=True)
    # Só o admin liga isto (cadastro direto): aceita o valor do boleto mesmo
    # diferente do combinado — único caminho para pagar valor MAIOR.
    valor_livre = models.BooleanField(default=False)
    # Cobrança extra/avulsa (ex.: ajuda de custo): convive com o boleto
    # normal do mesmo posto/mês — fora da régua e da trava de duplicidade.
    extra = models.BooleanField(default=False)
    # Boleto PARCIAL: N boletos compõem UM pagamento (a mensalidade do
    # posto). A soma das parciais é conferida contra o combinado; nenhum
    # substitui/duplica o outro.
    parcial = models.BooleanField(default=False)
    # Anotações do mês (ex.: "descontada parcela 3/7 do notebook — R$ 600").
    # Vai no bloco de dados do e-mail p/ o financeiro.
    observacao = models.TextField(blank=True)

    status = models.CharField(max_length=14, choices=Status.choices,
                              default=Status.RECEBIDO)
    # Quando o financeiro respondeu "recebido" ao e-mail de pagamento
    fin_recebido_em = models.DateTimeField(null=True, blank=True)
    # TRAVA contra pagar duas vezes: quando (e com que valor) o e-mail
    # "Pagamento — …" foi à equipe@. Reaprovação com o mesmo valor NÃO
    # reenvia; valor diferente vai como CORREÇÃO.
    pagamento_enviado_em = models.DateTimeField(null=True, blank=True)
    pagamento_enviado_valor = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True)
    valor_esperado = models.DecimalField(max_digits=12, decimal_places=2,
                                         null=True, blank=True)
    valor_extraido = models.DecimalField(max_digits=12, decimal_places=2,
                                         null=True, blank=True)
    ia_resposta = models.TextField(blank=True)
    # Confiança (0-100) que a IA declarou na extração do valor. 100 quando a
    # conferência é determinística (código de barras) ou dupla (PDF × linha).
    ia_confianca = models.PositiveSmallIntegerField(null=True, blank=True)
    tentativas = models.PositiveSmallIntegerField(default=0)

    criado_em = models.DateTimeField(auto_now_add=True)
    verificado_em = models.DateTimeField(null=True, blank=True)
    pago_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-competencia', 'prestador__nome']

    def __str__(self):
        alvo = self.posto or self.prestador.posto_cobranca or 'único'
        return f'{self.prestador} — {alvo} — {self.competencia:%m/%Y}'

    @property
    def posto_efetivo(self):
        return self.posto or self.prestador.posto_cobranca

    @property
    def motivo_manual(self):
        """Último motivo registrado de queda para MANUAL (para o tooltip)."""
        for linha in reversed((self.ia_resposta or '').splitlines()):
            if linha.startswith('[manual]'):
                return linha[len('[manual]'):].strip()
        return ''


class Configuracao(models.Model):
    """Configurações do sistema (menu Configurações do painel)."""
    chave = models.CharField(max_length=60, unique=True)
    valor = models.CharField(max_length=255)
    atualizado_em = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.chave}={self.valor}'

    @classmethod
    def get_int(cls, chave, padrao):
        try:
            return int(cls.objects.get(chave=chave).valor)
        except (cls.DoesNotExist, ValueError):
            return padrao

    @classmethod
    def definir(cls, chave, valor):
        cls.objects.update_or_create(chave=chave,
                                     defaults={'valor': str(valor)})


class AuditLog(models.Model):
    """Trilha de auditoria: logins (ok e negados), uploads, downloads,
    mudanças de status. Boleto é dinheiro — tudo fica registrado."""

    class Evento(models.TextChoices):
        LOGIN_OK = 'LOGIN_OK', 'Login OK'
        LOGIN_NEGADO = 'LOGIN_NEGADO', 'Login negado'
        UPLOAD_BOLETO = 'UP_BOLETO', 'Upload de boleto'
        UPLOAD_CONTRATO = 'UP_CONTRATO', 'Upload de contrato'
        DOWNLOAD = 'DOWNLOAD', 'Download de arquivo'
        DOWNLOAD_NEGADO = 'DL_NEGADO', 'Download negado'
        STATUS = 'STATUS', 'Mudança de status'
        CRUD = 'CRUD', 'Cadastro alterado'

    evento = models.CharField(max_length=12, choices=Evento.choices)
    ator_email = models.CharField(max_length=255, blank=True)
    detalhe = models.TextField(blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return f'{self.get_evento_display()} — {self.ator_email}'

    @classmethod
    def registrar(cls, evento, request=None, ator='', detalhe=''):
        ip = None
        if request is not None:
            ip = (request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip()
                  or request.META.get('REMOTE_ADDR'))
            if not ator and getattr(request, 'user', None) and request.user.is_authenticated:
                ator = request.user.email
        try:
            cls.objects.create(evento=evento, ator_email=ator or '',
                               detalhe=detalhe[:2000], ip=ip or None)
        except Exception:  # auditoria nunca derruba o fluxo principal
            pass


class EmailRecebido(models.Model):
    """Dedupe da caixa pj@camim.com.br — cada e-mail processado uma vez só
    (Message-ID é UNIQUE, mesma regra do import_email_pjs do relatorio_h_t)."""

    class Resultado(models.TextChoices):
        BOLETO_CRIADO = 'BOLETO', 'Boleto(s) criado(s)'
        SEM_PRESTADOR = 'SEM_PREST', 'Remetente sem cadastro'
        SEM_CONTEUDO = 'SEM_CONT', 'Sem PDF e sem linha digitável'
        FIN = 'FIN', 'Confirmação do financeiro'

    message_id = models.CharField(max_length=255, unique=True)
    remetente = models.CharField(max_length=255)
    assunto = models.CharField(max_length=255, blank=True)
    resultado = models.CharField(max_length=10, choices=Resultado.choices)
    detalhe = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return f'{self.remetente} — {self.get_resultado_display()}'


class EmailLog(models.Model):
    destinatario = models.CharField(max_length=255)
    assunto = models.CharField(max_length=255)
    corpo = models.TextField()
    boleto = models.ForeignKey(Boleto, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='emails')
    ok = models.BooleanField(default=False)
    erro = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return f'{self.destinatario} — {self.assunto}'
