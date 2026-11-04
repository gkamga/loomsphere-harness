# Licensing provenance

Audit performed: 2026-11-03, against `feature/developer-distribution-v0.1` at
`ad09b47`, and against the originating LoomSphere application checkout at
`~/loomsphere`. The purpose is to establish what this repository contains, who
authored it, and whether the Apache License 2.0 can be applied to all of it.

This is an engineering provenance record, not legal advice. It records the
evidence that was available and names what it could not establish.

## Scope and method

Inspected:

- every tracked file (`107` at the audited commit), classified by directory;
- the full Git history of both repositories, including whether a `LICENSE`,
  `NOTICE`, or `COPYING` file ever existed (`git log --all -- <paths>`);
- authorship of every commit (`git log --format='%an <%ae>'`) and the GitHub
  contributor list for both repositories;
- the commit that added the `.github/` assets and the commit that removed them
  from the application;
- the runtime dependency metadata of every declared dependency;
- a repository-wide text search for `Copyright`, `SPDX-License-Identifier`,
  `Licensed under`, `adapted from`, `derived from`, `based on`, and
  `All rights reserved`;
- the Dockerfile and CI workflow for embedded third-party material.

Not performed: code-similarity or plagiarism search against external
repositories, and review of any employment, contractor, or assignment agreement.
Those limits are recorded under [Residual uncertainty](#residual-uncertainty).

## Repository lineage

Both repositories are owned by the GitHub account `gkamga`. The GitHub
contributor API lists **one contributor** for each (`gkamga`); the local commit
authors are the account owner plus the automation identities
`LoomSphere Factory`, `LoomSphere Builder`, and `LoomSphere Extraction Agent`,
all using `@loomsphere.invalid` addresses.

The application repository does not vendor this code a second time. It declares
the harness as a Git submodule:

```text
[submodule ".harness"]
	path = .harness
	url = https://github.com/gkamga/loomsphere-harness.git
```

`diff -rq ~/loomsphere/.harness/src ~/loomsphere-harness/src` showed no content
difference other than interpreter-specific `__pycache__` files.

The assets were authored in the application repository and then moved out, not
copied in from an external project:

| Repository | Commit | Change |
| --- | --- | --- |
| LoomSphere application | `4156876` (2026-11-03) | `feat(harness): extract the trusted assets into the loomsphere-harness repository` — deletes the asset files from the application |
| LoomSphere harness | `51707b6` | `chore: adopt the .github/ canonical layout and drop application CI` — adds the same files under `.github/` |

## Inventory

### First-party source code

`src/loomsphere_harness/*` (30 tracked files) is the standalone runtime. Its
modules are relocated LoomSphere application services; `docs/RUNTIME.md`
documents the import surface as re-exports of same-named `app.*` services. No
vendored third-party source, generated binding, or copied library exists in this
directory.

`tools/*` (4 files) and `tests/*` (16 files) are first-party engineering
utilities and tests written for this repository.

### Trusted agent assets

`.github/` contains 42 tracked files, of which 41 are manifest-tracked assets:

| Bucket | Files |
| --- | --- |
| `agents` | 9 (7 agent definitions, 2 specialist catalogs) |
| `skills` | 13 |
| `contracts` | 17 (4 JSON Schemas and 13 example records) |
| `hooks` | 1 |
| `instructions` | 1 (`copilot-instructions.md`) |

All were authored under this owner's account and moved from the application as
described above. No file carries a copyright line, an SPDX identifier, an
attribution notice, or a statement that it was adapted from another project.

### Documentation

`README.md` and `docs/**` are first-party prose describing this repository's own
behaviour. `docs/decisions/` and other application-owned paths are deliberately
absent.

### Generated files

`harness.manifest.json` is generated from first-party files by
`tools/build_manifest.py`; it is a digest index, not third-party content.

### Third-party components

Nothing third-party is copied into the repository. The following are
**referenced at a distance** and keep their own licenses:

| Component | How it is used | License (observed from installed metadata) | Redistributed here? |
| --- | --- | --- | --- |
| `pydantic` 2.14.2 | runtime dependency | MIT | No |
| `pydantic-ai-slim` 2.65.0 | runtime dependency | MIT | No |
| `pydantic-ai-harness` 0.65.0 | runtime dependency | MIT | No |
| `PyYAML` 6.0.3 | dev/CI dependency | MIT | No |
| `jsonschema` 4.26.0 | dev/CI dependency | MIT | No |
| `pytest` 8.4.2 | installed *inside* the sandbox image, not in this package | MIT | No (installed by the consumer when building the image) |
| `python:3.12-slim` | base image in `docker/pytest-sandbox/Dockerfile` | Python-2.0 plus Debian component licenses | No (pulled by the consumer) |
| GitHub Actions (`actions/checkout`, `actions/setup-python`) | referenced by commit SHA in CI | MIT | No |

No dependency's license is changed by this repository. Consumers install these
packages themselves and remain bound by their terms.

## Notices

- No `LICENSE`, `NOTICE`, or `COPYING` file exists in either repository now, and
  `git log --all -- LICENSE LICENSE.txt NOTICE` is empty in both: no notice was
  ever committed and later removed.
- No copyright, SPDX, or attribution marker exists in the tracked tree.
- No third-party license text is embedded in source, tests, documentation, or
  the Dockerfile.

## Residual uncertainty

The audit establishes authorship and absence of third-party material. It cannot
establish the following, and does not assume them:

1. **Employment or assignment terms.** The commits are made from the owner's
   personal account; no employer IP assignment or contractor agreement was
   available to review.
2. **Prose originality.** The absence of attribution markers and the single-author
   history are evidence of first-party authorship. No external similarity search
   was run, so independently reproduced wording cannot be ruled out.
3. **Model-generated text.** Some commit content was produced with AI assistance
   under the owner's direction. Copyright in AI-assisted output is a legal
   question this audit does not resolve.

None of these is a file-level redistribution blocker: no identified file contains
third-party material, and the owner has recorded the decision to license the
repository. They are recorded so the conclusion is not read as broader than the
evidence supports.

## Conclusion

**No unresolved file-level licensing blocker was found.** Every tracked file is
first-party work authored under the owner's account, or a generated index of such
work. The declared dependencies and the container base image are referenced, not
redistributed, and are all permissively licensed.

Apache License 2.0 can therefore be applied to the repository as a whole. Because
no third-party material is included, no `NOTICE` file is required by section 4(d)
of that license; none is added.

See [`../LICENSE`](../LICENSE) for the license text, and the licensing section of
[`../README.md`](../README.md) for the consumer-facing statement.
