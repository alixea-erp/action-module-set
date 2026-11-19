import argparse
import json
import os
import tarfile
import tempfile
import unittest
import unittest.mock

import module_set


def write(path, content=''):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as file:
        file.write(content)


def make_module(root, name, depends=(), python=(), installable=True, excludes=(), status=None):
    manifest = {'name': name, 'depends': list(depends), 'installable': installable, 'external_dependencies': {'python': list(python)}}
    if excludes:
        manifest['excludes'] = list(excludes)
    if status:
        manifest['development_status'] = status
    write(os.path.join(root, name, '__manifest__.py'), repr(manifest))
    write(os.path.join(root, name, '__init__.py'))
    return os.path.join(root, name)


class ReadList(unittest.TestCase):
    def read(self, text):
        with tempfile.TemporaryDirectory() as folder:
            write(os.path.join(folder, 'list.txt'), text)
            return module_set.read_list(os.path.join(folder, 'list.txt'))

    def test_modules_installs_and_comments(self):
        entries = self.read(f'# picked\n\nOCA/web - +web_responsive web_tree   # why\nOCA/server-ux {"a" * 40} date_range\n')

        self.assertEqual([entry['repository'] for entry in entries], ['OCA/web', 'OCA/server-ux'])
        self.assertEqual(entries[0]['modules'], ['web_responsive', 'web_tree'])
        self.assertEqual(entries[0]['install'], ['web_responsive'])
        self.assertEqual(entries[0]['line'], 3)
        self.assertEqual(entries[1]['commit'], 'a' * 40)

    def test_every_module_of_a_repository_and_a_repository_for_dependencies_only(self):
        entries = self.read('OCA/bank-payment - *\nOCA/web - * +web_responsive\nOCA/queue -  # only what the others need\n')

        self.assertEqual([(entry['all'], entry['modules'], entry['install']) for entry in entries], [
            (True, [], []),
            (True, ['web_responsive'], ['web_responsive']),
            (False, [], []),
        ])

    def test_refuses_what_is_not_a_repository_commit_and_module(self):
        for text in ['OCA/web\n', 'OCA/web main web_responsive\n', 'https://github.com/OCA/web - web_responsive\n', 'OCA/web - web-responsive\n', 'OCA/web - **\n', '# nothing\n']:
            with self.assertRaises(SystemExit):
                self.read(text)


class Pick(unittest.TestCase):
    manifests = {
        'stable': {},
        'beta': {'development_status': 'Beta'},
        'alpha': {'development_status': 'Alpha'},
        'old': {'installable': False},
    }

    def test_a_star_takes_what_is_installable_and_not_alpha(self):
        names, alpha = module_set.pick({'all': True, 'modules': []}, self.manifests)

        self.assertEqual(sorted(names), ['beta', 'stable'])
        self.assertEqual(alpha, ['alpha'])

    def test_a_named_module_is_taken_also_when_alpha(self):
        names, alpha = module_set.pick({'all': True, 'modules': ['alpha']}, self.manifests)

        self.assertEqual(sorted(names), ['alpha', 'beta', 'stable'])
        self.assertEqual(alpha, [])

    def test_without_a_star_only_the_named_ones_and_none_for_a_dependency_line(self):
        self.assertEqual(module_set.pick({'all': False, 'modules': ['beta']}, self.manifests), (['beta'], []))
        self.assertEqual(module_set.pick({'all': False, 'modules': []}, self.manifests), ([], []))


class Pin(unittest.TestCase):
    def test_only_the_commit_changes_comments_and_spacing_stay(self):
        text = '# Bank payments\nOCA/bank-payment - *   # SEPA and mandates\n\nOCA/queue ' + 'a' * 40 + '  # for queue_job\n'
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, '19.0.txt')
            write(path, text)
            original = module_set.git
            module_set.git = lambda *args, **kwargs: 'b' * 40 + '\trefs/heads/19.0'
            try:
                module_set.oca_pin(argparse.Namespace(list=path, series='19.0', newest=False))
            finally:
                module_set.git = original

            with open(path) as file:
                self.assertEqual(file.read(), text.replace('OCA/bank-payment -', 'OCA/bank-payment ' + 'b' * 40))


class Choose(unittest.TestCase):
    def test_adds_dependencies_of_dependencies_and_reports_the_rest(self):
        with tempfile.TemporaryDirectory() as root:
            paths = {
                'report': make_module(root, 'report', depends=['base', 'range_account'], python=['pyzbar']),
                'range_account': make_module(root, 'range_account', depends=['account', 'range']),
                'range': make_module(root, 'range', depends=['web']),
                'unused': make_module(root, 'unused'),
            }

            chosen, added, outside, python, excludes, unusable = module_set.choose(paths, ['report'])

        self.assertEqual(chosen, {'report', 'range_account', 'range'})
        self.assertEqual(added, {'range_account', 'range'})
        self.assertEqual(outside, {'base', 'account', 'web'})
        self.assertEqual(python, {'pyzbar'})
        self.assertEqual(excludes, {})
        self.assertEqual(unusable, {})

    def test_names_the_modules_a_chosen_one_cannot_be_installed_with(self):
        with tempfile.TemporaryDirectory() as root:
            paths = {'responsive': make_module(root, 'responsive', excludes=['web_enterprise']), 'other': make_module(root, 'other')}

            _chosen, _added, _outside, _python, excludes, _unusable = module_set.choose(paths, ['responsive', 'other'])

        self.assertEqual(excludes, {'responsive': ['web_enterprise']})

    def test_a_picked_dependency_is_not_reported_as_added(self):
        with tempfile.TemporaryDirectory() as root:
            paths = {'a': make_module(root, 'a', depends=['b']), 'b': make_module(root, 'b')}

            chosen, added, _outside, _python, _excludes, _unusable = module_set.choose(paths, ['a', 'b'])

        self.assertEqual(chosen, {'a', 'b'})
        self.assertEqual(added, set())

    def test_a_dependency_that_is_not_installable_is_left_out_and_named(self):
        with tempfile.TemporaryDirectory() as root:
            paths = {'a': make_module(root, 'a', depends=['old']), 'old': make_module(root, 'old', installable=False)}

            chosen, added, _outside, _python, _excludes, unusable = module_set.choose(paths, ['a'])

        self.assertEqual(chosen, {'a'})
        self.assertEqual(added, set())
        self.assertEqual(unusable, {'old': {'a'}})

    def test_refuses_a_module_that_is_not_installable(self):
        with tempfile.TemporaryDirectory() as root:
            paths = {'old': make_module(root, 'old', installable=False)}

            with self.assertRaises(SystemExit):
                module_set.choose(paths, ['old'])


class Pack(unittest.TestCase):
    def pack(self, tree, archive, tag='19.0/1.4.0', install=None):
        module_set.pack(argparse.Namespace(tree=tree, archive=archive, tag=tag, commit='c' * 40, install=install))
        with tarfile.open(archive) as tar:
            return tar.getmembers(), json.load(tar.extractfile('set/manifest.json'))

    def test_one_top_folder_owner_root_and_only_what_odoo_needs(self):
        with tempfile.TemporaryDirectory() as root:
            tree = os.path.join(root, 'tree')
            make_module(tree, 'sale_custom')
            write(os.path.join(tree, 'sale_custom', 'tests', 'test_sale.py'))
            write(os.path.join(tree, 'sale_custom', 'static', 'tests', 'sale.test.js'))
            write(os.path.join(tree, 'sale_custom', '__pycache__', 'x.pyc'))
            os.symlink('/etc/passwd', os.path.join(tree, 'sale_custom', 'link'))
            write(os.path.join(tree, 'README.md'))
            write(os.path.join(tree, 'manifest.json'), json.dumps({'installModules': ['sale_custom']}))

            members, manifest = self.pack(tree, os.path.join(root, 'set.tar.gz'))

        names = {member.name for member in members}
        self.assertTrue(all(name == 'set' or name.startswith('set/') for name in names))
        self.assertIn('set/sale_custom/__manifest__.py', names)
        self.assertIn('set/sale_custom/static/tests/sale.test.js', names)
        self.assertNotIn('set/sale_custom/tests/test_sale.py', names)
        self.assertNotIn('set/sale_custom/__pycache__/x.pyc', names)
        self.assertNotIn('set/sale_custom/link', names)
        self.assertNotIn('set/README.md', names)
        self.assertTrue(all(member.uid == 0 and member.gid == 0 for member in members))
        self.assertEqual(manifest['modules'], ['sale_custom'])
        self.assertEqual(manifest['installModules'], ['sale_custom'])
        self.assertEqual(manifest['tag'], '19.0/1.4.0')

    def test_install_argument_wins_over_the_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            tree = os.path.join(root, 'tree')
            make_module(tree, 'web_enterprise')
            make_module(tree, 'account_accountant')
            write(os.path.join(tree, 'manifest.json'), json.dumps({'installModules': ['account_accountant']}))

            _members, manifest = self.pack(tree, os.path.join(root, 'set.tar.gz'), install='web_enterprise')

        self.assertEqual(manifest['installModules'], ['web_enterprise'])
        self.assertEqual(manifest['modules'], ['account_accountant', 'web_enterprise'])

    def test_refuses_a_bad_tag_a_missing_install_module_and_an_empty_tree(self):
        with tempfile.TemporaryDirectory() as root:
            tree = os.path.join(root, 'tree')
            make_module(tree, 'a')
            empty = os.path.join(root, 'empty')
            os.makedirs(empty)
            archive = os.path.join(root, 'set.tar.gz')

            for arguments in [{'tree': tree, 'tag': '19.0/main'}, {'tree': tree, 'tag': 'main'}, {'tree': tree, 'install': 'b'}, {'tree': empty}]:
                with self.assertRaises(SystemExit):
                    self.pack(archive=archive, **{'tree': tree, **arguments})


TEMPLATE = '''msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"

#. module: pay
#: model:ir.model.fields,field_description:pay.field_a
msgid "Payment Order"
msgstr ""

#. module: pay
#: model:ir.model.fields,help:pay.field_a
msgid ""
"A long "
"term"
msgstr ""

#. module: pay
#: model:ir.model,name:pay.model_a
msgid "Journal Item"
msgstr ""

#. module: base
#: model:ir.module.module,description:base.module_pay
msgid "The README of the module"
msgstr ""
'''


class Translations(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.tree = os.path.join(self.work.name, 'tree')
        self.ours = os.path.join(self.work.name, '19.0')
        self.module = make_module(self.tree, 'pay')
        write(os.path.join(self.module, 'i18n', 'pay.pot'), TEMPLATE)

    def test_reads_entries_and_leaves_out_the_header_and_fuzzy_and_obsolete_ones(self):
        path = os.path.join(self.work.name, 'fi.po')
        write(path, 'msgid ""\nmsgstr ""\n"Language: fi\\n"\n\nmsgid ""\n"A long "\n"term"\nmsgstr "Pitkä"\n'
                    '#, fuzzy\nmsgid "Guess"\nmsgstr "Arvaus"\n\n#~ msgid "Old"\n#~ msgstr "Vanha"\nmsgid "Empty"\nmsgstr ""\n')
        self.assertEqual(module_set.read_po(path), {'A long term': 'Pitkä', 'Empty': ''})

    def test_a_line_that_is_not_gettext_is_named(self):
        path = os.path.join(self.work.name, 'fi.po')
        write(path, 'msgid "Payment Order"\nmsgstr "Maksu\n')
        with self.assertRaisesRegex(module_set.PoError, r'fi\.po:2: not a line of a translation file'):
            module_set.read_po(path)

    def test_ours_go_into_the_module_next_to_a_copy_of_its_template(self):
        write(os.path.join(self.ours, 'pay', 'i18n_extra', 'fi.po'), 'msgid "Payment Order"\nmsgstr "Maksumääräys"\n')
        write(os.path.join(self.ours, 'gone', 'i18n_extra', 'fi.po'), 'msgid "x"\nmsgstr "y"\n')
        self.assertEqual(module_set.add_translations(self.tree, self.ours), ['pay'])
        self.assertEqual(sorted(os.listdir(os.path.join(self.module, 'i18n_extra'))), ['fi.po', 'pay.pot'])
        self.assertEqual(module_set.add_translations(self.tree, os.path.join(self.work.name, 'no-such-folder')), [])

    def test_a_translation_file_that_does_not_parse_stops_the_release(self):
        write(os.path.join(self.ours, 'pay', 'i18n_extra', 'fi.po'), 'msgid "Payment Order"\nmsgstr Maksu\n')
        with self.assertRaises(SystemExit):
            module_set.add_translations(self.tree, self.ours)

    def test_missing_is_what_neither_the_module_nor_we_translate_without_the_readme(self):
        self.assertEqual(module_set.missing_translations(self.module, 'fi'), ['Payment Order', 'A long term', 'Journal Item'])
        write(os.path.join(self.module, 'i18n', 'fi.po'), 'msgid "Payment Order"\nmsgstr "Maksu"\n\nmsgid "Journal Item"\nmsgstr ""\n')
        write(os.path.join(self.ours, 'pay', 'i18n_extra', 'fi.po'), 'msgid ""\n"A long term"\nmsgstr "Pitkä"\n')
        module_set.add_translations(self.tree, self.ours)
        self.assertEqual(module_set.missing_translations(self.module, 'fi'), ['Journal Item'])
        self.assertEqual(module_set.missing_translations(self.module, 'sv'), ['Payment Order', 'A long term', 'Journal Item'])
        self.assertIsNone(module_set.missing_translations(make_module(self.tree, 'bare'), 'fi'))

    def test_the_report_writes_the_line_for_slack_only_when_terms_are_missing(self):
        line = os.path.join(self.work.name, 'line.txt')
        module_set.report_translations(self.tree, ['fi', 'sv'], [], line)
        with open(line) as file:
            self.assertEqual(file.read(), 'Translations missing: fi 3, sv 3 terms. The summary of the run lists them.\n')
        os.remove(line)
        for language in ('fi', 'sv'):
            write(os.path.join(self.module, 'i18n', f'{language}.po'),
                  'msgid "Payment Order"\nmsgstr "a"\n\nmsgid "A long term"\nmsgstr "b"\n\nmsgid "Journal Item"\nmsgstr "c"\n')
        module_set.report_translations(self.tree, ['fi', 'sv'], [], line)
        self.assertFalse(os.path.exists(line))


    def test_the_summary_lists_the_missing_terms_folded_by_language_and_module(self):
        write(os.path.join(self.module, 'i18n', 'pay.pot'), TEMPLATE + '\n#. module: pay\nmsgid "<p>Hello</p>"\nmsgstr ""\n')
        path = os.path.join(self.work.name, 'summary.md')
        with unittest.mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': path}):
            module_set.report_translations(self.tree, ['fi'], ['pay'], None)
            with open(path) as file:
                text = file.read()
            self.assertIn('| fi | 4 | 1 of 1 |', text)
            self.assertIn('<details><summary>fi: 4 missing terms in 1 modules</summary>', text)
            self.assertIn('<details><summary>pay: 4</summary>\n\n```text\nPayment Order\nA long term\nJournal Item\n<p>Hello</p>\n```', text)

            # Past the size a summary may have, only the modules and their numbers
            os.remove(path)
            with unittest.mock.patch.object(module_set, 'TRANSLATIONS_SUMMARY_BYTES', 50):
                module_set.report_translations(self.tree, ['fi'], [], None)
            with open(path) as file:
                text = file.read()
            self.assertIn('- pay: 4', text)
            self.assertNotIn('Payment Order', text)
            self.assertIn('Too many terms are missing to list them here', text)


if __name__ == '__main__':
    unittest.main()
