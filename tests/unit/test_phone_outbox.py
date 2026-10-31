"""UI-POLISH P4: the simulated phone outbox holds immutable, paired, bounded copies."""

import ast
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from corridor.ui.phone_outbox import MAX_MESSAGES, MAX_PAIRS, Outbox, SimulatedSMS, sms_ref

ROOT = Path(__file__).resolve().parents[2]


def _sms(index, code="123456"):
    return SimulatedSMS(
        ref=f"ref-{index}", kind="transfer", template="polish.sms.generic", code=code
    )


def test_pairs_are_isolated_and_unknown_pair_is_empty():
    outbox = Outbox()
    outbox.deliver("pair-a", _sms(1))
    assert [sms.ref for sms in outbox.inbox("pair-a")] == ["ref-1"]
    assert outbox.inbox("pair-b") == ()
    assert outbox.inbox("unknown") == ()
    assert not outbox.known("pair-b")


def test_delivery_is_idempotent_by_reference_and_copies_are_immutable():
    outbox = Outbox()
    first = outbox.deliver("pair", _sms(1, "111111"))
    again = outbox.deliver("pair", _sms(1, "222222"))
    assert again is first and len(outbox.inbox("pair")) == 1
    assert outbox.inbox("pair")[0].code == "111111"
    with pytest.raises(FrozenInstanceError):
        first.code = "000000"


def test_newest_first_capacity_and_pair_eviction():
    outbox = Outbox()
    for index in range(MAX_MESSAGES + 5):
        outbox.deliver("pair", _sms(index))
    refs = [sms.ref for sms in outbox.inbox("pair")]
    assert len(refs) == MAX_MESSAGES
    assert refs[0] == f"ref-{MAX_MESSAGES + 4}" and refs[-1] == "ref-5"
    for index in range(MAX_PAIRS + 3):
        outbox.deliver(f"p{index}", _sms(0))
    assert not outbox.known("pair") and not outbox.known("p0")
    assert outbox.known(f"p{MAX_PAIRS + 2}")


def test_forget_drops_a_known_pair_and_leaves_an_unknown_one_alone():
    outbox = Outbox()
    outbox.deliver("pair", _sms(1))
    outbox.register("empty")
    outbox.forget("pair")
    outbox.forget("never-registered")
    assert not outbox.known("pair") and outbox.inbox("pair") == ()
    assert outbox.known("empty") and outbox.inbox("empty") == ()


def test_a_forgotten_pair_no_longer_counts_against_max_pairs():
    outbox = Outbox()
    outbox.deliver("keeper", _sms(1))
    for index in range(MAX_PAIRS + 10):
        outbox.register(f"p{index}")
        outbox.forget(f"p{index}")
    assert outbox.known("keeper") and [sms.ref for sms in outbox.inbox("keeper")] == ["ref-1"]


def test_the_outbox_keeps_its_bound_until_told_otherwise():
    assert MAX_PAIRS == 64 and Outbox().max_pairs == MAX_PAIRS


def test_a_reserved_bound_holds_every_pair_and_only_ever_rises():
    outbox = Outbox()
    outbox.reserve(200)
    outbox.reserve(10)
    assert outbox.max_pairs == 200
    outbox.deliver("first", _sms(1, "135790"))
    for index in range(199):
        outbox.register(f"p{index}")
    assert outbox.known("first") and outbox.inbox("first")[0].code == "135790"
    outbox.register("one-more")
    assert not outbox.known("first") and outbox.known("p0")


def test_delivery_to_a_new_pair_keeps_to_the_reserved_bound():
    outbox = Outbox()
    outbox.reserve(MAX_PAIRS + 5)
    for index in range(MAX_PAIRS + 5):
        outbox.deliver(f"p{index}", _sms(0))
    assert outbox.known("p0")
    outbox.deliver("one-more", _sms(0))
    assert not outbox.known("p0") and outbox.known("p1")


def test_marked_copies_no_longer_expose_the_code():
    outbox = Outbox()
    outbox.deliver("pair", _sms(1, "654321"))
    outbox.deliver("pair", _sms(2, "765432"))
    outbox.mark("pair", "ref-1", "used")
    outbox.mark("pair", "ref-2", "expired")
    statuses = {sms.ref: (sms.status, sms.code) for sms in outbox.inbox("pair")}
    assert statuses == {"ref-1": ("used", ""), "ref-2": ("expired", "")}
    outbox.mark("pair", "ref-1", "active")
    assert outbox.inbox("pair")[1].status == "used"


def test_concurrent_deliver_and_read_do_not_fail():
    outbox = Outbox()
    errors = []

    def writer(offset):
        try:
            for index in range(200):
                outbox.deliver("pair", _sms(offset * 1000 + index))
        except Exception as error:  # pragma: no cover - failure path
            errors.append(error)

    def reader():
        try:
            for _ in range(400):
                assert len(outbox.inbox("pair")) <= MAX_MESSAGES
        except Exception as error:  # pragma: no cover - failure path
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(2)]
    threads.append(threading.Thread(target=reader))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors


def test_reference_does_not_store_the_challenge_id():
    challenge = "4f9c5f4e-1d7b-5d2a-9a3e-2f1c0b8d7e6a"
    ref = sms_ref(challenge)
    assert len(ref) == 16 and challenge not in ref


FORBIDDEN_CALLS = (
    "approve",
    "confirm",
    "request_mfa",
    "complete_mfa",
    "verify",
    "dispatch",
    "advance_clock",
    "propose",
    "submit_",
)


@pytest.mark.parametrize("name", ["phone.py", "phone_outbox.py"])
def test_phone_modules_never_call_approval_or_verifier_methods(name):
    tree = ast.parse((ROOT / "src/corridor/ui" / name).read_text(encoding="utf-8"))
    attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for attribute in attributes:
        assert not attribute.startswith(FORBIDDEN_CALLS), attribute
    read = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    verifier_private = {
        item
        for item in read
        if item.startswith("_") and item in {"_lock", "_challenges", "_pending", "_store_prepared"}
    }
    assert verifier_private <= {"_lock", "_challenges"}
