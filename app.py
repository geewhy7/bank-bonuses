import json
import os
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, session, url_for

from banks import BANKS

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
SECRET_KEY_FILE = DATA_DIR / "secret_key"

BANKS_BY_ID = {b["id"]: b for b in BANKS}

ADMIN_PASSWORD = os.environ.get("BONUS_ADMIN_PASSWORD")


def get_secret_key():
    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_KEY_FILE.write_text(key)
    return key


app = Flask(__name__)
app.secret_key = get_secret_key()
app.permanent_session_lifetime = timedelta(days=365)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def is_admin():
    return bool(session.get("admin"))


def require_admin():
    if not is_admin():
        abort(403)


def today_iso():
    return date.today().isoformat()


def normalize_state(state):
    """Flip any bank whose cooldown has elapsed back to available. Returns
    True if the state dict was changed (caller should persist it)."""
    changed = False
    for bank_id, st in state.items():
        bank = BANKS_BY_ID.get(bank_id)
        if not bank or st.get("status") != "closed":
            continue
        cooldown_days = bank["cooldown_days"]
        closed_date = st.get("closed_date")
        if cooldown_days is None or not closed_date:
            continue
        eligible = date.fromisoformat(closed_date) + timedelta(days=cooldown_days)
        if date.today() >= eligible:
            st["status"] = "available"
            st["started"] = None
            st["checked"] = [False] * len(bank["checklist"])
            st["paid"] = False
            changed = True
    return changed


def bank_view(bank, state):
    v = dict(bank)
    st = state.get(bank["id"], {})
    status = st.get("status", "available")
    checked = st.get("checked") or [False] * len(bank["checklist"])
    if len(checked) != len(bank["checklist"]):
        checked = (checked + [False] * len(bank["checklist"]))[: len(bank["checklist"])]

    v["status"] = status
    v["started"] = st.get("started")
    v["checked"] = checked
    v["paid"] = bool(st.get("paid"))
    v["closed_date"] = st.get("closed_date")
    v["last_closed_date"] = st.get("last_closed_date")
    v["eligible_again"] = None
    v["safe_close_date"] = None
    v["can_close"] = False

    if status == "in_progress" and v["started"]:
        started = date.fromisoformat(v["started"])
        safe_close = started + timedelta(days=bank["hold_days"])
        v["safe_close_date"] = safe_close.isoformat()
        v["can_close"] = date.today() >= safe_close

    if status == "closed" and v["closed_date"]:
        if bank["cooldown_days"] is None:
            v["eligible_again"] = None
        else:
            eligible = date.fromisoformat(v["closed_date"]) + timedelta(days=bank["cooldown_days"])
            v["eligible_again"] = eligible.isoformat()

    # sort priority: in_progress first, then available, then closed/cooldown
    v["sort_priority"] = {"in_progress": 0, "available": 1, "closed": 2}.get(status, 1)
    return v


@app.route("/bonus/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if not ADMIN_PASSWORD:
            error = "Admin login isn't configured on this server (BONUS_ADMIN_PASSWORD unset)."
        elif secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD):
            session.permanent = True
            session["admin"] = True
            return redirect(url_for("index"))
        else:
            error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/bonus/logout", methods=["POST"])
def logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.route("/bonus")
@app.route("/bonus/")
def index():
    state = load_state()
    if normalize_state(state):
        save_state(state)

    views = [bank_view(b, state) for b in BANKS]
    views.sort(key=lambda v: (v["sort_priority"], -v["bonus"]))

    totals = {"available": 0, "in_progress": 0, "received": 0, "cooldown": 0}
    for v in views:
        if v["paid"]:
            totals["received"] += v["bonus"]
        elif v["status"] == "in_progress":
            totals["in_progress"] += v["bonus"]
        elif v["status"] == "available":
            totals["available"] += v["bonus"]
        elif v["status"] == "closed":
            totals["cooldown"] += v["bonus"]

    return render_template(
        "index.html",
        banks=views,
        totals=totals,
        is_admin=is_admin(),
        today=today_iso(),
    )


@app.route("/bonus/api/start/<bank_id>", methods=["POST"])
def start_bank(bank_id):
    require_admin()
    bank = BANKS_BY_ID.get(bank_id)
    if not bank:
        abort(404)
    state = load_state()
    state[bank_id] = {
        "status": "in_progress",
        "started": today_iso(),
        "checked": [False] * len(bank["checklist"]),
        "paid": False,
        "closed_date": None,
    }
    save_state(state)
    return redirect(url_for("index"))


@app.route("/bonus/api/started-date/<bank_id>", methods=["POST"])
def set_started_date(bank_id):
    require_admin()
    bank = BANKS_BY_ID.get(bank_id)
    if not bank:
        abort(404)
    new_date = request.form.get("started", "")
    try:
        datetime.strptime(new_date, "%Y-%m-%d")
    except ValueError:
        abort(400)
    state = load_state()
    st = state.get(bank_id)
    if st and st.get("status") == "in_progress":
        st["started"] = new_date
        save_state(state)
    return redirect(url_for("index"))


@app.route("/bonus/api/check/<bank_id>/<int:index>", methods=["POST"])
def toggle_check(bank_id, index):
    require_admin()
    bank = BANKS_BY_ID.get(bank_id)
    if not bank or index < 0 or index >= len(bank["checklist"]):
        abort(404)
    state = load_state()
    st = state.get(bank_id)
    if st and st.get("status") == "in_progress":
        checked = st.get("checked") or [False] * len(bank["checklist"])
        if len(checked) != len(bank["checklist"]):
            checked = (checked + [False] * len(bank["checklist"]))[: len(bank["checklist"])]
        checked[index] = not checked[index]
        st["checked"] = checked
        save_state(state)
    return redirect(url_for("index"))


@app.route("/bonus/api/paid/<bank_id>", methods=["POST"])
def toggle_paid(bank_id):
    require_admin()
    bank = BANKS_BY_ID.get(bank_id)
    if not bank:
        abort(404)
    state = load_state()
    st = state.get(bank_id)
    if st and st.get("status") == "in_progress":
        st["paid"] = not st.get("paid", False)
        save_state(state)
    return redirect(url_for("index"))


@app.route("/bonus/api/close/<bank_id>", methods=["POST"])
def close_bank(bank_id):
    require_admin()
    bank = BANKS_BY_ID.get(bank_id)
    if not bank:
        abort(404)
    state = load_state()
    st = state.get(bank_id)
    if st and st.get("status") == "in_progress":
        st["status"] = "closed"
        st["closed_date"] = today_iso()
        save_state(state)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8935)
