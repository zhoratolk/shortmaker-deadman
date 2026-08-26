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


class TestReadingTheGist:
    @staticmethod
    def _response(files):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"files": files}).encode()

        return FakeResponse()

    def test_a_gist_without_the_beat_file_raises_beat_missing(self, monkeypatch):
        """The absence has to be distinguishable from not reaching GitHub at all: only
        the server can remove the file, so only one of the two may page anybody."""
        monkeypatch.setattr(watcher.urllib.request, "urlopen",
                            lambda *a, **kw: self._response({}))

        with pytest.raises(watcher.BeatMissing):
            watcher.read_beat("abc")

    def test_a_gist_with_the_beat_file_returns_its_contents(self, monkeypatch):
        monkeypatch.setattr(
            watcher.urllib.request, "urlopen",
            lambda *a, **kw: self._response({"beat.txt": {"content": "1700000000 ok"}}))

        assert watcher.read_beat("abc") == "1700000000 ok"


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


class TestAnAgeNobodyCanKnow:
    def test_the_down_message_admits_it_cannot_know(self):
        """Quoting '0 мин назад' about a machine nothing was heard from contradicts the
        alarm's own headline, and a self-contradicting alarm teaches its reader to hunt
        for tricks instead of acting."""
        text = watcher.message(watcher.DOWN, None)

        assert "неизвестен" in text
        assert "0 мин" not in text


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


class TestTroubleThatEndedUnheard:
    """A problem whose alarm never left - Telegram was down - and that was already over
    by the next run. The ordinary 'all clear' would confirm a system the person never
    saw fail, and the outage - a reboot, a brownout - becomes invisible."""

    def test_recovery_after_an_undelivered_alarm_reports_the_outage(self, monkeypatch):
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        watcher.save_state(state, {"status": watcher.DOWN,
                                   "changed_at": int(time.time()) - 47 * 60,
                                   "keepalive": time.time(),
                                   "announced": False})
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("ALERT_BOT_TOKEN", "1:tok")
        monkeypatch.setenv("ALERT_CHAT_ID", "42")
        monkeypatch.setattr(watcher, "read_beat",
                            lambda *a, **kw: f"{int(time.time())} ok")
        sent = []
        monkeypatch.setattr(watcher, "send_telegram",
                            lambda token, chat, text: sent.append(text) or True)

        watcher.main()

        assert len(sent) == 1
        assert "47" in sent[0]
        assert "снова на связи" not in sent[0]
        saved = watcher.load_state(state)
        assert saved["status"] == watcher.OK
        assert saved["announced"] is True

    def test_a_recovery_notice_that_did_not_get_through_is_retried_as_one(self, monkeypatch):
        """Remembered as OK, the next run would find no trouble left to report and send
        the plain all-clear - and the outage nobody heard about would be gone for good."""
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        watcher.save_state(state, {"status": watcher.DOWN,
                                   "changed_at": int(time.time()) - 47 * 60,
                                   "keepalive": time.time(),
                                   "announced": False})
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("ALERT_BOT_TOKEN", "1:tok")
        monkeypatch.setenv("ALERT_CHAT_ID", "42")
        monkeypatch.setattr(watcher, "read_beat",
                            lambda *a, **kw: f"{int(time.time())} ok")

        sent = []

        def failing(token, chat, text):
            sent.append(text)
            return False

        monkeypatch.setattr(watcher, "send_telegram", failing)
        watcher.main()

        saved = watcher.load_state(state)
        assert saved["status"] == watcher.DOWN
        assert saved["announced"] is False

        monkeypatch.setattr(watcher, "send_telegram",
                            lambda token, chat, text: sent.append(text) or True)
        watcher.main()

        assert len(sent) == 2
        assert "снова на связи" not in sent[1]
        assert "прошла сама" in sent[1]
        assert watcher.load_state(state)["status"] == watcher.OK

    def test_recovery_after_a_delivered_alarm_is_the_usual_all_clear(self, monkeypatch):
        """Once the problem was reported, coming back needs only the short line."""
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        watcher.save_state(state, {"status": watcher.DOWN,
                                   "changed_at": int(time.time()) - 47 * 60,
                                   "keepalive": time.time(),
                                   "announced": True})
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("ALERT_BOT_TOKEN", "1:tok")
        monkeypatch.setenv("ALERT_CHAT_ID", "42")
        monkeypatch.setattr(watcher, "read_beat",
                            lambda *a, **kw: f"{int(time.time())} ok")
        sent = []
        monkeypatch.setattr(watcher, "send_telegram",
                            lambda token, chat, text: sent.append(text) or True)

        watcher.main()

        assert sent == ["✅ Сервер снова на связи."]

    def test_an_old_state_without_delivery_fields_takes_the_usual_path(self, monkeypatch):
        """States written before 'announced' existed say nothing about delivery; assuming
        delivered keeps them off the recovery path instead of crashing on it."""
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        watcher.save_state(state, {"status": watcher.DOWN, "keepalive": time.time()})
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("ALERT_BOT_TOKEN", "1:tok")
        monkeypatch.setenv("ALERT_CHAT_ID", "42")
        monkeypatch.setattr(watcher, "read_beat",
                            lambda *a, **kw: f"{int(time.time())} ok")
        sent = []
        monkeypatch.setattr(watcher, "send_telegram",
                            lambda token, chat, text: sent.append(text) or True)

        watcher.main()

        assert sent == ["✅ Сервер снова на связи."]

    def test_the_recovery_text_names_its_length(self):
        assert "12" in watcher.recovered_message(12.0)

    def test_the_recovery_text_survives_an_unknown_start(self):
        """A hand-edited or ancient state may carry no changed_at; the message still has
        to go out, minus any number it would only be guessing at."""
        text = watcher.recovered_message(None)

        assert "задним числом" in text
        assert "мин" not in text


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


class TestAVanishedHeartbeatFileRaisesTheAlarm:
    """Only the server can delete or empty the beat file, so its absence is its silence -
    the exact case this exists for. It used to exit 1 like a network failure and page
    nobody, forever."""

    def test_it_pages_instead_of_exiting_quietly(self, monkeypatch):
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("ALERT_BOT_TOKEN", "1:tok")
        monkeypatch.setenv("ALERT_CHAT_ID", "42")

        def no_file(*args, **kwargs):
            raise watcher.BeatMissing("the gist has no beat.txt")

        monkeypatch.setattr(watcher, "read_beat", no_file)
        sent = []
        monkeypatch.setattr(watcher, "send_telegram",
                            lambda token, chat, text: sent.append(text) or True)

        assert watcher.main() == 0
        assert len(sent) == 1
        assert watcher.load_state(state)["status"] == watcher.DOWN
        assert "0 мин" not in sent[0]
        assert "неизвестен" in sent[0]


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

    def test_a_blind_spell_stops_writing_once_it_has_been_reported(self, monkeypatch):
        """The count of blind runs has to be written down or it could never reach the
        threshold - but writing every run is a commit every fifteen minutes for as long
        as the fault lasts. It stops once the message is out."""
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        watcher.save_state(state, {"status": watcher.OK, "keepalive": time.time(),
                                   "announced": True,
                                   "blind": watcher.BLIND_RUNS_BEFORE_ALERT,
                                   "blind_told": True})
        output = os.path.join(directory, "gh-output")
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("GITHUB_OUTPUT", output)
        monkeypatch.setattr(watcher, "read_beat", _unreachable)

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

def _unreachable(*args, **kwargs):
    raise OSError("github is down")


class TestBlindnessIsOurOwnFailure:
    """A watchman that cannot see says so. Silence from a deadman switch is
    indistinguishable from good news, and the causes here - a deleted gist, an expired
    token, a rate limit shared with every other job on the runner - last until someone
    acts on them."""

    def _blind_run(self, monkeypatch, state, previous, sent):
        watcher.save_state(state, previous)
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setenv("ALERT_BOT_TOKEN", "1:tok")
        monkeypatch.setenv("ALERT_CHAT_ID", "42")
        monkeypatch.setattr(watcher, "read_beat", _unreachable)
        monkeypatch.setattr(watcher, "send_telegram",
                            lambda token, chat, text: sent.append(text) or True)
        watcher.main()
        return watcher.load_state(state)

    def test_a_single_failure_is_counted_but_not_announced(self, monkeypatch):
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        sent = []

        saved = self._blind_run(monkeypatch, state,
                                {"status": watcher.OK, "keepalive": time.time(),
                                 "announced": True}, sent)

        assert saved["blind"] == 1
        assert sent == []

    def test_the_span_is_measured_not_derived_from_the_schedule(self, monkeypatch):
        """The cron line asks for every fifteen minutes and GitHub delivers 30 to 160
        minutes apart, so a count of runs says nothing about how long this has lasted."""
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        sent = []
        started = time.time() - 200 * 60
        previous = {"status": watcher.OK, "keepalive": time.time(), "announced": True,
                    "blind": watcher.BLIND_RUNS_BEFORE_ALERT - 1,
                    "blind_since": started}

        self._blind_run(monkeypatch, state, previous, sent)

        assert len(sent) == 1
        assert "200 мин" in sent[0]

    def test_the_first_blind_run_starts_the_clock(self, monkeypatch):
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        sent = []

        saved = self._blind_run(monkeypatch, state,
                                {"status": watcher.OK, "keepalive": time.time(),
                                 "announced": True}, sent)

        assert saved["blind_since"] is not None
        assert abs(saved["blind_since"] - time.time()) < 60

    def test_an_hour_of_blindness_is_reported_once(self, monkeypatch):
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        sent = []
        previous = {"status": watcher.OK, "keepalive": time.time(), "announced": True,
                    "blind": watcher.BLIND_RUNS_BEFORE_ALERT - 1}

        saved = self._blind_run(monkeypatch, state, previous, sent)

        assert len(sent) == 1
        assert "не видит" in sent[0]
        assert saved["blind_told"] is True

        # The next failure must not repeat it: a message every quarter of an hour is how
        # a chat gets muted, and a muted chat is the same as no alarm at all. Nor is the
        # state rewritten - the count has done its job, and further writes would be a
        # commit every fifteen minutes for as long as the fault lasts.
        saved = self._blind_run(monkeypatch, state, saved, sent)
        assert len(sent) == 1
        assert saved["blind"] == watcher.BLIND_RUNS_BEFORE_ALERT

    def test_reading_the_gist_again_clears_the_spell(self, monkeypatch):
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        watcher.save_state(state, {"status": watcher.OK, "keepalive": 0,
                                   "announced": True, "blind": 9, "blind_told": True})
        monkeypatch.setenv("STATE_FILE", state)
        monkeypatch.setenv("GIST_ID", "abc")
        monkeypatch.setattr(watcher, "read_beat",
                            lambda *a, **kw: f"{int(time.time())} ok")

        watcher.main()

        saved = watcher.load_state(state)
        assert saved["blind"] == 0
        assert saved["blind_told"] is False
        assert saved["blind_since"] is None

    def test_the_alarm_about_the_server_is_not_lost_while_blind(self, monkeypatch):
        """The blind path owns nothing but its own counter: an undelivered alarm about
        the server has to survive it, or a fault on both sides at once loses the one
        that matters."""
        directory = tempfile.mkdtemp()
        state = os.path.join(directory, "state.json")
        sent = []
        previous = {"status": watcher.DOWN, "changed_at": int(time.time()) - 3600,
                    "keepalive": time.time(), "announced": False}

        saved = self._blind_run(monkeypatch, state, previous, sent)

        assert saved["status"] == watcher.DOWN
        assert saved["announced"] is False


class TestTheReasonIsNamed:
    def test_an_http_failure_carries_its_status(self):
        """A type name cannot tell a 403 rate limit from a DNS failure, and those call
        for opposite responses."""
        import urllib.error

        error = urllib.error.HTTPError("https://api.github.com/gists/x", 403,
                                       "rate limited", {}, None)
        assert watcher._why(error) == "HTTPError 403"

    def test_a_failure_without_one_is_named_by_type_alone(self):
        assert watcher._why(OSError("no route to host")) == "OSError"
