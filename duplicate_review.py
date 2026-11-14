"""Local duplicate-review decisions and expiring trusted address evidence.

Callers MUST obtain ``owner`` from the authenticated staff session, never model
output or submitted approval text. Fingerprints must be made from fresh server
reads. A row number alone is not an order identity: suppression requires an
actual immutable order_id or source row_revision/generation. Invented IDs,
timestamps or hashes of mutable values cannot prove a row was not reused.

Only a trusted server address-provider adapter may put evidence. The provider's
actual verified unique result must match the query/source/version. A dict saying
verified=True is not authentication. No cache API is intended for user/model
payloads. Cache records omit the original query and store a hash key; necessary
provider address evidence and staff reasons remain only in the local SQLite DB.
This module has no network, order editing, deletion, cancellation or send tools.
"""
import hashlib
import json
import math
import re
import time
from contextlib import contextmanager

from order_locations import _resolve as _validate_provider_result


_ERRORS = {'#n/a', '#ref!', '#value!', '#div/0!', '#num!', '#name?', '#null!', '#error!', '#spill!'}
_MISSING = {'미확인', '확인필요', '미정', '없음', 'unknown', 'none', 'null', 'n/a', '?'}
_EVIDENCE_FIELDS = {'status', 'verified', 'provider', 'aliases', 'building_id',
                    'canonical_road_address', 'canonical_building_name'}
CACHE_MAX_ENTRIES = 5000
CACHE_MAX_TTL_SECONDS = 86400


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _text(value, name, limit=2000):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 for c in value)):
        raise ValueError('Invalid ' + name)
    return value


def _now(value):
    value = time.time() if value is None else value
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid clock')
    return value


def _ttl(value, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError('Invalid expiry')
    return value


@contextmanager
def _atomic(c):
    c.execute('SAVEPOINT duplicate_review_change')
    try:
        yield
    except BaseException:
        c.execute('ROLLBACK TO duplicate_review_change')
        c.execute('RELEASE duplicate_review_change')
        raise
    else:
        c.execute('RELEASE duplicate_review_change')


def setup(c):
    c.execute('''CREATE TABLE IF NOT EXISTS duplicate_review_decisions (
        owner TEXT NOT NULL, first_fingerprint TEXT NOT NULL, second_fingerprint TEXT NOT NULL,
        reason TEXT NOT NULL, recorded REAL NOT NULL, expires REAL NOT NULL,
        PRIMARY KEY(owner,first_fingerprint,second_fingerprint))''')
    c.execute('''CREATE TABLE IF NOT EXISTS duplicate_location_evidence (
        query_hash TEXT NOT NULL, source TEXT NOT NULL, version TEXT NOT NULL,
        evidence TEXT NOT NULL, recorded REAL NOT NULL, expires REAL NOT NULL,
        PRIMARY KEY(query_hash,source,version))''')


def snapshot_fingerprint(snapshot):
    """Hash exact conditions AND row identity; incomplete/unidentified rows fail closed."""
    required = {'row_number', 'delivery_date', 'event_time', 'location', 'sender_summary'}
    if (not isinstance(snapshot, dict) or not required <= snapshot.keys()
            or set(snapshot) - required - {'order_id', 'row_revision'}):
        raise ValueError('Exact order snapshot required')
    if type(snapshot['row_number']) is not int or snapshot['row_number'] < 5:
        raise ValueError('Invalid ledger row')
    identity = [snapshot.get(key) for key in ('order_id', 'row_revision') if snapshot.get(key) is not None]
    if not identity:
        raise ValueError('Immutable order identity or actual source row revision required')
    for item in identity:
        _text(item, 'order identity', 512)
        if re.sub(r'\s+', '', item).casefold() in _MISSING:
            raise ValueError('Actual source order identity required')
    for key in ('location', 'sender_summary'):
        value = _text(snapshot[key], key)
        if value.strip().casefold() in _ERRORS or re.sub(r'\s+', '', value).casefold() in _MISSING:
            raise ValueError('Unreadable order condition')
    for key in ('delivery_date', 'event_time'):
        value = snapshot[key]
        if isinstance(value, str):
            if (not value.strip() or value.strip().casefold() in _ERRORS
                    or re.sub(r'\s+', '', value).casefold() in _MISSING):
                raise ValueError('Missing order condition')
        elif type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('Unreadable order condition')
    return hashlib.sha256(_json(snapshot).encode()).hexdigest()


def _pair(owner, left_fingerprint, right_fingerprint):
    _text(owner, 'authenticated owner', 512)
    for value in (left_fingerprint, right_fingerprint):
        if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
            raise ValueError('Exact snapshot fingerprints required')
    if left_fingerprint == right_fingerprint:
        raise ValueError('Two different order snapshots required')
    return owner, *sorted((left_fingerprint, right_fingerprint))


def record_decision(c, owner, left_fingerprint, right_fingerprint, *, reason,
                    ttl_seconds=86400, now=None):
    """Record this staff member's not-duplicate decision, not a global approval."""
    key = _pair(owner, left_fingerprint, right_fingerprint)
    reason = _text(reason, 'not-duplicate reason', 500)
    stamp = _now(now)
    expires = stamp + _ttl(ttl_seconds, 30 * 86400)
    with _atomic(c):
        c.execute('''INSERT INTO duplicate_review_decisions VALUES (?,?,?,?,?,?)
            ON CONFLICT(owner,first_fingerprint,second_fingerprint)
            DO UPDATE SET reason=excluded.reason,recorded=excluded.recorded,expires=excluded.expires''',
                  (*key, reason, stamp, expires))
    return {'reason': reason, 'recorded': stamp, 'expires': expires}


def get_active_decision(c, owner, left_fingerprint, right_fingerprint, *, now=None):
    key = _pair(owner, left_fingerprint, right_fingerprint)
    stamp = _now(now)
    row = c.execute('''SELECT reason,recorded,expires FROM duplicate_review_decisions
        WHERE owner=? AND first_fingerprint=? AND second_fingerprint=?''', key).fetchone()
    if not row:
        return None
    if not row[1] <= stamp < row[2]:
        c.execute('DELETE FROM duplicate_review_decisions WHERE owner=? AND first_fingerprint=? AND second_fingerprint=?', key)
        return None
    return {'reason': row[0], 'recorded': row[1], 'expires': row[2]}


def is_suppressed(c, owner, left_fingerprint, right_fingerprint, *, now=None):
    return get_active_decision(c, owner, left_fingerprint, right_fingerprint, now=now) is not None


def _cache_key(query, source, version):
    _text(query, 'exact provider query')
    _text(source, 'provider source', 200)
    _text(version, 'provider version', 200)
    return hashlib.sha256(query.encode()).hexdigest(), source, version


def _evidence(query, source, evidence):
    if not isinstance(evidence, dict) or evidence.get('provider') != source:
        raise ValueError('Actual provider evidence required')
    # Reuse the actual comparator's strict contract so the cache cannot silently
    # widen its accepted identity evidence to coordinate-only or ambiguous data.
    _validate_provider_result(query, lambda _: evidence)
    return {key: evidence[key] for key in _EVIDENCE_FIELDS if key in evidence}


def invalidate_location_evidence(c, query, source, version):
    key = _cache_key(query, source, version)
    return bool(c.execute('DELETE FROM duplicate_location_evidence WHERE query_hash=? AND source=? AND version=?', key).rowcount)


def _maintain_location_cache(c, stamp):
    # Cleanup is global to provider evidence, including queries never revisited.
    # Staff decisions are a separate table and are never evicted for cache space.
    c.execute('''DELETE FROM duplicate_location_evidence
        WHERE expires<=? OR recorded>? OR expires<=recorded
        OR typeof(recorded) NOT IN ('real','integer')
        OR typeof(expires) NOT IN ('real','integer')
        OR expires-recorded>?''', (stamp, stamp, CACHE_MAX_TTL_SECONDS))
    c.execute('''DELETE FROM duplicate_location_evidence WHERE rowid IN (
        SELECT rowid FROM duplicate_location_evidence
        ORDER BY recorded DESC,rowid DESC LIMIT -1 OFFSET ?)''', (CACHE_MAX_ENTRIES,))


def put_location_evidence(c, query, source, version, evidence, *, ttl_seconds=3600, now=None):
    """Cache actual results; purge expired evidence and evict oldest above 5,000.

    Any failed/unverified refresh removes old success. Every put also cleans up
    never-revisited expired/future-clock entries; no scheduler is started here.
    """
    key = _cache_key(query, source, version)
    stamp = _now(now)
    expires = stamp + _ttl(ttl_seconds, CACHE_MAX_TTL_SECONDS)
    try:
        payload = _evidence(query, source, evidence)
        serialized = _json(payload)
    except (ValueError, TypeError):
        serialized = None
    with _atomic(c):
        if serialized is None:
            invalidate_location_evidence(c, query, source, version)
        else:
            c.execute('''INSERT INTO duplicate_location_evidence VALUES (?,?,?,?,?,?)
                ON CONFLICT(query_hash,source,version)
                DO UPDATE SET evidence=excluded.evidence,recorded=excluded.recorded,expires=excluded.expires''',
                      (*key, serialized, stamp, expires))
        _maintain_location_cache(c, stamp)
    return serialized is not None


def get_location_evidence(c, query, source, version, *, now=None):
    """Return fresh provider evidence or an explicit needs_review, never an empty hit."""
    key = _cache_key(query, source, version)
    stamp = _now(now)
    row = c.execute('''SELECT evidence,recorded,expires FROM duplicate_location_evidence
        WHERE query_hash=? AND source=? AND version=?''', key).fetchone()
    if not row:
        return {'status': 'needs_review', 'reason': 'not_cached'}
    try:
        if not row[1] <= stamp < row[2]:
            raise ValueError('expired')
        evidence = {**json.loads(row[0]), 'query': query}
        evidence = {**_evidence(query, source, evidence), 'query': query}
    except (ValueError, TypeError):
        invalidate_location_evidence(c, query, source, version)
        return {'status': 'needs_review', 'reason': 'expired_or_invalid'}
    return evidence
