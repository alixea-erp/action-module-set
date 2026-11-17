"""The decisions of the install check, without Odoo: run with python3 -m unittest"""
import contextlib
import io
import os
import unittest

import module_check


def manifest(depends=(), **values):
    return {'depends': list(depends), **values}


class PlanTest(unittest.TestCase):
    def plan(self, manifests, available=('base', 'web', 'account'), extra=None, missing=None):
        missing = missing or {}
        return module_check.plan(
            manifests,
            set(available),
            extra or {},
            missing_python=lambda package: missing.get(package),
            missing_program=lambda program: missing.get(program),
        )

    def test_modules_on_odoo_alone_go_first_those_needing_another_set_second(self):
        first, second, skipped = self.plan(
            {
                'theme': manifest(['web']),
                'sale_mobile': manifest(['theme', 'account']),
                'reports_fix': manifest(['account_reports']),
                'uses_fix': manifest(['reports_fix']),
            },
            extra={'account_reports': manifest(['account'])},
        )
        self.assertEqual((first, second, skipped), (['theme', 'sale_mobile'], ['reports_fix', 'uses_fix'], {}))

    def test_a_module_whose_dependency_is_nowhere_and_those_that_need_it_are_not_tried(self):
        _first, _second, skipped = self.plan(
            {'a': manifest(['nowhere']), 'b': manifest(['a']), 'c': manifest(['web'])},
        )
        self.assertEqual(skipped, {'a': 'needs nowhere', 'b': 'needs a (not checked either)'})

    def test_not_installable_a_missing_package_and_a_missing_program(self):
        _first, _second, skipped = self.plan(
            {
                'old': manifest(['web'], installable=False),
                'mcp': manifest(['web'], external_dependencies={'python': ['authlib>=1.6']}),
                'pdf': manifest(['web'], external_dependencies={'bin': ['wkhtmltopdf']}),
                'fine': manifest(['web'], external_dependencies={'python': ['lxml']}),
            },
            missing={'authlib>=1.6': 'the Python package authlib>=1.6', 'wkhtmltopdf': 'the program wkhtmltopdf'},
        )
        self.assertEqual(
            skipped,
            {'old': 'not installable', 'mcp': 'needs the Python package authlib>=1.6', 'pdf': 'needs the program wkhtmltopdf'},
        )

    def test_of_two_modules_that_exclude_each_other_the_first_by_name_is_tried(self):
        first, _second, skipped = self.plan(
            {'a_theme': manifest(['web'], excludes=['b_theme']), 'b_theme': manifest(['web']), 'c': manifest(['web'])},
        )
        self.assertEqual((first, skipped), (['a_theme', 'c'], {'b_theme': 'cannot be installed with a_theme'}))

    def test_a_module_odoo_has_is_not_needed_from_the_other_set(self):
        first, second, _skipped = self.plan({'a': manifest(['account'])}, extra={'account': manifest()})
        self.assertEqual((first, second), (['a'], []))


class LogTest(unittest.TestCase):
    LOG = '\n'.join([
        '2026-11-16 13:36:30,001 1074 INFO db odoo.modules.loading: Loading module mail (12/40)',
        '2026-11-16 13:36:33,001 1074 INFO db odoo.modules.loading: Loading module exc_portal_theme (13/40)',
        '2026-11-16 13:36:34,780 1074 ERROR db odoo.registry: Failed to load registry',
        'Traceback (most recent call last):',
        '  File "/opt/odoo/odoo/tools/convert.py", line 61, in load_data',
        "odoo.tools.convert.ParseError: while parsing None:19",
        'Error while parsing or validating view:',
        '',
        "Element '<xpath expr=\"//div[hasclass('container')]\">' cannot be located in parent view",
        '',
        'View error context:',
    ])

    def test_the_module_odoo_was_installing_and_its_error(self):
        self.assertEqual(module_check.failed_module(self.LOG), 'exc_portal_theme')
        self.assertEqual(
            module_check.error_text(self.LOG),
            "odoo.tools.convert.ParseError: while parsing None:19 Error while parsing or validating view: "
            "Element '<xpath expr=\"//div[hasclass('container')]\">' cannot be located in parent view",
        )

    def test_an_exception_without_a_message_says_where_it_came_from(self):
        log = '\n'.join([
            'Traceback (most recent call last):',
            '  File "/set/erpw_approvals/controllers/controller.py", line 53, in VendorInvoiceApproval',
            '  File "/var/lib/odoo/code/current/odoo/http.py", line 739, in decorator',
            "    assert routing.get('type', 'http') in _dispatchers.keys()",
            '           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^',
            'AssertionError',
        ])
        self.assertEqual(
            module_check.error_text(log),
            "AssertionError at assert routing.get('type', 'http') in _dispatchers.keys() from erpw_approvals/controllers/controller.py:53",
        )

    def test_a_module_odoo_refused_for_a_missing_package_is_the_one_that_stopped_it(self):
        log = '\n'.join([
            'INFO db odoo.modules.loading: Loading module account_reports (40/90)',
            'odoo.exceptions.UserError: Unable to install module "l10n_nl_reports" because an external dependency is not met: xmlsec',
        ])
        self.assertEqual(module_check.failed_module(log), 'l10n_nl_reports')

    def test_a_plain_exception_and_a_log_without_one(self):
        self.assertEqual(module_check.error_text('Traceback (most recent call last):\nAssertionError\n'), 'AssertionError')
        self.assertEqual(module_check.failed_module('nothing here'), None)
        self.assertEqual(module_check.error_text('INFO only\n'), 'Odoo stopped without an error in the log')


class FakeOdoo:
    """Installs everything given, in order, and stops at the first module named in fails"""

    def __init__(self, fails=()):
        self.fails = set(fails)
        self.state = {}
        self.runs = []

    def run(self, database, addons, modules, seconds, log_name):
        self.runs.append(list(modules))
        for name in modules:
            if name in self.fails:
                self.state[name] = 'to install'
                return 1, f'INFO Loading module {name} (1/2)\nValueError: {name} is broken\n'
            self.state[name] = 'installed'
        return 0, ''

    def states(self, database, names):
        return {name: self.state[name] for name in names if name in self.state}

    def undo_pending(self, database):
        self.state = {name: state for name, state in self.state.items() if state != 'to install'}


class InstallTest(unittest.TestCase):
    def test_a_failing_module_and_the_ones_that_need_it_are_taken_out_and_the_rest_installed(self):
        manifests = {'a': manifest(['web']), 'broken': manifest(['web']), 'needs_broken': manifest(['broken']), 'z': manifest(['a'])}
        odoo = FakeOdoo(fails={'broken'})
        results = {}
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            module_check.install_all(odoo, 'db', ['set'], list(manifests), manifests, float('inf'), results)
        # The step's log names the module and has Odoo's own lines on why
        self.assertIn('1 installed, then broken failed: ValueError: broken is broken', printed.getvalue())
        self.assertIn('INFO Loading module broken (1/2)', printed.getvalue())
        self.assertEqual(
            results,
            {
                'a': ('installed', ''),
                'broken': ('failed', 'ValueError: broken is broken'),
                'needs_broken': ('not checked', 'needs broken, which failed'),
                'z': ('installed', ''),
            },
        )
        self.assertEqual(odoo.runs, [['a', 'broken', 'needs_broken', 'z'], ['z']])


class ExitCodeTest(unittest.TestCase):
    def test_a_failed_module_then_a_check_out_of_time_then_clean(self):
        installed, failed = ('installed', ''), ('failed', 'ValueError')
        out_of_time = ('not checked', module_check.OUT_OF_TIME)
        needs_other = ('not checked', 'needs broken, which failed')
        self.assertEqual(module_check.exit_code({'a': installed, 'b': failed, 'c': out_of_time}), module_check.EXIT_FAILED)
        self.assertEqual(module_check.exit_code({'a': installed, 'c': out_of_time}), module_check.EXIT_INCOMPLETE)
        self.assertEqual(module_check.exit_code({'a': installed, 'c': needs_other}), 0)
        self.assertEqual(module_check.exit_code({}), 0)


class VisibleTest(unittest.TestCase):
    def test_only_a_set_aside_module_that_installs_by_itself_is_hidden(self):
        manifests = {
            'tried': manifest(['web']),
            'auto_tried': manifest(['web'], auto_install=True),
            'set_aside': manifest(['web']),
            'auto_set_aside': manifest(['account'], auto_install=['account']),
        }
        skipped = {'set_aside': 'needs the Python package pyjwt', 'auto_set_aside': 'needs the Python package xmlsec'}
        self.assertEqual(module_check.visible(manifests, skipped), ['tried', 'auto_tried', 'set_aside'])


class ViewTest(unittest.TestCase):
    def test_only_the_named_modules_are_seen(self):
        import tempfile
        root = tempfile.mkdtemp(prefix='view-')
        for name in ('tried', 'set_aside'):
            os.makedirs(os.path.join(root, 'set', name))
            open(os.path.join(root, 'set', name, '__manifest__.py'), 'w').write('{}')
        view = module_check.addons_view(os.path.join(root, 'set'), ['tried'], os.path.join(root, 'view'))
        self.assertEqual(sorted(module_check.find_manifests(view)), ['tried'])


if __name__ == '__main__':
    unittest.main()


class LineTest(unittest.TestCase):
    def test_the_line_file_names_the_first_failed_modules_after_its_first_line(self):
        import argparse
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            args = argparse.Namespace(summary=os.path.join(folder, 'summary.md'), line=os.path.join(folder, 'line.txt'))
            results = {name: ('failed', f'{name} is broken') for name in 'dcba'} | {'fine': ('installed', '')}
            odoo = argparse.Namespace(version='19.0')
            module_check.write_summary(args, odoo, {name: manifest() for name in results}, results, {}, '')
            with open(args.line) as file:
                lines = file.read().splitlines()
        self.assertEqual(lines[0], '1 of 5 modules install, 4 fail: a, b, c, d.')
        self.assertEqual(lines[1:], ['`a`: a is broken', '`b`: b is broken', '`c`: c is broken', 'and 1 more'])
