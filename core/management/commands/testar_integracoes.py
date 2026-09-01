"""Testa as integrações reais SEM tocar em PJ nenhum:
  * Groq: pede para extrair o valor de um boleto fictício;
  * E-mail: envia um teste para EMAIL_ADMIN (cristiano@).

Uso:  .venv/bin/python manage.py testar_integracoes
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from core.services import emails, ia


class Command(BaseCommand):
    help = 'Testa Groq e SMTP de verdade (e-mail só para o admin).'

    def handle(self, *args, **opts):
        self.stdout.write('1/2 Groq...')
        texto = ('BANCO TESTE 001-9\nBeneficiário: EMPRESA EXEMPLO LTDA\n'
                 'Vencimento: 10/09/2026\nValor do documento: R$ 1.234,56')
        valor, bruto = ia.extrair_valor(texto)
        if valor is None:
            self.stdout.write(self.style.ERROR(f'  Groq não extraiu: {bruto}'))
        else:
            estilo = (self.style.SUCCESS if str(valor) == '1234.56'
                      else self.style.WARNING)
            self.stdout.write(estilo(f'  Groq OK — extraiu R$ {valor} '
                                     f'(esperado 1234.56)'))

        self.stdout.write('2/2 E-mail...')
        ok = emails.enviar(
            settings.EMAIL_ADMIN, 'Teste — Controle dos PJs',
            'Se você recebeu este e-mail, o SMTP do pj.camim.com.br está OK.')
        if ok:
            self.stdout.write(self.style.SUCCESS(
                f'  E-mail OK — enviado para {settings.EMAIL_ADMIN}'))
        else:
            self.stdout.write(self.style.ERROR(
                '  Falha no e-mail — veja o painel /painel/emails/'))
