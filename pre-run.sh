#!/bin/bash
cutoff=$(date -v-2m +%Y%m%d 2>/dev/null || date -d '2 months ago' +%Y%m%d)
for f in db/database-*.db; do
  [[ "$f" =~ db/database-([0-9]{8})- ]] || continue
  [[ "${BASH_REMATCH[1]}" < "$cutoff" ]] && rm "$f"
done
echo "Deleted stale db files"
