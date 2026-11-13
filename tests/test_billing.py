"""Tariff estimates and shop packages."""

from bot.billing import (
    estimate_job_cost,
    minimum_job_cost,
    package_by_id,
    parse_invoice_payload,
    tariffs_text,
)
from bot.formats import CutMode


def test_estimate_ai_without_subs() -> None:
    assert estimate_job_cost(CutMode.HIGHLIGHTS, burn_subs=False, clip_count=3) == 20


def test_estimate_ai_with_subs() -> None:
    assert estimate_job_cost(CutMode.HIGHLIGHTS, burn_subs=True, clip_count=3) == 29


def test_estimate_sequential() -> None:
    assert estimate_job_cost(CutMode.SEQUENTIAL, burn_subs=False, clip_count=10) == 5
    assert estimate_job_cost(CutMode.SEQUENTIAL, burn_subs=True, clip_count=10) == 35


def test_minimum_job_cost() -> None:
    assert minimum_job_cost(CutMode.HIGHLIGHTS) == 20
    assert minimum_job_cost(CutMode.SEQUENTIAL) == 5


def test_package_prices_rub() -> None:
    s = package_by_id("s")
    m = package_by_id("m")
    large = package_by_id("l")
    assert s is not None and s.diamonds == 50 and s.price_rub == 69
    assert m is not None and m.diamonds == 150 and m.price_rub == 149
    assert large is not None and large.diamonds == 500 and large.price_rub == 399
    assert s.price_kopecks == 6900
    assert package_by_id("nope") is None


def test_parse_invoice_payload() -> None:
    assert parse_invoice_payload("diamonds:m") == "m"
    assert parse_invoice_payload("diamonds:nope") is None
    assert parse_invoice_payload("other") is None


def test_tariffs_text_mentions_actions() -> None:
    text = tariffs_text()
    assert "Яркие моменты" in text
    assert "Нарезка подряд" in text
    assert "Субтитры" in text
    assert "69" in text
    assert "💎" in text
