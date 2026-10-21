"""Total and date extraction across AUD, USD and JPY receipts, and source reconciliation."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from paperless_field_extractor.extract import (
    Extraction,
    extract,
    reconcile_amount,
    reconcile_date,
    to_jsonable,
)
from paperless_field_extractor.lines import Line, lines_from_text
from paperless_field_extractor.reasons import Confidence, Corroboration, Reason


def _run(text: str, **options: object) -> Extraction:
    return extract(lines_from_text(text), **options)  # type: ignore[arg-type]


AUD_INVOICE = """ACME PLUMBING PTY LTD
ABN 12 345 678 901
TAX INVOICE
Date: 25/10/2026
Pipe fitting        $45.45
GST                  $4.55
Total               $50.00
"""

USD_RECEIPT = """Corner Cafe, Austin TX
10/25/2026
Subtotal   $9.25
Sales Tax  $0.75
Total     $10.00
"""

JPY_RECEIPT = """ローソン 渋谷店
2026年10月25日
小計 ¥1,000
消費税 ¥100
合計 ¥1,100
"""


class TestAud:
    def test_a_gst_invoice_is_high_confidence_with_its_corroboration(self) -> None:
        result = _run(AUD_INVOICE)

        assert result.amount is not None
        assert (result.amount.value, result.amount.currency) == (Decimal("50.00"), "AUD")
        assert result.amount.confidence is Confidence.HIGH
        assert Corroboration.TAX_INCLUDED in result.amount.corroboration
        assert result.amount_ready
        assert result.date is not None and result.date.value == "2026-10-25"
        assert result.date_ready

    def test_a_total_with_nothing_to_corroborate_it_is_only_medium(self) -> None:
        result = _run("ABN 12 345 678 901\n25/10/2026\nTotal $50.00")

        assert result.amount is not None
        assert result.amount.confidence is Confidence.MEDIUM
        assert Reason.NO_CORROBORATION in result.amount_reasons
        assert not result.amount_ready

    def test_a_total_that_appears_twice_is_corroborated(self) -> None:
        result = _run("ABN 12 345 678 901\nTotal $50.00\nEFTPOS $50.00")

        assert result.amount is not None
        assert result.amount.corroboration == (Corroboration.TOTAL_REPEATED,)
        assert result.amount.confidence is Confidence.HIGH

    def test_a_gst_line_that_does_not_match_the_total_is_not_corroboration(self) -> None:
        result = _run("ABN 12 345 678 901\nGST $9.99\nTotal $50.00")

        assert result.amount is not None
        assert Corroboration.TAX_INCLUDED not in result.amount.corroboration

    def test_the_gst_check_allows_one_cent_of_rounding(self) -> None:
        result = _run("ABN 1\nGST $4.54\nTotal $50.00")

        assert result.amount is not None
        assert Corroboration.TAX_INCLUDED in result.amount.corroboration

    def test_a_total_on_the_line_below_its_label_is_read(self) -> None:
        result = _run("ABN 1\nTOTAL\n$12.34\nGST 1.12")

        assert result.amount is not None
        assert result.amount.value == Decimal("12.34")

    def test_the_decimal_is_exact_and_never_a_float(self) -> None:
        result = _run("ABN 1\nTotal $1,234.56\nGST $112.23")

        assert result.amount is not None
        assert result.amount.value == Decimal("1234.56")
        assert isinstance(result.amount.value, Decimal)


class TestLabelHandling:
    @pytest.mark.parametrize(
        "text",
        [
            "Total items: 3\nTotal $50.00",
            "Total GST $4.55\nTotal $50.00",
            "GST total $4.55\nTotal $50.00",
            "Subtotal $45.45\nTotal $50.00",
            "Net total $45.45\nTotal $50.00",
            "Total savings $5.00\nTotal $50.00",
            "Total points 120\nTotal $50.00",
        ],
    )
    def test_a_total_that_is_not_the_receipt_total_is_ignored(self, text: str) -> None:
        result = _run("ABN 1\n" + text)

        assert result.amount is not None
        assert result.amount.value == Decimal("50.00")

    def test_total_outranks_balance(self) -> None:
        result = _run("ABN 1\nTotal $50.00\nBalance $10.00")

        assert result.amount is not None
        assert result.amount.value == Decimal("50.00")

    def test_amount_due_is_used_when_there_is_no_total(self) -> None:
        result = _run("ABN 1\nAmount due $75.00\nBalance $5.00")

        assert result.amount is not None
        assert result.amount.value == Decimal("75.00")

    def test_a_subtotal_alone_is_not_a_total(self) -> None:
        result = _run("ABN 1\nSubtotal $45.45")

        assert result.amount is None
        assert result.amount_reasons == (Reason.NO_AMOUNT,)

    def test_a_quantity_after_the_label_is_not_money(self) -> None:
        result = _run("ABN 1\nTotal 3 items\nTotal $50.00")

        assert result.amount is not None
        assert result.amount.value == Decimal("50.00")

    def test_a_percentage_is_not_an_amount(self) -> None:
        result = _run("ABN 1\nGST 10% $4.55\nTotal $50.00")

        assert result.amount is not None
        assert Corroboration.TAX_INCLUDED in result.amount.corroboration

    def test_phone_numbers_and_abns_are_not_totals(self) -> None:
        result = _run("ABN 12 345 678 901\nPh 02 9876 5432\nTotal $50.00")

        assert result.amount is not None
        assert result.amount.value == Decimal("50.00")

    def test_two_different_totals_conflict(self) -> None:
        result = _run("ABN 1\nTotal $50.00\nTotal $60.00")

        assert Reason.CONFLICTING_TOTALS in result.amount_reasons
        assert not result.amount_ready

    def test_a_zero_total_is_no_amount(self) -> None:
        assert _run("ABN 1\nTotal $0.00").amount is None

    def test_an_unlabelled_amount_is_low_confidence_and_never_ready(self) -> None:
        result = _run("ABN 1\nThanks for shopping\n$12.34")

        assert result.amount is not None
        assert result.amount.confidence is Confidence.LOW
        assert Reason.NO_TOTAL_LABEL in result.amount_reasons
        assert not result.amount_ready


class TestAmbiguity:
    @pytest.mark.parametrize(
        "text", ["Total 1.234,56", "Total 1234,56", "Total 1.234", "Total 12.3"]
    )
    def test_ambiguous_formats_are_withheld(self, text: str) -> None:
        result = _run("ABN 1\n" + text)

        assert result.amount is None
        assert result.amount_reasons == (Reason.AMBIGUOUS_AMOUNT,)

    def test_an_ambiguous_total_is_not_replaced_by_a_lower_priority_label(self) -> None:
        result = _run("ABN 1\nTotal 1.234,56\nBalance $9.00")

        assert result.amount is None
        assert result.amount_reasons == (Reason.AMBIGUOUS_AMOUNT,)

    def test_a_credit_note_is_flagged(self) -> None:
        result = _run("ABN 1\nCredit note\nTotal $30.00\nGST $2.73")

        assert Reason.CREDIT_NOTE in result.amount_reasons
        assert not result.amount_ready

    @pytest.mark.parametrize("total", ["-$30.00", "$-30.00", "($30.00)"])
    def test_a_negative_total_is_flagged(self, total: str) -> None:
        result = _run(f"ABN 1\nTotal {total}")

        assert Reason.NEGATIVE_AMOUNT in result.amount_reasons
        assert not result.amount_ready

    def test_a_low_word_confidence_on_the_total_line_blocks_the_value(self) -> None:
        lines = [Line("ABN 1", 95), Line("GST $4.55", 95), Line("Total $50.00", 41)]

        result = extract(lines)

        assert Reason.LOW_WORD_CONFIDENCE in result.amount_reasons
        assert not result.amount_ready

    def test_a_confident_total_line_is_accepted(self) -> None:
        lines = [Line("ABN 1", 95), Line("GST $4.55", 95), Line("Total $50.00", 88)]

        assert extract(lines).amount_ready


class TestUsd:
    def test_a_us_receipt_is_high_confidence_through_subtotal_plus_tax(self) -> None:
        result = _run(USD_RECEIPT)

        assert result.amount is not None
        assert (result.amount.value, result.amount.currency) == (Decimal("10.00"), "USD")
        assert result.amount.corroboration == (Corroboration.SUBTOTAL_PLUS_TAX,)
        assert result.amount_ready
        assert result.date is not None and result.date.value == "2026-10-25"

    def test_a_us_receipt_with_an_explicit_code_resolves_without_context(self) -> None:
        result = _run("Hotel\nOct 25, 2026\nSubtotal USD 90.00\nTax USD 9.00\nTotal USD 99.00")

        assert result.amount is not None
        assert (result.amount.value, result.amount.currency) == (Decimal("99.00"), "USD")
        assert result.amount_ready

    def test_a_bare_dollar_receipt_with_no_evidence_is_not_assumed_to_be_either(self) -> None:
        result = _run("Corner Cafe\n25/10/2026\nTotal $10.00")

        assert result.amount is not None
        assert result.amount.currency is None
        assert Reason.CURRENCY_AMBIGUOUS in result.amount_reasons
        assert not result.amount_ready

    def test_sales_tax_is_not_treated_as_inclusive_gst(self) -> None:
        result = _run("Sales Tax $0.91\nTotal $10.00 USD")

        assert result.amount is not None
        assert Corroboration.TAX_INCLUDED not in result.amount.corroboration

    def test_an_ambiguous_numeric_date_on_a_us_receipt_is_not_guessed(self) -> None:
        result = _run("Subtotal USD 9.00\nTax USD 1.00\nTotal USD 10.00\n03/04/2026")

        assert result.date is None
        assert result.date_reasons == (Reason.DATE_AMBIGUOUS,)

    def test_the_same_date_on_an_aud_receipt_reads_day_first(self) -> None:
        result = _run("ABN 1\nGST $0.91\nTotal $10.00\n03/04/2026")

        assert result.date is not None and result.date.value == "2026-04-03"


class TestJpy:
    def test_a_yen_receipt_has_no_decimals_and_checks_consumption_tax(self) -> None:
        result = _run(JPY_RECEIPT)

        assert result.amount is not None
        assert (result.amount.value, result.amount.currency) == (Decimal("1100"), "JPY")
        assert Corroboration.TAX_INCLUDED in result.amount.corroboration
        assert Corroboration.SUBTOTAL_PLUS_TAX in result.amount.corroboration
        assert result.amount_ready
        assert result.date is not None and result.date.value == "2026-10-25"

    def test_full_width_digits_and_yen_suffix_are_read(self) -> None:
        result = _run("２０２６/１０/２５\n合計 １，１００円\n内消費税(10%) 100円")

        assert result.amount is not None
        assert (result.amount.value, result.amount.currency) == (Decimal("1100"), "JPY")
        assert result.amount_ready
        assert result.date is not None and result.date.value == "2026-10-25"

    def test_the_reduced_consumption_tax_rate_is_recognised(self) -> None:
        result = _run("合計 ¥1,080\n消費税 ¥80")

        assert result.amount is not None
        assert Corroboration.TAX_INCLUDED in result.amount.corroboration

    def test_an_item_count_after_the_label_is_skipped(self) -> None:
        result = _run("合計 3点 ¥1,100")

        assert result.amount is not None
        assert result.amount.value == Decimal("1100")

    def test_a_decimal_yen_amount_is_ambiguous(self) -> None:
        result = _run("合計 ¥1,100.50")

        assert result.amount is None
        assert result.amount_reasons == (Reason.AMBIGUOUS_AMOUNT,)

    def test_a_reiwa_date_is_read(self) -> None:
        result = _run("令和8年10月25日\n合計 ¥1,100\n消費税 ¥100")

        assert result.date is not None and result.date.value == "2026-10-25"

    def test_the_amounts_own_marker_wins_over_a_reference_conversion_elsewhere(self) -> None:
        result = _run("合計 1,100円\n(参考 USD 7.30)")

        assert result.amount is not None
        assert (result.amount.value, result.amount.currency) == (Decimal("1100"), "JPY")

    def test_two_currencies_in_the_text_conflict_when_the_total_names_neither(self) -> None:
        result = _run("Total 1,100\nPaid in JPY\nRate USD 0.0067")

        assert Reason.CURRENCY_CONFLICT in result.amount_reasons
        assert not result.amount_ready


def test_an_unsupported_currency_is_detected_and_withheld() -> None:
    result = _run("Hotel\nTotal EUR 99.00")

    assert result.amount is not None
    assert result.amount.currency == "EUR"
    assert Reason.UNSUPPORTED_CURRENCY in result.amount_reasons
    assert not result.amount_ready


class TestDates:
    def test_no_date_is_reported_and_created_is_never_substituted(self) -> None:
        result = _run("ABN 1\nGST $4.55\nTotal $50.00")

        assert result.date is None
        assert result.date_reasons == (Reason.NO_DATE,)

    def test_the_email_header_date_is_a_medium_confidence_fallback(self) -> None:
        result = _run("ABN 1\nGST $4.55\nTotal $50.00", header_date="2026-10-24")

        assert result.date is not None
        assert (result.date.value, result.date.from_header) == ("2026-10-24", True)
        assert result.date.confidence is Confidence.MEDIUM
        assert not result.date_ready

    def test_text_dates_beat_the_header(self) -> None:
        result = _run("ABN 1\n25/10/2026\nTotal $50.00", header_date="2026-10-24")

        assert result.date is not None and result.date.value == "2026-10-25"

    def test_several_dates_lower_confidence_and_take_the_first(self) -> None:
        result = _run("ABN 1\n25/10/2026\nPrinted 30/10/2026\nTotal $50.00")

        assert result.date is not None
        assert result.date.value == "2026-10-25"
        assert result.date.confidence is Confidence.MEDIUM
        assert Reason.MULTIPLE_DATES in result.date_reasons


def test_extraction_is_deterministic() -> None:
    assert _run(AUD_INVOICE) == _run(AUD_INVOICE)


def test_the_json_view_carries_amounts_as_strings_and_round_trips() -> None:
    payload = to_jsonable(_run(AUD_INVOICE))

    assert payload["amount"]["value"] == "50.00"
    assert json.loads(json.dumps(payload)) == payload


class TestReconcile:
    def test_both_sources_agreeing_on_a_high_confidence_value_is_written(self) -> None:
        decision = reconcile_amount(_run(AUD_INVOICE), _run(AUD_INVOICE))

        assert decision.write
        assert (decision.value, decision.currency) == ("50.00", "AUD")
        assert decision.reasons == ()

    def test_sources_that_disagree_are_not_written(self) -> None:
        other = _run("ABN 1\nGST $5.45\nTotal $60.00")

        decision = reconcile_amount(_run(AUD_INVOICE), other)

        assert not decision.write
        assert Reason.SOURCE_DISAGREEMENT in decision.reasons

    def test_a_different_currency_is_a_disagreement(self) -> None:
        usd = _run("Subtotal USD 40.00\nTax USD 10.00\nTotal USD 50.00")

        assert not reconcile_amount(_run(AUD_INVOICE), usd).write

    def test_a_missing_source_is_not_written(self) -> None:
        decision = reconcile_amount(_run(AUD_INVOICE), _run("nothing useful"))

        assert not decision.write
        assert Reason.SOURCE_MISSING in decision.reasons
        assert decision.value == "50.00"

    def test_agreement_without_any_corroboration_is_not_written(self) -> None:
        weak = _run("ABN 1\nTotal $50.00")

        decision = reconcile_amount(weak, weak)

        assert not decision.write
        assert decision.reasons == (Reason.NO_CORROBORATION,)

    def test_one_corroborated_source_is_enough_when_both_agree(self) -> None:
        weak = _run("ABN 1\nTotal $50.00")

        assert reconcile_amount(_run(AUD_INVOICE), weak).write

    def test_agreement_does_not_override_a_hard_reason(self) -> None:
        credit = _run("ABN 1\nCredit note\nGST $2.73\nTotal $30.00")

        decision = reconcile_amount(credit, credit)

        assert not decision.write
        assert Reason.CREDIT_NOTE in decision.reasons

    def test_dates_that_agree_are_written(self) -> None:
        decision = reconcile_date(_run(AUD_INVOICE), _run(AUD_INVOICE))

        assert decision.write
        assert decision.value == "2026-10-25"

    def test_dates_that_disagree_are_not_written(self) -> None:
        other = _run("ABN 1\n26/10/2026\nTotal $50.00")

        decision = reconcile_date(_run(AUD_INVOICE), other)

        assert not decision.write
        assert Reason.SOURCE_DISAGREEMENT in decision.reasons

    def test_a_header_only_date_is_never_written(self) -> None:
        header = _run("ABN 1\nTotal $50.00", header_date="2026-10-25")

        assert not reconcile_date(header, header).write
