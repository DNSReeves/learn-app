"""packver.py — P5.17 pack versioning + migrations (iss_8d424360, 2026-07-22).

Every topic pack gets a content hash. When a live pack's content changes, the
sync diffs its concept ids against the last registered version and — instead of
silently orphaning progress — either:

  * MIGRATES concept_state rows, when the pack declares the rename:
        "migrations": {"renamed_concepts": {"old-id": "new-id"}}
    (state moves for every user; if a user somehow already has state under the
    new id, the old row is archived rather than clobbering either), or
  * ARCHIVES concept_state rows for concepts that were removed outright
    (copied to concept_state_archive with the from/to pack hashes, then
    deleted from the live table).

Question-id churn inside a concept does NOT touch state (concept_state is
keyed per concept; the answers table is a historical log and keeps old ids),
but every content change registers a new pack_versions row, so the full edit
history of a live pack is reconstructable from the registry.

Wired into app.load_topics() behind an in-memory hash cache: the sync runs on
the first request after any pack file actually changes (preserving the
edit-file → refresh convention) and is a no-op otherwise.
"""
import hashlib
import json
import logging
import time

import db

logger = logging.getLogger("learn.packver")

# in-memory throttle: topic_id -> last-synced pack hash (process lifetime)
_seen: dict = {}


def pack_hash(pack: dict) -> str:
    """Canonical content hash of a pack (key order independent)."""
    return hashlib.sha256(
        json.dumps(pack, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def manifest(pack: dict) -> dict:
    """The id surface the versioning cares about."""
    return {
        "concept_ids": [c["id"] for c in pack.get("concepts", [])],
        "question_ids": {
            c["id"]: [q.get("id") for q in c.get("questions", [])]
            for c in pack.get("concepts", [])
        },
    }


def _archive_rows(c, topic_id: str, concept_id: str, reason: str,
                  from_hash: str, to_hash: str) -> int:
    now = time.time()
    rows = c.execute(
        "SELECT * FROM concept_state WHERE topic_id=? AND concept_id=?",
        (topic_id, concept_id)).fetchall()
    for r in rows:
        c.execute(
            """INSERT INTO concept_state_archive
               (user_id, topic_id, concept_id, p_mastery, attempts, correct,
                streak, unlocked, mastered_at, interval_d, ease, due_at,
                archived_at, reason, from_hash, to_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["user_id"], r["topic_id"], r["concept_id"], r["p_mastery"],
             r["attempts"], r["correct"], r["streak"], r["unlocked"],
             r["mastered_at"], r["interval_d"], r["ease"], r["due_at"],
             now, reason, from_hash, to_hash))
    c.execute("DELETE FROM concept_state WHERE topic_id=? AND concept_id=?",
              (topic_id, concept_id))
    return len(rows)


def sync_pack(pack: dict) -> dict | None:
    """Register the pack's current content; migrate/archive on change.

    Returns a report dict when a new version was registered, None on no-op.
    """
    tid = pack["id"]
    h = pack_hash(pack)
    with db.conn() as c:
        latest = c.execute(
            "SELECT version, pack_hash, manifest_json FROM pack_versions "
            "WHERE topic_id=? ORDER BY version DESC LIMIT 1", (tid,)).fetchone()
        if latest and latest["pack_hash"] == h:
            return None

        new_man = manifest(pack)
        report = {"topic_id": tid, "version": 1, "migrated": {}, "archived": {},
                  "collisions": {}, "prev_hash": None, "pack_hash": h}

        if latest:
            report["version"] = latest["version"] + 1
            report["prev_hash"] = latest["pack_hash"]
            old_man = json.loads(latest["manifest_json"])
            old_ids = set(old_man.get("concept_ids", []))
            new_ids = set(new_man["concept_ids"])

            # 1. declared renames migrate state (old row moves unless the user
            #    already has state under the new id — then archive the old).
            renames = (pack.get("migrations") or {}).get("renamed_concepts") or {}
            for old, new in renames.items():
                if old not in old_ids or new not in new_ids:
                    continue          # stale/forward declaration — nothing to move
                cur = c.execute(
                    """UPDATE concept_state SET concept_id=?
                       WHERE topic_id=? AND concept_id=?
                         AND NOT EXISTS (SELECT 1 FROM concept_state s2
                                         WHERE s2.user_id=concept_state.user_id
                                           AND s2.topic_id=concept_state.topic_id
                                           AND s2.concept_id=?)""",
                    (new, tid, old, new))
                if cur.rowcount:
                    report["migrated"][old] = {"to": new, "rows": cur.rowcount}
                leftover = _archive_rows(
                    c, tid, old, f"rename_collision:{old}->{new}", latest["pack_hash"], h)
                if leftover:
                    report["collisions"][old] = {"to": new, "archived": leftover}

            # 2. concepts removed outright → archive their state.
            for rid in sorted(old_ids - new_ids - set(renames.keys())):
                n = _archive_rows(c, tid, rid, "concept_removed",
                                  latest["pack_hash"], h)
                if n:
                    report["archived"][rid] = n

        c.execute(
            "INSERT INTO pack_versions (topic_id, version, pack_hash, "
            "manifest_json, seen_at) VALUES (?,?,?,?,?)",
            (tid, report["version"], h, json.dumps(new_man), time.time()))

    if report["migrated"] or report["archived"] or report["collisions"]:
        logger.warning("pack %s v%d: migrated=%s archived=%s collisions=%s",
                       tid, report["version"], report["migrated"],
                       report["archived"], report["collisions"])
    else:
        logger.info("pack %s registered v%d (%s)", tid, report["version"], h[:12])
    return report


def maybe_sync(packs: dict) -> list:
    """Cheap per-request gate: hash each pack in memory; run the DB sync only
    for packs whose content actually changed since this process last saw them."""
    reports = []
    for tid, pack in packs.items():
        h = pack_hash(pack)
        if _seen.get(tid) == h:
            continue
        r = sync_pack(pack)
        _seen[tid] = h
        if r:
            reports.append(r)
    return reports
