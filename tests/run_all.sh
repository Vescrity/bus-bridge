#!/bin/bash
# Run every integration test.  Each test is self-contained: it starts two
# private dbus-daemons and one bridge process and cleans up afterwards.
set -u
cd "$(dirname "$0")"

status=0
for test in test_*.py; do
    printf '%-24s ' "$test"
    if timeout 120 python3 "$test" >"/tmp/bus-bridge-$test.log" 2>&1; then
        echo PASS
    else
        echo FAIL
        sed 's/^/    /' "/tmp/bus-bridge-$test.log" | tail -40
        status=1
    fi
done
exit "$status"
