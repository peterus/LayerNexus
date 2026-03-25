# Reverse Proxy / HTTPS

In production, you should place LayerNexus behind a reverse proxy to provide HTTPS termination, better security, and proper hostname handling.

## Why a Reverse Proxy?

- **HTTPS/TLS** — Encrypt traffic between users and the server
- **Domain routing** — Serve LayerNexus on a custom domain
- **Rate limiting** — Protect against abuse
- **Static file caching** — Improve performance (though WhiteNoise handles this well)

!!! important "Required Environment Variables"
    When behind a reverse proxy, you **must** set:

    - `ALLOWED_HOSTS` — include your domain (e.g., `layernexus.example.com`)
    - `CSRF_TRUSTED_ORIGINS` — include the full origin URL (e.g., `https://layernexus.example.com`)

---

## Nginx

### Basic Configuration

```nginx
upstream layernexus {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name layernexus.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name layernexus.example.com;

    ssl_certificate     /etc/letsencrypt/live/layernexus.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/layernexus.example.com/privkey.pem;

    client_max_body_size 100M;

    location / {
        proxy_pass http://layernexus;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

!!! tip "File Upload Size"
    LayerNexus allows file uploads up to 75 MB (project documents). Set `client_max_body_size` to at least `100M` to allow headroom for STL and G-code files.

### Nginx in Docker Compose

```yaml
services:
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
    depends_on:
      - web
    restart: unless-stopped

  web:
    image: ghcr.io/peterus/layernexus:latest
    expose:
      - "8000"
    # ... rest of web config
```

---

## Traefik

### Docker Labels Configuration

If you use Traefik as your reverse proxy, add these labels to the `web` service in your `docker-compose.yml`:

```yaml
services:
  web:
    image: ghcr.io/peterus/layernexus:latest
    expose:
      - "8000"
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.layernexus.rule=Host(`layernexus.example.com`)"
      - "traefik.http.routers.layernexus.entrypoints=websecure"
      - "traefik.http.routers.layernexus.tls.certresolver=letsencrypt"
      - "traefik.http.services.layernexus.loadbalancer.server.port=8000"
    volumes:
      - db_data:/app/data
      - media_data:/app/media
    environment:
      - DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY}
      - ALLOWED_HOSTS=layernexus.example.com
      - CSRF_TRUSTED_ORIGINS=https://layernexus.example.com
      - DEBUG=0
    restart: unless-stopped
    # ... rest of config
```

!!! note
    Make sure your Traefik instance is on the same Docker network as the LayerNexus service. You may need to add a `networks` section to your configuration.

---

## Checklist

Before deploying behind a reverse proxy, verify:

- [ ] `DJANGO_SECRET_KEY` is set to a unique, secure value
- [ ] `DEBUG=0`
- [ ] `ALLOWED_HOSTS` includes your domain
- [ ] `CSRF_TRUSTED_ORIGINS` includes the full origin (e.g., `https://layernexus.example.com`)
- [ ] Reverse proxy forwards `X-Forwarded-Proto` and `X-Forwarded-For` headers
- [ ] Upload size limit is set high enough (`100M+`)
- [ ] HTTPS is configured with a valid certificate

---

## Next Steps

- [Backup & restore procedures](backup.md)
- [Docker Compose examples](docker-compose.md)
