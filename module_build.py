#!/usr/bin/env python3
"""Builds a staged module set in place: Python as bytecode only (the platform's docs/OdooModules.md, phase 5). It runs
inside Alixea's Odoo image of the set's series, so the bytecode is for the Python that runs it, with python-minifier
installed there.

  module_build.py <folder>

Every Python file of every module is minified, without docstrings and with local names renamed, compiled next to
where it was and deleted. Odoo then imports the bytecode. Kept as source: each __manifest__.py, which Odoo reads as
text, migrations/ and upgrades/, whose scripts Odoo finds by their .py name, and static/, which is the browser's. A
module's own __init__.py stays too, minified: Odoo takes a folder for a module only when it has one.
manifest.json gets pythonTag, the Python the bytecode is for. Prints its part of the run's summary.
"""
import json
import os
import py_compile
import sys

import python_minifier

# Odoo finds these by their file names
KEPT_FOLDERS = {'migrations', 'upgrades', 'static'}

# Annotations stay: dataclasses need them. Globals keep their names: Odoo finds models, fields and methods by name.
# Arguments keep theirs anyway, as they may be passed by keyword, and nothing is renamed in a file that calls locals()
MINIFY = {
    'remove_literal_statements': True,
    'rename_locals': True,
    'rename_globals': False,
    'remove_annotations': False,
    'hoist_literals': False,
    'remove_asserts': False,
    'remove_debug': False,
}


def python_files(module_path):
    """The module's Python files to build, as paths relative to the module"""
    for folder, folders, names in os.walk(module_path):
        relative = os.path.relpath(folder, module_path)
        if relative == '.':
            folders[:] = [name for name in folders if name not in KEPT_FOLDERS]
        for name in sorted(names):
            path = os.path.normpath(os.path.join(relative, name))
            if name.endswith('.py') and path != '__manifest__.py':
                yield path


def build_file(path, shown_as, keep_source=False):
    """Replaces the file with its bytecode, or with its minified source: says which. None when it is not Python this
    interpreter reads: it stays as it is"""
    with open(path, encoding='utf-8') as file:
        source = file.read()
    try:
        minified = python_minifier.minify(source, filename=shown_as, **MINIFY)
    except (SyntaxError, UnicodeError, ValueError):
        return None

    with open(path, 'w', encoding='utf-8') as file:
        file.write(minified)
    if keep_source:
        return 'source'
    # Only the source's hash is in the file, no time: an unchanged module has the same bytes in the next version
    py_compile.compile(path, cfile=path + 'c', dfile=shown_as, doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    os.remove(path)
    return 'bytecode'


def build(folder):
    with open(os.path.join(folder, 'manifest.json')) as file:
        manifest = json.load(file)

    counts, left = {'bytecode': 0, 'source': 0}, []
    for name in manifest['modules']:
        module_path = os.path.join(folder, name)
        for relative in python_files(module_path):
            outcome = build_file(os.path.join(module_path, relative), f'{name}/{relative}', keep_source=relative == '__init__.py')
            if outcome:
                counts[outcome] += 1
            else:
                left.append(f'{name}/{relative}')

    manifest['pythonTag'] = sys.implementation.cache_tag
    with open(os.path.join(folder, 'manifest.json'), 'w') as file:
        json.dump(manifest, file, indent=2)

    print('### Build')
    print(f'{len(manifest["modules"])} modules for {manifest["pythonTag"]}: {counts["bytecode"]} Python files as bytecode, {counts["source"]} minified (their own `__init__.py`)')
    if left:
        print(f'- Left as source, as Python {sys.version_info.major}.{sys.version_info.minor} cannot read them: {", ".join(left[:10])}{" …" if len(left) > 10 else ""}')
    return counts, left


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    build(sys.argv[1])
