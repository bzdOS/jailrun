#!/usr/bin/env python3
# START_AI_HEADER
# MODULE: runtime/test_netpolicy.py
# PURPOSE: unit tests for runtime/netpolicy.py — the pure destination-scoped-
#          network policy (parse --network allow=, resolve hostnames, allocate a
#          jail IP, render the pf anchor text). All host touches (DNS, resolv.conf)
#          are injected, so this runs on Linux with no FreeBSD/pf — the fast
#          test loop the 0.4 feature otherwise lacks.
# DEPENDENCIES: stdlib, runtime.netpolicy
# PUBLIC_API: pytest test_* functions
# END_AI_HEADER
"""Unit tests for runtime/netpolicy.py. No FreeBSD/pf needed:

    python3 -m pytest runtime/test_netpolicy.py -v
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from runtime.netpolicy import (  # noqa: E402
    AllowEntry,
    NetworkSpec,
    alloc_jail_ip,
    anchor_name,
    host_resolvers,
    parse_network_spec,
    render_pf_anchor,
    resolve_allowlist,
    tag_for,
)


# ---- parse_network_spec ----

def test_parse_none_and_inherit():
    assert parse_network_spec("none") == NetworkSpec(kind="none")
    assert parse_network_spec("inherit") == NetworkSpec(kind="inherit")
    assert parse_network_spec("  inherit ") == NetworkSpec(kind="inherit")


def test_parse_allow_single_and_multi_with_ports():
    s = parse_network_spec("allow=github.com:443")
    assert s.kind == "allow"
    assert s.allow == (AllowEntry("github.com", 443),)
    s2 = parse_network_spec("allow=a.com:80,b.com,c.com:443")
    assert s2.allow == (
        AllowEntry("a.com", 80), AllowEntry("b.com", None), AllowEntry("c.com", 443),
    )


def test_parse_allow_no_port_means_all_ports():
    s = parse_network_spec("allow=example.com")
    assert s.allow == (AllowEntry("example.com", None),)


def test_parse_rejects_bad_specs():
    for bad in ["", "allow=", "bogus", "allow=:80", "allow=x:y", "allow=x:0",
                "allow=x:70000", "allow=x:80,", "allow"]:
        try:
            parse_network_spec(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_parse_port_range_enforced():
    for ok in ["allow=x:1", "allow=x:65535"]:
        parse_network_spec(ok)
    for bad in ["allow=x:0", "allow=x:65536", "allow=x:-1"]:
        try:
            parse_network_spec(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


# ---- resolve_allowlist ----

def test_resolve_expands_one_host_to_many_ips_and_dedupes():
    fake = {"a.com": ["1.1.1.1", "1.0.0.1"], "b.com": ["2.2.2.2"]}
    out = resolve_allowlist(
        [AllowEntry("a.com", 443), AllowEntry("b.com", None), AllowEntry("a.com", 443)],
        resolver=lambda h: fake[h],
    )
    assert out == [("1.1.1.1", 443), ("1.0.0.1", 443), ("2.2.2.2", None)]


def test_resolve_literal_ip_skips_dns():
    out = resolve_allowlist([AllowEntry("9.9.9.9", 53)], resolver=lambda h: 1/0)
    assert out == [("9.9.9.9", 53)]  # resolver never called for a literal IP


def test_resolve_unresolvable_name_raises():
    def boom(_h):
        raise ValueError("nope")
    try:
        resolve_allowlist([AllowEntry("ghost.invalid", None)], resolver=boom)
    except ValueError:
        return
    raise AssertionError("expected ValueError for unresolvable host")


# ---- host_resolvers ----

def test_host_resolvers_parses_nameserver_lines(tmp_path):
    f = tmp_path / "resolv.conf"
    f.write_text("nameserver 8.8.8.8\nnameserver 1.1.1.1\n# comment\nnameserver 8.8.8.8\n")
    assert host_resolvers(f) == ["8.8.8.8", "1.1.1.1"]


def test_host_resolvers_missing_file_returns_empty():
    assert host_resolvers("/nonexistent/resolv.conf") == []


# ---- alloc_jail_ip ----

def test_alloc_picks_lowest_free_skipping_gateway():
    assert alloc_jail_ip(used=set()) == "10.99.0.2"  # .1 skipped, .0 is network


def test_alloc_skips_used():
    assert alloc_jail_ip(used={"10.99.0.2", "10.99.0.3"}) == "10.99.0.4"


def test_alloc_exhausted_raises():
    # /30 hosts() = [.1, .2]; .1 is the gateway (skipped), mark .2 used -> exhausted.
    try:
        alloc_jail_ip(used={"10.99.0.2"}, network="10.99.0.0/30")
    except ValueError:
        return
    raise AssertionError("expected ValueError on exhausted pool")


# ---- render_pf_anchor ----

def test_render_uses_tagging_shape_proven_live():
    # The PROVEN shape (validated on FreeBSD 15.1): match tags + nat-to, then
    # pass/block by tag — a plain `block from jail_ip` does NOT match post-NAT.
    text = render_pf_anchor(
        jail_name="jr-abc", jail_ip="10.99.0.5", ext_if="vtnet1",
        resolved=[("140.82.121.4", 443), ("1.1.1.1", None)],
        resolvers=["8.8.8.8"],
    )
    tag = tag_for("jr-abc")
    assert tag == "jailrun_jr_abc"
    # match: tag + nat-to on the original src, with the required `out` direction.
    assert f"match out on vtnet1 inet from 10.99.0.5 to any tag {tag} nat-to (vtnet1)" in text
    # per-allowed pass by tag, ordered before the block
    assert f"pass out quick on vtnet1 inet proto {{ tcp udp }} to 140.82.121.4 port = 443 tagged {tag} keep state" in text
    # port=None -> no port clause
    assert f"to 1.1.1.1 tagged {tag} keep state" in text
    # resolver auto-allowed on 53
    assert f"to 8.8.8.8 port = 53 tagged {tag} keep state" in text
    # catch-all block LAST, by tag (NOT by jail_ip — that was the pre-tag failure)
    assert text.rstrip().endswith(f"block drop out quick on vtnet1 proto {{ tcp udp }} tagged {tag}")
    assert "from 10.99.0.5" not in text.split("block drop")[1]  # block is tag-based, not src-based


def test_tag_for_sanitizes_and_prefixes():
    assert tag_for("jr-abc") == "jailrun_jr_abc"
    assert tag_for("jr.foo/bar") == "jailrun_jr_foo_bar"


def test_anchor_name_is_per_jail():
    assert anchor_name("jr-abc") == "jailrun/jr-abc"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
