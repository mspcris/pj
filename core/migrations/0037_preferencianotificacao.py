from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0036_nota_anterior'),
    ]

    operations = [
        migrations.CreateModel(
            name='PreferenciaNotificacao',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('email', models.EmailField(max_length=254)),
                ('tipo', models.CharField(max_length=20)),
                ('recebe', models.BooleanField(default=True)),
                ('atualizado_em', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'preferência de notificação',
                'verbose_name_plural': 'preferências de notificação',
                'ordering': ['email', 'tipo'],
                'unique_together': {('email', 'tipo')},
            },
        ),
    ]
