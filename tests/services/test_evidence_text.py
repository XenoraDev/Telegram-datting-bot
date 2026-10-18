"""Advisory screenshot analysis on already-read text (no OCR, no database)."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from bot.services import evidence as ev
from bot.services.evidence import MISMATCH, OK, UNREADABLE, WARNING, Expected, analyze_text

NOW = datetime(2026, 10, 16, 6, 30, tzinfo=UTC)  # 12:00 in India
CREATED = NOW - timedelta(minutes=5)
EXP = Expected(
    amount_minor=9900,
    upi_id="BharatPe.9000000000@yesbank",
    payee_name="Shiv Traders",
    created_at=CREATED,
    now=NOW,
)

GOOD = """Payment Successful
₹99.00
Paid to Shiv Traders
BharatPe.9000000000@yesbank
UPI Ref No: 412345678901
16 Oct 2026, 11:58 AM
"""


def run(text, expected=EXP):
    return analyze_text(text, expected, "test")


def codes(a):
    return set(a.findings)


# ---------------------------------------------------------------- the good case


def test_a_consistent_screenshot_is_ok_and_fields_are_extracted():
    a = run(GOOD)
    assert a.verdict == OK and a.findings == ()
    assert a.amount_minor == 9900
    assert a.payee == "bharatpe.9000000000@yesbank"
    assert a.utr == "412345678901"
    assert a.paid_at == datetime(2026, 10, 16, 11, 58)
    assert a.success_marker is True
    assert a.engine == "test"


@pytest.mark.parametrize("symbol", ["₹", "Rs.", "Rs", "INR", "¥", "%", "?"])
def test_the_rupee_sign_and_its_usual_ocr_misreadings_all_count(symbol):
    assert run(GOOD.replace("₹", symbol)).verdict == OK


@pytest.mark.parametrize("amount", ["99", "99.00", "99.0"])
def test_amount_formats(amount):
    assert run(GOOD.replace("99.00", amount)).amount_minor == 9900


def test_thousands_separators():
    exp = Expected(129950, EXP.upi_id, EXP.payee_name, CREATED, NOW)
    assert run(GOOD.replace("₹99.00", "₹1,299.50"), exp).amount_minor == 129950


# ---------------------------------------------------------------- amount


def test_a_different_amount_is_a_mismatch():
    a = run(GOOD.replace("99.00", "49.00"))
    assert a.verdict == MISMATCH and "amount_mismatch" in codes(a)
    assert a.amount_minor == 4900


def test_the_order_amount_among_other_amounts_still_matches():
    a = run(GOOD + "Balance ₹1,204.55\nCashback ₹5\n")
    assert a.verdict == OK and a.amount_minor == 9900


def test_an_amount_that_cannot_be_read_is_a_warning_not_a_mismatch():
    a = run(GOOD.replace("₹99.00\n", ""))
    assert a.verdict == WARNING and codes(a) == {"amount_not_found"}


# ---------------------------------------------------------------- payee


def test_a_clearly_different_payee_is_a_mismatch():
    a = run(
        GOOD.replace("BharatPe.9000000000@yesbank", "someoneelse@paytm").replace(
            "Shiv Traders", "X"
        )
    )
    assert a.verdict == MISMATCH and "payee_mismatch" in codes(a)
    assert a.payee == "someoneelse@paytm"


def test_a_lookalike_payee_is_flagged_but_only_as_unclear():
    a = run(
        GOOD.replace("BharatPe.9000000000@yesbank", "BharatPe.9000000001@yesbank").replace(
            "Shiv Traders", "X"
        )
    )
    assert a.verdict == WARNING and "payee_unclear" in codes(a)


def test_the_payee_name_alone_is_enough_when_no_upi_id_is_printed():
    a = run(GOOD.replace("BharatPe.9000000000@yesbank\n", ""))
    assert a.verdict == OK and a.payee is None


def test_no_payee_at_all_is_a_warning():
    a = run(GOOD.replace("BharatPe.9000000000@yesbank\n", "").replace("Shiv Traders", "someone"))
    assert a.verdict == WARNING and "payee_not_found" in codes(a)


# ---------------------------------------------------------------- status and time


def test_a_failed_payment_screenshot_is_a_mismatch():
    a = run(GOOD.replace("Payment Successful", "Payment Failed"))
    assert a.verdict == MISMATCH and "payment_failed" in codes(a) and a.success_marker is False


def test_a_pending_payment_is_a_warning():
    a = run(GOOD.replace("Payment Successful", "Payment Pending"))
    assert a.verdict == WARNING and "payment_pending" in codes(a)


def test_neither_success_wording_nor_a_reference_is_a_warning():
    text = "₹99.00\nBharatPe.9000000000@yesbank\n16 Oct 2026, 11:58 AM\nsome other words here"
    a = run(text)
    assert a.verdict == WARNING and "success_not_shown" in codes(a)


def test_a_screenshot_older_than_the_order_is_a_warning():
    a = run(GOOD.replace("16 Oct 2026, 11:58 AM", "02 Oct 2026, 09:15 AM"))
    assert a.verdict == WARNING and "time_too_old" in codes(a)


def test_a_date_without_a_time_only_fails_when_clearly_older():
    assert "time_too_old" not in codes(run(GOOD.replace("16 Oct 2026, 11:58 AM", "16 Oct 2026")))
    assert "time_too_old" in codes(run(GOOD.replace("16 Oct 2026, 11:58 AM", "10 Oct 2026")))


def test_a_time_in_the_future_is_a_warning():
    a = run(GOOD.replace("16 Oct 2026, 11:58 AM", "20 Oct 2026, 11:58 AM"))
    assert a.verdict == WARNING and "time_in_future" in codes(a)


@pytest.mark.parametrize(
    "stamp",
    [
        "16/10/2026 11:58",
        "16-10-2026 11:58 AM",
        "Oct 16, 2026 11:58 AM",
        "16th October 2026, 11:58",
    ],
)
def test_common_date_formats(stamp):
    a = run(GOOD.replace("16 Oct 2026, 11:58 AM", stamp))
    assert a.paid_at == datetime(2026, 10, 16, 11, 58)
    assert a.verdict == OK


def test_pm_times_are_converted():
    a = run(GOOD.replace("11:58 AM", "03:05 PM"))
    assert a.paid_at == datetime(2026, 10, 16, 15, 5)


# ---------------------------------------------------------------- reference


def test_a_bare_twelve_digit_number_is_taken_as_the_reference():
    a = run(GOOD.replace("UPI Ref No: ", ""))
    assert a.utr == "412345678901"


def test_labelled_references():
    for label in ("UTR: 412345678901", "Transaction ID T2610161158123456", "Txn ID: 412345678901"):
        assert run(GOOD.replace("UPI Ref No: 412345678901", label)).utr is not None


def test_utr_check_compares_with_what_the_user_typed():
    assert ev.utr_check("412345678901", "412345678901") == "match"
    assert ev.utr_check("412345678901", "999999999999") == "mismatch"
    assert ev.utr_check(None, "412345678901") == "unknown"
    assert ev.utr_check("412345678901", None) == "unknown"


# ---------------------------------------------------------------- unreadable


@pytest.mark.parametrize("text", ["", "   \n\n", "a b", "!!!! ???? ----"])
def test_almost_no_text_is_unreadable(text):
    a = run(text)
    assert a.verdict == UNREADABLE and a.findings == ("no_text",)


def test_text_without_any_payment_information_is_unreadable():
    a = run("The quick brown fox jumps over the lazy dog and keeps running far away")
    assert a.verdict == UNREADABLE and a.findings == ("no_payment_info",)


def test_a_failure_helper_is_unreadable_with_its_reason():
    a = ev.failed_analysis("tesseract", "ocr_timeout")
    assert a.verdict == UNREADABLE and a.findings == ("ocr_timeout",)


# ---------------------------------------------------------------- robustness


@pytest.mark.parametrize(
    "junk",
    [
        "₹" * 5000,
        "9" * 100_000,
        "@" * 10_000,
        "a@" + "b" * 5000,
        "Rs. " + "9," * 5000,
        "31 Feb 2026 99:99 PM " * 50,
        "\x00\x01\x02 ₹99 \udcff".encode("utf-8", "replace").decode("utf-8", "replace"),
    ],
)
def test_hostile_text_never_raises(junk):
    assert run(junk).verdict in ev.VERDICTS


def test_large_text_is_analysed_quickly():
    start = time.perf_counter()
    run(("Paid to x@y ₹99 UPI Ref No: 12 Oct 2026 10:00 AM 1,2,3 " * 20000)[:500_000])
    assert time.perf_counter() - start < 5


def test_the_result_never_contains_the_raw_text():
    a = run(GOOD + "SECRET-LOOKING-WORDS-123")
    assert "SECRET" not in repr(a) and "Traders" not in repr(a)


# --- real Paytm screenshot regression (Re 1 was read as Rs 29)

PAYTM_NOW = datetime(2026, 10, 29, 10, 50, tzinfo=UTC)  # 16:20 in India
PAYTM_EXP = Expected(
    amount_minor=100,
    upi_id="BHARATPE.8F0I1R0D3A21265@fbpe",
    payee_name=None,
    created_at=PAYTM_NOW - timedelta(minutes=8),
    now=PAYTM_NOW,
)
PAYTM = """Payment Successful
{amount}
Paid to
{upi}
Transferred 29 Oct 2026, 04:15 PM
UPI Ref No: 616292057492
"""


def paytm(amount="₹1", upi="BHARATPE.8F0I1R0D3A21265@fbpe", expected=PAYTM_EXP, **kw):
    return run(PAYTM.format(amount=amount, upi=upi) + kw.get("extra", ""), expected)


@pytest.mark.parametrize("amount", ["₹1", "₹ 1", "₹1.00", "T1", "<1", "~1", "1", "%1", "Rs. 1"])
def test_re_1_is_read_as_re_1_whatever_the_rupee_sign_became(amount):
    a = paytm(amount)
    assert a.verdict == OK and a.findings == ()
    assert a.amount_minor == 100
    assert a.utr == "616292057492"


def test_the_date_on_the_receipt_is_never_taken_for_the_amount():
    # The headline amount is missing from the OCR text; "29 Oct" must not become Rs 29.
    a = paytm("")
    assert a.amount_minor is None
    assert "amount_mismatch" not in codes(a)
    assert "amount_not_found" in codes(a) and a.verdict == WARNING


def test_a_labelled_date_is_not_an_amount():
    a = run(
        "Money Transferred 29 Oct 2026 04:15 PM\nPaid to x@y\nUPI Ref No: 616292057492", PAYTM_EXP
    )
    assert a.amount_minor is None
    assert "amount_mismatch" not in codes(a)


def test_time_and_reference_numbers_are_not_amounts():
    a = paytm("", expected=PAYTM_EXP, extra="Paid at 04:15 PM\nOrder 1234567890123\n")
    assert a.amount_minor is None and a.verdict == WARNING


@pytest.mark.parametrize(
    "minor,text", [(9900, "₹99"), (19900, "₹199"), (29900, "₹299"), (100, "₹1")]
)
def test_common_plan_amounts_are_matched(minor, text):
    exp = Expected(minor, PAYTM_EXP.upi_id, None, PAYTM_EXP.created_at, PAYTM_NOW)
    a = paytm(text, expected=exp)
    assert a.verdict == OK and a.amount_minor == minor


def test_a_different_amount_is_a_mismatch_never_a_pass():
    a = paytm("₹29")
    assert a.verdict == MISMATCH and "amount_mismatch" in codes(a)
    assert a.amount_minor == 2900


@pytest.mark.parametrize("seen", ["799", "299", "21"])
def test_a_rupee_sign_read_as_a_digit_is_unclear_not_correct(seen):
    exp = Expected(
        9900 if seen == "799" else 100, PAYTM_EXP.upi_id, None, PAYTM_EXP.created_at, PAYTM_NOW
    )
    a = paytm(seen, expected=exp)
    assert a.verdict in (WARNING, MISMATCH) and a.verdict != OK


def test_two_different_amounts_and_none_equal_is_unclear():
    a = paytm("₹29\n₹30")
    assert a.verdict == WARNING and "amount_unclear" in codes(a)


def test_the_expected_amount_wins_over_an_offer_banner():
    assert paytm("₹1\nGet ₹29 cashback").verdict == OK


@pytest.mark.parametrize(
    "printed",
    [
        "BHARATPE.8F0I1R0D3A21265@fbpe",
        "bharatpe.8f0i1r0d3a21265@fbpe",
        "BharatPe.8f0i1r0d3a21265@FBPE",
    ],
)
def test_the_upi_id_is_compared_case_insensitively(printed):
    a = paytm(upi=printed)
    assert a.verdict == OK and a.payee == "bharatpe.8f0i1r0d3a21265@fbpe"


def test_a_misread_upi_id_is_only_unclear_and_a_different_one_is_a_mismatch():
    assert "payee_unclear" in codes(paytm(upi="BHARATPE.8FOI1LR0D3A21265@fbpe"))
    other = paytm(upi="somebody.else@okaxis")
    assert other.verdict == MISMATCH and "payee_mismatch" in codes(other)


def test_the_utr_stays_strict():
    a = paytm()
    assert ev.utr_check(a.utr, "616292057492") == "match"
    assert ev.utr_check(a.utr, "616292057493") == "mismatch"
    assert ev.utr_check(None, "616292057492") == "unknown"


def test_unreadable_text_is_unreadable():
    assert run("..", PAYTM_EXP).verdict == UNREADABLE
