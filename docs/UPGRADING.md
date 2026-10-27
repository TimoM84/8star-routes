# Updating a running installation (Dockhand)

Your database (`/data/routes.sqlite` in the `routes-data` volume) holds the collected history, corridors and the
request ledger. An update never resets or recreates it: the new container opens the same file and only *adds* what
the new version needs. This page describes how to update safely, how to check the upgrade first, and how to go back.

## What the update to migration 003 changes

Migration `003_route_families` (this release) only **adds**:

- a table `route_families` (stable identities for route geometries),
- a column `route_variants.family_id`,
- a column `corridors.retired_utc`, filled for already retired corridors with the time of their last observation,
- two indexes.

No row is deleted or modified, apart from filling the two new columns. After the SQL migration the application
assigns a route family to every stored geometry. This uses only stored data: **no TomTom requests**.

Safety properties, all verified in automated tests and with a real `docker compose` rebuild (see
[TESTING.md](TESTING.md)):

- Before any migration runs, a consistent copy is written to `/data/backups/pre-migration-<time>.sqlite`. If that
  backup cannot be written (for example a full disk), the application stops before changing anything.
- Each migration runs in one transaction. If it fails or the container is killed half-way, nothing of it is applied
  and the next start simply tries again.
- Family assignment is idempotent and runs in small transactions. If the container is stopped half-way, the next
  start continues where it stopped.
- The previous version keeps working on the upgraded database (it ignores the new table and columns), so a
  code-only rollback does not need a restore.
- Start-up with 57,000 observations and 300 geometries took about 4 seconds. The health check allows 30 seconds
  plus three retries.

## 1. Before updating: take a copy you keep outside the server's volume

In the app: **Budget → Back up database now**. Then, on the Ubuntu host:

```bash
docker ps --format '{{.Names}}' | grep 8star-routes            # container name, normally 8star-routes
docker exec 8star-routes ls -l /data/backups                    # newest manual-*.sqlite
docker cp 8star-routes:/data/backups/<manual-file>.sqlite ~/8star-routes-before-update.sqlite
```

Or copy the whole volume (find its name with `docker volume ls | grep routes-data`; it is prefixed with the stack name):

```bash
docker run --rm -v <stack>_routes-data:/data:ro -v "$HOME":/out python:3.12-slim \
  tar czf /out/8star-routes-data-before-update.tgz -C /data .
```

## 2. Optional: dry-run the upgrade on a copy

This builds the new version next to the running one and migrates a **copy** of your live database in a temporary
directory. The live database is mounted read-only and the running app is not affected.

```bash
git clone -b <branch-or-main> https://github.com/TimoM84/8star-routes.git /tmp/8star-routes-check
docker build -t 8star-routes:check /tmp/8star-routes-check
docker run --rm --read-only --tmpfs /tmp -e APP_PASSWORD=x \
  -v <stack>_routes-data:/data:ro 8star-routes:check python scripts/check_upgrade.py
```

The last line must read `RESULT: OK, all existing rows are unchanged.` It lists every table with its row count
before and after and checks every existing row column by column.

## 3. Update in Dockhand

1. Open the 8star-routes stack.
2. If the stack follows a branch, make sure it points at the branch or commit you want (normally `main` after the
   pull request is merged).
3. **Pull/redeploy with rebuild** (Dockhand rebuilds the image from the `Dockerfile` and recreates the container).
   The named volume is kept. Do **not** remove the volume or use any "remove volumes" option.
4. Watch the stack log. Expected lines:

   ```
   routes.db: pre-migration backup written: /data/backups/pre-migration-....sqlite (pending: 003_route_families)
   routes.db: applied migration 003_route_families
   routes: assigned route families to N stored geometries
   routes.scheduler: scheduler started (...)
   ```

5. The container becomes `healthy`. Open the app: the Map shows the latest stored observation, Budget shows the same
   ledger count as before, and **Scheduled collection** says *Running*.

Nothing needs to be configured for this release. Settings stored in the database (budget values, corridors,
tolerances) are kept.

## 4. Rolling back

**A. Code only (recommended, keeps everything collected since the update).** Point the stack back at the previous
commit (`b91b480`) and redeploy with rebuild. The previous version runs on the upgraded database; the new table and
columns are ignored. Updating again later continues normally (families are assigned to any geometry stored in the
meantime).

**B. Restore the pre-migration database (only if the data itself is suspected).** This returns the database to the
exact state before the update; observations collected after the update are dropped from the live database (they
stay in the `pre-restore-*.sqlite` copy the restore script writes first).

```bash
# in the stack directory, or with Dockhand's stack stopped:
docker compose stop
docker compose run --rm --no-deps 8star-routes ls /data/backups              # find pre-migration-<time>.sqlite
docker compose run --rm --no-deps 8star-routes python scripts/restore.py /data/backups/pre-migration-<time>.sqlite
```

Without the compose file at hand, the same with plain Docker (image `8star-routes:local` as built by the stack):

```bash
docker stop 8star-routes
docker run --rm -e APP_PASSWORD=x -v <stack>_routes-data:/data 8star-routes:local ls /data/backups
docker run --rm -e APP_PASSWORD=x -v <stack>_routes-data:/data 8star-routes:local \
  python scripts/restore.py /data/backups/pre-migration-<time>.sqlite
```

Then redeploy the previous commit (`b91b480`) as in A. Starting the new version again would simply run the
migration again.

Both rollbacks were exercised with real containers: the previous image served statistics from the upgraded
database, and the restored database matched the original row for row.
