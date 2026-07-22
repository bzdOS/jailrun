# jailrun — GNU-vs-BSD Substitution Policy

**Status: pre-alpha, first pass.** This document is the first concrete
artifact for ROADMAP.md's 0.3 "Substitution policy per entry" item. It
states the policy, then audits `providers/provider-map.json` as it actually
stands today against that policy — honestly, including the parts that
aren't done yet. It does not add or change any binary→provider mapping in
`provider-map.json`; the only new data is `providers/coreutils-flavor.json`,
which *labels* mappings that already exist (see "What changed" at the
bottom).

---

## The rule (verbatim from ROADMAP.md, milestone 0.3)

> Substitution policy per entry — GNU-vs-BSD userland semantics is the
> deepest risk of the whole thesis (a script that assumes GNU `tar`/`sed`
> flags silently gets BSD behavior): conservative default = prefer
> GNU-flavored ports (`gtar`, `gsed`, coreutils) for coreutils-class
> binaries unless L3 proves the BSD variant compatible.

Two things follow directly from that sentence:

1. **Default posture is GNU**, not BSD, for coreutils-class binaries. FreeBSD
   base tools are not "the safe choice" here — an image built against a
   Linux distro's coreutils is exactly the case this guards against.
2. **The escape hatch requires L3 ("behaves")** — a differential test
   against the Linux original — not just L1/L2 (exists/runs). Merely
   confirming the BSD binary is present and executes is not enough to
   justify defaulting away from GNU under this policy.

## What "coreutils-class" means here

Classic Unix text/file utilities where GNU and BSD implementations have
historically-known, well-established flag/behavior differences. This audit
used the canonical example set: `tar`, `sed`, `grep`, `awk`, `find`, `cp`,
`ls`, `diff`, `patch`, `date`, `du`, `sort` — then intersected it with the
keys that actually exist in `providers/provider-map.json` today. Nothing
outside that intersection was added to `providers/coreutils-flavor.json`.

## Method for classifying an existing entry as gnu/bsd

Purely mechanical, derived from what `provider-map.json` **already**
resolves the binary to — no new mappings, no guessing:

- Provider string is (or wraps) a GNU-named port/package — e.g.
  `pkg:gtar` (the `g` prefix is FreeBSD's own packaging convention for a
  GNU-flavored alternative to a base-system tool) → **`gnu`**.
- Provider string is a bare base-system path or a non-GNU port → **`bsd`**.

## Findings: coreutils-class binaries currently in provider-map.json

| binary | provider-map.json entry (today, unchanged) | flavor | verification evidence |
|---|---|---|---|
| `tar` | `"tar": "pkg:gtar"` | **gnu** | none stored in the static registry — see "On verification" below |

That's it — **`tar` is the only canonical coreutils-class binary that
currently exists as a key in `provider-map.json`.** `sed`, `grep`, `awk`,
`find`, `cp`, `ls`, `diff`, `patch`, `date`, `du`, and `sort` are simply not
in the registry yet, in either flavor. `providers/coreutils-flavor.json`
therefore has exactly one entry: `{"tar": "gnu"}`.

### On verification

`schemas/substitution-manifest.schema.json` already defines a proper L0–L4
trust ladder (`guessed → exists → runs → behaves → proven`) under
`binaries[].native.verification` — but that field lives in the
**per-image substitution manifest**, produced at probe(S2)/bakery(S4) time
for a specific image run, not in the static `providers/provider-map.json`
registry or in the new `providers/coreutils-flavor.json` file. Neither of
those two static files carries any per-entry verification metadata today.
So: is `tar → pkg:gtar` actually confirmed GNU-tar-compatible on a real
image, at some L-level? **Unknown from the static data alone** — the
current jailrun schema has nowhere in `provider-map.json` to record that,
and this task did not add one (out of scope: adding a verification field to
`provider-map.json`'s entry shape would be a real, separate schema change,
and no such change was made here). Whoever is running images that pull in
`tar` would need to check that specific image's generated substitution
manifest to know its actual L-level, if one has been generated at all.

## Current policy gaps this document surfaces

These are the honest, current gaps — not hypothetical ones:

- **Zero coreutils-class binaries are mapped to a `bsd` flavor with no
  stated verification.** The one entry that exists (`tar`) already follows
  the GNU-default policy. So today there is no *wrong-flavor* violation —
  but only because the registry barely covers this category yet (see next
  point).
- **Coverage gap, not flavor gap: 11 of the 12 canonical coreutils-class
  binaries aren't in `provider-map.json` at all.** `sed`, `grep`, `awk`,
  `find`, `cp`, `ls`, `diff`, `patch`, `date`, `du`, `sort` have no entry,
  GNU or BSD. The ROADMAP.md 0.3 "conservative default" policy can't be
  said to be *implemented* for these — there's simply nothing to default
  yet. Any image that shells out to one of these today gets whatever
  `probe.py`'s classification logic does for an unmapped binary (see
  `probe/probe.py`), not a considered GNU-vs-BSD decision.
- **No per-entry verification metadata in the static registry.** As noted
  above, `provider-map.json` and `coreutils-flavor.json` have no field for
  L0–L4 status; that information, where it exists at all, lives only in
  per-image substitution manifests generated separately. A future registry
  schema change would be needed to track "this static mapping has been
  verified to L-level N in general," if that's ever wanted independently of
  any specific image run.

## Candidates worth investigating on a real FreeBSD host (unverified hypotheses — NOT facts, NOT added to provider-map.json)

Everything in this section is a guess about what *might* exist in FreeBSD's
package/ports collection, offered as a starting point for a future
VM-based verification task — never presented as confirmed, and none of it
has been written into `provider-map.json` or `coreutils-flavor.json`.

- **`sed` → possibly `textproc/gsed` (pkg name `gsed`).** Notably,
  `providers/pkg-artifacts.json` *already* contains a real, existing entry
  `"gsed": "/usr/local/bin/gsed"`, and `bakery/bakery.py` already lists
  `"gsed"` as a build dependency for an existing recipe (grep
  `bakery/bakery.py` for `gsed`). That's existing repo data, not a new
  guess — but it is a `pkg_deps` entry for a *build recipe*, not a
  `provider-map.json` entry for the binary `sed` itself. Whether wiring
  `"sed": "pkg:gsed"` into `provider-map.json` is correct still needs the
  same L1+ confirmation as any other entry — it is not done here.
- **`awk` → possibly `lang/gawk` (pkg name `gawk`).** Same situation as
  `gsed`: `providers/pkg-artifacts.json` already has
  `"gawk": "/usr/local/bin/gawk"` and `bakery/bakery.py` already depends on
  it for an existing recipe, but no `provider-map.json` entry currently
  routes the binary name `awk` to it.
- **`patch` → possibly `devel/patch` / pkg name `gpatch`.** One existing
  mention in this repo: `bakery/bakery.py`'s `xtensa-lx106-elf` recipe's
  free-text `notes` field (a manual crosstool-NG bootstrap walkthrough)
  contains the line `--with-patch=/usr/local/bin/gpatch`. This is *not* a
  `pkg_deps` entry, not a `PKG_ARTIFACTS` entry, and not a
  `provider-map.json` mapping — it's a comment describing a hypothetical
  manual build step for an already-`"stub"`/`"experimental"`-status recipe.
  It's weaker evidence than the `gsed`/`gawk` case above (those are real
  wired `pkg_deps` + `PKG_ARTIFACTS` entries), but it's still an existing,
  cite-able hint rather than a fabricated one.
- **`grep`, `find`, `cp`, `ls`, `diff`, `date`, `du`, `sort` — no
  corresponding GNU-flavored pkg/port name appears anywhere in this repo's
  data today.** It is plausible FreeBSD's package collection carries
  GNU-flavored alternatives for some of these (GNU findutils/coreutils are
  common FreeBSD ports in general), but this task did not check a real
  FreeBSD host or pkg repository to confirm any specific package name, and
  making one up would violate the anti-fabrication constraint this task was
  given. **This is flagged as an open question for a future task that has
  real FreeBSD/`pkg search` access — not a recommendation to add anything
  yet.**
- **Binaries already in `provider-map.json` but outside this pass's
  canonical list** — `gzip`, `bzip2`, `zip`, `unzip`, `make`/`gmake` — also
  have real historical GNU/BSD behavioral differences (e.g. GNU tar's
  `--sort`, GNU sed's `-i` semantics have analogues in gzip's `-k`, BSD
  make vs GNU make syntax). They were intentionally excluded from this
  audit because the task scoped "coreutils-class" to the specific canonical
  list above. Whether to fold them into `coreutils-flavor.json` under an
  expanded definition is a decision for a follow-up task, not made here.

## What changed (scope of this task)

- **`providers/provider-map.json` was not modified in any way.** No
  existing binary→provider mapping was added, removed, or changed.
- **New, purely additive file: `providers/coreutils-flavor.json`** — a
  separate binary→flavor label file (`{"tar": "gnu"}` today), validated by
  a new `coreutils_flavor` `$defs` entry in `providers/registry.schema.json`.
- **New loader constant `COREUTILS_FLAVOR: dict[str, str]`** in
  `providers/__init__.py`, loaded the same cwd-independent way as the
  existing constants. It is **not** wired into `probe.py` or `bakery.py`
  yet — that's explicitly a follow-up, not part of this change.
