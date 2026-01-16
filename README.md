# Newsletter Content Extraction Services

Two Flask-based microservices for AI-powered newsletter curation:

1. **Scraper** (port 5001) - Extract article text, metadata, images, and links
2. **Screener** (port 5002) - Generate screenshots with popup handling

## Features

### Scraper
- Text extraction (markdown/plain text)
- Metadata extraction (title, author, date, description)
- Image extraction with content filtering
- External link extraction
- Connection pooling
- Response caching (1 hour)
- Rate limiting

### Screener
- Multiple formats (PNG, JPEG, WebP)
- Quality control
- Automatic thumbnails
- Popup dismissal
- Element hiding
- Ad blocking (optional)
- Custom viewport sizes

## Quick Start

### Using Docker Compose (Recommended)
```bash
# Clone the repository
git clone https://github.com/yourusername/newsletter-services.git
cd newsletter-services

# Start services
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f
```

### Services will be available at:
- Scraper: http://localhost:5001
- Screener: http://localhost:5002

## API Usage

### Scraper - Extract Content
```bash
curl -X POST http://localhost:5001/extract \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com/article",
    "extract_images": true,
    "content_only": true,
    "extract_links": true
  }'
```

### Screener - Take Screenshot
```bash
curl -X POST http://localhost:5002/screenshot \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://example.com",
    "format": "jpeg",
    "quality": 80,
    "hide_elements": true
  }'
```

## Deployment to Coolify

1. Push this repository to GitHub
2. In Coolify, create a new service
3. Select "Docker Compose" as deployment type
4. Point to this repository
5. Configure environment variables from `.env.example`
6. Deploy!

## Configuration

See `.env.example` for available configuration options.

## Health Checks

- Scraper: `GET /stats`
- Screener: `GET /health`

## License

MIT
