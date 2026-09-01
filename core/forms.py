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

    def __init__(self, prestador, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prestador = prestador
        from .services.verificacao import competencia_extenso
        hoje = date.today().replace(day=1)
        self.fields['competencia'].choices = [
            (m.isoformat(), competencia_extenso(m).capitalize())
            for m in competencias_opcoes()]
        self.fields['competencia'].initial = hoje.isoformat()
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
    arquivo = forms.FileField(label='Arquivo do boleto (PDF)')
    linha_digitavel = forms.CharField(
        label='Linha digitável (opcional)', required=False,
        widget=forms.TextInput(attrs={'inputmode': 'numeric',
                                      'placeholder': '47 ou 48 dígitos'}))
    chave_pix = forms.CharField(label='Chave PIX (opcional)', required=False)

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
        return _validar_arquivo(self.cleaned_data['arquivo'], _EXT_BOLETO)

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
        prestador, posto = dados.get('prestador'), dados.get('posto')
        if prestador:
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
        fields = ['nome', 'cnpj', 'modo_boleto', 'posto_cobranca',
                  'valor_unico', 'ativo', 'observacao']
        widgets = {'observacao': forms.Textarea(attrs={'rows': 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['posto_cobranca'].queryset = Posto.objects.filter(ativo=True)
        self.fields['posto_cobranca'].required = False


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
