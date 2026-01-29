from datetime import datetime, date
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    TaskTemplate, TaskInstance, InstanceStatus,
    PointsLedger, LedgerReason, RentPolicy
)

def kid_balance(db: Session, kid_id: int) -> int:
    total = db.scalar(select(func.coalesce(func.sum(PointsLedger.amount), 0)).where(PointsLedger.kid_id == kid_id))
    return int(total or 0)

def months_covered(balance: int, rent_amount: int) -> float:
    if rent_amount <= 0:
        return 0.0
    return balance / rent_amount

def ensure_rent_policy(db: Session, kid_id: int) -> RentPolicy:
    rp = db.scalar(select(RentPolicy).where(RentPolicy.kid_id == kid_id))
    if rp:
        return rp
    rp = RentPolicy(kid_id=kid_id, rent_amount=50, rent_day_of_month=1)
    db.add(rp)
    db.flush()
    return rp

def create_instance_from_template(db: Session, template_id: int, kid_id: int) -> TaskInstance:
    tmpl = db.get(TaskTemplate, template_id)
    if not tmpl:
        raise ValueError("Template not found")
    if not tmpl.available:
        raise ValueError("That task is currently hidden. Use Refresh Pool or finish the active one.")

    inst = TaskInstance(
        template_id=tmpl.id,
        assigned_kid_id=kid_id,
        points_awarded=tmpl.default_points,
        details="",
        status=InstanceStatus.doing,
        archived=False,
    )
    db.add(inst)
    tmpl.available = False
    db.flush()
    return inst

def move_instance(db: Session, instance_id: int, new_status: InstanceStatus):
    inst = db.get(TaskInstance, instance_id)
    if not inst:
        raise ValueError("Instance not found")

    allowed = {
        InstanceStatus.doing: {InstanceStatus.review},
        InstanceStatus.review: {InstanceStatus.doing},
        InstanceStatus.done: set(),
    }
    if new_status not in allowed.get(inst.status, set()):
        raise ValueError(f"Cannot move from {inst.status} to {new_status}")

    inst.status = new_status

def update_instance_details(db: Session, instance_id: int, details: str):
    inst = db.get(TaskInstance, instance_id)
    if not inst:
        raise ValueError("Instance not found")
    if inst.status == InstanceStatus.done:
        raise ValueError("Cannot edit details after Done.")
    inst.details = (details or "")[:1000]

def approve_instance(db: Session, instance_id: int):
    """
    Gamemaster approves: task becomes Done but does NOT award points.
    Points are awarded only when Collect is clicked.
    """
    inst = db.get(TaskInstance, instance_id)
    if not inst:
        raise ValueError("Instance not found")
    if inst.status != InstanceStatus.review:
        raise ValueError("Instance is not in Review.")

    inst.status = InstanceStatus.done
    inst.approved_at = datetime.now()
    inst.archived = False  # ensure it appears in Done lane and is collectible

    # IMPORTANT: do NOT add PointsLedger entry here

    # Re-enable the template now that work is approved (you asked for that behavior)
    tmpl = db.get(TaskTemplate, inst.template_id)
    if tmpl and not tmpl.available:
        tmpl.available = True

def reject_instance(db: Session, instance_id: int):
    inst = db.get(TaskInstance, instance_id)
    if not inst:
        raise ValueError("Instance not found")
    if inst.status != InstanceStatus.review:
        raise ValueError("Instance is not in Review.")
    inst.status = InstanceStatus.doing

def collect_instance(db: Session, instance_id: int):
    """
    Player or gamemaster: moves a DONE instance out of Done lane into Archive
    AND awards points exactly once.
    """
    inst = db.get(TaskInstance, instance_id)
    if not inst:
        raise ValueError("Instance not found")
    if inst.status != InstanceStatus.done:
        raise ValueError("Only Done tickets can be collected.")
    if inst.archived:
        # already collected
        return

    # Mark collected (archived) and award points
    inst.archived = True

    db.add(PointsLedger(
        kid_id=inst.assigned_kid_id,
        amount=+inst.points_awarded,
        reason=LedgerReason.task_approved,
        instance_id=inst.id,
        note=f"Collected: {inst.template.title}",
    ))

def refresh_pool(db: Session):
    db.query(TaskTemplate).update({TaskTemplate.available: True})

def charge_rent_if_due(db: Session, kid_id: int, today: date | None = None) -> bool:
    today = today or date.today()
    rp = ensure_rent_policy(db, kid_id)

    if today.day != rp.rent_day_of_month:
        return False
    if rp.last_charged_on == today:
        return False

    db.add(PointsLedger(
        kid_id=kid_id,
        amount=-abs(rp.rent_amount),
        reason=LedgerReason.rent_paid,
        instance_id=None,
        note=f"Monthly rent (day {rp.rent_day_of_month})",
    ))
    rp.last_charged_on = today
    return True

def set_column_order(db: Session, status: InstanceStatus, ordered_instance_ids: list[int], filter_kid_id: int | None = None):
    if not ordered_instance_ids:
        return
    q = db.query(TaskInstance).filter(TaskInstance.status == status, TaskInstance.id.in_(ordered_instance_ids))
    if filter_kid_id:
        q = q.filter(TaskInstance.assigned_kid_id == filter_kid_id)

    found = {i.id: i for i in q.all()}
    for idx, iid in enumerate(ordered_instance_ids):
        inst = found.get(iid)
        if inst:
            inst.sort_order = idx
