# Backup & Restore

LayerNexus stores all its data in two locations that must be backed up:

| Path | Contents |
|---|---|
| `/app/data/db.sqlite3` | SQLite database (projects, parts, users, settings) |
| `/app/media/` | Uploaded files (STL, G-code, images, documents) |

---

## What to Back Up

### Database

The SQLite database file at `/app/data/db.sqlite3` contains all application data:

- Projects, parts, and print jobs
- User accounts and roles
- Printer profiles and cost profiles
- OrcaSlicer profiles
- Print queue entries
- Hardware catalog and project assignments

### Media Files

The `/app/media/` directory contains all user-uploaded files:

- STL files
- G-code files
- Project cover images
- Project documents (PDF, CAD, images)

---

## Simple Backup

### Using Docker

```bash
# Stop the container to ensure database consistency
docker compose stop web

# Back up the database
docker cp $(docker compose ps -q web):/app/data/db.sqlite3 ./backup-db.sqlite3

# Back up media files
docker cp $(docker compose ps -q web):/app/media/ ./backup-media/

# Restart the container
docker compose start web
```

### Using Named Volumes

If you use named volumes (as in the default `docker-compose.yml`):

```bash
# Create a backup directory with timestamp
BACKUP_DIR="backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Back up database
docker run --rm \
  -v layernexus_db_data:/data:ro \
  -v "$(pwd)/$BACKUP_DIR":/backup \
  alpine cp /data/db.sqlite3 /backup/db.sqlite3

# Back up media files
docker run --rm \
  -v layernexus_media_data:/data:ro \
  -v "$(pwd)/$BACKUP_DIR":/backup \
  alpine cp -r /data/ /backup/media/
```

!!! note "Volume Names"
    The actual volume names depend on your project directory name. Use `docker volume ls` to find the correct names. They typically follow the pattern `<directory>_db_data` and `<directory>_media_data`.

---

## Restore

### Restore Database

```bash
# Stop the container
docker compose stop web

# Copy the backup file into the container
docker cp ./backup-db.sqlite3 $(docker compose ps -q web):/app/data/db.sqlite3

# Restart — migrations will apply any schema updates
docker compose start web
```

### Restore Media Files

```bash
docker compose stop web
docker cp ./backup-media/ $(docker compose ps -q web):/app/media/
docker compose start web
```

---

## Automated Backup with Cron

Create a backup script:

```bash
#!/bin/bash
# backup-layernexus.sh

BACKUP_DIR="/path/to/backups"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
COMPOSE_DIR="/path/to/LayerNexus"

cd "$COMPOSE_DIR"

# Create backup directory
mkdir -p "$BACKUP_DIR/$TIMESTAMP"

# Back up database (using sqlite3 .backup for consistency)
docker compose exec -T web sqlite3 /app/data/db.sqlite3 ".backup '/app/data/backup.sqlite3'"
docker cp "$(docker compose ps -q web):/app/data/backup.sqlite3" "$BACKUP_DIR/$TIMESTAMP/db.sqlite3"
docker compose exec -T web rm /app/data/backup.sqlite3

# Back up media
docker cp "$(docker compose ps -q web):/app/media/" "$BACKUP_DIR/$TIMESTAMP/media/"

# Remove backups older than 30 days
find "$BACKUP_DIR" -maxdepth 1 -type d -mtime +30 -exec rm -rf {} \;

echo "Backup completed: $BACKUP_DIR/$TIMESTAMP"
```

Add to crontab:

```bash
# Run daily at 2 AM
0 2 * * * /path/to/backup-layernexus.sh >> /var/log/layernexus-backup.log 2>&1
```

---

## SQLite Notes

!!! info "WAL Mode"
    SQLite may use Write-Ahead Logging (WAL) mode, which creates additional files (`db.sqlite3-wal` and `db.sqlite3-shm`) alongside the main database. For a consistent backup:

    - **Preferred:** Use the `sqlite3 .backup` command (as shown in the automated backup script) which creates a clean copy.
    - **Alternative:** Stop the container before copying the database file.
    - **Avoid:** Copying `db.sqlite3` while the application is actively writing — the backup may be corrupted.

---

## Next Steps

- [Docker image details](docker.md)
- [Docker Compose examples](docker-compose.md)
