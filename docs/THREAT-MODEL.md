# jailrun — Threat Model

**Status: pre-alpha.** This document describes what jailrun actually defends
today, grounded in the real code (not the aspiration), and is explicit about
what it does not defend yet. It is part of the 0.6 milestone ("privilege
reduction & adversarial campaign") — see [ROADMAP.md](../ROADMAP.md)'s "Known
gaps" section, which this document expands on rather than duplicates.

jailrun's job is to run an **untrusted OCI image** — arbitrary Linux
userland pulled from a registry, content jailrun's authors never saw — inside
a FreeBSD jail, while the *unpack* step that gets it there runs **as root on
the host**. That combination (untrusted content + root-privileged
preprocessing) is the whole reason this document exists.

Four surfaces matter. Each section: who's attacking what, through which
door, what stops them today (cited to the actual code), and what's still
open.

---

## Surface (a) — Untrusted OCI image layers, parsed as root, during unpack

**Attacker:** whoever published (or tampered with) the OCI image being
pulled — a malicious registry, a compromised upstream image, or a
man-in-the-middle if the pull transport were ever weakened (it isn't
today: `skopeo copy docker://...` — see `store/store.py`'s `resolve()`).

**Asset:** the **host filesystem** outside the rootfs being unpacked, and
the integrity of the rootfs itself. This is the highest-stakes surface in
the whole project, because `_unpack_bsdtar()` (`store/store.py`) runs with
full host root privileges while processing bytes an attacker fully
controls — there is no sandboxing of the unpack step itself (see Residual
gaps).

**Attack shapes:** a layer's tar stream can contain any entry type tar
supports: regular files, directories, symlinks, hardlinks, device nodes,
and OCI's own whiteout markers (`.wh.<name>`, `.wh..wh..opq`). The classic
exploit family here is **symlink write-through**: a layer plants a symlink
at some path (e.g. `usr/x -> /` or `usr/x -> ../../../../etc`), and either
that same layer or a later one ships a file *underneath* that path (`usr/x/authorized_keys`),
hoping the extractor writes through the symlink onto the real host
filesystem instead of failing or staying contained.

**Current mitigation (cited):**

- **`_within(path, root)`** (`store/store.py`) is the single containment
  primitive everything else is built on: it resolves `path` (following any
  symlinks in its existing prefix — including multi-hop chains, since
  `Path.resolve()` dereferences the whole chain, not just one hop) and
  checks the result is `root` or a descendant of it. Fails closed (returns
  `False`) on `OSError`/`RuntimeError` (e.g. a symlink loop), not open.
- **`_merge_tree(src, dst)`** — the layer-copy step — computes
  `target = dst / rel` for every entry and calls
  `_within(target.parent, dst_real)` *before* creating anything; a
  resolved escape raises `StoreError`. It also never lets an **inherited
  symlink** stand in for a directory or a file it's about to write: if the
  current entry is a directory but `target` is currently a symlink (planted
  by an earlier layer), the symlink is unlinked and replaced with a real
  directory *before* descending into it; likewise for files. In practice
  this means a write-through attempt against a directory-shaped path is
  neutralized silently (the stale symlink is destroyed and replaced) rather
  than needing to hit the `_within` check at all — `store/test_layer_adversarial.py`'s
  multi-hop and relative-symlink tests confirm this is what actually
  happens, not just what's intended.
- **`_clear_opaque_whiteout()`** and **`_apply_file_whiteout()`** — a
  *second*, independent escape class from the same threat: an **earlier**
  layer's legitimate symlink (real images do this) combined with a
  **later** layer's whiteout marker under that same path must not cause the
  whiteout processing to delete/iterate through the symlink onto the host.
  Both functions check `_within` on the target (whiteout) / target's parent
  (opaque) before touching anything, and raise `StoreError` without
  deleting anything on an escape. `_rm_rf()`'s own symlink handling
  (`path.unlink()` for any symlink, never following it) is a second layer
  of the same defense: even where a symlink legitimately exists *inside*
  the rootfs and gets deleted as part of a whiteout, only the link itself
  is removed, never its target.
- Two real symlink-escape bugs were found and fixed this way; both are
  pinned down as regression tests in `store/test_merge_tree.py`
  (`test_symlink_write_through_is_prevented`,
  `test_opaque_whiteout_symlink_escape_is_prevented`,
  `test_file_whiteout_symlink_escape_is_prevented`).
- `store/test_layer_adversarial.py` (added for this milestone) extends that
  corpus: multi-hop symlink chains, **relative** (`../..`-shaped, not just
  absolute-path) escape targets, hardlink games, plain (non-symlink) `../`
  traversal against `_within` directly, whiteout markers nested *underneath*
  an earlier layer's symlink (not just at the symlinked path itself), and
  device-node-style special files (`FIFO`/`AF_UNIX` socket — the two
  special-file types representable without root). **No new escape was
  found in this pass** — see that file's module docstring
  (`REPORT_ON_FINDINGS`) for the one non-security exception-type
  inconsistency it did surface (a special-file entry raises
  `shutil.SpecialFileError`, not `StoreError` — fails closed, wrong
  exception type, documented not fixed).
- Hardlinks specifically: `_merge_tree` only ever uses `shutil.copy2`
  (content copy) or `os.symlink` — it never calls `os.link`. A hardlinked
  source file's *content* gets copied into the rootfs, but the destination
  is always an independent inode; a later in-container write can never
  corrupt a host file through a shared inode. Verified in
  `test_hardlink_is_flattened_to_independent_copy_not_live_link`.

**Residual gaps (honest):**

- **The unpack step itself is not sandboxed.** `_unpack_bsdtar()` and its
  helpers run with full root privileges directly on the host filesystem —
  there is no sub-jail, no `Capsicum` capability-mode restriction, no
  reduced-privilege user for this specific step (see ROADMAP.md: "No
  Capsicum"). Every defense above is *application-level* correctness in
  Python, not a kernel-enforced sandbox around the process doing the
  extracting. A bug in `_within`, `_merge_tree`, or a case neither
  anticipates is a direct root-level host compromise, not a jailed one.
- **The raw `bsdtar` extraction step (before `_merge_tree` ever runs) is a
  separate, real trust boundary this test suite deliberately does not
  exercise.** `_unpack_bsdtar()` extracts each layer's tar blob straight
  into a *fresh temp directory* via `bsdtar -xf ... --no-same-owner` with no
  explicit safety flags (no `--no-overwrite-dir`, no explicit
  secure-symlinks option passed). Whether that extraction itself already
  defends against a single layer that plants a symlink and writes through
  it *within that same extraction* depends entirely on the installed
  libarchive's own security-extraction defaults on the target FreeBSD host
  — already marked `# UNVERIFIED` in `store.py`, and out of scope for these
  Linux-host, pure-Python regression tests (there is no `bsdtar` on this
  host to shell out to). This is the single most important unverified
  claim in this whole surface.
- **Device nodes and xattrs are effectively unhandled by the bsdtar
  fallback path.** The module docstring says as much (`_unpack_bsdtar`:
  "Device node creation requires root; ... xattr support via bsdtar depends
  on the libarchive build flags. # UNVERIFIED"). A malicious layer shipping
  a real device-node tar entry (char/block) has never been exercised at
  all against the real bsdtar path; this suite's FIFO/socket tests are the
  closest representable-without-root stand-in, not a substitute.
  `_remove_whiteout_markers()`'s final residual-`.wh.*`-scrub pass is the
  same story: best-effort, `OSError` silently ignored.
  `devfs_ruleset` is separately never exercised against a hardened
  configuration (see Surface (b) below and ROADMAP.md).
- **Content-addressing (`image_id = sha256(sorted(layer_digests))`) is not
  a signature.** It makes re-pulls of the same content reproducible; it
  does not authenticate that the content is what the tag's publisher
  intended, or reject a compromised/typosquatted registry image. There is
  no image-signing verification (cosign/notation or similar) anywhere in
  the pull path.

---

## Surface (b) — Untrusted code executing inside the jail

**Attacker:** the process(es) started by `jexec` inside the running jail —
i.e. the image's own entrypoint/command, fully attacker-influenced (it's
the untrusted image's own payload, possibly with attacker-controlled
arguments too if a caller passes untrusted `cmd`/`env`/`workdir` through).

**Asset:** host resources (CPU, memory, disk I/O, process table), the
host's network, and — worst case — the boundary between "confined to the
jail" and "not."

**Current mitigation (cited, `runtime/engine.py` unless noted):**

- **Network default-deny.** `_build_jail_conf()` defaults `network="none"`,
  which renders `ip4 = disable; ip6 = disable;` — the jail gets no network
  stack at all unless the caller explicitly passes `--network inherit`
  (`runtime/cli.py`'s `--network` flag, `choices=("none", "inherit")`,
  `default="none"`). The rationale is recorded in the function's own
  docstring: package/toolchain provisioning already happens on the **host**
  side (`store.resolve()`'s `skopeo` pull, `bakery`'s `pkg`/port installs
  into the base dataset) *before* the jail exists, so the native-first path
  genuinely needs no network at `jexec` time. README.md documents this is
  verified behavior, not just code: "`ping` fails with 'Protocol not
  supported' by default."
- **`allow_raw_sockets`** defaults `False` (`runtime/cli.py`'s
  `--allow-raw-sockets`); the docstring is explicit that turning it on is
  host-network-wide (plain jails have no VNET isolation), so it's opt-in
  only for commands that actually need it (ping-like diagnostics).
- **Resource exhaustion via `rctl`.** `DEFAULT_RCTL_RULES` sets
  `memoryuse:sigkill=8g`, `pcpu:sigkill=400`, `maxproc:deny=512`,
  `cputime:sigkill=3600`, `readbps/writebps:throttle=200m`, applied via
  `_apply_rctl()` after jail creation and best-effort cleared via
  `_clear_rctl()` on teardown. The code carries a specific, dated
  confirmation of *why* `sigkill` (not `deny`) is used for accumulating
  resources: `deny` is a no-op for anything that isn't checked at the
  moment of a discrete action (a busy-loop under `cputime:deny=2` ran
  straight through an outer 30s timeout unaffected; `cputime:sigkill=2`
  killed it in ~2s — see the comment above `DEFAULT_RCTL_RULES`). Operators
  can override the whole rule set via `--rctl-rule` or disable it via
  `--no-rctl`.
- **Timeouts kill the whole jail, not just the wrapper.** `_stream_jexec()`'s
  `TimeoutError` handler explicitly does *not* trust `proc.kill()` alone:
  the comment records a confirmed failure mode where a hung `sleep 300`
  spawned as a grandchild via `jexec ... sh -c 'cmd1; cmd2'` kept running
  *inside the jail* after the tracked wrapper PID was killed (SIGKILL to a
  parent doesn't cascade to already-forked children). The fix is `jail -r`
  on timeout, which removes by **jail membership** at the kernel level,
  independent of the process tree — the actual mechanism that stops a
  wedged/malicious build from continuing to run.
- **`stdin=DEVNULL`** on the jexec'd process — deliberate, not accidental:
  a jailed process must never blindly inherit jailrun's own stdin, which in
  at least one real transport (bsdOS guest-agent EXEC) is not a normal
  fd a child can use at all (confirmed: `-it`/interactive is an explicit,
  documented stub, not implemented).
- **Teardown always runs** (`_run_async`'s `finally` block): `jail -r`
  (with the *same* `conf_path` used at `-c` time, so `mount +=` entries for
  Linuxulator pseudo-filesystems actually get unmounted — a bare `jail -r`
  was confirmed live to leave them mounted, which then made
  `zfs destroy` fail), then `rctl -r` to clear resource rules, then
  (`--rm`) `store.destroy()`.

**Residual gaps (honest):**

- **No Capsicum.** This is the single biggest structural gap on this
  surface (ROADMAP.md lists it explicitly): jail(8) confinement is the
  *only* isolation primitive in play. There is no capability-mode
  sandboxing layered underneath it, and no plan yet for one.
- **Network isolation is all-or-nothing, not destination-scoped.**
  `--network inherit` hands the jail the *entire* host network stack (no
  VNET, no per-jail `pf`/`ipfw` anchor) — there is no way today to say
  "only `pkg.freebsd.org` and `github.com` during a build, nothing after."
  A real fix needs VNET + a scoped firewall anchor per jail, which
  `_build_jail_conf`'s own docstring already flags as "a separate, larger
  change."
- **`rctl` action semantics are only individually confirmed for
  `cputime`.** `memoryuse`/`pcpu` use `sigkill` by analogy (same
  accumulating-resource category as `cputime`), not separately
  stress-tested; `readbps`/`writebps`'s `throttle` action has never been
  tested against real disk-saturating input. The numeric limits themselves
  (`8g`, `400`, `3600`, `200m`) are explicitly "provisional... NOT yet
  fully profiled against a real esphome/platformio build" per the comment
  above `DEFAULT_RCTL_RULES`.
- **`devfs_ruleset` is never set explicitly.** `_build_jail_conf` only
  emits `mount.devfs;` with no `devfs_ruleset=` parameter, so the jail gets
  whichever devfs ruleset the *host* has configured as its default — which
  is outside jailrun's control and has only ever been exercised against a
  default (non-hardened) devfs configuration (ROADMAP.md: "never exercised
  against a hardened FreeBSD devfs configuration").
- **`rctl` requires `kern.racct.enable=1`, a boot-time loader tunable.**
  `_apply_rctl` degrades gracefully (logs a warning, applies nothing) if
  racct is off — which means on a host where the operator forgot to set
  it, *every* resource limit in this section silently does not apply, with
  only a log line to notice it.
- **The residual jail-escape surface is whatever FreeBSD's own jail(8) has
  historically had** (privilege-escalation bugs in the jail implementation
  itself, e.g. via certain devfs nodes, certain syscalls not properly
  jail-aware, etc.) — jailrun adds no defense-in-depth against that class
  at all; it inherits the host kernel's jail hardening wholesale. This is
  precisely the gap Capsicum-on-top-of-jails (ROADMAP item 5) would
  address, and precisely why it hasn't been started.

---

## Surface (c) — `-v` volume destination containment

**Attacker:** whoever controls the *destination* half of a `-v host:ctr`
bind mount. Usually this is the operator's own trusted input — but
`runtime/cli.py`'s `_volume_spec()` and `store.py`'s `Store.mount()` both
carry the same explicit note: a caller may build `ctr` from data derived
from an untrusted source (e.g. a component name pulled from user upload),
so the destination cannot be assumed safe just because it usually is.

**Asset:** any host path outside the rootfs that `mount_nullfs` could be
pointed at if the destination weren't checked (e.g. `../../../../etc`).

**Current mitigation (cited, `store/store.py`'s `Store.mount()`):**

- `dest_rel = str(dest_raw).lstrip("/")`, then
  `dest_path = handle.rootfs / dest_rel`, then — **before any directory is
  created or any subprocess spawned** — `_within(dest_path, rootfs_real)`
  is checked; a `../../etc`-shaped destination resolves outside the rootfs
  and raises `StoreError` immediately. Only after that check passes does
  `dest_path.mkdir(parents=True, exist_ok=True)` run, and only after *that*
  does `mount_nullfs` get invoked.
- `store/test_mount_containment.py` pins this down directly:
  `test_dotdot_escape_is_refused_before_any_subprocess` asserts the
  `StoreError` is raised and `handle.mounts` stays empty (i.e. nothing was
  even attempted); `test_normal_dest_still_mkdirs_inside_rootfs` asserts a
  legitimate destination is *not* incorrectly rejected by the same guard.
- The same `_within` primitive backs this and Surface (a) — one
  containment implementation, not two independently-maintained ones that
  could drift apart.

**Residual gaps (honest):**

- The check validates the **destination** only. The **host** side
  (`host_path` in `-v host:ctr`) is never validated at all — by design,
  since it's the operator's own filesystem and jailrun has no basis to
  second-guess it, but worth stating explicitly: there is no allowlist of
  "safe" host paths, and an operator who scripts `-v` from
  untrusted input on the *host* side has no protection here.
- `mount_nullfs` has no uid/gid remapping (documented, not a bug): a host
  path bind-mounted in appears with the host's own uid numbers inside the
  jail. Not a containment break, but a real information/permission
  consideration operators need to know about.
- This check runs once, at mount time. There's no re-validation if the
  underlying filesystem changes shape between the check and the
  `mount_nullfs` call (a TOCTOU window) — considered low-risk today because
  there's no untrusted *concurrent* process racing this operation in the
  current single-run model, but worth naming rather than assuming away.

---

## Surface (d) — Substitution manifest / native-shadow path

**Attacker:** the OCI image content itself — specifically, the *names* it
gives its own files, which is the only signal `probe/probe.py` uses to
decide what's substitutable.

**Asset:** which binary ends up on `PATH` ahead of the image's own Linux
binaries, and — one step further back — what gets `pkg install`ed onto the
host-side bakery base dataset in the first place.

**How the pipeline actually decides "native" (cited):**

- `probe.py`'s `propose_native(name)` looks up `PROVIDER_MAP` **purely by
  the binary's lower-cased basename** — there is no content inspection, no
  hash check, nothing about the actual bytes of the image's binary. So an
  attacker who names *any* file `python3`, `gcc`, `curl`, etc. (any
  `PROVIDER_MAP` key) and ships it as a Linux-ABI ELF gets
  `status: "native"` proposed for it, purely from the filename.
- Critically, that classification does **not** mean the attacker's own
  bytes get treated as trusted or run natively. `bakery/bakery.py`'s
  `plan_to_provision_cmd()` builds the actual `pkg install`/`make install`
  commands **only from its own hardcoded `PKG_ARTIFACTS`/`PORT_ARTIFACTS`
  tables** — the module's own comment on `plan_to_provision_cmd` states
  this explicitly: "pkg names/port origins in a plan are only ever drawn
  from this module's own tables... nothing here originates from untrusted
  OCI-layer content" — and applies `shlex.quote()` regardless, as
  defense-in-depth. The artifact that ends up symlinked into
  `/jailrun-native/bin/` in `runtime/engine.py`'s
  `_assemble_native_shadow()` always comes from the **bakery's own
  host-provisioned base dataset**, bind-mounted in at
  `NATIVE_BASE_MOUNT` — never from the image's own layer content.
- **Artifact-reality is enforced before shadowing.** `_assemble_native_shadow()`
  checks `(Path(rootfs_path) / in_jail_path.lstrip("/")).exists()` — through
  the actual mount — before ever creating a symlink; a resolved-but-missing
  artifact is skipped with a warning, never silently shadowed as a phantom.
  This is `ARCHITECTURE.md`'s cross-seam invariant #1
  ("Artifact-reality... never shadow a phantom binary"), and it's the
  concrete answer to "could a crafted image get an *arbitrary* path
  shadowed": no — only a path that bakery already resolved to a real,
  existing, table-driven artifact ever gets linked.
- One narrower case does use the image's own filename directly in
  constructing a path: `bakery.py`'s `fill_artifact_paths()`, for packages
  in `MULTI_BINARY_PKGS` (e.g. `binutils`, which installs many
  differently-named tools, not one binary matching the package name), sets
  `native["artifact_path"] = f"/usr/local/bin/{Path(binary['path']).name}"`
  — i.e. the *image's own binary basename*, not a table lookup. This is
  still bounded (`Path.name` can only ever be a single path component —
  it cannot contain `/`, so no traversal is possible through it, and a
  real file named literally `..` isn't creatable on a POSIX filesystem
  either) and still gated by the same artifact-existence check before
  shadowing — so this cannot be used to point outside `/usr/local/bin/`,
  and cannot shadow a nonexistent path. The realistic residual effect is
  **name confusion, not code execution**: an attacker can cause their own
  file named e.g. `ld` to trigger shadowing with whatever *actually*
  exists at `/usr/local/bin/ld` in the shared base — which is exactly the
  intended substitution behavior, just triggerable by a name the attacker
  picked rather than one the operator picked.

**Residual gaps (honest):**

- **An attacker's image can trigger arbitrary installs from the fixed
  provider table onto the *shared*, host-side bakery base dataset**, simply
  by shipping files named to match `PROVIDER_MAP` keys. The install targets
  are bounded to jailrun's own hardcoded package/port list (not attacker-
  chosen package names), but the *decision to install* is attacker-
  influenced, and `register_base()`'s base dataset is cached/shared by
  provision-command hash (`store.py`'s `register_base`) — a later,
  unrelated, legitimate run that happens to need the same recipe hash
  reuses that same base. This is intentional CoW sharing working as
  designed, not a bug, but it does mean one image's classification
  decisions can shape what gets installed onto infrastructure another
  image's run later depends on.
- **The provider table itself is thin and heuristic**, per ROADMAP.md: a
  real `esphome/esphome:stable` benchmark already hit a dozen-plus
  "not in table; guessing..." warnings. `verification: "guessed"` (L0 of
  the schema's trust ladder) is the honest label for exactly this — probe
  never claims more confidence than "the name matched."
- **No image-content verification of "native" candidates at all** — there
  is no equivalent of "check this Linux binary's actual behavior against
  the native substitute" before wiring the substitute in; that's L2+ of the
  verification ladder (`runs`/`behaves`/`proven`) and is explicitly future
  work ("a future agent verification harness"), not present today.

---

## What this document is not

This is a description of what exists and what's been checked, not a
promise of imperviousness. jailrun is pre-alpha, greenfield systems
software (see `ARCHITECTURE.md`'s own HONEST STATUS banner); "verified
against deliberately-bad input" (README.md) is true and worth something,
but it is not the same claim as "audited" or "hardened." See
[SECURITY.md](../SECURITY.md) for how to report something this document
got wrong or missed.
