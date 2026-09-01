"""Seed dos 13 postos canônicos da Camim.

Mesma régua do legado (Cad_Endereco): letra + id_endereco. Fonte: CLAUDE.md do
relatorio_h_t ("NUNCA escrever nome de posto à mão") e seed do crm/local_db.py.
"""
from django.db import migrations

POSTOS = [
    # (letra, id_endereco, nome)
    ('A', 3, 'Anchieta'),
    ('B', 7, 'Bangu'),
    ('C', 2, 'Campinho'),
    ('D', 20, 'Del Castilho'),
    ('G', 5, 'Campo Grande'),
    ('I', 6, 'Nova Iguaçu'),
    ('J', 4, 'Jacarepaguá'),
    ('M', 25, 'Madureira'),
    ('N', 12, 'Nilópolis'),
    ('P', 26, 'Rio das Pedras'),
    ('R', 1, 'Realengo'),
    ('X', 21, 'X Campo Grande'),
    ('Y', 51, 'Y Campo Grande'),
]


def seed(apps, schema_editor):
    Posto = apps.get_model('core', 'Posto')
    for letra, id_end, nome in POSTOS:
        Posto.objects.update_or_create(
            id_endereco_legado=id_end,
            defaults={'nome': nome, 'codigo': letra, 'ativo': True})


def desfazer(apps, schema_editor):
    pass  # não apaga postos ao reverter


class Migration(migrations.Migration):
    dependencies = [('core', '0001_initial')]
    operations = [migrations.RunPython(seed, desfazer)]
