import copy
import unittest

from manager_bridge.regression_selection import RegressionSelector, SelectionError


class RegressionSelectionTests(unittest.TestCase):
    def setUp(self):
        self.checks = {
            'authorization_regression': ['/usr/bin/python3', '-m', 'unittest', 'fixture.auth'],
            'approval_regression': ['/usr/bin/python3', '-m', 'unittest', 'fixture.approvals'],
            'assets': ['/usr/bin/python3', '-m', 'unittest', 'fixture.assets'],
            'ui': ['/usr/bin/node', '/fixed/check-ui.js'],
            'full': ['/usr/bin/python3', '-m', 'unittest', 'discover', '-s', 'tests', '-q'],
        }
        self.mapping = {'assets/': ['assets'], 'manager/ui.js': ['ui'], 'manager/': ['approval_regression']}
        self.selector = RegressionSelector(self.checks, self.mapping,
            full_checks=['authorization_regression', 'approval_regression', 'full'])

    def test_known_paths_select_related_and_always_mandatory_without_execution(self):
        plan = self.selector.plan(['assets/a.png', 'manager/ui.js'])
        self.assertEqual([check['name'] for check in plan['checks']],
                         ['authorization_regression', 'approval_regression', 'assets', 'ui'])
        self.assertFalse(plan['commands_executed'])
        self.assertFalse(plan['full_validation_completed'])
        self.assertFalse(plan['full_fallback_selected'])

    def test_unknown_and_nearby_prefix_select_full_fallback(self):
        plan = self.selector.plan(['assets_extra/file.py'])
        self.assertTrue(plan['full_fallback_selected'])
        self.assertEqual(plan['unknown_paths'], ['assets_extra/file.py'])
        self.assertIn('full', [check['name'] for check in plan['checks']])
        self.assertNotIn('assets', [check['name'] for check in plan['checks']])

    def test_paths_cannot_hide_traversal_globs_or_control_characters(self):
        for path in ('../x.py', './assets/a.png', 'assets//a.png', 'assets/', '/tmp/x.py',
                     'assets/*.png', 'assets/../a.png', '.git/config', 'assets/line\nname', 'assets\\x.png'):
            with self.subTest(path=path), self.assertRaises(SelectionError):
                self.selector.plan([path])

    def test_no_change_still_selects_security_and_approval_and_caller_cannot_mutate_registry(self):
        self.checks['authorization_regression'].append('malicious later argument')
        self.mapping['assets/'].append('full')
        plan = self.selector.plan([])
        self.assertEqual(len(plan['checks']), 2)
        self.assertNotIn('malicious later argument', plan['checks'][0]['argv'])
        plan['checks'][0]['argv'].append('caller edit')
        self.assertNotIn('caller edit', self.selector.plan([])['checks'][0]['argv'])

    def test_invalid_registry_cannot_omit_mandatory_checks_or_invent_argv(self):
        for options in ({'required_checks':['assets']}, {'full_checks':['full']}):
            with self.assertRaises(SelectionError):
                RegressionSelector(self.checks, self.mapping, **options)
        with self.assertRaises(SelectionError):
            RegressionSelector(self.checks, {'assets/':['nonexistent']})
        unsafe = copy.deepcopy(self.checks)
        unsafe['assets'] = ['python', '-m', 'unittest']
        with self.assertRaises(SelectionError):
            RegressionSelector(unsafe, self.mapping)


if __name__ == '__main__':
    unittest.main()
