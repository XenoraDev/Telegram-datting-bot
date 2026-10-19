#!/usr/bin/env bash
# Create ONE annotated release tag on GitHub, for the "Create release tag" workflow
# (.github/workflows/create-release-tag.yml). Runs in CI, never on the server.
#
#   scripts/create_release_tag.sh --tag vX.Y.Z[-rcN] --expected-sha <40 hex> [--validate-only]
#
# Environment: GITHUB_REPOSITORY (owner/name), GH_TOKEN (for `gh api`), RELEASE_BRANCH (default
# release/v2.0.0), RELEASE_REMOTE (default origin). Run from a checkout that has the full history.
#
# Everything is validated BEFORE the first write; any failure stops with nothing written:
#   * the tag is a release tag (same pattern as release.sh) and the SHA is 40 lowercase hex;
#   * the commit exists and is reachable from <remote>/<RELEASE_BRANCH> (no other commit may be tagged);
#   * the commit carries the files release.sh requires of a tag;
#   * the tag does not exist, neither in this checkout nor on the remote.
# Then exactly two creations, in this order, both plain POSTs (GitHub refuses to create a ref that
# exists, so nothing can be overwritten):
#   1. POST git/tags  -> an annotated tag OBJECT that points at the commit
#   2. POST git/refs  -> refs/tags/<tag> pointing at that tag object
# and a read-back: the remote ref exists, is a tag object, and peels to the expected commit.
#
# Never here: deleting, moving, force-updating or replacing a tag or ref (no PATCH / PUT / DELETE,
# no git push, no update-ref). If the read-back fails the script reports it and stops; a person
# decides what to do.
set -euo pipefail

TAG_RE='^v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-rc[0-9]{1,3})?$'
SHA_RE='^[0-9a-f]{40}$'
RELEASE_BRANCH="${RELEASE_BRANCH:-release/v2.0.0}"
RELEASE_REMOTE="${RELEASE_REMOTE:-origin}"
# What release.sh requires of a tagged tree (git_check); a tag lacking them could never be deployed.
REQUIRED_PATHS=(Dockerfile docker-compose.yml scripts/backup.sh release/migration_compat.json)

say() { printf '%s\n' "$*"; }
die() { printf 'create-release-tag: %s\nNOTHING WAS WRITTEN.\n' "$*" >&2; exit 1; }
die_after_write() { printf 'create-release-tag: %s\n' "$*" >&2; exit 1; }

TAG=""; SHA=""; VALIDATE_ONLY=0
while (($#)); do
    case "$1" in
        --tag) (($# >= 2)) || die "--tag needs a value"; TAG="$2"; shift 2 ;;
        --expected-sha) (($# >= 2)) || die "--expected-sha needs a value"; SHA="$2"; shift 2 ;;
        --validate-only) VALIDATE_ONLY=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

# ---------------------------------------------------------------- validation (read-only)

validate() {
    [[ "$TAG" =~ $TAG_RE ]] || die "not a release tag (expected vX.Y.Z or vX.Y.Z-rcN): '$TAG'"
    [[ "$SHA" =~ $SHA_RE ]] || die "expected_sha must be the full 40-character lowercase commit id: '$SHA'"
    [[ "$RELEASE_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || die "invalid RELEASE_BRANCH: '$RELEASE_BRANCH'"
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not inside a git repository"

    git fetch --quiet --no-tags "$RELEASE_REMOTE" "$RELEASE_BRANCH" \
        || die "cannot fetch $RELEASE_REMOTE/$RELEASE_BRANCH"
    local tip
    tip="$(git rev-parse -q --verify "refs/remotes/$RELEASE_REMOTE/$RELEASE_BRANCH^{commit}")" \
        || die "$RELEASE_REMOTE/$RELEASE_BRANCH does not exist"

    [[ "$(git cat-file -t "$SHA" 2>/dev/null || true)" == commit ]] \
        || die "$SHA is not a commit in this repository"
    git merge-base --is-ancestor "$SHA" "$tip" \
        || die "$SHA is not part of $RELEASE_REMOTE/$RELEASE_BRANCH ($tip): refusing to tag a commit outside the release branch"

    local path
    for path in "${REQUIRED_PATHS[@]}"; do
        git cat-file -e "$SHA:$path" 2>/dev/null \
            || die "commit $SHA does not contain $path, so release.sh would refuse the tag"
    done

    if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null 2>&1; then
        die "tag $TAG already exists in this checkout: tags are never overwritten, moved or deleted"
    fi
    local remote_refs
    remote_refs="$(git ls-remote --tags "$RELEASE_REMOTE" "refs/tags/$TAG" 2>/dev/null)" \
        || die "cannot read tags from $RELEASE_REMOTE"
    [[ -z "$remote_refs" ]] \
        || die "tag $TAG already exists on $RELEASE_REMOTE: tags are never overwritten, moved or deleted"
}

# ---------------------------------------------------------------- creation (two POSTs, nothing else)

create() {
    : "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
    [[ "$GITHUB_REPOSITORY" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "invalid GITHUB_REPOSITORY"
    local tag_object
    tag_object="$(gh api --method POST "repos/$GITHUB_REPOSITORY/git/tags" \
        -f tag="$TAG" -f message="Release $TAG ($SHA)" -f object="$SHA" -f type=commit \
        -f 'tagger[name]=github-actions[bot]' \
        -f 'tagger[email]=41898282+github-actions[bot]@users.noreply.github.com' \
        --jq .sha)" || die "GitHub refused to create the tag object (nothing refers to it)"
    [[ "$tag_object" =~ $SHA_RE ]] || die "unexpected answer when creating the tag object"
    say "annotated tag object created: $tag_object (not yet referenced)"

    gh api --method POST "repos/$GITHUB_REPOSITORY/git/refs" \
        -f ref="refs/tags/$TAG" -f sha="$tag_object" --jq .ref >/dev/null \
        || die_after_write "GitHub refused to create refs/tags/$TAG (it may exist now). Nothing was overwritten; the tag object $tag_object is unreferenced and harmless. Check the tag by hand."
    say "refs/tags/$TAG created -> $tag_object"
    VERIFY_OBJECT="$tag_object"
}

# ---------------------------------------------------------------- read-back

verify() {
    local out ref_sha peeled kind commit probe="refs/release-verify/$TAG"
    out="$(git ls-remote "$RELEASE_REMOTE" "refs/tags/$TAG" "refs/tags/$TAG^{}" 2>/dev/null)" \
        || die_after_write "cannot read $RELEASE_REMOTE to verify the tag"
    ref_sha="$(awk -v r="refs/tags/$TAG" '$2 == r { print $1 }' <<<"$out")"
    peeled="$(awk -v r="refs/tags/$TAG^{}" '$2 == r { print $1 }' <<<"$out")"
    [[ -n "$ref_sha" ]] || die_after_write "VERIFY FAILED: refs/tags/$TAG is not on $RELEASE_REMOTE"
    [[ -n "$peeled" ]] || die_after_write "VERIFY FAILED: $TAG is a lightweight tag (no peeled entry). Do not re-tag; investigate."
    [[ "$ref_sha" == "$VERIFY_OBJECT" ]] \
        || die_after_write "VERIFY FAILED: refs/tags/$TAG is $ref_sha, expected the tag object $VERIFY_OBJECT"
    [[ "$peeled" == "$SHA" ]] \
        || die_after_write "VERIFY FAILED: $TAG peels to $peeled, expected $SHA. Do not re-tag; investigate."
    # fetch the objects into a private namespace (not refs/tags) and look at them
    git fetch --quiet --no-tags "$RELEASE_REMOTE" "refs/tags/$TAG:$probe" \
        || die_after_write "VERIFY FAILED: cannot fetch the new tag"
    kind="$(git cat-file -t "$probe")"
    [[ "$kind" == tag ]] || die_after_write "VERIFY FAILED: the tag object type is '$kind', expected 'tag'"
    commit="$(git rev-parse "$probe^{commit}")"
    [[ "$commit" == "$SHA" ]] || die_after_write "VERIFY FAILED: the peeled commit is $commit, expected $SHA"
    say "verified: $TAG is an annotated tag object ($ref_sha) whose peeled commit is $commit"
    say "           the ref exists on $RELEASE_REMOTE"
}

validate
say "validated: $TAG -> $SHA is on $RELEASE_REMOTE/$RELEASE_BRANCH and $TAG does not exist yet"
if ((VALIDATE_ONLY)); then
    say "validate-only: nothing was written."
    exit 0
fi
create
verify
say "done. Check on the server: scripts/release.sh check-tag $TAG --expect-sha $SHA"
