#!/usr/bin/env python3
"""
librus_checker.py — check the Librus (Synergia) messages inbox and notify on new messages.

Usage:
    python3 librus_checker.py                  # check inbox, notify on new messages
    python3 librus_checker.py --summarize ID   # fetch + AI-summarize message ID (marks it read)
    python3 librus_checker.py --list           # print current inbox and exit (no notifications)

Auth flow (cookie-based, same as the browser):
    1. GET  synergia.librus.pl/loguj/portalRodzina     -> Location to api.librus.pl OAuth page
    2. GET  api.librus.pl/OAuth/Authorization?...      -> sets OAuth session cookies
    3. POST api.librus.pl/OAuth/Authorization?client_id=46  (action=login&login=..&pass=..)
    4. Follow the returned goTo redirect chain         -> oauth_token cookie on synergia.librus.pl
    5. GET  synergia.librus.pl/wiadomosci3             -> bootstraps wiadomosci.librus.pl session
    6. GET  wiadomosci.librus.pl/api/inbox/messages    -> the actual inbox
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import os
import re
import shutil
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests

BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / ".state"
STATE_FILE = STATE_DIR / "seen_ids.json"

SYNERGIA = "https://synergia.librus.pl"
OAUTH = "https://api.librus.pl"
WIADOMOSCI = "https://wiadomosci.librus.pl"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 30
MAX_REDIRECTS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("librus")


class LibrusError(Exception):
    pass


class AuthError(LibrusError):
    pass


# ---------------------------------------------------------------- .env / config

def load_env(path: Path = BASE_DIR / ".env") -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip().strip('"').strip("'")
    # real environment variables override .env (e.g. `OLLAMA_MODEL=bielik ./...`)
    env.update({k: v for k, v in os.environ.items() if k in env})
    return env


def env_bool(env: dict, key: str, default: bool = False) -> bool:
    return env.get(key, str(default)).lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- auth

def login(session: requests.Session, login_name: str, password: str) -> None:
    r = session.get(f"{SYNERGIA}/loguj/portalRodzina", allow_redirects=False, timeout=TIMEOUT)
    location = r.headers.get("Location")
    if not location:
        raise AuthError(f"portalRodzina did not redirect (HTTP {r.status_code})")
    auth_url = urljoin(SYNERGIA, location)

    session.get(auth_url, allow_redirects=False, timeout=TIMEOUT)

    r = session.post(
        f"{OAUTH}/OAuth/Authorization?client_id=46",
        data={"action": "login", "login": login_name, "pass": password},
        headers={
            "Origin": OAUTH,
            "Referer": auth_url,
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
        allow_redirects=False,
        timeout=TIMEOUT,
    )
    if re.search(r"captcha|recaptcha|g-recaptcha|hcaptcha", r.text, re.I):
        raise AuthError("Librus asked for a captcha — log in manually in a browser first")
    try:
        payload = r.json()
    except ValueError:
        raise AuthError(f"unexpected login response (HTTP {r.status_code}): {r.text[:200]}")
    go_to = payload.get("goTo")
    if not go_to:
        raise AuthError(f"login rejected — check LIBRUS_LOGIN/LIBRUS_PASS ({payload})")

    url = urljoin(OAUTH, go_to)
    for _ in range(MAX_REDIRECTS):
        r = session.get(url, allow_redirects=False, timeout=TIMEOUT)
        location = r.headers.get("Location")
        if not location:
            break
        url = urljoin(url, location)

    if not any(c.name == "oauth_token" for c in session.cookies):
        raise AuthError("oauth_token cookie was not set — login flow failed")
    log.info("logged in to synergia.librus.pl")


def bootstrap_messages(session: requests.Session) -> None:
    """Set wiadomosci.librus.pl session cookies."""
    r = session.get(f"{SYNERGIA}/wiadomosci3", timeout=TIMEOUT)
    if "Brak dostępu" in r.text:
        raise LibrusError("this account has no access to the messages module")
    log.info("messages session bootstrapped")


# ---------------------------------------------------------------- api

def api_get(session: requests.Session, path: str, params: dict | None = None) -> requests.Response:
    """GET on wiadomosci.librus.pl with one session-refresh retry."""
    headers = {
        "Accept": "application/json",
        "Referer": f"{WIADOMOSCI}/nowy/inbox",
        "X-Requested-With": "XMLHttpRequest",
    }
    r = session.get(f"{WIADOMOSCI}{path}", params=params, headers=headers, timeout=TIMEOUT)
    if r.status_code in (401, 403):
        log.warning("messages session rejected (HTTP %s) — re-bootstrapping", r.status_code)
        bootstrap_messages(session)
        r = session.get(f"{WIADOMOSCI}{path}", params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def fetch_inbox(session: requests.Session, limit: int = 30) -> list[dict]:
    r = api_get(session, "/api/inbox/messages", params={"limit": limit})
    payload = r.json()
    # Most mailboxes return {"data": [...], "total": N}; at least one returns a bare array.
    items = payload.get("data", []) if isinstance(payload, dict) else payload
    return [m for m in items if isinstance(m, dict) and m.get("messageId")]


def fetch_message(session: requests.Session, message_id: str) -> dict:
    """Fetch a full message. NOTE: marks it as read, like opening it in the app."""
    r = api_get(session, f"/api/inbox/messages/{message_id}")
    return r.json().get("data", {})


def sender_of(item: dict) -> str:
    return item.get("senderName") or " ".join(
        filter(None, [item.get("senderFirstName"), item.get("senderLastName")])
    ) or "unknown"


def decode_preview(item: dict) -> str:
    """List 'content' is base64 UTF-8, possibly cut mid-character — ignore errors."""
    raw = item.get("content") or ""
    try:
        return base64.b64decode(raw).decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def decode_body(full_message: dict) -> str:
    """Single-message 'Message' is base64 XML: <Message><Content><![CDATA[html]]>."""
    raw = full_message.get("Message") or ""
    xml_text = base64.b64decode(raw).decode("utf-8", errors="ignore")
    m = re.search(r"<!\[CDATA\[(.*?)\]\]>", xml_text, re.S)
    body = m.group(1) if m else xml_text
    body = re.sub(r"(?i)<br\s*/?>", "\n", body)
    body = re.sub(r"(?i)</p>", "\n\n", body)
    body = re.sub(r"<[^>]+>", "", body)
    return html.unescape(body).strip()


# ---------------------------------------------------------------- state

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except ValueError:
            pass
    return {"seen": []}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    state["seen"] = state["seen"][-2000:]  # keep the file bounded
    STATE_FILE.write_text(json.dumps(state, indent=1))


# ---------------------------------------------------------------- notifications

def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def notify_macos(title: str, body: str) -> None:
    osascript = shutil.which("osascript") or "/usr/bin/osascript"
    script = (
        f"display notification {json.dumps(_one_line(body))} "
        f"with title {json.dumps(_one_line(title))} sound name \"Glass\""
    )
    subprocess.run([osascript, "-e", script], check=False)


def notify_ntfy(env: dict, title: str, body: str) -> None:
    server = env.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    topic = env.get("NTFY_TOPIC", "")
    if not topic:
        log.warning("NOTIFY_METHOD includes ntfy but NTFY_TOPIC is empty")
        return
    try:
        requests.post(
            f"{server}/{topic}",
            data=_one_line(body).encode("utf-8"),
            headers={"Title": _one_line(title), "Tags": "incoming_envelope"},
            timeout=15,
        ).raise_for_status()
    except requests.RequestException as e:
        log.error("ntfy notification failed: %s", e)


def notify_email(env: dict, title: str, body: str) -> None:
    host = env.get("SMTP_HOST", "smtp.gmail.com")
    port = int(env.get("SMTP_PORT", "465"))
    user, password = env.get("SMTP_USER", ""), env.get("SMTP_PASS", "")
    to_addr = env.get("EMAIL_TO", "")
    if not (user and password and to_addr):
        log.warning("NOTIFY_METHOD includes email but SMTP_USER/SMTP_PASS/EMAIL_TO not set")
        return
    msg = EmailMessage()
    msg["Subject"] = _one_line(title)
    msg["From"] = env.get("EMAIL_FROM", user)
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(user, password)
                s.send_message(msg)
    except Exception as e:
        log.error("email notification failed: %s", e)


def notify_telegram(env: dict, title: str, body: str) -> None:
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat_id):
        log.warning("NOTIFY_METHOD includes telegram but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{_one_line(title)}\n{_one_line(body)}"},
            timeout=15,
        ).raise_for_status()
    except requests.RequestException as e:
        log.error("telegram notification failed: %s", e)


def notify(env: dict, title: str, body: str) -> None:
    methods = {m.strip().lower() for m in env.get("NOTIFY_METHOD", "macos").split(",")}
    if "macos" in methods:
        notify_macos(title, body)
    if "ntfy" in methods:
        notify_ntfy(env, title, body)
    if "email" in methods:
        notify_email(env, title, body)
    if "telegram" in methods:
        notify_telegram(env, title, body)


# ---------------------------------------------------------------- AI (local or remote)

def llm_generate(env: dict, prompt: str, temperature: float = 0.3, system: str | None = None) -> str:
    provider = env.get("AI_PROVIDER", "basal").lower()
    if provider == "basal":
        raise LibrusError(
            "basal is a decision model, not a text generator — "
            "for summaries set AI_PROVIDER=ollama or openai"
        )
    if provider == "ollama":
        host = env.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        model = env.get("OLLAMA_MODEL", "llama3.1")
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system
        try:
            r = requests.post(f"{host}/api/generate", json=payload, timeout=180)
            r.raise_for_status()
        except requests.ConnectionError:
            raise LibrusError(
                f"cannot reach Ollama at {host} — is `ollama serve` running?"
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise LibrusError(f"model '{model}' not found — run `ollama pull {model}`")
            raise
        return r.json()["response"].strip()

    # openai / any OpenAI-compatible remote endpoint
    base = env.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = env.get("OPENAI_API_KEY", "")
    model = env.get("OPENAI_MODEL", "gpt-4o-mini")
    if not key:
        raise LibrusError("OPENAI_API_KEY is empty — set it in .env for remote AI")
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": messages, "temperature": temperature},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def summarize(env: dict, text: str) -> str:
    prompt = (
        "Streść poniższą wiadomość ze szkolnego dziennika Librus w 2-3 zdaniach po polsku. "
        "Wymień nadawcę, temat i wszystkie wymagane działania/terminy.\n\n" + text
    )
    return llm_generate(env, prompt)


def _sender_matches(sender: str, patterns: str) -> bool:
    return any(p.strip().lower() in sender.lower() for p in patterns.split(",") if p.strip())


def apply_sender_rules(env: dict, sender: str, result: dict) -> dict:
    """Deterministic rules the model can't override."""
    if _sender_matches(sender, env.get("IGNORED_SENDERS", "")):
        result.update(notify=False, action_required=False, priority="low",
                      reason=f"nadawca na liście ignorowanych ({sender})", action="nic")
    elif _sender_matches(sender, env.get("IMPORTANT_SENDERS", "")):
        result["notify"] = True
        if result["priority"] == "low":
            result["priority"] = "medium"
    return result


# basal is a calibrated typed-decision model served by `basal-serve` (POST /v1/systemone).
# One request = one forward pass; every option gets a calibrated probability.
_BASAL_KINDS = {
    "action": ("wymaga działania rodzica (przynieść, podpisać, opłacić, potwierdzić, przygotować)", True, True),
    "important": ("ważna informacja od szkoły, bez wymaganej akcji", True, False),
    "info": ("informacja ogólna, biuletyn lub podsumowanie", False, False),
    "auto": ("automatyczne potwierdzenie", False, False),
}

_BASAL_ACTIONS = {
    "przyniesc": "przynieść coś do szkoły",
    "podpisac": "podpisać / odesłać dokument",
    "oplacic": "dokonać płatności",
    "potwierdzic": "potwierdzić / zgłosić (obecność, zgodę, udział)",
    "przygotowac": "przygotować dziecko (strój, sprzęt, wyżywienie)",
    "usprawiedliwic": "usprawiedliwić nieobecność",
    "inne": "sprawdź treść i zareaguj",
}


def analyze_basal(env: dict, item: dict, content: str) -> dict:
    host = env.get("BASAL_HOST", "http://localhost:8000").rstrip("/")
    state = (f"Nadawca: {sender_of(item)}\nData: {item.get('sendDate', '')}\n"
             f"Temat: {item.get('topic', '')}\nTreść: {content}")
    try:
        r = requests.post(f"{host}/v1/systemone", json={
            "state": state,
            "questions": {
                "kind": {
                    "type": "choice",
                    "instructions": "Jak zaklasyfikować tę wiadomość dla rodzica?",
                    "criteria": {k: v[0] for k, v in _BASAL_KINDS.items()},
                },
                "urgency": {
                    "type": "score",
                    "instructions": "Jak pilna jest ta sprawa dla rodzica?",
                    "criteria": ["niska", "średnia", "wysoka"],
                },
                "action_type": {
                    "type": "choice",
                    "instructions": "Jakiego działania wymaga ta wiadomość od rodzica? "
                                    "Jeśli żadnego — wybierz 'inne'.",
                    "criteria": {
                        "przyniesc": "przynieść przedmiot do szkoły (blok, podręcznik, strój, materiały)",
                        "podpisac": "podpisać lub odesłać dokument, kartę, oświadczenie",
                        "oplacic": "zapłacić, wpłacić składkę",
                        "potwierdzic": "potwierdzić obecność, zgodę, udział, odpisać ankietę",
                        "przygotowac": "przygotować dziecko (strój, sprzęt, prowiant) na wydarzenie",
                        "usprawiedliwic": "usprawiedliwić nieobecność dziecka",
                        "inne": "wiadomość nie wymaga działania",
                    },
                },
            },
        }, timeout=60)
        r.raise_for_status()
    except requests.ConnectionError:
        raise LibrusError(
            f"cannot reach basal at {host} — start it: "
            "./basal-env/bin/basal-serve --model Remek/basal-2.0-mini-MLX-8bit --mode mlx --port 8000"
        )
    answers = r.json()["answers"]
    kind = answers["kind"]["choice"]
    kind_conf = answers["kind"]["confidence"]
    urgency = answers.get("urgency", {}).get("score", 0)  # expected level on the 0-2 scale
    label, notify, action_required = _BASAL_KINDS.get(kind, _BASAL_KINDS["info"])
    action_key = answers.get("action_type", {}).get("choice", "inne")
    if action_key == "inne":
        action_required = action_required and kind == "action"
    priority = "high" if urgency >= 1.0 else "medium" if urgency >= 0.4 else "low"
    if action_required and priority == "low":
        priority = "medium"
    action_text = _BASAL_ACTIONS.get(action_key, "sprawdź treść i zareaguj") if action_required else "nic"
    return {
        "notify": notify,
        "action_required": action_required,
        "priority": priority,
        "reason": f"[basal:{kind} {kind_conf:.0%}] {label}",
        "action": action_text,
        "raw": r.json(),
    }


def analyze(env: dict, item: dict, content: str) -> dict:
    """Triage one message: notify the parent? action required?"""
    sender = sender_of(item)
    if env.get("AI_PROVIDER", "basal").lower() == "basal":
        return apply_sender_rules(env, sender, analyze_basal(env, item, content))

    important = env.get("IMPORTANT_SENDERS", "")
    ignored = env.get("IGNORED_SENDERS", "")
    rules_hint = ""
    if important.strip():
        rules_hint += f"Nadawcy zawsze istotni (NOTIFY=yes): {important}.\n"
    if ignored.strip():
        rules_hint += f"Nadawcy nieistotni (NOTIFY=no): {ignored}.\n"

    prompt = f"""Oceń wiadomość ze szkolnego dziennika Librus i odpowiedz DOKŁADNIE w tym formacie (nic więcej):

NOTIFY: yes/no
ACTION_REQUIRED: yes/no
PRIORITY: high/medium/low
REASON: jedno zdanie po polsku — dlaczego
ACTION: jedno zdanie po polsku — co konkretnie zrobić, albo "nic"

Zasady:
- ACTION_REQUIRED=yes TYLKO gdy treść wyraźnie każe rodzicowi coś zrobić: przynieść,
  podpisać, opłacić, zgłosić, potwierdzić, przygotować dziecko. Sama informacja
  "do zapoznania się" NIE jest akcją — wtedy ACTION_REQUIRED=no i ACTION=nic.
- NOTIFY=yes gdy: jest akcja dla rodzica, pilny termin, zmiana organizacji dnia
  dziecka (opieka, basen, wycieczka), ważna informacja od wychowawcy o dziecku.
- NOTIFY=no gdy: biuletyn, porady, automatyczne potwierdzenie, podsumowanie
  po wydarzeniu, materiał informacyjny bez terminu i bez reakcji.

Przykłady:
- "proszę o przyniesienie bloku technicznego A3" -> NOTIFY=yes, ACTION_REQUIRED=yes, ACTION=przynieść blok A3
- "Zaakceptowane usprawiedliwienie" -> NOTIFY=no, ACTION_REQUIRED=no
- "zapraszamy do lektury biuletynu" -> NOTIFY=no, ACTION_REQUIRED=no
- "refleksje powycieczkowe" -> NOTIFY=no, ACTION_REQUIRED=no
- "15 października wyjście na basen, przygotować strój" -> NOTIFY=yes, ACTION_REQUIRED=yes
{rules_hint}
Nadawca: {sender}
Data: {item.get('sendDate', '')}
Temat: {item.get('topic', '')}
Treść: {content}
"""
    raw = llm_generate(env, prompt, temperature=0.1)

    def field(name: str, default: str = "?") -> str:
        m = re.search(rf"{name}\s*:\s*(.+)", raw, re.I)
        return m.group(1).strip() if m else default

    result = {
        "notify": field("NOTIFY").lower().startswith("y"),
        "action_required": field("ACTION_REQUIRED").lower().startswith("y"),
        "priority": field("PRIORITY").lower(),
        "reason": field("REASON", raw.strip()),
        "action": field("ACTION", ""),
        "raw": raw,
    }
    return apply_sender_rules(env, sender, result)


# ---------------------------------------------------------------- main

def cmd_check(session: requests.Session, env: dict) -> int:
    items = fetch_inbox(session)
    state = load_state()
    seen = set(state["seen"])

    if not STATE_FILE.exists() and env_bool(env, "SEED_ON_FIRST_RUN", True):
        state["seen"] = [m["messageId"] for m in items]
        save_state(state)
        log.info("first run — seeded %d existing messages, no notifications sent", len(items))
        return 0

    new_items = [m for m in items if m["messageId"] not in seen]
    if not new_items:
        log.info("no new messages (%d checked)", len(items))
        return 0

    summarize_on = env_bool(env, "SUMMARIZE_ENABLED", False)
    for m in reversed(new_items):  # oldest first
        title = f"Nowa wiadomość: {_one_line(m.get('topic', '(brak tematu)'))}"
        body = f"{sender_of(m)} • {m.get('sendDate', '')}"
        if summarize_on:
            try:
                full = fetch_message(session, m["messageId"])  # marks the message read
                body += "\n" + summarize(env, decode_body(full))
            except Exception as e:
                log.error("summary failed for message %s: %s", m["messageId"], e)
        log.info("NEW %s | %s | %s", m["messageId"], sender_of(m), m.get("topic"))
        notify(env, title, body)

    state["seen"] = list(seen | {m["messageId"] for m in new_items})
    save_state(state)
    log.info("%d new message(s) notified", len(new_items))
    return 0


def cmd_list(session: requests.Session) -> int:
    for m in fetch_inbox(session):
        unread = "UNREAD" if m.get("readDate") is None else "      "
        print(f"{unread} {m.get('sendDate', ''):<20} {sender_of(m):<25} {m.get('topic', '')}  [{m['messageId']}]")
    return 0


_SURNAME_RE = re.compile(
    r"\b[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ]*"
    r"(?:-[A-ZĄĆĘŁŃÓŚŹŻ][\wąćęłńóśźżĄĆĘŁŃÓŚŹŻ]*)*"
    r"(?:ski|ska|cki|cka|dzki|dzka|wicz|owicz|ewicz)\b"
)


def _redact(text: str) -> str:
    """Mask Polish-looking surnames and the first name right before them."""
    text = _SURNAME_RE.sub("***", text)
    return re.sub(r"\b[A-ZĄĆĘŁŃÓŚŹŻ][^\W_]*(?=\s+\*\*\*)", "***", text)


def cmd_analyze(session: requests.Session, env: dict, limit: int,
                full: bool = True, anon: bool = False) -> int:
    items = fetch_inbox(session, limit)
    if not items:
        log.info("inbox is empty")
        return 0
    if full:
        log.warning("full bodies fetched: analyzed messages will be marked READ in Librus")
    to_notify = 0
    anon_senders: dict = {}
    for i, m in enumerate(items, 1):
        content = decode_body(fetch_message(session, m["messageId"])) if full else decode_preview(m)
        try:
            a = analyze(env, m, content or "(brak podglądu treści)")
        except LibrusError as e:
            log.error("analysis failed: %s", e)
            return 1
        flag = "!!" if a["notify"] else "  "
        to_notify += a["notify"]
        if anon:
            s = sender_of(m)
            if s not in anon_senders:
                anon_senders[s] = f"Nadawca {chr(65 + len(anon_senders))}"
            disp = anon_senders[s]
            topic = _redact(_one_line(m.get("topic", "")))
            reason, action = _redact(a["reason"]), _redact(a["action"])
            msg_id = ""
        else:
            disp, topic = sender_of(m), _one_line(m.get("topic", ""))
            reason, action = a["reason"], a["action"]
            msg_id = f'  [{m["messageId"]}]'
        print(f'{flag} [{i}] {m.get("sendDate", "")}  {disp} — "{topic}"{msg_id}')
        print(f'      notify: {"YES" if a["notify"] else "no"}   '
              f'action_required: {"YES" if a["action_required"] else "no"}   priority: {a["priority"]}')
        print(f'      reason: {reason}')
        print(f'      action: {action}')
        print()
    print(f"== {to_notify}/{len(items)} message(s) would be notified ==")
    return 0


def cmd_summarize(session: requests.Session, env: dict, message_id: str) -> int:
    full = fetch_message(session, message_id)  # marks the message read
    text = decode_body(full)
    print("--- message ---")
    print(text)
    print("--- summary ---")
    print(summarize(env, text))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Librus inbox checker")
    parser.add_argument("--list", action="store_true", help="print inbox and exit")
    parser.add_argument("--summarize", metavar="ID", help="AI-summarize message ID (marks it read)")
    parser.add_argument("--analyze", nargs="?", const=5, type=int, metavar="N",
                        help="AI-triage last N inbox messages (default 5, full bodies — marks them read)")
    parser.add_argument("--preview", action="store_true",
                        help="with --analyze: use truncated list previews only (nothing marked read)")
    parser.add_argument("--anon", action="store_true",
                        help="with --analyze/--list: anonymize senders, topics and IDs (for screenshots)")
    args = parser.parse_args()

    env = load_env()
    login_name, password = env.get("LIBRUS_LOGIN", ""), env.get("LIBRUS_PASS", "")
    if not login_name or not password:
        log.error("LIBRUS_LOGIN / LIBRUS_PASS not set — fill in %s", BASE_DIR / ".env")
        return 2

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    try:
        login(session, login_name, password)
        bootstrap_messages(session)
        if args.list:
            return cmd_list(session)
        if args.analyze is not None:
            return cmd_analyze(session, env, args.analyze, not args.preview, args.anon)
        if args.summarize:
            return cmd_summarize(session, env, args.summarize)
        return cmd_check(session, env)
    except requests.RequestException as e:
        log.error("network error: %s", e)
        return 1
    except LibrusError as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
