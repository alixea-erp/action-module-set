#!/usr/bin/env python3
"""Installs every module of a staged module set on new databases and says which ones fail (the platform's
docs/OdooModules.md, phase 4). It runs inside Alixea's Odoo image of the set's series, with a PostgreSQL to create
databases on.

  module_check.py <folder> --summary <file> --line <file> [--with <folder>] [--db-host H] [--db-port P]
                  [--db-user U] [--db-password W] [--minutes M]

All modules go into one install. When one fails, it and the modules that need it are taken out and the others are
installed on the same database again: Odoo commits after each module, so what was installed stays. A module that
needs a module of --with (Enterprise, for an Alixea set) is installed on a second database that has --with on its
path, because --with's modules that install by themselves would change what the first one checks.

--summary gets Markdown, --line one line for a message. It exits with 0 when no module failed, 3 when one did, and
4 when it could not check everything: it ran out of time, or could not run at all. The release decides what to do.
"""
import argparse
import ast
import html
import importlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import time

DATABASES = ('module_check_set', 'module_check_with')
EXIT_FAILED = 3
EXIT_INCOMPLETE = 4
OUT_OF_TIME = 'the check ran out of time'
# An exception's last line, such as "odoo.tools.convert.ParseError: while parsing ..."
EXCEPTION_LINE = re.compile(r'^([A-Za-z_][\w.]*(Error|Exception|Warning|Exit)|AssertionError|KeyError)\b')
FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+), in ')


def frame_context(lines, index):
    """For an exception without a message: the code that raised it, and the last frame outside Odoo and Python"""
    frames = [(number, FRAME.match(line)) for number, line in enumerate(lines[:index]) if FRAME.match(line)]
    if not frames:
        return ''
    number, _last = frames[-1]
    code = lines[number + 1].strip() if number + 1 < index else ''
    own = [match for _, match in frames if not re.search(r'/odoo/|-packages/|/python3', match.group(1))]
    parts = [f'at {code}'] if code and not code.startswith('File ') else []
    if own:
        parts.append(f'from {"/".join(own[-1].group(1).split("/")[-3:])}:{own[-1].group(2)}')
    return ' '.join(parts)


def read_manifest(module_path):
    with open(os.path.join(module_path, '__manifest__.py')) as file:
        return ast.literal_eval(file.read())


def find_manifests(folder):
    """The manifests of the module folders directly in the folder, by name"""
    if not folder or not os.path.isdir(folder):
        return {}
    return {
        name: read_manifest(os.path.join(folder, name))
        for name in sorted(os.listdir(folder))
        if os.path.isfile(os.path.join(folder, name, '__manifest__.py'))
    }


def python_missing(requirement):
    """None when the Python package is there, as Odoo decides it, otherwise why not"""
    try:
        from packaging.requirements import Requirement
        parsed = Requirement(requirement)
    except Exception:
        return f'the requirement "{requirement}" does not parse'
    if parsed.marker and not parsed.marker.evaluate():
        return None
    try:
        version = importlib.metadata.version(parsed.name)
    except importlib.metadata.PackageNotFoundError:
        try:
            importlib.import_module(requirement)
            return None
        except Exception:
            return f'the Python package {requirement}'
    if parsed.specifier and not parsed.specifier.contains(version):
        return f'the Python package {requirement} (the image has {version})'
    return None


def program_missing(name):
    return None if shutil.which(name) else f'the program {name}'


def plan(manifests, available, extra, missing_python=python_missing, missing_program=program_missing):
    """Which modules of the set go on which database.

    manifests: the set's modules. available: the names of every other module Odoo has without the set (its own, and a
    project's). extra: the modules of --with. Returns (first, second, skipped): the names to install without --with,
    those that need it, and {name: reason} for the ones that are not tried.
    """
    skipped = {}
    for name, manifest in manifests.items():
        if not manifest.get('installable', True):
            skipped[name] = 'not installable'
            continue
        external = manifest.get('external_dependencies', {})
        needs = [missing_python(package) for package in external.get('python', [])]
        needs += [missing_program(program) for program in external.get('bin', [])]
        needs = [need for need in needs if need]
        if needs:
            skipped[name] = f'needs {", ".join(needs)}'

    # Takes a module out when one it depends on is nowhere, or out itself, until nothing changes
    known = set(manifests) | set(available) | set(extra)
    changed = True
    while changed:
        changed = False
        for name, manifest in manifests.items():
            if name in skipped:
                continue
            gone = [d for d in manifest.get('depends', []) if d not in known]
            out = [d for d in manifest.get('depends', []) if d in skipped]
            if gone or out:
                skipped[name] = f'needs {", ".join(gone + out)}' + (' (not checked either)' if out and not gone else '')
                changed = True

    # A module needs --with when it, or a module of the set it depends on, depends on a module only --with has
    needing = set()
    changed = True
    while changed:
        changed = False
        for name, manifest in manifests.items():
            if name in skipped or name in needing:
                continue
            depends = manifest.get('depends', [])
            if any((d in extra and d not in available and d not in manifests) or d in needing for d in depends):
                needing.add(name)
                changed = True

    first, second = [], []
    for name, manifest in manifests.items():
        if name in skipped:
            continue
        chosen = second if name in needing else first
        clash = [other for other in chosen if other in manifest.get('excludes', []) or name in manifests[other].get('excludes', [])]
        if clash:
            skipped[name] = f'cannot be installed with {", ".join(clash)}'
            continue
        chosen.append(name)
    return first, second, skipped


def failed_module(log):
    """The module Odoo stopped on: one it refused to install, otherwise the last one it was loading"""
    refused = re.findall(r'Unable to install module "(\w+)"', log)
    if refused:
        return refused[-1]
    names = re.findall(r'Loading module (\w+) \(\d+/\d+\)', log)
    return names[-1] if names else None


def visible(manifests, skipped):
    """The modules Odoo may see: all but those set aside that would install by themselves"""
    return [name for name, manifest in manifests.items() if not (name in skipped and manifest.get('auto_install'))]


def addons_view(folder, names, view):
    """A folder of links to the named modules of the folder only: Odoo cannot install by itself what it does not see"""
    os.makedirs(view, exist_ok=True)
    for name in names:
        os.symlink(os.path.join(os.path.abspath(folder), name), os.path.join(view, name))
    return view


def error_text(log, limit=300):
    """The last exception of the log, with the line after a ParseError that says what was wrong"""
    lines = log.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index].strip()
        if EXCEPTION_LINE.match(line) and not line.startswith('The above exception'):
            text = line
            if 'ParseError' in line:
                details = [l.strip() for l in lines[index + 1:index + 6] if l.strip() and not l.strip().startswith('View error context')]
                text = ' '.join([line] + details[:2])
            elif ':' not in line:
                text = f'{line} {frame_context(lines, index)}'.strip()
            return html.unescape(text)[:limit]
    for line in reversed(lines):
        if ' CRITICAL ' in line or ' ERROR ' in line:
            return line.split(' ', 5)[-1][:limit]
    return 'Odoo stopped without an error in the log'


def log_end(log, lines=120):
    """The end of Odoo's log, where the traceback of what stopped the install is"""
    return '\n'.join(log.splitlines()[-lines:])


def needing_module(name, remaining, manifests):
    """The modules of remaining that need the module, directly or through another one of remaining"""
    found = set()
    changed = True
    while changed:
        changed = False
        for other in remaining:
            if other not in found and any(d == name or d in found for d in manifests[other].get('depends', [])):
                found.add(other)
                changed = True
    return [other for other in remaining if other in found]


class Odoo:
    """Odoo of the image on throwaway databases"""

    def __init__(self, args, work):
        self.args = args
        self.work = work
        self.config = os.path.join(work, 'odoo.conf')
        with open(self.config, 'w') as file:
            file.write(
                '[options]\n'
                f'db_host = {args.db_host}\ndb_port = {args.db_port}\n'
                f'db_user = {args.db_user}\ndb_password = {args.db_password}\n'
                f'data_dir = {os.path.join(work, "data")}\n'
                'list_db = False\n'
            )
        version = subprocess.run(['odoo', '--version'], capture_output=True, text=True).stdout.strip()
        self.version = version.replace('Odoo Server ', '') or 'unknown'
        self.series = int(re.match(r'(\d+)', self.version).group(1)) if re.match(r'\d+', self.version) else 0

    def query(self, database, sql, parameters=None):
        """Runs one statement outside a transaction, as DROP DATABASE needs, and returns its rows"""
        import psycopg2
        connection = psycopg2.connect(dbname=database, host=self.args.db_host, port=self.args.db_port, user=self.args.db_user, password=self.args.db_password)
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, parameters)
                return cursor.fetchall() if cursor.description else []
        finally:
            connection.close()

    def drop(self, database):
        self.query('postgres', f'DROP DATABASE IF EXISTS "{database}"')

    def run(self, database, addons, modules, seconds, log_name):
        """Runs `odoo -i` and returns (exit code, log). The code is None after the time ran out"""
        command = ['odoo', '-c', self.config, '-d', database, f'--addons-path={",".join(addons)}', '-i', ','.join(modules), '--stop-after-init', '--no-http']
        # NOTE! Odoo 18 installs demo data unless told otherwise, from 19 on it does not by default
        if self.series and self.series < 19:
            command.append('--without-demo=all')
        log_path = os.path.join(self.work, log_name)
        with open(log_path, 'w') as log:
            try:
                code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=max(seconds, 1)).returncode
            except subprocess.TimeoutExpired:
                code = None
        with open(log_path, errors='replace') as log:
            return code, log.read()

    def states(self, database, names):
        return dict(self.query(database, 'SELECT name, state FROM ir_module_module WHERE name = ANY(%s)', (list(names),)))

    def module_names(self, database):
        return {row[0] for row in self.query(database, 'SELECT name FROM ir_module_module')}

    def undo_pending(self, database):
        """What a stopped install left marked, back as it was, as Odoo's own "Cancel Install" does"""
        self.query(database, "UPDATE ir_module_module SET state = 'uninstalled' WHERE state = 'to install'")
        self.query(database, "UPDATE ir_module_module SET state = 'installed' WHERE state IN ('to upgrade', 'to remove')")


def install_all(odoo, database, addons, modules, manifests, deadline, results):
    """Installs the modules on the database, taking out each one that fails. Fills results: {name: (outcome, text)}"""
    remaining = list(modules)
    attempt = 0
    while remaining:
        attempt += 1
        print(f'Install {attempt} on {database}: {len(remaining)} modules', flush=True)
        code, log = odoo.run(database, addons, remaining, deadline - time.time(), f'{database}-{attempt}.log')
        if code is None:
            print('Out of time', flush=True)
            for name in remaining:
                results[name] = ('not checked', OUT_OF_TIME)
            return

        states = odoo.states(database, remaining)
        installed = [name for name in remaining if states.get(name) == 'installed']
        for name in installed:
            results[name] = ('installed', '')
        remaining = [name for name in remaining if name not in installed]
        if code == 0:
            unseen = 'Odoo does not see it as a module' if 'invalid module names' in log else 'Odoo did not install it and named no error'
            for name in remaining:
                results[name] = ('failed', unseen)
            return

        failed = failed_module(log)
        error = error_text(log)
        # The step's log is what a person or an agent reads to fix the module, so it gets the traceback
        print(f'{len(installed)} installed, then {failed or "the install"} failed: {error}', flush=True)
        print(f'--- The end of Odoo\'s log\n{log_end(log)}\n---', flush=True)
        odoo.undo_pending(database)
        if failed in remaining:
            results[failed] = ('failed', error)
            remaining.remove(failed)
            for name in needing_module(failed, remaining, manifests):
                results[name] = ('not checked', f'needs {failed}, which failed')
                remaining.remove(name)
        else:
            # A module of Odoo's or of --with stopped it: the set's modules that need it cannot be checked
            blocked = needing_module(failed, remaining, manifests) if failed else []
            for name in blocked or remaining:
                results[name] = ('not checked', f'the install stopped on {failed}: {error}' if failed else f'the install stopped: {error}')
                remaining.remove(name)


def exit_code(results):
    """What the release gets: a failed module counts before a check that ran out of time"""
    if any(outcome == 'failed' for outcome, _ in results.values()):
        return EXIT_FAILED
    if any(text == OUT_OF_TIME for _, text in results.values()):
        return EXIT_INCOMPLETE
    return 0


def write_summary(args, odoo, manifests, results, skipped, extra_name):
    counts = {outcome: sum(1 for value in results.values() if value[0] == outcome) for outcome in ('installed', 'failed', 'not checked')}
    not_checked = {**skipped, **{name: text for name, (outcome, text) in results.items() if outcome == 'not checked'}}
    failed = {name: text for name, (outcome, text) in results.items() if outcome == 'failed'}

    lines = [f'### Install check on Odoo {odoo.version}', '']
    lines.append(f'{counts["installed"]} of {len(manifests)} modules installed. {len(failed)} failed, {len(not_checked)} not checked.')
    if extra_name:
        lines.append(f'The modules that need {extra_name} were installed with it, on a database of their own.')
    if failed:
        lines += ['', '#### Failed', '', '| Module | Error |', '| --- | --- |']
        lines += [f'| `{name}` | {escaped} |' for name, escaped in sorted((n, t.replace('|', '/')) for n, t in failed.items())]
    if not_checked:
        lines += ['', '#### Not checked', '']
        lines += [f'- `{name}`: {text}' for name, text in sorted(not_checked.items())]
    with open(args.summary, 'w') as file:
        file.write('\n'.join(lines) + '\n')

    line = f'{counts["installed"]} of {len(manifests)} modules install'
    if failed:
        names = sorted(failed)
        line += f', {len(failed)} fail: {", ".join(names[:5])}{" …" if len(names) > 5 else ""}'
    if not_checked:
        line += f', {len(not_checked)} not checked'
    with open(args.line, 'w') as file:
        file.write(line + '.\n')


def check(args):
    manifests = find_manifests(args.folder)
    extra = find_manifests(args.extra) if args.extra else {}
    deadline = time.time() + args.minutes * 60
    work = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'module-check')
    os.makedirs(work, exist_ok=True)
    odoo = Odoo(args, work)

    results = {}
    try:
        for database in DATABASES:
            odoo.drop(database)
        # What a database lists without the set on the path is Odoo's own modules
        empty = os.path.join(work, 'empty')
        os.makedirs(empty, exist_ok=True)
        code, log = odoo.run(DATABASES[0], [empty], ['base'], deadline - time.time(), 'base.log')
        if code != 0:
            raise RuntimeError(f'a new database could not be made: {error_text(log)}')
        available = odoo.module_names(DATABASES[0])

        first, second, skipped = plan(manifests, available, extra)
        if second and not args.extra:
            for name in second:
                skipped[name] = 'needs modules of another set'
            second = []

        # NOTE! Odoo installs a module marked auto_install by itself once its dependencies are there, also one set
        # aside here for a missing package, and then stops the whole install. Such a module is hidden from Odoo. The
        # others stay in sight, as data of a module may name one that is not installed
        view = addons_view(args.folder, visible(manifests, skipped), os.path.join(work, 'set'))
        install_all(odoo, DATABASES[0], [view], first, manifests, deadline, results)
        if second:
            _, _, extra_skipped = plan(extra, available, {})
            extra_view = addons_view(args.extra, visible(extra, extra_skipped), os.path.join(work, 'with'))
            install_all(odoo, DATABASES[1], [view, extra_view], second, manifests, deadline, results)
        write_summary(args, odoo, manifests, results, skipped, args.extra_name if second else '')
        return exit_code(results)
    except Exception as error:
        message = f'The install check could not finish: {error}.'
        with open(args.summary, 'w') as file:
            file.write(f'### Install check\n\n{message}\n')
        with open(args.line, 'w') as file:
            file.write(message + '\n')
        return EXIT_INCOMPLETE
    finally:
        for database in DATABASES:
            try:
                odoo.drop(database)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('folder')
    parser.add_argument('--summary', required=True)
    parser.add_argument('--line', required=True)
    parser.add_argument('--with', dest='extra', default='')
    parser.add_argument('--with-name', dest='extra_name', default='', help='What --with is, for the summary, such as odoo-enterprise 19.0/20261112')
    parser.add_argument('--db-host', default='localhost')
    parser.add_argument('--db-port', default='5432')
    parser.add_argument('--db-user', default='odoo')
    parser.add_argument('--db-password', default='odoo')
    parser.add_argument('--minutes', type=int, default=300)
    return check(parser.parse_args())


if __name__ == '__main__':
    sys.exit(main())
