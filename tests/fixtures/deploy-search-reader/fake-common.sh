# shellcheck shell=bash
# Shared helpers for the deploy-search-reader test fakes. Sourced, not executed.
# Knobs are environment variables set by the test; counters live in $FAKE_STATE.

# bump <name>: increment and print the per-name call counter.
bump() {
  local file="$FAKE_STATE/count.$1" n
  n=$(cat "$file" 2>/dev/null || echo 0)
  n=$((n + 1))
  echo "$n" >"$file"
  echo "$n"
}

# seq_next <name> <comma-list>: the Nth element for the Nth call; the last repeats.
seq_next() {
  local n list=$2 i=0 item last=""
  n=$(bump "$1")
  local IFS=','
  for item in $list; do
    i=$((i + 1))
    last=$item
    if [ "$i" -eq "$n" ]; then echo "$item"; return 0; fi
  done
  echo "$last"
}

# hit <kind> <comma-list> <op>: success when <op> is listed and the call is
# selected. "op" selects every call, "op:N" the first N calls, "op@K" every call
# after the first K.
hit() {
  local kind=$1 list=$2 op=$3 entry name spec n
  local IFS=','
  for entry in $list; do
    name=${entry%%[:@]*}
    [ "$name" = "$op" ] || continue
    spec=${entry#"$name"}
    n=$(bump "$kind.$op")
    case "$spec" in
      "") return 0;;
      :*) if [ "$n" -le "${spec#:}" ]; then return 0; fi;;
      @*) if [ "$n" -gt "${spec#@}" ]; then return 0; fi;;
    esac
  done
  return 1
}
