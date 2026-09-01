"""Razão social + CNPJ de cada posto — cadastro oficial passado pelo
Cristiano em 01/09/2026 (tabela IDEndereco/CF do legado). Usado para
destinar boletos pelo CNPJ do sacado impresso no PDF."""
from django.db import migrations

DADOS = {
    # letra: (razão social, cnpj)
    'R': ('CENTRO MÉDICO DOIS IRMÃOS LTDA', '72.357.999/0001-31'),
    'C': ('CAMIM CLÍNICA MÉDICA', '22.899.576/0001-75'),
    'A': ('CAMIM ANCHIETA', '27.110.113/0001-04'),
    'J': ('CENTRO MÉDICO PECHINCHA', '00.371.870/0001-77'),
    'G': ('POLICLÍNICA TRÊS IRMÃOS EIRELI', '29.470.619/0001-41'),
    'I': ('CENTRO MÉDICO FERNANDES', '28.593.859/0001-70'),
    'B': ('CENTRO MÉDICO ADELINO FERNANDES EIRELI', '33.040.053/0001-95'),
    'M': ('CLÍNICA OFTALMOLÓGICA TRÊS IRMÃOS EIRELI', '30.927.397/0001-22'),
    'N': ('CLÍNICA DE ASSISTÊNCIA MÉDICA SAÚDE PS LTDA',
          '25.247.840/0001-84'),
    'D': ('SDM CLINICA MEDICA LTDA ME', '34.521.665/0001-62'),
    'X': ('CENTRO MÉDICO CLARINDO BARROSO LOMBA', '35.978.024/0001-02'),
    'Y': ('CENTRO MÉDICO CGY CAMPO GRANDE EIRELI ME', '39.396.399/0001-07'),
}


def seed(apps, schema_editor):
    Posto = apps.get_model('core', 'Posto')
    for letra, (razao, cnpj) in DADOS.items():
        Posto.objects.filter(codigo=letra).update(razao_social=razao,
                                                  cnpj=cnpj)


class Migration(migrations.Migration):
    dependencies = [('core', '0009_posto_cnpj_posto_razao_social')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
