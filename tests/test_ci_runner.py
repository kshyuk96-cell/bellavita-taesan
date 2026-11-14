import unittest
from unittest.mock import patch
from validation.run_ci import run_script

class RunnerTests(unittest.TestCase):
    def test_successful_script_system_exit_does_not_stop_other_checks(self):
        with patch('validation.run_ci.runpy.run_path',side_effect=SystemExit(0)):run_script('synthetic.py')
    def test_nonzero_script_exit_fails_the_job(self):
        with patch('validation.run_ci.runpy.run_path',side_effect=SystemExit(1)),self.assertRaises(RuntimeError):run_script('synthetic.py')
