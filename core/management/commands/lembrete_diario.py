"""Lembrete diário para o Cristiano NUNCA perder a mão dos pagamentos.

Manda um resumo do mês por e-mail: quem ainda não mandou boleto, o que está
divergente/manual (esperando ação) e o que já foi para pagamento mas ainda
não foi marcado como pago.

Cron sugerido (todo dia 8h):
    0 8 * * * cd /opt/pj && .venv/bin/python manage.py lembrete_diario
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Boleto, Prestador
from core.services import emails
from core.services.verificacao import competencia_extenso, _moeda


class Command(BaseCommand):
    help = 'Envia o resumo diário de pendências dos PJs para o admin.'

    def handle(self, *args, **opts):
        mes = timezone.localdate().replace(day=1)
        boletos = list(
            Boleto.objects.filter(competencia=mes)
            .exclude(status__in=[Boleto.Status.SUBSTITUIDO,
                                 Boleto.Status.DESCARTADO])
            .select_related('prestador', 'posto', 'prestador__posto_cobranca'))

        faltando, atencao, aguardando = [], [], []
        for prestador in (Prestador.objects.filter(ativo=True)
                          .prefetch_related('vinculos__posto')):
            for posto, valor in prestador.boletos_esperados():
                achado = next(
                    (b for b in boletos
                     if b.status != Boleto.Status.DUPLICADO and
                     not b.extra and
                     b.prestador_id == prestador.pk and
                     (b.posto_id is None
                      if prestador.modo_boleto == Prestador.ModoBoleto.UNICO
                      else (posto and b.posto_id == posto.pk))), None)
                alvo = posto.nome if posto else 'boleto único'
                if achado is None:
                    faltando.append(f'  • {prestador.nome} — {alvo} '
                                    f'(R$ {_moeda(valor)})')
                elif achado.status in (Boleto.Status.DIVERGENTE,
                                       Boleto.Status.MANUAL):
                    atencao.append(f'  • {prestador.nome} — {alvo}: '
                                   f'{achado.get_status_display()}')
                elif achado.status == Boleto.Status.APROVADO:
                    aguardando.append(
                        f'  • {prestador.nome} — {alvo} — '
                        f'R$ {_moeda(achado.valor_extraido)} '
                        f'(enviado p/ pagamento, falta marcar PAGO)')

        partes = [f'Resumo dos PJs — {competencia_extenso(mes)}\n']
        partes.append(f'⛔ Sem boleto ainda ({len(faltando)}):')
        partes.extend(faltando or ['  (ninguém — tudo em dia!)'])
        partes.append(f'\n⚠️ Precisa da sua ação ({len(atencao)}):')
        partes.extend(atencao or ['  (nada)'])
        partes.append(f'\n💸 Enviados p/ pagamento, confirmar se pagou '
                      f'({len(aguardando)}):')
        partes.extend(aguardando or ['  (nada)'])
        partes.append('\nPainel: https://pj.camim.com.br/painel/')

        emails.enviar(settings.EMAIL_ADMIN,
                      f'Controle dos PJs — pendências de '
                      f'{competencia_extenso(mes)}',
                      '\n'.join(partes))
        self.stdout.write(self.style.SUCCESS('Lembrete enviado.'))
