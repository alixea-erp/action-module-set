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
| `callers/oca.yaml`        | The OCA list                   | **Run workflow** with the series                                                  |

The Enterprise fork also needs the secret `ODOO_ENTERPRISE_READ_TOKEN`: a token that can read `odoo/enterprise`.
The workflow only fetches Odoo's commits with it. Everything it writes goes through its own token. Use a token of
an account that has nothing else to reach.

With the organisation secret `SLACK_ODOO_RELEASES_WEBHOOK_URL`, the fork and `component-odoo` say in Slack when a
release is ready to try, and when a build or a release failed.

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
# <owner>/<repo> <commit or -> <module> ...
OCA/web - +web_responsive
OCA/server-ux - date_range_account
```

- `-` gets the newest commit of the series branch at the next release.
- `+name` is also installed when the set is turned on.
- A dependency that is in a listed repository is added by itself. The run's summary names it.
- The summary also names the Python packages the modules need. The Odoo image may lack them.

## Change this action

Run the tests with `python3 -m unittest`. They also run on every push.

The set repositories use the tag `v1`. Move it to release a change:

```sh
git tag -f v1 origin/main
git push -f origin v1
```

This repository must be public, or private with **Settings, Actions, Access** set to the organisation's
repositories. Otherwise the set repositories cannot use it.
