from __future__ import annotations

from sqlalchemy.orm import Session

from app import clock
from app.models import AuditLog, User


def actor_of(user: User | None, fallback: str = "system") -> str:
    return f"user:{user.id}" if user is not None else fallback


def log(
    db: Session,
    actor: str,
    action: str,
    obj=None,
    old: dict | None = None,
    new: dict | None = None,
    object_type: str | None = None,
    object_id: str | int | None = None,
) -> None:
    if obj is not None:
        object_type = object_type or obj.__tablename__
        object_id = object_id if object_id is not None else getattr(obj, "id", None)
    db.add(
        AuditLog(
            actor=actor,
            action=action,
            object_type=object_type,
            object_id=str(object_id) if object_id is not None else None,
            old=_jsonable(old),
            new=_jsonable(new),
            at=clock.now(),
        )
    )


def _jsonable(d: dict | None) -> dict | None:
    if d is None:
        return None
    out = {}
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, (str, int, float, bool, list, dict)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out
