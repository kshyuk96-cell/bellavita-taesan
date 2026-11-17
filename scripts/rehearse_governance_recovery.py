#!/usr/bin/env python3
"""Destructive only to a fresh temporary synthetic DB; no customer paths accepted."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager_bridge.store import Store, Conflict
from manager_bridge.maintenance_registry import Registry
from manager_bridge.three_tier import ThreeTier
from manager_bridge.governance_durability import GovernanceDurability
from manager_bridge.encrypted_backup import EncryptedBackup, PrivateLocalBackupBackend
from manager_bridge.recovery_catalog import RecoveryCatalog, RecoveryTrust
from manager_bridge.operating_budget import OperatingBudget, BudgetPolicy


def rehearse():
    # No credential paths, destination selectors, transport or real registry inputs.
    with tempfile.TemporaryDirectory(prefix='taesan-synthetic-recovery-') as folder:
        root = Path(folder)
        root.chmod(0o700)
        for label in ('objects', 'catalog', 'staging', 'recovery'):
            (root/label).mkdir(mode=0o700)
        store = Store(root/'synthetic.sqlite3')
        registry = Registry(store)
        owner = store.add_token('synthetic-owner', 'owner', ['*'])
        registry.register(owner, 'synthetic', {
            'company': 'synthetic', 'name': 'Recovery drill',
            'source_kind': 'github', 'source_ref': 'synthetic/recovery',
            'branch': 'main', 'services': [], 'maintenance_enabled': False})
        core = ThreeTier(store, registry)
        OperatingBudget(core).install(owner, 'synthetic', BudgetPolicy(1, 2, 60))
        encryption_key, trust_key = os.urandom(32), os.urandom(32)
        objects = PrivateLocalBackupBackend(root/'objects')
        encrypted = EncryptedBackup(GovernanceDurability(core), objects,
                                    lambda key_id: encryption_key, root/'staging')
        encrypted.backup(owner, 'synthetic-drill', 'ephemeral-key')
        trust = RecoveryTrust('synthetic-recovery', lambda: trust_key,
            lambda assertion, scope: 'synthetic-owner' if assertion is owner else None,
            lambda subject, scope: subject == 'synthetic-owner')
        catalog = RecoveryCatalog(trust, PrivateLocalBackupBackend(root/'catalog'),
                                 objects, lambda key_id: encryption_key, root/'recovery')
        catalog.publish(owner, encrypted, 'synthetic-drill')
        raw = (root/'objects/synthetic-drill.aead').read_bytes()
        plaintext_absent = all(x not in raw for x in
                               (b'SQLite format', owner.encode(), encryption_key))
        destination = root/'recovered.sqlite3'
        grant = trust.issue_grant(owner, 'synthetic-drill', destination)
        store.path.unlink()
        original_absent = not store.path.exists()
        restored = catalog.restore(grant)
        with sqlite3.connect(destination) as connection:
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            quarantined = connection.execute(
                'SELECT reconciliation_required FROM governance_restore_marker').fetchone()[0] == 1
        recovered_store = Store(destination)
        recovered_core = ThreeTier(recovered_store, Registry(recovered_store))
        dispatch_denied = not OperatingBudget(recovered_core).check(owner, 'synthetic')['allowed']
        # Remove only this drill's output, then verify that its consumed grant
        # cannot be replayed even to the now-absent original destination.
        destination.unlink()
        replay_denied = False
        try:
            catalog.restore(grant)
        except Conflict:
            replay_denied = not destination.exists()
        checks = {'ciphertext_excludes_known_plaintext': plaintext_absent,
                  'original_database_absent_at_restore': original_absent,
                  'restored_integrity_valid': integrity,
                  'restored_authority_quarantined': quarantined,
                  'new_dispatch_denied': dispatch_denied,
                  'consumed_grant_replay_denied': replay_denied,
                  'recovery_staging_clean': not any((root/'recovery').iterdir())}
        return {'scope': 'temporary_synthetic_local_recovery_only',
                'passed': all(checks.values()), 'checks': checks,
                'offsite_verified': False, 'always_on_verified': False,
                'customer_data_accessed': False, 'automatic_replay': False,
                'restored_state': restored['state']}


def main():
    if len(sys.argv) != 1:
        print('No external paths or arguments accepted.', file=sys.stderr)
        return 2
    try:
        result = rehearse()
    except Exception:
        # Exceptions may originate in key/transport implementations; no raw text.
        print(json.dumps({'passed': False, 'reason': 'synthetic_recovery_drill_failed'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
