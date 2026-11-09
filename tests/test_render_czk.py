import render


def test_czk_whole_crowns_without_decimals():
    assert render._money(12990, "CZK") == "12 990 Kč"
    assert render._money(990, "CZK") == "990 Kč"
    assert render._money(1234567, "CZK") == "1 234 567 Kč"


def test_czk_keeps_halere_when_present():
    assert render._money(1234.56, "CZK") == "1 234,56 Kč"


def test_existing_currencies_unchanged():
    assert render._money(1234.5, "EUR") == "€1.234,50"
    assert render._money(1234.5, "SEK") == "1 234,50 kr"
    assert render._money(1234.5, "DKK") == "1.234,50 kr."


def test_english_stores_write_euros_the_english_way():
    assert render._money(1234.5, "EUR", "en") == "€1,234.50"
    assert render._money(1234.5, "EUR", "en-IE") == "€1,234.50"
    assert render._money(1234.5, "EUR", "nl") == "€1.234,50"
    assert render._money(1234.5, "SEK", "en") == "1 234,50 kr"   # a market currency keeps its market's style
