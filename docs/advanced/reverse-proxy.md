# HTTPS & Reverse Proxy

A reverse proxy sits between your users and LayerNexus. You'd want one if you need:

- **HTTPS** — Encrypt traffic so passwords and data are safe
- **Custom domain** — Access LayerNexus at `https://prints.example.com` instead of `http://192.168.1.50:8000`
- **Remote access** — Reach LayerNexus from outside your home network

If you're only using LayerNexus on your local network, you can skip this.

---

## Required Settings

When running behind a reverse proxy, you **must** set these environment variables:

```bash
ALLOWED_HOSTS=layernexus.example.com
CSRF_TRUSTED_ORIGINS=https://layernexus.example.com
```

See [Configuration](../configuration.md) for details.

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
    LayerNexus allows uploads up to 75 MB. Set `client_max_body_size` to at least `100M` to give some headroom for large STL and G-code files.

### Nginx in Docker Compose

You can add Nginx to your `docker-compose.yml`:

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

If you use [Traefik](https://traefik.io/) as your reverse proxy, add these labels to the `web` service:

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
      - layernexus_data:/app/data
      - layernexus_media:/app/media
    environment:
      - DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY}
      - ALLOWED_HOSTS=layernexus.example.com
      - CSRF_TRUSTED_ORIGINS=https://layernexus.example.com
      - DEBUG=0
    restart: unless-stopped
    # ... rest of config
```

!!! note
    Make sure your Traefik instance is on the same Docker network as the LayerNexus service.

---

## Checklist

Before going live, verify:

- [ ] `DJANGO_SECRET_KEY` is set to a unique, random value
- [ ] `DEBUG=0`
- [ ] `ALLOWED_HOSTS` includes your domain
- [ ] `CSRF_TRUSTED_ORIGINS` includes the full URL with `https://`
- [ ] Reverse proxy forwards `X-Forwarded-Proto` and `X-Forwarded-For` headers
- [ ] Upload size limit is at least `100M`
- [ ] HTTPS is set up with a valid certificate

---

## Next Steps

- [Backup & Restore](backup.md)
- [Docker Details](docker.md)
