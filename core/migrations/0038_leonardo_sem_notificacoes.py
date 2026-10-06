"""Leonardo Pereira pediu para não receber mais e-mails do sistema
(pedido de 06/10/2026). Desliga TODOS os tipos de cópia interna para
leonardo@camim.com.br — ele continua na EMAIL_COPIA_OCULTA, mas sai de
toda cópia porque não recebe nenhum tipo. Reversível."""
from django.db import migrations

EMAIL = 'leonardo@camim.com.br'
# Todos os códigos de core.notificacoes.TIPOS_PREF (inline para a migração
# ficar autossuficiente, no padrão do Django).
CODIGOS = ['recebido', 'pagamento', 'aprovado', 'financeiro', 'nota',
           'divergente', 'manual', 'lembrete', 'outros']


def desligar(apps, schema_editor):
    Pref = apps.get_model('core', 'PreferenciaNotificacao')
    for codigo in CODIGOS:
        Pref.objects.update_or_create(
            email=EMAIL, tipo=codigo, defaults={'recebe': False})


def religar(apps, schema_editor):
    Pref = apps.get_model('core', 'PreferenciaNotificacao')
    Pref.objects.filter(email=EMAIL).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0037_preferencianotificacao'),
    ]

    operations = [
        migrations.RunPython(desligar, religar),
    ]
