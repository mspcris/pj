from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0030_boleto_aprovado_por'),
    ]

    operations = [
        migrations.AddField(
            model_name='prestador',
            name='recebe_por_cpf',
            field=models.BooleanField(default=False),
        ),
    ]
