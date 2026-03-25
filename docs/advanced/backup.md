# Backup & Restore

LayerNexus stores everything in two places. Back up both and you can restore your entire setup at any time.

| What | Container Path | Contains |
|---|---|---|
| **Database** | `/app/data/db.sqlite3` | All your projects, parts, users, settings, print jobs |
| **Uploaded Files** | `/app/media/` | STL files, G-code, images, documents |

---

## Backup

### Quick Backup

The simplest approach — stop the container, copy the data, start it again:

```bash
# Stop LayerNexus
docker compose stop web

# Copy the database
docker cp $(docker compose ps -q web):/app/data/db.sqlite3 ./backup-db.sqlite3

# Copy uploaded files
docker cp $(docker compose ps -q web):/app/media/ ./backup-media/

# Start LayerNexus again
docker compose start web
```

### Backup from Named Volumes

If you use named volumes (like in the [Quick Start](../quick-start.md)):

```bash
# Create a backup folder with today's date
BACKUP_DIR="backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Back up database
docker run --rm \
  -v layernexus_layernexus_data:/data:ro \
  -v "$(pwd)/$BACKUP_DIR":/backup \
  alpine cp /data/db.sqlite3 /backup/db.sqlite3

# Back up uploaded files
docker run --rm \
  -v layernexus_layernexus_media:/data:ro \
  -v "$(pwd)/$BACKUP_DIR":/backup \
  alpine cp -r /data/ /backup/media/
```

!!! note "Finding Volume Names"
    The actual volume names depend on your folder name. Run `docker volume ls` to see them. They typically look like `foldername_layernexus_data` and `foldername_layernexus_media`.

---

## Restore

### Restore Database

```bash
docker compose stop web
docker cp ./backup-db.sqlite3 $(docker compose ps -q web):/app/data/db.sqlite3
docker compose start web
```

The database schema will be updated automatically on restart if needed.

### Restore Uploaded Files

```bash
docker compose stop web
docker cp ./backup-media/ $(docker compose ps -q web):/app/media/
docker compose start web
```

---

## Tips

!!! tip "Automated Backups"
    For automated daily backups, you can create a small script that runs the backup commands above and schedule it with `cron`. Add a `find` command to clean up backups older than 30 days:

    ```bash
    find /path/to/backups -maxdepth 1 -type d -mtime +30 -exec rm -rf {} \;
    ```

!!! info "SQLite and Live Copies"
    If you copy the database file while LayerNexus is running, the copy might be incomplete. For a safe backup without stopping the container, use SQLite's built-in backup command:

    ```bash
    docker compose exec -T web sqlite3 /app/data/db.sqlite3 ".backup '/app/data/backup.sqlite3'"
    docker cp $(docker compose ps -q web):/app/data/backup.sqlite3 ./backup-db.sqlite3
    docker compose exec -T web rm /app/data/backup.sqlite3
    ```

---

## Next Steps

- [Docker Details](docker.md)
- [HTTPS & Reverse Proxy](reverse-proxy.md)
