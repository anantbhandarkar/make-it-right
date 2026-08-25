"""Installer prune suite (install.sh). Loaded by test_init.py, which injects `check`.

What is under test is a REMOVAL path that runs inside a user's $HOME, so the tests are
written against the two failure directions separately:

  under-pruning  a link this checkout wrote, now stale, survives -- the user keeps a skill
                 name that loads nothing, and the scope they thought they reduced is not
                 reduced. This is what the defect was.
  over-pruning   a link or file this checkout did NOT write is removed -- strictly worse,
                 because it destroys someone else's setup. Every ownership test here
                 exists to pin one way that could happen.

Everything drives install.sh as a subprocess with CLAUDE_HOME / CODEX_HOME / GEMINI_HOME
aimed at a throwaway directory, because the unit under test is the shell script's
behaviour on a real filesystem, not a Python re-implementation of its rules.
"""

from __future__ import annotations

import os
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
INSTALL = os.path.join(REPO, "install.sh")
SKILLS = os.path.join(REPO, "skills")
AGENTS = os.path.join(REPO, "agents")


def _run(home, *args):
    """Run install.sh with every home override pointed at `home`."""
    env = dict(os.environ)
    env["HOME"] = home
    env["CLAUDE_HOME"] = os.path.join(home, ".claude")
    env["CODEX_HOME"] = os.path.join(home, ".codex")
    env["GEMINI_HOME"] = os.path.join(home, ".gemini")
    return subprocess.run(["bash", INSTALL, *args], cwd=REPO, env=env,
                          capture_output=True, text=True)


def _skills_dir(home):
    return os.path.join(home, ".claude", "skills")


def _codex_skills_dir(home):
    return os.path.join(home, ".codex", "skills")


def _agents_dir(home):
    return os.path.join(home, ".claude", "agents")


def _names(d):
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def _snapshot(root):
    """Every path under `root` as (relpath, kind, symlink target).

    Symlinks are leaves: descending through one would report the checkout's contents, not
    the user's tree, and a prune that only unlinks must show up as a change here.
    """
    out = []
    stack = [root]
    while stack:
        cur = stack.pop()
        for name in sorted(os.listdir(cur)):
            p = os.path.join(cur, name)
            rel = os.path.relpath(p, root)
            if os.path.islink(p):
                out.append((rel, "link", os.readlink(p)))
            elif os.path.isdir(p):
                out.append((rel, "dir", ""))
                stack.append(p)
            else:
                out.append((rel, "file", ""))
    return sorted(out)


def _pillars():
    """Depth-1 slugs, by the same one-dash rule install.sh's --scope=pillars uses."""
    return sorted(d for d in os.listdir(SKILLS)
                  if os.path.isdir(os.path.join(SKILLS, d)) and d.count("-") == 1)


def _all_skills():
    return sorted(d for d in os.listdir(SKILLS) if os.path.isdir(os.path.join(SKILLS, d)))


def run(check):
    # -- consequence 2: reducing scope used to write 7 links, leave 39, and say it worked --
    with tempfile.TemporaryDirectory() as home:
        r = _run(home, "--tool=claude", "--scope=all")
        check("install --scope=all succeeds", r.returncode == 0, r.stderr[-300:])
        wide = _names(_skills_dir(home))
        check("--scope=all links every skill in the tree",
              wide == _all_skills(), f"{len(wide)} links vs {len(_all_skills())} skills")

        r = _run(home, "--tool=claude", "--scope=pillars", "--prune")
        check("--scope=pillars --prune succeeds", r.returncode == 0, r.stderr[-300:])
        narrow = _names(_skills_dir(home))
        check("--scope=pillars --prune actually REDUCES the global link count",
              narrow == _pillars(),
              f"expected {len(_pillars())} pillars, got {len(narrow)}: {narrow[:10]}")
        check("narrowing scope leaves no tier or module behind",
              not [s for s in narrow if s.count("-") > 1],
              str([s for s in narrow if s.count("-") > 1]))

    # -- ownership: a mir-* link into ANOTHER checkout is not ours to delete --------------
    with tempfile.TemporaryDirectory() as home:
        _run(home, "--tool=claude", "--scope=pillars")
        other = os.path.join(home, "other-checkout", "skills", "mir-foo")
        os.makedirs(other)
        outside = os.path.join(_skills_dir(home), "mir-foo")
        os.symlink(other, outside)

        r = _run(home, "--tool=claude", "--prune-only")
        check("a mir-* link into a different checkout SURVIVES prune",
              os.path.islink(outside) and os.readlink(outside) == other,
              r.stdout[-300:])
        check("prune says KEEP rather than deleting silently or pretending it removed it",
              "KEEP" in r.stdout and "mir-foo" in r.stdout, r.stdout[-300:])
        check("a link into THIS checkout is pruned in the same pass",
              "mir-backend" not in _names(_skills_dir(home)),
              str(_names(_skills_dir(home))))

    # -- ownership: never touch a real file or directory ---------------------------------
    with tempfile.TemporaryDirectory() as home:
        _run(home, "--tool=claude", "--scope=pillars")
        real = os.path.join(_skills_dir(home), "mir-foo")
        os.makedirs(real)
        marker = os.path.join(real, "SKILL.md")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("a real skill the user wrote by hand\n")

        r = _run(home, "--tool=claude", "--prune-only")
        check("a real DIRECTORY named mir-foo is never touched",
              os.path.isdir(real) and not os.path.islink(real), r.stdout[-300:])
        check("its contents survive too (prune never recurses into a directory)",
              os.path.isfile(marker) and open(marker, encoding="utf-8").read().startswith("a real"))

        plain = os.path.join(_agents_dir(home), "my-reviewer.md")
        with open(plain, "w", encoding="utf-8") as fh:
            fh.write("hand-written agent\n")
        _run(home, "--tool=claude", "--prune-only")
        check("a real FILE in the agents dir is never touched",
              os.path.isfile(plain) and not os.path.islink(plain))

    # -- the defect itself: dangling links, in both target dirs --------------------------
    with tempfile.TemporaryDirectory() as home:
        _run(home, "--tool=claude", "--scope=pillars")
        # A skill renamed away in a later release. The target does not exist, so anything
        # that resolves paths with realpath(1) would error out and skip exactly this case.
        dead_skill = os.path.join(_skills_dir(home), "mir-renamed-away")
        os.symlink(os.path.join(SKILLS, "mir-renamed-away"), dead_skill)
        dead_agent = os.path.join(_agents_dir(home), "deleted-reviewer.md")
        os.symlink(os.path.join(AGENTS, "deleted-reviewer.md"), dead_agent)
        check("setup really is dangling (the link exists, the target does not)",
              os.path.islink(dead_skill) and not os.path.exists(dead_skill))

        r = _run(home, "--tool=claude", "--prune-only")
        check("a dangling mir-* skill link is pruned",
              not os.path.islink(dead_skill), r.stdout[-300:])
        check("a dangling agents/*.md link is pruned",
              not os.path.islink(dead_agent), r.stdout[-300:])

    # -- --dry-run changes nothing, measured against a full before/after listing ----------
    with tempfile.TemporaryDirectory() as home:
        _run(home, "--tool=all", "--scope=all")
        os.symlink(os.path.join(SKILLS, "mir-renamed-away"),
                   os.path.join(_skills_dir(home), "mir-renamed-away"))
        before = _snapshot(home)
        r = _run(home, "--tool=all", "--scope=pillars", "--prune", "--dry-run")
        after = _snapshot(home)
        check("--prune --dry-run changes nothing on disk", before == after,
              f"delta: {sorted(set(before) ^ set(after))[:6]}")
        check("--dry-run still REPORTS the removals it declined to make",
              "would remove" in r.stdout and "mir-renamed-away" in r.stdout, r.stdout[-300:])
        check("--dry-run does not install either (an unwritten link is a disk change too)",
              "would link" in r.stdout, r.stdout[-300:])

        # ...and the same run without --dry-run must actually do it, or the dry-run above
        # would be passing for the wrong reason.
        _run(home, "--tool=all", "--scope=pillars", "--prune")
        check("the same command without --dry-run does change the disk",
              _snapshot(home) != before)

    # -- a NORMAL install deletes nothing, but must say what it found --------------------
    with tempfile.TemporaryDirectory() as home:
        _run(home, "--tool=claude", "--scope=all")
        r = _run(home, "--tool=claude", "--scope=pillars")
        left = _names(_skills_dir(home))
        check("a normal install still removes nothing (removal stays opt-in)",
              len(left) == len(_all_skills()), f"{len(left)} links")
        check("a normal install WARNS about links it did not write",
              "WARN" in r.stderr and "--prune --dry-run" in r.stderr, r.stderr[-300:])

    # -- Antigravity: the live path is installed, the legacy path is only cleaned --------
    with tempfile.TemporaryDirectory() as home:
        legacy = os.path.join(home, ".gemini", "antigravity", "skills")
        os.makedirs(legacy)
        os.symlink(os.path.join(SKILLS, "mir-backend"), os.path.join(legacy, "mir-backend"))
        r = _run(home, "--tool=antigravity", "--scope=pillars")
        live = os.path.join(home, ".gemini", "config", "skills")
        check("antigravity installs into config/skills", "mir-backend" in _names(live))
        check("a normal install warns about the legacy antigravity path",
              "antigravity/skills" in r.stderr, r.stderr[-300:])

        _run(home, "--tool=antigravity", "--prune-only")
        check("--prune-only cleans the legacy antigravity path",
              _names(legacy) == [], str(_names(legacy)))
        check("--prune-only cleans the live antigravity path too",
              _names(live) == [], str(_names(live)))
        check("--prune-only removes the AGENTS.md link it installed",
              not os.path.islink(os.path.join(home, ".gemini", "AGENTS.md")))
        _run(home, "--tool=antigravity", "--scope=pillars")
        check("the legacy antigravity path is cleaned but never installed INTO",
              _names(legacy) == [] and "mir-backend" in _names(live), str(_names(legacy)))

    # -- prune-only must work on a tree that no longer validates -------------------------
    with tempfile.TemporaryDirectory() as home:
        _run(home, "--tool=claude", "--scope=pillars")
        r = _run(home, "--tool=claude", "--prune-only")
        check("--prune-only does not run validate.py (uninstall must work when install is broken)",
              "Validating" not in r.stdout and r.returncode == 0, r.stdout[:200])

    # -- Codex: skills, not just AGENTS.md ----------------------------------------------
    # Codex CLI reads $CODEX_HOME/skills (confirmed against codex-cli 0.147.0 with
    # `codex debug prompt-input`, which renders the model-visible prompt: a skill linked
    # there comes back in the "### Available skills" block, and the loader follows the
    # symlink). Before this, --tool=codex linked ONE file and told the user to register the
    # skills through /skills -- which only toggles skills already discovered from a root,
    # so a skill that is on no root can never be offered there. Codex got the always-on
    # baseline and none of the 46 skill bodies. These pin the paths so a future edit that
    # points them somewhere unread fails here rather than in a user's session.
    with tempfile.TemporaryDirectory() as home:
        r = _run(home, "--tool=codex", "--scope=all")
        check("install --tool=codex succeeds", r.returncode == 0, r.stderr[-300:])
        codex = _codex_skills_dir(home)
        check("codex gets every skill in $CODEX_HOME/skills, not just AGENTS.md",
              _names(codex) == _all_skills(),
              f"{len(_names(codex))} links vs {len(_all_skills())} skills")
        # A link is only worth anything if it resolves; the Antigravity defect was links
        # that existed and pointed nowhere the tool would ever look.
        broken = [n for n in _names(codex)
                  if not os.path.isfile(os.path.join(codex, n, "SKILL.md"))]
        check("every codex skill link resolves to a real SKILL.md", not broken, str(broken[:5]))
        check("codex still gets the AGENTS.md link",
              os.path.islink(os.path.join(home, ".codex", "AGENTS.md")))
        # $CODEX_HOME/agents is NOT written: the same probe found no flat agent directory
        # anywhere in Codex's rendered prompt. Asserting its absence keeps a well-meant
        # future edit from re-creating the "links nothing reads" bug.
        check("no agents dir is invented for codex",
              not os.path.isdir(os.path.join(home, ".codex", "agents")))

    with tempfile.TemporaryDirectory() as home:
        _run(home, "--tool=codex", "--scope=pillars")
        codex = _codex_skills_dir(home)
        check("--scope=pillars applies to codex too",
              _names(codex) == _pillars(), str(_names(codex)))
        r = _run(home, "--tool=codex", "--prune-only")
        check("--prune removes the codex skill links",
              _names(codex) == [], str(_names(codex)))
        check("--prune removes the codex AGENTS.md link too",
              not os.path.islink(os.path.join(home, ".codex", "AGENTS.md")), r.stdout[-300:])

    # -- a MOVED checkout: --force-prune-path, and every guard on it ---------------------
    # A moved (not re-cloned) checkout leaves links whose target path no longer exists.
    # That is indistinguishable on disk from a link into a colleague's deleted checkout,
    # so prune keeps them -- correctly, and forever. --force-prune-path is the user
    # supplying the missing fact. Because it is a deletion root read off the command line,
    # each guard gets its own assertion; the over-pruning direction is the dangerous one.
    def _moved(home, old):
        """Links in a fresh $HOME that point into `old` with this repo's layout."""
        os.makedirs(_skills_dir(home))
        os.makedirs(_agents_dir(home))
        for name in ("mir-backend", "mir-frontend"):
            os.symlink(os.path.join(old, "skills", name),
                       os.path.join(_skills_dir(home), name))
        os.symlink(os.path.join(old, "agents", "security-reviewer.md"),
                   os.path.join(_agents_dir(home), "security-reviewer.md"))

    with tempfile.TemporaryDirectory() as sandbox:
        old = os.path.join(sandbox, "old-checkout")   # never created: the checkout MOVED

        with tempfile.TemporaryDirectory() as home:
            _moved(home, old)
            r = _run(home, "--tool=claude", "--prune-only")
            check("without the flag, a moved checkout's links are KEPT",
                  _names(_skills_dir(home)) == ["mir-backend", "mir-frontend"],
                  str(_names(_skills_dir(home))))
            check("...and prune says so, naming the escape hatch instead of failing mutely",
                  "--force-prune-path" in r.stdout, r.stdout[-400:])

        with tempfile.TemporaryDirectory() as home:
            _moved(home, old)
            before = _snapshot(home)
            r = _run(home, "--tool=claude", "--prune-only",
                     "--force-prune-path=" + old)
            check("--force-prune-path alone is DRY-RUN: nothing on disk changes",
                  _snapshot(home) == before, str(_snapshot(home)))
            check("...and it still prints what the confirmation would remove",
                  "would remove" in r.stdout and "dry-run by default" in r.stdout,
                  r.stdout[-400:])

        with tempfile.TemporaryDirectory() as home:
            _moved(home, old)
            r = _run(home, "--tool=claude", "--prune-only",
                     "--force-prune-path=" + old, "--confirm-force-prune")
            check("--force-prune-path + --confirm-force-prune adopts the moved links",
                  _names(_skills_dir(home)) == [], str(_names(_skills_dir(home))))
            check("the moved checkout's agents link is adopted too",
                  _names(_agents_dir(home)) == [], str(_names(_agents_dir(home))))

        # A root that is not where the checkout was matches nothing: the footprint test
        # requires the target to sit under <root>/skills or <root>/agents, this repo's own
        # layout, so a mistyped root cannot sweep up links it never wrote.
        with tempfile.TemporaryDirectory() as home:
            _moved(home, old)
            before = _snapshot(home)
            _run(home, "--tool=claude", "--prune-only",
                 "--force-prune-path=" + os.path.join(sandbox, "not-the-checkout"),
                 "--confirm-force-prune")
            check("a root with no make-it-right footprint removes nothing",
                  _snapshot(home) == before, str(sorted(set(before) ^ set(_snapshot(home)))))

        # The subtlest hole: a root that CONTAINS the install dir makes every link in that
        # dir resolve "under" it, footprint test included -- so it must be refused, per dir.
        with tempfile.TemporaryDirectory() as home:
            _moved(home, old)
            before = _snapshot(home)
            r = _run(home, "--tool=claude", "--prune-only",
                     "--force-prune-path=" + os.path.join(home, ".claude"),
                     "--confirm-force-prune")
            check("a root containing the install dir is REFUSED, not obeyed",
                  _snapshot(home) == before and "REFUSE" in r.stderr, r.stderr[-300:])

        for bad, why in (("/", "the filesystem root"),
                         ("relative/path", "a relative path")):
            with tempfile.TemporaryDirectory() as home:
                _moved(home, old)
                before = _snapshot(home)
                r = _run(home, "--tool=claude", "--prune-only",
                         "--force-prune-path=" + bad, "--confirm-force-prune")
                check("--force-prune-path refuses %s, and exits nonzero" % why,
                      r.returncode == 2 and _snapshot(home) == before, r.stderr[-200:])

        # $HOME itself. Passed through the env the script actually reads, not the caller's.
        with tempfile.TemporaryDirectory() as home:
            _moved(home, old)
            before = _snapshot(home)
            r = _run(home, "--tool=claude", "--prune-only",
                     "--force-prune-path=" + home, "--confirm-force-prune")
            check("--force-prune-path refuses $HOME, and exits nonzero",
                  r.returncode == 2 and _snapshot(home) == before, r.stderr[-200:])

        # Without a prune mode the flag can only mislead: a plain install would print
        # success while removing nothing, which reads as "the cleanup ran".
        with tempfile.TemporaryDirectory() as home:
            r = _run(home, "--tool=claude", "--force-prune-path=" + old)
            check("--force-prune-path without --prune/--prune-only is an error",
                  r.returncode == 2, r.stdout[-200:] + r.stderr[-200:])

        # Over-pruning guard: force mode widens which TARGETS count as ours. It must not
        # widen what may be REMOVED -- a real file or directory stays untouchable, and
        # nothing under the supplied root is ever touched at all.
        with tempfile.TemporaryDirectory() as home:
            real_old = os.path.join(sandbox, "still-there")
            os.makedirs(os.path.join(real_old, "skills", "mir-backend"))
            keeper = os.path.join(real_old, "skills", "mir-backend", "SKILL.md")
            with open(keeper, "w", encoding="utf-8") as fh:
                fh.write("a real file under the root the user named\n")
            _moved(home, real_old)
            hand_written = os.path.join(_skills_dir(home), "mir-mine")
            os.makedirs(hand_written)
            _run(home, "--tool=claude", "--prune-only",
                 "--force-prune-path=" + real_old, "--confirm-force-prune")
            check("force-prune never removes a real directory, only symlinks",
                  os.path.isdir(hand_written) and not os.path.islink(hand_written))
            check("force-prune never touches anything under the root it was given",
                  os.path.isfile(keeper))
            check("...while still unlinking the symlinks that root explains",
                  _names(_skills_dir(home)) == ["mir-mine"],
                  str(_names(_skills_dir(home))))
