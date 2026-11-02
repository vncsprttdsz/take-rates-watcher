#!/usr/bin/env python3
"""
Testes das partes do check_tarifas.py que nao dependem de rede: extracao de
numeros, filtro de linhas, deteccao de mudanca e descoberta da URL do ano novo.

    python test_parsing.py        (ou: pytest test_parsing.py)

Nao cobre o scraping em si - para conferir se o parser esta pegando a tabela
certa de cada site, use `python check_tarifas.py --debug`.
"""

import check_tarifas as ct

DEFAULTS = {
    "keywords": ["comissao", "tarifa", "taxa", "frete", "custo", "plano"],
    "ignore_patterns": [r"(?i)^\s*(ultima\s+)?atualiza(c|ç)(a|ã)o\s*[:\-]"],
}
SRC = {"id": "x", "marketplace": "Shopee", "nome": "Comissao"}

PAGINA_2026 = """
Central do Vendedor
Menu principal Minha conta Sair
Comissao para vendedores CNPJ e CPF em 2026
A partir de 1 de janeiro de 2026, a comissao do Programa de Frete Gratis
passa a ser de 14% por item vendido.
O teto de comissao por item e de R$ 100,00.
Vendedores CPF pagam taxa de 6% sobre o valor do pedido.
Baixe o app e acompanhe suas vendas
Atualizacao: 03/09/2026
(c) Shopee
"""


def snap(texto, headings=(), tabelas=()):
    """Mesmo formato do snapshot() real, a partir de texto puro."""
    kw, ig = ct.build_filters(DEFAULTS, SRC)
    linhas, secoes = ct.relevant_lines_sections(texto, kw, ig, list(headings))
    return {
        "url": "https://exemplo/2026",
        "numbers": ct.extract_numbers(linhas),
        "text_hash": ct.hash_lines(linhas),
        "lines": linhas,
        "sections": secoes,
        "tables": ct.normalize_tables(list(tabelas)),
    }


def test_numeros_extraidos():
    s = snap(PAGINA_2026)
    achados = {ct.display_key(k) for k in s["numbers"]}
    assert "14%" in achados, achados
    assert "6%" in achados, achados
    assert "R$ 100,00" in achados, achados


def test_ruido_descartado():
    """Menu, banner e data de revisao nao podem entrar na comparacao."""
    s = snap(PAGINA_2026)
    texto = " ".join(s["lines"]).lower()
    assert "menu principal" not in texto
    assert "baixe o app" not in texto
    assert "atualizacao: 03/09" not in texto
    # ...mas a data de VIGENCIA da tarifa tem que ficar.
    assert "1 de janeiro de 2026" in texto


def test_formatos_equivalentes_nao_alertam():
    """'14%', '14,0 %' e '14.00%' sao o mesmo numero."""
    a = snap("comissao de 14% por item")
    b = snap("comissao de 14,0 % por item")
    c = snap("comissao de 14.00% por item")
    assert set(a["numbers"]) == set(b["numbers"]) == set(c["numbers"])


def test_brl_milhar_vs_decimal():
    assert ct.parse_brl("1.234,56") == 1234.56
    assert ct.parse_brl("1.000") == 1000.0      # ponto de milhar
    assert ct.parse_brl("19.90") == 19.90       # ponto decimal
    assert ct.parse_brl("100") == 100.0
    assert ct.fmt_brl(1234.5) == "R$ 1.234,50"
    assert ct.fmt_pct(14.0) == "14%"
    assert ct.fmt_pct(14.5) == "14,5%"


def test_mudanca_de_tarifa_detectada():
    antes = snap(PAGINA_2026)
    depois = snap(PAGINA_2026.replace("14% por item", "15% por item"))
    evs = ct.compare(SRC, antes, depois)
    assert [e["kind"] for e in evs] == ["numeros"], evs
    assert [v for v, _ in evs[0]["entrou"]] == ["15%"]
    assert [v for v, _ in evs[0]["saiu"]] == ["14%"]


def test_pagina_igual_nao_alerta():
    antes = snap(PAGINA_2026)
    depois = snap(PAGINA_2026)
    assert ct.compare(SRC, antes, depois) == []


def test_banner_novo_nao_alerta():
    """Marketing mexendo na pagina nao pode acordar ninguem."""
    antes = snap(PAGINA_2026)
    depois = snap(PAGINA_2026.replace("Baixe o app e acompanhe suas vendas",
                                      "Novidade: campanha de aniversario!"))
    assert ct.compare(SRC, antes, depois) == []


def test_mudanca_de_regra_sem_numero_alerta_fraco():
    antes = snap(PAGINA_2026)
    depois = snap(PAGINA_2026.replace("por item vendido", "por pedido faturado"))
    evs = ct.compare(SRC, antes, depois)
    assert [e["kind"] for e in evs] == ["texto"], evs
    assert evs[0]["diff"], "diff vazio"


def test_descoberta_do_ano_seguinte():
    links = [
        {"href": "https://s/edu/article/31002/Comissao-para-vendedores-CNPJ-e-CPF-em-2027",
         "text": "Comissao 2027"},
        {"href": "https://s/edu/article/26839/Comissao-...-em-2026", "text": "Comissao 2026"},
        {"href": "https://s/edu/article/999/Como-embalar-em-2027", "text": "Embalagem"},
    ]
    src = {"link_discovery": {"enabled": True, "link_regex": "(?i)comiss|tarifa|taxa"}}
    achados = ct.discover_year_links(links, src, min_year=2026)
    assert achados == [
        "https://s/edu/article/31002/Comissao-para-vendedores-CNPJ-e-CPF-em-2027"
    ], achados


def test_descoberta_ignora_ano_igual_ou_menor():
    links = [{"href": "https://s/comissao-2025", "text": "Comissao 2025"}]
    src = {"link_discovery": {"enabled": True, "link_regex": "(?i)comiss"}}
    assert ct.discover_year_links(links, src, min_year=2026) == []


def test_pagina_invalida_rejeitada():
    src = {"validate_regex": "(?i)comiss"}
    erro404 = {"status": 404, "text": "x" * 900}
    curta = {"status": 200, "text": "comissao"}
    outra = {"status": 200, "text": "Pagina nao encontrada. " + "x" * 900}
    boa = {"status": 200, "text": "Comissao de 14% " + "x" * 900}
    assert ct.page_is_valid(erro404, src, 400)[0] is False
    assert ct.page_is_valid(curta, src, 400)[0] is False
    assert ct.page_is_valid(outra, src, 400)[0] is False
    assert ct.page_is_valid(boa, src, 400)[0] is True


def test_falha_nao_conta_como_alteracao_de_tarifa():
    """Monitor quebrado e status, nao noticia de tarifa - secoes separadas."""
    falha = {"kind": "falha", "severidade": 3, "fonte": "Shopee - x",
             "src": SRC, "horas": 12.5, "erro": "timeout"}
    msg = ct.build_message([falha], [])
    assert "Status do monitor" in msg
    assert "alteracao detectada" not in msg

    antes = snap(PAGINA_2026)
    depois = snap(PAGINA_2026.replace("14% por item", "15% por item"))
    msg2 = ct.build_message(ct.compare(SRC, antes, depois) + [falha], [])
    assert "1 alteracao detectada" in msg2, msg2
    assert "Status do monitor" in msg2


def test_mensagem_montada_e_fatiada():
    antes = snap(PAGINA_2026)
    depois = snap(PAGINA_2026.replace("14% por item", "15% por item"))
    evs = ct.compare(SRC, antes, depois)
    msg = ct.build_message(evs, [])
    assert "15%" in msg and "14%" in msg
    assert "<b>" in msg
    for chunk in ct.split_chunks("bloco\n\n" + ("y" * 9000)):
        assert len(chunk) <= ct.TELEGRAM_CHUNK


def test_url_canonica_ignora_tracking():
    """'?utm_source=x' e barra final nao sao mudanca de pagina."""
    a = "https://s/comissao-2026"
    assert ct.canon_url(a + "?utm_source=news") == ct.canon_url(a)
    assert ct.canon_url(a + "/") == ct.canon_url(a)
    assert ct.canon_url(a + "#tarifas") == ct.canon_url(a)
    assert ct.canon_url("https://s/comissao-2027") != ct.canon_url(a)


def test_troca_de_pagina_alerta_uma_vez_so():
    """O alerta de edicao nova sai da comparacao com o estado.

    Sem isso a rodada seguinte - que ja nasce apontando para a URL nova -
    repetiria o mesmo alerta todo dia.
    """
    antes = snap(PAGINA_2026)
    depois = snap(PAGINA_2026)
    depois["url"] = "https://exemplo/2027"

    evs = ct.compare(SRC, antes, depois)
    assert [e["kind"] for e in evs] == ["url_ano"], evs

    assert ct.compare(SRC, depois, depois) == [], "repetiu o alerta na rodada seguinte"


def test_query_string_nova_nao_alerta():
    antes = snap(PAGINA_2026)
    depois = snap(PAGINA_2026)
    depois["url"] = antes["url"] + "?utm_campaign=set26"
    assert ct.compare(SRC, antes, depois) == []


def test_rodada_sem_mudanca_nao_mexe_no_estado():
    """Com varias rodadas por dia, estado igual = nenhum commit.

    Se algo que muda a cada rodada (horario, titulo) entrasse no estado, o
    workflow commitaria toda vez.
    """
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)
    s1 = snap(PAGINA_2026)
    e1 = ct.persisted(s1, {}, changed=True, now=t0)

    s2 = snap(PAGINA_2026)
    s2["upgraded_from"] = None
    e2 = ct.persisted(s2, e1, changed=False, now=t0 + timedelta(hours=3))
    assert e2 == e1, (e1, e2)
    assert set(e1) == set(ct.PERSIST_KEYS)

    e3 = ct.persisted(snap(PAGINA_2026), e1, changed=True, now=t0 + timedelta(hours=6))
    assert e3["changed_at"] != e1["changed_at"]


def test_falha_alerta_por_tempo_e_nao_por_contagem():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 9, 22, 11, 0, tzinfo=timezone.utc)
    bom = ct.persisted(snap(PAGINA_2026), {}, changed=True, now=t0)

    # varias falhas em poucas horas: silencio, e o estado so muda na primeira
    e1, alerta, _ = ct.register_failure(bom, "timeout", t0)
    assert not alerta
    e2, alerta, _ = ct.register_failure(e1, "timeout de novo", t0 + timedelta(hours=3))
    assert not alerta
    assert e2 == e1, "queda longa nao pode gerar um commit por rodada"
    e3, alerta, _ = ct.register_failure(e2, "x", t0 + timedelta(hours=6))
    assert not alerta, "6h de falha ainda e cedo"

    # passou de FAILURE_ALERT_HOURS: alerta uma vez
    e4, alerta, horas = ct.register_failure(e3, "x", t0 + timedelta(hours=12, minutes=5))
    assert alerta and horas >= 12
    e5, alerta, _ = ct.register_failure(e4, "x", t0 + timedelta(hours=15))
    assert not alerta, "repetiu o alerta de falha"

    # a ultima leitura boa sobrevive a queda
    assert e5["numbers"] == bom["numbers"] and e5["text_hash"] == bom["text_hash"]


def test_mesma_pagina_com_outro_ano_na_url_nao_e_edicao_nova():
    """O caso real da Shopee: o site ignora o slug e devolve o artigo de 2026
    em `.../26839/...-em-2027`. Isso nao pode virar "edicao nova"."""
    atual = {"title": "Comissao", "text": PAGINA_2026}
    mesma = {"title": "Comissao", "text": PAGINA_2026}
    ok, why = ct.is_new_edition(mesma, atual, 2027, SRC, DEFAULTS)
    assert not ok and "mesmo conteudo" in why, why


def test_pagina_diferente_sem_o_ano_nao_e_edicao_nova():
    atual = {"title": "", "text": PAGINA_2026}
    outra = {"title": "", "text": PAGINA_2026.replace("14%", "15%")}
    ok, why = ct.is_new_edition(outra, atual, 2027, SRC, DEFAULTS)
    assert not ok and "2027" in why, why


def test_edicao_nova_de_verdade_e_aceita():
    atual = {"title": "", "text": PAGINA_2026}
    nova = {"title": "", "text": PAGINA_2026.replace("2026", "2027").replace("14%", "15%")}
    ok, why = ct.is_new_edition(nova, atual, 2027, SRC, DEFAULTS)
    assert ok, why


def test_brl_com_mil_e_milhao():
    """'R$ 500 mil' era lido como R$ 500,00."""
    achados = {ct.display_key(k) for k in ct.extract_numbers([
        "venda ate R$ 500 mil com comissao ZERO",
        "faturamento igual ou superior a R$81 mil",
        "premio de R$ 1,5 milhão",
        "custa R$ 19,90 por item",
    ])}
    assert achados == {"R$ 500.000,00", "R$ 81.000,00", "R$ 1.500.000,00", "R$ 19,90"}, achados


def _rodada(prev, texto, url="https://exemplo/2026", hora=0):
    """Simula o que main() faz com uma leitura: (alertas, estado novo)."""
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc) + timedelta(hours=hora)
    s = snap(texto)
    s["url"] = url
    evs = ct.compare(SRC, prev, s)
    if evs and not ct.is_confirmed(prev, s):
        return [], ct.with_pending(prev, s, now)
    return evs, ct.persisted(s, prev, bool(evs), now)


def test_oscilacao_de_uma_rodada_nao_alerta():
    """Incidentes reais de set/2026: a pagina carregou so o menu / com glitch
    e voltou ao normal na rodada seguinte. Antes: 2 alertas falsos cada."""
    from datetime import datetime, timezone
    base = ct.persisted(snap(PAGINA_2026), {}, True,
                        datetime(2026, 9, 22, tzinfo=timezone.utc))
    so_menu = "Comissao e Frete\nAnuncios em Lives\n" + "menu " * 100
    evs, st = _rodada(base, so_menu, hora=1)
    assert evs == [] and "pending" in st
    evs, st = _rodada(st, PAGINA_2026, hora=2)
    assert evs == [] and "pending" not in st
    assert st == base, "estado deveria voltar a ser exatamente o confirmado"


def test_mudanca_que_persiste_alerta_uma_vez():
    from datetime import datetime, timezone
    base = ct.persisted(snap(PAGINA_2026), {}, True,
                        datetime(2026, 9, 22, tzinfo=timezone.utc))
    nova = PAGINA_2026.replace("14% por item", "15% por item")
    evs, st = _rodada(base, nova, hora=1)
    assert evs == [], "alertou na primeira leitura"
    evs, st = _rodada(st, nova, hora=3)
    assert [e["kind"] for e in evs] == ["numeros"], evs
    evs, st = _rodada(st, nova, hora=5)
    assert evs == [], "repetiu o alerta"


def test_pendente_substituido_por_outra_mudanca():
    """A->B->C->C: B nunca confirmou; o alerta e de A para C."""
    from datetime import datetime, timezone
    base = ct.persisted(snap(PAGINA_2026), {}, True,
                        datetime(2026, 9, 22, tzinfo=timezone.utc))
    b = PAGINA_2026.replace("14% por item", "15% por item")
    c = PAGINA_2026.replace("14% por item", "16% por item")
    _, st = _rodada(base, b, hora=1)
    evs, st = _rodada(st, c, hora=2)
    assert evs == []
    evs, st = _rodada(st, c, hora=3)
    num = evs[0]
    assert [v for v, _ in num["entrou"]] == ["16%"] and [v for v, _ in num["saiu"]] == ["14%"]


def test_titulo_com_ano_do_slug_nao_conta_como_edicao_nova():
    """Incidente de 24/09: o titulo da aba vem do slug (`...-em-2027`)."""
    atual = {"title": "Comissao em 2026", "text": PAGINA_2026}
    glitch = {"title": "Comissao em 2027",
              "text": PAGINA_2026.replace("Programa de Frete", "Programa  de  Frete!")}
    ok, why = ct.is_new_edition(glitch, atual, 2027, SRC, DEFAULTS)
    assert not ok and "2027" in why, why


def test_imagem_trocada_vira_linha_e_muda_o_hash():
    imgs_a = [{"src": "https://cdn/file/aaa.png?x=1", "alt": "tabela", "w": 800, "h": 400}]
    imgs_b = [{"src": "https://cdn/file/bbb.png?x=1", "alt": "tabela", "w": 800, "h": 400}]
    imgs_a2 = [{"src": "https://cdn/file/aaa.png?x=2", "alt": "tabela", "w": 800, "h": 400}]
    assert ct.image_lines(imgs_a) == ["[imagem] https://cdn/file/aaa.png"]
    assert ct.image_lines(imgs_a) == ct.image_lines(imgs_a2), "query da CDN nao e mudanca"
    src = dict(SRC, track_images=True)
    res = lambda imgs: {"url": "u", "title": "", "text": PAGINA_2026, "images": imgs}
    sa = ct.snapshot(src, res(imgs_a), DEFAULTS)
    sb = ct.snapshot(src, res(imgs_b), DEFAULTS)
    assert sa["text_hash"] != sb["text_hash"]
    assert sa["numbers"] == sb["numbers"]
    ev = ct.compare(src, sa, sb)
    assert [e["kind"] for e in ev] == ["texto"]
    assert "Imagem da pagina trocada" in ct.render_event(ev[0])
    # sem track_images, imagem nao entra
    assert ct.snapshot(SRC, res(imgs_a), DEFAULTS)["text_hash"] == \
           ct.snapshot(SRC, res(imgs_b), DEFAULTS)["text_hash"]


def _rodada_v(prev, texto, quando):
    """Como main() trata uma leitura, com memoria de versoes: (alertas, estado)."""
    s = snap(texto)
    evs = ct.compare(SRC, prev, s)
    if evs and ct.is_known_variant(prev, s, quando):
        return [], ct.with_variant_seen(prev, s, quando)
    if evs and not ct.is_confirmed(prev, s):
        return [], ct.with_pending(prev, s, quando)
    return evs, ct.persisted(s, prev, bool(evs), quando)


PAG_A = PAGINA_2026
PAG_B = PAGINA_2026.replace("Vendedores CPF pagam taxa de 6%",
                            "Amazon Mais Vantagens: taxa de 2,5% para produtos ate R$ 10.000")


def test_pagina_alternando_entre_duas_versoes_alerta_uma_vez():
    """Incidente real (Amazon /precos, 28/10 a 02/11/2026): o site servia duas
    versoes e cada troca confirmada virava alerta - 8 alertas, 7 falsos."""
    from datetime import datetime, timedelta, timezone
    t = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
    st = ct.persisted(snap(PAG_A), {}, True, t)
    seq = "BBAABABBAABBABAA"  # inclui repeticoes seguidas, como na vida real
    alertas = []
    for i, v in enumerate(seq):
        evs, st = _rodada_v(st, PAG_A if v == "A" else PAG_B, t + timedelta(hours=2 * (i + 1)))
        alertas += evs
    assert len(alertas) == 1, [e["kind"] for e in alertas]
    assert {x for x, _ in alertas[0]["entrou"]} == {"2,5%", "R$ 10.000,00"}, alertas[0]["entrou"]


def test_alternancia_no_mesmo_dia_nao_mexe_no_estado():
    from datetime import datetime, timedelta, timezone
    t = datetime(2026, 10, 28, 1, tzinfo=timezone.utc)
    st = ct.persisted(snap(PAG_A), {}, True, t)
    _, st = _rodada_v(st, PAG_B, t + timedelta(hours=1))
    _, st = _rodada_v(st, PAG_B, t + timedelta(hours=2))  # confirmada: B atual, A lembrada
    _, st1 = _rodada_v(st, PAG_A, t + timedelta(hours=3))
    _, st2 = _rodada_v(st1, PAG_A, t + timedelta(hours=5))
    _, st3 = _rodada_v(st2, PAG_B, t + timedelta(hours=7))
    assert st1 == st2 == st3, "alternar no mesmo dia gerou commit"


def test_versao_nova_de_verdade_ainda_alerta_durante_alternancia():
    from datetime import datetime, timedelta, timezone
    t = datetime(2026, 10, 28, 1, tzinfo=timezone.utc)
    st = ct.persisted(snap(PAG_A), {}, True, t)
    for i, v in enumerate("BBAB"):
        _, st = _rodada_v(st, PAG_A if v == "A" else PAG_B, t + timedelta(hours=i + 1))
    pag_c = PAG_B.replace("2,5%", "3%")
    evs, st = _rodada_v(st, pag_c, t + timedelta(hours=6))
    assert evs == []
    evs, st = _rodada_v(st, pag_c, t + timedelta(hours=7))
    assert [x for x, _ in evs[0]["entrou"]] == ["3%"], evs


def test_versao_esquecida_depois_de_14_dias_volta_a_alertar():
    """Um rollback de verdade, semanas depois, nao pode passar calado."""
    from datetime import datetime, timedelta, timezone
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    st = ct.persisted(snap(PAG_A), {}, True, t)
    _, st = _rodada_v(st, PAG_B, t + timedelta(hours=1))
    evs, st = _rodada_v(st, PAG_B, t + timedelta(hours=2))
    assert evs  # A -> B alertou; A agora e lembrada
    depois = t + timedelta(days=ct.VARIANT_MEMORY_DAYS + 2)
    evs, st = _rodada_v(st, PAG_A, depois)
    assert evs == [] and "pending" in st, "versao velha deveria contar como novidade"
    evs, st = _rodada_v(st, PAG_A, depois + timedelta(hours=2))
    assert evs, "rollback depois da janela nao alertou"


TAB_ML = {"title": "Custos dos Envios", "rows": [
    {"h": True, "c": ["Preço do produto"] * 4},
    {"h": True, "c": ["Peso", "R$ 0 a R$ 18,99", "R$ 19 a R$ 48,99", "R$ 79 a R$ 99,99"]},
    {"h": False, "c": ["Até 0,3 kg", "R$ 5,65", "R$ 6,85", "R$ 12,95"]},
    {"h": False, "c": ["De 0,3 a 0,5 kg", "R$ 5,95", "R$ 6,95", "R$ 13,85"]},
]}


def _tab(mudar=None, extra=None):
    import copy
    t = copy.deepcopy(TAB_ML)
    for (i, j), v in (mudar or {}).items():
        t["rows"][i]["c"][j] = v
    if extra:
        t["rows"].append({"h": False, "c": extra})
    return t


def test_tabela_normalizada_com_cabecalho_em_dois_niveis():
    t = ct.normalize_tables([TAB_ML])[0]
    assert t["titulo"] == "Custos dos Envios"
    # o grupo "Preco do produto" cobre todas as colunas e sai do nome
    assert t["colunas"] == ["Peso", "R$ 0 a R$ 18,99", "R$ 19 a R$ 48,99", "R$ 79 a R$ 99,99"]
    assert [r[0] for r in t["linhas"]] == ["Até 0,3 kg", "De 0,3 a 0,5 kg"]


def test_celula_alterada_diz_linha_coluna_antes_agora():
    a = ct.normalize_tables([_tab()])[0]
    b = ct.normalize_tables([_tab({(2, 3): "R$ 13,60"})])[0]
    d = ct.diff_table(a, b)
    assert d["celulas"] == [{"linha": "Até 0,3 kg", "coluna": "R$ 79 a R$ 99,99",
                             "antes": "R$ 12,95", "agora": "R$ 13,60", "delta": "+5,0%"}], d
    assert d["total"] == 6


def test_linha_nova_na_tabela():
    a = ct.normalize_tables([_tab()])[0]
    b = ct.normalize_tables([_tab(extra=["De 0,5 a 1 kg", "R$ 6,05", "R$ 7,15", "R$ 14,45"])])[0]
    d = ct.diff_table(a, b)
    assert d["linhas_novas"] == ["De 0,5 a 1 kg"] and not d["celulas"]


def test_tabela_igual_nao_gera_diferenca():
    a = ct.normalize_tables([_tab()])[0]
    assert ct.diff_table(a, ct.normalize_tables([_tab()])[0]) is None


def test_alerta_de_tabela_nao_repete_a_linha_como_frase():
    """A linha da tabela tambem esta no texto; nao pode aparecer duas vezes."""
    txt_a = "Custos de envio\nAté 0,3 kg R$ 5,65 R$ 6,85 R$ 12,95\n" + PAGINA_2026
    txt_b = "Custos de envio\nAté 0,3 kg R$ 5,65 R$ 6,85 R$ 13,60\n" + PAGINA_2026
    a, b = snap(txt_a, tabelas=[_tab()]), snap(txt_b, tabelas=[_tab({(2, 3): "R$ 13,60"})])
    ev = ct.compare(SRC, a, b)[0]
    assert ev["kind"] == "numeros"
    assert ev["explicacao"]["pares"] == [], ev["explicacao"]["pares"]
    msg = ct.render_event(ev)
    assert "Até 0,3 kg · R$ 79 a R$ 99,99: R$ 12,95 → <b>R$ 13,60</b> (+5,0%)" in msg, msg


def test_frase_alterada_vira_par_antes_agora_com_secao():
    a = snap("Parcelamento\nPor uma tarifa de 1,5% sobre as vendas.\nTaxa fixa de R$ 2,00.",
             headings=["Parcelamento"])
    b = snap("Parcelamento\nPor uma tarifa de 2,5% sobre as vendas.\nTaxa fixa de R$ 2,00.",
             headings=["Parcelamento"])
    ex = ct.compare(SRC, a, b)[0]["explicacao"]
    assert len(ex["pares"]) == 1 and ex["pares"][0]["secao"] == "Parcelamento"
    assert ex["resumo"] == ["1,5% → 2,5%"]
    antes, agora = ct.inline_diff(ex["pares"][0]["antes"], ex["pares"][0]["agora"])
    assert "<s>1,5%</s>" in antes and "<b>2,5%</b>" in agora


def test_valor_que_continua_em_outro_lugar_ainda_conta_como_tarifa():
    """1,5% -> 2,5% numa frase, com outro 1,5% em outra: antes era 'texto'."""
    base = "Amazon Vantagens: tarifa de 1,5%.\nAmazon Mais Vantagens: tarifa de {}."
    a, b = snap(base.format("1,5%")), snap(base.format("2,5%"))
    a_b = ct.compare(SRC, a, b)
    assert [e["kind"] for e in a_b] == ["numeros"]
    volta = ct.compare(SRC, b, a)  # 2,5% -> 1,5%: o conjunto de valores fica igual
    assert [e["kind"] for e in volta] == ["numeros"], [e["kind"] for e in volta]


def test_frase_que_so_mudou_de_lugar_nao_e_mudanca():
    pares, rem, add = ct.pair_lines(["a tarifa e 1%", "b custo R$ 2", "c taxa 3%"],
                                    ["c taxa 3%", "a tarifa e 1%", "b custo R$ 2"])
    assert pares == [] and rem == [] and add == []


def test_alerta_prioriza_o_que_tem_valor():
    a = snap("Ao iniciar, tarifa de 1,5% para produtos a partir de R$ 40.\nSaiba mais sobre os planos")
    b = snap("Mais Vantagens: plano completo, tarifa de 2,5% e desconto Pix de 5%, inscricao automatica.\n"
             "Conheca os planos de pagamento")
    msg = ct.render_event(ct.compare(SRC, a, b)[0])
    pos = lambda t: msg.index(t)
    assert pos("R$ 40") < pos("Saiba mais") and pos("2,5%") < pos("Conheca"), msg


def test_secoes_e_tabelas_nao_mexem_no_hash():
    """Guardar a estrutura nao pode, sozinho, disparar mudanca."""
    a = snap(PAGINA_2026)
    b = snap(PAGINA_2026, headings=["Comissao para vendedores CNPJ e CPF em 2026"], tabelas=[_tab()])
    assert a["text_hash"] == b["text_hash"]
    assert ct.compare(SRC, a, b) == []


def test_migracao_de_formato_adota_sem_alertar_e_aprende_as_versoes():
    """Linhas deixaram de ser cortadas em 300 chars: o hash muda sem a pagina
    mudar. Por 12h a fonte adota as leituras calada, inclusive as duas versoes
    de um site que alterna, e depois volta ao normal."""
    from datetime import datetime, timedelta, timezone
    t = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    velho = {k: v for k, v in snap(PAG_A).items() if k in ("url", "numbers", "text_hash", "lines")}
    velho["text_hash"] = "hash-do-formato-antigo"
    st = velho
    for i, pag in enumerate([PAG_A, PAG_B, PAG_A, PAG_B]):
        quando = t + timedelta(hours=2 * i)
        ate = ct.migration_deadline(st, quando)
        assert ate, f"deveria estar migrando na leitura {i}"
        st = ct.migrate_entry(st, snap(pag), quando, ate)
        st["formato"] = ct.LINE_FORMAT
    depois = t + timedelta(hours=ct.MIGRATION_HOURS + 1)
    assert ct.migration_deadline(st, depois) is None
    s_b = snap(PAG_B)
    assert ct.is_known_variant(st, s_b, depois) or st["text_hash"] == s_b["text_hash"]
    s_a = snap(PAG_A)
    assert ct.is_known_variant(st, s_a, depois) or st["text_hash"] == s_a["text_hash"]


def test_fonte_ja_no_formato_novo_nao_migra():
    from datetime import datetime, timezone
    st = dict(snap(PAG_A), formato=ct.LINE_FORMAT)
    assert ct.migration_deadline(st, datetime(2026, 11, 2, tzinfo=timezone.utc)) is None


def test_url_do_ano():
    assert ct.url_year("https://s/artigo-em-2026") == 2026
    assert ct.url_year("https://s/artigo") is None


if __name__ == "__main__":
    testes = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    falhas = 0
    for nome, fn in testes:
        try:
            fn()
            print(f"  ok   {nome}")
        except AssertionError as exc:
            falhas += 1
            print(f"  FALHOU {nome}: {exc}")
    print(f"\n{len(testes) - falhas}/{len(testes)} testes passaram")
    raise SystemExit(1 if falhas else 0)
