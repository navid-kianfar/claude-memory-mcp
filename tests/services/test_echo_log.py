"""Recognising our own writes coming back.

The whole reason this exists: asoode's socket layer applies no actor exclusion
(message-handler.service.ts:93-97) and the client model drops `userId`
(domain-event.listener.ts:131 sets it, the handler does not forward it), so a
listener cannot tell its own change from anyone else's by looking at the event.
"""

from memory_mcp.services.echo_log import EchoLog


class TestRecognisingOurOwnWrites:
    def test_a_noted_id_is_an_echo(self):
        log = EchoLog()
        log.note("t-1")
        assert log.is_echo({"t-1"}) is True

    def test_an_unknown_id_is_not(self):
        log = EchoLog()
        log.note("t-1")
        assert log.is_echo({"t-2"}) is False

    def test_one_unknown_id_is_enough_to_reconcile(self):
        """All-or-nothing on purpose: a change we did not make must win."""
        log = EchoLog()
        log.note("t-1")
        assert log.is_echo({"t-1", "t-2"}) is False

    def test_an_empty_set_is_never_an_echo(self):
        """An event we cannot attribute to a task gets reconciled. Being wrong
        the other way loses a change."""
        log = EchoLog()
        log.note("t-1")
        assert log.is_echo(set()) is False

    def test_none_and_empty_ids_are_ignored(self):
        log = EchoLog()
        log.note(None)
        log.note("")
        assert log.is_echo({""}) is False


class TestItDoesNotGrowForever:
    def test_entries_expire(self):
        log = EchoLog(window=0.0)
        log.note("t-1")
        assert log.is_echo({"t-1"}) is False

    def test_a_stale_entry_is_dropped_from_the_store(self):
        log = EchoLog(window=0.0)
        log.note("t-1")
        log.is_echo({"anything"})
        assert log._seen == {}

    def test_it_is_capped(self):
        log = EchoLog(max_entries=10)
        for i in range(50):
            log.note(f"t-{i}")
        assert len(log._seen) <= 10

    def test_the_cap_keeps_the_newest(self):
        log = EchoLog(max_entries=5)
        for i in range(20):
            log.note(f"t-{i}")
        assert log.is_echo({"t-19"}) is True


class TestItCounts:
    def test_suppressions_are_counted(self):
        log = EchoLog()
        log.note("t-1")
        log.is_echo({"t-1"})
        log.is_echo({"t-1"})
        log.is_echo({"other"})
        assert log.suppressed == 2
