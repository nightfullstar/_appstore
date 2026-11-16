#!/usr/bin/env bash
# Write recasaos.org/store/ first, then GitHub. A GitHub failure is logged;
# org files are not deleted.
set -euo pipefail

usage() {
  echo "usage: publish.sh --artifacts DIR --org DIR [--github CMD] [--readme PATH] [--org-push CMD]" >&2
  exit 2
}

artifacts=""
org=""
github=""
readme=""
org_push=""
have_github=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --artifacts) artifacts="${2:-}"; shift 2 ;;
    --org) org="${2:-}"; shift 2 ;;
    --github) github="${2:-}"; have_github=1; shift 2 ;;
    --readme) readme="${2:-}"; shift 2 ;;
    --org-push) org_push="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

[[ -n "$artifacts" && -n "$org" ]] || usage

script_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd)"
artifacts="$(cd "$artifacts" && pwd)"
mkdir -p "$org/store"
org="$(cd "$org" && pwd)"
store="$org/store"
export ARTIFACTS="$artifacts" ORG="$org"

write_org() {
  local f
  for f in recasaos.zip recasaos.zip.sha256 store-index.json; do
    if [[ -f "$artifacts/$f" ]]; then
      cp -f "$artifacts/$f" "$store/$f"
    else
      rm -f "$store/$f"
    fi
  done
  if [[ -f "$store/recasaos.zip" && -f "$store/store-index.json" ]]; then
    printf 'ok\n' > "$store/health"
  else
    rm -f "$store/health"
  fi
}

push_org() {
  if [[ -n "$org_push" ]]; then
    bash -c "$org_push"
    return
  fi
  if [[ ! -d "$org/.git" ]]; then
    return 0
  fi
  git -C "$org" add store
  if git -C "$org" diff --cached --quiet; then
    echo "recasaos.org already has these store files"
    return 0
  fi
  git -C "$org" \
    -c user.name="${GIT_AUTHOR_NAME:-catalogue}" \
    -c user.email="${GIT_AUTHOR_EMAIL:-catalogue@recasaos.invalid}" \
    commit -m "Catalogue of $(date -u +%Y-%m-%d)"
  git -C "$org" push origin HEAD
}

github_failed() {
  echo "GitHub publish failed; recasaos.org files were left in place" >&2
  exit 1
}

publish_github_release_and_branch() {
  local zip="$artifacts/recasaos.zip"
  local sha="$artifacts/recasaos.zip.sha256"
  local index="$artifacts/store-index.json"
  local assets=()
  [[ -f "$zip" ]] && assets+=("$zip")
  [[ -f "$sha" ]] && assets+=("$sha")
  [[ -f "$index" ]] && assets+=("$index")
  if [[ ${#assets[@]} -eq 0 ]]; then
    echo "no GitHub assets under $artifacts" >&2
    return 1
  fi
  if gh release view catalogue >/dev/null 2>&1; then
    gh release upload catalogue "${assets[@]}" --clobber
  else
    gh release create catalogue "${assets[@]}" --latest \
      --title "ReCasaOS catalogue" \
      --notes "Rolling catalogue. Same bytes as https://recasaos.org/store/recasaos.zip"
  fi

  (
    cd "$repo_root"
    export GIT_INDEX_FILE
    GIT_INDEX_FILE="$(mktemp -u)"
    git update-index --add --cacheinfo "100644,$(git hash-object -w "$zip"),store/recasaos.zip"
    if [[ -f "$index" ]]; then
      git update-index --add --cacheinfo "100644,$(git hash-object -w "$index"),store/store-index.json"
    fi
    if [[ -f "$sha" ]]; then
      git update-index --add --cacheinfo "100644,$(git hash-object -w "$sha"),store/recasaos.zip.sha256"
    fi
    if [[ -n "$readme" && -f "$readme" ]]; then
      git update-index --add --cacheinfo "100644,$(git hash-object -w "$readme"),README.md"
    fi
    local apps="?"
    apps="$(unzip -p "$zip" catalogue.json | python -c 'import json,sys; print(len(json.load(sys.stdin)["apps"]))')"
    local commit
    commit="$(git commit-tree "$(git write-tree)" -m "Catalogue of $(date -u +%Y-%m-%d), ${apps} apps")"
    git push --force --quiet origin "${commit}:refs/heads/catalogue"
    echo "catalogue is now the build of $(date -u +%Y-%m-%d), ${apps} apps"
  )
}

write_org
push_org

if [[ "$have_github" -eq 1 ]]; then
  if ! bash -c "$github"; then
    github_failed
  fi
elif ! publish_github_release_and_branch; then
  github_failed
fi
