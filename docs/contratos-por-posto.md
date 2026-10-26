# Dividir um contrato em um contrato (e um boleto) por posto

Procedimento usado para a Victória (23/10/2026, à mão), o Caio (26/10/2026,
à mão) e o Lincoln (26/10/2026, já pelo comando). Pedido do Cristiano:
"pega o valor do contrato, divide igualmente entre os postos, um contrato por
posto, um boleto por posto, desativa os antigos".

## O comando

No servidor, como `www-data`, em `/opt/pj`:

```bash
# 1) Só gerar os PDFs para conferir (NÃO mexe no banco)
sudo -u www-data .venv/bin/python manage.py contratos_por_posto \
    --prestador 1 --postos A,N,I,X,G,Y,B,R --modelo ti --total 3990 \
    --so-pdf /tmp/conferir

# 2) Valer
sudo -u www-data .venv/bin/python manage.py contratos_por_posto \
    --prestador 1 --postos A,N,I,X,G,Y,B,R --modelo ti --total 3990 \
    --inicio 2026-10-26 --desativar 43,44 \
    --ator "cristiano@camim.com.br (via Claude Code, shell do servidor)"
```

| opção | o que faz |
|---|---|
| `--prestador` | pk do prestador (ficha em `/painel/prestadores/<pk>/`) |
| `--postos` | letras na ordem; a sobra de centavos vai, um a um, para os primeiros (4.548,14 ÷ 8 = 568,52 ×6 + 568,51 ×2) |
| `--modelo` | `ti` (texto do contrato de TI do Caio) ou `consultoria_comercial` (Lincoln); ou o caminho de outro `.json` |
| `--total` / `--por-posto` | valor mensal total a dividir, ou o valor de cada posto. Sem os dois: usa o valor que já está em cada vínculo |
| `--inicio`, `--meses` | vigência (padrão hoje, 12 meses; o fim é véspera do aniversário) |
| `--dia` | dia do vencimento no texto (padrão `dia_pagamento` do prestador). O 1º vencimento é esse dia do mês SEGUINTE ao início (regime POSTERIOR) |
| `--desativar` | pks de contratos antigos — soft delete (ficam em cinza no painel, com Reativar) |
| `--manter-modo` | não trocar `modo_boleto` para POR_POSTO |
| `--por-posto-desde YYYY-MM` | mês a partir do qual a régua espera um boleto por posto (padrão: mês do 1º pagamento). Só vale quando o prestador ERA boleto único: os meses anteriores seguem como boleto único (`Prestador.por_posto_desde`) |
| `--manter-outros-vinculos` | não desativar vínculos de postos fora da lista |
| `--so-pdf PASTA` | só gera os PDFs; nada no banco |

O que ele grava, dentro de uma transação, com auditoria em cada passo
(`AuditLog`, ator = `--ator`):

1. `Prestador.modo_boleto` = POR_POSTO, `posto_cobranca` e `valor_unico` zerados. Se era ÚNICO, `por_posto_desde` recebe o mês do 1º pagamento — outubro pago num boleto só continua casando (Caio e Victória, 26/10/2026).
2. Vínculos dos postos da lista ativos com o valor; os outros desativados.
3. Um `Contrato` por posto, `assinatura_govbr=True`, vigência, nome
   `Contrato <Nome Sobrenome> - <Posto> - <AAAA-MM-DD>.pdf`.
4. Contratos de `--desativar` com `desativado_em/por`.

O que ele NÃO faz: não manda e-mail (o "📨 Avisar" na ficha é clique do
gestor — regra do projeto: nunca e-mail avulso) e não assina.

## Depois do comando (gestor)

1. Abrir a ficha do prestador, baixar um PDF e ler.
2. "📨 Avisar" → o prestador recebe um e-mail só, sem valores, e assina os
   PDFs no gov.br pelo portal (`/contratos/assinar-todos/`).
3. Assinar como gestor em lote (`/painel/assinaturas/?prestador=<pk>`).
4. Se o modo mudou: avisar o prestador que a partir do mês seguinte são N
   boletos, um por posto (o mês corrente já pago não muda).

## Os PDFs

`core/services/contratos_pdf.py` monta o PDF (reportlab, Helvetica, A4) a
partir de um modelo JSON em `core/modelos_contrato/`:

```json
{
  "titulo": "CONTRATO DE ...\nSEGUNDA LINHA",
  "contratante": "opcional — padrão: razão social + CNPJ + sede do posto, 'representada na forma de seu contrato social'",
  "contratada": "opcional — padrão: razão social + CNPJ + sede + e-mail + representante + CPF do prestador",
  "preambulo": ["parágrafos antes das cláusulas"],
  "clausulas": [{"titulo": "CLÁUSULA 1ª – DO OBJETO", "itens": ["1.1. ...", "1.2. ..."]}],
  "encerramento": "opcional",
  "cidade": "Rio de Janeiro"
}
```

Marcadores aceitos em qualquer texto: `{valor}` (R$ 498,75),
`{valor_extenso}`, `{total}`, `{total_extenso}`, `{inicio}`, `{fim}`,
`{primeiro_pagamento}`, `{dia_pagamento}`, `{meses}`, `{meses_extenso}`,
`{data_assinatura}`, `{posto}`, `{posto_razao_social}`, `{posto_cnpj}`,
`{posto_sede}`, `{razao_social}`, `{nome}`, `{cnpj}`, `{sede}`, `{email}`,
`{representante}`, `{cpf}`. Datas saem por extenso ("26 de outubro de 2026").

Para um prestador com texto próprio: extrair o texto do contrato vigente
(`pdftotext -layout`), copiar as cláusulas para um JSON novo trocando valor,
datas e vigência pelos marcadores, e rodar com `--so-pdf` para conferir.

## Dados dos postos

A qualificação do CONTRATANTE vem de `Posto.razao_social_contrato` (razão
social como na Receita, com LTDA) e `Posto.sede` — editáveis em
`/painel/postos/`. Os 8 postos do Altamiro (A, N, I, X, G, Y, B, R) foram
semeados na migração 0042 com os dados dos contratos da Victória; Realengo
com o CEP 21710-231 (o do contrato do Caio). Posto sem sede sai com
"(sede não cadastrada)" e o comando avisa.

Sempre o mesmo contratante por posto — não citamos o gerente, só "neste ato
representada na forma de seu contrato social".

## Histórico

- 23/10/2026 Victória (prestador 7): R$ 4.548,14 ÷ 8 → #76–#83 (à mão).
- 26/10/2026 Caio (1): R$ 3.990,00 ÷ 8 = 498,75 → #99–#106; #43/#44 desativados; Victória também passou a POR_POSTO.
- 26/10/2026 Lincoln (6): valor já estava por posto (370,88 × 8 = 2.967,04, R$ 300 a mais que o contrato de 32.004/ano desde out/26) → modelo `consultoria_comercial`; #25 já estava desativado.
