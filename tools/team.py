#!/usr/bin/env python3
"""Шина общения команды Claude-чатов через git-репозиторий.

Только стандартная библиотека Python 3.8+.

Команды:
  send  FROM TO "тема" ["текст"]   отправить сообщение (TO = роль или all)
  inbox ROLE [--all]               входящие роли (по умолчанию только не закрытые)
  ack   ID ROLE                    подтвердить, что указание получено
  done  ID ROLE ["итог"]           отметить, что работа по сообщению сделана
  beat  ROLE "чем занят"           отметка «я жив» (heartbeat), раз в час минимум
  check [--notify]                 сторож: просрочки, нарушения, мусор
  clean [--apply]                  найти (и удалить) мусор в репозитории
  sync  ROLE ["комментарий"]       git pull --rebase + commit team/ + push
  hook                             короткая памятка для SessionStart-хука
"""
import argparse
import datetime as dt
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEAM = ROOT / "team"
MSGS = TEAM / "msgs"
STATUS = TEAM / "status"

ROLES = ["coordinator", "hardware", "research", "coder", "tester", "docs", "repo"]
SYSTEM_SENDERS = ["watchdog", "human"]

# Какие пути каждая роль имеет право менять (проверяется сторожем по коммитам).
# team/msgs/ и team/status/<роль>.md разрешены всем.
OWNERSHIP = {
    "coordinator": ["team/board.md", "team/roles/", "CLAUDE.md"],
    "hardware": ["hw/", "firmware/"],
    "research": ["research/"],
    "coder": ["src/", "config/", "firmware/"],
    "tester": ["tests/"],
    "docs": ["docs/", "README.md"],
    "repo": [""],  # хозяин репозитория: может всё (уборка, настройки, tools/)
}

HEARTBEAT_MAX_MIN = 70      # нет отметки дольше — чат «уснул»
ACK_MAX_MIN = 30            # указание не подтверждено дольше — «не получил»
DONE_MAX_HOURS = 24         # подтверждено, но не сделано дольше — «зависло»
BIG_FILE_MB = 5

JUNK_PATTERNS = [
    r"(^|/)__pycache__/", r"\.pyc$", r"(^|/)\.DS_Store$", r"(^|/)Thumbs\.db$",
    r"\.tmp$", r"\.bak$", r"~$", r"\.swp$", r"\.log$", r"(^|/)node_modules/",
    r"(^|/)\.pytest_cache/", r"(^|/)build/", r"(^|/)dist/", r"\.orig$", r"\.rej$",
]
SECRET_PATTERNS = [
    r"sk-ant-[A-Za-z0-9_-]{20,}", r"ghp_[A-Za-z0-9]{30,}", r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)(password|passwd|api_key|token)\s*[:=]\s*['\"][^'\"]{6,}['\"]",
]
COMMIT_PREFIX = re.compile(r"^\[(%s)\]" % "|".join(ROLES + SYSTEM_SENDERS))


# ---------- утилиты ----------

def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit("git %s: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout


def check_role(role, allow_system=False):
    ok = ROLES + (SYSTEM_SENDERS if allow_system else [])
    if role not in ok:
        sys.exit("Неизвестная роль %r. Допустимо: %s" % (role, ", ".join(ok)))


# ---------- сообщения ----------

def read_msg(path):
    head, _, body = path.read_text(encoding="utf-8").partition("\n---\n")
    meta = {}
    for line in head.splitlines():
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    meta["body"] = body.strip()
    meta["path"] = path
    meta["acked_by"] = [x for x in meta.get("acked_by", "").split(",") if x]
    return meta


def write_msg(m):
    keys = ["id", "from", "to", "subject", "created", "acked_by", "done", "result"]
    lines = []
    for k in keys:
        v = m.get(k, "")
        lines.append("%s: %s" % (k, ",".join(v) if isinstance(v, list) else v))
    m["path"].write_text("\n".join(lines) + "\n---\n" + m.get("body", "") + "\n", encoding="utf-8")


def recipients(m):
    if m["to"] == "all":
        return [r for r in ROLES if r != m["from"]]
    return [m["to"]]


def all_msgs():
    if not MSGS.exists():
        return []
    return [read_msg(p) for p in sorted(MSGS.glob("*.md"))]


def find_msg(msg_id):
    for m in all_msgs():
        if m["id"] == msg_id or m["id"].endswith(msg_id):
            return m
    sys.exit("Сообщение %s не найдено" % msg_id)


def send(frm, to, subject, body=""):
    MSGS.mkdir(parents=True, exist_ok=True)
    t = now()
    msg_id = "%s-%s-%s-%s" % (t.strftime("%Y%m%d-%H%M"), frm, to, secrets.token_hex(2))
    m = {"id": msg_id, "from": frm, "to": to, "subject": subject.replace("\n", " "),
         "created": iso(t), "acked_by": [], "done": "", "result": "",
         "body": body, "path": MSGS / (msg_id + ".md")}
    write_msg(m)
    return m


def cmd_send(a):
    check_role(a.frm, allow_system=True)
    if a.to != "all":
        check_role(a.to)
    m = send(a.frm, a.to, a.subject, a.body or "")
    print("Отправлено: %s" % m["id"])


def cmd_inbox(a):
    check_role(a.role)
    rows = []
    for m in all_msgs():
        if a.role not in recipients(m) or (m["done"] and not a.all):
            continue
        state = "СДЕЛАНО" if m["done"] else ("принято" if a.role in m["acked_by"] else "НОВОЕ")
        rows.append((state, m))
    if not rows:
        print("[%s] входящих нет" % a.role)
    for state, m in rows:
        print("%-8s %s  от %-11s %s" % (state, m["id"], m["from"], m["subject"]))
        if state == "НОВОЕ" and m["body"]:
            for line in m["body"].splitlines():
                print("         | " + line)


def cmd_ack(a):
    check_role(a.role)
    m = find_msg(a.id)
    if a.role not in recipients(m):
        sys.exit("%s не адресат сообщения %s" % (a.role, m["id"]))
    if a.role not in m["acked_by"]:
        m["acked_by"].append(a.role)
        write_msg(m)
    print("Принято: %s" % m["id"])


def cmd_done(a):
    check_role(a.role)
    m = find_msg(a.id)
    if a.role not in m["acked_by"]:
        m["acked_by"].append(a.role)
    m["done"] = iso(now())
    m["result"] = (a.result or "").replace("\n", " ")
    write_msg(m)
    print("Закрыто: %s" % m["id"])


# ---------- heartbeat ----------

def cmd_beat(a):
    check_role(a.role)
    STATUS.mkdir(parents=True, exist_ok=True)
    (STATUS / (a.role + ".md")).write_text(
        "last: %s\ndoing: %s\n" % (iso(now()), a.doing.replace("\n", " ")), encoding="utf-8")
    print("Отметка %s сохранена" % a.role)


def read_beat(role):
    p = STATUS / (role + ".md")
    if not p.exists():
        return None, ""
    meta = dict(l.split(": ", 1) for l in p.read_text(encoding="utf-8").splitlines() if ": " in l)
    return parse_iso(meta["last"]), meta.get("doing", "")


# ---------- сторож ----------

def tracked_files():
    return [f for f in git("ls-files").splitlines() if f]


def find_junk(files):
    junk, big = [], []
    for f in files:
        if any(re.search(p, f) for p in JUNK_PATTERNS):
            junk.append(f)
        p = ROOT / f
        if p.is_file() and p.stat().st_size > BIG_FILE_MB * 1024 * 1024:
            big.append(f)
    return junk, big


def find_secrets(files):
    hits = []
    for f in files:
        p = ROOT / f
        if not p.is_file() or p.stat().st_size > 1024 * 1024:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(re.search(pat, text) for pat in SECRET_PATTERNS):
            hits.append(f)
    return hits


def commit_violations(hours=24):
    out = git("log", "--since=%d hours ago" % hours, "--no-merges",
              "--name-only", "--format=@@%h %s", check=False)
    problems, sha, subj, role = [], None, "", None
    for line in out.splitlines() + ["@@"]:
        if line.startswith("@@"):
            sha, _, subj = line[2:].partition(" ")
            mt = COMMIT_PREFIX.match(subj)
            role = mt.group(1) if mt else None
            if sha and not mt:
                problems.append((None, "коммит %s без префикса [роль]: %s" % (sha, subj)))
            continue
        if not line or not role or role in SYSTEM_SENDERS:
            continue
        allowed = OWNERSHIP.get(role, []) + ["team/msgs/", "team/status/%s.md" % role]
        if not any(line == p or line.startswith(p) for p in allowed):
            problems.append((role, "коммит %s: %s изменил чужой файл %s" % (sha, role, line)))
    return problems


def cmd_check(a):
    t = now()
    alerts = []  # (кому, текст)

    for role in ROLES:
        last, doing = read_beat(role)
        if last is None:
            alerts.append((role, "%s ни разу не отметился (tools/team.py beat)" % role))
        elif (t - last).total_seconds() > HEARTBEAT_MAX_MIN * 60:
            mins = int((t - last).total_seconds() // 60)
            alerts.append((role, "%s молчит %d мин (последнее: %s)" % (role, mins, doing)))

    for m in all_msgs():
        if m["done"] or m["from"] == "watchdog":
            continue
        age = (t - parse_iso(m["created"])).total_seconds()
        for r in recipients(m):
            if r not in m["acked_by"] and age > ACK_MAX_MIN * 60:
                alerts.append((r, "%s не подтвердил получение %s «%s»" % (r, m["id"], m["subject"])))
        if m["to"] != "all" and m["acked_by"] and age > DONE_MAX_HOURS * 3600:
            alerts.append((m["to"], "%s висит больше %d ч: %s" % (m["to"], DONE_MAX_HOURS, m["id"])))

    for role, text in commit_violations():
        alerts.append((role, "НАРУШЕНИЕ: " + text))

    files = tracked_files()
    junk, big = find_junk(files)
    for f in junk:
        alerts.append(("repo", "мусор в репозитории: %s" % f))
    for f in big:
        alerts.append(("repo", "файл больше %d МБ: %s" % (BIG_FILE_MB, f)))
    for f in find_secrets(files):
        alerts.append(("repo", "ПОХОЖЕ НА СЕКРЕТ: %s" % f))

    if not alerts:
        print("Сторож: всё в порядке (%s)" % iso(t))
        return
    print("Сторож: %d проблем(ы) (%s)" % (len(alerts), iso(t)))
    for who, text in alerts:
        print(" - [%s] %s" % (who or "?", text))

    if a.notify:
        # Не спамим: на каждого адресата одно открытое сообщение сторожа, оно обновляется.
        open_msgs = {m["to"]: m for m in all_msgs() if m["from"] == "watchdog" and not m["done"]}
        by_role = {"coordinator": ["- [%s] %s" % (w or "?", x) for w, x in alerts]}
        for who, text in alerts:
            if who in ROLES and who != "coordinator":
                by_role.setdefault(who, []).append("- " + text)
        for who, lines in by_role.items():
            body = "Проверка %s. Исправь и закрой: tools/team.py done <id> %s\n\n%s" % (
                iso(t), who, "\n".join(lines))
            if who in open_msgs:
                open_msgs[who]["body"] = body
                write_msg(open_msgs[who])
            else:
                send("watchdog", who, "Сторож: есть проблемы", body)
        print("Уведомления записаны в team/msgs/ — не забудь sync")
    sys.exit(1)


# ---------- уборка ----------

def cmd_clean(a):
    files = tracked_files()
    junk, big = find_junk(files)
    untracked = [f for f in git("ls-files", "--others", "--exclude-standard").splitlines()
                 if any(re.search(p, f) for p in JUNK_PATTERNS)]
    old_msgs = [m for m in all_msgs() if m["done"]
                and (now() - parse_iso(m["done"])).days >= 7]
    if not (junk or untracked or old_msgs or big):
        print("Мусора нет")
        return
    for f in junk:
        print("мусор в git:        " + f)
    for f in untracked:
        print("мусор вне git:      " + f)
    for f in big:
        print("большой (руками!):  " + f)
    for m in old_msgs:
        print("старое сообщение:   " + m["path"].name)
    if not a.apply:
        print("\nЭто пробный прогон. Удалить: tools/team.py clean --apply")
        return
    for f in junk:
        git("rm", "-r", "--cached", "--quiet", f)
        (ROOT / f).unlink(missing_ok=True)
    for f in untracked:
        (ROOT / f).unlink(missing_ok=True)
    archive = TEAM / "archive"
    archive.mkdir(exist_ok=True)
    for m in old_msgs:
        m["path"].rename(archive / m["path"].name)
    print("Готово. Большие файлы не трогал — реши вручную (Git LFS или .gitignore).")


# ---------- git ----------

def cmd_sync(a):
    check_role(a.role, allow_system=True)
    branch = os.environ.get("TEAM_BRANCH", "main")
    git("add", "team/")
    if git("diff", "--cached", "--name-only").strip():
        git("commit", "-m", "[%s] %s" % (a.role, a.comment or "team: сообщения/статус"))
    git("pull", "--rebase", "--autostash", "origin", branch)
    for delay in (0, 2, 4, 8, 16):
        if delay:
            time.sleep(delay)
        if subprocess.run(["git", "push", "origin", "HEAD:" + branch], cwd=ROOT).returncode == 0:
            print("Синхронизировано с %s" % branch)
            return
    sys.exit("push не удался — проверь сеть/права")


def cmd_hook(a):
    role = os.environ.get("CLAUDE_ROLE", "")
    print("Команда «Удалённый телескоп». Правила — в CLAUDE.md, твоя роль — team/roles/<роль>.md.")
    if role in ROLES:
        print("Твоя роль: %s. Сначала: python3 tools/team.py inbox %s" % (role, role))
    else:
        print("Роль не задана (CLAUDE_ROLE). Возьми её из первого сообщения пользователя.")
    unacked = {}
    for m in all_msgs():
        if not m["done"]:
            for r in recipients(m):
                if r not in m["acked_by"]:
                    unacked[r] = unacked.get(r, 0) + 1
    if unacked:
        print("Неподтверждённые указания: " + ", ".join("%s=%d" % kv for kv in sorted(unacked.items())))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("send"); s.add_argument("frm"); s.add_argument("to")
    s.add_argument("subject"); s.add_argument("body", nargs="?"); s.set_defaults(f=cmd_send)
    s = sp.add_parser("inbox"); s.add_argument("role"); s.add_argument("--all", action="store_true")
    s.set_defaults(f=cmd_inbox)
    s = sp.add_parser("ack"); s.add_argument("id"); s.add_argument("role"); s.set_defaults(f=cmd_ack)
    s = sp.add_parser("done"); s.add_argument("id"); s.add_argument("role")
    s.add_argument("result", nargs="?"); s.set_defaults(f=cmd_done)
    s = sp.add_parser("beat"); s.add_argument("role"); s.add_argument("doing"); s.set_defaults(f=cmd_beat)
    s = sp.add_parser("check"); s.add_argument("--notify", action="store_true"); s.set_defaults(f=cmd_check)
    s = sp.add_parser("clean"); s.add_argument("--apply", action="store_true"); s.set_defaults(f=cmd_clean)
    s = sp.add_parser("sync"); s.add_argument("role"); s.add_argument("comment", nargs="?")
    s.set_defaults(f=cmd_sync)
    s = sp.add_parser("hook"); s.set_defaults(f=cmd_hook)

    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
