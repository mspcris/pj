# Posto: razão social (Receita) e sede para a qualificação do CONTRATANTE
# nos contratos gerados por posto. Seed dos 8 postos do Altamiro com os
# dados usados nos contratos da Victória (23/10/2026) e do Caio (26/10/2026).

from django.db import migrations, models

SEED = {
    'A': ('CLÍNICA DE ASSISTÊNCIA MÉDICA ANCHIETA LTDA',
          'Estrada Marechal Alencastro, nº 3759, Anchieta, Rio de Janeiro/RJ, CEP 21625-000'),
    'N': ('CLÍNICA DE ASSISTÊNCIA MÉDICA SAÚDE PS LTDA',
          'Rua Professor Alfredo Gonçalves Figueira, nº 100, lojas 224 e 226, Centro, Nilópolis/RJ, CEP 26525-060'),
    'I': ('CENTRO MÉDICO FERNANDES LTDA',
          'Avenida Abílio Augusto Távora, nº 1111, lojas 2009/2010, Da Luz, Nova Iguaçu/RJ, CEP 26260-045'),
    'X': ('CENTRO MÉDICO CLARINDO BARROSO LOMBA LTDA',
          'Rua Augusto de Azevedo Santos, nº 9, loja A, Campo Grande, Rio de Janeiro/RJ, CEP 23087-040'),
    'G': ('POLICLÍNICA TRÊS IRMÃOS LTDA',
          'Avenida Cesário de Melo, nº 3752, salas 301/310, 401/412, 501/512 e 601, Campo Grande, Rio de Janeiro/RJ, CEP 23050-102'),
    'Y': ('CENTRO MÉDICO CGY CAMPO GRANDE LTDA',
          'Estrada Cachamorra, nº 1755, Campo Grande, Rio de Janeiro/RJ, CEP 23040-152'),
    'B': ('CENTRO MÉDICO ADELINO FERNANDES LTDA',
          'Rua Francisco Real, nº 1783, lote 3, PAL 8762, Bangu, Rio de Janeiro/RJ, CEP 21810-041'),
    'R': ('CENTRO MÉDICO DOIS IRMÃOS LTDA',
          'Avenida Santa Cruz, nº 598, Realengo, Rio de Janeiro/RJ, CEP 21710-231'),
}


def semear(apps, schema_editor):
    Posto = apps.get_model('core', 'Posto')
    for codigo, (razao, sede) in SEED.items():
        Posto.objects.filter(codigo=codigo, id_endereco_legado__isnull=False,
                             razao_social_contrato='').update(
            razao_social_contrato=razao, sede=sede)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0041_prestador_primeiro_mes'),
    ]

    operations = [
        migrations.AddField(
            model_name='posto',
            name='razao_social_contrato',
            field=models.CharField(blank=True, max_length=200, verbose_name='Razão social nos contratos (Receita)'),
        ),
        migrations.AddField(
            model_name='posto',
            name='sede',
            field=models.CharField(blank=True, max_length=300, verbose_name='Sede nos contratos (rua, nº, bairro, cidade/UF, CEP)'),
        ),
        migrations.RunPython(semear, migrations.RunPython.noop),
    ]
