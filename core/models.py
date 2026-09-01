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
    ativo = models.BooleanField(default=True)

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
    ativo = models.BooleanField(default=True)
    observacao = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

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


class UsuarioPermitido(models.Model):
    """Whitelist de login. Quem não tem linha aqui (ativo=True) NÃO entra."""
    email = models.EmailField(unique=True)
    nome = models.CharField(max_length=120, blank=True)
    prestador = models.ForeignKey(Prestador, null=True, blank=True,
                                  on_delete=models.CASCADE,
                                  related_name='usuarios')
    is_admin = models.BooleanField(default=False)
    ativo = models.BooleanField(default=True)
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
    posto = models.ForeignKey(Posto, on_delete=models.CASCADE,
                              related_name='contratos')
    arquivo = models.FileField(upload_to=_upload_contrato)
    nome_original = models.CharField(max_length=255, blank=True)
    vigencia_inicio = models.DateField()
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
        if self.vigencia_inicio > hoje:
            return False
        return self.vigencia_fim is None or self.vigencia_fim >= hoje


class Boleto(models.Model):
    class Status(models.TextChoices):
        RECEBIDO = 'RECEBIDO', 'Recebido — aguardando verificação'
        APROVADO = 'APROVADO', 'Valor confere — enviado p/ pagamento'
        DIVERGENTE = 'DIVERGENTE', 'Valor divergente — aguardando contato'
        MANUAL = 'MANUAL', 'Verificar manualmente (IA não leu)'
        PAGO = 'PAGO', 'Pago'
        SUBSTITUIDO = 'SUBSTITUIDO', 'Substituído por novo arquivo'

    prestador = models.ForeignKey(Prestador, on_delete=models.CASCADE,
                                  related_name='boletos')
    # None quando o prestador emite boleto único (vale posto_cobranca).
    posto = models.ForeignKey(Posto, null=True, blank=True,
                              on_delete=models.SET_NULL, related_name='boletos')
    competencia = models.DateField(help_text='Sempre dia 1 do mês')
    arquivo = models.FileField(upload_to=_upload_boleto)
    nome_original = models.CharField(max_length=255, blank=True)
    enviado_por = models.EmailField(blank=True)

    # Dados de pagamento — preenchidos pelo admin (boleto que chegou pelo
    # zap) ou extraídos do PDF pela IA. Vão no e-mail para o pagador.
    linha_digitavel = models.CharField(max_length=60, blank=True)
    chave_pix = models.CharField(max_length=140, blank=True)

    status = models.CharField(max_length=12, choices=Status.choices,
                              default=Status.RECEBIDO)
    valor_esperado = models.DecimalField(max_digits=12, decimal_places=2,
                                         null=True, blank=True)
    valor_extraido = models.DecimalField(max_digits=12, decimal_places=2,
                                         null=True, blank=True)
    ia_resposta = models.TextField(blank=True)
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
