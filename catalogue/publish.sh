#!/usr/bin/env bash
# Write recasaos.org/store/ first, then GitHub. A GitHub failure is logged;
# org files are not deleted.
set -euo pipefail

RELEASE_TAG="catalogue-latest"
ORG_ZIP_URL="https://recasaos.org/store/recasaos.zip"
GITHUB_ZIP_URL="https://github.com/ReCasaOS/_appstore/releases/latest/download/recasaos.zip"
CATALOGUE_BRANCH_ZIP_URL="https://raw.githubusercontent.com/ReCasaOS/_appstore/catalogue/store/recasaos.zip"

usage() {
  echo "usage: publish.sh --artifacts DIR --org DIR [--github CMD] [--readme PATH] [--org-push CMD]" >&2
  echo "       publish.sh --fetch-previous PATH" >&2
  echo "       publish.sh --skip-if-published --artifacts DIR" >&2
  exit 2
}

artifacts=""
org=""
github=""
readme=""
org_push=""
have_github=0
fetch_previous=""
skip_if_published=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --artifacts) artifacts="${2:-}"; shift 2 ;;
    --org) org="${2:-}"; shift 2 ;;
    --github) github="${2:-}"; have_github=1; shift 2 ;;
    --readme) readme="${2:-}"; shift 2 ;;
    --org-push) org_push="${2:-}"; shift 2 ;;
    --fetch-previous) fetch_previous="${2:-}"; shift 2 ;;
    --skip-if-published) skip_if_published=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

fetch_previous_zip() {
  local dest="$1" url
  [[ -n "$dest" ]] || return 2
  rm -f "$dest"
  for url in "$ORG_ZIP_URL" "$GITHUB_ZIP_URL" "$CATALOGUE_BRANCH_ZIP_URL"; do
    if curl -fsSL -o "$dest" "$url"; then
      echo "previous catalogue from $url"
      return 0
    fi
    rm -f "$dest"
  done
  echo "no published catalogue yet"
  return 0
}

already_on_both_hosts() {
  local zip="$1" org_tmp gh_tmp
  [[ -f "$zip" ]] || return 1
  org_tmp="$(mktemp)"
  gh_tmp="$(mktemp)"
  if ! curl -fsSL -o "$org_tmp" "$ORG_ZIP_URL"; then
    rm -f "$org_tmp" "$gh_tmp"
    return 1
  fi
  if ! curl -fsSL -o "$gh_tmp" "$GITHUB_ZIP_URL"; then
    rm -f "$org_tmp" "$gh_tmp"
    return 1
  fi
  if cmp -s "$org_tmp" "$zip" && cmp -s "$gh_tmp" "$zip"; then
    rm -f "$org_tmp" "$gh_tmp"
    return 0
  fi
  rm -f "$org_tmp" "$gh_tmp"
  return 1
}

if [[ -n "$fetch_previous" ]]; then
  fetch_previous_zip "$fetch_previous"
  exit 0
fi

if [[ "$skip_if_published" -eq 1 ]]; then
  [[ -n "$artifacts" ]] || usage
  artifacts="$(cd "$artifacts" && pwd)"
  if already_on_both_hosts "$artifacts/recasaos.zip"; then
    echo "org and GitHub already hold this zip"
    exit 0
  fi
  exit 1
fi

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

stage_blob() {
  local src="$1" dest="$2" blob
  blob="$(git hash-object -w "$src")" || return 1
  git update-index --add --cacheinfo "100644,${blob},${dest}" || return 1
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
  if gh release view "$RELEASE_TAG" >/dev/null 2>&1; then
    gh release upload "$RELEASE_TAG" "${assets[@]}" --clobber || return 1
  else
    gh release create "$RELEASE_TAG" "${assets[@]}" --latest \
      --title "ReCasaOS catalogue" \
      --notes "Rolling catalogue. Same bytes as https://recasaos.org/store/recasaos.zip" \
      || return 1
  fi

  (
    cd "$repo_root" || exit 1
    export GIT_INDEX_FILE
    GIT_INDEX_FILE="$(mktemp -u)"
    stage_blob "$zip" "store/recasaos.zip" || exit 1
    if [[ -f "$index" ]]; then
      stage_blob "$index" "store/store-index.json" || exit 1
    fi
    if [[ -f "$sha" ]]; then
      stage_blob "$sha" "store/recasaos.zip.sha256" || exit 1
    fi
    if [[ -n "$readme" && -f "$readme" ]]; then
      stage_blob "$readme" "README.md" || exit 1
    fi
    local apps tree commit
    apps="$(unzip -p "$zip" catalogue.json | python -c 'import json,sys; print(len(json.load(sys.stdin)["apps"]))')" || exit 1
    tree="$(git write-tree)" || exit 1
    commit="$(git commit-tree "$tree" -m "Catalogue of $(date -u +%Y-%m-%d), ${apps} apps")" || exit 1
    git push --force --quiet origin "${commit}:refs/heads/catalogue" || exit 1
    echo "catalogue is now the build of $(date -u +%Y-%m-%d), ${apps} apps"
  ) || return 1
}

write_org
push_org

if [[ "$have_github" -eq 1 ]]; then
  bash -c "$github" || github_failed
else
  publish_github_release_and_branch || github_failed
fi
