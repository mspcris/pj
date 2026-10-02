"""Reconstitui a ORIGEM dos boletos que já existiam:

  * E-MAIL: o robô anota em EmailRecebido.detalhe os boletos que criou
    ("#12, #13") — o boleto ganha também o vínculo com esse e-mail.
  * API / plataforma / painel: a auditoria de upload registra a porta
    ("(api) Boleto #N …", "(admin) Boleto #N …", "Boleto #N …").

O que não der para reconstituir fica com a origem em branco.
"""
import re

from django.db import migrations

RE_AUDIT = re.compile(r'^(\((api|admin)\) )?Boleto #(\d+) ')


def preencher(apps, schema_editor):
    Boleto = apps.get_model('core', 'Boleto')
    EmailRecebido = apps.get_model('core', 'EmailRecebido')
    AuditLog = apps.get_model('core', 'AuditLog')

    for e in EmailRecebido.objects.filter(resultado='BOLETO').order_by('pk'):
        pks = [int(n) for n in re.findall(r'#(\d+)', e.detalhe or '')]
        if pks:
            (Boleto.objects.filter(pk__in=pks, origem='')
             .update(origem='EMAIL', email_origem=e))

    porta = {'api': 'API', 'admin': 'PAINEL', None: 'PORTAL'}
    for detalhe in (AuditLog.objects.filter(evento='UP_BOLETO')
                    .order_by('pk').values_list('detalhe', flat=True)):
        m = RE_AUDIT.match(detalhe or '')
        if m:
            (Boleto.objects.filter(pk=int(m.group(3)), origem='')
             .update(origem=porta[m.group(2)]))


def desfazer(apps, schema_editor):
    pass  # os campos somem na migração anterior


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0034_origem_do_boleto'),
    ]

    operations = [
        migrations.RunPython(preencher, desfazer),
    ]
