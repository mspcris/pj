# Controle dos PJs — pj.camim.com.br

Portal onde os prestadores PJ anexam **boletos mensais** e **contratos**, com
verificação automática de valor por IA e controle rígido de pagamento no painel
do Cristiano — para nunca esquecer um boleto.

## Como funciona

**Lado do PJ (idiota de tão simples):** dois botões.
- **ANEXAR BOLETO** → escolhe o mês (atual pré-selecionado) → anexa o PDF.
  Se emite um boleto por posto, escolhe o posto (pré-selecionado se só tem um).
- **CONTRATOS** → escolhe o posto → lista de contratos com vigência + anexar.

**Como o boleto entra (3 caminhos, mesmo fluxo):**
- Upload do PJ na plataforma;
- E-mail do PJ para `pj@camim.com.br` (robô `importar_emails_pj`, leitura
  SOMENTE-READ da caixa, dedupe por Message-ID) — PDF anexo, ou linha
  digitável no corpo;
- Cadastro direto pelo admin no painel (boleto que chegou pelo zap), com
  linha digitável, chave PIX e PDF opcional.

**Fluxo de verificação (a IA extrai; quem decide é código):**
1. E-mail de "recebemos, verificando" (remetente `pj@camim.com.br`).
2. IA (`openai/gpt-oss-120b` via OpenRouter, provedor mais barato) extrai valor, vencimento e linha
   digitável do PDF. Sem PDF, o valor vem do próprio código de barras
   (determinístico: centavos embutidos na linha digitável).
3. **O código tem de bater com o valor**: linha digitável × PDF divergentes
   → **MANUAL**, nunca paga.
4. **Não duplicidade**: se já existe boleto APROVADO/PAGO da mesma
   competência/posto → **MANUAL**, nunca paga. (Reenvio só substitui
   pendências.)
5. **O mês tem de bater**: vencimento fora da janela da competência →
   **MANUAL**.
6. Valor × combinado: **igual ou MENOR** (pode haver acordo) → **APROVADO**
   → e-mail p/ `equipe@camim.com.br` saindo do `cristiano@` com anexo +
   linha digitável/PIX + e-mail ao PJ. Valor **MAIOR NUNCA passa sozinho**
   → **DIVERGENTE**, e-mail ao PJ pedindo para ligar. Valor maior só entra
   pelo cadastro do admin com "aceitar este valor" marcado.
7. As frases dos e-mails são redigidas pela IA (sempre variadas); se a IA
   cair, caem em modelos prontos sorteados — o fluxo nunca trava.

**Painel (`/painel/`):** régua mensal *quem deveria mandar boleto × o que
chegou*, com pendências no topo; marcar **PAGO**; aprovar manualmente;
reverificar; CRUD de prestadores, postos (13 canônicos já no seed), valores
combinados e usuários; log de e-mails e auditoria.

**Redes de segurança contra esquecer pagamento:**
- Dashboard ordena pendências primeiro + KPIs (sem boleto / precisa de você /
  enviados p/ pagamento / pagos).
- Cron diário 8h manda resumo de pendências por e-mail (`lembrete_diario`).
- Cron a cada 10 min reprocessa verificações travadas (`processar_boletos`).

## Segurança (leia antes de mexer)

- **Login**: idCamim (OIDC, ES256) + **whitelist** `UsuarioPermitido` — quem
  não está cadastrado e ativo NÃO entra, mesmo com senha certa no idCamim.
  `cristiano@camim.com.br` é superadmin protegido (nunca fica trancado fora).
- **Arquivos**: boletos/contratos NUNCA são servidos pelo nginx. Só saem pela
  view `/arquivo/...`, que checa se o arquivo é do prestador logado (admin vê
  tudo). O nginx dá 404 em `/media`.
- **Upload**: só PDF (assinatura `%PDF-` conferida), máx. 15 MB, nome trocado
  por UUID.
- **IA e prompt injection**: o texto do PDF entra SÓ no prompt de extração de
  valor (que devolve número). O prompt que redige e-mails recebe apenas fatos
  nossos (nomes, valores, mês) — texto de boleto jamais vira frase de e-mail.
  A decisão de pagar é 100% código (Decimal, tolerância de 1 centavo) — a IA
  não decide nada.
- **Auditoria**: logins (ok e negados), uploads, downloads (e negados) e
  mudanças de status em `/painel/auditoria/`; e-mails em `/painel/emails/`.
- **Segredos**: tudo no `.env` (chmod 600, gitignored). Nunca comitar.
- **`EMAIL_MODO_TESTE=true`** (padrão inicial): todo e-mail sai só para o
  Cristiano, com o destinatário real no assunto. Troque para `false` quando
  validar o fluxo.

## API para PJs desenvolvedores

Autenticação por token (`Authorization: Bearer <token>`), gerado pelo admin
em Usuários (botão 🔑, exibido uma única vez; gerar outro revoga o atual).

```bash
# Anexar o boleto do mês — UM ENVIO POR POSTO, com a nota fiscal junto
curl -X POST https://pj.camim.com.br/api/boletos/ \
  -H "Authorization: Bearer SEU_TOKEN" \
  -F "posto=A" \
  -F "arquivo=@boleto.pdf" \
  -F "nota_fiscal=@nf.pdf"
# → 201 {"id": 42, "competencia": "2026-10", "posto": "Anchieta",
#        "posto_letra": "A", "status": "RECEBIDO",
#        "valor_esperado": "1234.56", "tem_nota_fiscal": true,
#        "aguardando_nota_fiscal": false, ...}

# Mandar SÓ a nota fiscal de um boleto que já está no sistema (veio depois)
curl -X POST https://pj.camim.com.br/api/boletos/42/nota/ \
  -H "Authorization: Bearer SEU_TOKEN" \
  -F "nota_fiscal=@nf.pdf"

# Consultar os boletos do mês
curl "https://pj.camim.com.br/api/boletos/?competencia=2026-10" \
  -H "Authorization: Bearer SEU_TOKEN"
```

Campos do POST `/api/boletos/`:

- `arquivo` — PDF do boleto (obrigatório).
- `posto` — letra (A, B, G…) ou nome do posto. Quem atende vários postos
  manda um envio por posto; sem o campo, o CNPJ do sacado no PDF destina
  sozinho.
- `nota_fiscal` — PDF da NFS-e daquele posto. Pode ir junto ou depois, por
  `/api/boletos/<id>/nota/`. Para o prestador que EXIGE nota, o boleto sem
  ela entra mas fica retido (`"aguardando_nota_fiscal": true`): só vai para
  pagamento quando a nota chegar ou o admin liberar no painel.
- `competencia` — YYYY-MM do mês do **PAGAMENTO**, não do serviço (serviço
  de setembro pago em outubro é `2026-10`). Na dúvida, **omita**: vale o
  mês atual. Boleto de qualquer mês que NÃO seja o vigente nunca vai
  sozinho ao financeiro: fica retido até liberação manual no painel.
- `linha_digitavel` — opcional.

A resposta traz `id`, `competencia`, `posto` (nome), `posto_letra`,
`status`, `situacao`, `valor_esperado`, `valor_extraido`,
`tem_nota_fiscal` e `aguardando_nota_fiscal`. O boleto entra no MESMO fluxo
de verificação do portal.

## Rodar local

```bash
cd pj
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
.venv/bin/python manage.py test                # 18 testes
.venv/bin/python manage.py testar_integracoes  # IA (OpenRouter) + SMTP de verdade
```

## Deploy (VPS)

```bash
sudo rsync -a --exclude .venv --exclude db.sqlite3 ./ /opt/pj/
cd /opt/pj && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate && .venv/bin/python manage.py collectstatic --noinput
sudo cp deploy/pj.service /etc/systemd/system/ && sudo systemctl enable --now pj
# Deploys seguintes: rsync + migrate + `sudo systemctl reload pj` (suave, sem derrubar requisições)
sudo cp deploy/nginx-pj.conf /etc/nginx/sites-available/pj
sudo ln -s /etc/nginx/sites-available/pj /etc/nginx/sites-enabled/
sudo certbot --nginx -d pj.camim.com.br && sudo systemctl reload nginx
crontab -e   # colar deploy/crontab.txt
```

DNS: `pj.camim.com.br` → IP da VPS. O redirect_uri registrado no idCamim já é
`https://pj.camim.com.br/auth/callback`.
