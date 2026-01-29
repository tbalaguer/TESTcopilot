from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from sqlalchemy import select, desc
from dotenv import load_dotenv
from urllib.parse import urlparse
from datetime import datetime

from config import SECRET_KEY
from db import get_db
from models import (
    User, Role, Kid, TaskTemplate, TaskInstance,
    InstanceStatus, PointsLedger, LedgerReason
)
from auth import hash_password, verify_password, login_required
from services import (
    kid_balance, months_covered, ensure_rent_policy,
    create_instance_from_template, move_instance, update_instance_details,
    approve_instance, reject_instance, collect_instance, refresh_pool,
    charge_rent_if_due, set_column_order
)

load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="templates")
# The following line of code is for dev/staging only! (app.config['TEMPLATES_AUTO_RELOAD'] = True )
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = SECRET_KEY


# -----------------------
# Template filters
# -----------------------
@app.template_filter('format_approved')
def format_approved(dt):
    """Format approved_at datetime as 'Approved: MM-DD-YYYY at h:mm AM/PM'"""
    if dt is None:
        return ""
    if isinstance(dt, str):
        # Try to parse if it's a string
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return dt
    # Use %-I for Unix/Linux (no leading zero), fallback to %I for Windows
    try:
        formatted = dt.strftime('%m-%d-%Y at %-I:%M %p')
    except ValueError:
        # Windows doesn't support %-I, use %I and strip leading zero manually
        formatted = dt.strftime('%m-%d-%Y at %I:%M %p')
        # Remove leading zero from hour if present
        parts = formatted.split(' at ')
        if len(parts) == 2:
            time_parts = parts[1].split(':')
            if time_parts[0].startswith('0'):
                time_parts[0] = time_parts[0][1:]
            parts[1] = ':'.join(time_parts)
            formatted = ' at '.join(parts)
    return f"Approved: {formatted}"


# -----------------------
# Helpers / session state
# -----------------------
def current_user(db):
    uid = session.get("user_id")
    if not uid:
        return None
    return db.get(User, int(uid))


def require_gamemaster(user):
    return bool(user and user.role == Role.gamemaster)


def is_gamemaster_unlocked() -> bool:
    return bool(session.get("gm_unlocked"))


def gm_guard_or_redirect():
    if not is_gamemaster_unlocked():
        return redirect(url_for("board"))
    return None


def redirect_back(fallback_endpoint: str = "board", **fallback_values):
    """
    Redirect to the page that submitted the form (referrer), falling back to a safe endpoint.
    Only allows same-host redirects to avoid open-redirect issues.
    """
    ref = request.referrer
    if ref:
        try:
            ref_url = urlparse(ref)
            # Allow relative or same-host absolute URLs only
            if (not ref_url.netloc) or (ref_url.netloc == request.host):
                return redirect(ref)
        except Exception:
            pass
    return redirect(url_for(fallback_endpoint, **fallback_values))


def get_acting_kid_from_request() -> int | None:
    """
    Prefer POSTed acting_kid (hidden input), otherwise allow querystring acting_kid.
    """
    raw = (request.form.get("acting_kid") or request.args.get("acting_kid") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def redirect_to_board_preserving_acting_kid(*, fallback_kid: int | None = None):
    """
    Deterministic redirect to board preserving the active player if possible.

    Priority:
    1) request.form['acting_kid'] (hidden input)
    2) request.args['acting_kid']
    3) fallback_kid (usually instance.assigned_kid_id)
    4) /board (default to first kid on load)
    """
    acting_kid = get_acting_kid_from_request() or fallback_kid
    if acting_kid:
        return redirect(url_for("board", acting_kid=acting_kid))
    return redirect(url_for("board"))


# ---------------
# Basic navigation
# ---------------
@app.get("/")
def home():
    return redirect(url_for("board"))


# -------------
# Auth endpoints
# -------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None, user=None)

    username = request.form.get("username", "")
    password = request.form.get("password", "")

    db = get_db()
    try:
        user = db.scalar(select(User).where(User.username == username))
        if not user or not verify_password(password, user.password_hash):
            return render_template("login.html", error="Invalid credentials.", user=None)
        if user.role != Role.gamemaster:
            return render_template("login.html", error="Gamemaster account required.", user=None)

        session["user_id"] = user.id
        session["gm_unlocked"] = False
        return redirect(url_for("board"))
    finally:
        db.close()


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------
# Gamemaster lock/unlock
# ---------------------
@app.post("/gamemaster/unlock")
@login_required
def gamemaster_unlock():
    db = get_db()
    try:
        user = current_user(db)
        if not require_gamemaster(user):
            return jsonify({"error": "Gamemaster account required"}), 403

        password = request.form.get("password", "")
        if not verify_password(password, user.password_hash):
            return jsonify({"error": "Incorrect password"}), 400

        session["gm_unlocked"] = True
        return jsonify({"ok": True})
    finally:
        db.close()


@app.post("/gamemaster/lock")
@login_required
def gamemaster_lock():
    session["gm_unlocked"] = False
    return redirect_back("board")


# ----------
# Seed helper
# ----------
@app.get("/seed")
def seed():
    db = get_db()
    try:
        admin = db.scalar(select(User).where(User.username == "admin"))
        if not admin:
            admin = User(username="admin", password_hash=hash_password("admin"), role=Role.gamemaster)
            db.add(admin)
            db.flush()

        for name, color in [("Alex", "#3b82f6"), ("Sam", "#22c55e")]:
            if not db.scalar(select(Kid).where(Kid.name == name)):
                db.add(Kid(name=name, color=color))
        db.commit()

        if not db.scalar(select(TaskTemplate).limit(1)):
            db.add_all([
                TaskTemplate(title="Make bed", default_points=5, help_text="Make your bed neatly.", sort_order=10, available=True),
                TaskTemplate(title="Feed the pet", default_points=8, help_text="Refill food and water.", sort_order=20, available=True),
                TaskTemplate(title="Tidy toys", default_points=6, help_text="Put toys back in their place.", sort_order=30, available=True),
                TaskTemplate(title="Clean something", default_points=10, help_text="Add details: what did you clean?", sort_order=40, available=True),
            ])
            db.commit()

        return jsonify({"ok": True, "login": "admin/admin"})
    finally:
        db.close()


# ----------------
# Main board routes
# ----------------
@app.get("/board")
@login_required
def board():
    db = get_db()
    try:
        user = current_user(db)
        if not require_gamemaster(user):
            return redirect(url_for("login"))

        acting_kid = request.args.get("acting_kid", type=int)

        kids = db.scalars(select(Kid).order_by(Kid.name)).all()
        balances = {k.id: kid_balance(db, k.id) for k in kids}
        if kids and acting_kid is None:
            acting_kid = kids[0].id

        pool = db.scalars(
            select(TaskTemplate)
            .where(TaskTemplate.available == True)  # noqa: E712
            .order_by(TaskTemplate.sort_order, TaskTemplate.id)
        ).all()

        doing_q = select(TaskInstance).where(
            TaskInstance.status == InstanceStatus.doing,
            TaskInstance.assigned_kid_id == acting_kid
        )
        review_q = select(TaskInstance).where(
            TaskInstance.status == InstanceStatus.review,
            TaskInstance.assigned_kid_id == acting_kid
        )
        done_q = select(TaskInstance).where(
            TaskInstance.status == InstanceStatus.done,
            TaskInstance.assigned_kid_id == acting_kid,
            TaskInstance.archived == False  # noqa: E712
        )

        doing = db.scalars(doing_q.order_by(TaskInstance.sort_order, TaskInstance.id)).all()
        review = db.scalars(review_q.order_by(TaskInstance.sort_order, TaskInstance.id)).all()
        done = db.scalars(done_q.order_by(desc(TaskInstance.approved_at).nullslast(), desc(TaskInstance.id))).all()

        return render_template(
            "board.html",
            user=user,
            gm_unlocked=is_gamemaster_unlocked(),
            kids=kids,
            balances=balances,
            acting_kid=acting_kid,
            pool=pool,
            doing=doing,
            review=review,
            done=done
        )
    finally:
        db.close()


@app.post("/pool/refresh")
@login_required
def pool_refresh():
    db = get_db()
    try:
        refresh_pool(db)
        db.commit()
        # Preserve acting_kid deterministically
        return redirect_to_board_preserving_acting_kid()
    finally:
        db.close()


# --------------------------
# Templates (predefined tasks)
# --------------------------
@app.post("/templates/create")
@login_required
def create_template():
    g = gm_guard_or_redirect()
    if g:
        return g

    db = get_db()
    try:
        title = request.form.get("title", "").strip()
        default_points = int(request.form.get("default_points", "1"))
        help_text = request.form.get("help_text", "")

        if not title:
            return redirect_back("board")

        db.add(TaskTemplate(title=title, default_points=default_points, help_text=help_text, available=True))
        db.commit()
        return redirect_back("board")
    finally:
        db.close()


@app.post("/templates/<int:template_id>/delete")
@login_required
def delete_template(template_id: int):
    g = gm_guard_or_redirect()
    if g:
        return g

    db = get_db()
    try:
        tmpl = db.get(TaskTemplate, template_id)
        if not tmpl:
            return redirect_back("board")

        any_inst = db.scalar(select(TaskInstance.id).where(TaskInstance.template_id == template_id).limit(1))
        if any_inst:
            return redirect_back("board")

        db.delete(tmpl)
        db.commit()
        return redirect_back("board")
    finally:
        db.close()


@app.post("/templates/<int:template_id>/instantiate")
@login_required
def instantiate_template(template_id: int):
    db = get_db()
    try:
        acting_raw = (request.form.get("acting_kid_id", "") or "").strip()
        target_status = request.form.get("target_status", "doing")

        if target_status != "doing":
            return jsonify({"error": "Templates can only be dropped into Doing."}), 400
        if not acting_raw or acting_raw.lower() == "none":
            return jsonify({"error": "No active player selected."}), 400

        acting_kid_id = int(acting_raw)

        inst = create_instance_from_template(db, template_id, acting_kid_id)
        db.commit()
        return jsonify({"ok": True, "instance_id": inst.id})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        db.close()


# ----------------
# Instance actions
# ----------------
@app.post("/instances/<int:instance_id>/move")
@login_required
def move_instance_route(instance_id: int):
    db = get_db()
    try:
        status = request.form.get("status", "")
        move_instance(db, instance_id, InstanceStatus(status))
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        db.close()


@app.post("/instances/<int:instance_id>/details")
@login_required
def details_route(instance_id: int):
    db = get_db()
    try:
        details = request.form.get("details", "")
        update_instance_details(db, instance_id, details)
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        db.close()


@app.post("/instances/<int:instance_id>/approve")
@login_required
def approve_route(instance_id: int):
    g = gm_guard_or_redirect()
    if g:
        return g

    db = get_db()
    try:
        # capture kid before any mutations
        inst = db.get(TaskInstance, instance_id)
        inst_kid = inst.assigned_kid_id if inst else None

        approve_instance(db, instance_id)
        db.commit()

        # Preserve acting_kid; if missing, fall back to instance kid
        return redirect_to_board_preserving_acting_kid(fallback_kid=inst_kid)
    finally:
        db.close()


@app.post("/instances/<int:instance_id>/reject")
@login_required
def reject_route(instance_id: int):
    g = gm_guard_or_redirect()
    if g:
        return g

    db = get_db()
    try:
        inst = db.get(TaskInstance, instance_id)
        inst_kid = inst.assigned_kid_id if inst else None

        reject_instance(db, instance_id)
        db.commit()

        return redirect_to_board_preserving_acting_kid(fallback_kid=inst_kid)
    finally:
        db.close()


@app.post("/instances/<int:instance_id>/collect")
@login_required
def collect_route(instance_id: int):
    db = get_db()
    try:
        inst = db.get(TaskInstance, instance_id)
        inst_kid = inst.assigned_kid_id if inst else None

        collect_instance(db, instance_id)
        db.commit()

        return redirect_to_board_preserving_acting_kid(fallback_kid=inst_kid)
    finally:
        db.close()


@app.post("/instances/<int:instance_id>/delete")
@login_required
def delete_instance(instance_id: int):
    g = gm_guard_or_redirect()
    if g:
        return g

    db = get_db()
    try:
        inst = db.get(TaskInstance, instance_id)
        if not inst:
            return redirect_back("archive")

        inst_kid = inst.assigned_kid_id

        db.query(PointsLedger).filter(PointsLedger.instance_id == instance_id).delete()
        db.delete(inst)
        db.commit()
        
        # Check if we should redirect to board (from board) or archive (from archive)
        ref = request.referrer
        if ref and 'archive' in ref:
            return redirect_back("archive")
        else:
            return redirect_to_board_preserving_acting_kid(fallback_kid=inst_kid)
    finally:
        db.close()


@app.post("/instances/reorder")
@login_required
def reorder_route():
    db = get_db()
    try:
        status = InstanceStatus(request.form.get("status"))
        ordered_ids = request.form.get("ordered_ids", "")
        filter_kid = (request.form.get("filter_kid", "") or "").strip()

        ids = [int(x) for x in ordered_ids.split(",") if x.strip()]
        fk = int(filter_kid) if (filter_kid and filter_kid.lower() != "none") else None

        set_column_order(db, status, ids, filter_kid_id=fk)
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        db.close()


# -------------------
# Archive / Ledger UI
# -------------------
@app.get("/archive")
@login_required
def archive():
    db = get_db()
    try:
        user = current_user(db)
        kid = request.args.get("kid", type=int)

        q = select(TaskInstance).where(
            TaskInstance.status == InstanceStatus.done,
            TaskInstance.archived == True  # noqa: E712
        )
        if kid:
            q = q.where(TaskInstance.assigned_kid_id == kid)

        items = db.scalars(q.order_by(desc(TaskInstance.approved_at).nullslast(), desc(TaskInstance.id))).all()
        kids = db.scalars(select(Kid).order_by(Kid.name)).all()

        return render_template(
            "archive.html",
            user=user,
            gm_unlocked=is_gamemaster_unlocked(),
            kids=kids,
            kid=kid,
            items=items
        )
    finally:
        db.close()


@app.get("/kids/<int:kid_id>/ledger")
@login_required
def ledger(kid_id: int):
    db = get_db()
    try:
        user = current_user(db)
        kid = db.get(Kid, kid_id)
        if not kid:
            return "Kid not found", 404

        rp = ensure_rent_policy(db, kid_id)
        balance = kid_balance(db, kid_id)
        covered = months_covered(balance, rp.rent_amount)

        entries = db.scalars(
            select(PointsLedger).where(PointsLedger.kid_id == kid_id).order_by(desc(PointsLedger.created_at))
        ).all()

        return render_template(
            "ledger.html",
            user=user,
            gm_unlocked=is_gamemaster_unlocked(),
            kid=kid,
            balance=balance,
            rent_policy=rp,
            months_covered=covered,
            entries=entries
        )
    finally:
        db.close()


@app.post("/kids/<int:kid_id>/rent")
@login_required
def update_rent(kid_id: int):
    g = gm_guard_or_redirect()
    if g:
        return g

    db = get_db()
    try:
        rp = ensure_rent_policy(db, kid_id)
        rent_amount = int(request.form.get("rent_amount", "0"))
        rent_day = int(request.form.get("rent_day_of_month", "1"))
        rp.rent_amount = max(0, rent_amount)
        rp.rent_day_of_month = min(28, max(1, rent_day))
        db.commit()
        return redirect(url_for("ledger", kid_id=kid_id))
    finally:
        db.close()


@app.post("/kids/<int:kid_id>/adjust")
@login_required
def adjust(kid_id: int):
    g = gm_guard_or_redirect()
    if g:
        return g

    db = get_db()
    try:
        amount = int(request.form.get("amount", "0"))
        note = (request.form.get("note", "") or "")[:255]
        db.add(
            PointsLedger(
                kid_id=kid_id,
                amount=amount,
                reason=LedgerReason.manual_adjustment,
                instance_id=None,
                note=note,
            )
        )
        db.commit()
        return redirect(url_for("ledger", kid_id=kid_id))
    finally:
        db.close()


@app.post("/rent/charge")
@login_required
def charge_rent():
    g = gm_guard_or_redirect()
    if g:
        return g

    db = get_db()
    try:
        kids = db.scalars(select(Kid)).all()
        charged = 0
        for k in kids:
            if charge_rent_if_due(db, k.id):
                charged += 1
        db.commit()
        return jsonify({"ok": True, "charged_kids": charged})
    finally:
        db.close()
