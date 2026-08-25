"""What the watcher must get right, tested without touching the network.

The alarm is only worth having if it fires when the machine dies and stays quiet when it
does not, so the thresholds and the state transitions are the whole subject here.
"""

import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watcher


class TestReadingWhatTheServerWrote:
    def test_a_normal_beat(self):
        assert watcher.parse_beat("1700000000 ok\n") == (1700000000, "ok")

    def test_a_beat_reporting_trouble(self):
        assert watcher.parse_beat("1700000000 fail\n") == (1700000000, "fail")

    def test_a_bare_timestamp_counts_as_healthy(self):
        """Older beats carried no verdict. Reading them as trouble would raise an alarm
        about the format rather than about the machine."""
        assert watcher.parse_beat("1700000000") == (1700000000, "ok")

    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_an_empty_file_is_not_a_fresh_beat(self, text):
        with pytest.raises(ValueError):
            watcher.parse_beat(text)

    def test_garbage_is_not_a_fresh_beat(self):
        with pytest.raises(ValueError):
            watcher.parse_beat("hello there")


class TestDecidingWhatIsGoingOn:
    def test_a_recent_healthy_beat_is_fine(self):
        assert watcher.classify(60, "ok") == watcher.OK

    def test_a_recent_beat_reporting_trouble(self):
        assert watcher.classify(60, "fail") == watcher.UNHEALTHY

    def test_an_old_beat_means_the_machine_is_gone(self):
        assert watcher.classify(31 * 60, "ok") == watcher.DOWN

    def test_age_outranks_the_verdict(self):
        """A stale 'ok' is a machine that was fine at the moment it stopped existing -
        which is the exact case this was built for."""
        assert watcher.classify(60 * 60, "ok") == watcher.DOWN

    def test_a_reboot_does_not_trip_the_alarm(self):
        """The server beats every minute and this runs every fifteen, so the window has
        to survive one missed run plus a restart, or every update would page us."""
        assert watcher.classify(20 * 60, "ok") == watcher.OK


class TestWhenItSpeaks:
    def test_a_change_is_announced(self):
        assert watcher.should_announce(watcher.OK, watcher.DOWN) is True

    def test_the_same_state_is_not_repeated(self):
        """Fifteen-minute reminders of a known problem train you to mute the chat, and a
        muted chat is the same as no alarm."""
        assert watcher.should_announce(watcher.DOWN, watcher.DOWN) is False

    def test_coming_back_is_announced(self):
        assert watcher.should_announce(watcher.DOWN, watcher.OK) is True

    def test_the_down_message_says_how_long(self):
        assert "47" in watcher.message(watcher.DOWN, 47.0)

    def test_the_unhealthy_message_distinguishes_itself(self):
        text = watcher.message(watcher.UNHEALTHY, 1.0)
        assert "жив" in text


class TestStayingEmployed:
    def test_a_state_change_is_written(self):
        assert watcher.should_commit(True, 0) is True

    def test_a_quiet_month_still_writes(self):
        """GitHub retires scheduled workflows in a repository nobody has touched for 60
        days. A watchman who silently stops showing up is worse than none."""
        assert watcher.should_commit(False, 22 * 24 * 3600) is True

    def test_a_quiet_week_writes_nothing(self):
        assert watcher.should_commit(False, 7 * 24 * 3600) is False


class TestTheStateFile:
    def test_a_missing_file_reads_as_healthy(self):
        directory = tempfile.mkdtemp()
        assert watcher.load_state(os.path.join(directory, "nope.json"))["status"] == "ok"

    def test_a_corrupt_file_reads_as_healthy(self):
        """A half-written file should not be the reason an alarm fires at 3am."""
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "state.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")

        assert watcher.load_state(path)["status"] == "ok"

    def test_a_round_trip(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "state.json")

        watcher.save_state(path, {"status": "down", "keepalive": 5})

        assert watcher.load_state(path) == {"status": "down", "keepalive": 5}
        with open(path, encoding="utf-8") as f:
            assert json.load(f)["status"] == "down"


class TestPastedSecrets:
    def test_a_trailing_newline_is_removed(self, monkeypatch):
        """Secrets are typed into a web form and saved with whatever came along. A token
        with a newline builds a URL urllib refuses outright - found on the first real
        alarm this raised, which noticed the server was gone and then could not say so."""
        monkeypatch.setenv("ALERT_BOT_TOKEN", "123:abc\n")

        assert watcher.setting("ALERT_BOT_TOKEN") == "123:abc"

    def test_surrounding_spaces_go_too(self, monkeypatch):
        monkeypatch.setenv("GIST_ID", "  deadbeef  ")

        assert watcher.setting("GIST_ID") == "deadbeef"

    def test_an_unset_value_is_empty(self, monkeypatch):
        monkeypatch.delenv("GIST_TOKEN", raising=False)

        assert watcher.setting("GIST_TOKEN") == ""


class TestAnUndeliveredAlarmIsNotAnAlarm:
    """The failure a review caught and a live test had already demonstrated: the state was
    written as announced whether or not the message left, so a transient delivery failure
    silenced the whole outage."""

    def test_a_failed_send_is_not_remembered_as_announced(self, monkeypatch):
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("ALERT_BOT_TOKEN", "1:tok")
        monkeypatch.setenv("ALERT_CHAT_ID", "42")
        monkeypatch.setattr(watcher, "read_beat", lambda *a, **kw: "1 ok")
        monkeypatch.setattr(watcher, "send_telegram", lambda *a, **kw: False)

        watcher.main()

        saved = watcher.load_state(state)
        assert saved["status"] == watcher.DOWN
        assert saved["announced"] is False

    def test_the_next_run_tries_again(self, monkeypatch):
        """Without this the outage is reported to nobody for as long as it lasts: the
        state already says 'down', so nothing has changed, so nothing is said."""
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        watcher.save_state(state, {"status": watcher.DOWN, "keepalive": time.time(),
                                   "announced": False})
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("ALERT_BOT_TOKEN", "1:tok")
        monkeypatch.setenv("ALERT_CHAT_ID", "42")
        monkeypatch.setattr(watcher, "read_beat", lambda *a, **kw: "1 ok")
        sent = []
        monkeypatch.setattr(watcher, "send_telegram",
                            lambda token, chat, text: sent.append(text) or True)

        watcher.main()

        assert len(sent) == 1
        assert watcher.load_state(state)["announced"] is True

    def test_a_delivered_alarm_is_not_repeated(self, monkeypatch):
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        watcher.save_state(state, {"status": watcher.DOWN, "keepalive": time.time(),
                                   "announced": True})
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("ALERT_BOT_TOKEN", "1:tok")
        monkeypatch.setenv("ALERT_CHAT_ID", "42")
        monkeypatch.setattr(watcher, "read_beat", lambda *a, **kw: "1 ok")
        sent = []
        monkeypatch.setattr(watcher, "send_telegram",
                            lambda token, chat, text: sent.append(text) or True)

        watcher.main()

        assert sent == []


class TestAGarbledHeartbeatRaisesTheAlarm:
    def test_nonsense_in_the_file_counts_as_down(self, monkeypatch):
        """The server died mid-write, or its disk filled. That is the case this exists
        for - and it used to end in a silent exit code."""
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("ALERT_BOT_TOKEN", "1:tok")
        monkeypatch.setenv("ALERT_CHAT_ID", "42")
        monkeypatch.setattr(watcher, "read_beat", lambda *a, **kw: "мусор")
        sent = []
        monkeypatch.setattr(watcher, "send_telegram",
                            lambda token, chat, text: sent.append(text) or True)

        assert watcher.main() == 0
        assert len(sent) == 1
        assert watcher.load_state(state)["status"] == watcher.DOWN


class TestTheWatchmanKeepsShowingUp:
    def test_a_long_outage_still_produces_a_keepalive_commit(self, monkeypatch):
        """GitHub disables a schedule after 60 quiet days. A fault that blocks every
        commit - a deleted gist, an expired token - would otherwise switch the alarm off
        precisely because something was wrong."""
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        watcher.save_state(state, {"status": watcher.OK,
                                   "keepalive": time.time() - 30 * 24 * 3600,
                                   "announced": True})
        output = os.path.join(directory, "gh-output")
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("GITHUB_OUTPUT", output)

        def unreachable(*args, **kwargs):
            raise OSError("github is down")

        monkeypatch.setattr(watcher, "read_beat", unreachable)

        assert watcher.main() == 1
        with open(output, encoding="utf-8") as f:
            assert "commit=true" in f.read()

    def test_a_short_outage_writes_nothing(self, monkeypatch):
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        watcher.save_state(state, {"status": watcher.OK, "keepalive": time.time(),
                                   "announced": True})
        output = os.path.join(directory, "gh-output")
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("GITHUB_OUTPUT", output)

        def unreachable(*args, **kwargs):
            raise OSError("github is down")

        monkeypatch.setattr(watcher, "read_beat", unreachable)

        watcher.main()

        with open(output, encoding="utf-8") as f:
            assert "commit=false" in f.read()


class TestTheTokenStaysOutOfTheLog:
    def test_only_the_exception_type_is_printed(self, monkeypatch, capsys):
        """A malformed URL is reported by quoting the URL back, and the URL carries the
        bot token."""
        def boom(*args, **kwargs):
            raise ValueError("URL can't contain control characters. '/bot123:SECRET/send'")

        monkeypatch.setattr(watcher.urllib.request, "urlopen", boom)

        assert watcher.send_telegram("123:SECRET", "42", "текст") is False
        assert "SECRET" not in capsys.readouterr().err
