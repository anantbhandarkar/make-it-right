"""Diagram generation and the DIA check family. Owned by workstream V.

Discovered and run automatically by test_init.py's `for _name in ... test_*.py` loop, which
requires each such module to define `run(check)` -- see test_init.py's own comment on that
contract. `check` is injected rather than imported so this module pulls in no test framework
of its own and still counts into test_init.py's single tally.

These tests are written against the CLASS of defect, not against today's fifteen diagrams:

  A. **The no-timestamp invariant is load-bearing and is proved by mutation.** The last test
     here patches a stamp into a copy of the generator and asserts `--check` then fails on a
     tree nobody touched. Without that test, the invariant is a comment in a docstring, and
     the day someone adds `generated_at` "for traceability" every check goes red for a reason
     that has nothing to do with the diagrams -- and the whole family gets switched off
     inside a week. This is the single most important test in the file.

  B. **The rendering rules are asserted mechanically, because they cannot be eyeballed here.**
     No browser runs in this suite, so instead every emitted block is parsed against the
     Mermaid 10 subset the generator claims to target, and the light/dark rules are checked
     as text: no pinned `theme:`, and every styled node setting `fill`, `stroke` AND `color`.
     `fill` without `color` is the specific bug that yields light-on-light text in dark mode,
     and it is invisible to anyone testing in one theme.

  C. **Sharding refuses rather than warns at the ceiling, and a runtime in no shard is
     loud.** Both are tested by re-grouping the real backend runtimes, not by a synthetic
     fixture, so the test exercises the policy that actually ships.

Every test that needs to write runs against a SANDBOX root -- a temp directory with a
`skills` symlink back into the checkout -- so the suite never touches the real documents and
never depends on what another workstream has half-landed in `skills/` this minute.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
GEN_PATH = REPO_ROOT / "docs" / "gen_diagrams.py"

_LOADS = [0]


def _load(root, source=None, mutate=None):
    """Load a (possibly mutated) copy of the generator, pointed at `root`.

    Registered in sys.modules before exec_module: @dataclass resolves string annotations
    through sys.modules[cls.__module__], so a module executed while absent from it dies on
    its first dataclass with an AttributeError that names nothing useful.
    """
    _LOADS[0] += 1
    name = "mir_gen_diagrams_test_%d" % _LOADS[0]
    path = Path(source) if source else GEN_PATH
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    mod.ROOT = Path(root)
    mod.INIT_DIR = REPO_ROOT / "init"
    if mutate:
        mutate(mod)
    return mod


def _sandbox(tmp, ids):
    """A repo-shaped temp root: real skills/ (read-only, via symlink) and one host document."""
    root = Path(tmp) / "repo"
    (root / "docs").mkdir(parents=True)
    os.symlink(REPO_ROOT / "skills", root / "skills")
    doc = root / "docs" / "host.md"
    doc.write_text("# host\n\n" + "\n\n".join(
        "<!-- mir:gen:begin id=%s src=docs/gen_diagrams.py -->\n"
        "<!-- mir:gen:end id=%s -->" % (i, i) for i in ids) + "\n", encoding="utf-8")
    return root, doc


# -- the Mermaid 10 subset this generator claims to target -------------------------------
# Deliberately strict: anything not matched by one of these is a construct nobody has
# checked renders on GitHub, and "it probably works" is how a broken diagram ships.

_HEADER = re.compile(r"^(flowchart (LR|TD)|sequenceDiagram)$")
_ACC = re.compile(r"^    acc(Title|Descr): \S.*$")
_NODE = re.compile(r'^    ([a-z][A-Za-z0-9_]*)(\["(.*)"\]|\{"(.*)"\})$')
_EDGE = re.compile(r"^    [a-z][A-Za-z0-9_]* (-->|-\.->)(\|[A-Za-z0-9 ]+\|)? [a-z][A-Za-z0-9_]*$")
_CLASSDEF = re.compile(r"^    classDef ([a-z]+) (.*)$")
_CLASS = re.compile(r"^    class [A-Za-z0-9_,]+ [a-z]+$")
_PARTICIPANT = re.compile(r"^    participant [a-z][A-Za-z0-9_]* as [A-Za-z0-9 ._/-]+$")
_MESSAGE = re.compile(r"^    [a-z][A-Za-z0-9_]*(->>|-->>)[a-z][A-Za-z0-9_]*: \S.*$")

_HEX3 = re.compile(r"^fill:(#[0-9a-f]{6}),stroke:(#[0-9a-f]{6}),color:(#[0-9a-f]{6})$")


def _mermaid_blocks(body):
    """Every ```mermaid ... ``` region in a rendered block body."""
    out, cur = [], None
    for line in body.splitlines():
        if line.strip() == "```mermaid":
            cur = []
            continue
        if cur is not None and line.strip() == "```":
            out.append(cur)
            cur = None
            continue
        if cur is not None:
            cur.append(line)
    return out


def _label_of(line):
    m = _NODE.match(line)
    if not m:
        return None
    return m.group(3) if m.group(3) is not None else m.group(4)


def _check_mermaid(check, did, lines):
    body = "\n".join(lines)

    # Rule A. GitHub re-colours a Mermaid block for the reader's light/dark preference ONLY
    # if the block does not pin a theme. Pinning one guarantees one mode is unreadable, and
    # it is the single commonest way this ships broken.
    check("%s pins no theme and no init directive" % did,
          "theme" not in body and "%%{" not in body and not body.startswith("---"), body[:80])
    check("%s uses no v11-only -beta diagram type" % did, "-beta" not in body)
    check("%s emits no click directive (dead ink under GitHub securityLevel strict)" % did,
          not re.search(r"^\s*click ", body, re.M))

    check("%s starts with a supported header" % did, bool(_HEADER.match(lines[0])), lines[0])
    check("%s declares accTitle and accDescr" % did,
          _ACC.match(lines[1]) and _ACC.match(lines[2]), lines[1:3])

    styled_classes, defined_classes = set(), {}
    bad = []
    for line in lines[3:]:
        if (_NODE.match(line) or _EDGE.match(line) or _CLASS.match(line)
                or _PARTICIPANT.match(line) or _MESSAGE.match(line)):
            if _CLASS.match(line):
                styled_classes.add(line.split()[-1])
            continue
        m = _CLASSDEF.match(line)
        if m:
            defined_classes[m.group(1)] = m.group(2)
            continue
        bad.append(line)
    check("%s contains only node, edge, class, participant and message lines" % did,
          not bad, bad[:3])

    # Rule C. fill WITHOUT color is the bug: the node goes mid-tone in both themes and the
    # label stays theme-coloured, so one of the two modes renders light-on-light.
    for cls, spec in sorted(defined_classes.items()):
        check("%s classDef %s sets fill, stroke AND color, hex only" % (did, cls),
              bool(_HEX3.match(spec)), spec)
    check("%s defines a classDef for every class it applies" % did,
          styled_classes <= set(defined_classes), sorted(styled_classes - set(defined_classes)))

    labels = [lab for lab in (_label_of(x) for x in lines) if lab is not None]
    for lab in labels:
        stripped = lab.replace("<br/>", "")
        check("%s label %r has no backtick and no raw angle bracket" % (did, lab[:40]),
              "`" not in stripped and "<" not in stripped and ">" not in stripped, lab)


def run(check) -> None:
    print("gen_diagrams -- the generator against the real tree")
    gen = _load(REPO_ROOT)
    ids = gen.diagram_ids()
    check("--list produces at least one diagram per pillar plus the stable set",
          len(ids) >= 12, str(ids))
    check("ids are unique", len(ids) == len(set(ids)))
    check("the worked example is asserted to exist, not assumed",
          gen.WORKED_EXAMPLE in gen.catalog().scan_skills(), gen.WORKED_EXAMPLE)

    print("the Mermaid 10 subset, and the light/dark rules (no browser runs here)")
    for did in ids:
        body = gen.render_body(did, REPO_ROOT / "README.md")
        blocks = _mermaid_blocks(body)
        check("%s emits exactly one mermaid block" % did, len(blocks) == 1, str(len(blocks)))
        if blocks:
            _check_mermaid(check, did, blocks[0])
        check("%s ships a text version, because Mermaid conveys no structure to a screen "
              "reader" % did, "<summary>Text version" in body)
        check("%s node count is within the hard ceiling" % did,
              gen.build(did).node_count() <= gen.MAX_NODES)

    print("generated links resolve (a diagram's link list is the only navigable one)")
    # A skill link resolves to skills/<slug>/SKILL.md, and the DIRECTORY is what is asserted
    # here. A directory that exists with no SKILL.md inside it is already a validate.py SK001
    # error, and a suite that also fails on it just reports someone else's half-landed work
    # twice -- which is how a test file acquires a reputation for being flaky.
    dead = []
    for did in ids:
        for link in gen.build(did).links:
            if not link.target:
                continue
            target = REPO_ROOT / link.target
            probe = target.parent if link.target.startswith("skills/") else target
            if not probe.exists():
                dead.append((did, link.target))
    check("every generated link points at something that exists", not dead, str(dead[:4]))

    print("no clock, no network, no hash inside a managed block")
    src = GEN_PATH.read_text(encoding="utf-8")
    check("the generator imports no clock or network module",
          not re.search(r"^\s*(import|from)\s+(time|datetime|random|urllib|socket|"
                        r"http\.client|hashlib|subprocess)\b", src, re.M))
    joined = "\n".join(gen.render_body(d, REPO_ROOT / "README.md") for d in ids)
    check("no rendered block contains a year-like stamp",
          not re.search(r"\b20[0-9]{2}-[0-9]{2}-[0-9]{2}\b", joined))

    with tempfile.TemporaryDirectory() as tmp:
        print("--check passes on freshly written output")
        root, doc = _sandbox(tmp, ids)
        sb = _load(root)
        written = sb.write_all()
        check("--write wrote the host document", written == ["docs/host.md"], str(written))
        r = sb.check_all()
        check("--check is clean immediately after --write",
              not r["drift"] and not r["markers"],
              str(r["drift"][:1]) + str(r["markers"][:1]))
        check("every id is reported as embedded", sorted(r["embedded"]) == sorted(ids))
        check("nothing is reported as unembedded", r["unembedded"] == [], str(r["unembedded"]))

        print("--write is idempotent (a second run is a no-op, which is what --check asserts)")
        check("re-running --write changes nothing", sb.write_all() == [], "second write mutated")

        print("a hand-edit inside a managed block is caught (DIA001)")
        text = doc.read_text(encoding="utf-8")
        doc.write_text(text.replace("mir-backend", "mir-backdoor", 1), encoding="utf-8")
        r = sb.check_all()
        check("a one-word edit inside a block is drift", len(r["drift"]) == 1, str(len(r["drift"])))
        check("the drift report carries a unified diff a human can act on",
              r["drift"] and "@@" in r["drift"][0][2] and "mir-backdoor" in r["drift"][0][2])
        sb.write_all()
        check("--write repairs the hand-edit", not sb.check_all()["drift"])

        print("marker problems are caught (DIA002)")
        good = doc.read_text(encoding="utf-8")

        doc.write_text(good.replace("<!-- mir:gen:begin id=%s src=docs/gen_diagrams.py -->"
                                    % ids[0], "<!-- mir:gen:begin id=%s -->" % ids[0]),
                       encoding="utf-8")
        msgs = [m for _f, _l, m in sb.check_all()["markers"]]
        check("a begin marker missing src= is malformed",
              any("malformed" in m for m in msgs), str(msgs[:2]))

        # The LAST block, so nothing follows it: that is the case where an unclosed marker
        # would put the whole rest of the file inside the managed region and --write would
        # delete it. Dropping an interior end marker is a different (also caught) shape.
        doc.write_text(good.replace("<!-- mir:gen:end id=%s -->" % ids[-1], ""), encoding="utf-8")
        msgs = [m for _f, _l, m in sb.check_all()["markers"]]
        check("an unclosed begin marker is caught before --write can swallow the file",
              any("never closed" in m for m in msgs), str(msgs[:2]))
        check("--write refuses a document with malformed markers",
              _raises(sb.write_all, sb.DiagramError))

        doc.write_text(good.replace("<!-- mir:gen:end id=%s -->" % ids[0], ""), encoding="utf-8")
        msgs = [m for _f, _l, m in sb.check_all()["markers"]]
        check("a begin marker opening while another is still open is caught",
              any("still open" in m for m in msgs), str(msgs[:2]))

        doc.write_text(good + "\n<!-- mir:gen:begin id=no-such-diagram src=docs/gen_diagrams.py "
                              "-->\n<!-- mir:gen:end id=no-such-diagram -->\n", encoding="utf-8")
        msgs = [m for _f, _l, m in sb.check_all()["markers"]]
        check("an unknown diagram id is caught",
              any("unknown diagram id" in m for m in msgs), str(msgs[:2]))

        dup = root / "docs" / "dup.md"
        dup.write_text("<!-- mir:gen:begin id=%s src=docs/gen_diagrams.py -->\n"
                       "<!-- mir:gen:end id=%s -->\n" % (ids[0], ids[0]), encoding="utf-8")
        doc.write_text(good, encoding="utf-8")
        msgs = [m for _f, _l, m in sb.check_all()["markers"]]
        check("the same id embedded twice is caught -- two copies cannot be kept in sync",
              any("already embedded" in m for m in msgs), str(msgs[:2]))
        dup.unlink()

        print("a marker shown as an EXAMPLE is documentation, not a block")
        ex = root / "docs" / "example.md"
        ex.write_text("Use this shape:\n\n```html\n"
                      "<!-- mir:gen:begin id=%s src=docs/gen_diagrams.py -->\n"
                      "<!-- mir:gen:end id=%s -->\n```\n\n"
                      "Prose may also name `mir:gen:` without being a marker.\n"
                      % (ids[0], ids[0]), encoding="utf-8")
        before = ex.read_text(encoding="utf-8")
        r = sb.check_all()
        sb.write_all()
        check("a fenced example marker is neither generated into nor reported as duplicate",
              ex.read_text(encoding="utf-8") == before
              and not any("already embedded" in m for _f, _l, m in r["markers"]))
        check("prose naming mir:gen is not reported as a malformed marker",
              not any("malformed" in m for _f, _l, m in r["markers"]),
              str(r["markers"][:2]))
        ex.unlink()

        print("an id nobody embeds is reported (DIA004), not silently forgotten")
        doc.write_text("\n\n".join(
            "<!-- mir:gen:begin id=%s src=docs/gen_diagrams.py -->\n"
            "<!-- mir:gen:end id=%s -->" % (i, i) for i in ids[1:]) + "\n", encoding="utf-8")
        r = sb.check_all()
        check("a diagram embedded in no document is reported as unembedded",
              r["unembedded"] == [ids[0]], str(r["unembedded"]))

        print("sharding is enforced by the generator, not by the author's judgement")
        every = sorted(r_ for _k, _t, ms in gen.BACKEND_RUNTIME_GROUPS for r_ in ms)

        one = _load(REPO_ROOT)
        one.BACKEND_RUNTIME_GROUPS = [("all", "everything", every)]
        err = _raises(lambda: one.build("tree-backend-all"), one.DiagramError, want_msg=True)
        check("a single backend shard is REFUSED, not emitted, past MAX_NODES",
              err and "MAX_NODES" in err, str(err)[:120])

        two = _load(REPO_ROOT)
        two.BACKEND_RUNTIME_GROUPS = [
            ("big", "big", [r_ for r_ in every if r_ in ("python", "node", "go", "jvm", "rust")]),
            ("rest", "rest", [r_ for r_ in every
                              if r_ not in ("python", "node", "go", "jvm", "rust")])]
        over = dict(two.check_all()["oversize"])
        check("a shard between SOFT_NODES and MAX_NODES warns instead of refusing",
              "tree-backend-big" in over and over["tree-backend-big"] > two.SOFT_NODES,
              str(over))

        gap = _load(REPO_ROOT)
        gap.BACKEND_RUNTIME_GROUPS = [("some", "some", [r_ for r_ in every if r_ != "python"])]
        err = _raises(gap.diagram_ids, gap.DiagramError, want_msg=True)
        check("a backend runtime in no shard FAILS LOUDLY instead of vanishing from the tree",
              err and "python" in err, str(err)[:160])

        print("MUTATION -- the no-timestamp invariant is load-bearing, not decorative")
        # The generator is copied and a stamp is injected at the documented anchor, exactly
        # as `init/generate.py` legitimately stamps its own artifacts. The sandbox document
        # was written by the clean generator and has not been touched since, so ANY drift
        # the mutant reports is caused by the stamp and by nothing else. If this test ever
        # fails, someone has made the diagram blocks non-deterministic and --check has
        # started failing on clean trees -- which is how the whole mechanism becomes noise.
        doc.write_text("\n\n".join(
            "<!-- mir:gen:begin id=%s src=docs/gen_diagrams.py -->\n"
            "<!-- mir:gen:end id=%s -->" % (i, i) for i in ids) + "\n", encoding="utf-8")
        sb.write_all()
        check("precondition -- the sandbox tree is clean before the mutation",
              not sb.check_all()["drift"])

        anchor = "    out: list = []"
        check("the mutation anchor still exists in the generator", anchor in src)
        mutant_path = Path(tmp) / "gen_diagrams_stamped.py"
        mutant_path.write_text(src.replace(
            anchor,
            '    import time as _t\n'
            '    out: list = ["<!-- generated_at: %s -->" % _t.time(), ""]', 1),
            encoding="utf-8")
        mutant = _load(root, source=mutant_path)
        drift = mutant.check_all()["drift"]
        check("a generator that stamps a timestamp makes --check fail on an UNMODIFIED tree",
              len(drift) == len(ids), "%d of %d blocks drifted" % (len(drift), len(ids)))
        check("the drift is the stamp itself, so the failure names its own cause",
              drift and "generated_at" in drift[0][2], str(drift[:1])[:200])


def _raises(fn, exc, want_msg=False):
    try:
        fn()
    except exc as e:
        return str(e) if want_msg else True
    except Exception:
        return False
    return False
