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


def snap(texto):
    kw, ig = ct.build_filters(DEFAULTS, SRC)
    linhas = ct.relevant_lines(texto, kw, ig)
    return {
        "url": "https://exemplo/2026",
        "numbers": ct.extract_numbers(linhas),
        "text_hash": ct.hash_lines(linhas),
        "lines": linhas,
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
