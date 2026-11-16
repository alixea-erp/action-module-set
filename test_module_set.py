import argparse
import json
import os
import tarfile
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
