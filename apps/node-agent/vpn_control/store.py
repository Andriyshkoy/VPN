"""Durable identity reservation before backend calls and replayable operations."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from uuid import uuid4


class StoreError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class Store:
    def __init__(self, path: Path, lease_seconds=120):
        self.path, self.lease_seconds = Path(path), lease_seconds
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                or stat.S_IMODE(parent.st_mode) != 0o700):
            raise ValueError("database_parent_must_be_owned_and_private")
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_uid != os.geteuid():
                raise ValueError("invalid_database_file")
        else:
            os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accesses (
                  id TEXT PRIMARY KEY, service_id TEXT NOT NULL, node_id TEXT NOT NULL,
                  owner_ref TEXT, backend_username TEXT UNIQUE NOT NULL,
                  desired_status TEXT NOT NULL, status TEXT NOT NULL,
                  settings TEXT NOT NULL, snapshot TEXT,
                  current_operation TEXT, revision INTEGER NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                  id TEXT PRIMARY KEY, principal TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                  request_hash TEXT NOT NULL, kind TEXT NOT NULL, access_id TEXT NOT NULL,
                  state TEXT NOT NULL, response TEXT, http_status INTEGER,
                  lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
                  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                  UNIQUE(principal,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS accesses_owner ON accesses(owner_ref,created_at,id);
            """)
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def decode(row):
        if row is None:
            return None
        item = dict(row)
        item["settings"] = json.loads(item["settings"])
        item["snapshot"] = json.loads(item["snapshot"]) if item["snapshot"] else None
        return item

    def get(self, access_id):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM accesses WHERE id=?", (access_id,)).fetchone())

    def operation_exists(self, principal, key, request_hash):
        with self.connect() as db:
            op = db.execute("SELECT request_hash FROM operations WHERE principal=? AND idempotency_key=?", (principal, key)).fetchone()
        if op is not None and op["request_hash"] != request_hash:
            raise StoreError("idempotency_key_reused")
        return op is not None

    def listing(self, owner_ref=None, service_id=None, limit=100, offset=0):
        filters, args = [], []
        if owner_ref is not None:
            filters.append("owner_ref=?"); args.append(owner_ref)
        if service_id is not None:
            filters.append("service_id=?"); args.append(service_id)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM accesses" + where, args).fetchone()[0]
            rows = db.execute("SELECT * FROM accesses" + where + " ORDER BY created_at,id LIMIT ? OFFSET ?", [*args, limit, offset]).fetchall()
        return [self.decode(x) for x in rows], total

    def reserve(self, principal, key, request_hash, kind, access_id=None, create=None):
        timestamp, lease_owner = int(time.time()), uuid4().hex
        with self.transaction() as db:
            op = db.execute("SELECT * FROM operations WHERE principal=? AND idempotency_key=?", (principal, key)).fetchone()
            if op:
                op = dict(op)
                if op["request_hash"] != request_hash:
                    raise StoreError("idempotency_key_reused")
                if op["state"] in ("done", "failed"):
                    return {"replay": True, "response": json.loads(op["response"]), "http_status": op["http_status"]}
                if op["lease_until"] > time.time():
                    raise StoreError("operation_in_progress")
                db.execute("UPDATE operations SET lease_owner=?,lease_until=?,updated_at=? WHERE id=?", (lease_owner, time.time() + self.lease_seconds, timestamp, op["id"]))
                row = db.execute("SELECT * FROM accesses WHERE id=?", (op["access_id"],)).fetchone()
                return {"replay": False, "operation_id": op["id"], "lease_owner": lease_owner, "access": self.decode(row)}
            operation_id = str(uuid4())
            if create is not None:
                access_id = str(uuid4())
                name = "vc_" + access_id.replace("-", "")[:24]
                db.execute("INSERT INTO accesses(id,service_id,node_id,owner_ref,backend_username,desired_status,status,settings,current_operation,created_at,updated_at) VALUES(?,?,?,?,?,'active','provisioning',?,?,?,?)",
                           (access_id, create["service_id"], create["node_id"], create["owner_ref"], name, json.dumps(create["settings"], sort_keys=True), operation_id, timestamp, timestamp))
            else:
                row = db.execute("SELECT * FROM accesses WHERE id=?", (access_id,)).fetchone()
                if row is None:
                    raise StoreError("access_not_found", 404)
                if row["current_operation"] is not None:
                    # A different unresolved operation must be reconciled first.
                    raise StoreError("access_has_pending_operation")
                if row["status"] == "revoked" and kind != "revoke":
                    raise StoreError("access_revoked")
                db.execute("UPDATE accesses SET current_operation=? WHERE id=?", (operation_id, access_id))
            db.execute("INSERT INTO operations(id,principal,idempotency_key,request_hash,kind,access_id,state,lease_owner,lease_until,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',?,?,?,?)",
                       (operation_id, principal, key, request_hash, kind, access_id, lease_owner, time.time() + self.lease_seconds, timestamp, timestamp))
            row = db.execute("SELECT * FROM accesses WHERE id=?", (access_id,)).fetchone()
            return {"replay": False, "operation_id": operation_id, "lease_owner": lease_owner, "access": self.decode(row)}

    def finish(self, operation_id, lease_owner, access_id, response, http_status, *, snapshot=None, settings=None, owner_ref=None, owner_changed=False, desired_status=None, terminal_error=False):
        timestamp = int(time.time())
        with self.transaction() as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None or op["lease_owner"] != lease_owner:
                raise StoreError("operation_lease_lost")
            if not terminal_error:
                row = self.decode(db.execute("SELECT * FROM accesses WHERE id=?", (access_id,)).fetchone())
                next_settings = settings if settings is not None else row["settings"]
                next_owner = owner_ref if owner_changed else row["owner_ref"]
                next_status = snapshot["status"] if snapshot else row["status"]
                next_desired = desired_status or row["desired_status"]
                db.execute("UPDATE accesses SET settings=?,snapshot=?,owner_ref=?,status=?,desired_status=?,revision=revision+1,current_operation=NULL,updated_at=? WHERE id=?",
                           (json.dumps(next_settings, sort_keys=True), json.dumps(snapshot) if snapshot else row["snapshot"] and json.dumps(row["snapshot"]), next_owner, next_status, next_desired, timestamp, access_id))
            else:
                status = "failed" if op["kind"] == "create" else None
                db.execute("UPDATE accesses SET current_operation=NULL,status=COALESCE(?,status),updated_at=? WHERE id=?", (status, timestamp, access_id))
            db.execute("UPDATE operations SET state=?,response=?,http_status=?,lease_owner=NULL,lease_until=0,updated_at=? WHERE id=?", ("failed" if terminal_error else "done", json.dumps(response), http_status, timestamp, operation_id))

    def release(self, operation_id, lease_owner):
        # Keep the operation and its identity durable after an ambiguous failure.
        with self.transaction() as db:
            db.execute("UPDATE operations SET lease_owner=NULL,lease_until=0,updated_at=? WHERE id=? AND lease_owner=?", (int(time.time()), operation_id, lease_owner))
