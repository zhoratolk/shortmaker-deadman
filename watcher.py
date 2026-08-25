"""Notices when the shortmaker server stops saying it is alive.

Nothing running on that machine can report that the machine died - a dead process sends
nothing, and silence looks exactly like health. So the machine writes the time into a gist
every minute, and this runs somewhere else on a schedule and looks at how old that
timestamp is. If it stopped moving, the power went out, the network went out, or the
watchdog itself died, and all three deserve the same phone call.

This lives here rather than on a monitoring service because every one of those wanted an
account confirmed by an email that never arrived. What the job actually needs is a
computer that is not in the same room, and a scheduled workflow is one.

Standard library only: this runs on a bare runner with nothing installed.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

# The file the watchdog writes, and the format it writes: "<epoch seconds> ok|fail".
BEAT_FILE = "beat.txt"

# The server beats once a minute. This has to clear the gap between two of these runs plus
# a reboot, or a machine that came back before anyone noticed would still raise an alarm.
SILENCE_SECONDS = 30 * 60

# GitHub disables scheduled workflows in a repository nobody has touched for 60 days. A
# commit well inside that window keeps this from quietly retiring - the failure mode being
# a watchman who stopped showing up without telling anyone.
KEEPALIVE_SECONDS = 21 * 24 * 3600

DOWN = "down"
UNHEALTHY = "unhealthy"
OK = "ok"


def setting(name: str) -> str:
    """A secret's value, without the whitespace that pasting brings along.

    Secrets are typed into a web form, and one saved with a trailing newline builds a URL
    urllib refuses outright: "URL can't contain control characters". Found on the first
    real alarm this ever raised - it correctly noticed the server was gone, and then could
    not say so, which is the one failure that makes the whole thing pointless.
    """
    return os.environ.get(name, "").strip()


def parse_beat(text: str) -> tuple[int, str]:
    """The timestamp and verdict the server last wrote.

    A malformed or empty file is treated as no beat at all rather than as a fresh one:
    the whole point is to fail towards raising the alarm.
    """
    parts = text.strip().split()
    if not parts:
        raise ValueError("the heartbeat file is empty")
    stamp = int(parts[0])
    verdict = parts[1] if len(parts) > 1 else OK
    return stamp, verdict


def read_beat(gist_id: str, token: str = "") -> str:
    """The heartbeat file's current contents.

    A secret gist is readable by anyone holding its id, so no token is needed here. One is
    accepted anyway for the case where the gist is made private later.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "shortmaker-deadman",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}", headers=headers
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode())

    files = payload.get("files") or {}
    entry = files.get(BEAT_FILE)
    if not entry:
        raise ValueError(f"the gist has no {BEAT_FILE}")
    return entry.get("content") or ""


def classify(beat_age: float, verdict: str, silence: float = SILENCE_SECONDS) -> str:
    """What state the server is in, from the age of its last beat and what it said.

    Age wins over the verdict: a stale "ok" is a machine that was fine at the moment it
    stopped existing, which is the case this whole thing was built for.
    """
    if beat_age > silence:
        return DOWN
    return OK if verdict == OK else UNHEALTHY


def message(state: str, minutes_quiet: float) -> str:
    if state == DOWN:
        return (
            "🚨 Сервер молчит.\n"
            f"Последний сигнал {minutes_quiet:.0f} мин назад.\n"
            "Похоже на отключение света, сети или зависание машины — "
            "сам он об этом сообщить не может."
        )
    if state == UNHEALTHY:
        return (
            "⚠️ Сервер жив, но сообщает о проблеме.\n"
            "Подробности должны были прийти от сторожа на самом сервере — "
            "если их нет, у него не работает связь с Telegram."
        )
    return "✅ Сервер снова на связи."


def should_announce(previous: str, current: str) -> bool:
    """Alerts fire on change.

    A message every fifteen minutes for a problem already reported teaches you to mute the
    chat, and a muted chat is the same as having no alarm at all.
    """
    return previous != current


def should_commit(state_changed: bool, keepalive_age: float,
                  window: float = KEEPALIVE_SECONDS) -> bool:
    return state_changed or keepalive_age > window


def send_telegram(token: str, chat_id: str, text: str) -> bool:
    body = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "disable_web_page_preview": "true",
    }).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=body
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status == 200
    except Exception as e:
        # The exception type only, never its message: a malformed URL is reported by
        # quoting the whole URL back, and the whole URL contains the bot token. Actions
        # masks known secrets in its own log, but stderr is copied into artifacts and
        # pasted into chats where nothing masks anything.
        print(f"watcher: could not reach Telegram: {type(e).__name__}", file=sys.stderr)
        return False


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"status": OK, "keepalive": 0}


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def _record(state_path: str, status: str, changed_at: int, now: float,
            announced: bool) -> None:
    save_state(state_path, {
        "status": status,
        "changed_at": changed_at,
        "keepalive": int(now),
        # Whether the person was actually told. A state remembered as announced when the
        # message never left means the next run sees no change and says nothing - the
        # outage goes unreported for as long as it lasts.
        "announced": announced,
    })


def _keepalive_only(state_path: str, previous: dict, now: float) -> bool:
    """Touches the file just to prove the repository is still alive, if it is due.

    Called on the paths that end early. Without it a fault that lasts - a deleted gist, an
    expired token, a rate limit - stops every commit, and sixty quiet days later GitHub
    disables the schedule. The watchman would stop showing up precisely because something
    was wrong, which is the failure this whole file exists to prevent.
    """
    if now - previous.get("keepalive", 0) <= KEEPALIVE_SECONDS:
        return False
    _record(state_path, previous.get("status", OK), previous.get("changed_at", int(now)),
            now, previous.get("announced", True))
    return True


def _tell_workflow(writing: bool) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"commit={'true' if writing else 'false'}\n")


def main() -> int:
    gist_id = setting("GIST_ID")
    gist_token = setting("GIST_TOKEN")
    bot_token = setting("ALERT_BOT_TOKEN")
    chat_id = setting("ALERT_CHAT_ID")
    state_path = os.environ.get("STATE_FILE", "state.json")
    now = time.time()

    if not gist_id:
        print("watcher: GIST_ID is not set", file=sys.stderr)
        _tell_workflow(False)
        return 2

    previous = load_state(state_path)

    # Reading the gist and reading what is in it are different failures. Not reaching
    # GitHub is our silence, and claiming a power cut on it would be a lie. A file we did
    # read whose contents are nonsense is the server's silence: it died mid-write, or its
    # disk filled - exactly the case this was built for, so it counts as down.
    try:
        raw = read_beat(gist_id, gist_token)
    except Exception as e:
        print(f"watcher: cannot reach the heartbeat: {type(e).__name__}", file=sys.stderr)
        _tell_workflow(_keepalive_only(state_path, previous, now))
        return 1

    try:
        stamp, verdict = parse_beat(raw)
        age = max(0.0, now - stamp)
        current = classify(age, verdict)
    except ValueError as e:
        print(f"watcher: the heartbeat is unreadable, treating it as down: {e}",
              file=sys.stderr)
        stamp, verdict, age, current = 0, "unreadable", 0.0, DOWN

    was = previous.get("status", OK)
    # An alarm that was never delivered is not an alarm. Re-announcing the same state is
    # right here, unlike the fifteen-minute repeats this deliberately avoids: nobody has
    # heard this one yet.
    changed = should_announce(was, current) or not previous.get("announced", True)

    print(f"watcher: last beat {age / 60:.1f} min ago, verdict '{verdict}', state '{current}'")

    announced = True
    if changed:
        if bot_token and chat_id:
            announced = send_telegram(bot_token, chat_id, message(current, age / 60))
            if not announced:
                print("watcher: the alert did not go out, will try again next run",
                      file=sys.stderr)
        else:
            print("watcher: no ALERT_BOT_TOKEN/ALERT_CHAT_ID set, nobody was told",
                  file=sys.stderr)
            announced = False

    # An undelivered alert has to be retried, so it is written down every time rather than
    # only when the keepalive is due - otherwise the retry would wait three weeks.
    writing = should_commit(changed, now - previous.get("keepalive", 0)) or not announced
    if writing:
        _record(state_path, current,
                int(now) if current != was else previous.get("changed_at", int(now)),
                now, announced)

    _tell_workflow(writing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
