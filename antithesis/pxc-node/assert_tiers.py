#!/usr/bin/env python3
"""Build-time table: which InnoDB assertion sites exist only in debug builds.

Why this exists: findings go to the Percona team, and every mysqld death must
say whether production would die the same way. The harness builds mysqld as
CMAKE_BUILD_TYPE=Debug (UNIV_DEBUG on, NDEBUG off) and Galera with debug=3
(NDEBUG off), so it checks invariants that a release build compiles out.

- A glibc `assert()` failure ("mysqld: FILE:LINE: FUNC: Assertion `X' failed.")
  is always debug-only: release builds define NDEBUG (cmake release types;
  Galera's SConstruct:92 opt_flags). The supervisor needs no table for those.
- An InnoDB failure ("[InnoDB] Assertion failure: FILE:LINE:EXPR") prints the
  same text for `ut_a` (kept in release builds) and `ut_ad` (debug only), and
  `ut_a` itself is debug-only when it sits inside `#ifdef UNIV_DEBUG`. Only the
  source can tell them apart, and the runtime image has no source. So this
  script reads the source once, at build time, and writes the verdict per
  FILE:LINE for the supervisor (pxc-node/entrypoint.sh, assert_tier) to look up.

Output, one line per source line that an assertion macro covers:

    <basename>:<line>\t<tier>\t<macro>\t<path>

tier is `debug_only` or `release`. InnoDB prints only the basename, so a
basename:line that two files share with different verdicts is written as
`unknown`.

Deliberately simple preprocessor tracking, not a real preprocessor:
- A group counts as debug-only when its condition requires UNIV_DEBUG
  (`#ifdef UNIV_DEBUG`, `#if defined(UNIV_DEBUG) && ...`), and as release-only
  when it requires its absence (`#ifndef UNIV_DEBUG`); `#else` flips that.
  Any `||` makes the group neutral.
- TODO: a `ut_a` in a function that only debug code calls, outside any
  UNIV_DEBUG block, is labeled `release`. Telling those apart needs a call
  graph; the label errs toward "production would crash too".
"""

from __future__ import annotations

import os
import re
import sys

MACRO = re.compile(r"\b(ut_ad|ut_a|ut_error|ut_d)\b")
DEBUG_MACROS = {"ut_ad", "ut_d"}
WORD = re.compile(r"\bUNIV_DEBUG\b")
EXTENSIONS = (".cc", ".c", ".h", ".ic", ".cpp")
# InnoDB and the plugins that link its headers. Scanning the whole tree would
# only add basename collisions with unrelated code.
ROOTS = ("storage/innobase", "plugin/innodb_memcached")


def condition_sign(directive: str, cond: str) -> int:
    """+1: the branch requires UNIV_DEBUG. -1: requires its absence. 0: neither."""
    if directive == "ifdef":
        return 1 if cond.strip() == "UNIV_DEBUG" else 0
    if directive == "ifndef":
        return -1 if cond.strip() == "UNIV_DEBUG" else 0
    if not WORD.search(cond) or "||" in cond:
        return 0
    negated = re.search(r"!\s*defined\s*\(?\s*UNIV_DEBUG\b", cond)
    return -1 if negated else 1


def scan(path: str) -> list[tuple[int, str, str]]:
    """(line, tier, macro) for every line an assertion macro covers."""
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().split("\n")

    # Per source line: True when an enclosing group requires UNIV_DEBUG.
    in_debug = [False] * (len(lines) + 1)
    stack: list[int] = []  # sign of the CURRENT branch of each open group
    for i, raw in enumerate(lines, start=1):
        m = re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)", raw)
        if m:
            directive, cond = m.group(1), m.group(2)
            if directive in ("if", "ifdef", "ifndef"):
                stack.append(condition_sign(directive, cond))
            elif directive == "elif" and stack:
                stack[-1] = condition_sign("if", cond)
            elif directive == "else" and stack:
                stack[-1] = -stack[-1]
            elif directive == "endif" and stack:
                stack.pop()
        in_debug[i] = any(s > 0 for s in stack)

    out: list[tuple[int, str, str]] = []
    for i, raw in enumerate(lines, start=1):
        code = raw.split("//", 1)[0]
        if code.lstrip().startswith(("*", "/*", "#")):
            continue
        for m in MACRO.finditer(code):
            macro = m.group(1)
            # Span of the invocation, so a multi-line ut_a(...) maps every
            # line: compilers disagree on which line __LINE__ reports.
            end = i
            depth = 0
            started = False
            for j in range(i, min(i + 40, len(lines)) + 1):
                text = lines[j - 1][m.end():] if j == i else lines[j - 1]
                for ch in text:
                    if ch == "(":
                        depth += 1
                        started = True
                    elif ch == ")":
                        depth -= 1
                if started and depth <= 0:
                    end = j
                    break
                if not started and j == i:
                    break  # ut_error has no argument list
            tier = "debug_only" if (macro in DEBUG_MACROS or in_debug[i]) else "release"
            for ln in range(i, end + 1):
                out.append((ln, tier, macro))
    return out


def main(src: str) -> int:
    verdicts: dict[str, set[str]] = {}
    detail: dict[str, tuple[str, str]] = {}
    for root in ROOTS:
        base = os.path.join(src, root)
        for dirpath, _, files in os.walk(base):
            for name in files:
                if not name.endswith(EXTENSIONS):
                    continue
                path = os.path.join(dirpath, name)
                for line, tier, macro in scan(path):
                    key = f"{name}:{line}"
                    verdicts.setdefault(key, set()).add(tier)
                    detail.setdefault(key, (macro, os.path.relpath(path, src)))
    for key in sorted(verdicts):
        tiers = verdicts[key]
        tier = tiers.pop() if len(tiers) == 1 else "unknown"
        macro, rel = detail[key]
        print(f"{key}\t{tier}\t{macro}\t{rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/src"))
