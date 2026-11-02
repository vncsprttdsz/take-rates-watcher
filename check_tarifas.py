#!/usr/bin/env python3
"""
take-rates-watcher - avisa quando Shopee, Mercado Livre ou Amazon mexem nas
tarifas cobradas de vendedores.

Roda no GitHub Actions, abre cada pagina de `sources.yaml` num Chromium
headless, guarda o que leu em state/last_seen.json e manda um Telegram quando
alguma coisa muda.

Uso:
    python check_tarifas.py                 # rodada normal (notifica e grava)
    python check_tarifas.py --debug         # so mostra o que extraiu, nao grava
    python check_tarifas.py --dry-run       # compara e mostra o alerta, nao grava
    python check_tarifas.py --only shopee_comissao
    python check_tarifas.py --seed          # regrava a baseline de tudo, sem alertar
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    sys.exit("Faltando dependencia: pip install PyYAML")

try:
    import requests
except ImportError:
    sys.exit("Faltando dependencia: pip install requests")

try:
    from playwright.sync_api import sync_playwright
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeout
except ImportError:
    sys.exit("Faltando dependencia: pip install playwright && playwright install chromium")


ROOT = Path(__file__).parent
CONFIG_FILE = ROOT / "sources.yaml"
STATE_FILE = ROOT / "state" / "last_seen.json"
DEBUG_DIR = ROOT / "debug"

STATE_VERSION = 1

NAV_TIMEOUT_MS = 45_000

# Quantas linhas relevantes guardamos por fonte. So servem para montar o diff
# da notificacao; o hash e calculado sobre todas elas, entao o corte tem que
# ser generoso o bastante para nao decapitar uma tabela de tarifas.
MAX_LINES_STORED = 600
# Era 300 ate nov/2026, e cortava justamente o que importa: a frase da Amazon
# sobre o Mais Vantagens era guardada ate "a tarifa padrao ... pass" - a taxa
# vinha depois do corte, e uma mudanca nela passaria sem alerta.
MAX_LINE_CHARS = 2000

# Formato das linhas guardadas no estado. Mudou de 1 para 2 quando as linhas
# deixaram de ser cortadas em 300 caracteres: o hash de toda frase longa muda
# sem a pagina mudar. Por MIGRATION_HOURS a fonte adota as leituras sem alertar
# - tempo para reaprender tambem as versoes de um site que alterna (Amazon).
LINE_FORMAT = 2
MIGRATION_HOURS = 12

# Ha quanto tempo uma fonte precisa estar falhando para avisar que o proprio
# monitor quebrou. E tempo, e nao numero de falhas, porque o numero depende da
# grade do cron: com ~9 rodadas por dia, "3 falhas" seriam 4 horas - uma
# janela de manutencao do site ja dispararia. 12h cobre varias rodadas em
# qualquer grade razoavel e ainda avisa no mesmo dia.
FAILURE_ALERT_HOURS = 12

# Campos da leitura que vao para o arquivo de estado. `sections` e `tables` nao
# entram no hash (a deteccao continua so pelas linhas); existem para guardar a
# pagina numa forma que permita EXPLICAR a mudanca depois. Fica de fora tudo
# que muda a cada rodada sem a pagina mudar (horario da leitura, titulo da aba,
# de onde veio o upgrade de URL): com varias rodadas por dia, cada um desses
# viraria um commit inutil no repositorio.
PERSIST_KEYS = ("url", "numbers", "text_hash", "lines", "changed_at", "sections", "tables")

# Versoes da pagina que ja foram a confirmada e por quanto tempo sao lembradas.
# Existe porque a Amazon serviu /precos em DUAS versoes ao mesmo tempo (teste
# A/B ou troca em andamento, out/2026): cada rodada caia numa delas e cada
# troca virava "tarifa mudou" - 7 alertas falsos em 5 dias. Uma versao lembrada
# que volta nao alerta; so conteudo inedito alerta. 14 dias: depois disso, uma
# versao que reaparece e tratada como novidade (um rollback de verdade).
VARIANT_MEMORY_DAYS = 14
MAX_VARIANTS = 5

# Telegram corta em 4096 caracteres. 3500 deixa folga para o HTML.
TELEGRAM_CHUNK = 3500

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

EMOJI = {"Shopee": "🟠", "Mercado Livre": "🟡", "Amazon": "🔵"}


# ============================================================
# Extracao de texto da pagina
# ============================================================

# Roda dentro do navegador. Remove a moldura do site (menu, rodape, scripts) e
# devolve o innerText do maior candidato a "conteudo". innerText - e nao
# textContent - porque ele respeita o layout renderizado: celula de tabela vira
# tabulacao e bloco vira quebra de linha, que e exatamente o que o parser quer.
CLEAN_JS = """
() => {
  const kill = 'script,style,noscript,svg,iframe,nav,[role="navigation"],' +
               'footer,[role="contentinfo"],[aria-hidden="true"]';
  document.querySelectorAll(kill).forEach(e => e.remove());
  const cands = ['main', '[role="main"]', 'article', '#content', '.content', 'body'];
  let root = document.body;
  for (const sel of cands) {
    const el = document.querySelector(sel);
    if (el && (el.innerText || '').trim().length > 300) { root = el; break; }
  }
  if (!root) return {text: '', images: []};
  // Imagens de conteudo (icone e avatar ficam de fora pelo tamanho). Tabela de
  // tarifa publicada como imagem e invisivel para o innerText.
  const images = Array.from(root.querySelectorAll('img')).map(img => ({
    src: img.currentSrc || img.src || img.getAttribute('data-src') || '',
    alt: (img.alt || '').trim().slice(0, 120),
    w: img.naturalWidth || img.width || 0,
    h: img.naturalHeight || img.height || 0,
  })).filter(i => i.src && !i.src.startsWith('data:') && i.w >= 200);

  // Tabelas como tabelas (titulo, cabecalho, linhas) e os titulos de secao, para
  // o alerta conseguir dizer "linha X, coluna Y: antes -> agora" em vez de uma
  // sopa de numeros. Isolado num try: um site esquisito aqui nao pode derrubar
  // a leitura do texto, que e o que detecta a mudanca.
  let tables = [], headings = [];
  try {
    const clean = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
    const hs = Array.from(root.querySelectorAll('h1,h2,h3,h4,h5,h6,[role="heading"]'));
    headings = hs.map(clean).filter(t => t);
    const sel = 'table,[role="table"],[role="grid"]';
    const tbls = Array.from(root.querySelectorAll(sel))
      .filter(t => !(t.parentElement && t.parentElement.closest(sel)));
    for (const t of tbls) {
      const isHtml = t.tagName === 'TABLE';
      const rowEls = isHtml ? Array.from(t.rows)
                            : Array.from(t.querySelectorAll('[role="row"]'));
      const rows = [];
      for (const r of rowEls) {
        const cellEls = isHtml ? Array.from(r.cells) : Array.from(r.querySelectorAll(
          '[role="cell"],[role="gridcell"],[role="columnheader"],[role="rowheader"]'));
        const cells = [];
        let head = cellEls.length > 0;
        for (const c of cellEls) {
          const isHead = c.tagName === 'TH' || /header/.test(c.getAttribute('role') || '');
          head = head && isHead;
          // colspan repetido mantem as colunas alinhadas com o cabecalho
          const span = Math.min(isHtml ? (c.colSpan || 1) : 1, 20);
          for (let i = 0; i < span; i++) cells.push(clean(c).slice(0, 120));
        }
        if (cells.some(x => x)) rows.push({h: head, c: cells.slice(0, 20)});
      }
      if (rows.length < 2 || Math.max(...rows.map(r => r.c.length)) < 2) continue;
      let title = (isHtml && t.caption) ? clean(t.caption) : '';
      if (!title) {
        for (const h of hs) {
          if (h.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING) title = clean(h);
        }
      }
      tables.push({title: title.slice(0, 150), rows: rows.slice(0, 200)});
    }
  } catch (e) { tables = []; }
  return {text: root.innerText || '', images: images, tables: tables, headings: headings};
}
"""

LINKS_JS = """
() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
  href: a.href,
  text: (a.innerText || '').trim().slice(0, 200)
}))
"""


def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def normalize_line(s: str) -> str:
    """Espacos colapsados. Preserva acentos e caixa (a linha vai pro alerta)."""
    return re.sub(r"[ \t   ]+", " ", s).strip()


# ============================================================
# Numeros
# ============================================================

PCT_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*%")
BRL_RE = re.compile(
    r"R\$\s*(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"(?:\s*(mil|milh(?:ão|ao|ões|oes))\b)?",
    re.IGNORECASE,
)
# "R$ 500 mil" e "R$ 1,5 milhão" - texto de marketing escreve assim.
BRL_MULT = {"mil": 1_000, "milhão": 1_000_000, "milhao": 1_000_000,
            "milhões": 1_000_000, "milhoes": 1_000_000}


def parse_brl(raw: str) -> Optional[float]:
    """'1.234,56' -> 1234.56 | '100' -> 100.0 | '19.90' -> 19.90

    Regra: se ha virgula, o ponto e separador de milhar. Sem virgula, um ponto
    seguido de exatamente 3 digitos tambem e milhar ('R$ 1.000'); qualquer
    outro ponto e decimal ('R$ 19.90', que aparece em pagina traduzida).
    """
    raw = raw.strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw):
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_pct(raw: str) -> Optional[float]:
    """Em percentual o ponto e sempre decimal - ninguem escreve '1.000%'."""
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def fmt_pct(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{s.replace('.', ',')}%"


def fmt_brl(v: float) -> str:
    s = f"{v:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {s}"


def extract_numbers(lines: List[str]) -> Dict[str, str]:
    """Devolve {chave_canonica: linha onde apareceu pela primeira vez}.

    A chave e canonica ('pct:14.00') para que '14%', '14,0 %' e '14.00%' sejam
    o mesmo numero e nao virem alerta de mudanca. O valor guardado e a linha
    inteira, que e o que da sentido ao numero na hora de notificar.
    """
    found: Dict[str, str] = {}
    for line in lines:
        for m in PCT_RE.finditer(line):
            v = parse_pct(m.group(1))
            if v is not None and 0 < v <= 100:
                found.setdefault(f"pct:{v:.2f}", line)
        for m in BRL_RE.finditer(line):
            v = parse_brl(m.group(1))
            if v is not None and m.group(2):
                v *= BRL_MULT[m.group(2).lower()]
            if v is not None:
                found.setdefault(f"brl:{v:.2f}", line)
    return found


def display_key(key: str) -> str:
    kind, _, raw = key.partition(":")
    v = float(raw)
    return fmt_pct(v) if kind == "pct" else fmt_brl(v)


# ============================================================
# Filtro de linhas relevantes
# ============================================================


def build_filters(defaults: dict, src: dict) -> Tuple[List[str], List[re.Pattern]]:
    keywords = src.get("keywords", defaults.get("keywords", []))
    kw = [strip_accents(k).lower() for k in keywords]
    raw_ignores = src.get("ignore_patterns", defaults.get("ignore_patterns", []))
    ignores = [re.compile(p) for p in raw_ignores]
    return kw, ignores


def relevant_lines(text: str, keywords: List[str], ignores: List[re.Pattern]) -> List[str]:
    return relevant_lines_sections(text, keywords, ignores, [])[0]


def relevant_lines_sections(text: str, keywords: List[str], ignores: List[re.Pattern],
                            headings: List[str]) -> Tuple[List[str], List[str]]:
    """Fica so com o que fala de dinheiro - e anota a secao de cada linha.

    A secao e o ultimo titulo (h1-h6) visto antes da linha. Nao entra no hash:
    serve so para o alerta dizer ONDE na pagina a frase mudou.

    Sem esse filtro o monitor compara a pagina inteira e alerta a cada troca de
    banner promocional. Uma linha entra se tem '%', 'R$' ou uma das palavras de
    `keywords` - e nao casa com nenhum `ignore_patterns`.
    """
    heads = {strip_accents(normalize_line(h)).lower() for h in headings}
    out: List[str] = []
    sections: List[str] = []
    current = ""
    label = ""
    for raw in text.splitlines():
        line = normalize_line(raw)
        if not line or len(line) < 2:
            continue
        flat = strip_accents(line).lower()
        if flat in heads:
            current = line[:120]
        so_valor = is_value_only(line)
        if any(p.search(line) for p in ignores):
            continue
        if "%" in line or "r$" in flat or any(k in flat for k in keywords):
            if so_valor and label:
                line = f"{label}: {line}"
            out.append(line[:MAX_LINE_CHARS])
            sections.append(current)
        # Rotulo e texto curto SEM numero ("Eletronicos"); uma linha com valor
        # ("Comissao minima R$1,00") e informacao propria, nao nome de categoria.
        if not so_valor and len(line) <= 100 and not number_list(line):
            label = line
        if len(out) >= MAX_LINES_STORED:
            break
    return out, sections


def is_value_only(line: str) -> bool:
    """'12%', 'R$ 2,00', '15% ate R$ 100,00': valor sem dizer de que.

    Na Amazon a comissao por categoria e uma grade de cartoes (nao <table>):
    o texto vem como "Eletronicos" numa linha e "12%" na seguinte, e so o
    "12%" passava pelo filtro - um alerta "12% -> 13%" nao diria de qual
    categoria. Linha assim leva junto o rotulo que vem antes. Linha de tabela
    (muitos numeros, "Ate 0,3 kg R$ 5,65 R$ 6,85 ...") nao entra: la o rotulo
    e a propria primeira coluna.
    """
    nums = number_list(line)
    if not 1 <= len(nums) <= 2:
        return False
    resto = re.sub(r"R\$\s*[\d.,]+(\s*(mil|milh\w+))?|\d+(?:[.,]\d+)?\s*%", " ", line, flags=re.I)
    # <= 4 letras: "ate", "mes", "por". "Taxa fixa de R$ 2,00" ja diz o que e.
    return len(re.sub(r"[^A-Za-zÀ-ÿ]", "", resto)) <= 4


def hash_lines(lines: List[str]) -> str:
    """Hash da versao sem acento/caixa: acento corrigido nao e mudanca de tarifa."""
    flat = "\n".join(strip_accents(l).lower() for l in lines)
    return hashlib.sha256(flat.encode("utf-8")).hexdigest()


# ============================================================
# Navegacao
# ============================================================


class FetchError(Exception):
    pass


def fetch(page, url: str, settle_ms: int) -> dict:
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except (PWTimeout, PWError) as exc:
        raise FetchError(f"navegacao falhou: {type(exc).__name__}: {exc}") from exc

    status = resp.status if resp else 0
    page.wait_for_timeout(settle_ms)

    # Rola ate o fim e volta: pagina de marketing da Amazon e central de ajuda
    # do ML carregam blocos por lazy-load, e a tabela de tarifas costuma estar
    # justamente num bloco de baixo.
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1200)
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
    except PWError:
        pass

    links = []
    try:
        links = page.evaluate(LINKS_JS)
    except PWError:
        pass

    try:
        extracted = page.evaluate(CLEAN_JS) or {}
        text = extracted.get("text") or ""
        images = extracted.get("images") or []
        tables = extracted.get("tables") or []
        headings = extracted.get("headings") or []
        title = page.title()
        final_url = page.url
    except PWError as exc:
        raise FetchError(f"extracao falhou: {exc}") from exc

    return {
        "status": status,
        "url": final_url,
        "title": title,
        "text": text,
        "images": images,
        "tables": tables,
        "headings": headings,
        "links": links,
    }


def page_is_valid(res: dict, src: dict, min_chars: int) -> Tuple[bool, str]:
    if res["status"] and res["status"] >= 400:
        return False, f"HTTP {res['status']}"
    if len(res["text"].strip()) < min_chars:
        return False, f"texto curto demais ({len(res['text'].strip())} chars)"
    pattern = src.get("validate_regex")
    if pattern and not re.search(pattern, res["text"]):
        return False, f"nao casou validate_regex {pattern!r}"
    return True, "ok"


# ============================================================
# Resolucao de URL (a virada de ano)
# ============================================================

YEAR_RE = re.compile(r"(20\d{2})")


def url_year(url: str) -> Optional[int]:
    m = YEAR_RE.search(url)
    return int(m.group(1)) if m else None


def canon_url(url: str) -> str:
    """URL sem query, fragmento nem barra final.

    Serve para responder "a pagina mudou?" sem que um '?utm_source=' novo ou
    uma barra a mais passem por mudanca de edicao.
    """
    return re.sub(r"[?#].*$", "", (url or "").strip()).rstrip("/")


def discover_year_links(links: List[dict], src: dict, min_year: int) -> List[str]:
    """Procura, entre os links da pagina, a versao de um ano mais novo.

    Existe porque o id do artigo da Shopee (26839) muda quando eles publicam a
    tabela do ano seguinte - trocar so o '2026' por '2027' na URL nao acha
    nada. O que sobrevive a isso e o fato de a pagina antiga (ou o hub) linkar
    a nova. Devolve os candidatos do ano mais alto para o mais baixo.
    """
    disc = src.get("link_discovery") or {}
    if not disc.get("enabled"):
        return []
    rx = re.compile(disc.get("link_regex", "(?i)comiss|tarifa|taxa"))

    scored: List[Tuple[int, str]] = []
    seen = set()
    for link in links:
        href = (link.get("href") or "").split("#")[0]
        text = link.get("text") or ""
        if not href.startswith("http") or href in seen:
            continue
        if not (rx.search(href) or rx.search(text)):
            continue
        year = url_year(href) or url_year(text)
        if year is None or year <= min_year:
            continue
        seen.add(href)
        scored.append((year, href))

    scored.sort(key=lambda t: (-t[0], t[1]))
    return [href for _, href in scored[:5]]


def is_new_edition(cand: dict, current: dict, year: int, src: dict,
                   defaults: dict) -> Tuple[bool, str]:
    """A pagina candidata e mesmo a edicao de `year`, e nao a atual de novo?

    Carregar sem erro nao basta. O site da Shopee roteia pelo id do artigo e
    ignora o texto do link: `.../26839/...-em-2027` devolve o artigo de 2026
    com HTTP 200. Sem esta checagem o monitor "achava" uma edicao nova a cada
    rodada, subindo o ano sem parar e alertando toda vez.
    """
    keywords, ignores = build_filters(defaults, src)
    h_cand = hash_lines(relevant_lines(cand["text"], keywords, ignores))
    h_cur = hash_lines(relevant_lines(current["text"], keywords, ignores))
    if h_cand == h_cur:
        return False, "mesmo conteudo da pagina atual (o site ignora o texto da URL)"
    # So o corpo conta: o titulo da aba da Shopee e montado a partir do slug,
    # entao `...-em-2027` vira um titulo com "2027" mesmo sendo o artigo de 2026.
    if not re.search(rf"\b{year}\b", cand["text"]):
        return False, f"conteudo nao menciona {year}"
    return True, "ok"


def resolve_source(page, src: dict, prev: dict, defaults: dict, log) -> dict:
    """Carrega a fonte, tentando antes achar a edicao do ano seguinte.

    Ordem:
      1. ultima URL que funcionou (state) ou a do sources.yaml;
      2. se a pagina tem ano e ja estamos na janela de lookahead, tenta os
         templates dos anos seguintes e depois os links descobertos na pagina;
      3. se a URL base morreu, cai para os templates de qualquer ano.
    """
    settle = src.get("settle_ms", defaults.get("settle_ms", 3500))
    min_chars = src.get("min_chars", defaults.get("min_chars", 400))

    base_url = prev.get("url") or src["url"]
    attempts: List[str] = []

    def attempt(url: str) -> Optional[dict]:
        if url in attempts:
            return None
        attempts.append(url)
        log(f"    GET {url}")
        try:
            res = fetch(page, url, settle)
        except FetchError as exc:
            log(f"      x {exc}")
            return None
        ok, why = page_is_valid(res, src, min_chars)
        if not ok:
            log(f"      x rejeitada: {why}")
            return None
        log(f"      ok ({len(res['text'])} chars)")
        return res

    current = attempt(base_url)
    if current is None and base_url != src["url"]:
        # A URL salva morreu; volta para a do arquivo de configuracao.
        current = attempt(src["url"])

    templates = src.get("url_templates") or []
    now_year = datetime.now(timezone.utc).year

    # --- procura ativa pela edicao do ano seguinte ---------------------
    upgraded_from = None
    if src.get("year_in_url") and current is not None:
        cur_year = url_year(current["url"]) or now_year
        from_month = int(src.get("year_lookahead_from_month", 9))
        in_window = datetime.now(timezone.utc).month >= from_month
        # Se o calendario ja passou do ano da pagina, procura o tempo todo:
        # ficar em janeiro de 2027 lendo a tabela de 2026 e o pior dos mundos.
        overdue = now_year > cur_year
        if in_window or overdue:
            wanted = sorted({now_year + 1, now_year, cur_year + 1}, reverse=True)
            wanted = [y for y in wanted if y > cur_year]
            candidates: List[Tuple[int, str]] = []
            for year in wanted:
                for tpl in templates:
                    candidates.append((year, tpl.format(year=year)))
            for href in discover_year_links(current.get("links", []), src, cur_year):
                candidates.append((url_year(href) or cur_year + 1, href))

            for year, cand in candidates:
                res = attempt(cand)
                if res is None:
                    continue
                ok, why = is_new_edition(res, current, year, src, defaults)
                if not ok:
                    log(f"      x nao e edicao nova: {why}")
                    continue
                upgraded_from = current["url"]
                current = res
                break

    # --- ultimo recurso: templates de qualquer ano ---------------------
    if current is None and templates:
        for year in (now_year + 1, now_year, now_year - 1):
            for tpl in templates:
                res = attempt(tpl.format(year=year))
                if res is not None:
                    current = res
                    break
            if current is not None:
                break

    if current is None:
        raise FetchError(
            "nenhuma URL valida. Tentadas: " + ", ".join(attempts)
        )

    current["upgraded_from"] = upgraded_from
    return current


# ============================================================
# Estado
# ============================================================


def load_state(path: Path = STATE_FILE) -> dict:
    if not path.exists():
        return {"version": STATE_VERSION, "sources": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"AVISO: {path} ilegivel ({exc}); recomecando do zero.")
        return {"version": STATE_VERSION, "sources": {}}
    data.setdefault("version", STATE_VERSION)
    data.setdefault("sources", {})
    return data


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ============================================================
# Comparacao
# ============================================================


# ============================================================
# Explicacao da mudanca
# ============================================================
#
# A deteccao (hash das linhas) diz SE a pagina mudou. Esta parte diz O QUE
# mudou, numa forma que alguem entenda sem abrir o site:
#   - tabela: celula a celula, "linha . coluna: antes -> agora (+x%)";
#   - frase: o par antes/agora com o trecho que mudou destacado e a secao da
#     pagina onde ela esta;
#   - o que sumiu ou apareceu sem par.

def number_list(text: str) -> List[Tuple[str, float]]:
    """Todos os numeros (% e R$) da frase, NA ORDEM e com repeticao."""
    found: List[Tuple[int, str, float]] = []
    for m in PCT_RE.finditer(text):
        v = parse_pct(m.group(1))
        if v is not None and 0 < v <= 100:
            found.append((m.start(), "pct", v))
    for m in BRL_RE.finditer(text):
        v = parse_brl(m.group(1))
        if v is not None and m.group(2):
            v *= BRL_MULT[m.group(2).lower()]
        if v is not None:
            found.append((m.start(), "brl", v))
    return [(k, v) for _, k, v in sorted(found)]


def fmt_num(kind: str, v: float) -> str:
    return fmt_pct(v) if kind == "pct" else fmt_brl(v)


def fmt_delta(old: float, new: float) -> str:
    if not old:
        return ""
    d = (new - old) / old * 100
    return f"{d:+.1f}%".replace(".", ",")


def numbers_changed(old_lines: List[str], new_lines: List[str]) -> bool:
    """Contagem com repeticao: '1,5%' que vira '2,5%' conta mesmo se outro
    '1,5%' continuar na pagina - com conjunto, essa troca passava como texto."""
    def count(lines):
        c: Dict[Tuple[str, float], int] = {}
        for l in lines:
            for k in number_list(l):
                c[k] = c.get(k, 0) + 1
        return c
    return count(old_lines) != count(new_lines)


def _flat(s: str) -> str:
    return strip_accents(s).lower().strip()


def normalize_tables(raw: List[dict]) -> List[dict]:
    """{title, rows:[{h, c}]} do navegador -> {titulo, colunas, linhas}.

    Cabecalho = linhas iniciais so de <th>; com mais de uma (grupo "Preco do
    produto" em cima, faixas embaixo), o nome da coluna junta as duas.
    """
    out = []
    for t in raw or []:
        rows = [r for r in t.get("rows", []) if r.get("c")]
        if len(rows) < 2:
            continue
        n_head = 0
        while n_head < len(rows) - 1 and rows[n_head].get("h"):
            n_head += 1
        if n_head == 0:
            n_head = 1
        width = max(len(r["c"]) for r in rows)
        partes = []
        for j in range(width):
            parts = []
            for r in rows[:n_head]:
                cell = r["c"][j] if j < len(r["c"]) else ""
                if cell and cell not in parts:
                    parts.append(cell)
            partes.append(parts)
        # Grupo que cobre TODAS as colunas ("Preco do produto" em cima de tudo)
        # nao distingue coluna nenhuma - so encompridaria cada linha do alerta.
        primeiros = {p[0] for p in partes if len(p) > 1}
        if len(primeiros) == 1 and all(len(p) > 1 or not p for p in partes[1:]):
            grupo = primeiros.pop()
            partes = [[x for x in p if x != grupo] or p for p in partes]
        cols = [" · ".join(p) for p in partes]
        linhas = [r["c"] + [""] * (width - len(r["c"])) for r in rows[n_head:]]
        out.append({"titulo": t.get("title", ""), "colunas": cols, "linhas": linhas})
    return out


def table_rows_text(tables: List[dict]) -> set:
    """Como cada linha de tabela aparece no innerText (para nao explicar duas
    vezes: uma como celula e outra como frase)."""
    out = set()
    for t in tables or []:
        for row in [t["colunas"]] + t["linhas"]:
            out.add(_flat(normalize_line(" ".join(c for c in row if c))))
    return out


def _match_tables(old: List[dict], new: List[dict]) -> List[Tuple[Optional[dict], Optional[dict]]]:
    pairs, used = [], set()
    for t in new:
        hit = None
        for i, o in enumerate(old):
            if i not in used and o["titulo"] and _flat(o["titulo"]) == _flat(t["titulo"]):
                hit = i
                break
        if hit is None:
            for i, o in enumerate(old):
                if i not in used and [_flat(c) for c in o["colunas"]] == [_flat(c) for c in t["colunas"]]:
                    hit = i
                    break
        if hit is None:
            for i, o in enumerate(old):
                if i not in used:
                    hit = i
                    break
        if hit is not None:
            used.add(hit)
            pairs.append((old[hit], t))
        else:
            pairs.append((None, t))
    pairs += [(o, None) for i, o in enumerate(old) if i not in used]
    return pairs


def diff_table(old: Optional[dict], new: Optional[dict]) -> Optional[dict]:
    if old is None:
        return {"titulo": new["titulo"], "nova": True, "n_linhas": len(new["linhas"])}
    if new is None:
        return {"titulo": old["titulo"], "removida": True}

    def keyed(t):
        out, seen = {}, {}
        for row in t["linhas"]:
            k = _flat(row[0])
            seen[k] = seen.get(k, 0) + 1
            out[k if seen[k] == 1 else f"{k}#{seen[k]}"] = row
        return out

    ko, kn = keyed(old), keyed(new)
    cols = new["colunas"]
    celulas, deltas = [], []
    total = 0
    for k, row in kn.items():
        if k not in ko:
            continue
        orow = ko[k]
        for j in range(1, len(row)):
            total += 1
            a = orow[j] if j < len(orow) else ""
            b = row[j]
            if _flat(a) == _flat(b):
                continue
            col = cols[j] if j < len(cols) else f"coluna {j + 1}"
            na, nb = number_list(a), number_list(b)
            delta = ""
            if len(na) == 1 and len(nb) == 1 and na[0][0] == nb[0][0]:
                delta = fmt_delta(na[0][1], nb[0][1])
                if na[0][1]:
                    deltas.append((nb[0][1] - na[0][1]) / na[0][1] * 100)
            celulas.append({"linha": row[0], "coluna": col, "antes": a, "agora": b, "delta": delta})
    novas = [r[0] for k, r in kn.items() if k not in ko]
    removidas = [r[0] for k, r in ko.items() if k not in kn]
    colunas_mudaram = [_flat(c) for c in old["colunas"]] != [_flat(c) for c in cols]
    if not (celulas or novas or removidas or colunas_mudaram):
        return None
    return {
        "titulo": new["titulo"] or old["titulo"],
        "celulas": celulas, "total": total, "deltas": deltas,
        "linhas_novas": novas, "linhas_removidas": removidas,
        "colunas_antes": old["colunas"] if colunas_mudaram else None,
        "colunas_agora": cols if colunas_mudaram else None,
    }


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _flat(a), _flat(b), autojunk=False).ratio()


def pair_lines(old: List[str], new: List[str]) -> Tuple[List[Tuple[str, str]], List[str], List[str]]:
    """Casa cada frase que sumiu com a frase nova mais parecida (>= 50%).

    Primeiro bloco a bloco (onde a pagina mudou); depois, o que sobrou, no geral
    - pega frase que mudou E trocou de lugar. Frase identica que so mudou de
    lugar nao e mudanca.
    """
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    pairs: List[Tuple[str, str]] = []
    removed: List[str] = []
    added: List[str] = []

    def greedy(olds, news):
        used, prs, rem = set(), [], []
        for o in olds:
            best, bj = 0.0, None
            for j, n in enumerate(news):
                if j not in used:
                    r = _sim(o, n)
                    if r > best:
                        best, bj = r, j
            if bj is not None and best >= 0.5:
                used.add(bj)
                prs.append((o, news[bj]))
            else:
                rem.append(o)
        return prs, rem, [n for j, n in enumerate(news) if j not in used]

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            continue
        prs, rem, add = greedy(old[i1:i2], new[j1:j2])
        pairs += prs
        removed += rem
        added += add
    # frase identica so trocada de lugar
    for line in list(removed):
        if line in added:
            removed.remove(line)
            added.remove(line)
    prs, removed, added = greedy(removed, added)
    pairs += prs
    pairs = [(a, b) for a, b in pairs if _flat(a) != _flat(b)]
    return pairs, removed, added


def inline_diff(a: str, b: str, context: int = 10) -> Tuple[str, str]:
    """Antes/agora em HTML do Telegram: so o trecho que mudou, com folga de
    `context` palavras de cada lado; o que saiu riscado, o que entrou em negrito."""
    ta, tb = a.split(), b.split()
    sm = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    ops = sm.get_opcodes()
    changed = [o for o in ops if o[0] != "equal"]
    if not changed:
        return esc(a), esc(b)
    a_lo, a_hi = max(0, changed[0][1] - context), min(len(ta), changed[-1][2] + context)
    b_lo, b_hi = max(0, changed[0][3] - context), min(len(tb), changed[-1][4] + context)

    def side(tokens, lo, hi, idx, tag):
        out = []
        for op, i1, i2, j1, j2 in ops:
            s0, s1 = (i1, i2) if idx == 0 else (j1, j2)
            s0, s1 = max(s0, lo), min(s1, hi)
            if s0 >= s1:
                continue
            chunk = esc(" ".join(tokens[s0:s1]))
            out.append(chunk if op == "equal" else f"<{tag}>{chunk}</{tag}>")
        text = " ".join(out)
        return ("… " if lo > 0 else "") + text + (" …" if hi < len(tokens) else "")

    return side(ta, a_lo, a_hi, 0, "s"), side(tb, b_lo, b_hi, 1, "b")


def explain(prev: dict, snap: dict) -> dict:
    """O que mudou, em partes que a mensagem sabe mostrar."""
    tabelas = [d for d in (diff_table(o, n) for o, n in
               _match_tables(prev.get("tables") or [], snap.get("tables") or [])) if d]
    em_tabela = table_rows_text(prev.get("tables")) | table_rows_text(snap.get("tables"))

    def prosa(lines, secs):
        secs = secs if secs and len(secs) == len(lines) else [""] * len(lines)
        return [(l, s) for l, s in zip(lines, secs)
                if _flat(l) not in em_tabela and not l.startswith(IMAGE_PREFIX)]

    old_p = prosa(prev.get("lines", []), prev.get("sections"))
    new_p = prosa(snap.get("lines", []), snap.get("sections"))
    sec_of = {l: s for l, s in old_p}
    sec_of.update({l: s for l, s in new_p})
    pairs, removed, added = pair_lines([l for l, _ in old_p], [l for l, _ in new_p])

    imgs_old = [l for l in prev.get("lines", []) if l.startswith(IMAGE_PREFIX)]
    imgs_new = [l for l in snap.get("lines", []) if l.startswith(IMAGE_PREFIX)]

    resumo = []
    for a, b in pairs:
        na, nb = number_list(a), number_list(b)
        if len(na) == len(nb):
            for (ka, va), (kb, vb) in zip(na, nb):
                if ka == kb and va != vb:
                    resumo.append(f"{fmt_num(ka, va)} → {fmt_num(kb, vb)}")
    return {
        "tabelas": tabelas,
        "pares": [{"antes": a, "agora": b, "secao": sec_of.get(b) or sec_of.get(a, "")}
                  for a, b in pairs],
        "removidas": [{"texto": l, "secao": sec_of.get(l, "")} for l in removed],
        "novas": [{"texto": l, "secao": sec_of.get(l, "")} for l in added],
        "imagens": imgs_old != imgs_new,
        "resumo": list(dict.fromkeys(resumo)),
    }


def line_diff(old: List[str], new: List[str], limit: int = 12) -> List[str]:
    diff = [
        l
        for l in difflib.unified_diff(old, new, lineterm="", n=0)
        if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))
    ]
    return diff[:limit]


def compare(src: dict, prev: dict, snap: dict) -> List[dict]:
    """Eventos que merecem notificacao, do mais grave para o menos."""
    events: List[dict] = []
    nome = f"{src['marketplace']} - {src['nome']}"

    # A troca de pagina sai da comparacao com o ESTADO, nao do flag da rodada:
    # assim a rodada seguinte, que ja nasce apontando para a URL nova, nao
    # repete o alerta - e um redirecionamento inesperado tambem aparece.
    antes = canon_url(prev.get("url", ""))
    agora = canon_url(snap["url"])
    if antes and antes != agora:
        events.append(
            {
                "kind": "url_ano",
                "severidade": 1,
                "fonte": nome,
                "src": src,
                "url": snap["url"],
                "detalhe": f"a pagina passou a ser outra:\n{prev['url']}\n->  {snap['url']}",
            }
        )

    old_nums = set(prev.get("numbers", {}))
    new_nums = set(snap["numbers"])
    saiu = sorted(old_nums - new_nums)
    entrou = sorted(new_nums - old_nums)

    if snap["text_hash"] == prev.get("text_hash") and not (saiu or entrou):
        return events

    explicacao = explain(prev, snap)
    if saiu or entrou or numbers_changed(prev.get("lines", []), snap["lines"]):
        events.append(
            {
                "kind": "numeros",
                "severidade": 0,
                "fonte": nome,
                "src": src,
                "url": snap["url"],
                "saiu": [(display_key(k), prev["numbers"][k]) for k in saiu],
                "entrou": [(display_key(k), snap["numbers"][k]) for k in entrou],
                "explicacao": explicacao,
            }
        )
    else:
        events.append(
            {
                "kind": "texto",
                "severidade": 2,
                "fonte": nome,
                "src": src,
                "url": snap["url"],
                "diff": line_diff(prev.get("lines", []), snap["lines"]),
                "explicacao": explicacao,
            }
        )

    return events


# ============================================================
# Mensagem
# ============================================================


def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def mp_emoji(src: dict) -> str:
    return EMOJI.get(src.get("marketplace", ""), "•")


MAX_CELULAS_MSG = 10
MAX_FRASES_MSG = 6


def _secao(secao: str, texto: str) -> str:
    return f"<i>{esc(secao)}</i>\n" if secao and _flat(secao) != _flat(texto) else ""


def _corta(texto: str, n: int = 600) -> str:
    return texto if len(texto) <= n else texto[: n - 1].rstrip() + "…"


def render_explicacao(ex: dict) -> List[str]:
    """As partes de explain() em texto de Telegram, da mais importante para a
    menos: tabelas, frases alteradas, frases novas/removidas, imagens."""
    out: List[str] = []
    if ex.get("resumo"):
        out.append("<b>Resumo:</b> " + esc(" · ".join(ex["resumo"][:4])))

    for t in ex.get("tabelas", []):
        nome = esc(t["titulo"] or "Tabela")
        if t.get("nova"):
            out.append(f"📊 <b>{nome}</b>\nTabela nova na página ({t['n_linhas']} linhas)")
            continue
        if t.get("removida"):
            out.append(f"📊 <b>{nome}</b>\nTabela removida da página")
            continue
        bloco = [f"📊 <b>{nome}</b>"]
        cel, deltas = t["celulas"], t["deltas"]
        if cel:
            linha = f"{len(cel)} de {t['total']} valores mudaram"
            if len(deltas) >= 3:
                media = sum(deltas) / len(deltas)
                if max(deltas) - min(deltas) < 0.05:
                    linha += f" · todos <b>{media:+.1f}%</b>".replace(".", ",")
                else:
                    linha += (f" · variação média <b>{media:+.1f}%</b>"
                              f" (de {min(deltas):+.1f}% a {max(deltas):+.1f}%)").replace(".", ",")
            bloco.append(linha)
            for c in cel[:MAX_CELULAS_MSG]:
                delta = f" ({c['delta']})" if c["delta"] else ""
                bloco.append(f"• {esc(c['linha'])} · {esc(c['coluna'])}: "
                             f"{esc(c['antes'] or '—')} → <b>{esc(c['agora'] or '—')}</b>{esc(delta)}")
            if len(cel) > MAX_CELULAS_MSG:
                bloco.append(f"<i>… e mais {len(cel) - MAX_CELULAS_MSG}</i>")
        for r in t["linhas_novas"][:5]:
            bloco.append(f"➕ linha nova: {esc(r)}")
        for r in t["linhas_removidas"][:5]:
            bloco.append(f"➖ linha removida: {esc(r)}")
        if t.get("colunas_agora"):
            bloco.append("Colunas mudaram:\n  antes: " + esc(" | ".join(t["colunas_antes"]))
                         + "\n  agora: " + esc(" | ".join(t["colunas_agora"])))
        out.append("\n".join(bloco))

    # O que mexe em valor vem primeiro; "Saiba mais sobre os planos" por ultimo.
    # Removido vem antes de novo: le-se "saiu isto, entrou aquilo".
    itens = []
    for p in ex.get("pares", []):
        mudou_valor = number_list(p["antes"]) != number_list(p["agora"])
        antes, agora = inline_diff(p["antes"], p["agora"])
        itens.append((0 if mudou_valor else 2,
                      f"✏️ {_secao(p['secao'], p['agora'])}<b>Antes:</b> {antes}\n<b>Agora:</b> {agora}"))
    sem_par = [(x, "➖ <b>Removido:</b>") for x in ex.get("removidas", [])] + \
              [(x, "➕ <b>Novo:</b>") for x in ex.get("novas", [])]
    for item, rot in sem_par:
        tem_valor = bool(number_list(item["texto"]))
        itens.append((1 if tem_valor else 3,
                      f"{_secao(item['secao'], item['texto'])}{rot} {esc(_corta(item['texto']))}"))
    itens.sort(key=lambda t: t[0])  # sort estavel: dentro da prioridade, ordem da pagina
    out += [t for _, t in itens[:MAX_FRASES_MSG]]
    if len(itens) > MAX_FRASES_MSG:
        out.append(f"<i>… e mais {len(itens) - MAX_FRASES_MSG} trecho(s) alterado(s) na página</i>")
    if ex.get("imagens"):
        out.append("🖼 Uma imagem do conteúdo foi trocada — pode ser a tabela de tarifas, confira na página")
    return out


def render_event(ev: dict) -> str:
    head = f"{mp_emoji(ev['src'])} <b>{esc(ev['fonte'])}</b>"
    corpo = render_explicacao(ev["explicacao"]) if ev.get("explicacao") else []

    if ev["kind"] in ("numeros", "texto") and corpo:
        titulo = ("🚨 " + head + "\n<i>Valor de tarifa mudou</i>") if ev["kind"] == "numeros" \
            else ("📝 " + head + "\n<i>Texto mudou, nenhum valor mudou</i>")
        so_imagem = ev["explicacao"].get("imagens") and len(corpo) == 1
        if so_imagem:
            titulo = "📝 " + head + "\n<i>Imagem da pagina trocada — pode ser a tabela de tarifas, confira</i>"
            corpo = []
        return "\n\n".join([titulo] + corpo + [f'🔗 <a href="{esc(ev["url"])}">ver pagina</a>'])

    if ev["kind"] == "numeros":
        parts = [f"🚨 {head}", "<i>Valor de tarifa mudou</i>"]
        if ev["entrou"]:
            parts.append("\n<b>Entrou:</b>")
            for val, ctx in ev["entrou"][:8]:
                parts.append(f"  ➕ <b>{esc(val)}</b> — {esc(ctx[:180])}")
            if len(ev["entrou"]) > 8:
                parts.append(f"  <i>… e mais {len(ev['entrou']) - 8}</i>")
        if ev["saiu"]:
            parts.append("\n<b>Saiu:</b>")
            for val, ctx in ev["saiu"][:8]:
                parts.append(f"  ➖ <b>{esc(val)}</b> — {esc(ctx[:180])}")
            if len(ev["saiu"]) > 8:
                parts.append(f"  <i>… e mais {len(ev['saiu']) - 8}</i>")
        parts.append(f'\n🔗 <a href="{esc(ev["url"])}">ver pagina</a>')
        return "\n".join(parts)

    if ev["kind"] == "texto":
        so_imagem = ev["diff"] and all(IMAGE_PREFIX in l for l in ev["diff"])
        aviso = ("Imagem da pagina trocada — pode ser a tabela de tarifas, confira"
                 if so_imagem else "Texto mudou, mas nenhum valor mudou")
        parts = [f"📝 {head}", f"<i>{aviso}</i>", ""]
        for line in ev["diff"]:
            parts.append(f"<code>{esc(line[:200])}</code>")
        parts.append(f'\n🔗 <a href="{esc(ev["url"])}">ver pagina</a>')
        return "\n".join(parts)

    if ev["kind"] == "url_ano":
        return (
            f"📅 {head}\n<i>Edicao nova publicada</i>\n\n"
            f"<code>{esc(ev['detalhe'])}</code>\n"
            f'\n🔗 <a href="{esc(ev["url"])}">ver pagina</a>'
        )

    if ev["kind"] == "falha":
        return (
            f"⚠️ {head}\n<i>O monitor nao consegue mais ler esta pagina</i>\n"
            f"(falhando ha {ev['horas']:.0f}h)\n\n<code>{esc(ev['erro'][:400])}</code>"
        )

    if ev["kind"] == "recuperou":
        return f"✅ {head}\n<i>Voltou a ser lida normalmente</i>"

    if ev["kind"] == "baseline":
        parts = [
            f"{mp_emoji(ev['src'])} <b>{esc(ev['fonte'])}</b>",
            f"{len(ev['numeros'])} valores registrados: "
            + esc(", ".join(ev["numeros"][:20]))
            + ("…" if len(ev["numeros"]) > 20 else ""),
            f'🔗 <a href="{esc(ev["url"])}">ver pagina</a>',
        ]
        return "\n".join(parts)

    return head


# Eventos sobre a tarifa em si. O resto ("falha", "recuperou") e sobre a saude
# do proprio monitor e vai numa secao separada, para nao inflar a contagem de
# "alteracoes detectadas" com barulho de infraestrutura.
TARIFA_KINDS = {"numeros", "texto", "url_ano"}


def build_message(events: List[dict], baselines: List[dict]) -> str:
    blocks: List[str] = []
    ordenados = sorted(events, key=lambda e: e.get("severidade", 9))
    mudancas = [e for e in ordenados if e["kind"] in TARIFA_KINDS]
    saude = [e for e in ordenados if e["kind"] not in TARIFA_KINDS]

    if mudancas:
        n = len(mudancas)
        blocks.append(
            f"<b>Tarifas de marketplace — {n} "
            + ("alteracao detectada" if n == 1 else "alteracoes detectadas")
            + "</b>"
        )
        for ev in mudancas:
            blocks.append(render_event(ev))

    if saude:
        blocks.append("<b>Status do monitor</b>")
        for ev in saude:
            blocks.append(render_event(ev))

    if baselines:
        blocks.append(
            "<b>Baseline criada</b>\n<i>Primeira leitura destas paginas. "
            "Confira se os valores abaixo batem com o site — se estiverem "
            "estranhos, o parser precisa de ajuste.</i>"
        )
        for ev in baselines:
            blocks.append(render_event(ev))

    return "\n\n".join(blocks)


def split_chunks(text: str, limit: int = TELEGRAM_CHUNK) -> List[str]:
    chunks: List[str] = []
    buf = ""
    for block in text.split("\n\n"):
        candidate = f"{buf}\n\n{block}" if buf else block
        if len(candidate) > limit and buf:
            chunks.append(buf)
            buf = block[:limit]
        else:
            buf = candidate[:limit] if len(candidate) > limit else candidate
    if buf:
        chunks.append(buf)
    return chunks


def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("AVISO: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes; nao notifiquei.")
        print("----- mensagem que seria enviada -----")
        print(text)
        return False

    ok = True
    for chunk in split_chunks(text):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                },
                timeout=30,
            )
            if resp.status_code != 200:
                ok = False
                print(f"ERRO Telegram {resp.status_code}: {resp.text[:300]}")
        except requests.RequestException as exc:
            ok = False
            print(f"ERRO Telegram: {exc}")
    return ok


# ============================================================
# Main
# ============================================================


IMAGE_PREFIX = "[imagem] "


def image_lines(images: List[dict]) -> List[str]:
    """Uma linha por imagem de conteudo, pelo endereco sem query string.

    Entra no hash como qualquer linha: se a Shopee trocar a imagem da tabela de
    comissao, o arquivo novo tem outro endereco e a troca vira alerta. A query
    fica de fora porque CDN costuma pendurar ali assinatura e redimensionamento
    que mudam a cada carregamento.
    """
    out: List[str] = []
    for img in images:
        line = IMAGE_PREFIX + canon_url(img.get("src", ""))
        if line not in out:
            out.append(line)
    return out


def snapshot(src: dict, res: dict, defaults: dict) -> dict:
    keywords, ignores = build_filters(defaults, src)
    lines, sections = relevant_lines_sections(
        res["text"], keywords, ignores, res.get("headings", []))
    numbers = extract_numbers(lines)
    if src.get("track_images"):
        imgs = image_lines(res.get("images", []))
        lines += imgs
        sections += [""] * len(imgs)
    return {
        "url": res["url"],
        "titulo": res["title"],
        "numbers": numbers,
        "text_hash": hash_lines(lines),
        "lines": lines,
        "sections": sections,
        "tables": normalize_tables(res.get("tables", [])),
        "upgraded_from": res.get("upgraded_from"),
    }


def persisted(snap: dict, prev: dict, changed: bool, now: datetime) -> dict:
    """O que da leitura vai para o estado.

    `changed_at` so anda quando a pagina de fato mudou; numa rodada sem
    mudanca o registro sai identico ao anterior e o workflow nao commita nada.
    """
    entry = {k: snap[k] for k in PERSIST_KEYS if k in snap}
    entry["changed_at"] = (
        now.isoformat(timespec="seconds") if changed or not prev.get("changed_at")
        else prev["changed_at"]
    )
    variants = live_variants(prev, now)
    if changed and prev.get("text_hash"):
        # A versao que deixa de ser a confirmada passa a ser "conhecida".
        old = [prev["text_hash"], canon_url(prev.get("url", ""))]
        variants = [v for v in variants if v["key"] not in (old, change_key(snap))]
        variants = (variants + [{"key": old, "last_seen": now.date().isoformat()}])[-MAX_VARIANTS:]
    if variants:
        entry["variants"] = variants
    return entry


def live_variants(prev: dict, now: datetime) -> List[dict]:
    """Versoes lembradas que ainda estao dentro de VARIANT_MEMORY_DAYS."""
    out = []
    for v in prev.get("variants") or []:
        try:
            seen = date.fromisoformat(v["last_seen"])
        except (KeyError, TypeError, ValueError):
            continue
        if (now.date() - seen).days <= VARIANT_MEMORY_DAYS:
            out.append({"key": list(v["key"]), "last_seen": v["last_seen"]})
    return out[-MAX_VARIANTS:]


def is_known_variant(prev: dict, snap: dict, now: datetime) -> bool:
    key = change_key(snap)
    return any(v["key"] == key for v in live_variants(prev, now))


def with_variant_seen(prev: dict, snap: dict, now: datetime) -> dict:
    """A leitura e uma versao ja conhecida: nada muda, so a data em que foi vista.

    A data tem granularidade de dia, entao uma pagina que alterna o dia todo
    gera no maximo um commit por dia - e nenhum alerta. Uma mudanca pendente e
    descartada, como acontece quando a leitura volta a versao confirmada: a
    confirmacao exige duas leituras SEGUIDAS da mesma novidade.
    """
    entry = {k: prev[k] for k in PERSIST_KEYS if k in prev}
    key, today = change_key(snap), now.date().isoformat()
    entry["variants"] = [
        {"key": key, "last_seen": today} if v["key"] == key else v
        for v in live_variants(prev, now)
    ]
    return entry


def change_key(snap: dict) -> List[str]:
    return [snap["text_hash"], canon_url(snap["url"])]


def with_pending(prev: dict, snap: dict, now: datetime) -> dict:
    """Estado confirmado intacto + a leitura nova anotada como pendente.

    Guarda so a chave (hash + URL), nao a leitura inteira: para confirmar basta
    saber se a proxima rodada ve a MESMA coisa.
    """
    entry = {k: prev[k] for k in PERSIST_KEYS if k in prev}
    variants = live_variants(prev, now)
    if variants:
        entry["variants"] = variants
    entry["pending"] = {
        "key": change_key(snap),
        "seen_at": now.isoformat(timespec="seconds"),
    }
    return entry


def is_confirmed(prev: dict, snap: dict) -> bool:
    """A mudanca de agora e a mesma que a rodada anterior viu?

    Alerta so sai na segunda leitura igual. As paginas oscilam: em set/2026 a
    Shopee carregou uma vez so o menu (sumiram 22 valores) e outra vez com uma
    chave de traducao crua no lugar de "Proximo:", e nos dois casos voltou ao
    normal na rodada seguinte - 4 alertas falsos. Mudanca de tarifa de verdade
    persiste, e com ~9 rodadas por dia confirmar custa 1 a 3 horas.
    """
    pending = prev.get("pending") or {}
    return pending.get("key") == change_key(snap)


def migration_deadline(prev: dict, now: datetime) -> Optional[str]:
    """Ate quando a fonte ainda esta migrando de formato (None = nao esta)."""
    if not prev.get("text_hash"):
        return None
    ate = prev.get("migrar_ate")
    if not ate and prev.get("formato", 1) < LINE_FORMAT:
        ate = (now + timedelta(hours=MIGRATION_HOURS)).isoformat(timespec="seconds")
    if ate and now < datetime.fromisoformat(ate):
        return ate
    return None


def migrate_entry(prev: dict, snap: dict, now: datetime, ate: str) -> dict:
    """Adota a leitura sem alertar; a confirmada anterior vira versao conhecida."""
    if change_key(snap) == [prev["text_hash"], canon_url(prev.get("url", ""))]:
        entry = persisted(snap, prev, False, now)
    elif is_known_variant(prev, snap, now):
        entry = with_variant_seen(prev, snap, now)
    else:
        entry = persisted(snap, prev, True, now)
    entry["migrar_ate"] = ate
    return entry


def register_failure(prev: dict, erro: str, now: datetime) -> Tuple[dict, bool, float]:
    """Anota a falha no estado e diz se ja e hora de alertar.

    Preserva a ultima leitura boa (numeros, hash, URL): quando a pagina voltar,
    a comparacao continua de onde parou, em vez de recomecar da baseline.
    So grava algo na primeira falha e no momento do alerta - uma queda longa
    nao gera um commit por rodada.
    """
    entry = dict(prev)
    entry.pop("consecutive_failures", None)  # formato antigo, por contagem
    if not entry.get("failing_since"):
        entry["failing_since"] = now.isoformat(timespec="seconds")
        entry["last_error"] = erro[:500]
    desde = datetime.fromisoformat(entry["failing_since"])
    horas = (now - desde).total_seconds() / 3600
    alertar = horas >= FAILURE_ALERT_HOURS and not entry.get("failure_alerted")
    if alertar:
        entry["failure_alerted"] = True
        entry["last_error"] = erro[:500]
    return entry, alertar, horas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--debug", action="store_true",
                    help="so mostra o que extraiu (sem gravar estado nem notificar)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compara e mostra o alerta, mas nao grava nem notifica")
    ap.add_argument("--seed", action="store_true",
                    help="regrava a baseline de todas as fontes, sem alertar mudanca")
    ap.add_argument("--only", metavar="ID", help="roda so a fonte com este id")
    ap.add_argument("--config", type=Path, default=CONFIG_FILE, metavar="ARQ",
                    help=f"arquivo de fontes (padrao: {CONFIG_FILE.name})")
    ap.add_argument("--state", type=Path, default=STATE_FILE, metavar="ARQ",
                    help="arquivo de estado (padrao: state/last_seen.json)")
    args = ap.parse_args()

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    defaults = config.get("defaults", {})
    sources = config["sources"]
    if args.only:
        sources = [s for s in sources if s["id"] == args.only]
        if not sources:
            print(f"Nenhuma fonte com id {args.only!r}.")
            return 2

    state = load_state(args.state)
    now = datetime.now(timezone.utc)
    if args.debug:
        DEBUG_DIR.mkdir(exist_ok=True)

    events: List[dict] = []
    baselines: List[dict] = []
    erros = 0

    with sync_playwright() as pw:
        launch_args = {"args": ["--disable-blink-features=AutomationControlled"]}
        # Escape para rodar com um Chromium que ja esteja na maquina, em vez do
        # que o `playwright install` baixaria (util em container ou offline).
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
        if exe:
            launch_args["executable_path"] = exe
        browser = pw.chromium.launch(**launch_args)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1440, "height": 1000},
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"},
        )
        page = context.new_page()

        for src in sources:
            sid = src["id"]
            prev = state["sources"].get(sid, {})
            print(f"\n[{sid}] {src['marketplace']} - {src['nome']}")

            try:
                res = resolve_source(page, src, prev, defaults, log=print)
            except FetchError as exc:
                erros += 1
                entry, alertar, horas = register_failure(prev, str(exc), now)
                print(f"  FALHOU (falhando ha {horas:.1f}h): {exc}")
                if not args.debug and not args.dry_run:
                    # A pagina que sumiu tambem e noticia - mas so depois de
                    # FAILURE_ALERT_HOURS, para nao alertar a cada soluco de rede.
                    if alertar:
                        events.append({
                            "kind": "falha", "severidade": 3,
                            "fonte": f"{src['marketplace']} - {src['nome']}",
                            "src": src, "horas": horas, "erro": str(exc),
                        })
                    state["sources"][sid] = entry
                continue

            snap = snapshot(src, res, defaults)
            print(f"  {len(snap['lines'])} linhas relevantes, "
                  f"{len(snap['numbers'])} valores distintos")

            if args.debug:
                out = DEBUG_DIR / f"{sid}.txt"
                out.write_text(
                    f"URL: {snap['url']}\nTITULO: {snap['titulo']}\n"
                    f"VALORES: {', '.join(sorted(display_key(k) for k in snap['numbers']))}\n"
                    + "=" * 70 + "\n" + "\n".join(snap["lines"]),
                    encoding="utf-8",
                )
                try:
                    page.screenshot(path=str(DEBUG_DIR / f"{sid}.png"), full_page=True)
                except PWError:
                    pass
                valores = ", ".join(sorted(display_key(k) for k in snap["numbers"]))
                imgs = res.get("images", [])
                print(f"  valores: {valores}")
                for img in imgs:
                    print(f"  imagem {img['w']}x{img['h']} alt={img['alt']!r} {img['src']}")
                print(f"  -> {out}")
                # Anotacao aparece na pagina da execucao e e lida pela API do
                # GitHub - sem precisar baixar log nem artifact.
                if os.environ.get("GITHUB_ACTIONS"):
                    tabs = "; ".join(
                        f"'{t['titulo'] or '(sem titulo)'}' {len(t['linhas'])}x{len(t['colunas'])} "
                        f"[{' | '.join(t['colunas'][:4])}]" for t in snap["tables"]) or "nenhuma"
                    secs = sorted({x for x in snap["sections"] if x})
                    rotuladas = [l for l in snap["lines"] if ": " in l and len(l) <= 90
                                 and is_value_only(l.rsplit(": ", 1)[-1])][:10]
                    msg = (f"URL: {snap['url']}\nTITULO: {snap['titulo']}\n"
                           f"{len(snap['lines'])} linhas, {len(secs)} secoes, tabelas: {tabs}\n"
                           f"rotuladas: {' | '.join(rotuladas) or 'nenhuma'}\n"
                           f"valores: {valores}\n"
                           f"{len(imgs)} imagens:\n" + "\n".join(
                               f"{i['w']}x{i['h']} alt={i['alt']!r} {i['src']}" for i in imgs[:15]))
                    msg = msg[:3500].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                    print(f"::notice title=debug {sid}::{msg}")
                continue

            is_baseline = args.seed or not prev.get("text_hash")
            migrando_ate = None if is_baseline else migration_deadline(prev, now)
            novos: List[dict] = []
            pendente = False
            variante = False
            if migrando_ate:
                print(f"  estado em formato novo: leitura adotada sem alerta ate {migrando_ate}")
            elif is_baseline:
                print("  baseline registrada (sem comparacao)")
                baselines.append({
                    "kind": "baseline",
                    "fonte": f"{src['marketplace']} - {src['nome']}",
                    "src": src, "url": snap["url"],
                    "numeros": sorted(display_key(k) for k in snap["numbers"]),
                })
            else:
                novos = compare(src, prev, snap)
                variante = bool(novos) and is_known_variant(prev, snap, now)
                if variante:
                    print("  versao da pagina ja conhecida (o site alterna entre "
                          "versoes); sem alerta")
                    novos = []
                    pendente = False
                elif novos and not is_confirmed(prev, snap):
                    kinds = ", ".join(e["kind"] for e in novos)
                    print(f"  mudanca vista pela 1a vez ({kinds}); "
                          "so alerta se a proxima rodada confirmar")
                    novos = []
                    pendente = True
                else:
                    pendente = False
                    for ev in novos:
                        print(f"  MUDOU (confirmado): {ev['kind']}")
                events.extend(novos)
                if prev.get("failure_alerted"):
                    events.append({
                        "kind": "recuperou", "severidade": 4,
                        "fonte": f"{src['marketplace']} - {src['nome']}", "src": src,
                    })

            if not args.dry_run:
                if migrando_ate:
                    state["sources"][sid] = migrate_entry(prev, snap, now, migrando_ate)
                elif not is_baseline and variante:
                    state["sources"][sid] = with_variant_seen(prev, snap, now)
                elif not is_baseline and pendente:
                    state["sources"][sid] = with_pending(prev, snap, now)
                else:
                    mudou = is_baseline or any(e["kind"] in TARIFA_KINDS for e in novos)
                    state["sources"][sid] = persisted(snap, prev, mudou, now)
                state["sources"][sid]["formato"] = LINE_FORMAT

        context.close()
        browser.close()

    if args.debug:
        print(f"\nModo debug: nada gravado, nada notificado. Veja {DEBUG_DIR}/")
        return 1 if erros == len(sources) else 0

    if events or baselines:
        message = build_message(events, baselines)
        if args.dry_run:
            print("\n----- dry-run: mensagem que seria enviada -----")
            print(message)
        else:
            send_telegram(message)
    else:
        sufixo = f" ({erros} fonte(s) com falha)" if erros else ""
        print(f"\nNenhuma mudanca{sufixo}.")

    if not args.dry_run:
        save_state(state, args.state)
        print(f"Estado gravado em {args.state}")

    # So falha o job se TUDO falhou - uma fonte fora do ar nao pode derrubar
    # o monitoramento das outras.
    return 1 if erros and erros == len(sources) else 0


if __name__ == "__main__":
    sys.exit(main())
