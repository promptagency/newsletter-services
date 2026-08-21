# Newsletter Content Extraction Services

[![License: MIT](https://img.shields.io/github/license/promptagency/newsletter-services)](LICENSE)
[![Top language](https://img.shields.io/github/languages/top/promptagency/newsletter-services)](https://github.com/promptagency/newsletter-services)

Two Flask-based microservices that power AI-driven newsletter curation:

| Service      | Port | Purpose                                                       |
|--------------|------|--------------------------------------------------------------|
| **Scraper**  | 5001 | Extract article text, metadata, images, and external links   |
| **Screener** | 5002 | Capture screenshots with popup dismissal and element hiding  |

Both services ship as self-contained Docker containers and run together with
plain Docker Compose — no reverse proxy or orchestrator required. They can
optionally be deployed via [Coolify](https://coolify.io/), but that is just one
hosting option, not a dependency.

## Features

### Scraper
- Text extraction as Markdown or plain text (via [trafilatura](https://trafilatura.readthedocs.io/))
- Metadata extraction (title, author, date, description, sitename)
- Image extraction with content-area filtering and size thresholds
- External link extraction (skips navigation/footer/sidebar in `content_only` mode)
- Connection pooling, in-memory response caching (default 1 hour), and per-IP rate limiting

### Screener
- Screenshots via headless Chromium ([Puppeteer](https://pptr.dev/))
- Multiple output formats (PNG, JPEG, WebP) with quality control
- Automatic thumbnail generation
- Popup / cookie-banner dismissal and distracting-element hiding
- Optional ad blocking and custom viewport sizes
- In-memory caching and automatic cleanup of old screenshots

## Quick Start

### Using Docker Compose (recommended)
```bash
# Clone the repository
git clone https://github.com/promptagency/newsletter-services.git
cd newsletter-services

# (Optional) create a .env file to override defaults
cp .env.example .env

# Build and start both services
docker compose up -d

# Check status
docker compose ps

# Follow logs
docker compose logs -f
```

Services will be available at:
- Scraper: <http://localhost:5001>
- Screener: <http://localhost:5002>

## Authentication

Set an `API_KEY` (see [`.env.example`](.env.example)) to require an
`X-API-Key` header on every endpoint **except** the health checks
(`/stats` and `/health`, so container healthchecks keep working). If `API_KEY`
is left empty, authentication is disabled and the service logs a warning at
startup — do not run publicly without a key.

```bash
# Generate a strong key
openssl rand -hex 32
```

Requests then include the header:
```bash
-H "X-API-Key: <your-key>"
```

Requests with a missing or invalid key receive `401 Unauthorized`.

## API Usage

### Scraper — `POST /extract`

Request:
```bash
curl -X POST http://localhost:5001/extract \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "url": "https://example.com/article",
    "extract_images": true,
    "content_only": true,
    "extract_links": true
  }'
```

Response:
```json
{
  "success": true,
  "url": "https://example.com/article",
  "content_type": "markdown",
  "content": "# Article title\n\nArticle body rendered as Markdown...",
  "content_length": 4213,
  "extracted_at": "2026-08-21T10:15:30.123456",
  "from_cache": false,
  "metadata": {
    "title": "Article title",
    "author": "Jane Doe",
    "date": "2026-08-20",
    "description": "A short summary of the article",
    "sitename": "Example News",
    "categories": [],
    "tags": []
  },
  "images": {
    "total": 2,
    "urls": [
      { "url": "https://example.com/og.jpg", "alt": "Open Graph image", "type": "og_image" },
      { "url": "https://example.com/photo.jpg", "alt": "A photo", "title": "", "width": 800, "height": 600 }
    ]
  },
  "external_links": {
    "total": 1,
    "urls": [
      { "url": "https://other.com/page", "text": "Related read", "title": "", "domain": "other.com", "nofollow": false }
    ]
  }
}
```

**Request fields:** `url` (required), `extract_images` (default `true`), `content_only`
(default `false`), `filter_images` (default `true`), `min_image_width` / `min_image_height`
(default `200`), `extract_links` (default `false`).

### Screener — `POST /screenshot`

Request:
```bash
curl -X POST http://localhost:5002/screenshot \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{
    "url": "https://example.com",
    "format": "jpeg",
    "quality": 80,
    "hide_elements": true
  }'
```

Response:
```json
{
  "success": true,
  "url": "https://example.com",
  "format": "jpeg",
  "quality": 80,
  "width": 1200,
  "height": 800,
  "full_page": false,
  "timestamp": "20260821_101530",
  "files": {
    "png": "/tmp/screener_screenshots/screener_abc12345_20260821_101530.png",
    "jpeg": "/tmp/screener_screenshots/screener_abc12345_20260821_101530.jpeg",
    "thumbnail": "/tmp/screener_screenshots/screener_abc12345_20260821_101530_thumb.jpg"
  },
  "file_sizes": { "png": 245678, "jpeg": 45231, "thumbnail": 8123 },
  "primary_file": "/tmp/screener_screenshots/screener_abc12345_20260821_101530.jpeg",
  "thumbnail_created": true,
  "from_cache": false
}
```

Retrieve a generated file with `GET /screenshot/<filename>`.

**Request fields:** `url` (required), `width` (default `1200`), `height` (default `800`),
`full_page` (default `false`), `format` (`png` | `jpeg` | `jpg` | `webp`, default `png`),
`quality` (1–100, default `85`), `thumbnail` (default `true`), `hide_elements`
(default `true`), `block_ads` (default `false`).

## Configuration

All configuration is via environment variables — see [`.env.example`](.env.example)
for the full list (ports, cache durations, rate limit, cleanup interval).

## Health Checks

| Service  | Endpoint          | Extra endpoints                          |
|----------|-------------------|------------------------------------------|
| Scraper  | `GET /stats`      | `GET /cache/status`                      |
| Screener | `GET /health`     | `GET /status`, `GET /screenshot/<file>`  |

The Docker Compose healthchecks poll `/stats` and `/health` respectively.

## Deployment

The services run anywhere Docker Compose is available — a plain
`docker compose up -d` on any host is all that's required. Set `API_KEY`
(see [Authentication](#authentication)) before exposing them.

### Optional: Coolify

1. Push this repository to GitHub.
2. In Coolify, create a new **Docker Compose** resource pointing at this repo.
3. Coolify reads [`.coolify`](.coolify) and uses `docker-compose.yaml` as the build pack.
4. Set any environment variable overrides from `.env.example` in the Coolify UI.
5. Deploy.

## Verification

Run the full end-to-end check against a real Docker build:

```bash
./scripts/verify.sh
```

It builds both images, boots the stack with a throwaway `API_KEY`, and asserts
auth, SSRF blocking, a real screenshot (container Chromium), and the
redirect-SSRF guard — then tears everything down and prints a pass/fail tally.
Requires a running Docker daemon with the compose plugin.

The security-relevant behaviour it confirms:

| Case                                             | Scraper `/extract` | Screener `/screenshot` |
|--------------------------------------------------|:------------------:|:----------------------:|
| Health endpoint without key (`/stats`, `/health`)| `200`              | `200`                  |
| Request without API key                          | `401`              | `401`                  |
| Request with wrong API key                       | `401`              | `401`                  |
| SSRF — cloud metadata IP (`169.254.169.254`)     | `400`              | `400`                  |
| SSRF — loopback / `127.0.0.1`                    | `400`              | `400`                  |
| SSRF — private range (`10.0.0.0/8`)              | `400`              | `400`                  |
| SSRF — `file://` scheme                          | `400`              | `400`                  |
| Malformed / empty JSON body                      | `400`              | `400`                  |
| Path traversal on file serving                   | n/a                | `404`                  |
| Valid request with correct key                   | `200` (markdown)   | `200` (PNG/JPEG)       |
| Redirect (public URL) → internal IP              | blocked per hop    | blocked in-browser     |

The scraper extracts a live URL correctly (trafilatura 2.2.0), the screener
produces a real screenshot via the container's bundled Chromium, and a public
URL that 302-redirects to an internal address is blocked — in the scraper by
re-validating every hop, in the screener by aborting the navigation in Chromium.

## License

[MIT](LICENSE) © Prompt Agency AB
