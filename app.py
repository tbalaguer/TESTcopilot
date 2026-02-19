from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from sqlalchemy import select, desc
from dotenv import load_dotenv
from urllib.parse import urlparse
from datetime import datetime
from zoneinfo import ZoneInfo

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

# Pacific Time timezone constant
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


# -----------------------
# Template filters
# -----------------------
@app.template_filter('format_approved')
def format_approved(dt):
    """Format approved_at datetime as 'Approved: MM-DD-YYYY at h:mm AM/PM' in Pacific Time"""
    if dt is None:
        return ""
    if isinstance(dt, str):
        # Try to parse if it's a string
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return dt

    # Convert to Pacific Time if datetime is timezone-aware or naive (assume UTC)
    if dt.tzinfo is None:
        # Treat naive datetime as UTC
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    dt = dt.astimezone(PACIFIC_TZ)

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


# ADDED: Template filter for formatting dates in Pacific Time
@app.template_filter('datetime_local')
def datetime_local(dt):
    """Format datetime as 'YYYY-MM-DDTHH:MM' in Pacific Time for datetime-local inputs"""
    if dt is None:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    dt = dt.astimezone(PACIFIC_TZ)
    return dt.strftime('%Y-%m-%dT%H:%M')


@app.template_filter('format_date')
def format_date(dt):
    """Format datetime as 'MM-DD-YYYY at h:mm AM/PM' in Pacific Time"""
    if dt is None:
        return "N/A"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return dt

    # Convert to Pacific Time if datetime is timezone-aware or naive (assume UTC)
    if dt.tzinfo is None:
        # Treat naive datetime as UTC
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    dt = dt.astimezone(PACIFIC_TZ)

    try:
        formatted = dt.strftime('%m-%d-%Y at %-I:%M %p')
    except ValueError:
        formatted = dt.strftime('%m-%d-%Y at %I:%M %p')
        parts = formatted.split(' at ')
        if len(parts) == 2:
            time_parts = parts[1].split(':')
            if time_parts[0].startswith('0'):
                time_parts[0] = time_parts[0][1:]
            parts[1] = ':'.join(time_parts)
            formatted = ' at '.join(parts)
    return formatted


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


def get_pool_visible_from_request() -> bool:
    """
    Get pool visibility state from form data or URL parameter.
    Returns True if pool should be visible, False if collapsed.
    Defaults to False (collapsed).
    """
    # CHANGED: Check both form data (POST) and query params (GET)
    pool_param = (request.form.get("pool") or request.args.get("pool") or "0").strip()
    return pool_param == "1"


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
    # Also preserve pool visibility state
    pool_visible = get_pool_visible_from_request()

    if acting_kid:
        return redirect(url_for("board", acting_kid=acting_kid, pool=1 if pool_visible else 0))
    return redirect(url_for("board", pool=1 if pool_visible else 0))


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
            return jsonify({"error": "Incorrect password"}), 401

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
        pool_visible = get_pool_visible_from_request()

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
            pool_visible=pool_visible,
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

        # CHANGED: Templates cannot be placed directly into "done"
        if target_status == "done":
            return jsonify({"error": "Templates cannot be placed directly into Claim Reward."}), 400

        # CHANGED: Players can now instantiate into "doing" or "review"
        # GM can instantiate into any non-Done column
        if not is_gamemaster_unlocked() and target_status not in ["doing", "review"]:
            return jsonify({"error": "Templates can be dropped into 'On an Adventure' or 'Ready for Check'."}), 400

        if not acting_raw or acting_raw.lower() == "none":
            return jsonify({"error": "No active player selected."}), 400

        acting_kid_id = int(acting_raw)

        inst = create_instance_from_template(db, template_id, acting_kid_id)
        # CHANGED: Set the target status
        if target_status != "doing":
            inst.status = InstanceStatus(target_status)

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


# ADDED: New endpoint for drag-to-done approval (GM only)
@app.post("/instances/<int:instance_id>/approve-drag")
@login_required
def approve_drag_route(instance_id: int):
    # ADDED: GM guard for drag approval
    if not is_gamemaster_unlocked():
        return jsonify({"error": "Only Game Master can drag to Claim Reward"}), 403

    db = get_db()
    try:
        approve_instance(db, instance_id)
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        db.close()


# MODIFIED: Endpoint for unapproving and moving when dragged out of Done (GM only)
@app.post("/instances/<int:instance_id>/unapprove")
@login_required
def unapprove_route(instance_id: int):
    # GM guard for unapprove
    if not is_gamemaster_unlocked():
        return jsonify({"error": "Only Game Master can move tasks out of Claim Reward"}), 403

    db = get_db()
    try:
        inst = db.get(TaskInstance, instance_id)
        if not inst:
            return jsonify({"error": "Task not found"}), 404

        # Get target status from request (optional - for moving to specific column)
        target_status = request.form.get("status", "")

        # Remove approval timestamp and revert archived status
        inst.approved_at = None
        inst.archived = False

        # ADDED: Change status if provided
        if target_status:
            try:
                inst.status = InstanceStatus(target_status)
            except ValueError:
                return jsonify({"error": f"Invalid status: {target_status}"}), 400

        # Remove any points ledger entries for this instance (unapprove reverses the reward)
        db.query(PointsLedger).filter(
            PointsLedger.instance_id == instance_id,
            PointsLedger.reason == LedgerReason.task_approved
        ).delete()

        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
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


@app.post("/instances/<int:instance_id>/edit")
@login_required
def edit_instance(instance_id: int):
    g = gm_guard_or_redirect()
    if g:
        return g

    db = get_db()
    try:
        inst = db.get(TaskInstance, instance_id)
        if not inst:
            return jsonify({"error": "Task not found"}), 404

        title = request.form.get("title", "").strip()
        if title:
            inst.template.title = title

        points_str = request.form.get("points_awarded", "").strip()
        if points_str:
            try:
                inst.points_awarded = int(points_str)
            except ValueError:
                return jsonify({"error": "Invalid points value"}), 400

        assigned_kid_str = request.form.get("assigned_kid_id", "").strip()
        if assigned_kid_str:
            try:
                kid_id = int(assigned_kid_str)
            except ValueError:
                return jsonify({"error": "Invalid player ID"}), 400
            if not db.get(Kid, kid_id):
                return jsonify({"error": "Player not found"}), 404
            inst.assigned_kid_id = kid_id

        details = request.form.get("details", None)
        if details is not None:
            inst.details = details

        created_at_str = request.form.get("created_at", "").strip()
        if created_at_str:
            try:
                pt_dt = datetime.fromisoformat(created_at_str)
                pt_dt = pt_dt.replace(tzinfo=PACIFIC_TZ)
                inst.created_at = pt_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            except ValueError:
                pass

        approved_at_str = request.form.get("approved_at", "").strip()
        if approved_at_str:
            try:
                pt_dt = datetime.fromisoformat(approved_at_str)
                pt_dt = pt_dt.replace(tzinfo=PACIFIC_TZ)
                inst.approved_at = pt_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            except ValueError:
                pass

        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"error": str(e)}), 400
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

        # CHANGED: Check if we should redirect to board, archive, or ledger
        ref = request.referrer
        if ref and 'archive' in ref:
            return redirect_back("archive")
        elif ref and '/kids/' in ref and '/ledger' in ref:
            # Redirect back to the same ledger page
            return redirect(url_for("ledger", kid_id=inst_kid))
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

        # Get completed task instances
        q = select(TaskInstance).where(
            TaskInstance.status == InstanceStatus.done,
            TaskInstance.archived == True  # noqa: E712
        )
        if kid:
            q = q.where(TaskInstance.assigned_kid_id == kid)

        items = db.scalars(q.order_by(desc(TaskInstance.approved_at).nullslast(), desc(TaskInstance.id))).all()

        # Get manual adjustments (ledger entries without instance_id)
        ledger_q = select(PointsLedger).where(
            PointsLedger.reason == LedgerReason.manual_adjustment
        )
        if kid:
            ledger_q = ledger_q.where(PointsLedger.kid_id == kid)

        manual_adjustments = db.scalars(ledger_q.order_by(desc(PointsLedger.created_at))).all()

        # ADDED: Get rent charges - FIXED to use rent_paid
        rent_q = select(PointsLedger).where(
            PointsLedger.reason == LedgerReason.rent_paid
        )
        if kid:
            rent_q = rent_q.where(PointsLedger.kid_id == kid)

        rent_charges = db.scalars(rent_q.order_by(desc(PointsLedger.created_at))).all()

        # ADDED: Combine and sort entries chronologically
        all_entries = []

        # Add task instances
        for inst in items:
            all_entries.append({
                'type': 'task',
                'date': inst.approved_at if inst.approved_at else datetime.min,
                'data': inst
            })

        # Add manual adjustments
        for adj in manual_adjustments:
            # FIXED: Eager load the kid relationship
            kid_obj = db.get(Kid, adj.kid_id)
            all_entries.append({
                'type': 'adjustment',
                'date': adj.created_at if adj.created_at else datetime.min,
                'data': adj,
                'kid': kid_obj
            })

        # ADDED: Add rent charges
        for rent in rent_charges:
            # FIXED: Eager load the kid relationship
            kid_obj = db.get(Kid, rent.kid_id)
            all_entries.append({
                'type': 'rent',
                'date': rent.created_at if rent.created_at else datetime.min,
                'data': rent,
                'kid': kid_obj
            })

        # Sort by date descending (most recent first)
        all_entries.sort(key=lambda x: x['date'], reverse=True)

        kids = db.scalars(select(Kid).order_by(Kid.name)).all()

        return render_template(
            "archive.html",
            user=user,
            gm_unlocked=is_gamemaster_unlocked(),
            kids=kids,
            kid=kid,
            items=items,
            manual_adjustments=manual_adjustments,
            all_entries=all_entries
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

        # ADDED: Get all kids for wallet switcher dropdown
        kids = db.scalars(select(Kid).order_by(Kid.name)).all()

        return render_template(
            "ledger.html",
            user=user,
            gm_unlocked=is_gamemaster_unlocked(),
            kid=kid,
            kids=kids,
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


# MODIFIED: Allow deletion of manual adjustments AND rent charges - FIXED to use rent_paid
@app.post("/ledger/<int:ledger_id>/delete")
@login_required
def delete_ledger_entry(ledger_id: int):
    g = gm_guard_or_redirect()
    if g:
        return g

    db = get_db()
    try:
        ledger_entry = db.get(PointsLedger, ledger_id)
        if not ledger_entry:
            return redirect_back("archive")

        # FIXED: Allow deletion of manual adjustments and rent_paid
        if ledger_entry.reason not in [LedgerReason.manual_adjustment, LedgerReason.rent_paid]:
            return redirect_back("archive")

        # Store kid_id before deleting
        entry_kid_id = ledger_entry.kid_id

        db.delete(ledger_entry)
        db.commit()

        # Check if we should redirect to archive or ledger
        ref = request.referrer
        if ref and 'archive' in ref:
            return redirect_back("archive")
        else:
            return redirect(url_for("ledger", kid_id=entry_kid_id))
    finally:
        db.close()


# FIXED: Charge rent in Pacific Time
@app.post("/rent/charge")
@login_required
def charge_rent():
    if not is_gamemaster_unlocked():
        return redirect(url_for("board"))

    db = get_db()
    kid_id = None
    try:
        kid_id_str = request.form.get("kid_id", "").strip()

        if not kid_id_str:
            return redirect(url_for("board"))

        try:
            kid_id = int(kid_id_str)
        except ValueError:
            return redirect(url_for("board"))

        # Get kid and rent policy
        kid = db.get(Kid, kid_id)
        if not kid:
            return redirect(url_for("board"))

        rp = ensure_rent_policy(db, kid_id)

        # Only charge if rent amount is greater than 0
        if rp.rent_amount > 0:
            # Create ledger entry with Pacific Time timestamp
            now = datetime.now(PACIFIC_TZ)

            db.add(
                PointsLedger(
                    kid_id=kid_id,
                    amount=-abs(rp.rent_amount),
                    reason=LedgerReason.rent_paid,
                    instance_id=None,
                    note=f"Monthly rent (day {rp.rent_day_of_month})",
                )
            )

            # Update last charged date
            rp.last_charged_on = now.date()

            # Commit the transaction
            db.commit()

        return redirect(url_for("ledger", kid_id=kid_id))

    except Exception as e:
        db.rollback()
        import traceback
        traceback.print_exc()
        if kid_id:
            return redirect(url_for("ledger", kid_id=kid_id))
        else:
            return redirect(url_for("board"))
    finally:
        db.close()
