"""Formulários. Upload é validado com rigor: extensão, assinatura e tamanho."""
from datetime import date
from decimal import Decimal, InvalidOperation

from django import forms

from .models import Contrato, Posto, Prestador, UsuarioPermitido

MAX_UPLOAD = 15 * 1024 * 1024  # 15 MB
_EXT_BOLETO = ('.pdf',)
_EXT_CONTRATO = ('.pdf', '.jpg', '.jpeg', '.png')


def _validar_arquivo(arq, extensoes):
    nome = (arq.name or '').lower()
    if not nome.endswith(extensoes):
        raise forms.ValidationError(
            f'Arquivo deve ser {", ".join(e.upper().strip(".") for e in extensoes)}.')
    if arq.size > MAX_UPLOAD:
        raise forms.ValidationError('Arquivo muito grande (máximo 15 MB).')
    if nome.endswith('.pdf'):
        inicio = arq.read(5)
        arq.seek(0)
        if inicio != b'%PDF-':
            raise forms.ValidationError(
                'Este arquivo não parece ser um PDF válido.')
    return arq


def competencias_opcoes():
    """Últimos 3 meses + atual + próximo. Atual pré-selecionado."""
    hoje = date.today()
    meses = []
    ano, mes = hoje.year, hoje.month + 1
    if mes > 12:
        ano, mes = ano + 1, 1
    for _ in range(5):
        meses.append(date(ano, mes, 1))
        ano, mes = (ano, mes - 1) if mes > 1 else (ano - 1, 12)
    return list(reversed(meses))


class BoletoForm(forms.Form):
    competencia = forms.ChoiceField(label='Mês do boleto')
    posto = forms.ModelChoiceField(label='Posto', queryset=Posto.objects.none(),
                                   required=False, empty_label=None)
    arquivo = forms.FileField(label='Arquivo do boleto (PDF)')
    nota_fiscal = forms.FileField(
        label='Nota fiscal (PDF — opcional)', required=False)

    def __init__(self, prestador, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prestador = prestador
        from .services.verificacao import competencia_extenso
        hoje = date.today().replace(day=1)
        self.fields['competencia'].choices = [
            (m.isoformat(), competencia_extenso(m).capitalize())
            for m in competencias_opcoes()]
        self.fields['competencia'].initial = hoje.isoformat()
        if prestador.exige_nf:
            self.fields['nota_fiscal'].required = True
            self.fields['nota_fiscal'].label = 'Nota fiscal (PDF)'
        if prestador.modo_boleto == Prestador.ModoBoleto.POR_POSTO:
            qs = Posto.objects.filter(
                vinculos__prestador=prestador, vinculos__ativo=True,
                ativo=True).distinct()
            self.fields['posto'].queryset = qs
            self.fields['posto'].required = True
            if qs.count() == 1:
                self.fields['posto'].initial = qs.first()
        else:
            del self.fields['posto']

    def clean_arquivo(self):
        return _validar_arquivo(self.cleaned_data['arquivo'], _EXT_BOLETO)

    def clean_nota_fiscal(self):
        nf = self.cleaned_data.get('nota_fiscal')
        return _validar_arquivo(nf, _EXT_CONTRATO) if nf else None

    def clean_competencia(self):
        try:
            d = date.fromisoformat(self.cleaned_data['competencia'])
        except ValueError:
            raise forms.ValidationError('Mês inválido.')
        return d.replace(day=1)


class BoletoAdminForm(forms.Form):
    """Cadastro de boleto pelo admin (ex.: boleto que chegou pelo zap)."""
    prestador = forms.ModelChoiceField(label='Prestador',
                                       queryset=Prestador.objects.none())
    posto = forms.ModelChoiceField(label='Posto (se um boleto por posto)',
                                   queryset=Posto.objects.none(),
                                   required=False)
    competencia = forms.ChoiceField(label='Mês do boleto')
    arquivo = forms.FileField(
        label='Arquivo do boleto (PDF ou imagem — opcional se tiver linha '
              'digitável)',
        required=False)
    nota_fiscal = forms.FileField(
        label='Nota fiscal (PDF ou imagem — opcional)', required=False)
    linha_digitavel = forms.CharField(
        label='Linha digitável', required=False,
        widget=forms.TextInput(attrs={'inputmode': 'numeric',
                                      'placeholder': '47 ou 48 dígitos'}))
    chave_pix = forms.CharField(label='Chave PIX (opcional)', required=False)
    valor_livre = forms.BooleanField(
        label='Aceitar este valor mesmo diferente do combinado '
              '(acordo/ajuste — único caminho para valor MAIOR)',
        required=False)
    extra = forms.BooleanField(
        label='Cobrança EXTRA/avulsa — convive com o boleto normal do mês '
              '(ex.: ajuda de custo, reembolso)', required=False)
    parcial = forms.BooleanField(
        label='Boleto PARCIAL — vários boletos compõem a mensalidade do '
              'posto (a soma é conferida contra o combinado)',
        required=False)
    observacao = forms.CharField(
        label='Observação do mês (vai no e-mail do financeiro)',
        required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .services.verificacao import competencia_extenso
        self.fields['prestador'].queryset = Prestador.objects.filter(ativo=True)
        self.fields['posto'].queryset = Posto.objects.filter(ativo=True)
        hoje = date.today().replace(day=1)
        self.fields['competencia'].choices = [
            (m.isoformat(), competencia_extenso(m).capitalize())
            for m in competencias_opcoes()]
        self.fields['competencia'].initial = hoje.isoformat()

    def clean_arquivo(self):
        arq = self.cleaned_data.get('arquivo')
        # Admin pode anexar imagem (print do zap/e-mail); a conferência aí
        # sai pela linha digitável e a imagem vai de anexo p/ o financeiro.
        return _validar_arquivo(arq, _EXT_CONTRATO) if arq else None

    def clean_nota_fiscal(self):
        nf = self.cleaned_data.get('nota_fiscal')
        return _validar_arquivo(nf, _EXT_CONTRATO) if nf else None

    def clean_competencia(self):
        try:
            return date.fromisoformat(
                self.cleaned_data['competencia']).replace(day=1)
        except ValueError:
            raise forms.ValidationError('Mês inválido.')

    def clean_linha_digitavel(self):
        ld = ''.join(c for c in self.cleaned_data['linha_digitavel']
                     if c.isdigit())
        if ld and not 40 <= len(ld) <= 48:
            raise forms.ValidationError(
                'Linha digitável deve ter 47 ou 48 dígitos.')
        return ld

    def clean(self):
        dados = super().clean()
        arq = dados.get('arquivo')
        if not arq and not dados.get('linha_digitavel'):
            raise forms.ValidationError(
                'Anexe o boleto ou informe a linha digitável — sem nenhum '
                'dos dois não há o que conferir.')
        if (arq and not arq.name.lower().endswith('.pdf')
                and not dados.get('linha_digitavel')):
            raise forms.ValidationError(
                'Imagem não dá para a IA ler — cole também a linha '
                'digitável para a conferência sair pelo código de barras.')
        prestador, posto = dados.get('prestador'), dados.get('posto')
        if prestador:
            if prestador.exige_nf and not dados.get('nota_fiscal'):
                raise forms.ValidationError(
                    f'{prestador.nome} exige nota fiscal anexa — anexe a '
                    'NFS-e junto com o boleto.')
            if prestador.modo_boleto == Prestador.ModoBoleto.POR_POSTO:
                if not posto:
                    raise forms.ValidationError(
                        f'{prestador.nome} emite um boleto POR POSTO — '
                        'escolha o posto.')
                if not prestador.vinculos_ativos().filter(posto=posto).exists():
                    raise forms.ValidationError(
                        f'{prestador.nome} não tem vínculo ativo com '
                        f'{posto.nome} (cadastre o valor primeiro).')
            else:
                dados['posto'] = None
        return dados


class BoletoEditForm(forms.Form):
    """Edição de boleto pelo admin: destinar posto (ex.: os vários PDFs de
    um mesmo e-mail), acertar a competência e anotar a observação do mês."""
    posto = forms.ModelChoiceField(label='Posto', required=False,
                                   queryset=Posto.objects.none())
    competencia = forms.ChoiceField(label='Mês (competência)')
    linha_digitavel = forms.CharField(label='Linha digitável', required=False)
    chave_pix = forms.CharField(label='Chave PIX', required=False)
    valor_livre = forms.BooleanField(
        label='Aceitar este valor mesmo diferente do combinado', required=False)
    extra = forms.BooleanField(
        label='Cobrança EXTRA/avulsa (convive com o boleto normal do mês)',
        required=False)
    parcial = forms.BooleanField(
        label='Boleto PARCIAL (vários compõem a mensalidade; soma conferida '
              'contra o combinado)', required=False)
    observacao = forms.CharField(
        label='Observação do mês (vai no e-mail do financeiro)',
        required=False, widget=forms.Textarea(attrs={'rows': 3}))

    def __init__(self, boleto, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .services.verificacao import competencia_extenso
        self.fields['posto'].queryset = Posto.objects.filter(ativo=True)
        opcoes = competencias_opcoes()
        if boleto.competencia not in opcoes:
            opcoes = sorted(set(opcoes) | {boleto.competencia})
        self.fields['competencia'].choices = [
            (m.isoformat(), competencia_extenso(m).capitalize())
            for m in opcoes]

    def clean_linha_digitavel(self):
        ld = ''.join(c for c in self.cleaned_data['linha_digitavel']
                     if c.isdigit())
        if ld and not 40 <= len(ld) <= 48:
            raise forms.ValidationError(
                'Linha digitável deve ter 47 ou 48 dígitos.')
        return ld

    def clean_competencia(self):
        try:
            return date.fromisoformat(
                self.cleaned_data['competencia']).replace(day=1)
        except ValueError:
            raise forms.ValidationError('Mês inválido.')


class ContratoForm(forms.Form):
    arquivo = forms.FileField(label='Arquivo do contrato (PDF)')
    vigencia_inicio = forms.DateField(
        label='Início da vigência',
        widget=forms.DateInput(attrs={'type': 'date'}))
    vigencia_fim = forms.DateField(
        label='Fim da vigência (deixe vazio se indeterminado)', required=False,
        widget=forms.DateInput(attrs={'type': 'date'}))

    def clean_arquivo(self):
        return _validar_arquivo(self.cleaned_data['arquivo'], _EXT_CONTRATO)

    def clean(self):
        dados = super().clean()
        ini, fim = dados.get('vigencia_inicio'), dados.get('vigencia_fim')
        if ini and fim and fim < ini:
            raise forms.ValidationError('Fim da vigência antes do início.')
        return dados


class ContratoAdminForm(ContratoForm):
    """Anexar contrato pelo painel: posto opcional e vigência flexível."""
    posto = forms.ModelChoiceField(
        label='Posto (vazio = contrato geral da empresa)', required=False,
        queryset=Posto.objects.filter(ativo=True,
                                      excluido_em__isnull=True))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['vigencia_inicio'].required = False
        self.fields['vigencia_inicio'].label = \
            'Início da vigência (opcional)'


class ValorBRField(forms.Field):
    """Aceita '1.234,56', '1234,56' ou '1234.56'."""
    def to_python(self, value):
        if value in (None, ''):
            return None
        v = str(value).strip().replace('R$', '').strip()
        if ',' in v:
            v = v.replace('.', '').replace(',', '.')
        try:
            return Decimal(v).quantize(Decimal('0.01'))
        except InvalidOperation:
            raise forms.ValidationError('Valor inválido. Ex.: 1.234,56')


class PrestadorForm(forms.ModelForm):
    valor_unico = ValorBRField(required=False, label='Valor do boleto único')

    class Meta:
        model = Prestador
        fields = ['nome', 'representante', 'representante_nome_social',
                  'cnpj', 'modo_boleto', 'posto_cobranca',
                  'valor_unico', 'exige_nf', 'ativo', 'emails_aviso',
                  'observacao']
        widgets = {'observacao': forms.Textarea(attrs={'rows': 2}),
                   'emails_aviso': forms.TextInput(attrs={
                       'placeholder': 'fulano@gmail.com, outro@x.com'})}
        labels = {'representante': 'Representante (a pessoa — é quem os '
                                   'e-mails tratam por "Prezado(a)")',
                  'representante_nome_social': 'Representante — Nome social '
                                               '(se preenchido, é assim que '
                                               'a pessoa é chamada)',
                  'emails_aviso': 'E-mails do prestador SEM login (idCamim): '
                                  'recebem os avisos E podem mandar boleto '
                                  'por e-mail — vírgula separa'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['posto_cobranca'].queryset = Posto.objects.filter(ativo=True)
        self.fields['posto_cobranca'].required = False
        self.fields['posto_cobranca'].label = \
            'Posto cobrança (só no modo boleto ÚNICO)'
        self.fields['valor_unico'].label = \
            'Valor do boleto único (só no modo ÚNICO; vazio = soma dos postos)'

    def clean_representante(self):
        # Vazio = a própria empresa (cadastros antigos: nome == representante)
        return (self.cleaned_data.get('representante') or '').strip() \
            or (self.cleaned_data.get('nome') or '').strip()

    def clean_emails_aviso(self):
        from django.core.validators import validate_email
        bruto = self.cleaned_data.get('emails_aviso') or ''
        ems = [e.strip().lower() for e in bruto.replace(';', ',').split(',')
               if e.strip()]
        for e in ems:
            try:
                validate_email(e)
            except forms.ValidationError:
                raise forms.ValidationError(f'E-mail inválido: {e}')
        return ', '.join(ems)

    def clean(self):
        dados = super().clean()
        if (dados.get('modo_boleto') == Prestador.ModoBoleto.POR_POSTO
                and (dados.get('valor_unico')
                     or dados.get('posto_cobranca'))):
            raise forms.ValidationError(
                'Você preencheu "Posto cobrança"/"Valor do boleto único", '
                'mas o modo está "Um boleto por posto" — nesses campos só '
                'vale o modo ÚNICO. Ou troque o modo para "boleto único", '
                'ou preencha os valores na tabela de postos abaixo.')
        return dados


class ValeForm(forms.Form):
    """Vale/adiantamento descontado em parcelas do boleto mensal."""
    descricao = forms.CharField(label='Descrição (ex.: Notebook pago pela '
                                      'Camim)', max_length=160)
    posto = forms.ModelChoiceField(
        label='Descontar do boleto de qual posto', required=False,
        queryset=Posto.objects.filter(ativo=True))
    valor_parcela = ValorBRField(label='Valor de cada parcela (R$)')
    parcelas_total = forms.IntegerField(label='Número de parcelas',
                                        min_value=1, max_value=120)
    primeira_competencia = forms.CharField(
        label='Mês da 1ª parcela',
        widget=forms.TextInput(attrs={'type': 'month'}))

    def clean_valor_parcela(self):
        v = self.cleaned_data['valor_parcela']
        if v is None or v <= 0:
            raise forms.ValidationError('Informe o valor da parcela.')
        return v

    def clean_primeira_competencia(self):
        try:
            return date.fromisoformat(
                self.cleaned_data['primeira_competencia'] + '-01')
        except ValueError:
            raise forms.ValidationError('Mês inválido.')


class PostoForm(forms.ModelForm):
    class Meta:
        model = Posto
        fields = ['nome', 'codigo', 'ativo']


class UsuarioForm(forms.ModelForm):
    class Meta:
        model = UsuarioPermitido
        fields = ['email', 'nome', 'prestador', 'is_admin', 'ativo']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['prestador'].queryset = Prestador.objects.filter(ativo=True)
        self.fields['prestador'].required = False

    def clean(self):
        dados = super().clean()
        if not dados.get('is_admin') and not dados.get('prestador'):
            raise forms.ValidationError(
                'Usuário precisa ser admin OU estar ligado a um prestador.')
        return dados
