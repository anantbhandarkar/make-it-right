#!/usr/bin/env bash
# Make It Right — multi-tool installer.
# Installs the skills, reviewer agents, and AGENTS.md into the right location for your
# coding agent. Symlinks (not copies) so edits in this repo are live immediately.
#
# Runs ./validate.py first and refuses to install if it reports errors. A skill that
# names a skill nobody wrote loads nothing, silently — that is what this prevents.
#
# Usage:  ./install.sh [--tool=claude|cursor|codex|antigravity|all]   (default: claude)
#         ./install.sh --scope=pillars        install only the 7 pillars globally
#         ./install.sh --prune                remove this checkout's stale links, then install
#         ./install.sh --prune-only           remove them and stop (the uninstall path)
#         ./install.sh --prune --dry-run      print what prune would do, change nothing
#         ./install.sh --prune-only --force-prune-path=<old-dir> [--confirm-force-prune]
#                                             adopt links left by a MOVED checkout (see below)
#         CLAUDE_HOME / CODEX_HOME / GEMINI_HOME override the target dirs.
#
# Why --prune exists. Installing is `ln -sfn`, which overwrites but never removes. So a
# skill renamed or deleted in a later release leaves a symlink that resolves to nothing:
# the user sees a skill name that loads no content -- the silent failure this repo exists
# to prevent, reappearing one layer down at the install boundary. validate.py cannot see
# it, because validate.py validates the repo, not your $HOME. Worse, REDUCING scope was a
# no-op that reported success: --scope=pillars wrote 7 links and left the other 39 in
# place, then printed "linked 7 pillar(s) globally" while the global index was unchanged.
#
# Removal is opt-in, always. A plain ./install.sh never deletes anything in your home
# directory; it only counts what looks stale and points you at --prune --dry-run.
#
# --scope controls how much of the tree goes into the GLOBAL config:
#   all      (default)  every skill. Simple, but every repo pays for every skill's
#                       description at session start (about 15k tokens for 46 skills).
#   pillars             only the depth-1 skills (mir-backend, mir-frontend, mir-mobile,
#                       mir-database, mir-cloud, mir-devsecops, mir-init) -- about 2k
#                       tokens. Every repo still gets the gates; the runtime tiers and
#                       framework modules are installed per project by `mir init --install`,
#                       which resolves exactly the ones that repo needs.
# Claude Code merges ~/.claude/skills and <repo>/.claude/skills, so the two combine.
#
# Why --force-prune-path exists. Ownership is proved by resolving a link's target and
# checking it is inside THIS checkout. If the checkout was MOVED (not re-cloned in place),
# every old link now names a path that does not exist -- which is byte-for-byte
# indistinguishable from "a link into a colleague's checkout that they deleted". Prune
# refuses both, correctly, and the orphans stay forever. --force-prune-path=<old-dir> is
# the user supplying the one fact the script cannot derive: "that path used to be me."
# Because it means accepting a deletion root from the command line, it is fenced in by the
# guards documented at force_prune_root() and used in prune_dir()/prune_file(); read those
# before widening it. It is dry-run by default: --confirm-force-prune is the second key.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The PHYSICAL path of the checkout. Ownership is decided by comparing resolved paths, so
# the side we compare against has to be resolved too -- otherwise a $HOME reached through
# a symlink (or /tmp -> /private/tmp on macOS) would read as "some other checkout".
REPO_REAL="$(cd "$REPO_DIR" && pwd -P)"
TOOL="claude"
SCOPE="all"
PRUNE=0
PRUNE_ONLY=0
DRY_RUN=0
WROTE=""      # newline-separated dsts written this run; used to spot links we did NOT write
PRUNED_N=0
KEPT_N=0
FORCE_PATH=""       # raw --force-prune-path value; "" means "only this checkout is ours"
FORCE_CONFIRMED=0   # --confirm-force-prune; without it, --force-prune-path is dry-run
SAW_MOVED=0         # a KEEP whose target does not exist -- worth naming the flag once
for arg in "$@"; do
  case "$arg" in
    --tool=*)             TOOL="${arg#*=}" ;;
    --tool)               shift; TOOL="${1:-claude}" ;;
    --scope=*)            SCOPE="${arg#*=}" ;;
    --prune)              PRUNE=1 ;;
    --prune-only)         PRUNE_ONLY=1 ;;
    --dry-run)            DRY_RUN=1 ;;
    --force-prune-path=*) FORCE_PATH="${arg#*=}" ;;
    --confirm-force-prune) FORCE_CONFIRMED=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
case "$SCOPE" in
  all|pillars) ;;
  *) echo "unknown --scope: $SCOPE (use all or pillars)" >&2; exit 2 ;;
esac

link() {  # link <source> <target>  (skips real files, refuses to clobber non-symlinks)
  local src="$1" dst="$2"
  WROTE="$WROTE
$dst"
  if [ "$DRY_RUN" = 1 ]; then
    # Not even mkdir: --dry-run means the filesystem is identical afterwards, and a
    # created-but-empty ~/.claude/skills is still a change.
    echo "  DRY   would link $dst -> $src"
    return
  fi
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ] && [ ! -L "$dst" ]; then
    echo "  SKIP  $dst exists and is not a symlink — remove it first" >&2
    return
  fi
  ln -sfn "$src" "$dst"
  echo "  LINK  $dst -> $src"
}

# ── Ownership: the four rules that make removing things under $HOME safe ─────────
# This code deletes paths in a user's home directory, so it removes a link only when it
# can PROVE the link is ours. Guessing once is worse than never pruning at all.

resolve_path() {  # absolute, symlink-resolved path of $1 -- works when $1 does not exist
  # Resolve the deepest ANCESTOR that exists and keep the rest lexically. `realpath`/
  # `readlink -f` fail outright on a dangling path, and a dangling link is precisely what
  # we came to remove, so failing there would exempt the defect from the fix.
  #
  # Why the deepest ancestor rather than just the parent: after a checkout MOVES, a link's
  # whole target chain below the old root is gone, so parent-only resolution leaves the
  # target lexical (/tmp/old/skills/mir-x) while --force-prune-path=/tmp/old resolves one
  # level and comes back physical (/private/tmp/old). Two spellings of the same directory
  # then compare unequal and every link is kept. Both sides must normalise identically or
  # the comparison is measuring the spelling, not the path.
  local p="$1" d b tail
  # The root is its own parent and its own basename, so the walk below would emit "//" for
  # it. That spelling then fails the literal `= "/"` refusal in validate_force_path, which
  # is the one guard whose whole job is to notice this exact argument.
  if [ "$p" = "/" ]; then printf '/\n'; return; fi
  d="$(dirname "$p")"; tail="/$(basename "$p")"
  while [ "$d" != "/" ] && [ ! -d "$d" ]; do
    b="$(basename "$d")"; d="$(dirname "$d")"; tail="/$b$tail"
  done
  # If the ancestor cannot be entered, keep the lexical path: it will fail the ownership
  # test below and the link is kept. Failing towards "keep" is the only safe direction.
  if [ -d "$d" ]; then d="$(cd "$d" 2>/dev/null && pwd -P)" || d="$d"; fi
  case "$d" in
    /) printf '%s\n' "$tail" ;;
    *) printf '%s%s\n' "$d" "$tail" ;;
  esac
}

link_target() {  # the path a symlink names, made absolute (the target need not exist)
  local t; t="$(readlink "$1")"
  case "$t" in
    /*) printf '%s\n' "$t" ;;
    *)  printf '%s\n' "$(dirname "$1")/$t" ;;
  esac
}

under() {  # under <path> <dir> -- true when <path> is strictly INSIDE <dir>
  # The pattern's prefix is quoted so it matches literally; `?*` forces at least one
  # character, so <dir> itself never counts as being inside itself.
  case "$1" in
    "$2"/?*) return 0 ;;
  esac
  return 1
}

# ── --force-prune-path: accepting a deletion root from the command line ──────────
# FORCE_ROOT is the validated, resolved form of --force-prune-path, or "" when the flag
# was not given. Every guard below exists because the alternative is a script that deletes
# under $HOME from an argument, and each one blocks a specific way that goes wrong.
FORCE_ROOT=""
validate_force_path() {
  [ -n "$FORCE_PATH" ] || return 0
  # (a) Only meaningful while pruning. Silently ignoring it on a plain install would let a
  #     typo'd invocation look like it did the cleanup the user asked for.
  if [ "$PRUNE" != 1 ] && [ "$PRUNE_ONLY" != 1 ]; then
    echo "--force-prune-path needs --prune or --prune-only (it only ever removes links)" >&2
    exit 2
  fi
  # (b) Absolute only. A relative root would be resolved against whatever directory the
  #     user happened to be in, so the same command would delete different things.
  case "$FORCE_PATH" in
    /*) ;;
    *) echo "--force-prune-path must be an absolute path (got: $FORCE_PATH)" >&2; exit 2 ;;
  esac
  FORCE_ROOT="$(resolve_path "$FORCE_PATH")"
  # (c) A root that is not a directory cannot have been a checkout. A moved checkout leaves
  #     NOTHING there, which is fine and expected; a regular file there means a mistyped path.
  if [ -e "$FORCE_ROOT" ] && [ ! -d "$FORCE_ROOT" ]; then
    echo "--force-prune-path=$FORCE_ROOT exists and is not a directory" >&2; exit 2
  fi
  # (d) Named refusals for the two roots that would make the footprint test below vacuous
  #     by being ancestors of everything the user owns.
  if [ "$FORCE_ROOT" = "/" ] || [ "$FORCE_ROOT" = "$(resolve_path "$HOME")" ]; then
    echo "--force-prune-path refuses / and \$HOME: a root that broad makes the" >&2
    echo "footprint check meaningless. Name the actual old checkout directory." >&2
    exit 2
  fi
  # (e) Dry-run unless a SECOND, separate flag is given. One mistyped path should print a
  #     list, not perform a deletion; the confirmation is what turns the list into an act.
  if [ "$FORCE_CONFIRMED" != 1 ]; then
    DRY_RUN=1
    echo "  NOTE  --force-prune-path is dry-run by default. Review the list below, then"
    echo "        re-run with --confirm-force-prune to actually remove those links."
  fi
}

force_root_for() {  # force_root_for <dir being pruned> -- the root to accept for THIS dir, or ""
  # (f) The last guard, and the one that is easy to miss: if the supplied root CONTAINS the
  #     directory being pruned, then every link in that directory resolves "under" it --
  #     including links belonging to other checkouts -- and the footprint test collapses
  #     into "delete everything in our namespace". Refuse per-directory, because the
  #     directory differs per tool and per CLAUDE_HOME/CODEX_HOME/GEMINI_HOME override.
  local rdir
  [ -n "$FORCE_ROOT" ] || { printf '' ; return 0; }
  rdir="$(resolve_path "$1")"
  if [ "$rdir" = "$FORCE_ROOT" ] || under "$rdir" "$FORCE_ROOT"; then
    echo "  REFUSE --force-prune-path=$FORCE_ROOT contains $1 — a root that contains the" >&2
    echo "         install directory would match every link in it, not just ours." >&2
    printf ''
    return 0
  fi
  printf '%s\n' "$FORCE_ROOT"
}

name_matches() {  # name_matches <path> <skills|agents> -- does the basename look like ours?
  # skills: this repo owns the `mir-` namespace and nothing else.
  # agents: any *.md. Deliberately WIDER than "matches a filename in agents/" -- an agent
  # deleted in a later release is by definition no longer in agents/, and that dangling
  # link is the exact thing prune is for. The target test below is what makes it safe.
  case "$2" in
    skills) case "$(basename "$1")" in mir-*) return 0 ;; esac ;;
    agents) case "$(basename "$1")" in *.md) return 0 ;; esac ;;
  esac
  return 1
}

owned() {  # owned <path> <skills|agents> -- quiet form of the full test
  name_matches "$1" "$2" || return 1
  [ -L "$1" ] || return 1
  under "$(resolve_path "$(link_target "$1")")" "$REPO_REAL/$2" || return 1
  return 0
}

remove_link() {  # remove_link <link> <resolved target> [note] -- honours --dry-run
  local note="${3:-}"
  if [ "$DRY_RUN" = 1 ]; then
    echo "  DRY   would remove $1 -> $2$note"
  else
    rm -f -- "$1"   # $1 is known to be a symlink, so this unlinks it, never its target
    echo "  PRUNE $1$note"
  fi
  PRUNED_N=$((PRUNED_N + 1))
}

keep_link() {  # keep_link <link> <resolved target> -- the refusal, plus why it happened
  if [ -e "$2" ]; then
    echo "  KEEP  $1 -> $2 (not this checkout — remove it yourself if you want it gone)"
  else
    # The moved-checkout case. Nothing on disk can distinguish "this checkout, before it
    # moved" from "someone else's checkout, since deleted", so keeping is still correct --
    # but the user knows which it is, and --force-prune-path is how they say so.
    echo "  KEEP  $1 -> $2 (target does not exist — another checkout, or this one before it moved)"
    SAW_MOVED=1
  fi
  KEPT_N=$((KEPT_N + 1))
}

prune_dir() {  # prune_dir <dir> <skills|agents>
  local dir="$1" sub="$2" entry target force
  if [ "$PRUNE" != 1 ] && [ "$PRUNE_ONLY" != 1 ]; then return 0; fi
  [ -d "$dir" ] || return 0
  force="$(force_root_for "$dir")"
  for entry in "$dir"/*; do
    # -e is false for a dangling symlink, so -L has to be asked separately; this also
    # skips the un-expanded glob when the directory is empty.
    if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then continue; fi
    # Anything not in our namespace is somebody else's file. Say nothing about it.
    name_matches "$entry" "$sub" || continue
    if [ ! -L "$entry" ]; then
      # Unconditional, and deliberately ABOVE the force branch: --force-prune-path widens
      # which TARGETS count as ours, never what may be removed. A real file or directory
      # is never removable by any flag, because unlinking is recoverable and rm -r is not.
      echo "  KEEP  $entry (a real file or directory — prune only ever unlinks symlinks)"
      KEPT_N=$((KEPT_N + 1))
      continue
    fi
    target="$(resolve_path "$(link_target "$entry")")"
    if under "$target" "$REPO_REAL/$sub"; then
      remove_link "$entry" "$target"
    elif [ -n "$force" ] && under "$target" "$force/$sub"; then
      # The footprint test. Two conditions had to hold to get here: the basename is in our
      # namespace (name_matches, above) AND the target sits under <old>/skills or
      # <old>/agents -- this repo's own directory layout. A user who mistypes the root at
      # some unrelated directory matches nothing, because nothing links into <that>/skills.
      remove_link "$entry" "$target" "  (via --force-prune-path=$force)"
    else
      # A mir-* link into a DIFFERENT checkout is another setup's, and a link into a
      # path that no longer exists cannot be told apart from one. Refuse either way.
      keep_link "$entry" "$target"
    fi
  done
}

prune_file() {  # prune_file <link> <repo-relative source> -- one named link, e.g. AGENTS.md
  local dst="$1" src="$REPO_REAL/$2" target force
  if [ "$PRUNE" != 1 ] && [ "$PRUNE_ONLY" != 1 ]; then return 0; fi
  [ -L "$dst" ] || return 0          # absent, or a real file the user wrote: leave it
  target="$(resolve_path "$(link_target "$dst")")"
  if [ "$target" = "$src" ]; then
    remove_link "$dst" "$target"
    return 0
  fi
  # A moved checkout orphans this link exactly as it orphans the skill links, so the same
  # opt-in applies. Equality, not `under`: this is one named file, so widening to a prefix
  # test would accept <old>/AGENTS.md.backup and anything else sharing the prefix.
  force="$(force_root_for "$(dirname "$dst")")"
  if [ -n "$force" ] && [ "$target" = "$force/$2" ]; then
    remove_link "$dst" "$target" "  (via --force-prune-path=$force)"
    return 0
  fi
  keep_link "$dst" "$target"
}

warn_stale() {  # warn_stale <dir> <skills|agents> -- detect, on a normal install, what prune would find
  local dir="$1" sub="$2" entry n=0
  if [ "$PRUNE" = 1 ] || [ "$PRUNE_ONLY" = 1 ]; then return 0; fi   # nothing survived to be stale
  [ -d "$dir" ] || return 0
  for entry in "$dir"/*; do
    if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then continue; fi
    owned "$entry" "$sub" || continue
    # Ours, but this run did not write it: a rename, a deletion, or a scope reduction.
    # A here-string, not a pipe: under `pipefail` a pipeline into `grep -q` can report the
    # writer's SIGPIPE instead of grep's match, which would flag links we DID just write.
    if grep -Fxq -- "$entry" <<< "$WROTE"; then continue; fi
    n=$((n + 1))
  done
  if [ "$n" -gt 0 ]; then
    echo "  WARN  $n link(s) in $dir point into this checkout but were not installed by this run." >&2
    echo "        They are left over from an earlier install (a rename, a removed skill, or a" >&2
    echo "        wider --scope) and may resolve to nothing. See: ./install.sh --prune --dry-run" >&2
  fi
}

validate_tree() {  # refuse to install a tree that fails validation
  local script="$REPO_DIR/validate.py"
  if [ ! -f "$script" ]; then
    echo "  WARN  validate.py not found — installing unvalidated" >&2
    return
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "  WARN  python3 not found — skipping validation. Run ./validate.py when you can." >&2
    return
  fi
  local status=0
  python3 "$script" --quiet || status=$?
  if [ "$status" -ne 0 ]; then
    echo >&2
    echo "Refusing to install: validate.py exited $status (1 = errors, 2 = unreadable tree)." >&2
    echo "Run ./validate.py for the full report, fix the errors, then re-run ./install.sh." >&2
    exit 1
  fi
  echo "  OK    skill tree validated"
}

install_skills_to() {  # install_skills_to <skills_dir>
  local dir="$1" n=0
  for skill in "$REPO_DIR"/skills/*/; do
    local name; name="$(basename "${skill%/}")"
    # --scope=pillars installs only depth-1 slugs (mir-<pillar>); a tier or module has
    # two or more dashes. Counting dashes is the same rule the naming convention encodes.
    if [ "$SCOPE" = "pillars" ]; then
      local dashes="${name//[^-]/}"
      [ "${#dashes}" -eq 1 ] || continue
    fi
    link "${skill%/}" "$dir/$name"
    n=$((n + 1))
  done
  if [ "$SCOPE" = "pillars" ]; then
    echo "  NOTE  scope=pillars: linked $n pillar(s) globally."
    echo "        Run \`mir init --install\` in a repo to add just that repo's tiers/modules."
  fi
}

install_agents_to() {  # install_agents_to <agents_dir>
  local dir="$1"
  for agent in "$REPO_DIR"/agents/*.md; do
    link "$agent" "$dir/$(basename "$agent")"
  done
}

# ── Claude Code (and Cursor, which reads ~/.claude/{skills,agents}) ──────────────
install_claude() {
  local base="${CLAUDE_HOME:-$HOME/.claude}"
  echo "→ Claude Code  ($base)"
  # Prune BEFORE installing, and prune everything we own rather than diffing: the install
  # that follows re-links exactly what --scope asks for, so "old set minus new set" needs
  # no bookkeeping and cannot leave a stale link behind by arithmetic error.
  prune_dir "$base/skills" skills
  prune_dir "$base/agents" agents
  if [ "$PRUNE_ONLY" = 1 ]; then return 0; fi
  install_skills_to "$base/skills"
  install_agents_to "$base/agents"
  warn_stale "$base/skills" skills
  warn_stale "$base/agents" agents
}

# ── Cursor: loads ~/.claude/{skills,agents} globally; AGENTS.md works per-repo ───
install_cursor() {
  echo "→ Cursor  (uses Claude's ~/.claude resources)"
  install_claude
  echo "  NOTE  Cursor auto-discovers the above from ~/.claude. For repo-scoped rules,"
  echo "        copy AGENTS.md into a project root (Cursor reads AGENTS.md natively)."
}

# ── Codex CLI: $CODEX_HOME/skills is a real discovery root, plus AGENTS.md ───────
install_codex() {
  local base="${CODEX_HOME:-$HOME/.codex}"
  echo "→ Codex CLI  ($base)"
  # Skills go in $CODEX_HOME/skills. Verified against codex-cli 0.147.0 rather than assumed:
  # `codex debug prompt-input` renders the model-visible prompt, and a probe skill planted
  # there comes back in the "## Skills / ### Available skills" block with a `file:` locator
  # naming the path we wrote. The same probe confirmed Codex's loader FOLLOWS a symlinked
  # skill directory, which is what makes the link() idiom usable here at all. It is also
  # the path Codex's own bundled `skill-installer` skill documents: "Installs into
  # `$CODEX_HOME/skills/<skill-name>` (defaults to `~/.codex/skills`)". The earlier
  # instruction to "register these through /skills" was wrong: /skills only toggles skills
  # that have ALREADY been discovered from a root, so a skill that is nowhere on disk under
  # a root can never appear there to be selected.
  prune_dir "$base/skills" skills
  prune_file "$base/AGENTS.md" AGENTS.md
  if [ "$PRUNE_ONLY" = 1 ]; then return 0; fi
  install_skills_to "$base/skills"
  link "$REPO_DIR/AGENTS.md" "$base/AGENTS.md"
  warn_stale "$base/skills" skills
  # No agents/ install, and that is a finding rather than an omission. The same probe
  # planted agent .md files in $CODEX_HOME/agents, $HOME/.agents/agents, <repo>/.agents/agents
  # and <repo>/.codex/agents; none appeared anywhere in the rendered prompt. Codex's
  # per-skill `agents/openai.yaml` (see its bundled skills) carries display metadata --
  # display_name, short_description, icons -- not a reviewer sub-agent definition. So there
  # is no Codex equivalent of ~/.claude/agents to link into, and inventing one would repeat
  # the Antigravity defect: links in a directory nothing reads.
  echo "  NOTE  Skills load on-demand by description from $base/skills; AGENTS.md is always on."
  echo "        Codex has no directory for standalone reviewer agents — the reviewers run"
  echo "        inline per the AGENTS.md pipeline (their checklists ship inside the skills)."
}

# ── Antigravity: SKILL.md skills + cross-tool AGENTS.md, both global ──────────────
install_antigravity() {
  local base="${GEMINI_HOME:-$HOME/.gemini}"
  echo "→ Antigravity  ($base)"
  # Global user skills live in config/skills, NOT antigravity/skills. Antigravity
  # generates a read_file allowlist into its own system prompt, and that table names
  # ~/.gemini/config/skills (allowed for both the CLI and the IDE) while
  # ~/.gemini/antigravity/skills appears nowhere in it. `antigravity/` is the legacy
  # Antigravity 2.0 product dir; ~/.gemini/config/.migrated records the move. Skills
  # linked into the old path are silently never loaded -- the failure this repo exists
  # to prevent, one layer down at the install boundary.
  prune_dir "$base/config/skills" skills
  # The legacy Antigravity 2.0 path. Prune it, never install into it. Anything of ours
  # still sitting there was written by a pre-493b04b install and is invisible to
  # Antigravity, so it is pure dead weight; the ownership test is the same one used on the
  # live path, so cleaning it is exactly as safe. Not installing there is what keeps this
  # a cleanup rather than a resurrection of the broken target.
  prune_dir "$base/antigravity/skills" skills
  prune_file "$base/AGENTS.md" AGENTS.md
  if [ "$PRUNE_ONLY" = 1 ]; then return 0; fi
  install_skills_to "$base/config/skills"
  link "$REPO_DIR/AGENTS.md" "$base/AGENTS.md"
  warn_stale "$base/config/skills" skills
  warn_stale "$base/antigravity/skills" skills
  echo "  NOTE  Skills load on-demand by description. Reviewer agents run inline per the"
  echo "        AGENTS.md pipeline (their checklists ship inside the mir-backend skill)."
}

validate_force_path   # after resolve_path/under exist, before anything is removed

if [ "$PRUNE_ONLY" = 1 ]; then
  # Uninstalling must work on a tree that no longer validates -- otherwise the one command
  # that removes a broken install is gated on the install not being broken.
  echo "→ Pruning only  ($REPO_DIR)"
else
  echo "→ Validating  ($REPO_DIR)"
  validate_tree
fi
echo

case "$TOOL" in
  claude)       install_claude ;;
  cursor)       install_cursor ;;
  codex)        install_codex ;;
  antigravity)  install_antigravity ;;
  all)          install_claude; echo; install_codex; echo; install_antigravity ;;
  *) echo "unknown --tool '$TOOL' (use: claude|cursor|codex|antigravity|all)" >&2; exit 2 ;;
esac

echo
if [ "$PRUNE" = 1 ] || [ "$PRUNE_ONLY" = 1 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    echo "Dry run: $PRUNED_N link(s) would be removed, $KEPT_N kept. Nothing on disk changed."
  else
    echo "Pruned $PRUNED_N link(s); kept $KEPT_N that this checkout could not prove it owns."
  fi
  # Said once, not once per link: a moved checkout orphans every link at the same time, so
  # the per-link KEEP lines would otherwise repeat the same advice 46 times.
  if [ "$SAW_MOVED" = 1 ] && [ -z "$FORCE_ROOT" ]; then
    echo
    echo "Some kept links point at a path that no longer exists. If that path was THIS"
    echo "checkout before you moved it, adopt those links with:"
    echo "  ./install.sh --prune-only --force-prune-path=<that old directory>"
    echo "That prints a list and changes nothing; add --confirm-force-prune to remove them."
  fi
fi

if [ "$PRUNE_ONLY" = 1 ]; then
  echo "Done. Restart your agent so it stops offering the removed skills."
  exit 0
fi

echo "Done. Restart your agent to pick up the changes."
echo "Try:  /mir-backend <your backend task>   (or just describe a backend task — skills auto-trigger)"
