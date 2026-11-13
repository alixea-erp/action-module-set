#!/usr/bin/env python3
"""Builds the archive of one version of a module set (the platform's docs/OdooModules.md, phase 4). The actions of
this repository run it.

  module_set.py oca-pin <list> <series> [--newest]
      Fills in the commit of every line whose commit is "-" (every line with --newest) from the newest commit of the
      series branch, and rewrites the list.
  module_set.py oca-collect <list> <tree>
      Fetches each listed repository at its commit and copies the picked modules, and the modules of the listed
      repositories they depend on, into <tree>.
  module_set.py pack <tree> <archive> --tag <tag> --commit <commit> [--install a,b]
      Copies every module of <tree> into a gzipped tar with one top folder and writes its manifest.json.
  module_set.py odoo-release <series>
      Prints the release date of Docker Hub's odoo:<series>, such as 20261112.

The OCA list has one repository per line: "<owner>/<repo> <commit or -> <module> [<module> ...]", "#" for comments.
A module written as "+name" is also installed when the set is turned on.
"""
import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone

# Not needed at run time: Odoo imports no module's tests, and Python compiles again
SKIPPED_NAMES = {'.git', '__pycache__'}


def fail(message):
    print(f'::error::{message}', file=sys.stderr)
    sys.exit(1)


def summary(lines):
    text = '\n'.join(lines) + '\n'
    print(text, end='')
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a') as file:
            file.write(text)


def git(*args, cwd=None):
    result = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        fail(f'git {" ".join(args)}: {result.stderr.strip()}')
    return result.stdout.strip()


def read_manifest(module_path):
    with open(os.path.join(module_path, '__manifest__.py')) as file:
        return ast.literal_eval(file.read())


def find_modules(folder):
    """The module folders directly in the folder, by name"""
    return {
        name: os.path.join(folder, name)
        for name in sorted(os.listdir(folder))
        if os.path.isfile(os.path.join(folder, name, '__manifest__.py'))
    }


def read_list(path):
    entries = []
    with open(path) as file:
        for number, raw in enumerate(file, 1):
            line = raw.split('#', 1)[0].split()
            if not line:
                continue
            if len(line) < 3 or not re.fullmatch(r'[\w.-]+/[\w.-]+', line[0]) or not re.fullmatch(r'-|[0-9a-f]{40}', line[1]):
                fail(f'{path}:{number}: expected "<owner>/<repo> <commit or -> <module> ...", got "{raw.strip()}"')
            modules = [name.lstrip('+') for name in line[2:]]
            install = [name[1:] for name in line[2:] if name.startswith('+')]
            entries.append({'line': number, 'repository': line[0], 'commit': line[1], 'modules': modules, 'install': install})
    if not entries:
        fail(f'{path} lists no repository')
    return entries


def oca_pin(args):
    entries = read_list(args.list)
    with open(args.list) as file:
        lines = file.readlines()

    changes = []
    for entry in entries:
        if entry['commit'] != '-' and not args.newest:
            continue
        found = git('ls-remote', f'https://github.com/{entry["repository"]}.git', f'refs/heads/{args.series}')
        if not found:
            fail(f'{entry["repository"]} has no branch {args.series}')
        commit = found.split()[0]
        if commit != entry['commit']:
            changes.append(f'- `{entry["repository"]}`: {entry["commit"][:12]} → {commit[:12]}')
        # Only the commit field changes, comments and spacing stay
        index = entry['line'] - 1
        lines[index] = re.sub(r'^(\s*\S+\s+)(\S+)', lambda match: match.group(1) + commit, lines[index], count=1)

    with open(args.list, 'w') as file:
        file.writelines(lines)
    summary(['### Pins', *(changes or ['No pin changed'])])


def oca_collect(args):
    entries = read_list(args.list)
    with tempfile.TemporaryDirectory(prefix='oca-') as work:
        collect(entries, work, args.tree)


def collect(entries, work, tree):
    index = {}
    for entry in entries:
        if entry['commit'] == '-':
            fail(f'{entry["repository"]} has no commit, run oca-pin first')
        clone = os.path.join(work, entry['repository'].replace('/', '__'))
        os.makedirs(clone)
        git('init', '-q', cwd=clone)
        git('fetch', '-q', '--depth', '1', f'https://github.com/{entry["repository"]}.git', entry['commit'], cwd=clone)
        git('checkout', '-q', 'FETCH_HEAD', cwd=clone)
        for name, path in find_modules(clone).items():
            if name in index:
                fail(f'Module {name} is in {index[name]["repository"]} and {entry["repository"]}')
            index[name] = {'path': path, 'repository': entry['repository']}
        for name in entry['modules']:
            if index.get(name, {}).get('repository') != entry['repository']:
                fail(f'{entry["repository"]} has no module {name} at {entry["commit"][:12]}')

    picked = [name for entry in entries for name in entry['modules']]
    chosen, added, outside, python = choose({name: found['path'] for name, found in index.items()}, picked)

    os.makedirs(tree, exist_ok=True)
    for name in sorted(chosen):
        shutil.copytree(index[name]['path'], os.path.join(tree, name), symlinks=True, ignore=shutil.ignore_patterns('.git'))
    # pack reads the modules to install from here
    with open(os.path.join(tree, 'manifest.json'), 'w') as file:
        json.dump({'installModules': [name for entry in entries for name in entry['install']]}, file)

    lines = ['### Modules', f'{len(picked)} picked, {len(added)} added as dependencies: {len(chosen)} in the set']
    if added:
        lines.append(f'- Added: {", ".join(sorted(added))}')
    if outside:
        lines.append(f'- Needed from outside the list (Odoo, Enterprise, or missing here): {", ".join(sorted(outside))}')
    if python:
        lines.append(f'- **Python packages the image may lack**: {", ".join(sorted(python))}')
    summary(lines)


def choose(paths, picked):
    """The picked modules with every module of `paths` (name to folder) they depend on. Returns the chosen names, the
    ones added as dependencies, the dependencies `paths` does not have, and the Python packages the chosen need"""
    chosen, added, outside, python = set(), set(), set(), set()
    pending = list(picked)
    while pending:
        name = pending.pop()
        if name in chosen:
            continue
        chosen.add(name)
        manifest = read_manifest(paths[name])
        if not manifest.get('installable', True):
            fail(f'{name} is not installable in this series')
        python.update(manifest.get('external_dependencies', {}).get('python', []))
        for dependency in manifest.get('depends', []):
            if dependency not in paths:
                outside.add(dependency)
                continue
            if dependency not in picked:
                added.add(dependency)
            pending.append(dependency)
    return chosen, added, outside, python


def pack(args):
    if not re.fullmatch(r'\d+\.\d+/\d+(\.\d+)*', args.tag):
        fail(f'The tag "{args.tag}" is not <series>/<version>, such as 19.0/1.4.0')
    modules = find_modules(args.tree)
    if not modules:
        fail(f'No module in {args.tree}')

    install = [name for name in (args.install or '').split(',') if name]
    if args.install is None and os.path.isfile(os.path.join(args.tree, 'manifest.json')):
        with open(os.path.join(args.tree, 'manifest.json')) as file:
            install = json.load(file).get('installModules', [])
    missing = [name for name in install if name not in modules]
    if missing:
        fail(f'Modules to install that the set does not have: {", ".join(missing)}')

    with tempfile.TemporaryDirectory(prefix='set-') as work:
        links = write_archive(args, modules, install, work)

    size = os.path.getsize(args.archive)
    lines = ['### Archive', f'{len(modules)} modules, {size / 1024 / 1024:.1f} MiB, installs: {", ".join(install) or "nothing"}']
    if links:
        lines.append(f'- Left out, the core skips links: {", ".join(links[:10])}{" …" if len(links) > 10 else ""}')
    summary(lines)


def write_archive(args, modules, install, work):
    """Copies the modules into <work>/set, writes its manifest.json and packs it. Returns the links it left out"""
    top = 'set'
    for name, path in modules.items():
        # Only a module's own tests folder: static/tests is named in asset bundles
        def ignore(folder, names, module_path=path):
            skipped = {n for n in names if n in SKIPPED_NAMES or n.endswith('.pyc')}
            if os.path.samefile(folder, module_path) and 'tests' in names:
                skipped.add('tests')
            return skipped

        shutil.copytree(path, os.path.join(work, top, name), symlinks=True, ignore=ignore)

    manifest = {
        'tag': args.tag,
        'commit': args.commit,
        'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'modules': sorted(modules),
        'installModules': install,
    }
    with open(os.path.join(work, top, 'manifest.json'), 'w') as file:
        json.dump(manifest, file, indent=2)

    links = []

    # The core unpacks as root, files and folders only: no owner from this machine, and no links
    def clean(info):
        if info.issym() or info.islnk():
            links.append(info.name)
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        return info

    with tarfile.open(args.archive, 'w:gz') as archive:
        archive.add(os.path.join(work, top), arcname=top, filter=clean)
    return links


def registry_json(url, token, accept):
    request = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}', 'Accept': accept})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def odoo_release(args):
    # The official image has no label with its date, only the build argument in its history
    scope = 'repository:library/odoo:pull'
    with urllib.request.urlopen(f'https://auth.docker.io/token?service=registry.docker.io&scope={scope}', timeout=30) as response:
        token = json.load(response)['token']
    base = 'https://registry-1.docker.io/v2/library/odoo'
    index = registry_json(f'{base}/manifests/{args.series}', token, 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json')
    digest = next(m['digest'] for m in index['manifests'] if m.get('platform', {}).get('architecture') == 'arm64')
    manifest = registry_json(f'{base}/manifests/{digest}', token, 'application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json')
    config = registry_json(f'{base}/blobs/{manifest["config"]["digest"]}', token, 'application/json')
    for step in config.get('history', []):
        found = re.search(r'ODOO_RELEASE=(\d{8})', step.get('created_by', ''))
        if found:
            print(found.group(1))
            return
    fail(f'No ODOO_RELEASE in the history of odoo:{args.series}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)

    command = commands.add_parser('oca-pin')
    command.add_argument('list')
    command.add_argument('series')
    command.add_argument('--newest', action='store_true')
    command.set_defaults(run=oca_pin)

    command = commands.add_parser('oca-collect')
    command.add_argument('list')
    command.add_argument('tree')
    command.set_defaults(run=oca_collect)

    command = commands.add_parser('pack')
    command.add_argument('tree')
    command.add_argument('archive')
    command.add_argument('--tag', required=True)
    command.add_argument('--commit', required=True)
    command.add_argument('--install', help='Comma-separated. Without it, the installModules of the tree\'s manifest.json')
    command.set_defaults(run=pack)

    command = commands.add_parser('odoo-release')
    command.add_argument('series')
    command.set_defaults(run=odoo_release)

    args = parser.parse_args()
    args.run(args)


if __name__ == '__main__':
    main()
