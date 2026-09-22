# take-rates-watcher

Avisa no Telegram quando **Shopee**, **Mercado Livre** ou **Amazon** mexem nas
tarifas cobradas de vendedores.

Roda sozinho no GitHub Actions, 9 vezes por dia. Não precisa de servidor,
banco de dados nem máquina ligada.

Páginas monitoradas (configuráveis em [`sources.yaml`](sources.yaml)):

| Marketplace | Página |
|---|---|
| Shopee | [Comissão para vendedores CNPJ e CPF](https://seller.br.shopee.cn/edu/article/26839/Comissao-para-vendedores-CNPJ-e-CPF-em-2026) |
| Mercado Livre | [Custos de vender (ajuda 40538)](https://www.mercadolivre.com.br/ajuda/40538) |
| Amazon | [Logística da Amazon (FBA)](https://venda.amazon.com.br/cresca/fba) |
| Amazon | [Preços e planos de venda](https://venda.amazon.com.br/precos) |

---

## Como funciona

A cada rodada, para cada página:

1. **Abre num Chromium headless** (Playwright). Precisa ser navegador de
   verdade: as três páginas montam a tabela de tarifas via JavaScript, então
   baixar o HTML cru com `requests` não traria número nenhum.
2. **Joga fora a moldura do site** — menu, rodapé, scripts — e fica só com as
   linhas que falam de dinheiro: as que contêm `%`, `R$` ou uma das palavras
   de `keywords` (comissão, tarifa, frete, plano…). É esse filtro que separa a
   tabela de tarifas do carrossel de promoção.
3. **Compara com a última leitura** (`state/last_seen.json`, versionado no
   próprio repositório) em duas camadas:

   | O que mudou | Alerta | Exemplo |
   |---|---|---|
   | Um número (`%` ou `R$`) | 🚨 **Tarifa mudou** | comissão de 14% → 15% |
   | Só o texto, nenhum número | 📝 Mudança de regra | "por item" → "por pedido" |
   | A própria URL da página | 📅 Edição nova | artigo de 2026 → 2027 |
   | Nada | *silêncio* | |

4. **Manda um Telegram** com o antes/depois e o link, e commita o estado novo.

Números equivalentes não viram alerta: `14%`, `14,0 %` e `14.00%` são lidos
como o mesmo valor, e o mesmo vale para `R$ 1.000,00` vs `R$ 1000`. Banner
novo, acento corrigido e `?utm_source=` na URL também passam batido — o
monitor só acorda quando muda o que importa.

### A virada de ano da Shopee

A URL da Shopee tem o ano embutido:

```
.../edu/article/26839/Comissao-para-vendedores-CNPJ-e-CPF-em-2026
                ^^^^^                                         ^^^^
```

O detalhe é que **o id do artigo (`26839`) também muda** quando eles publicam
a tabela do ano seguinte — trocar só `2026` por `2027` não acha nada. Por
isso a busca tem três camadas, nesta ordem (`sources.yaml`, chave
`year_in_url`):

1. **Templates de URL** — tenta `.../26839/...-em-2027`. Barato, e funciona no
   caso (improvável) de eles manterem o id.
2. **Links da própria página** — varre os `<a>` da página atual atrás de um
   link cujo texto ou URL case com `comiss|tarifa|taxa` **e** tenha um ano
   maior. É o mecanismo que costuma acertar: quando a Shopee publica o artigo
   novo, o antigo passa a linkar para ele. Se você descobrir a URL do índice
   de categoria do Seller Education Hub, coloque em `link_discovery.extra_pages`
   — aí a detecção fica independente de a página velha linkar a nova.
3. **Última URL que funcionou** — guardada no estado. Se nada novo aparecer,
   continua monitorando a de sempre.

Qualquer candidato só é aceito se a página carregar de verdade (`HTTP < 400`,
texto acima de `min_chars` e casando com `validate_regex`) — assim um 404
estilizado ou uma home genérica não vira "a nova fonte de tarifas".

A busca pelo ano seguinte começa em **setembro** (`year_lookahead_from_month`)
e passa a rodar o ano todo se o calendário virar sem a página ter mudado —
ficar em janeiro de 2027 lendo a tabela de 2026 é justamente o que não pode
acontecer.

### Quando o próprio monitor quebra

Se uma página parar de carregar, as falhas ficam só no log por um tempo
(quase sempre é rede ou manutenção do site). Quando ela está **falhando há 12
horas** chega um ⚠️ avisando que o monitor está cego naquela fonte — porque
uma página que some também é notícia. Não repete o alerta enquanto o problema
durar, e manda um ✅ quando voltar. A última leitura boa é preservada durante
a queda, então a comparação continua de onde parou.

O critério é tempo, e não número de falhas, porque o número depende de quantas
vezes o job roda — com 9 rodadas por dia, "3 falhas seguidas" seriam 4 horas.
O prazo fica em `FAILURE_ALERT_HOURS`, no topo de `check_tarifas.py`.

Uma fonte fora do ar não derruba as outras: o job só falha se *todas* falharem.

---

## Colocando no ar

### 1. Criar o bot do Telegram

1. Fale com [@BotFather](https://t.me/BotFather) → `/newbot` → guarde o
   **token**.
2. Mande qualquer mensagem para o seu bot e abra
   `https://api.telegram.org/bot<SEU_TOKEN>/getUpdates` para ver o
   **chat_id** (ou use [@userinfobot](https://t.me/userinfobot)).

### 2. Configurar os secrets

No repositório: **Settings → Secrets and variables → Actions → New repository
secret**:

| Secret | Valor |
|---|---|
| `TELEGRAM_BOT_TOKEN` | o token do BotFather |
| `TELEGRAM_CHAT_ID` | o chat que recebe os alertas |

Sem esses dois o script continua rodando e gravando o estado; só imprime o
alerta no log em vez de mandar mensagem.

### 3. Primeira rodada: a baseline

**Actions → Verifica take rates dos marketplaces → Run workflow**, modo `normal`.

A primeira rodada não compara nada — ela registra o estado inicial e manda no
Telegram a lista de valores que encontrou em cada página. **Confira essa
lista.** É o momento de saber se o parser está pegando a tabela certa: se os
percentuais e valores baterem com o que você vê no site, o monitor está
calibrado. Se vier vazio ou com números que não têm nada a ver, vá para
[Calibrando](#calibrando).

Depois disso ele roda sozinho e só fala quando alguma coisa muda — rodar 9
vezes não gera 9 alertas: uma mudança é notificada uma vez, na primeira rodada
que a vê.

---

## Quando ele roda

O GitHub Actions não garante horário: o agendamento é *best-effort* e atrasa.
No repositório `clipping` foram medidos **34 a 110 min** de atraso em dia
normal e **5 a 12 horas** em dia ruim — e, no dia ruim, todos os horários
atrasam juntos. Por isso cada meta tem mais de um horário, e o primeiro de
cada uma dispara com horas de folga:

| Meta | Horários (BRT) | Folga do primeiro |
|---|---|---|
| Ter rodado **antes das 09:00** | 02:13 · 05:13 · 06:43 · 07:43 | ~6,5h de atraso |
| Durante o dia | 11:23 · 14:23 · 17:23 | — |
| Ter rodado **antes do fim do dia** | 19:53 · 21:53 | ~4h de atraso |

Num dia normal as rodadas chegam de 30 min a 2h depois do horário nominal. Num
dia muito ruim (atraso > 6,5h) nem a rodada das 02:13 chega antes das 09:00 —
nenhuma grade de cron resolve isso; só um disparo externo (ver abaixo).

O custo é de uns 3 minutos de Actions por rodada, ~800 min/mês. O plano Free
dá 2.000 min/mês para repositórios privados, **somados entre todos os seus
repos privados** — vale conferir em *Settings → Billing* se os outros watchers
não estão perto do limite.

Para mudar a grade, edite as linhas `cron` em
`.github/workflows/check.yml` (sempre em UTC: BRT + 3h).

**Se o atraso incomodar:** o workflow também aceita disparo manual
(`workflow_dispatch`), então um agendador externo que chame a API do GitHub no
horário exato (cron-job.org, por exemplo, com um token de acesso) elimina o
atraso da fila. Não está configurado porque exige criar e guardar um token
fora do GitHub.

---

## Rodando na sua máquina

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium

python check_tarifas.py --debug      # só mostra o que extraiu, não grava nada
```

| Comando | O que faz |
|---|---|
| `--debug` | Extrai e salva `debug/<fonte>.txt` + screenshot. Não grava estado, não notifica. |
| `--dry-run` | Compara com o estado e mostra o alerta que sairia, sem gravar nem enviar. |
| `--seed` | Regrava a baseline de todas as fontes (use depois de mexer nos filtros). |
| `--only ID` | Roda só uma fonte (`--only shopee_comissao`). |
| `--config` / `--state` | Aponta para outro `sources.yaml` / arquivo de estado. |

Os três primeiros também estão no `Run workflow` do GitHub, no seletor
**modo** — dá para calibrar direto pelo Actions, sem instalar nada. No modo
`debug` o que foi extraído fica disponível como artifact da execução.

Os testes do parser (não precisam de rede) rodam com:

```bash
python test_parsing.py
```

Eles também rodam no CI antes de cada verificação — se uma mudança no parser
quebrar a detecção, o job para antes de gravar um estado errado.

---

## Calibrando

Praticamente todo ajuste é em `sources.yaml`, sem tocar no código:

**Veio pouca coisa / faltou a tabela.** A página provavelmente demora mais
para montar: aumente `settle_ms` daquela fonte. Confira o screenshot do modo
`--debug` para ver o que o navegador estava mostrando na hora da leitura.

**Alerta todo dia por bobagem.** Alguma linha muda sozinha (data de revisão,
contador, nome de campanha). Rode `--debug`, ache a linha em
`debug/<fonte>.txt` e acrescente um regex em `ignore_patterns`. Depois rode
`--seed` para regravar a baseline limpa.

**Entrou muito número irrelevante** (preço de produto de exemplo, número de
telefone). Deixe `keywords` mais restrito para aquela fonte — a chave aceita
override por fonte.

**Quer monitorar outra página.** Copie um bloco de `sources`, troque `id`,
`url` e `validate_regex`. Não precisa mudar mais nada; o `id` é o que liga a
fonte ao estado salvo.

---

## Limitações, honestamente

- **A calibração ainda não foi validada contra os sites reais.** O parser foi
  testado contra páginas de exemplo, com testes automatizados cobrindo
  extração, filtro de ruído, virada de ano e escalonamento de falha — mas o
  ambiente onde ele foi escrito não tinha acesso aos quatro sites. A primeira
  rodada (a baseline, no passo 3) é o que fecha essa lacuna: é ela que mostra
  se os números lidos batem com o site.
- **Isto é scraping.** Se a Shopee, o Mercado Livre ou a Amazon reformularem a
  página, o monitor pode passar a ler outra coisa — ou nada. O alerta de
  "monitor cego" cobre o caso de a página sumir, e o alerta de texto cobre
  reformulação parcial; mas uma reescrita grande pede uma passada no
  `sources.yaml`.
- **Bloqueio anti-bot é possível.** Nenhum dos quatro endereços exige login
  hoje, mas se um deles passar a servir captcha para datacenter, aquela fonte
  vai cair no alerta de falha. A saída seria rodar o check de outro lugar
  (um runner self-hosted, por exemplo).
- **As páginas não contam a história toda.** A comissão real do Mercado Livre
  varia por categoria e por tipo de anúncio, e a tarifa do FBA varia por peso
  e dimensão. O monitor vigia o que está publicado nessas quatro URLs; para
  tabela por categoria, o certo é acrescentar a URL específica em
  `sources.yaml`.

---

## Estrutura

```
take-rates-watcher/
├── .github/workflows/check.yml   # agendamento diário + commit do estado
├── check_tarifas.py              # o monitor
├── sources.yaml                  # o que é monitorado (e como)
├── test_parsing.py               # testes do parser, sem rede
├── state/last_seen.json          # última leitura (commitada pelo bot)
└── requirements.txt
```
