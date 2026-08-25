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
        print(f"watcher: could not reach Telegram: {e}", file=sys.stderr)
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


def main() -> int:
    gist_id = os.environ.get("GIST_ID", "")
    gist_token = os.environ.get("GIST_TOKEN", "")
    bot_token = os.environ.get("ALERT_BOT_TOKEN", "")
    chat_id = os.environ.get("ALERT_CHAT_ID", "")
    state_path = os.environ.get("STATE_FILE", "state.json")
    now = time.time()

    if not gist_id:
        print("watcher: GIST_ID is not set", file=sys.stderr)
        return 2

    previous = load_state(state_path)

    # A gist that cannot be read is itself a kind of silence, but it is our silence rather
    # than the server's - saying so plainly beats reporting a power cut that never was.
    try:
        stamp, verdict = parse_beat(read_beat(gist_id, gist_token))
    except Exception as e:
        print(f"watcher: cannot read the heartbeat: {e}", file=sys.stderr)
        return 1

    age = max(0.0, now - stamp)
    current = classify(age, verdict)
    changed = should_announce(previous.get("status", OK), current)

    print(f"watcher: last beat {age / 60:.1f} min ago, verdict '{verdict}', state '{current}'")

    if changed and bot_token and chat_id:
        send_telegram(bot_token, chat_id, message(current, age / 60))

    keepalive = previous.get("keepalive", 0)
    writing = should_commit(changed, now - keepalive)
    if writing:
        save_state(state_path, {
            "status": current,
            "changed_at": int(now) if changed else previous.get("changed_at", int(now)),
            "keepalive": int(now),
        })

    # Read by the workflow to decide whether there is anything to commit.
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"commit={'true' if writing else 'false'}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
