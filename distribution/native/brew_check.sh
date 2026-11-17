#!/usr/bin/env bash
# Homebrew native checks on a disposable GitHub-hosted runner. No credentials.
#
#   brew_check.sh setup     OUT
#   brew_check.sh build     PLATFORM OUT PREPARE [OLDER_PREPARE]
#   brew_check.sh check     PLATFORM OUT PREPARE BREW
#   brew_check.sh lifecycle PLATFORM OUT OLDER_PREPARE OLDER_BREW PREPARE BREW
#
# PREPARE is a prepare.py candidate tree (prepare.json), BREW a bottled tree
# (brew.json). The staging tap is darkquasar/tap created with --no-git, so the
# formula's full name equals the published one. Run from the tap checkout root.
set -uo pipefail

TAP=darkquasar/tap
FORMULA=patronus
NATIVE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=("${NATIVE_PYTHON:-python3}" -B)  # setup-python, not a Homebrew python3 earlier on PATH
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ENV_HINTS=1

die() { echo "error: $*" >&2; exit 1; }
field() { "${PY[@]}" -c 'import json,sys; v=json.load(open(sys.argv[1])); [v:=v[k] for k in sys.argv[2:]]; print("" if v is None else v)' "$@"; }

host_platform() {
  local system cpu
  case "$(uname -s)" in Darwin) system=darwin ;; Linux) system=linux ;; *) system=unknown ;; esac
  case "$(uname -m)" in arm64|aarch64) cpu=arm64 ;; x86_64) cpu=amd64 ;; *) cpu=unknown ;; esac
  echo "$system-$cpu"
}

require_host() {
  [ "$(host_platform)" = "$1" ] || die "runner is $(host_platform), not $1"
}

setup() {
  local out="$1" brew_bin=""
  mkdir -p "$out"
  for candidate in "$(command -v brew || true)" /opt/homebrew/bin/brew /usr/local/bin/brew \
                   /home/linuxbrew/.linuxbrew/bin/brew; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then brew_bin="$candidate"; break; fi
  done
  if [ -z "$brew_bin" ]; then
    [ "$(uname -s)" = Linux ] || die 'Homebrew missing on a macOS runner'
    echo 'Homebrew absent: installing with the pinned installer revision'
    sudo apt-get update
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential procps curl file git ca-certificates
    local installer url sha
    installer="$RUNNER_TEMP/homebrew-install.sh"
    url="$(field "$NATIVE/pins.json" homebrew_installer url)"
    sha="$(field "$NATIVE/pins.json" homebrew_installer sha256)"
    curl --fail --location --silent --show-error --max-time 120 "$url" --output "$installer"
    printf '%s  %s\n' "$sha" "$installer" | sha256sum --check || die 'Homebrew installer checksum mismatch'
    NONINTERACTIVE=1 /bin/bash "$installer" || die 'Homebrew installation failed'
    brew_bin=/home/linuxbrew/.linuxbrew/bin/brew
    echo installed > "$out/homebrew-source.txt"
  else
    echo preinstalled > "$out/homebrew-source.txt"
  fi
  dirname "$brew_bin" >> "${GITHUB_PATH:-/dev/null}"
  {
    echo "platform: $(host_platform)"
    echo "runner_label: ${RUNNER_LABEL:-}"
    echo "image: ${ImageOS:-} ${ImageVersion:-}"
    uname -a
    if [ "$(uname -s)" = Darwin ]; then sw_vers; else cat /etc/os-release; fi
    echo "brew: $brew_bin"
    "$brew_bin" --version
    git -C "$("$brew_bin" --repository)" rev-parse HEAD
    python3 --version
    ls "${RUNNER_TOOL_CACHE:-/nonexistent}/Python" 2>/dev/null | sed 's/^/toolcache-python: /'
  } | tee "$out/host.txt"
}

serve() {  # serve DIR LOOPBACK_URL LOG
  local port
  port="${2##*:}"
  "${PY[@]}" -m http.server "$port" --bind 127.0.0.1 --directory "$1" > "$3" 2>&1 &
  SERVER=$!
  for _ in $(seq 1 40); do
    curl --silent --fail --max-time 2 "$2/" > /dev/null && return 0
    sleep 0.25
  done
  die "loopback server on $2 unavailable"
}

staging_tap() {
  local repo
  repo="$(brew --repository)/Library/Taps/darkquasar/homebrew-tap"
  if [ ! -d "$repo" ]; then
    brew tap-new --no-git "$TAP" > /dev/null || die 'brew tap-new failed'
    rm -rf "$repo/.github"
  fi
  echo "$repo"
}

trust() {
  if brew trust --help > /dev/null 2>&1; then
    brew trust --formula "$TAP/$FORMULA" || die 'brew trust failed'
  fi
}

report() {  # report STAGE PLATFORM OUTCOME REASON STEPS INPUTS OUT
  "${PY[@]}" -m distribution.native.prepare report --stage "$1" --manager brew --platform "$2" \
    --outcome "$3" --reason "$4" --steps "$5" --inputs "$6" --out "$7" \
    --runner "${RUNNER_LABEL:-unknown}" --manager-version "$(brew --version | head -1)" > /dev/null
}

# Exit status for a finished platform: normal runs need a pass; negatives need the matching failure.
verdict() {  # verdict EXPECT OUTCOME REASON
  case "$1" in
    '') [ "$2" = passed ] ;;
    arch) [ "$2" = failed ] && [[ "$3" == architecture-mismatch:* ]] ;;
    version) [ "$2" = failed ] && [[ "$3" == version-mismatch:* ]] ;;
    *) return 1 ;;
  esac
}

expected_sha() {  # expected_sha PREPARE PLATFORM
  "${PY[@]}" -c 'import json,sys
p=sys.argv[1]; r=json.load(open(p+"/registration.json")); s=json.load(open(p+"/snapshot.json"))
print(s["payload"]["identity"]["assets"][r["configuration"]["assets"][sys.argv[2]]]["sha256"])' "$1" "$2"
}

file_sha() { "${PY[@]}" -c 'import hashlib,os,sys; print(hashlib.sha256(open(os.path.realpath(sys.argv[1]),"rb").read()).hexdigest())' "$1"; }

# Version and identity of the resolved command. Prints a reason on failure.
resolved_check() {  # resolved_check PREPARE PLATFORM VERSION LOG
  local path sha expected output
  hash -r
  path="$(command -v "$FORMULA")" || { echo "install-failed: $FORMULA not on PATH"; return 1; }
  case "$path" in "$(brew --prefix)"/bin/*) ;; *) echo "install-failed: $FORMULA resolves to $path, not the Homebrew prefix"; return 1 ;; esac
  sha="$(file_sha "$path")"
  expected="$(expected_sha "$1" "$2")"
  output="$("$path" --version 2>&1)"
  { echo "path: $path"; echo "realpath: $("${PY[@]}" -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$path")"
    echo "sha256: $sha"; echo "expected: $expected"; echo "output: $output"; } > "$4"
  if [ "$sha" != "$expected" ]; then
    echo "architecture-mismatch: resolved executable sha256 $sha is not the $2 snapshot asset $expected"; return 1
  fi
  case "$output" in
    *"v$3"*) ;;
    *) echo "version-mismatch: expected v$3, got: $output"; return 1 ;;
  esac
}

build_one() {  # build_one PLATFORM OUT PREPARE ; prints a failure reason on stdout
  local platform="$1" out="$2" prepare="$3" version loopback root repo work json tag filename local_name cellar
  version="$(field "$prepare/prepare.json" version)"
  loopback="$(field "$prepare/prepare.json" loopback)"
  root="$("${PY[@]}" -c 'import json,sys
from distribution.bottles import canonical_root_url
p=sys.argv[1]; r=json.load(open(p+"/registration.json")); s=json.load(open(p+"/snapshot.json"))
print(canonical_root_url(r["configuration"], s["payload"]["identity"]))' "$prepare")"
  mkdir -p "$out/$version"
  SERVER=
  if [ -n "$loopback" ]; then serve "$prepare/assets" "$loopback" "$out/$version/assets-http.log" >&2; fi
  repo="$(staging_tap)"
  cp "$prepare/staging/brew/Formula/$FORMULA.rb" "$repo/Formula/$FORMULA.rb"
  trust >&2
  brew install --build-bottle "$TAP/$FORMULA" > "$out/$version/build-install.log" 2>&1 \
    || { echo "bottle-build-failed: brew install --build-bottle exited non-zero (see build-install.log)"; return 1; }
  work="$out/$version/bottle"
  mkdir -p "$work"
  (cd "$work" && brew bottle --json --no-rebuild --root-url="$root" "$TAP/$FORMULA") > "$out/$version/bottle.log" 2>&1 \
    || { echo "bottle-build-failed: brew bottle exited non-zero (see bottle.log)"; return 1; }
  json="$(ls "$work"/*.bottle.json)"
  cp "$json" "$out/$version/"
  read -r tag filename local_name cellar < <("${PY[@]}" -c 'import json,sys
d=json.load(open(sys.argv[1])); (entry,)=d.values(); b=entry["bottle"]; (tag,)=b["tags"]
t=b["tags"][tag]; print(tag, t["filename"], t["local_filename"], b["cellar"])' "$json")
  echo "$tag $filename $local_name $cellar" > "$out/$version/bottle-names.txt"
  [ "$cellar" = any_skip_relocation ] || { echo "bottle-build-failed: cellar $cellar is not any_skip_relocation"; return 1; }
  mkdir -p "$out/bottles"
  mv "$work/$local_name" "$out/bottles/$filename"
  tar -tvzf "$out/bottles/$filename" > "$out/$version/members.txt" 2>&1
  local code
  "${PY[@]}" -m distribution.cli verify-bottle --archive "$out/bottles/$filename" --tag "$tag" \
    --registration "$prepare/registration.json" --snapshot "$prepare/snapshot.json" \
    --out "$out/$version/verify.json" > /dev/null 2>&1
  code=$?
  if [ $code -ne 0 ]; then
    local message
    message="$(field "$out/$version/verify.json" message)"
    rm -f "$out/bottles/$filename"
    # Any nonzero verify-bottle exit fails the build; another platform's executable is the arch diagnostic.
    if [[ "$message" == *" asset, not "* ]]; then
      echo "architecture-mismatch: $message"
    else
      echo "bottle-verification-failed: $(field "$out/$version/verify.json" error): $message"
    fi
    return 1
  fi
  [ "$("${PY[@]}" -c 'import sys; from distribution.bottles import bottle_tag_platform as t; print("-".join(t(sys.argv[1])))' "$tag")" = "$platform" ] \
    || { echo "bottle-build-failed: built tag $tag does not belong to $platform"; return 1; }
  brew uninstall "$TAP/$FORMULA" > "$out/$version/build-uninstall.log" 2>&1
  [ -z "$SERVER" ] || kill "$SERVER"
  return 0
}

build() {
  local platform="$1" out="$2" prepare="$3" older="${4:-}" reason expect outcome=passed
  require_host "$platform"
  mkdir -p "$out"
  expect="$(field "$prepare/prepare.json" expect_failure)"
  if [ -n "$older" ]; then
    reason="$(build_one "$platform" "$out" "$older")" || die "older lifecycle bottle failed: $reason"
  fi
  if ! reason="$(build_one "$platform" "$out" "$prepare")"; then
    outcome=failed
    report build "$platform" failed "$reason" validate=failed,install=not-run,version=not-run,uninstall=not-run \
      "$prepare/prepare.json" "$out/report-build-brew-$platform.json"
  fi
  echo "build $platform: $outcome ${reason:-}" | tee "$out/build-outcome.txt"
  if [ -z "$expect" ] || [ "$expect" = version ]; then [ "$outcome" = passed ]; else verdict "$expect" "$outcome" "$reason"; fi
}

check() {
  local platform="$1" out="$2" prepare="$3" brew_dir="$4" version expect tag repo reason="" outcome=failed
  local validate=not-run install=not-run versioned=not-run uninstall=not-run
  require_host "$platform"
  mkdir -p "$out"
  version="$(field "$prepare/prepare.json" version)"
  expect="$(field "$prepare/prepare.json" expect_failure)"
  tag="$("${PY[@]}" -c 'import json,sys
from distribution.bottles import bottle_tag_platform as t
d=json.load(open(sys.argv[1])); print(next(k for k in d["bottle_set"]["tags"] if "-".join(t(k))==sys.argv[2]))' "$brew_dir/brew.json" "$platform")"
  serve "$brew_dir/bottles" "$(field "$brew_dir/brew.json" loopback)" "$out/bottles-http.log"
  repo="$(staging_tap)"
  cp "$brew_dir/staging/brew/Formula/$FORMULA.rb" "$repo/Formula/$FORMULA.rb"
  trust
  while :; do
    if "${PY[@]}" -m distribution.native.prepare validate-bottled --prepare "$prepare" --brew "$brew_dir" \
         --out "$out/validate.json" > "$out/validate.log" 2>&1; then validate=passed
    else validate=failed; reason='validation-failed: ruby -c / brew style / bottle block (see validate.json)'; break; fi
    if brew install "$TAP/$FORMULA" > "$out/install.log" 2>&1 \
       && grep -q "Pouring $FORMULA--$version.$tag.bottle.tar.gz" "$out/install.log" \
       && ! grep -qi 'from source' "$out/install.log" \
       && brew info --json=v2 "$TAP/$FORMULA" > "$out/info.json" \
       && [ "$("${PY[@]}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["formulae"][0]["installed"][0]["poured_from_bottle"])' "$out/info.json")" = True ]; then
      install=passed
    else install=failed; reason="install-failed: no pour of $FORMULA--$version.$tag.bottle.tar.gz (see install.log)"; break; fi
    if reason="$(resolved_check "$prepare" "$platform" "$version" "$out/version.txt")" \
       && brew test "$TAP/$FORMULA" > "$out/test.log" 2>&1; then
      versioned=passed; reason=""
    else
      versioned=failed; reason="${reason:-version-failed: brew test failed (see test.log)}"; break
    fi
    if brew uninstall "$TAP/$FORMULA" > "$out/uninstall.log" 2>&1 && { hash -r; ! command -v "$FORMULA" > /dev/null; }; then
      uninstall=passed; outcome=passed
    else uninstall=failed; reason="uninstall-failed: $FORMULA still resolves after brew uninstall"; fi
    break
  done
  report check "$platform" "$outcome" "$reason" \
    "validate=$validate,install=$install,version=$versioned,uninstall=$uninstall" \
    "$brew_dir/brew.json" "$out/report-check-brew-$platform.json"
  echo "check $platform: $outcome $reason" | tee "$out/check-outcome.txt"
  verdict "$expect" "$outcome" "$reason"
}

lifecycle() {
  local platform="$1" out="$2" older="$3" older_brew="$4" prepare="$5" brew_dir="$6"
  local old_version new_version tag repo project stages="$2/stages.jsonl" reason
  require_host "$platform"
  mkdir -p "$out/served"
  old_version="$(field "$older/prepare.json" version)"
  new_version="$(field "$prepare/prepare.json" version)"
  [ "$(field "$older_brew/brew.json" loopback)" = "$(field "$brew_dir/brew.json" loopback)" ] || die 'bottle loopback roots differ'
  tag="$("${PY[@]}" -c 'import json,sys
from distribution.bottles import bottle_tag_platform as t
d=json.load(open(sys.argv[1])); print(next(k for k in d["bottle_set"]["tags"] if "-".join(t(k))==sys.argv[2]))' "$brew_dir/brew.json" "$platform")"
  cp "$older_brew"/bottles/* "$brew_dir"/bottles/* "$out/served/"
  serve "$out/served" "$(field "$brew_dir/brew.json" loopback)" "$out/bottles-http.log"
  repo="$(staging_tap)"
  project="$RUNNER_TEMP/lifecycle-project"
  mkdir -p "$project"
  stage() {  # stage NAME COMMAND EXIT VERSION_FILE PRESERVATION_FILE
    "${PY[@]}" -c 'import json,sys
name,command,code,version,preservation=sys.argv[1:6]
v=open(version).read() if version else ""
p=json.load(open(preservation))
print(json.dumps(dict(stage=name,command=command,exit=int(code),resolution=v,preserved=p["preserved"],differences=p["differences"])))' "$@" >> "$stages"
  }
  preserved() {  # preserved NAME
    "${PY[@]}" -m distribution.native.prepare preservation check --source distribution/tests/native/preservation \
      --project "$project" --home "$HOME" --out "$out/preservation-$1.json" > /dev/null
  }
  "${PY[@]}" -m distribution.native.prepare preservation seed --source distribution/tests/native/preservation \
    --project "$project" --home "$HOME" --out "$out/preservation-seeded.json" > /dev/null || die 'seeding sentinels failed'
  stage seeded 'seed preservation sentinels' 0 '' "$out/preservation-seeded.json"
  cp "$older_brew/staging/brew/Formula/$FORMULA.rb" "$repo/Formula/$FORMULA.rb"
  trust
  brew install "$TAP/$FORMULA" > "$out/install.log" 2>&1 || die 'older bottled install failed'
  grep -q "Pouring $FORMULA--$old_version.$tag.bottle.tar.gz" "$out/install.log" || die "no pour of $old_version"
  reason="$(resolved_check "$older" "$platform" "$old_version" "$out/version-installed.txt")" || die "$reason"
  preserved installed || die 'sentinels changed after install'
  stage installed "brew install $TAP/$FORMULA" 0 "$out/version-installed.txt" "$out/preservation-installed.json"
  # Advance the staging catalogue: the no-git tap now carries the newer canonical formula's staging copy.
  cp "$brew_dir/staging/brew/Formula/$FORMULA.rb" "$repo/Formula/$FORMULA.rb"
  brew upgrade "$TAP/$FORMULA" > "$out/upgrade.log" 2>&1 || die 'brew upgrade failed'
  grep -q "Pouring $FORMULA--$new_version.$tag.bottle.tar.gz" "$out/upgrade.log" || die "upgrade did not pour $new_version"
  ! grep -qi 'from source' "$out/upgrade.log" || die 'upgrade built from source'
  reason="$(resolved_check "$prepare" "$platform" "$new_version" "$out/version-upgraded.txt")" || die "$reason"
  preserved upgraded || die 'sentinels changed after upgrade'
  stage upgraded "brew upgrade $TAP/$FORMULA" 0 "$out/version-upgraded.txt" "$out/preservation-upgraded.json"
  brew uninstall "$TAP/$FORMULA" > "$out/uninstall.log" 2>&1 || die 'brew uninstall failed'
  hash -r
  ! command -v "$FORMULA" > /dev/null || die "$FORMULA still resolves after uninstall"
  preserved removed || die 'sentinels changed after uninstall'
  stage removed "brew uninstall $TAP/$FORMULA" 0 '' "$out/preservation-removed.json"
  "${PY[@]}" -c 'import json,sys
stages=[json.loads(l) for l in open(sys.argv[1])]
print(json.dumps(dict(manager="brew", platform=sys.argv[2], outcome="passed",
  package_shape="Homebrew formula, bottled (cellar any_skip_relocation), staging tap darkquasar/tap (--no-git), bottles poured from a loopback root_url",
  versions=[sys.argv[3], sys.argv[4]], stages=stages), indent=2))' "$stages" "$platform" "$old_version" "$new_version" \
    > "$out/lifecycle.json"
  cat "$out/lifecycle.json"
}

command="${1:-}"
shift || true
case "$command" in
  setup) setup "$@" ;;
  build) build "$@" ;;
  check) check "$@" ;;
  lifecycle) lifecycle "$@" ;;
  *) die "usage: brew_check.sh setup|build|check|lifecycle ..." ;;
esac
