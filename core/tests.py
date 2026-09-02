"""Testes dos fluxos críticos: whitelist, upload, verificação e permissões."""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import (Boleto, Contrato, Posto, Prestador, PrestadorPosto,
                     UsuarioPermitido)
from .services import verificacao

User = get_user_model()

PDF_MINI = b'%PDF-1.4 conteudo de teste'


def _destinos(m_mail):
    """Todos os endereços de todas as chamadas (aceita str ou lista)."""
    out = []
    for c in m_mail.call_args_list:
        d = c.args[0]
        out.extend([d] if isinstance(d, str) else list(d))
    return out


def _pdf(nome='boleto.pdf'):
    return SimpleUploadedFile(nome, PDF_MINI, content_type='application/pdf')


class BaseSetup(TestCase):
    def setUp(self):
        self.posto1 = Posto.objects.get(codigo='A')   # Anchieta (seed)
        self.posto2 = Posto.objects.get(codigo='B')   # Bangu (seed)
        self.prestador = Prestador.objects.create(nome='Limpeza Total LTDA')
        PrestadorPosto.objects.create(prestador=self.prestador,
                                      posto=self.posto1,
                                      valor_mensal=Decimal('1500.00'))
        PrestadorPosto.objects.create(prestador=self.prestador,
                                      posto=self.posto2,
                                      valor_mensal=Decimal('2000.00'))
        self.up = UsuarioPermitido.objects.create(
            email='pj@empresa.com.br', prestador=self.prestador)
        self.user = User.objects.create_user(
            username='pj@empresa.com.br', email='pj@empresa.com.br')

        self.admin_up = UsuarioPermitido.objects.create(
            email='cristiano@camim.com.br', is_admin=True)
        self.admin = User.objects.create_user(
            username='cristiano@camim.com.br', email='cristiano@camim.com.br')

    def login_pj(self):
        self.client.force_login(self.user)

    def login_admin(self):
        self.client.force_login(self.admin)


class WhitelistTest(BaseSetup):
    def test_anonimo_redireciona_para_login(self):
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/oidc/authenticate/', resp.url)

    def test_logado_sem_whitelist_cai_em_sem_acesso(self):
        intruso = User.objects.create_user(username='x@camim.com.br',
                                           email='x@camim.com.br')
        self.client.force_login(intruso)
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/sem-acesso/', resp.url)

    def test_pj_bloqueado_nao_entra(self):
        self.up.ativo = False
        self.up.save()
        self.login_pj()
        resp = self.client.get('/')
        self.assertEqual(resp.status_code, 302)

    def test_seed_postos(self):
        self.assertEqual(
            Posto.objects.filter(id_endereco_legado__isnull=False).count(), 13)
        self.assertFalse(Posto.objects.filter(nome__icontains='Méier').exists())


class UploadBoletoTest(BaseSetup):
    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_upload_por_posto(self, m_async):
        self.login_pj()
        hoje = date.today().replace(day=1)
        resp = self.client.post('/boleto/', {
            'competencia': hoje.isoformat(),
            'posto': self.posto1.pk,
            'arquivo': _pdf(),
        })
        self.assertEqual(resp.status_code, 302)
        b = Boleto.objects.get()
        self.assertEqual(b.valor_esperado, Decimal('1500.00'))
        self.assertEqual(b.status, Boleto.Status.RECEBIDO)
        m_async.assert_called_once_with(b.pk)

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_reenvio_substitui_anterior(self, m_async):
        self.login_pj()
        hoje = date.today().replace(day=1)
        dados = {'competencia': hoje.isoformat(), 'posto': self.posto1.pk}
        self.client.post('/boleto/', {**dados, 'arquivo': _pdf()})
        self.client.post('/boleto/', {**dados, 'arquivo': _pdf('novo.pdf')})
        status = list(Boleto.objects.order_by('pk')
                      .values_list('status', flat=True))
        self.assertEqual(status, [Boleto.Status.SUBSTITUIDO,
                                  Boleto.Status.RECEBIDO])

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_boleto_unico_soma_postos(self, m_async):
        self.prestador.modo_boleto = Prestador.ModoBoleto.UNICO
        self.prestador.posto_cobranca = self.posto1
        self.prestador.save()
        self.login_pj()
        hoje = date.today().replace(day=1)
        self.client.post('/boleto/', {'competencia': hoje.isoformat(),
                                      'arquivo': _pdf()})
        b = Boleto.objects.get()
        self.assertIsNone(b.posto)
        self.assertEqual(b.valor_esperado, Decimal('3500.00'))

    def test_rejeita_nao_pdf(self):
        self.login_pj()
        hoje = date.today().replace(day=1)
        falso = SimpleUploadedFile('boleto.pdf', b'GIF89a nada de pdf')
        resp = self.client.post('/boleto/', {
            'competencia': hoje.isoformat(), 'posto': self.posto1.pk,
            'arquivo': falso})
        self.assertEqual(resp.status_code, 200)  # volta com erro
        self.assertEqual(Boleto.objects.count(), 0)


@override_settings(EMAIL_MODO_TESTE=True)
class VerificacaoTest(BaseSetup):
    def _boleto(self, valor_esperado='1500.00'):
        return Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1),
            arquivo=_pdf(), enviado_por='pj@empresa.com.br',
            valor_esperado=Decimal(valor_esperado))

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1500.00'),
                              '{"valor":"1500.00","confianca":100}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='BOLETO 1500')
    def test_valor_bate_aprova_e_envia_pagador(self, m_pdf, m_ia, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        destinos = _destinos(m_mail)
        self.assertIn('equipe@camim.com.br', destinos)      # pagador
        self.assertIn('pj@empresa.com.br', destinos)         # aviso ao PJ

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1800.00'), '{"valor":"1800.00"}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='BOLETO 1800')
    def test_valor_diverge_avisa_pj(self, m_pdf, m_ia, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.DIVERGENTE)
        destinos = _destinos(m_mail)
        self.assertNotIn('equipe@camim.com.br', destinos)  # NÃO paga divergente
        self.assertIn('pj@empresa.com.br', destinos)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.pdf.extrair_texto', return_value='')
    def test_pdf_ilegivel_vira_manual(self, m_pdf, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.MANUAL)
        destinos = _destinos(m_mail)
        self.assertIn('cristiano@camim.com.br', destinos)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1500.00'), '{}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_nao_processa_duas_vezes(self, m_pdf, m_ia, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        chamadas = m_mail.call_count
        verificacao.processar(b.pk)  # já não está RECEBIDO
        self.assertEqual(m_mail.call_count, chamadas)


class AdminBoletoTest(BaseSetup):
    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_admin_cadastra_boleto_do_zap(self, m_async):
        self.login_admin()
        hoje = date.today().replace(day=1)
        linha = '23793.38128 60007.827136 95000.063305 9 84340000150000'
        resp = self.client.post('/painel/boleto/novo/', {
            'prestador': self.prestador.pk, 'posto': self.posto1.pk,
            'competencia': hoje.isoformat(), 'arquivo': _pdf(),
            'linha_digitavel': linha, 'chave_pix': 'pix@empresa.com.br'})
        self.assertEqual(resp.status_code, 302)
        b = Boleto.objects.get()
        self.assertEqual(b.valor_esperado, Decimal('1500.00'))
        self.assertEqual(b.linha_digitavel,
                         ''.join(c for c in linha if c.isdigit()))
        self.assertEqual(b.chave_pix, 'pix@empresa.com.br')
        self.assertEqual(b.enviado_por, 'cristiano@camim.com.br')
        m_async.assert_called_once_with(b.pk)

    def test_admin_precisa_escolher_posto_no_modo_por_posto(self):
        self.login_admin()
        hoje = date.today().replace(day=1)
        resp = self.client.post('/painel/boleto/novo/', {
            'prestador': self.prestador.pk,
            'competencia': hoje.isoformat(), 'arquivo': _pdf()})
        self.assertEqual(resp.status_code, 200)  # volta com erro
        self.assertEqual(Boleto.objects.count(), 0)


class EditarBoletoTest(BaseSetup):
    def _boleto_sem_posto(self):
        return Boleto.objects.create(
            prestador=self.prestador, posto=None,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.MANUAL)

    @mock.patch('core.services.verificacao.processar_async')
    def test_destinar_posto_recalcula_e_reverifica(self, m_proc):
        b = self._boleto_sem_posto()
        self.login_admin()
        resp = self.client.post(f'/painel/boleto/{b.pk}/editar/', {
            'posto': self.posto1.pk,
            'competencia': date(2026, 9, 1).isoformat(),
            'observacao': 'descontada parcela 3/7 do notebook (R$ 600)'})
        self.assertEqual(resp.status_code, 302)
        b.refresh_from_db()
        self.assertEqual(b.posto, self.posto1)
        self.assertEqual(b.valor_esperado, Decimal('1500.00'))
        self.assertEqual(b.status, Boleto.Status.RECEBIDO)
        self.assertIn('notebook', b.observacao)
        m_proc.assert_called_once_with(b.pk)

    @mock.patch('core.services.verificacao.processar_async')
    def test_so_observacao_nao_reverifica(self, m_proc):
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.APROVADO)
        self.login_admin()
        self.client.post(f'/painel/boleto/{b.pk}/editar/', {
            'posto': self.posto1.pk,
            'competencia': date(2026, 9, 1).isoformat(),
            'observacao': 'só uma nota'})
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)  # intacto
        m_proc.assert_not_called()

    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_observacao_vai_no_email_do_financeiro(self, m_mail):
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.MANUAL, valor_esperado=Decimal('1500.00'),
            observacao='descontada parcela 3/7 do notebook')
        self.login_admin()
        self.client.post(f'/painel/boleto/{b.pk}/aprovar/')
        corpo = next(c.args[2] for c in m_mail.call_args_list
                     if c.args[0] == 'equipe@camim.com.br')
        self.assertIn('parcela 3/7 do notebook', corpo)


class ContratoPainelTest(BaseSetup):
    def test_admin_anexa_contrato_pelo_painel(self):
        from core.models import Contrato
        self.login_admin()
        resp = self.client.post(
            f'/painel/prestadores/{self.prestador.pk}/',
            {'qual': 'contrato', 'posto': self.posto1.pk,
             'arquivo': _pdf('contrato.pdf'),
             'vigencia_inicio': '2026-09-01', 'vigencia_fim': '2027-09-01'})
        self.assertEqual(resp.status_code, 302)
        c = Contrato.objects.get(prestador=self.prestador)
        self.assertEqual(c.posto, self.posto1)
        self.assertEqual(c.enviado_por, 'cristiano@camim.com.br')


class ValoresPostosTest(BaseSetup):
    def test_checkbox_atende_cria_e_remove_vinculo(self):
        self.login_admin()
        url = f'/painel/prestadores/{self.prestador.pk}/'
        # marca só Anchieta com valor; Bangu (que tinha vínculo) desmarcado
        self.client.post(url, {'qual': 'valores',
                               f'atende_{self.posto1.pk}': 'on',
                               f'valor_{self.posto1.pk}': '1.800,00'})
        vinculos = {v.posto_id: v for v in
                    PrestadorPosto.objects.filter(prestador=self.prestador)}
        self.assertTrue(vinculos[self.posto1.pk].ativo)
        self.assertEqual(vinculos[self.posto1.pk].valor_mensal,
                         Decimal('1800.00'))
        self.assertFalse(vinculos[self.posto2.pk].ativo)  # desmarcado

    def test_atende_sem_valor_nao_salva(self):
        self.login_admin()
        url = f'/painel/prestadores/{self.prestador.pk}/'
        self.client.post(url, {'qual': 'valores',
                               f'atende_{self.posto1.pk}': 'on'})
        v = PrestadorPosto.objects.get(prestador=self.prestador,
                                       posto=self.posto1)
        self.assertEqual(v.valor_mensal, Decimal('1500.00'))  # intacto


class BoletoExtraTest(BaseSetup):
    """Dois boletos no mesmo posto/mês: o normal + uma cobrança extra
    (ex.: ajuda de custo 'passagem')."""

    def _regular_aprovado(self):
        return Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.APROVADO,
            valor_esperado=Decimal('1500.00'))

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('312.50'),
                              '{"valor":"312.50","confianca":100}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_extra_nao_vira_duplicado_e_aprova_com_valor_livre(
            self, m_pdf, m_ia, m_mail):
        self._regular_aprovado()
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            extra=True, valor_livre=True,
            observacao='ajuda de custo — passagem')
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        self.assertEqual(b.valor_extraido, Decimal('312.50'))

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('312.50'),
                              '{"valor":"312.50","confianca":100}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_extra_sem_valor_livre_espera_liberacao(self, m_pdf, m_ia,
                                                    m_mail):
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(), extra=True)
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.MANUAL)
        self.assertIsNone(b.valor_esperado)  # extra não herda o combinado
        self.assertIn('EXTRA', b.motivo_manual)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1500.00'),
                              '{"valor":"1500.00","confianca":100}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_extra_aprovado_nao_bloqueia_o_regular(self, m_pdf, m_ia,
                                                   m_mail):
        Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            extra=True, status=Boleto.Status.APROVADO)
        regular = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            valor_esperado=Decimal('1500.00'))
        verificacao.processar(regular.pk)
        regular.refresh_from_db()
        self.assertEqual(regular.status, Boleto.Status.APROVADO)

    def test_extra_tem_secao_propria_no_dashboard(self):
        Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date.today().replace(day=1), arquivo=_pdf(),
            extra=True, status=Boleto.Status.APROVADO,
            observacao='ajuda de custo')
        self.login_admin()
        resp = self.client.get('/painel/')
        self.assertContains(resp, 'Cobranças extras do mês')
        self.assertNotContains(resp, 'fora da régua')  # sem anomalias

    def test_registrar_extra_nao_substitui_o_regular(self):
        from core.services import boletos as svc
        regular = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.RECEBIDO)
        novo = svc.registrar(self.prestador, date(2026, 9, 1),
                             enviado_por='x@x.com', posto=self.posto1,
                             extra=True, linha_digitavel=_linha_47(31250))
        self.assertTrue(novo.extra)  # o flag tem que ser GRAVADO
        self.assertIsNone(novo.valor_esperado)  # extra não herda combinado
        regular.refresh_from_db()
        self.assertEqual(regular.status, Boleto.Status.RECEBIDO)  # intacto


class BoletoParcialTest(BaseSetup):
    """N boletos compondo UMA mensalidade (Elias mandou 2×500 por posto)."""

    def _parcial(self, valor_linha, **kw):
        base = dict(prestador=self.prestador, posto=self.posto1,
                    competencia=date(2026, 9, 1), arquivo=None,
                    parcial=True, linha_digitavel=_linha_47(valor_linha),
                    enviado_por='pj@empresa.com.br')
        base.update(kw)
        return Boleto.objects.create(**base)

    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_duas_parciais_somando_o_combinado_aprovam(self, m_mail):
        b1 = self._parcial(50000)   # R$ 500,00 (combinado do posto: 1500)
        verificacao.processar(b1.pk)
        b1.refresh_from_db()
        self.assertEqual(b1.status, Boleto.Status.APROVADO)
        b2 = self._parcial(100000)  # R$ 1.000,00 → soma 1500 = combinado
        verificacao.processar(b2.pk)
        b2.refresh_from_db()
        self.assertEqual(b2.status, Boleto.Status.APROVADO)
        corpo = [c.args[2] for c in m_mail.call_args_list
                 if c.args[0] == 'equipe@camim.com.br'][-1]
        self.assertIn('PARCIAL', corpo)
        self.assertIn('1.500,00', corpo)
        self.assertIn('✅ Fechou!', corpo)
        self.assertIn('Já entregue antes: R$ 500,00 (1 boleto)', corpo)

    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_emails_do_parcial_dizem_quanto_falta(self, m_mail):
        """Elias mandou 499,93 + 499,94 p/ combinado de 1.000: os e-mails
        (recebido, pagador e PJ) têm de dizer o que falta — nos 3 blocos,
        no assunto e na frase do texto (modelo pronto, IA desligada)."""
        PrestadorPosto.objects.filter(prestador=self.prestador,
                                      posto=self.posto1).update(
            valor_mensal=Decimal('1000.00'))
        with mock.patch('core.services.ia.redigir_email',
                        side_effect=RuntimeError('IA off')):
            b1 = self._parcial(49993)
            verificacao.enviar_recebido(b1)
            assunto, corpo = m_mail.call_args.args[1:3]
            self.assertIn('PARCIAL: R$ 499,93 de R$ 1.000,00 — '
                          'falta R$ 500,07', assunto)
            self.assertIn('Valor: R$ 499,93', corpo)   # e NÃO 1.000,00
            self.assertNotIn('Valor: R$ 1.000,00', corpo)
            self.assertIn('ainda falta gerar um boleto de R$ 500,07', corpo)
            self.assertIn('Já entregue antes: R$ 0,00 (nenhum', corpo)
            self.assertIn('⏳ Ainda NÃO fechou: falta gerar mais um boleto '
                          'de R$ 500,07', corpo)
            verificacao.processar(b1.pk)
            b1.refresh_from_db()
            self.assertEqual(b1.status, Boleto.Status.APROVADO)

            b2 = self._parcial(49994)
            verificacao.processar(b2.pk)
            b2.refresh_from_db()
            self.assertEqual(b2.status, Boleto.Status.APROVADO)
            pagador = [c for c in m_mail.call_args_list
                       if c.args[0] == 'equipe@camim.com.br'][-1]
            assunto, corpo = pagador.args[1:3]
            # o prefixo que o financeiro responde continua íntegro
            self.assertTrue(assunto.startswith(
                'Pagamento — Limpeza Total LTDA — Anchieta — '
                'setembro/2026 — R$ 499,94'))
            self.assertIn('falta R$ 0,13', assunto)
            self.assertIn('Já entregue antes: R$ 499,93 (1 boleto)', corpo)
            self.assertIn('Entregue até agora: R$ 999,87', corpo)
            self.assertIn('Ainda falta: R$ 0,13', corpo)
            self.assertIn('Já havia R$ 499,93 entregue antes', corpo)
            pj = [c for c in m_mail.call_args_list
                  if c.args[0] == ['pj@empresa.com.br']][-1]
            self.assertIn('falta R$ 0,13', pj.args[1])
            self.assertIn('Ainda falta: R$ 0,13', pj.args[2])
            self.assertNotIn('de acordo com o valor contratado', pj.args[2])
            # a resposta do financeiro ainda casa com o boleto
            from core.services.boletos import localizar_boleto_por_assunto
            self.assertEqual(localizar_boleto_por_assunto('Re: ' + assunto),
                             b2)

    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_parcial_que_estoura_o_combinado_vira_manual(self, m_mail):
        b1 = self._parcial(100000, status=Boleto.Status.APROVADO,
                           valor_extraido=Decimal('1000.00'))
        b2 = self._parcial(60000)  # 1000 + 600 > 1500
        verificacao.processar(b2.pk)
        b2.refresh_from_db()
        self.assertEqual(b2.status, Boleto.Status.MANUAL)
        self.assertIn('passam do combinado', b2.motivo_manual)

    def test_parcial_nao_e_duplicidade(self):
        from core.services.boletos import duplicado_de
        self._parcial(50000, status=Boleto.Status.APROVADO,
                      valor_extraido=Decimal('500.00'))
        b2 = self._parcial(100000)
        self.assertIsNone(duplicado_de(b2))
        # e um boleto CHEIO também não é barrado por parciais
        cheio = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf())
        self.assertIsNone(duplicado_de(cheio))

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_dashboard_mostra_soma_das_parciais(self, m_async):
        self._parcial(50000, status=Boleto.Status.APROVADO,
                      valor_extraido=Decimal('500.00'),
                      competencia=date.today().replace(day=1))
        self.login_admin()
        resp = self.client.get('/painel/')
        self.assertContains(resp, 'Boletos parciais do mês')
        self.assertContains(resp, 'Parciais: R$ 500,00 de R$ 1.500,00')


class TravaReenvioTest(BaseSetup):
    """Nova Iguaçu 01/09: editar um boleto já APROVADO mandava-o de novo ao
    financeiro (2 boletos, 4 e-mails). Agora: mesmo valor → nada reenviado;
    valor diferente → CORREÇÃO; "Reenviar e-mails" → reenvio marcado."""

    def _boleto(self, centavos, **kw):
        base = dict(prestador=self.prestador, posto=self.posto1,
                    competencia=date(2026, 9, 1), arquivo=None,
                    linha_digitavel=_linha_47(centavos), valor_livre=True,
                    enviado_por='cristiano@camim.com.br')
        base.update(kw)
        return Boleto.objects.create(**base)

    def _pagamentos(self, m_mail):
        return [c for c in m_mail.call_args_list
                if c.args[0] == 'equipe@camim.com.br']

    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_reaprovacao_apos_edicao_nao_reenvia(self, m_mail):
        b = self._boleto(150000)
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        self.assertIsNotNone(b.pagamento_enviado_em)
        self.assertEqual(b.pagamento_enviado_valor, Decimal('1500.00'))
        self.assertEqual(len(self._pagamentos(m_mail)), 1)
        # edição que muda algo → volta p/ verificação → aprova de novo
        self.login_admin()
        with mock.patch('core.services.verificacao.processar_async'):
            self.client.post(f'/painel/boleto/{b.pk}/editar/', {
                'posto': self.posto1.pk, 'competencia': '2026-09-01',
                'linha_digitavel': b.linha_digitavel, 'chave_pix': '',
                'observacao': 'ajuste'})  # tirou o "valor livre" → mudou
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.RECEBIDO)
        b.valor_livre = True
        b.save(update_fields=['valor_livre'])
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        self.assertEqual(len(self._pagamentos(m_mail)), 1)  # NÃO reenviou
        # e ficou registrado na auditoria
        from core.models import AuditLog
        self.assertTrue(AuditLog.objects.filter(
            detalhe__contains='nada reenviado').exists())

    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_valor_diferente_vai_como_correcao_e_avisa_admin(self, m_mail):
        b = self._boleto(150000)
        verificacao.processar(b.pk)
        b.refresh_from_db()
        b.linha_digitavel = _linha_47(160000)  # trocou o boleto
        b.status = Boleto.Status.RECEBIDO
        b.tentativas = 0
        b.valor_extraido = None
        b.save()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        pags = self._pagamentos(m_mail)
        self.assertEqual(len(pags), 2)
        self.assertTrue(pags[-1].args[1].startswith('CORREÇÃO — Pagamento — '))
        self.assertIn('SUBSTITUI o enviado em', pags[-1].args[2])
        self.assertIn('R$ 1.500,00', pags[-1].args[2])
        self.assertEqual(b.pagamento_enviado_valor, Decimal('1600.00'))
        # o financeiro ainda casa a resposta (prefixo preservado)
        from core.services.boletos import localizar_boleto_por_assunto
        self.assertEqual(localizar_boleto_por_assunto(
            'Re: ' + pags[-1].args[1]), b)
        # admin avisado: correção + valor MAIOR que o combinado
        avisos = [c for c in m_mail.call_args_list
                  if c.args[0] == 'cristiano@camim.com.br'
                  and 'Discrepância' in c.args[1]]
        self.assertEqual(len(avisos), 1)
        self.assertIn('MAIOR', avisos[0].args[2])
        self.assertIn('CORREÇÃO', avisos[0].args[2])

    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_botao_reenviar_manda_de_novo_marcado(self, m_mail):
        b = self._boleto(150000)
        verificacao.processar(b.pk)
        self.login_admin()
        self.client.post(f'/painel/boleto/{b.pk}/aprovar/')
        pags = self._pagamentos(m_mail)
        self.assertEqual(len(pags), 2)
        self.assertIn('REENVIO', pags[-1].args[2])

    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_valor_menor_aceito_aparece_no_email_e_na_regua(self, m_mail):
        b = self._boleto(149997)  # 1.499,97 × 1.500,00
        verificacao.processar(b.pk)
        corpo = self._pagamentos(m_mail)[-1].args[2]
        self.assertIn('valor DIFERENTE do combinado (R$ 1.500,00): −R$ 0,03',
                      corpo)
        self.login_admin()
        resp = self.client.get('/painel/?m=2026-09')
        self.assertContains(resp, '≠ combinado')
        # centavos a menos NÃO viram e-mail de alerta (só valor MAIOR)
        self.assertFalse([c for c in m_mail.call_args_list
                          if 'Discrepância' in c.args[1]])


class QuantoFaltaTest(BaseSetup):
    """"EU SEMPRE PRECISO VER QUANTO JÁ COLOQUEI E QUANTO FALTA."""

    def test_endpoint_do_cadastro(self):
        Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), parcial=True,
            status=Boleto.Status.APROVADO, valor_extraido=Decimal('500.00'))
        self.login_admin()
        base = {'prestador': self.prestador.pk, 'posto': self.posto1.pk,
                'competencia': '2026-09-01'}
        d = self.client.get('/painel/parciais-status/', base).json()
        self.assertIn('Já entrou R$ 500,00', d['texto'])
        self.assertIn('Falta R$ 1.000,00', d['texto'])
        d = self.client.get('/painel/parciais-status/',
                            dict(base, linha=_linha_47(99998))).json()
        self.assertIn('ficará R$ 1.499,98 de R$ 1.500,00', d['texto'])
        self.assertIn('faltará R$ 0,02', d['texto'])
        d = self.client.get('/painel/parciais-status/',
                            dict(base, linha=_linha_47(100000))).json()
        self.assertIn('✅ Fecha', d['texto'])
        d = self.client.get('/painel/parciais-status/',
                            dict(base, linha=_linha_47(100100))).json()
        self.assertIn('PASSA do combinado em R$ 1,00', d['texto'])
        self.assertEqual(d['nivel'], 'ruim')

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_regua_e_secao_mostram_falta_e_botao_parcial(self, m_async):
        Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date.today().replace(day=1), parcial=True,
            status=Boleto.Status.APROVADO, valor_extraido=Decimal('500.00'))
        self.login_admin()
        resp = self.client.get('/painel/')
        self.assertContains(resp, 'falta R$ 1.000,00')
        self.assertContains(resp, 'cadastrar parcial')
        self.assertContains(resp, 'já entrou <strong>R$ 500,00')
        # botão pré-preenche o cadastro
        resp = self.client.get(
            f'/painel/boleto/novo/?prestador={self.prestador.pk}'
            f'&posto={self.posto1.pk}&competencia=2026-09&parcial=1')
        import re as _re
        self.assertTrue(_re.search(r'name="parcial"[^>]*checked',
                                   resp.content.decode()))


class FiltroPainelTest(BaseSetup):
    """Filtrar o painel por prestador/posto vira tela de conferência:
    previsto × boletos até aqui × falta, e a tabela só do filtro."""

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_resumo_por_prestador_e_por_posto(self, m_async):
        mes = date.today().replace(day=1)
        outro = Prestador.objects.create(nome='Outro PJ')
        PrestadorPosto.objects.create(prestador=outro, posto=self.posto2,
                                      valor_mensal=Decimal('700.00'))
        # Anchieta: 2 parciais (500 + 999,99) de 1.500; Bangu: cheio 2.000
        for v in ('500.00', '999.99'):
            Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                                  competencia=mes, parcial=True,
                                  status=Boleto.Status.APROVADO,
                                  valor_extraido=Decimal(v))
        Boleto.objects.create(prestador=self.prestador, posto=self.posto2,
                              competencia=mes, status=Boleto.Status.PAGO,
                              valor_extraido=Decimal('2000.00'))
        self.login_admin()
        self.prestador.representante = 'Ivanildo'
        self.prestador.representante_nome_social = 'Guido Cerqueira'
        self.prestador.save()
        resp = self.client.get(f'/painel/?prestador={self.prestador.pk}')
        self.assertContains(resp, '🧮 Limpeza Total LTDA')
        self.assertContains(resp, '👤 Guido Cerqueira')
        self.assertContains(resp, 'R$ 3.500,00')   # previsto
        self.assertContains(resp, 'R$ 3.499,99')   # até aqui
        self.assertContains(resp, '⏳ R$ 0,01')     # falta
        self.assertNotContains(resp, 'Outro PJ</strong>')
        resp = self.client.get(f'/painel/?posto={self.posto2.pk}')
        self.assertContains(resp, 'R$ 2.700,00')   # 2.000 + 700 previstos
        self.assertContains(resp, 'Outro PJ')
        self.assertNotContains(resp, 'Anchieta</td>')
        # setas do mês carregam o filtro
        self.assertContains(resp, f'&amp;posto={self.posto2.pk}">→</a>')


class EmailsAvisoTest(BaseSetup):
    """Rosana não tem login, mas precisa receber os avisos."""

    def test_emails_aviso_entram_nos_destinatarios(self):
        self.prestador.emails_aviso = 'Profissionaledfisica10@gmail.com; x@y.com'
        self.prestador.save()
        b = Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                                  competencia=date(2026, 9, 1))
        dest = verificacao.destinatarios_pj(b)
        self.assertIn('profissionaledfisica10@gmail.com', dest)
        self.assertIn('x@y.com', dest)
        self.assertIn('pj@empresa.com.br', dest)  # usuário continua
        # prestador SEM usuário nenhum: só o e-mail de aviso (não cai no admin)
        self.up.delete()
        self.assertEqual(verificacao.destinatarios_pj(b),
                         ['profissionaledfisica10@gmail.com', 'x@y.com'])

    def test_form_valida_e_normaliza(self):
        from core.forms import PrestadorForm
        f = PrestadorForm({'nome': 'Rosana', 'modo_boleto': 'UNICO',
                           'ativo': 'on',
                           'emails_aviso': ' A@B.com ,inválido '})
        self.assertFalse(f.is_valid())
        self.assertIn('inválido', str(f.errors))
        f = PrestadorForm({'nome': 'Rosana', 'modo_boleto': 'UNICO',
                           'ativo': 'on', 'emails_aviso': ' A@B.com ; c@d.com'})
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data['emails_aviso'], 'a@b.com, c@d.com')


class RoboEmailRemetenteTest(BaseSetup):
    """Guido não tem idCamim: o e-mail dele, cadastrado no prestador, vale
    como remetente; qualquer outro continua "sem cadastro"."""

    def test_remetente_sem_login_cadastrado_no_prestador(self):
        from core.management.commands.importar_emails_pj import (
            prestador_do_remetente)
        self.assertEqual(prestador_do_remetente('pj@empresa.com.br'),
                         self.prestador)
        self.assertIsNone(prestador_do_remetente('euguidocerqueira@gmail.com'))
        self.prestador.emails_aviso = 'euguidocerqueira@gmail.com'
        self.prestador.save()
        self.assertEqual(prestador_do_remetente('euguidocerqueira@gmail.com'),
                         self.prestador)
        self.assertIsNone(prestador_do_remetente('outro@gmail.com'))

    def test_resposta_do_financeiro_casa_mesmo_se_o_posto_mudou(self):
        from core.services.boletos import localizar_boleto_por_assunto
        b = Boleto.objects.create(prestador=self.prestador, posto=self.posto2,
                                  competencia=date(2026, 9, 1),
                                  status=Boleto.Status.APROVADO,
                                  valor_extraido=Decimal('499.93'))
        # e-mail saiu como "Anchieta", boleto foi movido para Bangu
        self.assertEqual(localizar_boleto_por_assunto(
            'Re: Pagamento — Limpeza Total LTDA — Anchieta — setembro/2026 '
            '— R$ 499,93'), b)
        # sem valor exato, não arrisca
        self.assertIsNone(localizar_boleto_por_assunto(
            'Re: Pagamento — Limpeza Total LTDA — Anchieta — setembro/2026 '
            '— R$ 499,94'))

    def test_resposta_do_financeiro_casa_mesmo_se_o_mes_mudou(self):
        from core.services.boletos import localizar_boleto_por_assunto
        b = Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                                  competencia=date(2026, 9, 1),
                                  status=Boleto.Status.APROVADO,
                                  valor_extraido=Decimal('13700.00'))
        self.assertEqual(localizar_boleto_por_assunto(
            'Re: Pagamento — Limpeza Total LTDA — Anchieta — agosto/2026 — '
            'R$ 13.700,00'), b)

    def test_nf_escaneada_reconhecida_pelo_nome(self):
        from core.management.commands.importar_emails_pj import classificar_pdf
        self.assertEqual(classificar_pdf('NF Agosto Guido.pdf', ''), 'nf')
        self.assertEqual(classificar_pdf('nota-fiscal.pdf', ' '), 'nf')
        self.assertEqual(classificar_pdf('BOLETO GUIDO AGOSTO .pdf', ''),
                         'boleto')
        self.assertEqual(classificar_pdf('NF.pdf', 'Boleto Pix R$ 13.700,00'),
                         'boleto')  # com texto, o texto manda


class RepresentanteTest(BaseSetup):
    def test_contato_e_saudacao(self):
        from core.forms import PrestadorForm
        from core.services import frases
        self.assertEqual(self.prestador.contato, 'Limpeza Total LTDA')
        self.prestador.representante = 'Guido Cerqueira'
        self.assertEqual(self.prestador.contato, 'Guido Cerqueira')
        self.prestador.representante_nome_social = 'Gui'
        self.assertEqual(self.prestador.contato, 'Gui')
        b = Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                                  competencia=date(2026, 9, 1))
        fatos = verificacao._fatos(b)
        self.assertEqual(fatos['contato'], 'Gui')
        with mock.patch('random.choice', side_effect=lambda pool: pool[1]):
            corpo = frases.corpo('aprovado_pj', fatos)
        self.assertTrue(corpo.startswith('Prezado(a) Gui,'), corpo)
        # form: representante vazio vira o nome da empresa
        f = PrestadorForm({'nome': 'Empresa X', 'modo_boleto': 'UNICO',
                           'ativo': 'on', 'representante': ''})
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data['representante'], 'Empresa X')


class RegimePagamentoTest(BaseSetup):
    """Serviço prestado em X / Competência / Pagamento em Y nos e-mails."""

    def _dados(self):
        b = Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                                  competencia=date(2026, 9, 1),
                                  valor_extraido=Decimal('1500.00'))
        return verificacao.dados_pj(b, verificacao._fatos(b)), b

    def test_pagador_e_saudado_como_equipe_e_ve_o_representante(self):
        self.prestador.representante = 'Guido Cerqueira'
        self.prestador.save()
        b = Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                                  competencia=date(2026, 9, 1),
                                  valor_extraido=Decimal('1500.00'),
                                  linha_digitavel=_linha_47(150000))
        with mock.patch('core.services.emails.enviar',
                        return_value=True) as m_mail, \
             mock.patch('core.services.ia.redigir_email',
                        side_effect=RuntimeError('off')):
            verificacao.enviar_para_pagamento(b)
        pag = [c for c in m_mail.call_args_list
               if c.args[0] == 'equipe@camim.com.br'][-1].args[2]
        self.assertTrue(pag.startswith('Prezada equipe do setor financeiro,'),
                        pag)
        self.assertNotIn('Prezado(a) Guido', pag)
        self.assertIn('Representante: Guido Cerqueira', pag)
        pj = [c for c in m_mail.call_args_list
              if c.args[0] == ['pj@empresa.com.br']][-1].args[2]
        self.assertTrue(pj.startswith('Prezado'), pj)
        self.assertIn('Representante: Guido Cerqueira', pj)

    def test_padrao_e_mes_seguinte(self):
        self.assertEqual(Prestador().regime_pagamento,
                         Prestador.Regime.POSTERIOR)

    def test_vigente(self):
        self.prestador.regime_pagamento = Prestador.Regime.VIGENTE
        self.prestador.save()
        dados, b = self._dados()
        self.assertNotIn('Representante:', dados)  # igual ao nome: omite
        self.assertIn('Serviço prestado em: setembro/2026', dados)
        self.assertIn('Competência: setembro/2026', dados)
        self.assertNotIn('Pagamento em:', dados)
        self.assertNotIn('Vencimento', dados)

    def test_posterior_com_prazos_do_contrato(self):
        self.prestador.dia_pagamento = 10
        self.prestador.dia_vencimento = 5
        self.prestador.save()
        self.assertEqual(self.prestador.mes_servico(date(2026, 1, 1)),
                         date(2025, 12, 1))
        dados, b = self._dados()
        self.assertIn('Serviço prestado em: agosto/2026', dados)
        self.assertIn('Competência: agosto/2026', dados)
        self.assertIn('Pagamento em: setembro/2026', dados)
        self.assertIn('Pagamento previsto: dia 10 de setembro/2026', dados)
        self.assertIn('Vencimento: dia 5 (previsto no contrato)', dados)
        pag = verificacao.dados_pagamento(b, verificacao._fatos(b))
        self.assertIn('Pagamento em: setembro/2026', pag)
        # o assunto continua pelo mês do pagamento (chave do financeiro)
        self.assertEqual(verificacao._fatos(b)['competencia'],
                         'setembro/2026')

    def test_contrato_preenche_so_o_que_esta_em_branco(self):
        from core.models import Contrato
        from core.services.contratos import aplicar_dados
        self.prestador.dia_vencimento = 20  # já digitado: não mexe
        self.prestador.representante = self.prestador.nome  # backfill = vazio
        self.prestador.cidade = 'Niterói'  # já digitado: não mexe
        self.prestador.save()
        c = Contrato.objects.create(prestador=self.prestador,
                                    arquivo=_pdf('contrato.pdf'),
                                    nome_original='contrato.pdf')
        lido = {'dia_pagamento': 10, 'dia_vencimento': 5,
                'regime': 'POSTERIOR', 'trecho': 'até o dia 10',
                'cnpj': '31.603.189/0001-30',
                'endereco': 'Avenida das Américas nº 4200 Bloco 1 Sala 305',
                'bairro': 'Barra da Tijuca', 'cidade': 'Rio de Janeiro',
                'uf': 'RJ', 'cep': '22640907',
                'representante': 'Vinícius Correa Albuquerque',
                'representante_cpf': '134.595.677-08'}
        with mock.patch('core.services.pdf.extrair_texto',
                        return_value='JRA CONSULTORIA ...'), \
             mock.patch('core.services.ia.extrair_dados_contrato',
                        return_value=lido):
            lidos = aplicar_dados(c)
        p = Prestador.objects.get(pk=self.prestador.pk)
        self.assertEqual(p.dia_pagamento, 10)
        self.assertEqual(p.dia_vencimento, 20)
        self.assertEqual(p.cidade, 'Niterói')
        self.assertEqual(p.endereco,
                         'Avenida das Américas nº 4200 Bloco 1 Sala 305')
        self.assertEqual(p.bairro, 'Barra da Tijuca')
        self.assertEqual(p.uf, 'RJ')
        self.assertEqual(p.cep, '22640-907')
        self.assertEqual(p.cnpj, '31.603.189/0001-30')
        self.assertEqual(p.representante, 'Vinícius Correa Albuquerque')
        self.assertEqual(p.representante_cpf, '134.595.677-08')
        self.assertEqual(lidos['regime_sugerido'], 'POSTERIOR')
        self.assertIn('Barra da Tijuca', p.endereco_completo)
        # IA fora do ar: nada quebra, nada muda
        p.cidade = ''
        p.save()
        with mock.patch('core.services.pdf.extrair_texto', return_value='x'), \
             mock.patch('core.services.ia.extrair_dados_contrato',
                        side_effect=RuntimeError('off')):
            self.assertEqual(aplicar_dados(c), {})


class AjusteDiferencaTest(BaseSetup):
    """Diferença < R$ 5: 'Pagar em dinheiro' / 'Não será pago' quitam a
    linha; 'desfazer' reabre."""

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_parciais_quase_fechando(self, m_async):
        mes = date.today().replace(day=1)
        for v in ('500.00', '499.99'):
            Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                                  competencia=mes, parcial=True,
                                  status=Boleto.Status.APROVADO,
                                  valor_extraido=Decimal(v))
        # Bangu: boleto único 1.999,90 × 2.000 → falta 0,10
        Boleto.objects.create(prestador=self.prestador, posto=self.posto2,
                              competencia=mes, status=Boleto.Status.APROVADO,
                              valor_extraido=Decimal('1999.90'))
        self.login_admin()
        resp = self.client.get('/painel/')
        self.assertContains(resp, 'falta R$ 500,01')
        self.assertContains(resp, '💵 R$ 0,10 em dinheiro', count=1)  # só Bangu
        resp = self.client.post('/painel/ajuste-diferenca/', {
            'prestador': self.prestador.pk, 'posto': self.posto2.pk,
            'competencia': mes.isoformat(), 'valor': '0.10',
            'modo': 'NAO_PAGO'}, follow=True)
        self.assertContains(resp, 'R$ 0,10 não será pago — quitado')
        self.assertContains(resp, '↩ desfazer')
        self.assertNotContains(resp, 'em dinheiro</button>')
        # > R$ 5 não passa
        resp = self.client.post('/painel/ajuste-diferenca/', {
            'prestador': self.prestador.pk, 'posto': self.posto1.pk,
            'competencia': mes.isoformat(), 'valor': '500.01',
            'modo': 'DINHEIRO'}, follow=True)
        self.assertContains(resp, 'menores que R$ 5,00')
        # parciais chegando a 0,01 de falta → botões
        Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                              competencia=mes, parcial=True,
                              status=Boleto.Status.APROVADO,
                              valor_extraido=Decimal('500.00'))
        resp = self.client.get('/painel/')
        self.assertContains(resp, 'falta R$ 0,01')
        self.assertContains(resp, 'Resolver os R$ 0,01:', count=1)
        resp = self.client.post('/painel/ajuste-diferenca/', {
            'prestador': self.prestador.pk, 'posto': self.posto1.pk,
            'competencia': mes.isoformat(), 'valor': '0.01',
            'modo': 'DINHEIRO'}, follow=True)
        self.assertContains(resp, '✅ completa — R$ 0,01 pago em dinheiro')
        self.assertNotContains(resp, '➕ parcial')
        # resumo do filtro conta o ajuste
        resp = self.client.get(f'/painel/?prestador={self.prestador.pk}')
        self.assertContains(resp, 'Em dinheiro / não pago')
        self.assertContains(resp, '✅ R$ 0,00 — completo')
        # desfazer
        self.client.post('/painel/ajuste-diferenca/', {
            'prestador': self.prestador.pk, 'posto': self.posto1.pk,
            'competencia': mes.isoformat(), 'valor': '0', 'modo': 'DESFAZER'})
        resp = self.client.get('/painel/')
        self.assertContains(resp, 'falta R$ 0,01')
        # SEM BOLETO com combinado ínfimo (Salliseg: R$ 0,40)
        sal = Prestador.objects.create(nome='Salliseg')
        PrestadorPosto.objects.create(prestador=sal, posto=self.posto2,
                                      valor_mensal=Decimal('0.40'))
        resp = self.client.get('/painel/')
        self.assertContains(resp, 'SEM BOLETO')
        self.assertContains(resp, '🚫 R$ 0,40 não será pago', count=1)
        resp = self.client.post('/painel/ajuste-diferenca/', {
            'prestador': sal.pk, 'posto': self.posto2.pk,
            'competencia': mes.isoformat(), 'valor': '0.40',
            'modo': 'NAO_PAGO'}, follow=True)
        self.assertContains(resp, 'QUITADO')
        self.assertNotContains(resp, 'SEM BOLETO')


class CpfRepresentanteTest(BaseSetup):
    def test_valida_e_formata(self):
        from core.forms import validar_cpf
        from django import forms as djforms
        self.assertEqual(validar_cpf('02930362413'), '029.303.624-13')
        self.assertEqual(validar_cpf(' 029.303.624-13 '), '029.303.624-13')
        self.assertEqual(validar_cpf(''), '')
        for ruim in ('123', '111.111.111-11', '029.303.624-14'):
            with self.assertRaises(djforms.ValidationError):
                validar_cpf(ruim)
        from core.forms import PrestadorForm
        f = PrestadorForm({'nome': 'X', 'modo_boleto': 'UNICO', 'ativo': 'on',
                           'representante_cpf': '029.303.624-13'})
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.cleaned_data['representante_cpf'],
                         '029.303.624-13')


class HomePjTest(BaseSetup):
    def test_lista_60_dias_e_ver_todos(self):
        from django.utils import timezone
        for i in range(9):
            Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                                  competencia=date(2026, 9, 1),
                                  valor_extraido=Decimal('10.00') * (i + 1))
        antigo = Boleto.objects.create(prestador=self.prestador,
                                       posto=self.posto2,
                                       competencia=date(2026, 5, 1))
        Boleto.objects.filter(pk=antigo.pk).update(
            criado_em=timezone.now() - timezone.timedelta(days=90))
        self.login_pj()
        resp = self.client.get('/')
        self.assertContains(resp, 'últimos 60 dias')
        self.assertContains(resp, 'R$ 90,00')  # os 9 aparecem
        self.assertNotContains(resp, '05/2026')
        self.assertContains(resp, 'ver todos (1 mais antigo)')
        resp = self.client.get('/?todos=1')
        self.assertContains(resp, '05/2026')
        self.assertContains(resp, 'Todos os seus boletos')


class AnexarPorPostoTest(BaseSetup):
    """PJ com vários postos: quadrados por posto → formulário do posto →
    volta aos quadrados."""

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_quadrados_e_fluxo(self, m_async):
        self.login_pj()
        resp = self.client.get('/boleto/')
        self.assertContains(resp, 'Anchieta')
        self.assertContains(resp, 'Bangu')
        self.assertContains(resp, 'Falta anexar', count=2)
        self.assertContains(resp, '0 de 2')
        # toca em Anchieta → formulário com o posto travado
        resp = self.client.get(f'/boleto/?posto={self.posto1.pk}')
        self.assertContains(resp, 'Anexar boleto — Anchieta')
        self.assertContains(resp, 'type="hidden" name="posto"')
        self.assertContains(resp, 'trocar de posto')
        hoje = date.today().replace(day=1)
        resp = self.client.post('/boleto/', {
            'competencia': hoje.isoformat(), 'posto': self.posto1.pk,
            'arquivo': _pdf()})
        self.assertRedirects(resp, '/boleto/')
        resp = self.client.get('/boleto/')
        self.assertContains(resp, '1 de 2')
        self.assertContains(resp, 'Falta anexar', count=1)
        self.assertContains(resp, 'toque para anexar outro')
        self.assertEqual(Boleto.objects.get().posto, self.posto1)

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_boleto_unico_vai_direto_ao_formulario(self, m_async):
        self.prestador.modo_boleto = Prestador.ModoBoleto.UNICO
        self.prestador.posto_cobranca = self.posto1
        self.prestador.save()
        self.login_pj()
        resp = self.client.get('/boleto/')
        self.assertContains(resp, 'ENVIAR BOLETO')
        self.assertNotContains(resp, 'Falta anexar')


class ContratoBoletoUnicoTest(BaseSetup):
    def test_pj_de_boleto_unico_ve_o_posto_de_cobranca(self):
        self.prestador.modo_boleto = Prestador.ModoBoleto.UNICO
        self.prestador.posto_cobranca = Posto.objects.get(codigo='C')
        self.prestador.save()
        PrestadorPosto.objects.filter(prestador=self.prestador).delete()
        self.login_pj()
        resp = self.client.get('/contratos/', follow=True)
        self.assertNotContains(resp, 'Nenhum posto vinculado')
        self.assertContains(resp, self.prestador.posto_cobranca.nome)
        with mock.patch('core.services.contratos.aplicar_dados_async') as m_ap:
            resp = self.client.post(
                f'/contratos/{self.prestador.posto_cobranca.pk}/',
                {'arquivo': _pdf('contrato.pdf'),
                 'vigencia_inicio': '2026-01-01', 'vigencia_fim': ''})
        self.assertTrue(m_ap.called)
        self.assertEqual(Contrato.objects.filter(
            prestador=self.prestador).count(), 1)
        # Fátima: contrato com Anchieta, paga por Nilópolis → vê os dois
        Contrato.objects.create(prestador=self.prestador, posto=self.posto1,
                                arquivo=_pdf('c2.pdf'), nome_original='c2')
        resp = self.client.get('/contratos/')
        self.assertContains(resp, 'Anchieta')
        self.assertContains(resp, self.prestador.posto_cobranca.nome)


class ReceitaTest(BaseSetup):
    RESP = {'razao_social': 'MRF INFORMATICA LTDA', 'nome_fantasia': 'MRF Solution',
            'descricao_tipo_de_logradouro': 'Avenida',
            'logradouro': 'Ernani do Amaral Peixoto', 'numero': '300',
            'complemento': 'Sala 1021', 'bairro': 'Centro',
            'municipio': 'Niterói', 'uf': 'RJ', 'cep': '24020076',
            'ddd_telefone_1': '2122102114', 'email': 'ROBSON@GMAIL.COM',
            'descricao_situacao_cadastral': 'Ativa',
            'data_inicio_atividade': '2000-06-21',
            'qsa': [{'nome_socio': 'MILENA MACIEL DE CARVALHO',
                     'qualificacao_socio': 'Sócio'},
                    {'nome_socio': 'ROBSON DE JESUS FERNANDES',
                     'qualificacao_socio': 'Sócio-Administrador'}]}

    def test_ficha_preenche_so_o_que_esta_em_branco(self):
        self.prestador.cnpj = '03894412000100'
        self.prestador.cidade = 'Rio'  # já digitado: fica
        self.prestador.representante = self.prestador.nome  # = vazio
        self.prestador.save()
        self.login_admin()
        fake = mock.Mock(status_code=200, json=lambda: self.RESP)
        fake.raise_for_status = lambda: None
        with mock.patch('core.services.receita.requests.get',
                        return_value=fake):
            resp = self.client.post(f'/painel/prestadores/{self.prestador.pk}/',
                                    {'qual': 'receita'}, follow=True)
        p = Prestador.objects.get(pk=self.prestador.pk)
        self.assertEqual(p.cnpj, '03.894.412/0001-00')
        self.assertEqual(p.razao_social, 'MRF INFORMATICA LTDA')
        self.assertEqual(p.endereco, 'Avenida Ernani do Amaral Peixoto nº 300 Sala 1021')
        self.assertEqual(p.cidade, 'Rio')
        self.assertEqual(p.cep, '24020-076')
        self.assertEqual(p.telefone, '(21) 22102114')
        self.assertEqual(p.email_empresa, 'robson@gmail.com')
        self.assertEqual(p.situacao_cadastral, 'Ativa')
        self.assertEqual(p.abertura, date(2000, 6, 21))
        self.assertEqual(p.representante, 'Robson De Jesus Fernandes')
        self.assertIn('MILENA MACIEL DE CARVALHO — Sócio', p.lista_socios)
        self.assertContains(resp, 'MRF INFORMATICA LTDA')
        self.assertContains(resp, 'Sócios e administradores')
        self.assertContains(resp, 'Ativa')
        # API fora: mensagem, nada quebra
        with mock.patch('core.services.receita.requests.get',
                        side_effect=RuntimeError('timeout')):
            resp = self.client.post(f'/painel/prestadores/{self.prestador.pk}/',
                                    {'qual': 'receita'}, follow=True)
        self.assertContains(resp, 'Não consegui consultar a Receita')


class TravaCopiasTest(BaseSetup):
    """01/09/2026: um script mandou UM e-mail com os 9 boletos da GP5 em
    cópia para os 9 gerentes — cada gerente viu o valor dos outros postos
    (empresas e sócios diferentes). Nunca mais: mais de 1 cc = recusado."""

    @mock.patch('core.services.emails.EmailMultiAlternatives')
    def test_mais_de_um_cc_e_recusado(self, m_msg):
        from core.models import EmailLog
        from core.services import emails
        ok = emails.enviar(['pj@x.com'], 'Boletos', 'corpo',
                           cc=['g1@camim.com.br', 'g2@camim.com.br'])
        self.assertFalse(ok)
        self.assertFalse(m_msg.called)  # nada saiu
        log = EmailLog.objects.get()
        self.assertFalse(log.ok)
        self.assertIn('RECUSADO', log.assunto)
        # um gerente em cópia continua normal
        ok = emails.enviar(['pj@x.com'], 'Boleto', 'corpo',
                           cc=['g1@camim.com.br'])
        self.assertTrue(ok)


class ListaPrestadoresTest(BaseSetup):
    def test_boleto_unico_mostra_o_posto_de_cobranca(self):
        self.prestador.modo_boleto = Prestador.ModoBoleto.UNICO
        self.prestador.posto_cobranca = Posto.objects.get(codigo='R')
        self.prestador.valor_unico = Decimal('3990.00')
        self.prestador.save()
        self.login_admin()
        resp = self.client.get('/painel/prestadores/')
        self.assertContains(resp, 'Realengo</a>')  # vermelho: sem contrato
        self.assertContains(resp, 'R$ 3.990,00')

    def test_posto_sem_contrato_vigente_fica_vermelho_com_link(self):
        # Anchieta com contrato vigente; Bangu com contrato VENCIDO
        Contrato.objects.create(prestador=self.prestador, posto=self.posto1,
                                arquivo=_pdf('a.pdf'), nome_original='a',
                                vigencia_fim=date(2099, 1, 1))
        Contrato.objects.create(prestador=self.prestador, posto=self.posto2,
                                arquivo=_pdf('b.pdf'), nome_original='b',
                                vigencia_fim=date(2020, 1, 1))
        self.login_admin()
        resp = self.client.get('/painel/prestadores/')
        self.assertContains(resp, '<span title="contrato vigente">Anchieta')
        self.assertContains(resp, 'R$ 1.500,00')   # valor de Anchieta
        self.assertContains(resp, 'Total mensal: R$ 3.500,00')
        self.assertContains(resp, f'?anexar={self.posto2.pk}#contratos')
        self.assertContains(resp, '1 sem contrato vigente')
        # o link abre a página com "Anexar contrato" aberto e o posto escolhido
        resp = self.client.get(f'/painel/prestadores/{self.prestador.pk}/'
                               f'?anexar={self.posto2.pk}')
        self.assertContains(resp, 'Bangu está sem contrato vigente')
        self.assertContains(resp, f'<option value="{self.posto2.pk}" selected')
        # contrato GERAL (sem posto) cobre todos
        Contrato.objects.create(prestador=self.prestador, posto=None,
                                arquivo=_pdf('g.pdf'), nome_original='g')
        resp = self.client.get('/painel/prestadores/')
        self.assertNotContains(resp, 'sem contrato vigente')


class ValeTest(BaseSetup):
    def _vale(self, **kw):
        from core.models import Vale
        base = dict(prestador=self.prestador, posto=self.posto1,
                    descricao='Notebook', valor_parcela=Decimal('600.00'),
                    parcelas_total=7,
                    primeira_competencia=date(2026, 7, 1))
        base.update(kw)
        return Vale.objects.create(**base)

    def test_parcela_abate_o_esperado_no_periodo(self):
        from core.services.boletos import valor_esperado_para
        self._vale()
        # setembro = parcela 3/7 → 1500 - 600
        self.assertEqual(
            valor_esperado_para(self.prestador, self.posto1, date(2026, 9, 1)),
            Decimal('900.00'))
        # depois da 7ª parcela (fev/2027) volta ao cheio
        self.assertEqual(
            valor_esperado_para(self.prestador, self.posto1, date(2027, 2, 1)),
            Decimal('1500.00'))
        # antes da 1ª também
        self.assertEqual(
            valor_esperado_para(self.prestador, self.posto1, date(2026, 6, 1)),
            Decimal('1500.00'))
        # outro posto não é afetado
        self.assertEqual(
            valor_esperado_para(self.prestador, self.posto2, date(2026, 9, 1)),
            Decimal('2000.00'))

    def test_parcelas_pendentes_somem_com_boleto_aprovado(self):
        v = self._vale()  # 7x desde jul/2026 no posto1
        # jul e ago já tiveram boletos aprovados com o desconto
        for mes in (7, 8):
            Boleto.objects.create(
                prestador=self.prestador, posto=self.posto1,
                competencia=date(2026, mes, 1), arquivo=_pdf(),
                status=Boleto.Status.APROVADO)
        pendentes = v.parcelas_pendentes()
        self.assertEqual([p['n'] for p in pendentes], [3, 4, 5, 6, 7])
        self.assertTrue(pendentes[0]['atual'])       # set/2026 é a atual
        self.assertFalse(pendentes[0]['atrasada'])
        # boleto de outro posto não consome parcela
        Boleto.objects.create(
            prestador=self.prestador, posto=self.posto2,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.APROVADO)
        self.assertEqual(len(v.parcelas_pendentes()), 5)

    def test_vale_encerrado_nao_abate(self):
        v = self._vale(ativo=False)
        from core.services.boletos import valor_esperado_para
        self.assertEqual(
            valor_esperado_para(self.prestador, self.posto1, date(2026, 9, 1)),
            Decimal('1500.00'))

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('900.00'),
                              '{"valor":"900.00","confianca":100}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_boleto_com_vale_aprova_e_cita_desconto(self, m_pdf, m_ia,
                                                    m_mail):
        from core.services import boletos as svc
        self._vale()
        b = svc.registrar(self.prestador, date(2026, 9, 1),
                          enviado_por='pj@empresa.com.br',
                          posto=self.posto1, arquivo=_pdf())
        self.assertEqual(b.valor_esperado, Decimal('900.00'))
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        corpo = next(c.args[2] for c in m_mail.call_args_list
                     if c.args[0] == 'equipe@camim.com.br')
        self.assertIn('parcela 3/7', corpo)
        self.assertIn('Notebook', corpo)


class FavorecidoTest(BaseSetup):
    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.pdf.extrair_texto',
                return_value='Beneficiário: OUTRA EMPRESA '
                             'CNPJ 99.999.999/0001-99')
    def test_cnpj_do_prestador_ausente_vira_manual(self, m_pdf, m_mail):
        self.prestador.cnpj = '11.222.333/0001-44'
        self.prestador.save()
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            valor_esperado=Decimal('1500.00'))
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.MANUAL)
        destinos = _destinos(m_mail)
        self.assertNotIn('equipe@camim.com.br', destinos)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1500.00'),
                              '{"valor":"1500.00","confianca":100}'))
    @mock.patch('core.services.pdf.extrair_texto',
                return_value='Beneficiário CNPJ 11.222.333/0001-44 ok')
    def test_cnpj_do_prestador_presente_segue_o_fluxo(self, m_pdf, m_ia,
                                                      m_mail):
        self.prestador.cnpj = '11.222.333/0001-44'
        self.prestador.save()
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            valor_esperado=Decimal('1500.00'))
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)


class IdentificarPostoTest(BaseSetup):
    def test_identifica_pelo_cnpj_do_sacado(self):
        from core.services.boletos import identificar_posto
        texto = ('BOLETO... Sacado: CAMIM ANCHIETA '
                 'CNPJ: 27.110.113/0001-04 ...')
        self.assertEqual(identificar_posto(texto), self.posto1)  # Anchieta
        self.assertIsNone(identificar_posto('sem cnpj nenhum aqui'))

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1500.00'),
                              '{"valor":"1500.00","confianca":100}'))
    @mock.patch('core.services.pdf.extrair_texto',
                return_value='Sacado CNPJ 27.110.113/0001-04 valor 1500')
    def test_processar_destina_posto_pelo_cnpj(self, m_pdf, m_ia, m_mail):
        b = Boleto.objects.create(
            prestador=self.prestador, posto=None,
            competencia=date(2026, 9, 1), arquivo=_pdf())
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.posto, self.posto1)                 # Anchieta
        self.assertEqual(b.valor_esperado, Decimal('1500.00'))  # do vínculo
        self.assertEqual(b.status, Boleto.Status.APROVADO)


class MultiplosPdfsTest(BaseSetup):
    def test_boletos_sem_posto_nao_se_substituem(self):
        """9 PDFs no mesmo e-mail (posto ainda indefinido) são 9 boletos
        distintos — o bug era o último substituir todos os anteriores."""
        from core.services import boletos as svc
        hoje = date(2026, 9, 1)
        for i in range(3):
            svc.registrar(self.prestador, hoje, enviado_por='pj@empresa.com.br',
                          posto=None, nome_original=f'pdf{i}.pdf')
        vivos = Boleto.objects.exclude(status=Boleto.Status.SUBSTITUIDO)
        self.assertEqual(vivos.count(), 3)

    def test_com_posto_definido_continua_substituindo(self):
        from core.services import boletos as svc
        hoje = date(2026, 9, 1)
        svc.registrar(self.prestador, hoje, enviado_por='x@x.com',
                      posto=self.posto1)
        svc.registrar(self.prestador, hoje, enviado_por='x@x.com',
                      posto=self.posto1)
        status = list(Boleto.objects.order_by('pk')
                      .values_list('status', flat=True))
        self.assertEqual(status, [Boleto.Status.SUBSTITUIDO,
                                  Boleto.Status.RECEBIDO])


class BoletoImagemTest(BaseSetup):
    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_admin_cadastra_imagem_com_linha(self, m_async):
        self.login_admin()
        img = SimpleUploadedFile('boleto-colado.png', b'\x89PNG fake',
                                 content_type='image/png')
        resp = self.client.post('/painel/boleto/novo/', {
            'prestador': self.prestador.pk, 'posto': self.posto1.pk,
            'competencia': date.today().replace(day=1).isoformat(),
            'arquivo': img, 'linha_digitavel': _linha_47(150000)})
        self.assertEqual(resp.status_code, 302)
        b = Boleto.objects.get()
        self.assertTrue(b.arquivo.name.endswith('.png'))

    def test_imagem_sem_linha_e_recusada_no_form(self):
        self.login_admin()
        img = SimpleUploadedFile('print.png', b'\x89PNG fake',
                                 content_type='image/png')
        resp = self.client.post('/painel/boleto/novo/', {
            'prestador': self.prestador.pk, 'posto': self.posto1.pk,
            'competencia': date.today().replace(day=1).isoformat(),
            'arquivo': img})
        self.assertEqual(resp.status_code, 200)  # volta com erro
        self.assertEqual(Boleto.objects.count(), 0)

    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_imagem_com_linha_confere_pelo_codigo(self, m_mail):
        img = SimpleUploadedFile('boleto.png', b'\x89PNG fake',
                                 content_type='image/png')
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=img,
            linha_digitavel=_linha_47(150000),
            valor_esperado=Decimal('1500.00'))
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        self.assertEqual(b.valor_extraido, Decimal('1500.00'))


class VerComoTest(BaseSetup):
    def test_admin_ve_portal_como_pj_e_volta(self):
        self.login_admin()
        self.client.post(f'/painel/ver-como/{self.prestador.pk}/')
        resp = self.client.get('/')
        self.assertContains(resp, 'ANEXAR BOLETO')
        self.assertContains(resp, 'Modo visualização')
        # no modo "ver como", o painel fica inacessível até sair
        self.assertEqual(self.client.get('/painel/').status_code, 302)
        self.client.get('/sair-ver-como/')
        self.assertEqual(self.client.get('/painel/').status_code, 200)

    def test_ver_como_abre_o_prestador_certo(self):
        caio = Prestador.objects.create(nome='Caio Augusto')
        teste = Prestador.objects.create(nome='Empresa Teste LTDA')
        self.login_admin()
        self.client.post(f'/painel/ver-como/{caio.pk}/')
        resp = self.client.get('/')
        self.assertContains(resp, 'Caio Augusto')
        self.assertNotContains(resp, 'Empresa Teste')
        # trocar direto para outro prestador (SEM sair antes) tem que
        # funcionar — era o bug do "cliquei no Caio, abriu a Empresa Teste"
        self.client.post(f'/painel/ver-como/{teste.pk}/')
        resp = self.client.get('/')
        self.assertContains(resp, 'Empresa Teste LTDA')

    def test_excluir_prestador_e_soft_delete(self):
        alvo = Prestador.objects.create(nome='Vai Sumir LTDA')
        u = UsuarioPermitido.objects.create(email='sumir@x.com',
                                            prestador=alvo)
        self.login_admin()
        resp = self.client.post(f'/painel/prestadores/{alvo.pk}/excluir/')
        self.assertEqual(resp.status_code, 302)
        alvo.refresh_from_db()
        u.refresh_from_db()
        self.assertIsNotNone(alvo.excluido_em)   # linha continua no banco
        self.assertFalse(alvo.ativo)
        self.assertFalse(u.ativo)                # usuário bloqueado
        self.client.get('/painel/prestadores/')  # consome a msg flash
        resp = self.client.get('/painel/prestadores/')
        self.assertNotContains(resp, 'Vai Sumir')  # some da lista
        # restaurar traz de volta
        self.client.post(f'/painel/prestadores/{alvo.pk}/restaurar/')
        alvo.refresh_from_db()
        self.assertIsNone(alvo.excluido_em)
        self.assertTrue(alvo.ativo)

    def test_pj_nao_consegue_usar_ver_como(self):
        outro = Prestador.objects.create(nome='Outro PJ')
        self.login_pj()
        self.client.post(f'/painel/ver-como/{outro.pk}/')
        self.assertNotIn('ver_como', self.client.session)


class LinhaDigitavelIATest(BaseSetup):
    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor', return_value=(
        Decimal('1500.00'),
        '{"valor":"1500.00","linha_digitavel":'
        '"23793381286000782713695000063305984340000150000"}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='BOLETO')
    def test_ia_extrai_linha_e_vai_no_email_do_pagador(self, m_pdf, m_ia,
                                                       m_mail):
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            valor_esperado=Decimal('1500.00'))
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(len(b.linha_digitavel), 47)
        corpo_pagador = next(c.args[2] for c in m_mail.call_args_list
                             if c.args[0] == 'equipe@camim.com.br')
        self.assertIn('Linha digitável: 2379338', corpo_pagador)


def _linha_47(valor_centavos):
    """Linha digitável fictícia de 47 dígitos com o valor embutido no fim."""
    return '2379338128600078271369500006330' + '9' + '843' \
        + str(valor_centavos).rjust(12, '0')[-12:].rjust(12, '0')


class RegrasNegocioTest(BaseSetup):
    """Valor menor pode (acordo); MAIOR nunca sozinho; código bate com
    valor; sem duplicidade; mês bate."""

    def _boleto(self, **kw):
        base = dict(prestador=self.prestador, posto=self.posto1,
                    competencia=date(2026, 9, 1), arquivo=_pdf(),
                    enviado_por='pj@empresa.com.br',
                    valor_esperado=Decimal('1500.00'))
        base.update(kw)
        return Boleto.objects.create(**base)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1400.00'),
                              '{"valor":"1400.00","confianca":100}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_valor_menor_aprova(self, m_pdf, m_ia, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)

    @mock.patch('core.services.ia.avaliar_diferenca',
                return_value=(True, 'desconto da parcela 3/7 do notebook'))
    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('900.00'),
                              '{"valor":"900.00","confianca":100}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_menor_com_obs_que_explica_aprova_com_motivo(
            self, m_pdf, m_ia, m_mail, m_dif):
        b = self._boleto()
        b.observacao = 'descontada parcela 3/7 do notebook (R$ 600)'
        b.save()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        corpo = next(c.args[2] for c in m_mail.call_args_list
                     if c.args[0] == 'equipe@camim.com.br')
        self.assertIn('parcela 3/7 do notebook', corpo)

    @mock.patch('core.services.ia.avaliar_diferenca',
                return_value=(False, ''))
    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('900.00'), '{"valor":"900.00"}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_menor_com_obs_que_nao_explica_vira_manual(
            self, m_pdf, m_ia, m_mail, m_dif):
        b = self._boleto()
        b.observacao = 'obs qualquer sem relação'
        b.save()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.MANUAL)
        destinos = _destinos(m_mail)
        self.assertNotIn('equipe@camim.com.br', destinos)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1600.00'), '{"valor":"1600.00"}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_valor_maior_nunca_aprova_sozinho(self, m_pdf, m_ia, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.DIVERGENTE)
        destinos = _destinos(m_mail)
        self.assertNotIn('equipe@camim.com.br', destinos)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1600.00'), '{"valor":"1600.00"}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_valor_livre_do_admin_aprova_maior(self, m_pdf, m_ia, m_mail):
        b = self._boleto(valor_livre=True)
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1500.00'), '{"valor":"1500.00"}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_duplicidade_marca_duplicado_sem_email(self, m_pdf, m_ia,
                                                    m_mail):
        self._boleto(status=Boleto.Status.PAGO)
        b2 = self._boleto()
        verificacao.processar(b2.pk)
        b2.refresh_from_db()
        self.assertEqual(b2.status, Boleto.Status.DUPLICADO)
        m_mail.assert_not_called()  # duplicado fica só marcado no painel

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1500.00'),
                              '{"valor":"1500.00","confianca":80}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_confianca_abaixo_do_limiar_espera_liberacao(self, m_pdf, m_ia,
                                                         m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.MANUAL)
        self.assertEqual(b.ia_confianca, 80)
        destinos = _destinos(m_mail)
        self.assertNotIn('equipe@camim.com.br', destinos)  # esperou liberar

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_reenvio_nao_substitui_aprovado(self, m_async):
        aprovado = self._boleto(status=Boleto.Status.APROVADO)
        self.login_pj()
        self.client.post('/boleto/', {
            'competencia': date(2026, 9, 1).isoformat(),
            'posto': self.posto1.pk, 'arquivo': _pdf()})
        aprovado.refresh_from_db()
        self.assertEqual(aprovado.status, Boleto.Status.APROVADO)  # intacto

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor', return_value=(
        Decimal('1500.00'),
        '{"valor":"1500.00","vencimento":"10/12/2026"}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_mes_nao_bate_vira_manual(self, m_pdf, m_ia, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.MANUAL)

    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor', return_value=(
        Decimal('1500.00'),
        '{"valor":"1500.00","linha_digitavel":"%s"}' % _linha_47(999900)))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_codigo_nao_bate_com_pdf_vira_manual(self, m_pdf, m_ia, m_mail):
        b = self._boleto()
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.MANUAL)
        destinos = _destinos(m_mail)
        self.assertNotIn('equipe@camim.com.br', destinos)

    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_sem_pdf_valor_vem_da_linha(self, m_mail):
        b = self._boleto(arquivo=None,
                         linha_digitavel=_linha_47(150000),
                         valor_esperado=Decimal('1500.00'))
        verificacao.processar(b.pk)
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        self.assertEqual(b.valor_extraido, Decimal('1500.00'))

    def test_valor_da_linha(self):
        self.assertEqual(
            verificacao.valor_da_linha(_linha_47(123456)),
            Decimal('1234.56'))
        self.assertIsNone(verificacao.valor_da_linha('123'))
        self.assertIsNone(verificacao.valor_da_linha(''))


class ApiBoletosTest(BaseSetup):
    def setUp(self):
        super().setUp()
        self.up.api_token = 'a' * 48
        self.up.save()
        self.auth = {'HTTP_AUTHORIZATION': 'Bearer ' + 'a' * 48}

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_post_cria_boleto_com_nf(self, m_async):
        resp = self.client.post('/api/boletos/', {
            'competencia': '2026-10', 'posto': 'A',
            'arquivo': _pdf(), 'nota_fiscal': _pdf('nf.pdf')}, **self.auth)
        self.assertEqual(resp.status_code, 201)
        dados = resp.json()
        self.assertEqual(dados['competencia'], '2026-10')
        self.assertEqual(dados['posto'], 'Anchieta')
        self.assertEqual(dados['valor_esperado'], '1500.00')
        self.assertTrue(dados['tem_nota_fiscal'])
        m_async.assert_called_once()

    def test_sem_token_401(self):
        resp = self.client.post('/api/boletos/', {'arquivo': _pdf()})
        self.assertEqual(resp.status_code, 401)

    def test_token_errado_401(self):
        resp = self.client.post('/api/boletos/', {'arquivo': _pdf()},
                                HTTP_AUTHORIZATION='Bearer errado')
        self.assertEqual(resp.status_code, 401)

    def test_exige_nf_barra_sem_nota(self):
        self.prestador.exige_nf = True
        self.prestador.save()
        resp = self.client.post('/api/boletos/', {
            'posto': 'A', 'arquivo': _pdf()}, **self.auth)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('nota fiscal', resp.json()['erro'])

    def test_pdf_invalido_400(self):
        falso = SimpleUploadedFile('b.pdf', b'nao eh pdf')
        resp = self.client.post('/api/boletos/', {
            'posto': 'A', 'arquivo': falso}, **self.auth)
        self.assertEqual(resp.status_code, 400)

    @mock.patch('core.services.verificacao.fluxo_completo_async')
    def test_get_lista_do_mes(self, m_async):
        self.client.post('/api/boletos/', {
            'competencia': '2026-10', 'posto': 'B',
            'arquivo': _pdf()}, **self.auth)
        resp = self.client.get('/api/boletos/?competencia=2026-10',
                               **self.auth)
        self.assertEqual(resp.status_code, 200)
        lista = resp.json()['boletos']
        self.assertEqual(len(lista), 1)
        self.assertEqual(lista[0]['posto'], 'Bangu')


class GerentesPainelTest(BaseSetup):
    def test_lista_e_edita_posto_manual(self):
        manual = Posto.objects.create(nome='Égide Teste')
        self.posto1.gerente_nome = 'Elisangela'
        self.posto1.gerente_email = 'elisangela.rodrigues@clinicacamim.com.br'
        self.posto1.save()
        self.login_admin()
        resp = self.client.get('/painel/gerentes/')
        self.assertContains(resp, 'Elisangela')
        self.assertContains(resp, 'espelho do CRM')
        # posto manual (sem id do legado) é editável aqui
        self.client.post('/painel/gerentes/', {
            'acao': 'salvar', 'pk': manual.pk,
            'gerente_nome': 'Fulano', 'gerente_email': 'fulano@camim.com.br'})
        manual.refresh_from_db()
        self.assertEqual(manual.gerente_email, 'fulano@camim.com.br')
        # posto do legado editado aqui vira FIXO: o espelho do CRM não mexe
        self.client.post('/painel/gerentes/', {
            'acao': 'salvar', 'pk': self.posto1.pk,
            'gerente_nome': 'Júlio Moreira',
            'gerente_email': 'julio@camim.com.br'})
        self.posto1.refresh_from_db()
        self.assertTrue(self.posto1.gerente_fixo)
        self.assertEqual(self.posto1.gerente_email, 'julio@camim.com.br')
        resp = self.client.get('/painel/gerentes/')
        self.assertContains(resp, 'fixado no painel')
        # o sync pula o fixo (mesmo com o CRM dizendo outra coisa)
        Posto.objects.filter(pk=self.posto1.pk).exclude(
            gerente_fixo=True).update(gerente_email='ninguem@x.com')
        self.posto1.refresh_from_db()
        self.assertEqual(self.posto1.gerente_email, 'julio@camim.com.br')
        # "voltar ao CRM" desfixa
        with mock.patch('django.core.management.call_command'):
            self.client.post('/painel/gerentes/',
                             {'acao': 'liberar', 'pk': self.posto1.pk})
        self.posto1.refresh_from_db()
        self.assertFalse(self.posto1.gerente_fixo)


class EmailsPainelTest(BaseSetup):
    def test_filtra_por_destinatario_e_abre_o_email(self):
        from core.models import EmailLog
        a = EmailLog.objects.create(
            destinatario='equipe@camim.com.br +cc: leticia@clinicacamim.com.br',
            assunto='Pagamento — X', corpo='Oi\n' + '-' * 40 + '\nValor: R$ 1,00',
            ok=True)
        EmailLog.objects.create(destinatario='elias@clinicacamim.com.br',
                                assunto='Recebido do Elias', corpo='x', ok=True)
        self.login_admin()
        resp = self.client.get('/painel/emails/?q=leticia')
        self.assertContains(resp, 'Pagamento — X')
        self.assertNotContains(resp, 'Recebido do Elias')
        self.assertContains(resp, 'leticia@clinicacamim.com.br')  # datalist
        # filtros combinados: para equipe@ + prestador X
        outro = Prestador.objects.create(nome='Outro PJ')
        b_x = Boleto.objects.create(prestador=self.prestador,
                                    posto=self.posto1,
                                    competencia=date(2026, 9, 1))
        b_o = Boleto.objects.create(prestador=outro, posto=self.posto2,
                                    competencia=date(2026, 8, 1))
        EmailLog.objects.create(destinatario='equipe@camim.com.br',
                                assunto='Pagamento — Limpeza', corpo='x',
                                ok=True, boleto=b_x)
        EmailLog.objects.create(destinatario='equipe@camim.com.br',
                                assunto='Pagamento — Outro', corpo='x',
                                ok=False, boleto=b_o)
        resp = self.client.get('/painel/emails/', {
            'para': 'equipe@camim.com.br', 'prestador': self.prestador.pk})
        self.assertContains(resp, 'Pagamento — Limpeza')
        self.assertNotContains(resp, 'Pagamento — Outro')
        self.assertNotContains(resp, 'Recebido do Elias')
        resp = self.client.get('/painel/emails/', {'tipo': 'pagamento',
                                                   'ok': 'nao'})
        self.assertContains(resp, 'Pagamento — Outro')
        self.assertNotContains(resp, 'Pagamento — Limpeza')
        resp = self.client.get('/painel/emails/', {'posto': self.posto2.pk,
                                                   'mes': '2026-08'})
        self.assertContains(resp, 'Pagamento — Outro')
        self.assertNotContains(resp, 'Pagamento — Limpeza')
        resp = self.client.get(f'/painel/emails/{a.pk}/')
        self.assertContains(resp, 'Pagamento — X')
        self.assertContains(resp, 'R$ 1,00')  # HTML renderizado
        self.assertContains(resp, 'Texto puro')


class FinanceiroRecebidoTest(BaseSetup):
    def _aprovado(self, **kw):
        base = dict(prestador=self.prestador, posto=self.posto1,
                    competencia=date(2026, 9, 1), arquivo=_pdf(),
                    status=Boleto.Status.APROVADO,
                    valor_extraido=Decimal('1500.00'))
        base.update(kw)
        return Boleto.objects.create(**base)

    def test_localiza_boleto_pelo_assunto_da_resposta(self):
        from core.services.boletos import localizar_boleto_por_assunto
        b = self._aprovado()
        assunto = ('Re: Pagamento — Limpeza Total LTDA — Anchieta — '
                   'setembro/2026 — R$ 1.500,00')
        self.assertEqual(localizar_boleto_por_assunto(assunto), b)
        # valor diferente não casa (extra × regular do mesmo posto)
        self.assertIsNone(localizar_boleto_por_assunto(
            'Re: Pagamento — Limpeza Total LTDA — Anchieta — '
            'setembro/2026 — R$ 85,00'))
        # assunto qualquer não casa
        self.assertIsNone(localizar_boleto_por_assunto('Re: bom dia'))

    def test_marcar_pago_a_partir_de_fin_recebido(self):
        b = self._aprovado(status=Boleto.Status.FIN_RECEBIDO)
        self.login_admin()
        self.client.post(f'/painel/boleto/{b.pk}/pagar/')
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.PAGO)

    def test_fin_recebido_conta_como_duplicidade(self):
        from core.services.boletos import duplicado_de
        self._aprovado(status=Boleto.Status.FIN_RECEBIDO)
        b2 = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf())
        self.assertIsNotNone(duplicado_de(b2))


class CcGerenteTest(BaseSetup):
    @mock.patch('core.services.emails.enviar', return_value=True)
    @mock.patch('core.services.ia.extrair_valor',
                return_value=(Decimal('1500.00'),
                              '{"valor":"1500.00","confianca":100}'))
    @mock.patch('core.services.pdf.extrair_texto', return_value='x')
    def test_gerente_do_posto_vai_em_cc(self, m_pdf, m_ia, m_mail):
        self.posto1.gerente_nome = 'Camilla Gaspar'
        self.posto1.gerente_email = 'gerenciar@camim.com.br'
        self.posto1.save()
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            enviado_por='pj@empresa.com.br',
            valor_esperado=Decimal('1500.00'))
        verificacao.processar(b.pk)
        ccs = [c.kwargs.get('cc') for c in m_mail.call_args_list]
        # pagador e aviso ao PJ, ambos com o gerente em cópia
        self.assertTrue(all(cc == ['gerenciar@camim.com.br'] for cc in ccs),
                        ccs)

    def test_email_real_leva_cc(self):
        from django.core import mail
        from core.services import emails as svc
        svc.enviar('a@x.com', 'Assunto', 'Corpo',
                   cc=['gerente@camim.com.br'])
        self.assertEqual(mail.outbox[0].cc, ['gerente@camim.com.br'])


class EnviarEmailTest(BaseSetup):
    """O serviço de envio real (sem mock do enviar): sucesso, lista de
    destinatários e — principalmente — falha de SMTP sem estourar."""

    def test_envia_para_lista(self):
        from django.core import mail
        from core.services import emails as svc
        ok = svc.enviar(['a@x.com', 'b@x.com'], 'Assunto', 'Corpo')
        self.assertTrue(ok)
        self.assertEqual(mail.outbox[0].to, ['a@x.com', 'b@x.com'])

    def test_envia_com_anexo_e_remetente_custom(self):
        from django.core import mail
        from core.services import emails as svc
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            nome_original='boleto.pdf')
        ok = svc.enviar('equipe@camim.com.br', 'Pagamento', 'Corpo',
                        boleto=b, anexo_field=b.arquivo,
                        de='Cristiano <cristiano@camim.com.br>')
        self.assertTrue(ok)
        self.assertEqual(mail.outbox[0].from_email,
                         'Cristiano <cristiano@camim.com.br>')
        self.assertEqual(len(mail.outbox[0].attachments), 1)

    @mock.patch('core.services.emails.EmailMultiAlternatives.send',
                side_effect=RuntimeError('smtp caiu'))
    def test_falha_de_envio_nao_estoura_e_registra(self, m_send):
        from core.models import EmailLog
        from core.services import emails as svc
        ok = svc.enviar(['a@x.com'], 'Assunto', 'Corpo')
        self.assertFalse(ok)
        registro = EmailLog.objects.latest('criado_em')
        self.assertIn('smtp caiu', registro.erro)


class PermissoesTest(BaseSetup):
    def test_pj_nao_baixa_boleto_de_outro(self):
        outro = Prestador.objects.create(nome='Outra Empresa')
        b = Boleto.objects.create(prestador=outro, posto=self.posto1,
                                  competencia=date(2026, 9, 1), arquivo=_pdf())
        self.login_pj()
        resp = self.client.get(f'/arquivo/boleto/{b.pk}/')
        self.assertEqual(resp.status_code, 404)

    def test_admin_baixa_tudo(self):
        b = Boleto.objects.create(prestador=self.prestador, posto=self.posto1,
                                  competencia=date(2026, 9, 1), arquivo=_pdf())
        self.login_admin()
        resp = self.client.get(f'/arquivo/boleto/{b.pk}/')
        self.assertEqual(resp.status_code, 200)

    def test_pj_nao_ve_painel(self):
        self.login_pj()
        resp = self.client.get('/painel/')
        self.assertEqual(resp.status_code, 302)

    def test_dashboard_mostra_faltantes(self):
        self.login_admin()
        resp = self.client.get('/painel/')
        self.assertContains(resp, 'SEM BOLETO')
        self.assertContains(resp, 'Limpeza Total')


class PainelAcaoTest(BaseSetup):
    @mock.patch('core.services.emails.enviar', return_value=True)
    def test_aprovar_manual_envia_pagador(self, m_mail):
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.MANUAL, valor_esperado=Decimal('1500.00'))
        self.login_admin()
        self.client.post(f'/painel/boleto/{b.pk}/aprovar/')
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        destinos = _destinos(m_mail)
        self.assertIn('equipe@camim.com.br', destinos)   # pagador
        self.assertIn('pj@empresa.com.br', destinos)     # aviso ao PJ

    def test_desfazer_pago(self):
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.PAGO)
        self.login_admin()
        self.client.post(f'/painel/boleto/{b.pk}/despagar/')
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.APROVADO)
        self.assertIsNone(b.pago_em)

    def test_marcar_pago(self):
        b = Boleto.objects.create(
            prestador=self.prestador, posto=self.posto1,
            competencia=date(2026, 9, 1), arquivo=_pdf(),
            status=Boleto.Status.APROVADO)
        self.login_admin()
        self.client.post(f'/painel/boleto/{b.pk}/pagar/')
        b.refresh_from_db()
        self.assertEqual(b.status, Boleto.Status.PAGO)
        self.assertIsNotNone(b.pago_em)
