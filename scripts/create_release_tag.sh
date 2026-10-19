#!/usr/bin/env bash
# Create ONE annotated release tag on GitHub, for the "Create release tag" workflow
# (.github/workflows/create-release-tag.yml). Runs in CI, never on the server.
#
#   scripts/create_release_tag.sh --tag vX.Y.Z[-rcN] --expected-sha <40 hex>
#                                 [--dry-run | --validate-only] [--replace-lightweight]
#
# Environment: GITHUB_REPOSITORY (owner/name), GH_TOKEN (for `gh api`), RELEASE_BRANCH (default
# release/v2.0.0), RELEASE_REMOTE (default origin). Run from a checkout that has the full history.
#
# Everything is validated BEFORE the first write; any failure stops with nothing written or deleted:
#   * the tag is a release tag (same pattern as release.sh) and the SHA is 40 lowercase hex;
#   * the commit exists and is reachable from <remote>/<RELEASE_BRANCH> (no other commit may be tagged);
#   * the commit carries the files release.sh requires of a tag;
#   * the tag state on the remote decides the mode (see below).
#
# MODE create (the tag does not exist anywhere): exactly two creations, in this order, both plain POSTs
# (GitHub refuses to create a ref that exists, so nothing can be overwritten):
#   1. POST git/tags  -> an annotated tag OBJECT that points at the commit
#   2. POST git/refs  -> refs/tags/<tag> pointing at that tag object
#
# MODE replace (ONE known case only, and only with --replace-lightweight): the tag exists as a
# LIGHTWEIGHT tag. It is replaced only if ALL of these hold; otherwise nothing is touched:
#   a) the existing tag is lightweight (an annotated tag is NEVER replaced or force-updated);
#   b) it currently points exactly at REPLACE_ALLOW_OLD_SHA;
#   c) the requested SHA is exactly REPLACE_ALLOW_NEW_SHA and the tag is REPLACE_ALLOW_TAG;
#   d) that SHA is reachable from <remote>/<RELEASE_BRANCH> (checked above for every mode; it need not
#      be the tip), and the old commit is its ancestor.
# Sequence: 1. create the annotated tag object and read it back; 2. only then, re-check the remote ref
# is still the old lightweight one; 3. DELETE that one ref; 4. at once POST the new ref that points at
# the tag object made in step 1. If step 4 fails the tag is TEMPORARILY ABSENT: the script says so,
# prints the exact state and the two ways to put a tag back, and stops. It does not retry.
#
# Both modes end with a read-back: the remote ref exists, is a tag object (not lightweight), and
# peels to the expected commit. --dry-run (alias --validate-only) does every check and makes NO API
# call: no object, no delete, no ref.
#
# Never here: git push, update-ref, force, PATCH, PUT, a delete of anything but the single
# lightweight ref described above, or any repair attempt.
set -euo pipefail

TAG_RE='^v[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-rc[0-9]{1,3})?$'
SHA_RE='^[0-9a-f]{40}$'
RELEASE_BRANCH="${RELEASE_BRANCH:-release/v2.0.0}"
RELEASE_REMOTE="${RELEASE_REMOTE:-origin}"
# The one lightweight tag that may be replaced (v2.0.0-rc1 was never deployed). The workflow never
# sets these; they are only overridable so that the tests can use throw-away commits.
REPLACE_ALLOW_TAG="${REPLACE_ALLOW_TAG:-v2.0.0-rc1}"
REPLACE_ALLOW_OLD_SHA="${REPLACE_ALLOW_OLD_SHA:-74214af5aa2be3a71dc883d5a36b9c6296cc0f37}"
REPLACE_ALLOW_NEW_SHA="${REPLACE_ALLOW_NEW_SHA:-69566e3b8ef8b4c64c42c5aa6d69c9421bb12ca7}"
# What release.sh requires of a tagged tree (git_check); a tag lacking them could never be deployed.
REQUIRED_PATHS=(Dockerfile docker-compose.yml scripts/backup.sh release/migration_compat.json)

say() { printf '%s\n' "$*"; }
die() { printf 'create-release-tag: %s\nNOTHING WAS WRITTEN OR DELETED.\n' "$*" >&2; exit 1; }
die_after_write() { printf 'create-release-tag: %s\n' "$*" >&2; exit 1; }

TAG=""; SHA=""; DRY_RUN=0; REPLACE=0
while (($#)); do
    case "$1" in
        --tag) (($# >= 2)) || die "--tag needs a value"; TAG="$2"; shift 2 ;;
        --expected-sha) (($# >= 2)) || die "--expected-sha needs a value"; SHA="$2"; shift 2 ;;
        --dry-run | --validate-only) DRY_RUN=1; shift ;;
        --replace-lightweight) REPLACE=1; shift ;;
        *) die "unknown argument: $1" ;;
    esac
done

MODE=""        # create | replace
OLD_SHA=""     # replace: what the lightweight tag points at now
TIP=""
TAG_OBJECT=""

# ---------------------------------------------------------------- validation (read-only)

remote_tag_lines() { git ls-remote "$RELEASE_REMOTE" "refs/tags/$TAG" "refs/tags/$TAG^{}" 2>/dev/null; }
line_sha() { awk -v r="$1" '$2 == r { print $1 }' <<<"$2"; }  # <ref name> <ls-remote output>

validate() {
    [[ "$TAG" =~ $TAG_RE ]] || die "not a release tag (expected vX.Y.Z or vX.Y.Z-rcN): '$TAG'"
    [[ "$SHA" =~ $SHA_RE ]] || die "expected_sha must be the full 40-character lowercase commit id: '$SHA'"
    [[ "$RELEASE_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || die "invalid RELEASE_BRANCH: '$RELEASE_BRANCH'"
    git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not inside a git repository"

    git fetch --quiet --no-tags "$RELEASE_REMOTE" "$RELEASE_BRANCH" \
        || die "cannot fetch $RELEASE_REMOTE/$RELEASE_BRANCH"
    TIP="$(git rev-parse -q --verify "refs/remotes/$RELEASE_REMOTE/$RELEASE_BRANCH^{commit}")" \
        || die "$RELEASE_REMOTE/$RELEASE_BRANCH does not exist"

    [[ "$(git cat-file -t "$SHA" 2>/dev/null || true)" == commit ]] \
        || die "$SHA is not a commit in this repository"
    git merge-base --is-ancestor "$SHA" "$TIP" \
        || die "$SHA is not part of $RELEASE_REMOTE/$RELEASE_BRANCH ($TIP): refusing to tag a commit outside the release branch"

    local path
    for path in "${REQUIRED_PATHS[@]}"; do
        git cat-file -e "$SHA:$path" 2>/dev/null \
            || die "commit $SHA does not contain $path, so release.sh would refuse the tag"
    done

    local lines ref_sha peeled local_obj
    lines="$(remote_tag_lines)" || die "cannot read tags from $RELEASE_REMOTE"
    ref_sha="$(line_sha "refs/tags/$TAG" "$lines")"
    peeled="$(line_sha "refs/tags/$TAG^{}" "$lines")"
    local_obj="$(git rev-parse -q --verify "refs/tags/$TAG" 2>/dev/null || true)"

    if [[ -z "$ref_sha" ]]; then
        [[ -z "$local_obj" ]] \
            || die "tag $TAG already exists in this checkout (but not on $RELEASE_REMOTE): tags are never overwritten, moved or deleted"
        MODE=create
        return 0
    fi

    # the tag exists on the remote
    [[ -z "$peeled" ]] \
        || die "tag $TAG already exists on $RELEASE_REMOTE and is ANNOTATED ($ref_sha): an annotated tag is never replaced, moved, deleted or force-updated"
    OLD_SHA="$ref_sha"
    # a lightweight tag: replaceable in exactly one known case
    [[ "$TAG" == "$REPLACE_ALLOW_TAG" ]] \
        || die "tag $TAG already exists on $RELEASE_REMOTE as a lightweight tag at $OLD_SHA, and only $REPLACE_ALLOW_TAG may be replaced: not touching it"
    [[ "$OLD_SHA" == "$REPLACE_ALLOW_OLD_SHA" ]] \
        || die "tag $TAG already exists on $RELEASE_REMOTE (lightweight) at $OLD_SHA, not at the known $REPLACE_ALLOW_OLD_SHA: not retargeting an unexpected tag"
    [[ "$SHA" == "$REPLACE_ALLOW_NEW_SHA" ]] \
        || die "tag $TAG exists (lightweight, $OLD_SHA); it may only be replaced by one at $REPLACE_ALLOW_NEW_SHA, not $SHA"
    [[ -z "$local_obj" || "$local_obj" == "$OLD_SHA" ]] \
        || die "the local $TAG ($local_obj) differs from $RELEASE_REMOTE's ($OLD_SHA): inconsistent, not touching it"
    [[ "$(git cat-file -t "$OLD_SHA" 2>/dev/null || true)" == commit ]] \
        || die "the old commit $OLD_SHA is not available in this checkout"
    git merge-base --is-ancestor "$OLD_SHA" "$SHA" \
        || die "the old commit $OLD_SHA is not an ancestor of $SHA: the replacement must move forward only"
    ((REPLACE)) \
        || die "tag $TAG exists as the known lightweight tag at $OLD_SHA. It can be replaced, but only on request: run again with replace_lightweight (--replace-lightweight)"
    MODE=replace
}

# ---------------------------------------------------------------- creation

gh_post_tag_object() {
    local tag_object back
    tag_object="$(gh api --method POST "repos/$GITHUB_REPOSITORY/git/tags" \
        -f tag="$TAG" -f message="Release $TAG ($SHA)" -f object="$SHA" -f type=commit \
        -f 'tagger[name]=github-actions[bot]' \
        -f 'tagger[email]=41898282+github-actions[bot]@users.noreply.github.com' \
        --jq .sha)" || die "GitHub refused to create the tag object (nothing refers to it, no tag was touched)"
    [[ "$tag_object" =~ $SHA_RE ]] || die "unexpected answer when creating the tag object (no tag was touched)"
    back="$(gh api "repos/$GITHUB_REPOSITORY/git/tags/$tag_object" --jq '.object.sha + " " + .object.type')" \
        || die "cannot read the new tag object $tag_object back (no tag was touched)"
    [[ "$back" == "$SHA commit" ]] \
        || die "the new tag object $tag_object points to '$back', expected '$SHA commit' (no tag was touched)"
    TAG_OBJECT="$tag_object"
    say "annotated tag object created and read back: $TAG_OBJECT (not yet referenced)"
}

create_mode() {
    gh_post_tag_object
    gh api --method POST "repos/$GITHUB_REPOSITORY/git/refs" \
        -f ref="refs/tags/$TAG" -f sha="$TAG_OBJECT" --jq .ref >/dev/null \
        || die_after_write "GitHub refused to create refs/tags/$TAG (it may exist now). Nothing was overwritten or deleted; the tag object $TAG_OBJECT is unreferenced and harmless. Check the tag by hand."
    say "refs/tags/$TAG created -> $TAG_OBJECT"
}

replace_mode() {
    gh_post_tag_object   # 1. the annotated object exists and is read back BEFORE anything is deleted

    # 2. the remote ref must still be exactly the old lightweight tag
    local lines now
    lines="$(remote_tag_lines)" || die "cannot re-read $TAG from $RELEASE_REMOTE (the old tag is untouched)"
    now="$(line_sha "refs/tags/$TAG" "$lines")"
    [[ "$now" == "$OLD_SHA" && -z "$(line_sha "refs/tags/$TAG^{}" "$lines")" ]] \
        || die "refs/tags/$TAG changed while the script ran (now '${now:-absent}'): the old tag was not deleted"

    # 3. delete that one ref; 4. at once create the new one
    gh api --method DELETE "repos/$GITHUB_REPOSITORY/git/refs/tags/$TAG" >/dev/null \
        || die_after_write "DELETE of the old lightweight refs/tags/$TAG failed. State now: $(remote_tag_lines | tr '\n' ' ')"
    say "old lightweight refs/tags/$TAG ($OLD_SHA) deleted"
    if ! gh api --method POST "repos/$GITHUB_REPOSITORY/git/refs" \
        -f ref="refs/tags/$TAG" -f sha="$TAG_OBJECT" --jq .ref >/dev/null; then
        die_after_write "THE TAG $TAG IS TEMPORARILY ABSENT: the old lightweight tag ($OLD_SHA) was deleted and creating the new ref FAILED.
State: refs/tags/$TAG does not exist (or was created by somebody else). The annotated tag object $TAG_OBJECT exists, points at $SHA and is unreferenced. Nothing was retried.
To put the annotated tag back (one command):
  gh api --method POST repos/$GITHUB_REPOSITORY/git/refs -f ref=refs/tags/$TAG -f sha=$TAG_OBJECT
or run this workflow again (the tag is missing, so it takes the normal creation path).
To restore the old lightweight tag instead:
  gh api --method POST repos/$GITHUB_REPOSITORY/git/refs -f ref=refs/tags/$TAG -f sha=$OLD_SHA"
    fi
    say "refs/tags/$TAG created -> $TAG_OBJECT (replaced the lightweight tag at $OLD_SHA)"
}

# ---------------------------------------------------------------- read-back

verify() {
    local out ref_sha peeled kind commit probe="refs/release-verify/$TAG" absent=""
    [[ "$MODE" == replace ]] && absent="The old lightweight tag ($OLD_SHA) was deleted. "
    out="$(remote_tag_lines)" || die_after_write "${absent}cannot read $RELEASE_REMOTE to verify the tag"
    ref_sha="$(line_sha "refs/tags/$TAG" "$out")"
    peeled="$(line_sha "refs/tags/$TAG^{}" "$out")"
    [[ -n "$ref_sha" ]] || die_after_write "VERIFY FAILED: ${absent}refs/tags/$TAG is not on $RELEASE_REMOTE"
    [[ -n "$peeled" ]] || die_after_write "VERIFY FAILED: ${absent}$TAG is a lightweight tag (no peeled entry). Do not re-tag; investigate."
    [[ "$ref_sha" == "$TAG_OBJECT" ]] \
        || die_after_write "VERIFY FAILED: ${absent}refs/tags/$TAG is $ref_sha, expected the tag object $TAG_OBJECT"
    [[ "$peeled" == "$SHA" ]] \
        || die_after_write "VERIFY FAILED: ${absent}$TAG peels to $peeled, expected $SHA. Do not re-tag; investigate."
    # fetch the objects into a private namespace (not refs/tags) and look at them
    git fetch --quiet --no-tags "$RELEASE_REMOTE" "refs/tags/$TAG:$probe" \
        || die_after_write "VERIFY FAILED: ${absent}cannot fetch the new tag"
    kind="$(git cat-file -t "$probe")"
    [[ "$kind" == tag ]] || die_after_write "VERIFY FAILED: ${absent}the tag object type is '$kind', expected 'tag'"
    commit="$(git rev-parse "$probe^{commit}")"
    [[ "$commit" == "$SHA" ]] || die_after_write "VERIFY FAILED: ${absent}the peeled commit is $commit, expected $SHA"
    say "verified: $TAG is an annotated tag object ($ref_sha) whose peeled commit is $commit"
    say "           the ref exists on $RELEASE_REMOTE"
}

# ---------------------------------------------------------------- main

validate
case "$MODE" in
    create) say "validated: $TAG -> $SHA is on $RELEASE_REMOTE/$RELEASE_BRANCH and $TAG does not exist yet (mode: create)" ;;
    replace) say "validated: $TAG exists as the known LIGHTWEIGHT tag at $OLD_SHA and may be replaced by an annotated tag at $SHA, which is reachable from $RELEASE_REMOTE/$RELEASE_BRANCH (mode: replace)" ;;
esac
if ((DRY_RUN)); then
    case "$MODE" in
        create) say "dry run, would: create the annotated tag object, create refs/tags/$TAG, read back." ;;
        replace) say "dry run, would: create the annotated tag object and read it back; re-check $TAG is still at $OLD_SHA; delete that lightweight ref; create refs/tags/$TAG at the tag object; read back." ;;
    esac
    say "dry run: nothing was written or deleted."
    exit 0
fi
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is not set}"
[[ "$GITHUB_REPOSITORY" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "invalid GITHUB_REPOSITORY"
case "$MODE" in
    create) create_mode ;;
    replace) replace_mode ;;
esac
verify
say "done. Check on the server: scripts/release.sh check-tag $TAG --expect-sha $SHA"
