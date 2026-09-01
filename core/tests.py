"""Testes dos fluxos críticos: whitelist, upload, verificação e permissões."""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from .models import Boleto, Posto, Prestador, PrestadorPosto, UsuarioPermitido
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

    @mock.patch('core.services.emails.EmailMessage.send',
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
