"""The build: python3 -m unittest. Needs python-minifier, as the Test workflow installs"""
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest

THING = '''"""The module's docstring"""
import sys


def total(values, scale=1):
    """Adds the values"""
    running_total = 0
    for value in values:
        running_total += value * scale
    return running_total


def python_tag():
    return sys.implementation.cache_tag
'''


@unittest.skipUnless(importlib.util.find_spec('python_minifier'), 'python-minifier is not installed')
class BuildTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='build-')
        self.module = os.path.join(self.root, 'set', 'built_module')
        files = {
            '__manifest__.py': "{'name': 'Built', 'version': '19.0.1.0.0'}\n",
            '__init__.py': '# Odoo needs this file\nfrom . import models\n',
            'models/__init__.py': 'from . import thing\n',
            'models/thing.py': THING,
            'models/broken.py': 'def broken(:\n',
            'migrations/19.0.1.1.0/post-migrate.py': 'def migrate(cr, version):\n    pass\n',
            'static/src/tool.py': 'print("for the browser")\n',
        }
        for path, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.module, path)), exist_ok=True)
            with open(os.path.join(self.module, path), 'w') as file:
                file.write(text)
        with open(os.path.join(self.root, 'set', 'manifest.json'), 'w') as file:
            json.dump({'tag': '19.0/1.0.0', 'modules': ['built_module'], 'installModules': []}, file)

    def test_python_becomes_bytecode_and_what_odoo_finds_by_name_stays(self):
        import module_build

        counts, left = module_build.build(os.path.join(self.root, 'set'))

        self.assertEqual(counts, {'bytecode': 2, 'source': 1})
        self.assertEqual(left, ['built_module/models/broken.py'])
        found = sorted(os.path.relpath(os.path.join(folder, name), self.module) for folder, _, names in os.walk(self.module) for name in names)
        self.assertEqual(found, [
            '__init__.py',
            '__manifest__.py',
            'migrations/19.0.1.1.0/post-migrate.py',
            'models/__init__.pyc',
            'models/broken.py',
            'models/thing.pyc',
            'static/src/tool.py',
        ])
        with open(os.path.join(self.module, '__init__.py')) as file:
            self.assertNotIn('#', file.read())
        with open(os.path.join(self.root, 'set', 'manifest.json')) as file:
            self.assertEqual(json.load(file)['pythonTag'], sys.implementation.cache_tag)

        # Imports from the bytecode alone, without the docstrings
        sys.path.insert(0, os.path.join(self.root, 'set'))
        try:
            thing = importlib.import_module('built_module.models.thing')
            self.assertEqual(thing.total([1, 2, 3], scale=2), 12)
            self.assertIsNone(thing.__doc__)
            self.assertIsNone(thing.total.__doc__)
        finally:
            sys.path.remove(os.path.join(self.root, 'set'))
            for name in [name for name in sys.modules if name.startswith('built_module')]:
                del sys.modules[name]

    def test_the_same_source_gives_the_same_bytes(self):
        import module_build

        path = os.path.join(self.module, 'models', 'thing.py')
        module_build.build_file(path, 'built_module/models/thing.py')
        with open(path + 'c', 'rb') as file:
            first = file.read()
        with open(path, 'w') as file:
            file.write(THING)
        module_build.build_file(path, 'built_module/models/thing.py')
        with open(path + 'c', 'rb') as file:
            self.assertEqual(file.read(), first)


if __name__ == '__main__':
    unittest.main()
