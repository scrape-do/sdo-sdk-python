<!-- SPDX-License-Identifier: Apache-2.0 -->
# Migrating `sdo-client`

This document is the authoritative record of breaking changes in the Python SDK
(`sdo-client` on PyPI). It exists so that upgrading is never a guessing game: every
removal that lands in a new major version is documented here, with the
deprecation that preceded it and the replacement you should adopt.

If you are upgrading across a major boundary (e.g. `1.x → 2.x`), read the
section for the major you are moving *to*. Sections are append-only — older
guidance is never rewritten.

## Compatibility policy

`sdo-client` follows [Semantic Versioning](https://semver.org/). Before 1.0, a
minor version may contain breaking changes; each is marked **Breaking** in
[CHANGELOG.md](CHANGELOG.md) and described in this file. Patch versions never
break.

The request parameters, defaults and observable behaviour are shared by all sdo
SDKs and pinned by the conformance corpus (`conformance.json`), which Scrape.do
maintains against https://scrape.do/documentation, and the exported names and
signatures are recorded in `api-surface.txt`, so "what is public" is not a
matter of opinion.

The public surface is:

- everything importable from the top-level `sdo` package (clients, request and
  response models, exceptions, enums);
- the constructor and method signatures of those symbols;
- documented keyword arguments and their defaults;
- the shape of response models (field names, types, nullability);
- error types and their attributes.

Anything whose name starts with an underscore (a module such as
`sdo._transport`, a function, a method or an attribute) is **not** public and
may change in any release.

### Within a major version: additive only

While the major version stays the same, changes are strictly additive and
backward compatible:

- **New** clients, methods, keyword arguments, response fields, exception types,
  and enum members may be added in a minor or patch release.
- New keyword arguments always carry a default that preserves prior behavior.
- New response fields may appear; code that reads fields by name is unaffected.
  Do not assume an exhaustive set of fields, and do not rely on iteration order.
- **Nothing public is removed or renamed**, and no default changes in a way that
  alters observed behavior, within a major version.

Because new enum members and response fields can appear in a minor release, treat
unknown values defensively (e.g. avoid exhaustive `match` with no fallback over
SDK enums you do not control).

### Deprecation before removal

Public symbols are never removed without warning. Before anything public is
removed, it goes through a deprecation window:

1. **Announce.** The symbol starts emitting a `DeprecationWarning` at call
   time, naming the replacement. The docstring and the
   [CHANGELOG](./CHANGELOG.md) record the deprecation, and the replacement ships
   in the same release.
2. **Wait.** The deprecation stays in place for a minimum window of **at least 2
   minor releases AND at least 90 days**, whichever is longer. A symbol
   deprecated in `1.4.0` is therefore not eligible for removal before `1.6.0`,
   and not before 90 days have passed.
3. **Remove.** Removal happens only at the **next major version bump**, and only
   for symbols whose window has fully elapsed. Each removal is documented in the
   matching major's section below.

`DeprecationWarning` is silent by default in most Python runtimes. To surface
deprecations in CI before they become breaking, run with warnings as errors:

```bash
python -W error::DeprecationWarning -m pytest
```

or scope it to this SDK in code:

```python
import warnings

warnings.filterwarnings("error", category=DeprecationWarning, module=r"sdo(\.|$)")
```

### Authentication stability

Authentication is token-only: an explicit `token=` argument, else the
`SDO_TOKEN` environment variable. Removing or renaming either, or adding any
other required auth mechanism, is a breaking change and will be called out
here.

### What is explicitly *not* covered

- Behavior of the remote Scrape.do API itself (rate limits, credit accounting,
  target-site responses). The SDK transports it; it does not version it.
- Wire-format details of internal HTTP requests.
- Anything whose name starts with an underscore.

### How to upgrade safely

1. Pin a major in your dependency spec, e.g. `sdo-client>=1.4,<2`.
2. Enable `error::DeprecationWarning` in CI and fix warnings as they appear, on
   your own schedule, while still on the current major.
3. When the next major is published, read its section here, apply the documented
   replacements, then lift your pin to the new major.

---

## Before 1.0

No version has been published yet, so there is nothing to migrate from. The
first releases are `0.x`: the additive-and-deprecate intent above is already in
force, but a minor version may still contain a breaking change before `1.0.0`;
each one is marked **Breaking** in the changelog and described here. Patch
versions never break. Pin a minor (`sdo-client==0.1.*`) if you need maximum
stability during this period.

---

<!--
Template for future majors. Copy this block above this comment when cutting a
new major release. Keep sections append-only; never edit a published major's
guidance.

## X.0.0

Summary of why this major was cut and the headline breaking changes.

### Removed

| Removed | Deprecated in | Replacement |
| ------- | ------------- | ----------- |
| `sdo.old_symbol` | `(W-1).N.0` | `sdo.new_symbol` |

### Changed defaults / behavior

- Describe any default or behavior change, with before/after.

### Action required

```python
# before
...
# after
...
```
-->
