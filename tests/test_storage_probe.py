import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('storage_probe',Path(__file__).resolve().parents[1]/'tools/check_shop_storage.py');probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


class StorageProbeTest(unittest.TestCase):
    def test_existing_database_is_inspected_without_mutation_or_customer_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.sqlite3'
            with sqlite3.connect(path) as c:
                c.execute('CREATE TABLE invitation_orders(id TEXT,details TEXT)');c.execute('INSERT INTO invitation_orders VALUES (?,?)',('private-order',json.dumps({'buyer':'private-buyer','buyer_phone':'01012345678'})))
                c.execute('CREATE TABLE shop_products(details TEXT)');c.execute('INSERT INTO shop_products VALUES (?)',(json.dumps({'public':True,'title':'private-title','cash_tax_type':''}),))
            before=hashlib.sha256(path.read_bytes()).hexdigest();report=probe.inspect(tmp)
            self.assertEqual(report['integrity'],'ok');self.assertEqual(report['counts']['invitation_orders'],1);self.assertIsNone(report['counts']['shop_messages']);self.assertEqual(report['public_catalog']['tax_type_missing_count'],1)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before);self.assertFalse(report['application_started']);self.assertFalse(report['database_writes']);self.assertEqual(report['external_requests'],0)
            output=json.dumps(report);self.assertNotIn('private-buyer',output);self.assertNotIn('private-order',output);self.assertNotIn('private-title',output);self.assertNotIn('01012345678',output)

    def test_missing_path_never_creates_data_or_an_empty_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing=Path(tmp)/'missing'
            with self.assertRaises((ValueError,OSError)):probe.inspect(str(missing))
            self.assertFalse(missing.exists())
            with self.assertRaises(ValueError):probe.inspect(tmp)
            self.assertFalse((Path(tmp)/'state.sqlite3').exists())
            with self.assertRaises(ValueError):probe.inspect(None)
