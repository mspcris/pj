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
    # Qualificação do posto como CONTRATANTE nos contratos gerados pelo
    # comando `contratos_por_posto` (26/10/2026): razão social como está na
    # Receita (com LTDA) e sede completa. Vazio → usa razao_social e não
    # cita sede. Seed dos 8 postos do Altamiro na migração 0042.
    razao_social_contrato = models.CharField(
        max_length=200, blank=True,
        verbose_name='Razão social nos contratos (Receita)')
    sede = models.CharField(
        max_length=300, blank=True,
        verbose_name='Sede nos contratos (rua, nº, bairro, cidade/UF, CEP)')
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
    # Pessoa por trás da empresa (quem a gente trata nos e-mails). Nos
    # cadastros antigos é igual ao nome; RABISCO → Guido Cerqueira.
    representante = models.CharField(max_length=120, blank=True)
    # Nome social do representante: se preenchido, é como a pessoa é chamada.
    representante_nome_social = models.CharField(max_length=120, blank=True)
    # CPF do representante legal (só no cadastro — nunca vai em e-mail).
    representante_cpf = models.CharField(max_length=14, blank=True)
    # PJs que recebem como PESSOA FÍSICA: o boleto vem no CPF (o do
    # representante), não no CNPJ. Liga a conferência do FAVORECIDO pelo CPF.
    recebe_por_cpf = models.BooleanField(default=False)
    cnpj = models.CharField(max_length=20, blank=True)
    # Endereço da sede (a IA lê do contrato ao anexar; editável).
    endereco = models.CharField(max_length=200, blank=True,
                                verbose_name='Endereço (rua, nº, compl.)')
    bairro = models.CharField(max_length=80, blank=True)
    cidade = models.CharField(max_length=80, blank=True)
    uf = models.CharField(max_length=2, blank=True, verbose_name='UF')
    cep = models.CharField(max_length=9, blank=True, verbose_name='CEP')

    # Ficha da Receita (BrasilAPI, pelo CNPJ) — botão "Buscar na Receita".
    razao_social = models.CharField(max_length=200, blank=True,
                                    verbose_name='Razão social')
    nome_fantasia = models.CharField(max_length=200, blank=True)
    telefone = models.CharField(max_length=40, blank=True)
    email_empresa = models.EmailField(blank=True,
                                      verbose_name='E-mail da empresa')
    situacao_cadastral = models.CharField(max_length=60, blank=True)
    abertura = models.DateField(null=True, blank=True)
    socios = models.TextField(blank=True, help_text='um por linha')
    receita_consultado_em = models.DateTimeField(null=True, blank=True)

    @property
    def cnpj_digitos(self):
        return ''.join(c for c in (self.cnpj or '') if c.isdigit())

    @property
    def cnpj_formatado(self):
        d = self.cnpj_digitos
        if len(d) != 14:
            return self.cnpj
        return f'{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}'

    @property
    def lista_socios(self):
        return [l.strip() for l in (self.socios or '').splitlines()
                if l.strip()]

    @property
    def endereco_completo(self):
        partes = [self.endereco, self.bairro,
                  ' / '.join(x for x in (self.cidade, self.uf) if x),
                  f'CEP {self.cep}' if self.cep else '']
        return ' — '.join(x for x in partes if x)
    modo_boleto = models.CharField(max_length=10, choices=ModoBoleto.choices,
                                   default=ModoBoleto.POR_POSTO)
    # Modo UNICO: contra qual posto o boleto único é emitido e, se preenchido,
    # o valor combinado (vazio = soma dos valores por posto).
    posto_cobranca = models.ForeignKey(Posto, null=True, blank=True,
                                       on_delete=models.SET_NULL,
                                       related_name='+')
    valor_unico = models.DecimalField(max_digits=12, decimal_places=2,
                                      null=True, blank=True)

    class Regime(models.TextChoices):
        VIGENTE = 'VIGENTE', ('Pagamento no mês vigente — serviço e '
                              'pagamento no mesmo mês')
        POSTERIOR = 'POSTERIOR', ('Pagamento no mês seguinte à prestação '
                                  'do serviço')
    # Boleto.competencia é SEMPRE o mês do pagamento (a régua). O regime
    # diz qual foi o mês do serviço: o anterior (padrão — quase todos) ou
    # o mesmo (vigente — exceção, ex.: Erick).
    regime_pagamento = models.CharField(max_length=10, choices=Regime.choices,
                                        default=Regime.POSTERIOR)
    # Prazos do contrato (a IA tenta ler do PDF ao anexar; editáveis).
    dia_pagamento = models.PositiveSmallIntegerField(
        null=True, blank=True,
        verbose_name='Data prevista para pagamento (dia do mês)')
    dia_vencimento = models.PositiveSmallIntegerField(
        null=True, blank=True,
        verbose_name='Vencimento do boleto (dia do mês)')

    def dia_para_cobrar_boleto(self, mes):
        """Dia do `mes` a partir do qual o lembrete diário cobra "sem
        boleto": a VÉSPERA do vencimento (ou do dia de pagamento, se não
        há vencimento cadastrado), limitada ao último dia do mês. None =
        nenhum dia cadastrado (cobra o mês inteiro, como sempre foi).
        03/11/2026: "não faz sentido me avisar o mês inteiro que a
        Desenlike não anexou boleto — o vencimento é só dia 30"."""
        import calendar
        dia = self.dia_vencimento or self.dia_pagamento
        if not dia:
            return None
        ultimo = calendar.monthrange(mes.year, mes.month)[1]
        return mes.replace(day=max(1, min(dia, ultimo) - 1))

    def contrato_vigente_para(self, posto):
        """Contrato ativo e vigente do posto (o mais recente); sem contrato
        do posto, o contrato geral (sem posto). None se não há."""
        ativos = [c for c in self.contratos.filter(desativado_em__isnull=True)
                  if c.vigente]
        alvo = posto.pk if posto is not None else None
        do_posto = [c for c in ativos if c.posto_id == alvo]
        if not do_posto and alvo is not None:
            do_posto = [c for c in ativos if c.posto_id is None]
        return do_posto[0] if do_posto else None

    def regra_pagamento_para(self, posto):
        """(dia, dia_util, contrato): a regra do contrato vigente do posto —
        dia fixo ("até o dia 10") OU N-ésimo dia útil ("até o quinto dia
        útil", Victoria, 10/11/2026) — senão o dia do cadastro (contrato
        None). Contratos de postos diferentes podem fixar regras
        diferentes."""
        c = self.contrato_vigente_para(posto)
        if c is not None and c.dia_util_pagamento:
            return None, c.dia_util_pagamento, c
        if c is not None and c.dia_pagamento:
            return c.dia_pagamento, None, c
        return self.dia_pagamento, None, None

    def dia_pagamento_para(self, posto):
        dia, _, c = self.regra_pagamento_para(posto)
        return dia, c

    @staticmethod
    def _data_do_dia(mes, dia):
        if not dia:
            return None
        import calendar
        ultimo = calendar.monthrange(mes.year, mes.month)[1]
        return mes.replace(day=min(dia, ultimo))

    @staticmethod
    def _enesimo_dia_util(mes, n):
        """N-ésimo dia útil do mês (segunda a sexta; feriados não entram —
        o painel avisa). Passou do mês: último dia útil."""
        import calendar
        from datetime import timedelta
        ultimo = calendar.monthrange(mes.year, mes.month)[1]
        d, conta, achado = mes.replace(day=1), 0, None
        while d.day <= ultimo:
            if d.weekday() < 5:
                conta += 1
                achado = d
                if conta == n:
                    return d
            if d.day == ultimo:
                break
            d += timedelta(days=1)
        return achado

    @staticmethod
    def _data_da_regra(mes, dia, dia_util):
        if dia_util:
            return Prestador._enesimo_dia_util(mes, dia_util)
        return Prestador._data_do_dia(mes, dia)

    @staticmethod
    def texto_da_regra(dia, dia_util):
        if dia_util:
            return f'{dia_util}º dia útil'
        return f'dia {dia}' if dia else ''

    def pagamento_previsto_em(self, mes, posto=None):
        """Data prevista de pagamento no `mes` (mês de competência), pela
        regra do contrato do posto (dia fixo, limitado ao último dia do
        mês, ou N-ésimo dia útil) ou pelo dia do cadastro. None = sem dia.
        10/11/2026: o financeiro vai desligar o e-mail de pagamento, então
        a data que ia nele precisa aparecer no painel."""
        dia, dia_util, _ = self.regra_pagamento_para(posto)
        return self._data_da_regra(mes, dia, dia_util)

    def previsao_pagamento(self, mes):
        """Resumo para o painel do financeiro (10/11/2026): por posto
        (vínculos do mês) a data e a cláusula do contrato. `data`/`clausula`
        só quando todos os postos têm o MESMO dia (cláusula só se todas
        iguais); `distintos` quando os contratos fixam dias diferentes —
        aí o painel mostra a coluna por posto."""
        itens = []
        postos = [v.posto for v in self.vinculos_em(mes)
                  .select_related('posto').order_by('posto__nome')] or [None]
        for posto in postos:
            dia, dia_util, c = self.regra_pagamento_para(posto)
            itens.append({'posto': posto, 'dia': dia, 'dia_util': dia_util,
                          'data': self._data_da_regra(mes, dia, dia_util),
                          'regra': self.texto_da_regra(dia, dia_util),
                          'clausula': c.clausula_pagamento if c else ''})
        distintos = len({(i['dia'], i['dia_util']) for i in itens}) > 1
        # A IA grafa a mesma cláusula de jeitos diferentes ("CLÁUSULA 3ª,
        # item 3.2" × "Cláusula 3ª, item 3.2" — Caio, 10/11/2026): maiúscula
        # e minúscula são a mesma, e vale a grafia mais frequente.
        from collections import Counter
        clausulas = Counter(i['clausula'].replace('CLÁUSULA', 'Cláusula')
                            .replace('PARÁGRAFO', 'Parágrafo').strip()
                            for i in itens if i['clausula'])
        return {
            'itens': itens, 'distintos': distintos,
            'data': None if distintos else itens[0]['data'],
            'dia': None if distintos else itens[0]['dia'],
            'dia_util': None if distintos else itens[0]['dia_util'],
            'regra': '' if distintos else itens[0]['regra'],
            'clausula': (clausulas.most_common(1)[0][0]
                         if clausulas and not distintos else ''),
        }

    def mes_servico(self, competencia):
        """Mês em que o serviço foi prestado, dado o mês do pagamento."""
        if self.regime_pagamento == self.Regime.POSTERIOR:
            m = competencia.month - 1 or 12
            ano = competencia.year - (1 if m == 12 else 0)
            return competencia.replace(year=ano, month=m, day=1)
        return competencia.replace(day=1)
    # Só aceita boleto acompanhado de nota fiscal (modelo nacional NFS-e).
    exige_nf = models.BooleanField(
        default=False, verbose_name='Exigir nota fiscal anexa')
    ativo = models.BooleanField(default=True)
    observacao = models.TextField(blank=True)
    # Financeiro controla este prestador pelo painel: o pedido de pagamento
    # NÃO vai mais por e-mail à equipe@ (cancelamentos e correções vão).
    fin_sem_email = models.BooleanField(
        default=False,
        verbose_name='Financeiro acompanha pelo painel (sem e-mail de '
                     'pagamento)')
    # 10/11/2026: em vez de um e-mail por boleto, a equipe@ recebe UM
    # e-mail a cada 2 horas (cron email_agrupado) com todos os boletos
    # aprovados anexos — de todos os prestadores com esta opção. Só vale
    # com fin_sem_email desmarcado.
    fin_email_agrupado = models.BooleanField(
        default=False,
        verbose_name='Financeiro recebe e-mail agrupado (um e-mail a cada '
                     '2h com todos os boletos)')
    # E-mails do prestador SEM login (sem idCamim): recebem os avisos E o
    # robô aceita boleto vindo deles (Rosana, Guido). Vírgula separa.
    # Somam-se aos usuários ativos.
    emails_aviso = models.CharField(
        max_length=300, blank=True,
        help_text='Separe vários por vírgula')

    @property
    def contato(self):
        """Como chamar a pessoa nos e-mails: nome social > representante >
        nome da empresa."""
        return (self.representante_nome_social or self.representante
                or self.nome)

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

    def vinculos_em(self, mes=None):
        """Vínculos que CONTAM no mês `mes` (None = hoje): ativos, posto
        ativo e dentro da vigência (desde/até). 03/11/2026: mudança de
        cadastro vale "a partir de um mês" — o passado não muda mais."""
        from django.db.models import Q
        from django.utils import timezone
        mes = (mes or timezone.localdate()).replace(day=1)
        return (self.vinculos.filter(ativo=True, posto__ativo=True)
                .filter(Q(desde__isnull=True) | Q(desde__lte=mes))
                .filter(Q(ate__isnull=True) | Q(ate__gte=mes)))

    def vinculos_ativos(self):
        """Vínculos vigentes HOJE (atalho de vinculos_em())."""
        return self.vinculos_em()

    def valor_esperado_unico(self, mes=None):
        if self.valor_unico is not None:
            return self.valor_unico
        total = sum((v.valor_em(mes) for v in self.vinculos_em(mes)),
                    Decimal('0'))
        return total or None

    # Primeiro mês (de PAGAMENTO) em que a régua espera boleto. Vazio =
    # sempre. Lucas (23/10/2026): começou em outubro, 1º boleto em novembro
    # — sem isso o painel e o lembrete cobravam outubro.
    primeiro_mes = models.DateField(
        null=True, blank=True,
        verbose_name='Primeiro mês com boleto (antes disso a régua não cobra)')

    # Quem passou de boleto ÚNICO para POR POSTO: a partir de qual mês (de
    # pagamento) a régua espera um boleto por posto. Antes disso o mês
    # continua como boleto único (Caio e Victória, 26/10/2026: outubro já
    # foi pago num boleto só; os separados começam em novembro).
    por_posto_desde = models.DateField(
        null=True, blank=True,
        verbose_name='Um boleto por posto a partir de (mês de pagamento; '
                     'antes disso a régua trata como boleto único)')

    def modo_boleto_em(self, mes=None):
        """Modo de boleto que vale para a competência `mes` (None = hoje)."""
        if (mes is not None and self.por_posto_desde is not None
                and self.modo_boleto == self.ModoBoleto.POR_POSTO
                and mes.replace(day=1) < self.por_posto_desde.replace(day=1)):
            return self.ModoBoleto.UNICO
        return self.modo_boleto

    def boletos_esperados(self, mes=None):
        """Lista [(posto|None, valor_esperado)] do mês — a régua do dashboard.
        Vazia se `mes` vem antes do primeiro mês com boleto. Antes de
        `por_posto_desde`, uma linha só (boleto único, soma dos postos)."""
        if (mes is not None and self.primeiro_mes is not None
                and mes.replace(day=1) < self.primeiro_mes.replace(day=1)):
            return []
        if self.modo_boleto_em(mes) == self.ModoBoleto.UNICO:
            return [(self.posto_cobranca, self.valor_esperado_unico(mes))]
        return [(v.posto, v.valor_em(mes)) for v in
                self.vinculos_em(mes).select_related('posto')]


class PrestadorPosto(models.Model):
    """Vínculo prestador × posto com o valor mensal combinado.

    Vigência (03/11/2026): `desde`/`ate` dizem em quais meses o posto conta
    na régua; o valor de cada mês sai do histórico (VinculoValor). Mudar o
    cadastro "a partir de um mês" não reescreve os meses anteriores."""
    prestador = models.ForeignKey(Prestador, on_delete=models.CASCADE,
                                  related_name='vinculos')
    posto = models.ForeignKey(Posto, on_delete=models.CASCADE,
                              related_name='vinculos')
    # Valor vigente mais recente (o de cada mês está em valor_em()).
    valor_mensal = models.DecimalField(max_digits=12, decimal_places=2)
    ativo = models.BooleanField(default=True)
    desde = models.DateField(null=True, blank=True,
                             verbose_name='Conta a partir de (mês)')
    ate = models.DateField(null=True, blank=True,
                           verbose_name='Último mês em que conta')

    class Meta:
        unique_together = [('prestador', 'posto')]
        ordering = ['posto__nome']

    def __str__(self):
        return f'{self.prestador} @ {self.posto}'

    def vigente_em(self, mes):
        mes = mes.replace(day=1)
        return (self.ativo and self.posto.ativo
                and (self.desde is None or self.desde <= mes)
                and (self.ate is None or self.ate >= mes))

    def valor_em(self, mes=None):
        """Valor combinado no mês `mes` (None = hoje): a última mudança
        registrada até aquele mês; sem histórico, o valor do cadastro."""
        from django.db.models import F, Q
        from django.utils import timezone
        mes = (mes or timezone.localdate()).replace(day=1)
        row = (self.historico
               .filter(Q(desde__isnull=True) | Q(desde__lte=mes))
               .order_by(F('desde').desc(nulls_last=True), '-pk').first())
        return row.valor_mensal if row else self.valor_mensal

    def registrar_valor(self, valor, desde, quem=''):
        """Novo valor a partir do mês `desde`. Na primeira mudança, guarda
        o valor antigo como "desde sempre", para os meses anteriores
        continuarem iguais."""
        desde = desde.replace(day=1)
        if not self.historico.exists():
            VinculoValor.objects.create(vinculo=self, desde=None,
                                        valor_mensal=self.valor_mensal,
                                        registrado_por=quem)
        VinculoValor.objects.update_or_create(
            vinculo=self, desde=desde,
            defaults={'valor_mensal': valor, 'registrado_por': quem})
        from django.utils import timezone
        if desde <= timezone.localdate().replace(day=1):
            self.valor_mensal = valor
            self.save(update_fields=['valor_mensal'])

    def mudancas_futuras(self, mes=None):
        """[(mês, valor)] registrados para DEPOIS de `mes` (painel)."""
        from django.utils import timezone
        mes = (mes or timezone.localdate()).replace(day=1)
        return [(r.desde, r.valor_mensal) for r in
                self.historico.filter(desde__gt=mes).order_by('desde')]


class VinculoValor(models.Model):
    """Histórico do valor mensal de um vínculo: cada linha vale a partir
    de `desde` (None = desde sempre) até a linha seguinte."""
    vinculo = models.ForeignKey(PrestadorPosto, on_delete=models.CASCADE,
                                related_name='historico')
    desde = models.DateField(null=True, blank=True)
    valor_mensal = models.DecimalField(max_digits=12, decimal_places=2)
    registrado_por = models.CharField(max_length=255, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('vinculo', 'desde')]
        ordering = ['desde']

    def __str__(self):
        return (f'{self.vinculo} — R$ {self.valor_mensal} desde '
                f'{self.desde:%m/%Y}' if self.desde else
                f'{self.vinculo} — R$ {self.valor_mensal} (desde sempre)')


class AjusteDiferenca(models.Model):
    """Diferença pequena (< R$ 5) entre o combinado e o que veio em boleto,
    resolvida fora do boleto: paga em DINHEIRO ou simplesmente NÃO paga.
    Fecha a pendência da linha (prestador, posto, mês) na régua."""
    LIMITE = Decimal('5.00')

    class Modo(models.TextChoices):
        DINHEIRO = 'DINHEIRO', 'Pago em dinheiro'
        NAO_PAGO = 'NAO_PAGO', 'Não será pago'

    prestador = models.ForeignKey(Prestador, on_delete=models.CASCADE,
                                  related_name='ajustes')
    posto = models.ForeignKey(Posto, null=True, blank=True,
                              on_delete=models.CASCADE, related_name='+')
    competencia = models.DateField()
    valor = models.DecimalField(max_digits=12, decimal_places=2)
    modo = models.CharField(max_length=10, choices=Modo.choices)
    por = models.EmailField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('prestador', 'posto', 'competencia')]

    def __str__(self):
        return (f'{self.prestador} — {self.posto or "único"} — '
                f'{self.competencia:%m/%Y}: R$ {self.valor} '
                f'{self.get_modo_display().lower()}')


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
    # Equipe do FINANCEIRO (03/11/2026): vê só o painel /financeiro/ — os
    # boletos já aprovados — e dá ciência de recebimento e de envio para
    # pagamento por lá, sem transitar e-mail.
    is_financeiro = models.BooleanField(default=False,
                                        verbose_name='Financeiro')
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


class PreferenciaNotificacao(models.Model):
    """Cópia interna (oculta) por tipo de aviso, por pessoa.

    QUEM recebe cópia dos e-mails do sistema continua vindo de
    EMAIL_COPIA_OCULTA. Aqui cada observador desliga os tipos que não
    quer: SEM linha = recebe (padrão, tudo ligado); uma linha com
    recebe=False desliga aquele tipo para aquele e-mail. Os tipos são os
    de core.notificacoes (classificados pelo assunto)."""
    email = models.EmailField()
    tipo = models.CharField(max_length=20)
    recebe = models.BooleanField(default=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('email', 'tipo')]
        ordering = ['email', 'tipo']
        verbose_name = 'preferência de notificação'
        verbose_name_plural = 'preferências de notificação'

    def __str__(self):
        estado = 'recebe' if self.recebe else 'não recebe'
        return f'{self.email} · {self.tipo} · {estado}'

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
    # Assinatura digital pelo gov.br (23/10/2026): o contrato nasce SEM
    # assinatura; prestador e gestor, cada um, baixam o PDF atual, assinam
    # no assinador do gov.br e anexam de volta. `arquivo` é SEMPRE a versão
    # mais recente (com as assinaturas já colhidas); as versões anteriores
    # ficam em ContratoVersao — nada é apagado. Contratos antigos (anexados
    # já assinados no papel) ficam com assinatura_govbr=False e não entram
    # nessa régua.
    assinatura_govbr = models.BooleanField(
        default=False,
        verbose_name='Colher assinaturas digitais pelo gov.br')
    assinado_prestador_em = models.DateTimeField(null=True, blank=True)
    assinado_prestador_por = models.EmailField(blank=True)
    assinado_gestor_em = models.DateTimeField(null=True, blank=True)
    assinado_gestor_por = models.EmailField(blank=True)
    # Soft delete (regra do projeto): contrato desativado continua no banco
    # e no painel (cinza, selo "desativado"), mas o prestador não o vê, ele
    # não conta como vigente nem entra na régua de assinaturas. Reversível.
    desativado_em = models.DateTimeField(null=True, blank=True)
    desativado_por = models.EmailField(blank=True)
    # 10/11/2026: prazo de pagamento LIDO DESTE contrato (a IA lê ao
    # anexar; comando ler_prazos_contratos relê os antigos): dia, a
    # referência da cláusula ("Cláusula 5ª, Parágrafo 2º, alínea b") e o
    # trecho. Contratos de postos diferentes podem fixar dias diferentes.
    dia_pagamento = models.PositiveSmallIntegerField(null=True, blank=True)
    # "até o quinto dia útil do mês subsequente" (Victoria): N-ésimo dia
    # útil — exclui dia_pagamento.
    dia_util_pagamento = models.PositiveSmallIntegerField(null=True,
                                                          blank=True)
    clausula_pagamento = models.CharField(max_length=200, blank=True)
    trecho_pagamento = models.CharField(max_length=300, blank=True)
    prazos_lidos_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-vigencia_inicio']

    def __str__(self):
        return f'Contrato {self.prestador} @ {self.posto}'

    @property
    def desativado(self):
        return self.desativado_em is not None

    # --- régua de assinaturas -------------------------------------------
    @property
    def falta_prestador(self):
        return (self.assinatura_govbr and not self.desativado
                and self.assinado_prestador_em is None)

    @property
    def falta_gestor(self):
        return (self.assinatura_govbr and not self.desativado
                and self.assinado_gestor_em is None)

    @property
    def falta_assinatura(self):
        return self.falta_prestador or self.falta_gestor

    @property
    def assinaturas_completas(self):
        return (self.assinatura_govbr and self.assinado_prestador_em
                is not None and self.assinado_gestor_em is not None)

    @property
    def situacao_assinatura(self):
        """Texto curto para listas e e-mails."""
        if not self.assinatura_govbr:
            return ''
        if self.assinaturas_completas:
            return 'assinado por prestador e gestor'
        faltam = []
        if self.falta_prestador:
            faltam.append('prestador')
        if self.falta_gestor:
            faltam.append('gestor')
        return 'falta assinatura: ' + ' e '.join(faltam)

    @property
    def vigente(self):
        from django.utils import timezone
        if self.desativado:
            return False
        hoje = timezone.localdate()
        if self.vigencia_inicio and self.vigencia_inicio > hoje:
            return False
        return self.vigencia_fim is None or self.vigencia_fim >= hoje


class ContratoVersao(models.Model):
    """Versão ANTERIOR do PDF de um contrato, guardada quando uma versão
    assinada substitui a atual (mesmo princípio da NotaAnterior: o arquivo
    antigo nunca é apagado). Só `assinaturas.registrar` cria registros."""
    class Etapa(models.TextChoices):
        ORIGINAL = 'ORIGINAL', 'Sem assinatura (versão original)'
        PRESTADOR = 'PRESTADOR', 'Assinada pelo prestador'
        GESTOR = 'GESTOR', 'Assinada pelo gestor'

    contrato = models.ForeignKey(Contrato, on_delete=models.CASCADE,
                                 related_name='versoes')
    # Aponta para o arquivo que JÁ estava no contrato (nada é copiado).
    arquivo = models.FileField(upload_to='contratos/')
    nome = models.CharField(max_length=255, blank=True)
    # O que esta versão ERA quando foi substituída.
    etapa = models.CharField(max_length=10, choices=Etapa.choices,
                             default=Etapa.ORIGINAL)
    substituida_em = models.DateTimeField(auto_now_add=True)
    substituida_por = models.EmailField(blank=True)

    class Meta:
        ordering = ['substituida_em', 'pk']

    def __str__(self):
        return f'Versão anterior do contrato #{self.contrato_id} ({self.etapa})'


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
        # "Não reconheço esta dívida": o admin cancelou a cobrança e o
        # prestador foi avisado por e-mail. Fica no banco, com o PDF.
        NAO_RECONHECIDO = 'NAO_RECONHECIDO', ('Dívida não reconhecida — '
                                               'prestador avisado')
        # "Valor errado — devolver": a dívida existe, mas o boleto veio com
        # o valor errado. Sai da régua como se nada tivesse chegado (o
        # posto volta a "sem boleto"), fica no histórico com o PDF, e o
        # prestador é avisado do valor certo para emitir outro (03/11/2026,
        # Lucas: boleto de R$ 100 quando o esperado era R$ 110).
        # 10/11/2026: o FINANCEIRO também devolve, quando o boleto está
        # fora da data prevista de pagamento (devolvido_motivo diz qual).
        DEVOLVIDO = 'DEVOLVIDO', 'Devolvido ao prestador — emitir outro'

    class Origem(models.TextChoices):
        API = 'API', 'API'
        PORTAL = 'PORTAL', 'Plataforma'
        PAINEL = 'PAINEL', 'Plataforma (cadastro do admin)'
        EMAIL = 'EMAIL', 'E-mail'

    prestador = models.ForeignKey(Prestador, on_delete=models.CASCADE,
                                  related_name='boletos')
    # Por onde o boleto ENTROU (gravado na criação, nunca muda). Vazio só
    # em registro antigo cuja porta de entrada não deu para reconstituir.
    origem = models.CharField(max_length=8, choices=Origem.choices,
                              blank=True)
    # Origem E-MAIL: o e-mail que trouxe o boleto (dá para abrir no painel).
    email_origem = models.ForeignKey('EmailRecebido', null=True, blank=True,
                                     on_delete=models.SET_NULL,
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
    # PDF(s) retirados quando o boleto foi puxado de volta do financeiro por
    # valor errado: o Robson precisa reanexar o boleto certo. Guardamos o
    # quando e o porquê para o histórico (linha do tempo + auditoria).
    arquivo_removido_em = models.DateTimeField(null=True, blank=True)
    arquivo_removido_motivo = models.CharField(max_length=255, blank=True)
    # "Não reconheço esta dívida": quando o admin cancelou a cobrança e o
    # motivo que ele escreveu (vai no e-mail ao prestador e no histórico).
    nao_reconhecido_em = models.DateTimeField(null=True, blank=True)
    nao_reconhecido_motivo = models.CharField(max_length=500, blank=True)
    # Painel do financeiro (03/11/2026): quem deu ciência do recebimento e
    # quando o financeiro enviou para pagamento — 24h depois vira PAGO.
    fin_recebido_por = models.CharField(max_length=255, blank=True)
    fin_pagamento_em = models.DateTimeField(null=True, blank=True)
    fin_pagamento_por = models.CharField(max_length=255, blank=True)
    # "Valor errado — devolver": quando e por quê o boleto foi devolvido.
    devolvido_em = models.DateTimeField(null=True, blank=True)
    devolvido_motivo = models.CharField(max_length=500, blank=True)

    @property
    def pagamento_previsto(self):
        """Data prevista de pagamento deste boleto: contrato do posto (ou
        cadastro do prestador), no mês de competência."""
        return self.prestador.pagamento_previsto_em(self.competencia,
                                                    self.posto_efetivo)

    @property
    def clausula_pagamento(self):
        c = self.prestador.dia_pagamento_para(self.posto_efetivo)[1]
        return c.clausula_pagamento if c else ''

    @property
    def regra_pagamento(self):
        """'dia 10' ou '5º dia útil' — o que o contrato do posto fixa."""
        dia, dia_util, _ = self.prestador.regra_pagamento_para(
            self.posto_efetivo)
        return Prestador.texto_da_regra(dia, dia_util)

    @property
    def regra_dia_util(self):
        return bool(self.prestador.regra_pagamento_para(self.posto_efetivo)[1])

    @property
    def vence_antes_do_previsto(self):
        """Boleto fora da data prevista: vence ANTES do dia em que a CAMIM
        paga pelo contrato (10/11/2026 — o financeiro recusa e pede outro)."""
        previsto = self.pagamento_previsto
        return bool(self.vencimento and previsto
                    and self.vencimento < previsto)
    # Quando o PJ foi avisado de que o boleto chegou sem dizer de qual
    # unidade (empresa pagadora) é — pagamento em espera até ele informar.
    pedido_posto_em = models.DateTimeField(null=True, blank=True)
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

    status = models.CharField(max_length=16, choices=Status.choices,
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
    # Quem aprovou: 'sistema' (agente autônomo + IA) ou o e-mail/nome do
    # admin que aprovou no olho. Vai escrito no e-mail ao financeiro.
    aprovado_por = models.CharField(max_length=120, blank=True)

    # 10/11/2026: aviso ao prestador de que o boleto vence ANTES da data
    # prevista de pagamento do contrato (pode ser negado). Uma vez só.
    aviso_vencimento_em = models.DateTimeField(null=True, blank=True)
    # E-mail agrupado ao financeiro (10/11/2026): marcado na aprovação
    # quando o prestador está na opção; fin_agrupado_em = quando a rotina
    # o incluiu no e-mail. Pendente = fin_agrupar e sem fin_agrupado_em.
    fin_agrupar = models.BooleanField(default=False)
    fin_agrupado_em = models.DateTimeField(null=True, blank=True)
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

    ORIGEM_ICONE = {'API': '🔌', 'PORTAL': '🖥️', 'PAINEL': '🖥️', 'EMAIL': '✉️'}

    @property
    def origem_icone(self):
        return self.ORIGEM_ICONE.get(self.origem, '')

    @property
    def motivo_manual(self):
        """Último motivo registrado de queda para MANUAL (para o tooltip)."""
        for linha in reversed((self.ia_resposta or '').splitlines()):
            if linha.startswith('[manual]'):
                return linha[len('[manual]'):].strip()
        return ''

    @property
    def valor_corrigido_mao(self):
        """Última correção manual do valor (para avisar na tela/timeline)."""
        for linha in reversed((self.ia_resposta or '').splitlines()):
            if linha.startswith('[corrigido]'):
                return linha[len('[corrigido]'):].strip()
        return ''

    @property
    def linha_do_tempo(self):
        """Marcos REAIS da vida do boleto (só os que aconteceram), para o
        'Histórico da comunicação' no painel. Usa os campos já carregados e
        as notas substituídas (o painel faz prefetch de `notas_anteriores`).
        Valores vão como Decimal — o template formata."""
        ev = []
        if self.criado_em:
            quem = (f'enviado por {self.enviado_por}'
                    if self.enviado_por else 'cadastrado no painel')
            ev.append({
                'icone': '📥', 'titulo': 'Chegou no sistema',
                'quando': self.criado_em,
                'detalhe': (f'origem: {self.get_origem_display()} · {quem}'
                            if self.origem else quem)})
        if self.pedido_posto_em:
            ev.append({
                'icone': '❓', 'titulo': 'Pedida a empresa pagadora ao PJ',
                'quando': self.pedido_posto_em,
                'detalhe': 'boleto chegou sem dizer de qual unidade é — '
                           'pagamento em espera'})
        if self.verificado_em:
            if self.valor_corrigido_mao:
                det = self.valor_corrigido_mao
            elif self.ia_confianca is not None:
                det = f'confiança {self.ia_confianca}%'
            else:
                det = ''
            ev.append({
                'icone': '🔎', 'titulo': 'Valor conferido',
                'quando': self.verificado_em, 'detalhe': det,
                'valor': self.valor_extraido})
        if self.pagamento_enviado_em:
            ev.append({
                'icone': '📤',
                'titulo': 'Enviado p/ pagamento (financeiro)',
                'quando': self.pagamento_enviado_em,
                'detalhe': (f'aprovado por {self.aprovado_por}'
                            if self.aprovado_por else ''),
                'valor': self.pagamento_enviado_valor})
        if self.fin_recebido_em:
            ev.append({
                'icone': '🏦', 'titulo': 'Financeiro confirmou o recebimento',
                'quando': self.fin_recebido_em,
                'detalhe': self.fin_recebido_por})
        if self.fin_pagamento_em:
            ev.append({
                'icone': '💸', 'titulo': 'Financeiro enviou para pagamento '
                                         '(PAGO automático em 24h)',
                'quando': self.fin_pagamento_em,
                'detalhe': self.fin_pagamento_por})
        if self.fin_agrupado_em:
            ev.append({
                'icone': '📦',
                'titulo': 'Enviado ao financeiro no e-mail agrupado',
                'quando': self.fin_agrupado_em, 'detalhe': ''})
        if self.aviso_vencimento_em:
            ev.append({
                'icone': '📅',
                'titulo': 'Prestador avisado: boleto vence antes da data '
                          'prevista de pagamento (pode ser negado)',
                'quando': self.aviso_vencimento_em, 'detalhe': ''})
        if self.pago_em:
            ev.append({'icone': '✅', 'titulo': 'Pago',
                       'quando': self.pago_em, 'detalhe': ''})
        if self.arquivo_removido_em:
            ev.append({
                'icone': '📎', 'titulo': 'Boleto retirado — Robson deve reanexar',
                'quando': self.arquivo_removido_em,
                'detalhe': self.arquivo_removido_motivo})
        if self.nao_reconhecido_em:
            ev.append({
                'icone': '🚫',
                'titulo': 'Dívida não reconhecida — prestador avisado',
                'quando': self.nao_reconhecido_em,
                'detalhe': self.nao_reconhecido_motivo})
        if self.devolvido_em:
            ev.append({
                'icone': '↩️',
                'titulo': 'Boleto devolvido ao prestador para emitir outro',
                'quando': self.devolvido_em,
                'detalhe': self.devolvido_motivo})
        for n in self.notas_anteriores.all():
            ev.append({
                'icone': '🧾', 'titulo': 'Nota fiscal substituída por uma nova',
                'quando': n.substituida_em,
                'detalhe': ' · '.join(x for x in (
                    f'pela {n.get_via_display()}' if n.via else '',
                    n.substituida_por) if x),
                'nota_anterior': n.pk})
        ev.sort(key=lambda e: e['quando'])
        return ev


class NotaAnterior(models.Model):
    """Nota fiscal SUBSTITUÍDA por outra no mesmo boleto (regra do
    Cristiano, 02/10/2026): vale sempre a nota nova, mas o PDF antigo nunca
    é apagado — fica aqui, no histórico do boleto. Só `boletos.trocar_nota`
    cria estes registros."""
    boleto = models.ForeignKey(Boleto, on_delete=models.CASCADE,
                               related_name='notas_anteriores')
    # Aponta para o arquivo que JÁ estava no boleto (nada é copiado).
    arquivo = models.FileField(upload_to='boletos/')
    nome = models.CharField(max_length=255, blank=True)
    substituida_em = models.DateTimeField(auto_now_add=True)
    # Quem mandou a nota nova e por onde (API ou painel).
    substituida_por = models.CharField(max_length=255, blank=True)
    via = models.CharField(max_length=8, choices=Boleto.Origem.choices,
                           blank=True)

    class Meta:
        ordering = ['substituida_em', 'pk']

    def __str__(self):
        return f'Nota anterior do boleto #{self.boleto_id}'


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
        ASSINATURA_CONTRATO = 'ASSINATURA', 'Assinatura de contrato (gov.br)'
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
        # O PJ respondeu sobre um boleto que JÁ está cadastrado (a linha
        # digitável citada no corpo é a dele) — nada novo é criado; o
        # admin lê o que ele escreveu.
        RESPOSTA = 'RESPOSTA', 'Resposta do prestador (boleto já cadastrado)'

    message_id = models.CharField(max_length=255, unique=True)
    remetente = models.CharField(max_length=255)
    assunto = models.CharField(max_length=255, blank=True)
    resultado = models.CharField(max_length=10, choices=Resultado.choices)
    detalhe = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    # O e-mail como chegou, para abrir no painel ("origem: e-mail → ver o
    # e-mail"). Só TEXTO — HTML de fora nunca é exibido no painel. Os PDFs
    # não ficam aqui: viram o boleto/nota; guardamos só os nomes.
    para = models.CharField(max_length=255, blank=True)
    enviado_em = models.DateTimeField(null=True, blank=True)
    corpo = models.TextField(blank=True)
    anexos = models.TextField(blank=True, help_text='Um nome por linha')
    # Registro antigo (sem corpo): quando já se tentou buscar na caixa.
    corpo_buscado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return f'{self.remetente} — {self.get_resultado_display()}'

    def lista_anexos(self):
        return [a for a in self.anexos.splitlines() if a.strip()]


class EmailLog(models.Model):
    destinatario = models.CharField(max_length=255)
    assunto = models.CharField(max_length=255)
    corpo = models.TextField()
    boleto = models.ForeignKey(Boleto, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='emails')
    # RESPOSTA a um e-mail que chegou na caixa de boletos (03/11/2026: o
    # Elias mandou só a linha digitável, sem dizer de qual posto era, e o
    # robô respondeu pedindo a empresa pagadora). A tela "abrir o e-mail"
    # mostra as respostas — controle e auditoria do que foi dito ao PJ.
    em_resposta_a = models.ForeignKey(
        'EmailRecebido', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='respostas')
    ok = models.BooleanField(default=False)
    erro = models.TextField(blank=True)
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-criado_em']

    def __str__(self):
        return f'{self.destinatario} — {self.assunto}'
