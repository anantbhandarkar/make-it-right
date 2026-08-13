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
#         CLAUDE_HOME / CODEX_HOME / GEMINI_HOME override the target dirs.
#
# --scope controls how much of the tree goes into the GLOBAL config:
#   all      (default)  every skill. Simple, but every repo pays for every skill's
#                       description at session start (~17k tokens for 46 skills).
#   pillars             only the depth-1 skills (mir-backend, mir-frontend, mir-mobile,
#                       mir-database, mir-cloud, mir-devsecops, mir-init) -- about 3k
#                       tokens. Every repo still gets the gates; the runtime tiers and
#                       framework modules are installed per project by `mir init --install`,
#                       which resolves exactly the ones that repo needs.
# Claude Code merges ~/.claude/skills and <repo>/.claude/skills, so the two combine.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL="claude"
SCOPE="all"
for arg in "$@"; do
  case "$arg" in
    --tool=*)  TOOL="${arg#*=}" ;;
    --tool)    shift; TOOL="${1:-claude}" ;;
    --scope=*) SCOPE="${arg#*=}" ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done
case "$SCOPE" in
  all|pillars) ;;
  *) echo "unknown --scope: $SCOPE (use all or pillars)" >&2; exit 2 ;;
esac

link() {  # link <source> <target>  (skips real files, refuses to clobber non-symlinks)
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  if [ -e "$dst" ] && [ ! -L "$dst" ]; then
    echo "  SKIP  $dst exists and is not a symlink — remove it first" >&2
    return
  fi
  ln -sfn "$src" "$dst"
  echo "  LINK  $dst -> $src"
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
  install_skills_to "$base/skills"
  install_agents_to "$base/agents"
}

# ── Cursor: loads ~/.claude/{skills,agents} globally; AGENTS.md works per-repo ───
install_cursor() {
  echo "→ Cursor  (uses Claude's ~/.claude resources)"
  install_claude
  echo "  NOTE  Cursor auto-discovers the above from ~/.claude. For repo-scoped rules,"
  echo "        copy AGENTS.md into a project root (Cursor reads AGENTS.md natively)."
}

# ── Codex CLI: AGENTS.md is the always-on, documented surface ────────────────────
install_codex() {
  local base="${CODEX_HOME:-$HOME/.codex}"
  echo "→ Codex CLI  ($base)"
  link "$REPO_DIR/AGENTS.md" "$base/AGENTS.md"
  echo "  NOTE  AGENTS.md loads automatically. To wire the skills/sub-agents natively,"
  echo "        register them via Codex '/skills' and custom-agent config (see Codex docs)."
}

# ── Antigravity: SKILL.md skills + cross-tool AGENTS.md, both global ──────────────
install_antigravity() {
  local base="${GEMINI_HOME:-$HOME/.gemini}"
  echo "→ Antigravity  ($base)"
  install_skills_to "$base/antigravity/skills"
  link "$REPO_DIR/AGENTS.md" "$base/AGENTS.md"
  echo "  NOTE  Skills load on-demand by description. Reviewer agents run inline per the"
  echo "        AGENTS.md pipeline (their checklists ship inside the mir-backend skill)."
}

echo "→ Validating  ($REPO_DIR)"
validate_tree
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
echo "Done. Restart your agent to pick up the changes."
echo "Try:  /mir-backend <your backend task>   (or just describe a backend task — skills auto-trigger)"
