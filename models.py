import enum
from datetime import datetime, date

from sqlalchemy import (
    String,
    Integer,
    DateTime,
    Date,
    ForeignKey,
    Enum,
    UniqueConstraint,
    Boolean,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


class Role(str, enum.Enum):
    gamemaster = "gamemaster"


class InstanceStatus(str, enum.Enum):
    doing = "doing"
    review = "review"
    done = "done"


class LedgerReason(str, enum.Enum):
    task_approved = "task_approved"
    rent_paid = "rent_paid"
    manual_adjustment = "manual_adjustment"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role), index=True)


class Kid(Base):
    __tablename__ = "kids"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    color: Mapped[str] = mapped_column(String(20), default="#3b82f6")


class TaskTemplate(Base):
    __tablename__ = "task_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(140), unique=True)
    default_points: Mapped[int] = mapped_column(Integer, default=1)
    help_text: Mapped[str] = mapped_column(String(1000), default="")
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TaskInstance(Base):
    __tablename__ = "task_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # FIXED: Make template_id nullable with ON DELETE SET NULL
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_templates.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )
    assigned_kid_id: Mapped[int] = mapped_column(ForeignKey("kids.id"), index=True)

    # ADDED: Instance-specific title (independent from template)
    title: Mapped[str] = mapped_column(String(140), default="")

    points_awarded: Mapped[int] = mapped_column(Integer)
    details: Mapped[str] = mapped_column(String(1000), default="")

    status: Mapped[InstanceStatus] = mapped_column(
        Enum(InstanceStatus), index=True, default=InstanceStatus.doing
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # IMPORTANT: matches DB column you added
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # FIXED: Relationship can now be None
    template: Mapped["TaskTemplate | None"] = relationship()
    assigned_kid: Mapped["Kid"] = relationship()


class PointsLedger(Base):
    __tablename__ = "points_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kid_id: Mapped[int] = mapped_column(ForeignKey("kids.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[LedgerReason] = mapped_column(Enum(LedgerReason), index=True)
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("task_instances.id"), nullable=True, index=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    kid: Mapped["Kid"] = relationship()
    # ADDED: Relationship to access the instance for displaying current title
    instance: Mapped["TaskInstance | None"] = relationship()


class RentPolicy(Base):
    __tablename__ = "rent_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kid_id: Mapped[int] = mapped_column(ForeignKey("kids.id"), unique=True, index=True)
    rent_amount: Mapped[int] = mapped_column(Integer, default=50)
    rent_day_of_month: Mapped[int] = mapped_column(Integer, default=1)
    last_charged_on: Mapped[date | None] = mapped_column(Date, nullable=True)

    __table_args__ = (UniqueConstraint("kid_id", name="uq_rent_kid"),)
