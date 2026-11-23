#!/bin/bash
# Double-click in Finder to start QC V6 on a Mac (opens Terminal). Same as running ./start.sh.
cd "$(dirname "$0")" || exit 1
exec /bin/bash ./start.sh
