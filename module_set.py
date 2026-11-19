#!/usr/bin/env python3
"""Builds the archive of one version of a module set (the platform's docs/OdooModules.md, phase 4). The actions of
this repository run it.

  module_set.py oca-pin <list> <series> [--newest]
      Fills in the commit of every line whose commit is "-" (every line with --newest) from the newest commit of the
      series branch, and rewrites the list.
  module_set.py oca-collect <list> <tree> [--translations <folder>] [--languages fi,sv]
      Fetches each listed repository at its commit and copies the picked modules, and the modules of the listed
      repositories they depend on, into <tree>. Adds our own translations, <folder>/<module>/i18n_extra/<lang>.po,
      and says which terms of the languages nobody has translated.
  module_set.py stage <tree> <folder> --tag <tag> --commit <commit> [--install a,b]
      Copies every module of <tree> into <folder> and writes its manifest.json: what the archive holds.
  module_set.py archive <folder> <archive>
      Packs a staged <folder> into a gzipped tar with one top folder.
  module_set.py pack <tree> <archive> --tag <tag> --commit <commit> [--install a,b]
      Both, for a set that is not built.
  module_set.py odoo-release <series>
      Prints the release date of Docker Hub's odoo:<series>, such as 20261112.

The OCA list has one repository per line: "<owner>/<repo> <commit or -> [<module> ...]". "#" starts a comment, on a
line of its own or after one.
  name    that module
  +name   that module, also installed when the set is turned on
  *       every module of the repository that is installable and not marked Alpha
  (none)  nothing of its own: the repository only gives the modules that the others depend on
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
            names = line[2:]
            if (
                len(line) < 2
                or not re.fullmatch(r'[\w.-]+/[\w.-]+', line[0])
                or not re.fullmatch(r'-|[0-9a-f]{40}', line[1])
                or not all(re.fullmatch(r'\*|\+?\w+', name) for name in names)
            ):
                fail(f'{path}:{number}: expected "<owner>/<repo> <commit or -> [<module> ...]", got "{raw.strip()}"')
            entries.append({
                'line': number,
                'repository': line[0],
                'commit': line[1],
                'all': '*' in names,
                'modules': [name.lstrip('+') for name in names if name != '*'],
                'install': [name[1:] for name in names if name.startswith('+')],
            })
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
    added = add_translations(args.tree, args.translations) if args.translations else []
    languages = [language for language in re.split(r'[,\s]+', args.languages or '') if language]
    if languages:
        report_translations(args.tree, languages, added, args.translations_line)


class PoError(Exception):
    pass


README_PLACE = 'model:ir.module.module,description:'


def read_po(path, places=None):
    """The entries of a .po or .pot file, msgid to msgstr, as they are written between the quotes. Without the
    header and without obsolete and fuzzy entries, which Odoo does not load either. `places` gets where each term is
    used, its "#:" lines. Raises PoError with the line of what is not gettext"""
    entries = {}
    entry, field = {}, None

    def close():
        nonlocal entry, field
        if entry.get('msgid') and not entry.get('fuzzy'):
            entries[entry['msgid']] = entry.get('msgstr', '')
            if places is not None:
                places[entry['msgid']] = entry.get('places', [])
        if 'msgid' in entry:
            entry = {}
        field = None

    with open(path, encoding='utf-8') as file:
        for number, raw in enumerate(file, 1):
            line = raw.strip()
            if not line:
                close()
                entry = {}
            elif line.startswith('#'):
                # Comments come before their entry. "#~" is an obsolete entry, all of it a comment
                close()
                if line.startswith('#,') and 'fuzzy' in line:
                    entry['fuzzy'] = True
                if line.startswith('#:'):
                    entry.setdefault('places', []).extend(line[2:].split())
            else:
                found = re.fullmatch(r'(?:(msgctxt|msgid|msgid_plural|msgstr)(?:\[(\d+)\])?\s+)?"((?:[^"\\]|\\.)*)"', line)
                if not found:
                    raise PoError(f'{path}:{number}: not a line of a translation file: {line[:80]}')
                keyword, plural, text = found.groups()
                if keyword is None:
                    if field is None:
                        raise PoError(f'{path}:{number}: text that belongs to no msgid or msgstr')
                    entry[field] += text
                    continue
                if keyword in ('msgctxt', 'msgid'):
                    close()
                # Of plural forms the first counts. Odoo's modules have none
                field = keyword if plural in (None, '0') else f'msgstr{plural}'
                entry[field] = text
    close()
    return entries


def add_translations(tree, folder):
    """Copies our own translations, <folder>/<module>/i18n_extra/<lang>.po, into the modules of the tree. Odoo loads
    i18n_extra after a module's own i18n, so ours are added to OCA's and win where both have a term. Returns the
    modules that got some"""
    added = []
    if not os.path.isdir(folder):
        return added
    for name in sorted(os.listdir(folder)):
        source = os.path.join(folder, name, 'i18n_extra')
        files = sorted(file for file in os.listdir(source) if file.endswith('.po')) if os.path.isdir(source) else []
        if not files:
            continue
        if not os.path.isdir(os.path.join(tree, name)):
            print(f'::warning::{folder} has translations for {name}, which is not in the set')
            continue
        # NOTE! Odoo reads where a term belongs from the template next to the file, so ours need not say it. Without
        # one it fails on the first entry, and only on a database that has the language
        template = os.path.join(tree, name, 'i18n', f'{name}.pot')
        if not os.path.isfile(template):
            fail(f'{name} has no i18n/{name}.pot, so its translations in {source} cannot be loaded')
        target = os.path.join(tree, name, 'i18n_extra')
        os.makedirs(target, exist_ok=True)
        shutil.copyfile(template, os.path.join(target, f'{name}.pot'))
        for file in files:
            try:
                read_po(os.path.join(source, file))
            except (PoError, UnicodeDecodeError) as error:
                fail(str(error))
            shutil.copyfile(os.path.join(source, file), os.path.join(target, file))
        added.append(name)
    return added


def missing_translations(module_path, language):
    """The terms of a module's template that neither its own i18n nor our i18n_extra translates to the language.
    None for a module without a template"""
    name = os.path.basename(module_path)
    template = os.path.join(module_path, 'i18n', f'{name}.pot')
    if not os.path.isfile(template):
        return None
    translated = {}
    for folder in ('i18n', 'i18n_extra'):
        path = os.path.join(module_path, folder, f'{language}.po')
        if not os.path.isfile(path):
            continue
        try:
            translated.update({term: text for term, text in read_po(path).items() if text})
        except (PoError, UnicodeDecodeError) as error:
            print(f'::warning::{error}')
    places = {}
    try:
        terms = read_po(template, places)
    except (PoError, UnicodeDecodeError) as error:
        print(f'::warning::{error}')
        return None
    # A module's README, which the templates have as a term since Odoo 20, is nobody's to translate
    readme = {term for term, used in places.items() if used and all(place.startswith(README_PLACE) for place in used)}
    return [term for term in terms if term not in translated and term not in readme]


# A step's summary may be 1 MiB, and the other steps of the release write to the same one
TRANSLATIONS_SUMMARY_BYTES = 600 * 1024


def fold(title, body):
    return f'<details><summary>{title}</summary>\n\n{body}\n\n</details>'


def report_translations(tree, languages, added, line_path):
    """The numbers and every missing term to the summary, folded by language and module, the terms to the log too,
    and one line for the Slack message"""
    modules = find_modules(tree)
    lines = ['### Translations', '| Language | Missing terms | Modules with missing terms |', '| --- | --- | --- |']
    counts, folds, short_folds = [], [], []
    for language in languages:
        missing = {name: missing_translations(path, language) for name, path in modules.items()}
        missing = {name: terms for name, terms in missing.items() if terms}
        total = sum(len(terms) for terms in missing.values())
        print(f'::group::Missing {language} translations: {total} terms in {len(missing)} modules')
        for name, terms in missing.items():
            for term in terms:
                print(f'{language} {name}: {term[:200]}')
        print('::endgroup::')
        lines.append(f'| {language} | {total} | {len(missing)} of {len(modules)} |')
        if not total:
            continue
        counts.append(f'{language} {total}')
        title = f'{language}: {total} missing terms in {len(missing)} modules'
        # In a code block, as a term may hold HTML
        folds.append(fold(title, '\n'.join(
            fold(f'{name}: {len(terms)}', '```text\n' + '\n'.join(term[:200].replace('`' * 3, '') for term in terms) + '\n```')
            for name, terms in missing.items()
        )))
        short_folds.append(fold(title, '\n'.join(f'- {name}: {len(terms)}' for name, terms in missing.items())))

    lines.append('')
    lines.append(f'Our own translations: {", ".join(added) or "none"}.')
    if len('\n'.join(folds).encode()) > TRANSLATIONS_SUMMARY_BYTES:
        folds = short_folds
        lines.append('Too many terms are missing to list them here. The log of this step has every one.')
    summary(lines + [''] + folds)
    if line_path and counts:
        with open(line_path, 'w') as file:
            file.write(f'Translations missing: {", ".join(counts)} terms. The summary of the run lists them.\n')


def pick(entry, manifests):
    """The modules a line of the list asks for, of its repository's manifests by name: the named ones, and with "*" every
    one that is installable and not Alpha. Returns them with the Alpha ones "*" left out"""
    names = list(entry['modules'])
    alpha = []
    if entry['all']:
        for name, manifest in manifests.items():
            if name in names or not manifest.get('installable', True):
                continue
            if manifest.get('development_status') == 'Alpha':
                alpha.append(name)
            else:
                names.append(name)
    return names, alpha


def collect(entries, work, tree):
    index, picked, skipped = {}, [], []
    for entry in entries:
        if entry['commit'] == '-':
            fail(f'{entry["repository"]} has no commit, run oca-pin first')
        clone = os.path.join(work, entry['repository'].replace('/', '__'))
        os.makedirs(clone)
        git('init', '-q', cwd=clone)
        git('fetch', '-q', '--depth', '1', f'https://github.com/{entry["repository"]}.git', entry['commit'], cwd=clone)
        git('checkout', '-q', 'FETCH_HEAD', cwd=clone)
        found = find_modules(clone)
        for name, path in found.items():
            if name in index:
                fail(f'Module {name} is in {index[name]["repository"]} and {entry["repository"]}')
            index[name] = {'path': path, 'repository': entry['repository']}
        for name in entry['modules']:
            if name not in found:
                fail(f'{entry["repository"]} has no module {name} at {entry["commit"][:12]}')
        names, alpha = pick(entry, {name: read_manifest(path) for name, path in found.items()})
        picked += names
        skipped += alpha

    chosen, added, outside, python, excludes, unusable = choose({name: found['path'] for name, found in index.items()}, picked)

    os.makedirs(tree, exist_ok=True)
    for name in sorted(chosen):
        shutil.copytree(index[name]['path'], os.path.join(tree, name), symlinks=True, ignore=shutil.ignore_patterns('.git'))
    # pack reads the modules to install from here
    with open(os.path.join(tree, 'manifest.json'), 'w') as file:
        json.dump({'installModules': [name for entry in entries for name in entry['install']]}, file)

    lines = ['### Modules', f'{len(chosen) - len(added)} picked, {len(added)} added as dependencies: {len(chosen)} in the set']
    if added:
        lines.append(f'- Added: {", ".join(sorted(added))}')
    if skipped:
        lines.append(f'- Left out as Alpha, name one to take it: {", ".join(sorted(skipped))}')
    for name, users in sorted(unusable.items()):
        lines.append(f'- **{name} is not installable in this series**, needed by: {", ".join(sorted(users))}')
    if outside:
        lines.append(f'- Needed from outside the list (Odoo, Enterprise, or missing here): {", ".join(sorted(outside))}')
    if python:
        lines.append(f'- Python packages the modules need: {", ".join(sorted(python))}. The install check fails a module whose package the image lacks')
    for name, others in sorted(excludes.items()):
        lines.append(f'- **{name} cannot be installed together with**: {", ".join(others)}')
    install = [name for entry in entries for name in entry['install']]
    for name in sorted(set(install) & set(excludes)):
        print(f'::warning::{name} is installed when the set is turned on, and fails where {", ".join(excludes[name])} is installed')
    summary(lines)


def choose(paths, picked):
    """The picked modules with every module of `paths` (name to folder) they depend on. Returns the chosen names, the
    ones added as dependencies, the dependencies `paths` does not have, the Python packages the chosen need, the
    modules each chosen one cannot be installed with, and the dependencies that are not installable with who needs them"""
    chosen, added, outside, python, excludes, unusable = set(), set(), set(), set(), {}, {}
    manifests = {}

    def manifest_of(name):
        if name not in manifests:
            manifests[name] = read_manifest(paths[name])
        return manifests[name]

    for name in picked:
        if not manifest_of(name).get('installable', True):
            fail(f'{name} is not installable in this series')

    pending = list(picked)
    while pending:
        name = pending.pop()
        if name in chosen:
            continue
        chosen.add(name)
        manifest = manifest_of(name)
        python.update(manifest.get('external_dependencies', {}).get('python', []))
        if manifest.get('excludes'):
            excludes[name] = sorted(manifest['excludes'])
        for dependency in manifest.get('depends', []):
            if dependency not in paths:
                outside.add(dependency)
            elif not manifest_of(dependency).get('installable', True):
                unusable.setdefault(dependency, set()).add(name)
            else:
                if dependency not in picked:
                    added.add(dependency)
                pending.append(dependency)
    return chosen, added, outside, python, excludes, unusable


def stage(args):
    """Copies the modules of args.tree into args.folder and writes its manifest.json"""
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

    for name, path in modules.items():
        # Only a module's own tests folder: static/tests is named in asset bundles
        def ignore(folder, names, module_path=path):
            skipped = {n for n in names if n in SKIPPED_NAMES or n.endswith('.pyc')}
            if os.path.samefile(folder, module_path) and 'tests' in names:
                skipped.add('tests')
            return skipped

        shutil.copytree(path, os.path.join(args.folder, name), symlinks=True, ignore=ignore)

    manifest = {
        'tag': args.tag,
        'commit': args.commit,
        'created': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'modules': sorted(modules),
        'installModules': install,
    }
    with open(os.path.join(args.folder, 'manifest.json'), 'w') as file:
        json.dump(manifest, file, indent=2)


def archive(args):
    """Packs the staged args.folder into args.archive under the top folder "set", without links"""
    with open(os.path.join(args.folder, 'manifest.json')) as file:
        manifest = json.load(file)
    links = []

    # The core unpacks as root, files and folders only: no owner from this machine, and no links
    def clean(info):
        if info.issym() or info.islnk():
            links.append(info.name)
            return None
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        return info

    with tarfile.open(args.archive, 'w:gz') as packed:
        packed.add(args.folder, arcname='set', filter=clean)

    size = os.path.getsize(args.archive)
    install = ', '.join(manifest['installModules']) or 'nothing'
    built = f', built for {manifest["pythonTag"]}' if manifest.get('pythonTag') else ''
    lines = ['### Archive', f'{len(manifest["modules"])} modules, {size / 1024 / 1024:.1f} MiB, installs: {install}{built}']
    if links:
        lines.append(f'- Left out, the core skips links: {", ".join(links[:10])}{" …" if len(links) > 10 else ""}')
    summary(lines)


def pack(args):
    with tempfile.TemporaryDirectory(prefix='set-') as work:
        folder = os.path.join(work, 'set')
        stage(argparse.Namespace(tree=args.tree, folder=folder, tag=args.tag, commit=args.commit, install=args.install))
        archive(argparse.Namespace(folder=folder, archive=args.archive))


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
    command.add_argument('--translations', help='The folder of our own translations, one folder per module')
    command.add_argument('--languages', help='Comma-separated, such as fi,sv. Their missing terms are reported')
    command.add_argument('--translations-line', help='A file for the line the Slack message gets, written when terms are missing')
    command.set_defaults(run=oca_collect)

    command = commands.add_parser('stage')
    command.add_argument('tree')
    command.add_argument('folder')
    command.add_argument('--tag', required=True)
    command.add_argument('--commit', required=True)
    command.add_argument('--install', help='Comma-separated. Without it, the installModules of the tree\'s manifest.json')
    command.set_defaults(run=stage)

    command = commands.add_parser('archive')
    command.add_argument('folder')
    command.add_argument('archive')
    command.set_defaults(run=archive)

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
