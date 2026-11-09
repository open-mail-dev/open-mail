import render


def test_ron_always_with_bani_and_dot_thousands():
    assert render._money(1299, "RON") == "1.299,00 lei"
    assert render._money(49, "RON") == "49,00 lei"
    assert render._money(1234567.5, "RON") == "1.234.567,50 lei"


def test_huf_whole_forints_space_thousands():
    assert render._money(12990, "HUF") == "12 990 Ft"
    assert render._money(990, "HUF") == "990 Ft"
    assert render._money(1234.56, "HUF") == "1 234,56 Ft"


def test_existing_currencies_unchanged():
    assert render._money(1234.5, "EUR") == "€1.234,50"
    assert render._money(12990, "CZK") == "12 990 Kč"
