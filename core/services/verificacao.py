"""Orquestração da verificação de boleto.

Fluxo: upload → e-mail "recebemos" → extrai texto do PDF → IA extrai o valor
→ compara com o valor acordado:
  * bate      → APROVADO: e-mail p/ pagador (equipe@) com o boleto anexo
                + e-mail p/ o PJ avisando que foi para pagamento
  * não bate  → DIVERGENTE: e-mail p/ o PJ pedindo para ligar
  * não leu   → MANUAL: e-mail p/ o Cristiano verificar no painel

A decisão de comparação é 100% nossa (Decimal, tolerância de 1 centavo).
A IA só extrai número e redige frase — nunca decide pagamento.
"""
import json
import logging
import re
import threading
from datetime import date, datetime
from decimal import Decimal

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from ..models import AuditLog, Boleto, Configuracao, Prestador
from . import boletos as svc_boletos
from . import emails, frases, ia, pdf

log = logging.getLogger(__name__)

TOLERANCIA = Decimal('0.01')
MAX_TENTATIVAS = 3

_MESES = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho', 'julho',
          'agosto', 'setembro', 'outubro', 'novembro', 'dezembro']


def competencia_extenso(d):
    return f'{_MESES[d.month - 1]}/{d.year}'


def _moeda(v):
    if v is None:
        return '—'
    s = f'{v:,.2f}'
    return s.replace(',', '_').replace('.', ',').replace('_', '.')


def _fatos(boleto):
    posto = boleto.posto_efetivo
    fatos = {
        'prestador': boleto.prestador.nome,
        'contato': boleto.prestador.contato,  # pessoa: "Prezado(a) Guido"
        'alvo': posto.nome if posto else 'boleto único',
        'competencia': competencia_extenso(boleto.competencia),
        'valor': _moeda(boleto.valor_extraido or boleto.valor_esperado),
    }
    if boleto.parcial:
        # Boleto PARCIAL: o "valor" é o DESTE boleto (nunca o combinado do
        # mês, senão o PJ lê "R$ 1.000,00" num boleto de R$ 499,93).
        sit = situacao_parcial(boleto)
        fatos['valor'] = _moeda(sit['este'] if sit else None)
        fatos['parcial_frase'] = frase_parcial(boleto, sit)
        if sit:
            fatos['pagamento_parcial'] = {
                'combinado_do_mes': _moeda(sit['total']),
                'ja_entregue_antes': _moeda(sit['antes']),
                'este_boleto': _moeda(sit['este']),
                'entregue_ate_agora': _moeda(sit['soma']),
                'ainda_falta': _moeda(sit['falta']),
                'situacao': ('MENSALIDADE COMPLETA' if sit['completa']
                             else f'FALTA R$ {_moeda(sit["falta"])}'),
            }
    return fatos


def situacao_parcial(boleto):
    """Conta da mensalidade paga em partes: combinado, o que já veio antes,
    este boleto, soma e o que falta. None se ainda não dá para contar."""
    if not boleto.parcial:
        return None
    total = boleto.valor_esperado
    if total is None:
        total = svc_boletos.valor_esperado_para(
            boleto.prestador, boleto.posto, boleto.competencia)
    este = boleto.valor_extraido or valor_da_linha(boleto.linha_digitavel)
    if total is None or este is None:
        return None
    anteriores = list(svc_boletos.parciais_anteriores(boleto))
    antes = sum((b.valor_extraido or Decimal('0') for b in anteriores),
                Decimal('0'))
    soma = antes + este
    falta = max(Decimal('0'), total - soma)
    return {'total': total, 'antes': antes, 'anteriores': anteriores,
            'este': este, 'soma': soma, 'falta': falta,
            'completa': falta <= TOLERANCIA}


def frase_parcial(boleto, sit=None):
    """UMA frase, em português claro, dizendo o que este boleto parcial
    representa e o que ainda falta — vai no texto do e-mail (IA e modelo
    pronto) para ninguém precisar deduzir pelos números."""
    if sit is None:
        sit = situacao_parcial(boleto)
    comp = competencia_extenso(boleto.competencia)
    if sit is None:
        return (f'Atenção: este boleto é apenas uma PARTE da mensalidade de '
                f'{comp}; o fechamento do valor será conferido.')
    if sit['completa']:
        return (f'Este boleto é a última parte da mensalidade de {comp}: '
                f'com ele o valor combinado de R$ {_moeda(sit["total"])} '
                f'fica completo — tudo certo.')
    if sit['antes'] > 0:
        return (f'Atenção: este boleto é uma PARTE da mensalidade de {comp} '
                f'(combinado: R$ {_moeda(sit["total"])}). Já havia '
                f'R$ {_moeda(sit["antes"])} entregue antes; com este, '
                f'R$ {_moeda(sit["soma"])}. Ainda falta gerar um boleto de '
                f'R$ {_moeda(sit["falta"])}.')
    return (f'Atenção: este boleto é uma PARTE da mensalidade de {comp} '
            f'(combinado: R$ {_moeda(sit["total"])}). Recebido '
            f'R$ {_moeda(sit["este"])}; ainda falta gerar um boleto de '
            f'R$ {_moeda(sit["falta"])}.')


def valor_da_linha(linha):
    """Valor embutido no código de barras — conferência 100% determinística.

    Linha digitável de boleto bancário (47 díg.): valor = últimos 10 dígitos,
    em centavos. Guia de arrecadação (48 díg.): remove o DV de cada bloco de
    12 e lê o valor nas posições 5-15 do código de barras resultante.
    """
    ld = re.sub(r'\D', '', linha or '')
    try:
        if len(ld) == 47:
            v = Decimal(ld[-10:]) / 100
        elif len(ld) == 48:
            cb = ''.join(d for i, d in enumerate(ld) if (i + 1) % 12 != 0)
            v = Decimal(cb[4:15]) / 100
        else:
            return None
        return v.quantize(Decimal('0.01')) if v > 0 else None
    except Exception:
        return None


def dados_pagamento(boleto, fatos):
    """Bloco determinístico com os dados de pagamento — anexado ao corpo do
    e-mail do pagador DEPOIS da redação (a IA nunca toca nesses dados)."""
    partes = ['', '-' * 40,
              f'Prestador: {fatos["prestador"]} — {fatos["alvo"]}',
              f'Competência: {fatos["competencia"]}',
              f'Valor: R$ {fatos["valor"]}']
    if boleto.vencimento:
        partes.append(f'Vencimento: {boleto.vencimento:%d/%m/%Y}')
    if boleto.linha_digitavel:
        partes.append(f'Linha digitável: {boleto.linha_digitavel}')
    if boleto.chave_pix:
        partes.append(f'Chave PIX: {boleto.chave_pix}')
    if boleto.extra:
        partes.append('Obs.: cobrança EXTRA/avulsa — não faz parte da '
                      'mensalidade do posto.')
    if boleto.parcial:
        partes.append('Obs.: boleto PARCIAL — é só uma parte da mensalidade '
                      'deste posto; o fechamento do mês está logo abaixo.')
    dif = diferenca_do_combinado(boleto)
    if dif is not None and boleto.valor_livre:
        sinal = '+' if dif > 0 else '−'
        partes.append(
            f'Obs.: valor DIFERENTE do combinado (R$ '
            f'{_moeda(boleto.valor_esperado)}): {sinal}R$ {_moeda(abs(dif))} '
            f'— aceito pelo admin no cadastro ("aceitar este valor").')
    if (not boleto.extra and not boleto.parcial
            and boleto.valor_esperado is not None
            and boleto.valor_extraido is not None
            and boleto.valor_esperado - boleto.valor_extraido > TOLERANCIA):
        if fatos.get('motivo_menor'):
            partes.append(f'Obs.: valor abaixo do combinado '
                          f'(R$ {_moeda(boleto.valor_esperado)}). '
                          f'Justificativa: {fatos["motivo_menor"]}')
        else:
            partes.append(f'Obs.: valor abaixo do combinado '
                          f'(R$ {_moeda(boleto.valor_esperado)}) — acordo '
                          'com o prestador.')
    for vale, n in svc_boletos.vales_aplicaveis(
            boleto.prestador, boleto.posto, boleto.competencia):
        partes.append(f'Desconto: {vale.descricao} — parcela '
                      f'{n}/{vale.parcelas_total} — '
                      f'R$ {_moeda(vale.valor_parcela)} (já abatido do '
                      'valor esperado)')
    if boleto.observacao:
        partes.append(f'Obs. do mês: {boleto.observacao}')
    partes.extend(resumo_parcial(boleto))
    return '\n'.join(partes)


def dados_pj(boleto, fatos):
    """Bloco formal do aviso ao prestador: quem paga, CNPJs, competência,
    valor e vencimento — mesmo padrão do e-mail do financeiro."""
    prest = boleto.prestador
    partes = ['', '-' * 40,
              f'Prestador: {prest.nome}'
              + (f' — CNPJ {prest.cnpj}' if prest.cnpj else '')]
    posto = boleto.posto_efetivo
    if posto is not None:
        pagador = posto.razao_social or posto.nome
        linha = f'Empresa pagadora: {pagador}'
        if posto.razao_social and posto.razao_social.upper() != \
                posto.nome.upper():
            linha += f' ({posto.nome})'
        if posto.cnpj:
            linha += f' — CNPJ {posto.cnpj}'
        partes.append(linha)
    partes.append(f'Competência: {fatos["competencia"]}')
    if fatos.get('valor') and fatos['valor'] != '—':
        partes.append(f'Valor: R$ {fatos["valor"]}')
    if boleto.vencimento:
        partes.append(f'Vencimento: {boleto.vencimento:%d/%m/%Y}')
    partes.extend(resumo_parcial(boleto))
    return '\n'.join(partes)


def resumo_parcial(boleto):
    """A "continha" da mensalidade paga em partes — bloco determinístico
    que fecha o e-mail (vira tabela no HTML). Lê-se de cima para baixo:
    combinado, o que já veio, este boleto, soma, o que falta, veredito."""
    if not boleto.parcial:
        return []
    posto = boleto.posto_efetivo
    onde = f'{posto.nome}, ' if posto else ''
    comp = competencia_extenso(boleto.competencia)
    cab = ['', f'🧩 Mensalidade em partes — {onde}{comp}:']
    sit = situacao_parcial(boleto)
    if sit is None:
        return cab + ['Este boleto é uma PARTE da mensalidade; o valor '
                      'ainda vai ser conferido para fechar a conta.']
    n = len(sit['anteriores'])
    antes = (f'R$ {_moeda(sit["antes"])} ({n} boleto{"s" if n > 1 else ""})'
             if n else 'R$ 0,00 (nenhum — este é o primeiro)')
    linhas = cab + [
        f'Valor combinado do mês: R$ {_moeda(sit["total"])}',
        f'Já entregue antes: {antes}',
        f'Este boleto: R$ {_moeda(sit["este"])}',
        f'Entregue até agora: R$ {_moeda(sit["soma"])}',
        f'Ainda falta: R$ {_moeda(sit["falta"])}',
    ]
    if sit['completa']:
        linhas.append(f'✅ Fechou! Com este boleto a mensalidade de {comp} '
                      'está completa. Nada mais a gerar.')
    else:
        linhas.append(f'⏳ Ainda NÃO fechou: falta gerar mais um boleto de '
                      f'R$ {_moeda(sit["falta"])} para completar '
                      f'R$ {_moeda(sit["total"])}.')
    return linhas


def diferenca_do_combinado(boleto):
    """valor − combinado, ou None quando não há discrepância (ou não dá
    para comparar: parcial/extra/sem valor)."""
    if boleto.parcial or boleto.extra:
        return None
    if boleto.valor_esperado is None or boleto.valor_extraido is None:
        return None
    dif = boleto.valor_extraido - boleto.valor_esperado
    return dif if abs(dif) > TOLERANCIA else None


def enviar_para_pagamento(boleto, fatos=None, reenviar=False):
    """ÚNICO caminho do e-mail "Pagamento — …" para a equipe@ (+ aviso ao
    PJ). TRAVA contra pagar duas vezes: se este boleto JÁ foi ao financeiro
    com o mesmo valor, não vai de novo (só com reenviar=True — o botão
    "Reenviar e-mails"). Valor diferente do já enviado → vai como CORREÇÃO,
    com aviso ao admin. Retorna a frase do que aconteceu."""
    fatos = fatos or _fatos(boleto)
    valor = boleto.valor_extraido or boleto.valor_esperado
    fatos['valor'] = _moeda(valor)
    ja = boleto.pagamento_enviado_em is not None
    mesmo = (ja and boleto.pagamento_enviado_valor is not None
             and valor is not None
             and abs(boleto.pagamento_enviado_valor - valor) <= TOLERANCIA)
    quando = (timezone.localtime(boleto.pagamento_enviado_em)
              .strftime('%d/%m/%Y %H:%M') if ja else '')
    if ja and mesmo and not reenviar:
        AuditLog.registrar(
            AuditLog.Evento.STATUS, ator='sistema',
            detalhe=f'Boleto #{boleto.pk} reaprovado, mas JÁ estava com o '
                    f'financeiro desde {quando} (R$ {fatos["valor"]}) — '
                    'nada reenviado (trava contra pagar 2x)')
        return (f'já estava com o financeiro desde {quando} com o mesmo '
                'valor — NADA foi reenviado, para não pagar duas vezes. '
                'Se precisar mesmo, use "Reenviar e-mails".')
    correcao = ja and not mesmo

    assunto = assunto_parcial(
        fatos,
        f'Pagamento — {fatos["prestador"]} — {fatos["alvo"]} — '
        f'{fatos["competencia"]} — R$ {fatos["valor"]}')
    aviso = ''
    if correcao:
        assunto = f'CORREÇÃO — {assunto}'
        aviso = (f'⚠️ ATENÇÃO: este e-mail SUBSTITUI o enviado em {quando} '
                 f'com R$ {_moeda(boleto.pagamento_enviado_valor)} para este '
                 'mesmo boleto. Pagar SOMENTE este valor, uma vez só.\n\n')
    elif reenviar and ja:
        aviso = (f'⚠️ REENVIO do e-mail de {quando} — é o MESMO boleto, '
                 'pagar UMA vez só.\n\n')
    emails.enviar(
        settings.EMAIL_PAGADOR, assunto,
        aviso + frases.corpo(
            'aprovado_pagador', fatos,
            instrucao_ia=('Escreva para a equipe de pagamento pedindo '
                          'para pagar o boleto em anexo, informando que '
                          'o valor já foi conferido e que os dados de '
                          'pagamento seguem abaixo da assinatura.'
                          + _instrucao_parcial(fatos)))
        + dados_pagamento(boleto, fatos),
        boleto=boleto,
        anexo_field=boleto.arquivo if boleto.arquivo else None,
        anexos=([(boleto.nota_fiscal,
                  boleto.nota_fiscal_nome or 'nota-fiscal.pdf')]
                if boleto.nota_fiscal else None),
        de=settings.EMAIL_FROM_PAGADOR, cc=cc_gerente(boleto))
    boleto.pagamento_enviado_em = timezone.now()
    boleto.pagamento_enviado_valor = valor
    boleto.save(update_fields=['pagamento_enviado_em',
                               'pagamento_enviado_valor'])

    if boleto.parcial:
        instr_pj = ('Escreva em tom FORMAL para o prestador, informando '
                    'que o boleto foi conferido e já foi encaminhado ao '
                    'setor financeiro para pagamento. Diga que os dados '
                    'da cobrança seguem abaixo da assinatura.'
                    + _instrucao_parcial(fatos))
    else:
        instr_pj = ('Escreva em tom FORMAL para o prestador, '
                    'informando que o boleto foi conferido, o '
                    'valor está de acordo com o contratado e o '
                    'documento já foi encaminhado ao setor '
                    'financeiro para pagamento. Diga que os dados '
                    'da cobrança seguem abaixo da assinatura.')
    if not (reenviar and ja) and not correcao:
        emails.enviar(
            destinatarios_pj(boleto),
            assunto_parcial(
                fatos,
                f'Boleto aprovado e enviado p/ pagamento — '
                f'{fatos["competencia"]}'),
            frases.corpo('aprovado_pj', fatos, instrucao_ia=instr_pj)
            + dados_pj(boleto, fatos),
            boleto=boleto, cc=cc_gerente(boleto))

    _alertar_discrepancia(boleto, fatos, correcao, quando)
    if correcao:
        return (f'CORREÇÃO enviada ao financeiro (o valor mudou: antes '
                f'R$ {_moeda(boleto.pagamento_enviado_valor)}, agora '
                f'R$ {fatos["valor"]}).')
    if reenviar and ja:
        return 'e-mail de pagamento REENVIADO à equipe (marcado como reenvio).'
    return 'enviado p/ pagamento (equipe e prestador avisados).'


def _alertar_discrepancia(boleto, fatos, correcao=False, quando=''):
    """Aviso ao admin sempre que o dinheiro sai diferente do combinado:
    valor MAIOR aceito à mão, ou CORREÇÃO de um envio anterior."""
    motivos = []
    dif = diferenca_do_combinado(boleto)
    if dif is not None and dif > 0:
        motivos.append(f'o valor pago (R$ {fatos["valor"]}) é MAIOR que o '
                       f'combinado (R$ {_moeda(boleto.valor_esperado)}): '
                       f'+R$ {_moeda(dif)}')
    if correcao:
        motivos.append(f'este boleto já tinha ido ao financeiro em {quando} '
                       'com outro valor — foi enviada uma CORREÇÃO; confira '
                       'com a equipe@ que só um pagamento será feito')
    if not motivos:
        return
    emails.enviar(
        settings.EMAIL_ADMIN,
        f'⚠️ Discrepância no pagamento — {fatos["prestador"]} — '
        f'{fatos["alvo"]} — {fatos["competencia"]}',
        'Cristiano,\n\nO boleto abaixo foi para pagamento, mas atenção:\n'
        + '\n'.join(f'• {m}' for m in motivos)
        + '\n\nPainel: https://pj.camim.com.br/painel/\n\n— Controle dos PJs'
        + dados_pagamento(boleto, fatos),
        boleto=boleto)


def _mes_seguinte_fim(competencia):
    """Último dia do mês seguinte ao da competência."""
    m = competencia.month + 2
    ano = competencia.year + (m - 1) // 12
    mes = (m - 1) % 12 + 1
    return date(ano, mes, 1)  # exclusivo: vencimento < este dia


def cc_gerente(boleto):
    """CC obrigatório do gerente do posto (espelho do CRM) em todo e-mail
    de boleto que tem posto identificado."""
    posto = boleto.posto_efetivo
    if posto is not None and posto.gerente_email:
        return [posto.gerente_email]
    return None


def destinatarios_pj(boleto):
    """Para quem vão os avisos do prestador: TODOS os usuários ativos do PJ
    + os "e-mails para avisos" do cadastro (quem não tem login, ex.:
    Rosana). Quem enviou também entra, se for outro endereço."""
    ems = [u.email for u in boleto.prestador.usuarios.filter(ativo=True)]
    for e in boleto.prestador.lista_emails_aviso():  # avisos sem login
        if e not in ems:
            ems.append(e)
    extra = (boleto.enviado_por or '').lower()
    if extra and extra != settings.EMAIL_ADMIN.lower() and extra not in ems:
        ems.append(extra)
    return ems or [settings.EMAIL_ADMIN]


def _instrucao_parcial(fatos):
    """Complemento da instrução da IA quando o boleto é PARCIAL: o texto
    tem de dizer, com todas as letras, o que falta (ou que fechou)."""
    if not fatos.get('parcial_frase'):
        return ''
    return (' IMPORTANTE: este boleto é PARCIAL — cobre só uma PARTE da '
            'mensalidade (veja "pagamento_parcial" nos fatos). Inclua, '
            'em uma frase própria e bem clara, exatamente esta informação: '
            f'"{fatos["parcial_frase"]}" Pode reescrever com suas palavras, '
            'mas mantenha TODOS os valores iguais. NUNCA diga que o valor '
            '"está de acordo com o contrato" — o valor do contrato é o do '
            'mês inteiro.')


def assunto_parcial(fatos, base):
    """Assunto que já conta a história: '(parte: R$ x de R$ y — falta R$ z)'."""
    sit = fatos.get('pagamento_parcial')
    if not sit:
        return base
    if sit['situacao'] == 'MENSALIDADE COMPLETA':
        fim = 'mensalidade completa ✅'
    else:
        fim = f'falta R$ {sit["ainda_falta"]}'
    return (f'{base} · PARCIAL: R$ {sit["este_boleto"]} de '
            f'R$ {sit["combinado_do_mes"]} — {fim}')


def enviar_recebido(boleto):
    fatos = _fatos(boleto)
    corpo = frases.corpo(
        'recebido', fatos,
        instrucao_ia=('Escreva em tom FORMAL confirmando que recebemos o '
                      'boleto do prestador e que ele passará por '
                      'conferência. Diga que os dados do documento seguem '
                      'abaixo da assinatura. NÃO cite valores no texto.'
                      + (_instrucao_parcial(fatos).replace(
                          'NÃO cite', 'cite') if fatos.get('parcial_frase')
                         else '')))
    emails.enviar(destinatarios_pj(boleto),
                  assunto_parcial(fatos,
                                  f'Boleto recebido — {fatos["competencia"]}'),
                  corpo + dados_pj(boleto, fatos), boleto=boleto,
                  cc=cc_gerente(boleto))


def _marcar(boleto, status):
    boleto.status = status
    boleto.verificado_em = timezone.now()
    boleto.save()
    AuditLog.registrar(AuditLog.Evento.STATUS, ator='sistema',
                       detalhe=f'Boleto #{boleto.pk} → {status}')


def _para_manual(boleto, motivo):
    boleto.ia_resposta = (boleto.ia_resposta + f'\n[manual] {motivo}').strip()
    _marcar(boleto, Boleto.Status.MANUAL)
    fatos = _fatos(boleto)
    emails.enviar(settings.EMAIL_ADMIN,
                  f'⚠️ Verificar boleto manualmente — {fatos["prestador"]} — '
                  f'{fatos["competencia"]}',
                  frases.corpo('manual_admin', fatos) + f'\n\nMotivo: {motivo}',
                  boleto=boleto)


def processar(boleto_pk):
    """Verifica um boleto RECEBIDO. Seguro para rodar por thread E por cron:
    a claim atômica em `tentativas` garante que só um processa."""
    try:
        boleto = (Boleto.objects.select_related('prestador', 'posto',
                                                'prestador__posto_cobranca')
                  .get(pk=boleto_pk))
    except Boleto.DoesNotExist:
        return

    if boleto.status != Boleto.Status.RECEBIDO:
        return
    claimed = Boleto.objects.filter(
        pk=boleto_pk, status=Boleto.Status.RECEBIDO,
        tentativas=boleto.tentativas,
    ).update(tentativas=boleto.tentativas + 1)
    if not claimed:
        return
    boleto.tentativas += 1

    fatos = _fatos(boleto)

    # 0) NOTA FISCAL: se o prestador exige, boleto sem NF não passa; e a
    # NF anexa (PDF) precisa ser NFS-e emitida pelo CNPJ do prestador.
    if boleto.prestador.exige_nf and not boleto.nota_fiscal:
        _para_manual(boleto, 'o prestador exige nota fiscal anexa e o '
                             'boleto veio sem NF')
        return
    if (boleto.nota_fiscal
            and boleto.nota_fiscal.name.lower().endswith('.pdf')):
        ok_nf, motivo_nf = svc_boletos.validar_nf(
            pdf.extrair_texto(boleto.nota_fiscal.path), boleto.prestador)
        if not ok_nf:
            _para_manual(boleto, motivo_nf)
            return

    # 1) Valor do PDF (via IA) — quando há PDF. Imagem/foto: a IA não lê,
    # mas com linha digitável a conferência sai pelo código de barras e a
    # imagem segue de anexo para o financeiro.
    valor_pdf = None
    confianca_pdf = None
    eh_pdf = bool(boleto.arquivo
                  and boleto.arquivo.name.lower().endswith('.pdf'))
    if boleto.arquivo and not eh_pdf and not boleto.linha_digitavel:
        _para_manual(boleto, 'arquivo é imagem (IA não lê) e sem linha '
                             'digitável — nada para conferir')
        return
    if eh_pdf:
        texto = pdf.extrair_texto(boleto.arquivo.path)
        if not texto:
            _para_manual(boleto, 'PDF sem texto legível (escaneado?)')
            return
        # FAVORECIDO: se o prestador tem CNPJ cadastrado, ele precisa
        # constar no boleto — proteção contra pagar boleto de terceiros.
        cnpj_prest = re.sub(r'\D', '', boleto.prestador.cnpj or '')
        if cnpj_prest and cnpj_prest not in re.sub(r'\D', '', texto):
            _para_manual(boleto,
                         f'o CNPJ do prestador ({boleto.prestador.cnpj}) '
                         'não aparece no boleto — confira o FAVORECIDO '
                         'antes de liberar')
            return

        # Destino do boleto: dica pelo CNPJ do posto (sacado) impresso no
        # PDF. NÃO é rigoroso — muitos PJs emitem contra si mesmos; se não
        # achar, fica aguardando destinação manual no painel.
        if (boleto.posto_id is None
                and boleto.prestador.modo_boleto ==
                Prestador.ModoBoleto.POR_POSTO):
            posto = svc_boletos.identificar_posto(texto)
            if posto is None:
                vinculos = list(boleto.prestador.vinculos_ativos())
                if len(vinculos) == 1:  # só atende um posto: é ele
                    posto = vinculos[0].posto
            if posto is not None:
                boleto.posto = posto
                boleto.valor_esperado = svc_boletos.valor_esperado_para(
                    boleto.prestador, posto, boleto.competencia)
                fatos = _fatos(boleto)
                AuditLog.registrar(
                    AuditLog.Evento.STATUS, ator='sistema',
                    detalhe=f'Boleto #{boleto.pk} destinado a {posto} '
                            'pelo CNPJ do sacado')
        try:
            valor_pdf, bruto = ia.extrair_valor(texto)
            boleto.ia_resposta = bruto[:4000]
        except Exception as e:
            log.error('IA falhou no boleto #%s: %s', boleto_pk, e)
            if boleto.tentativas >= MAX_TENTATIVAS:
                _para_manual(boleto, f'IA indisponível após '
                                     f'{MAX_TENTATIVAS} tentativas: {e}')
            else:
                boleto.save()  # continua RECEBIDO; o cron tenta de novo
            return
        if valor_pdf is None:
            _para_manual(boleto, 'IA não identificou o valor no PDF')
            return
        try:
            dados = json.loads(bruto)
        except Exception:
            dados = {}
        if not boleto.linha_digitavel:
            ld = re.sub(r'\D', '', str(dados.get('linha_digitavel') or ''))
            if 40 <= len(ld) <= 48:
                boleto.linha_digitavel = ld
        if boleto.vencimento is None:
            try:
                boleto.vencimento = datetime.strptime(
                    str(dados.get('vencimento') or ''), '%d/%m/%Y').date()
            except ValueError:
                pass
        try:
            confianca_pdf = max(0, min(100, int(dados.get('confianca'))))
        except (TypeError, ValueError):
            confianca_pdf = 50  # IA não declarou — trata como incerto

    # 2) Valor embutido no código de barras (determinístico, sem IA).
    valor_linha = valor_da_linha(boleto.linha_digitavel)

    if valor_pdf is None and valor_linha is None:
        _para_manual(boleto, 'sem PDF legível e sem linha digitável com '
                             'valor — nada para conferir')
        return

    # 3) O CÓDIGO TEM DE BATER COM O VALOR: PDF × código de barras.
    if (valor_pdf is not None and valor_linha is not None
            and abs(valor_pdf - valor_linha) > TOLERANCIA):
        _para_manual(boleto,
                     f'código de barras diz R$ {_moeda(valor_linha)}, mas o '
                     f'PDF diz R$ {_moeda(valor_pdf)} — documento '
                     'inconsistente, NÃO enviado para pagamento')
        return

    valor = valor_pdf if valor_pdf is not None else valor_linha
    boleto.valor_extraido = valor
    fatos['valor'] = _moeda(valor)

    # Confiança final: 100 quando o valor é determinístico (código de
    # barras) ou dupla checagem (PDF × linha batendo); senão, a que a
    # própria IA declarou na extração do PDF.
    if valor_pdf is None or valor_linha is not None:
        boleto.ia_confianca = 100
    else:
        boleto.ia_confianca = confianca_pdf if confianca_pdf is not None else 50

    # 4) NÃO DUPLICIDADE: nunca aprovar duas vezes a mesma competência.
    # Vira DUPLICADO e fica só marcado no painel — sem e-mail, sem drama
    # (o normal é o PJ mandar por e-mail algo que o admin já cadastrou).
    dup = svc_boletos.duplicado_de(boleto)
    if dup is not None:
        boleto.ia_resposta = (boleto.ia_resposta +
                              f'\n[duplicado] o boleto #{dup.pk} desta '
                              f'competência já está '
                              f'"{dup.get_status_display()}"').strip()
        _marcar(boleto, Boleto.Status.DUPLICADO)
        return

    # 5) O MÊS TEM DE BATER: vencimento dentro da janela da competência
    # (do dia 1 da competência até o fim do mês seguinte).
    if (boleto.vencimento is not None
            and not (boleto.competencia <= boleto.vencimento
                     < _mes_seguinte_fim(boleto.competencia))):
        _para_manual(boleto,
                     f'vencimento {boleto.vencimento:%d/%m/%Y} não bate com '
                     f'a competência {fatos["competencia"]}')
        return

    # PARCIAL: N boletos compõem a mensalidade — não compara um a um; a
    # SOMA das parciais aprovadas não pode passar do combinado do posto.
    if boleto.parcial:
        combinado = svc_boletos.valor_esperado_para(
            boleto.prestador, boleto.posto, boleto.competencia)
        boleto.valor_esperado = combinado
        if combinado is None:
            _para_manual(boleto, 'boleto parcial sem valor combinado '
                                 'cadastrado no posto')
            return
        soma = svc_boletos.soma_parciais(boleto) + valor
        if soma - combinado > TOLERANCIA:
            _para_manual(boleto,
                         f'as parciais somariam R$ {_moeda(soma)} e passam '
                         f'do combinado (R$ {_moeda(combinado)}) — nada '
                         'enviado')
            return
        fatos = _fatos(boleto)  # refaz a continha com o valor conferido

    # Valores podem ter sido cadastrados DEPOIS do boleto chegar — na
    # reverificação, recalcula o esperado em vez de reclamar à toa.
    # Cobrança EXTRA não tem combinado: nunca herda o valor do posto.
    if boleto.valor_esperado is None and not boleto.extra:
        boleto.valor_esperado = svc_boletos.valor_esperado_para(
            boleto.prestador, boleto.posto, boleto.competencia)

    if boleto.valor_esperado is None and not boleto.valor_livre:
        if boleto.extra:
            _para_manual(boleto, 'cobrança EXTRA sem valor de referência — '
                                 'confira e libere com "Aprovar assim '
                                 'mesmo" (ou edite marcando "aceitar este '
                                 'valor")')
        else:
            _para_manual(boleto, 'sem valor acordado cadastrado no painel')
        return

    # 6) Valor × combinado. Igual → aprova. MENOR: se houver observações
    # (do mês ou do cadastro), a IA confere se elas EXPLICAM a diferença
    # (ex.: "descontada parcela 3/7 do notebook — R$ 600"); obs que não
    # explica → MANUAL. Sem obs nenhuma, vale a regra do acordo: aprova.
    # MAIOR: NUNCA aprova sozinho — só o admin com "aceitar este valor".
    menor = (not boleto.valor_livre and not boleto.parcial
             and boleto.valor_esperado is not None
             and (boleto.valor_esperado - valor) > TOLERANCIA)
    if menor:
        obs = ' | '.join(t.strip() for t in
                         [boleto.observacao, boleto.prestador.observacao]
                         if t and t.strip())
        if obs:
            try:
                explica, motivo = ia.avaliar_diferenca(
                    valor, boleto.valor_esperado, obs)
            except Exception as e:
                log.warning('IA de diferença falhou (%s); '
                            'seguindo regra do acordo', e)
                explica, motivo = True, ''
            if not explica:
                _para_manual(boleto,
                             f'valor abaixo do combinado (R$ {_moeda(valor)} '
                             f'× R$ {_moeda(boleto.valor_esperado)}) e as '
                             'observações registradas NÃO explicam a '
                             'diferença')
                return
            if motivo:
                fatos['motivo_menor'] = motivo
                boleto.ia_resposta = (boleto.ia_resposta +
                                      f'\n[menor] {motivo}').strip()

    # 7) GATE DE CONVICÇÃO: só envia para pagamento sozinho se a confiança
    # for >= limiar (Configurações; padrão 99%). Abaixo disso, espera o
    # Cristiano liberar no painel ("Aprovar assim mesmo").
    aprovaria = (boleto.valor_livre or boleto.parcial
                 or (valor - boleto.valor_esperado) <= TOLERANCIA)
    limiar = Configuracao.get_int('limiar_confianca', 99)
    if (aprovaria and not boleto.valor_livre
            and (boleto.ia_confianca or 0) < limiar):
        _para_manual(boleto,
                     f'valor confere, mas a confiança da IA foi '
                     f'{boleto.ia_confianca}% (limiar: {limiar}%) — nada '
                     'enviado; libere o envio no painel se estiver ok')
        return

    if aprovaria:
        _marcar(boleto, Boleto.Status.APROVADO)
        enviar_para_pagamento(boleto, fatos)
    else:
        fatos['valor_esperado'] = _moeda(boleto.valor_esperado)
        _marcar(boleto, Boleto.Status.DIVERGENTE)
        emails.enviar(
            destinatarios_pj(boleto),
            f'Boleto {fatos["competencia"]} — valor a confirmar',
            frases.corpo(
                'divergente', fatos,
                instrucao_ia=('Escreva para o prestador dizendo que o valor '
                              'combinado não consta no boleto enviado e peça '
                              'para ligar para o Cristiano para entenderem a '
                              'diferença. NÃO cite números.')),
            boleto=boleto, cc=cc_gerente(boleto))


def processar_async(boleto_pk):
    """Só a verificação, sem reenviar o e-mail de 'recebemos' — para
    reverificação após edição no painel."""
    def _run():
        close_old_connections()
        try:
            processar(boleto_pk)
        except Exception:
            log.exception('reverificação do boleto #%s falhou', boleto_pk)
        finally:
            close_old_connections()
    threading.Thread(target=_run, daemon=True).start()


def fluxo_completo_async(boleto_pk):
    """Dispara em thread: e-mail de recebimento + verificação. O cron
    processar_boletos é a rede de segurança se a thread morrer."""
    def _run():
        close_old_connections()
        try:
            b = Boleto.objects.get(pk=boleto_pk)
            enviar_recebido(b)
            processar(boleto_pk)
        except Exception:
            log.exception('fluxo do boleto #%s falhou (cron retenta)',
                          boleto_pk)
        finally:
            close_old_connections()
    threading.Thread(target=_run, daemon=True).start()
