# Module set release

Releases one version of an Odoo module set to the bucket the Alixea cores download from. For Alixea's developers.

A module set is a repository of Odoo modules: Alixea Generic, Alixea Finance, Odoo Enterprise or OCA. Each version is
a git tag such as `19.0/20261116`. The cores never read GitHub. They download the archive this action uploaded.

## What a release does

1. Checks out the calling repository at the tag.
2. Copies every module folder into one tree. A module's `tests` folder, `__pycache__` and links are left out.
3. Writes `manifest.json`: every module, and the modules to install when the set is turned on.
4. Builds the Alixea sets: see below.
5. Installs every module once. A module that fails stops the release here.
6. Uploads `s3://alixea-module-sets/<repository>/<tag>.tar.gz`. Only now do the admins see the version.

A released version is final. The bucket refuses to replace an object, so a fix is a new version.

### The build

Only for Alixea's own sets (`build: true`). The archive gets the modules' Python as bytecode, without comments and
docstrings, and with local names renamed. It is made in Alixea's Odoo image of the series, so it fits that image's
Python only. `manifest.json` says which one, as `pythonTag`.

Kept as source: `__manifest__.py`, each module's own `__init__.py` (minified), `migrations/`, `upgrades/` and
`static/`. XML, CSV and JavaScript are as they are.

A traceback from a built module names its file, but the line is of the minified code. Reproduce it with the source.

### The install check

Every module goes into one install on a new database, in Alixea's Odoo image of the series. When one fails, it and
the modules that need it are taken out, and the rest are installed again. A module that needs another set's modules
is installed with that set's newest version, on a second database (`check-with`, for the Alixea sets Enterprise).

When every module installed, all installed modules are upgraded once, as an update on a deployment does. A module
can install and still break the next upgrade, its own or that of a module whose views it changes.

The check runs before the upload, and **a module or an upgrade that fails stops the release**: nothing is uploaded, so the
version never shows on the admin Updates page. The run's summary and Slack name the module and its error. A check
that could not finish, or ran longer than 90 minutes, stops the release too.

To release all the same, start the release by hand and tick **Skip check**. It then uploads without installing
anything. A release that starts by itself (a push, the schedule) always checks. Not
checked are modules that are not installable, that need a module no set has, or that need a Python package or
program the image lacks. The summary lists them with the reason. Such a module that would install by itself
(`auto_install`) is hidden from Odoo during the check, as it would stop every install.

It takes a few minutes for an Alixea set, about 25 for all of Enterprise.

## Use it

Copy the file for your kind of set from `callers/` to `.github/workflows/Release.yaml` of the set's repository.

| File                      | For                            | A release is                                                                      |
| ------------------------- | ------------------------------ | --------------------------------------------------------------------------------- |
| `callers/alixea.yaml`     | Alixea Generic, Alixea Finance | A push to a series branch, tagged `19.0/<date>` by itself. Or **Run workflow**    |
| `callers/enterprise.yaml` | The fork of Odoo Enterprise    | By itself, after Pre-Build built an image of a new Odoo date. Or **Run workflow** |
| `callers/oca.yaml`        | The OCA list                   | By itself, at the newest commits, after such an image. Or **Run workflow**        |

The Enterprise fork also needs the secret `ODOO_ENTERPRISE_READ_TOKEN`: a token that can read `odoo/enterprise`.
The workflow only fetches Odoo's commits with it. Everything it writes goes through its own token. Use a token of
an account that has nothing else to reach.

With the secret `SLACK_ODOO_RELEASES_WEBHOOK_URL`, every set and `component-odoo` say in Slack when a release or
an image is ready to try, and when a build or a release failed. They all send through
`alixea-erp/action-module-set/slack@v1`, so every message has the same layout: a headline, the facts one per line,
the next step, and buttons to the run and the admin Updates page. After a release in the same job it adds the
install check's result by itself, with the first modules that failed and their errors. The Updates page it links
is its input `updates-url`.

A new set repository also needs its name in the release role, `module_set_repositories` in
`infra/tofu/envs/canary/core/module-sets.tf` of the platform. Without it the upload is refused.

## The actions

| Action                                         | What it does                                               |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `alixea-erp/action-module-set@v1`              | The release. Inputs: `tag`, `install-modules`, `oca-list`, `languages`, `build`, `check` (false is "Skip check"), `check-with`. Output: `check`, one line for a message. |
| `alixea-erp/action-module-set/oca-pin@v1`      | Fills in the commits of an OCA list.                       |
| `alixea-erp/action-module-set/odoo-release@v1` | The date of Docker Hub's `odoo:<series>` image.            |

The job that runs the release needs `contents: read` and `id-token: write`, and an ARM runner
(`runs-on: ubuntu-24.04-arm`): the build and the install check run Alixea's Odoo image, which is arm64.

## The OCA list

One file per series in `alixea-erp/oca`, such as `19.0.txt`. One repository per line:

```
# <owner>/<repo> <commit or -> [<module> ...]
OCA/bank-payment - *                  # everything in it
OCA/web - web_responsive +web_theme   # two modules, the second installed with the set
OCA/queue -                           # only for dependencies
```

| After the commit | Means                                                                         |
| ---------------- | ----------------------------------------------------------------------------- |
| `*`              | Every module of the repository that is installable and not marked Alpha.      |
| `name`           | That module, also when it is Alpha.                                           |
| `+name`          | That module, also installed when the set is turned on.                        |
| nothing          | Nothing of its own. The repository only gives what the other lines depend on. |

- `-` as the commit gets the newest commit of the series branch at the next release.
- `#` starts a comment, on a line of its own or after one. A release keeps them.
- A dependency that is in a listed repository is added by itself.

The run's summary names:

- the dependencies it added, and the Alpha modules `*` left out;
- dependencies it did not find in the list. Odoo's own modules show up there too;
- the Python packages the modules need. The Odoo image may lack them;
- a module that cannot be installed together with another, such as `web_responsive` with Odoo Enterprise. Do not
  give such a module a `+`.

## Translations of an OCA set

The repository of an OCA list may hold its own translations, `<series>/<module>/i18n_extra/<language>.po`, the series
being the list's name without `.txt`. The release copies each into its module with a copy of the module's template
(`i18n/<module>.pot`): Odoo reads from the template where a term belongs, so a file needs only `msgid` and `msgstr`.
Odoo loads `i18n_extra` after `i18n`, so they are added to OCA's translations and win over them.

With `languages`, such as `fi sv`, the release lists in its log every term of those languages that neither the
module nor the set's own files translate. The summary has the numbers, and the Slack message of the release says
how many are missing. A module's README, a term of its own since Odoo 20, is not counted. A file that is not valid
gettext stops the release. Nothing is checked by installing: the install check has no second language.

## Change this action

Run the tests with `python3 -m unittest`. They also run on every push. The build's tests need `python-minifier`
and are skipped without it.

The set repositories use the tag `v1`. Move it to release a change:

```sh
git tag -f v1 origin/main
git push -f origin v1
```

This repository must be public, or private with **Settings, Actions, Access** set to the organisation's
repositories. Otherwise the set repositories cannot use it.
