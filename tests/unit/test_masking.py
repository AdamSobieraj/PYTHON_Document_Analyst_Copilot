"""Unit tests for PII masking — pure logic, no I/O."""

import pytest

from iso_insight.observability.masking import contains_pii, mask_pii


def test_masks_email() -> None:
    text = "Napisz do anna.kowalska@bank.pl w tej sprawie"
    assert mask_pii(text) == "Napisz do [EMAIL] w tej sprawie"


def test_masks_iban() -> None:
    assert mask_pii("Przelew na PL61109010140000071219812874") == "Przelew na [IBAN]"


def test_masks_national_id() -> None:
    assert mask_pii("Dane: 12345678901") == "Dane: [NATIONAL_ID]"


def test_masks_tax_id() -> None:
    assert mask_pii("NIP: 5270001681") == "NIP: [TAX_ID]"


def test_masks_phone_with_country_code() -> None:
    assert mask_pii("Zadzwoń: +48 612 345 678") == "Zadzwoń: [PHONE]"


def test_masks_phone_plain() -> None:
    assert mask_pii("Telefon 612 345 678") == "Telefon [PHONE]"


def test_plain_9digit_run_is_id_not_phone() -> None:
    # A 9-digit run without separators is a national ID, not a phone number.
    assert mask_pii("Id: 123456789") == "Id: [ID]"


def test_plain_text_unchanged() -> None:
    text = "Czy pole :20: musi byc unikalne w ramach biezacego dnia?"
    assert mask_pii(text) == text
    assert not contains_pii(text)


def test_masking_is_idempotent() -> None:
    text = "x anna@bank.pl 12345678901 5270001681"
    once = mask_pii(text)
    assert once == "x [EMAIL] [NATIONAL_ID] [TAX_ID]"
    assert mask_pii(once) == once


@pytest.mark.parametrize(
    "text",
    [
        "kontakt: jan@firma.pl",
        "IBAN PL61109010140000071219812874",
        "pesel 12345678901",
        "NIP 5270001681",
        "tel +48 612 345 678",
        "telefon 612 345 678",
        "id 123456789",
        "kod 12-34-56-78",
    ],
)
def test_contains_pii_detects_each_class(text: str) -> None:
    assert contains_pii(text)
