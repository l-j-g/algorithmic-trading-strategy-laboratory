#!/usr/bin/env bash
set -euo pipefail

# One control surface for the three local repositories:
#
#   jesse-upstream  public Jesse engine, fast-forward only
#   jesse-src       private research/config/runtime workspace
#   ATS Lab         harness, queue, evidence, and operator tooling
#
# Paths can be overridden for another machine. No credentials are read here.

usage() {
  cat <<'EOF'
Usage:
  scripts/jesse-workspace.sh status
  scripts/jesse-workspace.sh upstream init|update|refresh
  scripts/jesse-workspace.sh image build [--no-update]
  scripts/jesse-workspace.sh stack up|down
  scripts/jesse-workspace.sh worktree create <slug> [base-ref]
  scripts/jesse-workspace.sh worktree list
  scripts/jesse-workspace.sh worktree remove <slug>

Defaults:
  ATS repository:       this checkout
  Jesse research repo:  sibling jesse-src
  Jesse upstream repo:  sibling jesse-upstream
  Image repository:     ats-lab/jesse

The upstream checkout is updated by fast-forward only. Runtime state,
credentials, private strategies, and generated backtest artifacts stay out of
commits and out of the upstream image build context.
EOF
}

script_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace_root="${ATS_WORKSPACE_ROOT:-$(dirname "$script_root")}"
upstream_repo="${JESSE_UPSTREAM_REPOSITORY:-$workspace_root/jesse-upstream}"
research_repo="${JESSE_RESEARCH_REPOSITORY:-$workspace_root/jesse-src}"
image_repository="${JESSE_IMAGE_REPOSITORY:-ats-lab/jesse}"
compose_file="${JESSE_COMPOSE_FILE:-$research_repo/docker/docker-compose.yml}"
compose_override="$script_root/ops/jesse/docker-compose.upstream.yml"

die() {
  echo "error: $*" >&2
  exit 1
}

require_git_repo() {
  local path="$1"
  [[ -d "$path/.git" || -f "$path/.git" ]] || die "not a Git repository: $path"
}

repo_dirty() {
  [[ -n "$(git -C "$1" status --porcelain)" ]]
}

repo_summary() {
  local name="$1" path="$2"
  if [[ ! -e "$path" ]]; then
    printf '%-10s missing  %s\n' "$name" "$path"
    return
  fi
  require_git_repo "$path"
  local branch commit state
  branch="$(git -C "$path" branch --show-current)"
  commit="$(git -C "$path" rev-parse --short=12 HEAD)"
  state="clean"
  repo_dirty "$path" && state="dirty"
  printf '%-10s %-7s %-12s %-16s %s\n' "$name" "$state" "$branch" "$commit" "$path"
}

ensure_upstream_repo() {
  if [[ ! -e "$upstream_repo" ]]; then
    echo "cloning upstream Jesse: $upstream_repo"
    GIT_TERMINAL_PROMPT=0 git clone --depth 1 https://github.com/jesse-ai/jesse.git "$upstream_repo"
  fi
  require_git_repo "$upstream_repo"
  local remote
  remote="$(git -C "$upstream_repo" remote get-url origin 2>/dev/null || true)"
  [[ "$remote" == "https://github.com/jesse-ai/jesse.git" ]] || \
    die "upstream origin must be https://github.com/jesse-ai/jesse.git: $remote"
}

upstream_branch() {
  local branch
  branch="$(git -C "$upstream_repo" branch --show-current)"
  [[ -n "$branch" ]] || die "upstream checkout is detached: $upstream_repo"
  printf '%s\n' "$branch"
}

upstream_init() {
  ensure_upstream_repo
  echo "upstream=$upstream_repo"
  echo "remote=$(git -C "$upstream_repo" remote get-url origin)"
  echo "branch=$(upstream_branch)"
  echo "commit=$(git -C "$upstream_repo" rev-parse HEAD)"
}

upstream_update() {
  ensure_upstream_repo
  repo_dirty "$upstream_repo" && die "upstream checkout dirty; preserve or resolve it before update"
  local branch before after
  branch="$(upstream_branch)"
  before="$(git -C "$upstream_repo" rev-parse HEAD)"
  GIT_TERMINAL_PROMPT=0 git -C "$upstream_repo" fetch --prune origin "$branch"
  git -C "$upstream_repo" merge --ff-only "origin/$branch"
  after="$(git -C "$upstream_repo" rev-parse HEAD)"
  if [[ "$before" == "$after" ]]; then
    echo "upstream unchanged: $after"
  else
    echo "upstream advanced: $before -> $after"
  fi
}

image_tag() {
  printf '%s:%s\n' "$image_repository" "$(git -C "$upstream_repo" rev-parse --short=12 HEAD)"
}

image_build() {
  ensure_upstream_repo
  if [[ "${1:-}" != "--no-update" ]]; then
    upstream_update
  else
    repo_dirty "$upstream_repo" && die "upstream checkout dirty; cannot build reproducible image"
  fi
  local tag commit
  tag="$(image_tag)"
  commit="$(git -C "$upstream_repo" rev-parse HEAD)"
  docker build --pull \
    --label "org.opencontainers.image.source=https://github.com/jesse-ai/jesse" \
    --label "org.opencontainers.image.revision=$commit" \
    --label "org.opencontainers.image.description=Clean public Jesse engine for ATS research" \
    --tag "$tag" "$upstream_repo"
  docker tag "$tag" "$image_repository:upstream"
  echo "image=$tag"
  echo "image_alias=$image_repository:upstream"
}

upstream_refresh() {
  upstream_update
  local tag
  tag="$(image_tag)"
  if docker image inspect "$tag" >/dev/null 2>&1; then
    echo "image already present: $tag"
    return
  fi
  image_build --no-update
}

stack_compose() {
  [[ -f "$compose_file" ]] || die "Jesse compose file missing: $compose_file"
  [[ -f "$compose_override" ]] || die "compose override missing: $compose_override"
  docker compose -f "$compose_file" -f "$compose_override" "$@"
}

stack_up() {
  ensure_upstream_repo
  local tag
  tag="$(image_tag)"
  docker image inspect "$tag" >/dev/null 2>&1 || image_build --no-update >/dev/null
  JESSE_IMAGE="$tag" stack_compose up -d
  echo "Jesse stack running from $tag"
}

stack_down() {
  ensure_upstream_repo
  local tag
  tag="$(image_tag)"
  JESSE_IMAGE="$tag" stack_compose down
}

worktree_root() {
  printf '%s/%s-worktrees/%s\n' "$workspace_root" "$1" "$2"
}

worktree_create() {
  local slug="${1:-}" base_ref="${2:-HEAD}"
  [[ "$slug" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "slug must match [a-z0-9][a-z0-9-]*"
  require_git_repo "$script_root"
  require_git_repo "$research_repo"
  local ats_path jesse_path branch
  ats_path="$(worktree_root algorithmic-trading-strategy-laboratory "$slug")"
  jesse_path="$(worktree_root jesse-src "$slug")"
  branch="task/$slug"
  [[ ! -e "$ats_path" && ! -e "$jesse_path" ]] || die "worktree path already exists"
  git -C "$script_root" show-ref --verify --quiet "refs/heads/$branch" && die "ATS branch exists: $branch"
  git -C "$research_repo" show-ref --verify --quiet "refs/heads/$branch" && die "Jesse branch exists: $branch"
  mkdir -p "$(dirname "$ats_path")" "$(dirname "$jesse_path")"
  git -C "$script_root" worktree add -b "$branch" "$ats_path" "$base_ref"
  if ! git -C "$research_repo" worktree add -b "$branch" "$jesse_path" HEAD; then
    git -C "$script_root" worktree remove "$ats_path"
    git -C "$script_root" branch -D "$branch"
    die "Jesse worktree creation failed; ATS worktree rolled back"
  fi
  printf 'branch=%s\nats_worktree=%s\njesse_worktree=%s\n' "$branch" "$ats_path" "$jesse_path"
}

worktree_remove() {
  local slug="${1:-}"
  [[ "$slug" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "slug must match [a-z0-9][a-z0-9-]*"
  local ats_path jesse_path
  ats_path="$(worktree_root algorithmic-trading-strategy-laboratory "$slug")"
  jesse_path="$(worktree_root jesse-src "$slug")"
  for path in "$ats_path" "$jesse_path"; do
    [[ -e "$path" ]] || continue
    repo_dirty "$path" && die "worktree dirty; commit or preserve changes before removal: $path"
  done
  [[ ! -e "$ats_path" ]] || git -C "$script_root" worktree remove "$ats_path"
  [[ ! -e "$jesse_path" ]] || git -C "$research_repo" worktree remove "$jesse_path"
  git -C "$script_root" worktree prune
  git -C "$research_repo" worktree prune
  echo "removed task/$slug worktrees"
}

status() {
  printf 'role       state   branch       commit           path\n'
  repo_summary ats "$script_root"
  repo_summary jesse-src "$research_repo"
  repo_summary upstream "$upstream_repo"
  echo "compose=$compose_file"
  echo "image_repository=$image_repository"
  if [[ -d "$upstream_repo/.git" || -f "$upstream_repo/.git" ]]; then
    echo "expected_image=$(image_tag)"
  fi
  echo "ATS worktrees:"
  git -C "$script_root" worktree list --porcelain | awk '/^worktree / {print "  " $2}'
  echo "Jesse worktrees:"
  if [[ -d "$research_repo/.git" || -f "$research_repo/.git" ]]; then
    git -C "$research_repo" worktree list --porcelain | awk '/^worktree / {print "  " $2}'
  fi
}

command_name="${1:-}"
case "$command_name" in
  status)
    status
    ;;
  upstream)
    case "${2:-}" in
      init) upstream_init ;;
      update) upstream_update ;;
      refresh) upstream_refresh ;;
      *) usage >&2; exit 2 ;;
    esac
    ;;
  image)
    [[ "${2:-}" == build ]] || { usage >&2; exit 2; }
    image_build "${3:-}"
    ;;
  stack)
    case "${2:-}" in
      up) stack_up ;;
      down) stack_down ;;
      *) usage >&2; exit 2 ;;
    esac
    ;;
  worktree)
    case "${2:-}" in
      create) worktree_create "${3:-}" "${4:-HEAD}" ;;
      list) git -C "$script_root" worktree list; git -C "$research_repo" worktree list ;;
      remove) worktree_remove "${3:-}" ;;
      *) usage >&2; exit 2 ;;
    esac
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
