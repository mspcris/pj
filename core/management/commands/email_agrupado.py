"""E-mail agrupado ao financeiro (10/11/2026): um e-mail só, com todos os
boletos aprovados que ficaram na fila dos prestadores com a opção
"Enviar e-mail agrupado". Roda de 2 em 2 horas, para dar tempo de o
prestador anexar tudo.

Cron (ver deploy/crontab.txt):
    0 */2 * * * cd /opt/pj && .venv/bin/python manage.py email_agrupado
"""
from django.core.management.base import BaseCommand

from core.services.verificacao import enviar_email_agrupado


class Command(BaseCommand):
    help = 'Manda à equipe@ um e-mail só com os boletos aprovados pendentes.'

    def handle(self, *args, **opts):
        pks = enviar_email_agrupado()
        if pks:
            self.stdout.write(self.style.SUCCESS(
                f'E-mail agrupado enviado com {len(pks)} boleto(s): '
                + ', '.join(f'#{p}' for p in pks)))
        else:
            self.stdout.write('Nada pendente para o e-mail agrupado.')
