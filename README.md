# Module set release

Releases one version of an Odoo module set to the bucket the Alixea cores download from. For Alixea's developers.

A module set is a repository of Odoo modules: Alixea modules, Alixea Finance, Odoo Enterprise or OCA. Each version is
a git tag such as `19.0/1.4.0`. The cores never read GitHub. They download the archive this action uploaded.

## What a release does

1. Checks out the calling repository at the tag.
2. Copies every module folder into one tree. A module's `tests` folder, `__pycache__` and links are left out.
3. Writes `manifest.json`: every module, and the modules to install when the set is turned on.
4. Uploads `s3://alixea-module-sets/<repository>/<tag>.tar.gz`.

A released version is final. The bucket refuses to replace an object, so a fix is a new version.

## Use it

Copy the file for your kind of set from `callers/` to `.github/workflows/Release.yaml` of the set's repository.

| File                      | For                            | A release is                                                                      |
| ------------------------- | ------------------------------ | --------------------------------------------------------------------------------- |
| `callers/alixea.yaml`     | Alixea modules, Alixea Finance | A pushed tag such as `19.0/1.4.0`                                                 |
| `callers/enterprise.yaml` | The fork of Odoo Enterprise    | By itself, after Pre-Build built an image of a new Odoo date. Or **Run workflow** |
| `callers/oca.yaml`        | The OCA list                   | By itself, at the newest commits, after such an image. Or **Run workflow**        |

The Enterprise fork also needs the secret `ODOO_ENTERPRISE_READ_TOKEN`: a token that can read `odoo/enterprise`.
The workflow only fetches Odoo's commits with it. Everything it writes goes through its own token. Use a token of
an account that has nothing else to reach.

With the organisation secret `SLACK_ODOO_RELEASES_WEBHOOK_URL`, the fork, the OCA list and `component-odoo` say in
Slack when a release is ready to try, and when a build or a release failed.

A new set repository also needs its name in the release role, `module_set_repositories` in
`infra/tofu/envs/canary/core/module-sets.tf` of the platform. Without it the upload is refused.

## The actions

| Action                                         | What it does                                               |
| ---------------------------------------------- | ---------------------------------------------------------- |
| `alixea-erp/action-module-set@v1`              | The release. Inputs: `tag`, `install-modules`, `oca-list`. |
| `alixea-erp/action-module-set/oca-pin@v1`      | Fills in the commits of an OCA list.                       |
| `alixea-erp/action-module-set/odoo-release@v1` | The date of Docker Hub's `odoo:<series>` image.            |

The job that runs the release needs `contents: read` and `id-token: write`.

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

## Change this action

Run the tests with `python3 -m unittest`. They also run on every push.

The set repositories use the tag `v1`. Move it to release a change:

```sh
git tag -f v1 origin/main
git push -f origin v1
```

This repository must be public, or private with **Settings, Actions, Access** set to the organisation's
repositories. Otherwise the set repositories cannot use it.
