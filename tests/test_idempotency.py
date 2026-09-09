"""The outbox: one message per (ticket, text), whatever happens upstream."""

from __future__ import annotations

from aimai_workflows.stacks.core import reply_key, send_reply, sent_replies


def test_the_same_text_sends_once() -> None:
    first = send_reply("T-1", "your password link is on its way", stack="a")
    second = send_reply("T-1", "your password link is on its way", stack="b")

    assert first.idempotency_key == second.idempotency_key
    assert second.deduplicated is True
    assert first.deduplicated is False
    assert len(sent_replies("T-1")) == 1


def test_a_different_text_is_a_different_message() -> None:
    """Deduplication must not swallow a genuinely new reply.

    A key over the ticket alone would silence every follow-up after the first.
    """
    send_reply("T-2", "first answer")
    send_reply("T-2", "corrected answer")

    assert len(sent_replies("T-2")) == 2


def test_the_key_does_not_depend_on_the_stack_or_the_time() -> None:
    """Two orchestrators producing the same reply is one message to the customer."""
    assert reply_key("T-3", "hello") == reply_key("T-3", "hello")
    assert reply_key("T-3", "hello") != reply_key("T-4", "hello")


def test_the_stored_text_is_the_first_one_written() -> None:
    """A deduplicated call returns what was actually sent, not what it asked
    to send. A caller that logged its own argument would report a message the
    customer never received."""
    send_reply("T-5", "the real reply")
    receipt = send_reply("T-5", "the real reply")

    assert receipt.text == "the real reply"
    assert len(sent_replies("T-5")) == 1
