import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

from banks import BANKS

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
OVERRIDES_FILE = DATA_DIR / "overrides.json"
CUSTOM_BANKS_FILE = DATA_DIR / "custom_banks.json"
SECRET_KEY_FILE = DATA_DIR / "secret_key"

ADMIN_PASSWORD = os.environ.get("BONUS_ADMIN_PASSWORD")

BALANCE_STATUSES = ("required", "advisory", "not_required")

TEXT_FIELDS = (
    "name",
    "subtitle",
    "requirement",
    "monthly_fee",
    "exit_text",
    "cooldown_text",
    "balance_note",
)


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


def load_overrides():
    if OVERRIDES_FILE.exists():
        return json.loads(OVERRIDES_FILE.read_text())
    return {}


def save_overrides(overrides):
    OVERRIDES_FILE.write_text(json.dumps(overrides, indent=2))


def load_custom_banks():
    if CUSTOM_BANKS_FILE.exists():
        return json.loads(CUSTOM_BANKS_FILE.read_text())
    return []


def save_custom_banks(custom_banks):
    CUSTOM_BANKS_FILE.write_text(json.dumps(custom_banks, indent=2))


def base_banks():
    """The full catalog before per-field overrides: shipped banks.py entries
    plus any custom banks added via the admin UI."""
    return BANKS + load_custom_banks()


def effective_banks():
    """base_banks() with any saved per-field overrides layered on top."""
    overrides = load_overrides()
    out = []
    for b in base_banks():
        merged = dict(b)
        merged.update(overrides.get(b["id"], {}))
        out.append(merged)
    return out


def effective_banks_by_id():
    return {b["id"]: b for b in effective_banks()}


def custom_bank_ids():
    return {b["id"] for b in load_custom_banks()}


def is_admin():
    return bool(session.get("admin"))


def require_admin():
    if not is_admin():
        abort(403)


def today_iso():
    return date.today().isoformat()


def normalize_state(state, banks_by_id):
    """Flip any bank whose cooldown has elapsed back to available. Returns
    True if the state dict was changed (caller should persist it)."""
    changed = False
    for bank_id, st in state.items():
        bank = banks_by_id.get(bank_id)
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

    return v


def compute_totals(views):
    totals = {"available": 0, "in_progress": 0, "received": 0}
    for v in views:
        if v["paid"]:
            totals["received"] += v["bonus"]
        elif v["status"] == "in_progress":
            totals["in_progress"] += v["bonus"]
        elif v["status"] == "available":
            totals["available"] += v["bonus"]
    return totals


def is_ajax():
    return request.headers.get("X-Requested-With") == "fetch"


def ajax_response(bank_id):
    """After a tracking action, hand back just the affected card's HTML plus
    fresh totals, so the page can patch in place instead of reloading."""
    banks = effective_banks()
    banks_by_id = {b["id"]: b for b in banks}
    state = load_state()
    if normalize_state(state, banks_by_id):
        save_state(state)
    views = [bank_view(b, state) for b in banks]
    totals = compute_totals(views)
    bank_view_data = next(v for v in views if v["id"] == bank_id)
    card_html = render_template(
        "_card_fragment.html", b=bank_view_data, is_admin=is_admin(), today=today_iso()
    )
    return jsonify(card_html=card_html, totals=totals)


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
    banks = effective_banks()
    banks_by_id = {b["id"]: b for b in banks}
    state = load_state()
    if normalize_state(state, banks_by_id):
        save_state(state)

    views = [bank_view(b, state) for b in banks]
    views.sort(key=lambda v: -v["bonus"])
    totals = compute_totals(views)

    return render_template(
        "index.html",
        banks=views,
        totals=totals,
        is_admin=is_admin(),
        today=today_iso(),
    )


@app.route("/bonus/edit")
def edit_page():
    if not is_admin():
        return redirect(url_for("login"))
    banks = effective_banks()
    banks.sort(key=lambda b: b["name"].lower())
    overridden_ids = set(load_overrides().keys())
    return render_template(
        "edit.html", banks=banks, custom_ids=custom_bank_ids(), overridden_ids=overridden_ids
    )


@app.route("/bonus/api/start/<bank_id>", methods=["POST"])
def start_bank(bank_id):
    require_admin()
    bank = effective_banks_by_id().get(bank_id)
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
    return ajax_response(bank_id) if is_ajax() else redirect(url_for("index"))


@app.route("/bonus/api/started-date/<bank_id>", methods=["POST"])
def set_started_date(bank_id):
    require_admin()
    bank = effective_banks_by_id().get(bank_id)
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
    return ajax_response(bank_id) if is_ajax() else redirect(url_for("index"))


@app.route("/bonus/api/check/<bank_id>/<int:index>", methods=["POST"])
def toggle_check(bank_id, index):
    require_admin()
    bank = effective_banks_by_id().get(bank_id)
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
    return ajax_response(bank_id) if is_ajax() else redirect(url_for("index"))


@app.route("/bonus/api/paid/<bank_id>", methods=["POST"])
def toggle_paid(bank_id):
    require_admin()
    bank = effective_banks_by_id().get(bank_id)
    if not bank:
        abort(404)
    state = load_state()
    st = state.get(bank_id)
    if st and st.get("status") == "in_progress":
        st["paid"] = not st.get("paid", False)
        save_state(state)
    return ajax_response(bank_id) if is_ajax() else redirect(url_for("index"))


@app.route("/bonus/api/close/<bank_id>", methods=["POST"])
def close_bank(bank_id):
    require_admin()
    bank = effective_banks_by_id().get(bank_id)
    if not bank:
        abort(404)
    state = load_state()
    st = state.get(bank_id)
    if st and st.get("status") == "in_progress":
        st["status"] = "closed"
        st["closed_date"] = today_iso()
        save_state(state)
    return ajax_response(bank_id) if is_ajax() else redirect(url_for("index"))


@app.route("/bonus/api/abandon/<bank_id>", methods=["POST"])
def abandon_bank(bank_id):
    """Stop tracking a bank that was started by mistake — resets it straight
    back to available with no cooldown, unlike closing it for real."""
    require_admin()
    if bank_id not in effective_banks_by_id():
        abort(404)
    state = load_state()
    if bank_id in state and state[bank_id].get("status") == "in_progress":
        del state[bank_id]
        save_state(state)
    return ajax_response(bank_id) if is_ajax() else redirect(url_for("index"))


@app.route("/bonus/api/edit/<bank_id>", methods=["POST"])
def edit_bank(bank_id):
    require_admin()
    if bank_id not in {b["id"] for b in base_banks()}:
        abort(404)

    overrides = load_overrides()
    entry = {}

    for field in TEXT_FIELDS:
        value = request.form.get(field, "").strip()
        if value:
            entry[field] = value

    balance_status = request.form.get("balance_status", "")
    if balance_status in BALANCE_STATUSES:
        entry["balance_status"] = balance_status

    for field in ("bonus", "hold_days"):
        value = request.form.get(field, "").strip()
        if value:
            try:
                entry[field] = int(value)
            except ValueError:
                abort(400)

    cooldown_days = request.form.get("cooldown_days", "").strip()
    if cooldown_days.lower() in ("never", "none"):
        entry["cooldown_days"] = None
    elif cooldown_days:
        try:
            entry["cooldown_days"] = int(cooldown_days)
        except ValueError:
            abort(400)

    checklist_raw = request.form.get("checklist", "")
    checklist_items = [line.strip() for line in checklist_raw.splitlines() if line.strip()]
    if checklist_items:
        entry["checklist"] = checklist_items

    overrides[bank_id] = entry
    save_overrides(overrides)
    return redirect(url_for("edit_page", _anchor=bank_id))


@app.route("/bonus/api/add", methods=["POST"])
def add_bank():
    require_admin()
    name = request.form.get("name", "").strip()
    if not name:
        abort(400)

    existing_ids = {b["id"] for b in base_banks()}
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "bank"
    candidate = slug
    n = 2
    while candidate in existing_ids:
        candidate = f"{slug}-{n}"
        n += 1

    def int_field(field, default):
        value = request.form.get(field, "").strip()
        try:
            return int(value)
        except ValueError:
            return default

    balance_status = request.form.get("balance_status", "")
    if balance_status not in BALANCE_STATUSES:
        balance_status = "not_required"

    checklist_raw = request.form.get("checklist", "")
    checklist_items = [line.strip() for line in checklist_raw.splitlines() if line.strip()]
    if not checklist_items:
        checklist_items = ["Complete the bonus requirements"]

    cooldown_days_raw = request.form.get("cooldown_days", "").strip()
    if cooldown_days_raw.lower() in ("never", "none"):
        cooldown_days = None
    else:
        try:
            cooldown_days = int(cooldown_days_raw)
        except ValueError:
            cooldown_days = 365

    new_bank = {
        "id": candidate,
        "name": name,
        "subtitle": request.form.get("subtitle", "").strip(),
        "bonus": int_field("bonus", 0),
        "requirement": request.form.get("requirement", "").strip(),
        "checklist": checklist_items,
        "monthly_fee": request.form.get("monthly_fee", "").strip(),
        "exit_text": request.form.get("exit_text", "").strip(),
        "cooldown_text": request.form.get("cooldown_text", "").strip(),
        "balance_status": balance_status,
        "balance_note": request.form.get("balance_note", "").strip(),
        "hold_days": int_field("hold_days", 90),
        "cooldown_days": cooldown_days,
    }

    custom_banks = load_custom_banks()
    custom_banks.append(new_bank)
    save_custom_banks(custom_banks)
    return redirect(url_for("edit_page", _anchor=candidate))


@app.route("/bonus/api/delete/<bank_id>", methods=["POST"])
def delete_bank(bank_id):
    require_admin()
    custom_banks = load_custom_banks()
    filtered = [b for b in custom_banks if b["id"] != bank_id]
    if len(filtered) == len(custom_banks):
        abort(404)  # only custom (admin-added) banks can be deleted
    save_custom_banks(filtered)

    overrides = load_overrides()
    if bank_id in overrides:
        del overrides[bank_id]
        save_overrides(overrides)

    state = load_state()
    if bank_id in state:
        del state[bank_id]
        save_state(state)

    return redirect(url_for("edit_page"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8935)
