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
from datetime import datetime, timezone
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
MAX_LINE_CHARS = 300

# Ha quanto tempo uma fonte precisa estar falhando para avisar que o proprio
# monitor quebrou. E tempo, e nao numero de falhas, porque o numero depende da
# grade do cron: com ~9 rodadas por dia, "3 falhas" seriam 4 horas - uma
# janela de manutencao do site ja dispararia. 12h cobre varias rodadas em
# qualquer grade razoavel e ainda avisa no mesmo dia.
FAILURE_ALERT_HOURS = 12

# Campos da leitura que vao para o arquivo de estado. Fica de fora tudo que
# muda a cada rodada sem a pagina mudar (horario da leitura, titulo da aba,
# de onde veio o upgrade de URL): com varias rodadas por dia, cada um desses
# viraria um commit inutil no repositorio.
PERSIST_KEYS = ("url", "numbers", "text_hash", "lines", "changed_at")

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
  for (const sel of cands) {
    const el = document.querySelector(sel);
    if (el && (el.innerText || '').trim().length > 300) return el.innerText;
  }
  return document.body ? document.body.innerText : '';
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
    """Fica so com o que fala de dinheiro.

    Sem esse filtro o monitor compara a pagina inteira e alerta a cada troca de
    banner promocional. Uma linha entra se tem '%', 'R$' ou uma das palavras de
    `keywords` - e nao casa com nenhum `ignore_patterns`.
    """
    out: List[str] = []
    for raw in text.splitlines():
        line = normalize_line(raw)
        if not line or len(line) < 3:
            continue
        if any(p.search(line) for p in ignores):
            continue
        flat = strip_accents(line).lower()
        if "%" in line or "r$" in flat or any(k in flat for k in keywords):
            out.append(line[:MAX_LINE_CHARS])
        if len(out) >= MAX_LINES_STORED:
            break
    return out


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
        text = page.evaluate(CLEAN_JS) or ""
        title = page.title()
        final_url = page.url
    except PWError as exc:
        raise FetchError(f"extracao falhou: {exc}") from exc

    return {
        "status": status,
        "url": final_url,
        "title": title,
        "text": text,
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
    if not re.search(rf"\b{year}\b", f"{cand.get('title', '')}\n{cand['text']}"):
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

    if saiu or entrou:
        events.append(
            {
                "kind": "numeros",
                "severidade": 0,
                "fonte": nome,
                "src": src,
                "url": snap["url"],
                "saiu": [(display_key(k), prev["numbers"][k]) for k in saiu],
                "entrou": [(display_key(k), snap["numbers"][k]) for k in entrou],
            }
        )
    elif snap["text_hash"] != prev.get("text_hash"):
        events.append(
            {
                "kind": "texto",
                "severidade": 2,
                "fonte": nome,
                "src": src,
                "url": snap["url"],
                "diff": line_diff(prev.get("lines", []), snap["lines"]),
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


def render_event(ev: dict) -> str:
    head = f"{mp_emoji(ev['src'])} <b>{esc(ev['fonte'])}</b>"

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
        parts = [f"📝 {head}", "<i>Texto mudou, mas nenhum valor mudou</i>", ""]
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


def snapshot(src: dict, res: dict, defaults: dict) -> dict:
    keywords, ignores = build_filters(defaults, src)
    lines = relevant_lines(res["text"], keywords, ignores)
    numbers = extract_numbers(lines)
    return {
        "url": res["url"],
        "titulo": res["title"],
        "numbers": numbers,
        "text_hash": hash_lines(lines),
        "lines": lines,
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
                print(f"  valores: {', '.join(sorted(display_key(k) for k in snap['numbers']))}")
                print(f"  -> {out}")
                continue

            is_baseline = args.seed or not prev.get("text_hash")
            novos: List[dict] = []
            if is_baseline:
                print("  baseline registrada (sem comparacao)")
                baselines.append({
                    "kind": "baseline",
                    "fonte": f"{src['marketplace']} - {src['nome']}",
                    "src": src, "url": snap["url"],
                    "numeros": sorted(display_key(k) for k in snap["numbers"]),
                })
            else:
                novos = compare(src, prev, snap)
                for ev in novos:
                    print(f"  MUDOU: {ev['kind']}")
                events.extend(novos)
                if prev.get("failure_alerted"):
                    events.append({
                        "kind": "recuperou", "severidade": 4,
                        "fonte": f"{src['marketplace']} - {src['nome']}", "src": src,
                    })

            if not args.dry_run:
                mudou = is_baseline or any(e["kind"] in TARIFA_KINDS for e in novos)
                state["sources"][sid] = persisted(snap, prev, mudou, now)

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
