# Updating add-on dependencies

Each add-on keeps human-maintained compatibility bounds in `requirements.in` and a compiled, fully pinned package list with distribution hashes in `requirements.txt`. Docker installs the latter using `--require-hashes`. Playwright's browser installer runs from the pinned Playwright package, so the selected Chromium revision follows the same reviewed update.

To regenerate both lists from the repository root with uv 0.12.23:

```sh
uv pip compile stellantis_login_worker/requirements.in --python-version 3.11 --python-platform linux --generate-hashes --no-header --upgrade -o stellantis_login_worker/requirements.txt
uv pip compile stellantis_vehicles/requirements.in --python-version 3.11 --python-platform linux --generate-hashes --no-header --upgrade -o stellantis_vehicles/requirements.txt
```

Review the diff before committing. Keep `paho-mqtt<2` until the bridge callback API has been migrated and tested. Do not copy these dependency pins into the HACS integration: Home Assistant owns its environment and dependency compatibility.

Dependabot checks Python packages in both add-on directories and GitHub Actions weekly. CI installs the locked requirements on Python 3.11, runs `pip check`, scans the runtime list using `pip-audit`, and runs the existing smoke tests. The audit runs on pull requests and weekly, since advisories can change without a code change. A clean audit means no known findings from that scan, not a security guarantee. The audit tool is installed in a separate environment so it cannot alter the runtime versions under test.

The image-build workflow uses the composable Home Assistant build-image action on native amd64 and aarch64 runners. The deprecated root action is unsuitable for a plain SHA pin because it derives a container-image tag from that ref. Both architectures are tested on pull requests. Before release, require those builds and a real login test, because mock-based smoke tests do not exercise Chromium startup or provider behavior. Upstream fork workflows may require maintainer approval.

These changes pin Python runtime dependencies and the top-level Actions. They do not make images bit-for-bit reproducible: Debian base-image tags, apt packages, build tools for source distributions and nested Actions can still change. Base-image digest pinning and OS-package audits are separate follow-ups.

## Reviewing Dependabot changes

The compiled requirements currently omit the generator header. Dependabot may
therefore edit `requirements.txt` directly without updating `requirements.in` or
resolving the full dependency graph. Treat these PRs as update proposals, not as
ready-to-merge lockfiles. Before merging, review the compatibility bounds in
`requirements.in`, adjust them when needed, rerun the documented `uv pip compile`
command for every affected add-on, and commit the regenerated hashed output.
Require hash-verified installation, `pip check`, audits and regression tests on
the resulting commit. Do not hand-edit hashes or merge an unregenerated lockfile.

CI reads each architecture's base image and all labels from the add-on's
`build.yaml`, so source builds and workflow builds use the same metadata.
Versioned images and `latest` are published only for main or version tags;
PRs and manual builds on other branches do not publish. `latest` also supplies
the registry build cache. A Playwright update changes bundled Chromium: require
a real login with the exact built image on **both amd64 and aarch64** before
release. An offline browser startup check alone does not satisfy that gate.
