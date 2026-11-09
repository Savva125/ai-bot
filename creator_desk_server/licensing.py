"""All licensing mutations lock the license row (PostgreSQL)."""
import hashlib
import json
import secrets
import time
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from fastapi import HTTPException

from .models import Audit, Device, Job, License


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def now():
    return int(time.time())


def active(license):
    if license.blocked:
        raise HTTPException(403, "license_blocked")
    if license.expires_at is not None and license.expires_at <= now():
        raise HTTPException(403, "license_expired")


class Licensing:
    def __init__(self, sessions):
        self.sessions = sessions

    def audit(self, session, actor, action, license_id):
        session.add(Audit(id=str(uuid4()), actor=actor, action=action,
                          license_id=license_id, created_at=now()))

    def create(self, data, actor):
        raw = "cd_" + secrets.token_urlsafe(32)
        with self.sessions.begin() as s:
            item = License(id=str(uuid4()), key_hash=digest(raw), created_at=now(), **data.model_dump())
            s.add(item)
            self.audit(s, actor, "create", item.id)
            result = {"id": item.id, "key": raw, **data.model_dump()}
        return result

    def activate(self, data):
        with self.sessions.begin() as s:
            item = s.scalar(select(License).where(License.key_hash == digest(data.key)).with_for_update())
            if item is None:
                raise HTTPException(401, "invalid_key")
            active(item)
            device_hash = digest(data.device_id)
            device = s.scalar(select(Device).where(Device.license_id == item.id, Device.device_hash == device_hash))
            if device is None:
                count = s.scalar(select(func.count()).select_from(Device).where(Device.license_id == item.id))
                if count >= item.max_devices:
                    raise HTTPException(403, "device_limit")
            if item.expires_at is None:
                item.expires_at = now() + item.days * 86400
            token = secrets.token_urlsafe(48)
            expires = min(item.expires_at, now() + 30 * 86400)
            if device is None:
                device = Device(id=str(uuid4()), license_id=item.id, device_hash=device_hash,
                                token_hash=digest(token), expires_at=expires)
                s.add(device)
            else:
                device.token_hash, device.expires_at = digest(token), expires
            self.audit(s, "client", "activate", item.id)
            return {"token": token, "token_expires_at": expires, "expires_at": item.expires_at,
                    "license_id": item.id, "daily_limit": item.daily_limit}

    def authenticate(self, session, token, lock=False):
        device = session.scalar(select(Device).where(Device.token_hash == digest(token)))
        if device is None or device.expires_at <= now():
            raise HTTPException(401, "session_expired")
        statement = select(License).where(License.id == device.license_id)
        item = session.scalar(statement.with_for_update() if lock else statement)
        active(item)
        # A reset may have committed while we waited for the license lock.
        current = session.scalar(select(Device.id).where(Device.id == device.id,
                                                         Device.token_hash == digest(token)))
        if current is None:
            raise HTTPException(401, "session_expired")
        return item

    def status(self, token):
        with self.sessions() as s:
            item = self.authenticate(s, token)
            return self.describe(s, item)

    def describe(self, s, item):
        start = now() // 86400 * 86400
        used = s.scalar(select(func.count()).select_from(Job).where(
            Job.license_id == item.id, Job.created_at >= start, Job.state.in_(["pending", "done"])))
        devices = s.scalar(select(func.count()).select_from(Device).where(Device.license_id == item.id))
        return {"id": item.id, "label": item.label, "blocked": item.blocked,
                "expires_at": item.expires_at, "days": item.days, "daily_limit": item.daily_limit,
                "used_today": used, "max_devices": item.max_devices, "devices": devices}

    def admin_list(self, offset=0):
        with self.sessions() as s:
            return [self.describe(s, item) for item in s.scalars(
                select(License).order_by(License.created_at.desc(), License.id).offset(offset).limit(20))]

    def admin_change(self, license_id, action, actor, days=0):
        with self.sessions.begin() as s:
            item = s.scalar(select(License).where(License.id == license_id).with_for_update())
            if item is None:
                raise HTTPException(404, "license_not_found")
            if action == "extend":
                if item.expires_at is None:
                    item.days += days
                else:
                    item.expires_at = max(now(), item.expires_at) + days * 86400
            elif action in {"block", "unblock"}:
                item.blocked = action == "block"
            elif action == "reset":
                s.execute(delete(Device).where(Device.license_id == item.id))
            else:
                raise ValueError("Unknown action")
            self.audit(s, actor, action, item.id)
            return self.describe(s, item)

    def reserve(self, token, request_id, context_hash, timeout):
        with self.sessions.begin() as s:
            item = self.authenticate(s, token, lock=True)
            existing = s.scalar(select(Job).where(Job.license_id == item.id, Job.request_id == request_id))
            if existing:
                if existing.context_hash != context_hash:
                    raise HTTPException(409, "request_id_conflict")
                if existing.state == "done" and existing.result:
                    return existing.id, json.loads(existing.result)
                raise HTTPException(409, "request_pending" if existing.state == "pending" else "request_failed")
            # Never retain a generated message beyond one hour, even with continued uptime.
            s.execute(update(Job).where(Job.result.is_not(None), Job.created_at < now() - 3600).values(result=None))
            # A dead worker must not hold a device indefinitely. Request IDs remain unusable.
            s.execute(update(Job).where(Job.license_id == item.id, Job.state == "pending",
                                        Job.lease_until < now()).values(state="failed"))
            pending = s.scalar(select(Job.id).where(Job.license_id == item.id, Job.state == "pending").limit(1))
            if pending:
                raise HTTPException(429, "generation_in_progress")
            recent = s.scalar(select(func.count()).select_from(Job).where(
                Job.license_id == item.id, Job.created_at >= now() - 60))
            if recent >= 20:
                raise HTTPException(429, "request_rate_limit")
            used = s.scalar(select(func.count()).select_from(Job).where(Job.license_id == item.id,
                Job.created_at >= now() // 86400 * 86400, Job.state.in_(["pending", "done"])))
            if used >= item.daily_limit:
                raise HTTPException(429, "daily_limit")
            job_id = str(uuid4())
            s.add(Job(id=job_id, license_id=item.id, request_id=request_id, context_hash=context_hash,
                      created_at=now(), lease_until=now() + int(timeout) + 60, state="pending"))
            return job_id, None

    def finish(self, job_id, result=None):
        with self.sessions.begin() as s:
            job = s.get(Job, job_id)
            job.state = "done" if result is not None else "failed"
            job.result = json.dumps(result, ensure_ascii=False) if result is not None else None

    def maintenance(self):
        with self.sessions.begin() as s:
            s.execute(update(Job).where(Job.result.is_not(None), Job.created_at < now() - 3600).values(result=None))
            s.execute(delete(Job).where(Job.created_at < now() - 31 * 86400))
