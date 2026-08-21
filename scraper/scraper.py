from flask import Flask, request, jsonify
from trafilatura import extract, extract_metadata
from trafilatura.settings import use_config
import logging
import time
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os
import socket
import ipaddress
import hmac

# Configuration from environment variables
FLASK_PORT = int(os.getenv('SCRAPER_PORT', '5001'))
CACHE_DURATION_MINUTES = int(os.getenv('SCRAPER_CACHE_DURATION', '60'))
RATE_LIMIT_SECONDS = int(os.getenv('SCRAPER_RATE_LIMIT', '2'))
LOG_FILE = os.getenv('SCRAPER_LOG_FILE', '/tmp/scraper.log')
MAX_CONTENT_BYTES = int(os.getenv('SCRAPER_MAX_CONTENT_BYTES', str(10 * 1024 * 1024)))  # 10 MB
MAX_REDIRECTS = int(os.getenv('SCRAPER_MAX_REDIRECTS', '5'))
API_KEY = os.getenv('API_KEY', '').strip()

# Configure logging to both file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

if not API_KEY:
    logger.warning("API_KEY not set — request authentication is DISABLED. "
                   "Set the API_KEY environment variable to require an X-API-Key header.")

# Configure requests session with connection pooling
session = requests.Session()

# Configure connection pool
adapter = HTTPAdapter(
    pool_connections=10,
    pool_maxsize=20,
    max_retries=0,
    pool_block=False
)

# Mount adapter for both HTTP and HTTPS
session.mount('http://', adapter)
session.mount('https://', adapter)

# Set default headers for the session
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
})

# Configure trafilatura with better user agent and timeout
config = use_config()
config.set('DEFAULT', 'USER_AGENT', 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36')
config.set('DEFAULT', 'DOWNLOAD_TIMEOUT', '30')
config.set('DEFAULT', 'MIN_EXTRACTED_SIZE', '25')

app = Flask(__name__)

# Endpoints that stay open so container healthchecks work without a key
PUBLIC_PATHS = {'/stats'}

@app.before_request
def require_api_key():
    """Require a valid X-API-Key header when API_KEY is configured."""
    if not API_KEY:
        return  # authentication disabled
    if request.path in PUBLIC_PATHS:
        return
    provided = request.headers.get('X-API-Key', '')
    if not (provided and hmac.compare_digest(provided, API_KEY)):
        return jsonify(create_error_response("unauthorized", "Missing or invalid API key")), 401

# Simple rate limiting
last_request_times = {}

# Simple in-memory cache for responses
response_cache = {}

# Performance tracking
request_stats = {
    'total_requests': 0,
    'cache_hits': 0,
    'successful_fetches': 0,
    'failed_fetches': 0,
    'start_time': datetime.now()
}

# Capture the real resolver before installing the SSRF guard below.
_real_getaddrinfo = socket.getaddrinfo

def _ip_is_public(ip_str):
    """True only for globally routable addresses (blocks private/loopback/etc.)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_reserved or ip.is_multicast or ip.is_unspecified)

def is_safe_url(url):
    """SSRF guard: allow only http/https URLs that resolve to public IP addresses."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Could not parse URL"

    if parsed.scheme not in ('http', 'https'):
        return False, f"Unsupported scheme '{parsed.scheme}' (only http/https allowed)"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL is missing a hostname"

    try:
        addr_info = _real_getaddrinfo(hostname, None)
    except Exception:
        return False, "Hostname could not be resolved"

    for info in addr_info:
        if not _ip_is_public(info[4][0]):
            return False, "URL resolves to a non-public address"

    return True, "URL is safe"

def _guarded_getaddrinfo(host, *args, **kwargs):
    """Drop any non-public results so a socket can never be opened to an internal
    address — even if DNS changes between validation and connect. This closes the
    DNS-rebinding / TOCTOU gap that is_safe_url() alone leaves open, because this is
    the same resolution requests/urllib3 use to establish the connection.
    """
    results = _real_getaddrinfo(host, *args, **kwargs)
    safe = [r for r in results if _ip_is_public(r[4][0])]
    if not safe:
        raise socket.gaierror(f"blocked resolution to non-public address for host {host!r}")
    return safe

# Install process-wide. The scraper's only outbound traffic is fetching
# user-supplied URLs, so a blanket public-only resolution policy is safe here.
socket.getaddrinfo = _guarded_getaddrinfo

def fetch_url_with_session(url, timeout=30):
    """Fetch URL using the pooled session with SSRF-safe redirect handling and a size cap.

    Redirects are followed manually so each hop is re-validated against is_safe_url;
    this prevents a public URL from bouncing the fetch to an internal target.
    """
    current_url = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            is_safe, reason = is_safe_url(current_url)
            if not is_safe:
                logger.warning(f"Blocked unsafe fetch target {current_url}: {reason}")
                request_stats['failed_fetches'] += 1
                return None

            response = session.get(current_url, timeout=timeout,
                                   allow_redirects=False, stream=True)

            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get('Location')
                response.close()
                if not location:
                    break
                current_url = urljoin(current_url, location)
                continue

            response.raise_for_status()

            # Reject oversized responses up front when the server declares a length
            declared = response.headers.get('Content-Length')
            if declared and declared.isdigit() and int(declared) > MAX_CONTENT_BYTES:
                logger.warning(f"Response too large ({declared} bytes): {current_url}")
                response.close()
                request_stats['failed_fetches'] += 1
                return None

            # Stream with a hard cap in case Content-Length is missing or understated
            chunks = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > MAX_CONTENT_BYTES:
                    logger.warning(f"Response exceeded size cap while streaming: {current_url}")
                    response.close()
                    request_stats['failed_fetches'] += 1
                    return None
                chunks.append(chunk)

            encoding = response.encoding or 'utf-8'
            response.close()
            request_stats['successful_fetches'] += 1
            return b''.join(chunks).decode(encoding, errors='replace')

        logger.error(f"Too many redirects fetching URL: {url}")
        request_stats['failed_fetches'] += 1
        return None
    except requests.exceptions.Timeout:
        logger.error(f"Timeout fetching URL: {url}")
        request_stats['failed_fetches'] += 1
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error fetching URL {url}: {e}")
        request_stats['failed_fetches'] += 1
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching URL {url}: {e}")
        request_stats['failed_fetches'] += 1
        return None

def get_cache_key(url):
    """Generate a cache key for the URL"""
    return hashlib.md5(url.encode()).hexdigest()

def get_cached_response(url):
    """Check if we have a cached response for this URL"""
    cache_key = get_cache_key(url)
    if cache_key in response_cache:
        cached_item = response_cache[cache_key]
        if datetime.now() < cached_item['expires']:
            request_stats['cache_hits'] += 1
            return cached_item['data']
        else:
            del response_cache[cache_key]
    return None

def cache_response(url, response_data):
    """Cache a response for future use"""
    cache_key = get_cache_key(url)
    response_cache[cache_key] = {
        'data': response_data,
        'expires': datetime.now() + timedelta(minutes=CACHE_DURATION_MINUTES),
        'cached_at': datetime.now()
    }

def extract_external_links(html, base_url, content_only=False):
    """Extract external URLs from HTML content"""
    try:
        soup = BeautifulSoup(html, 'html.parser')
        links = []
        seen_urls = set()
        
        # Get the base domain to identify external links
        base_domain = urlparse(base_url).netloc
        
        # If content_only, try to find the main article container first
        if content_only:
            content_selectors = [
                'article',
                '[role="main"]',
                'main',
                '.article-content',
                '.post-content',
                '.entry-content',
                '.article-body',
                '.story-body',
                '#article-body',
                '.content',
                '#content'
            ]
            
            content_container = None
            for selector in content_selectors:
                content_container = soup.select_one(selector)
                if content_container:
                    logger.info(f"Found content container for links using selector: {selector}")
                    soup = content_container
                    break
            
            if not content_container:
                logger.info("No specific content container found for links, searching entire page")
        
        # Find all anchor tags
        for link in soup.find_all('a', href=True):
            href = link.get('href')
            
            if not href:
                continue
            
            # Convert relative URLs to absolute
            full_url = urljoin(base_url, href)
            
            # Parse the URL
            parsed_url = urlparse(full_url)
            link_domain = parsed_url.netloc
            
            # Skip if not a valid http/https URL
            if parsed_url.scheme not in ['http', 'https']:
                continue
            
            # Skip if it's an internal link (same domain)
            if link_domain == base_domain:
                continue
            
            # Skip if already seen
            if full_url in seen_urls:
                continue
            
            # Skip common non-content URLs
            skip_patterns = ['javascript:', 'mailto:', 'tel:', '#']
            if any(pattern in href.lower() for pattern in skip_patterns):
                continue
            
            # If content_only, skip links in navigation/footer/sidebar
            if content_only:
                parent_classes = ' '.join(link.parent.get('class', [])).lower() if link.parent else ''
                parent_id = link.parent.get('id', '').lower() if link.parent else ''
                
                skip_patterns = ['nav', 'sidebar', 'footer', 'header', 'menu', 'widget',
                               'related', 'advertisement', 'social', 'share', 'comment']
                if any(pattern in parent_classes or pattern in parent_id for pattern in skip_patterns):
                    continue
            
            seen_urls.add(full_url)
            
            # Get link metadata
            link_data = {
                'url': full_url,
                'text': link.get_text(strip=True),
                'title': link.get('title', ''),
                'domain': link_domain
            }
            
            # Check if it's a nofollow link
            rel = link.get('rel', [])
            if isinstance(rel, list):
                link_data['nofollow'] = 'nofollow' in rel
            else:
                link_data['nofollow'] = 'nofollow' in str(rel)
            
            links.append(link_data)
        
        logger.info(f"Extracted {len(links)} external links (content_only={content_only})")
        return links
        
    except Exception as e:
        logger.warning(f"External link extraction failed: {e}")
        return []

def extract_images(html, base_url, content_only=False):
    """Extract image URLs from HTML content"""
    try:
        soup = BeautifulSoup(html, 'html.parser')
        images = []
        seen_urls = set()
        
        # If content_only, try to find the main article container first
        if content_only:
            # Common article/content container selectors
            content_selectors = [
                'article',
                '[role="main"]',
                'main',
                '.article-content',
                '.post-content',
                '.entry-content',
                '.article-body',
                '.story-body',
                '#article-body',
                '.content',
                '#content'
            ]
            
            content_container = None
            for selector in content_selectors:
                content_container = soup.select_one(selector)
                if content_container:
                    logger.info(f"Found content container using selector: {selector}")
                    soup = content_container
                    break
            
            if not content_container:
                logger.info("No specific content container found, searching entire page")
        
        # Find all img tags (now potentially limited to content area)
        for img in soup.find_all('img'):
            img_data = {}
            
            # Get src or data-src (lazy loading)
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
            
            if not src:
                continue
            
            # Convert relative URLs to absolute
            img_url = urljoin(base_url, src)
            
            # Skip data URIs, tracking pixels, and duplicates
            if (img_url.startswith('data:') or 
                img_url in seen_urls or
                any(x in img_url.lower() for x in ['tracking', 'pixel', 'beacon', '1x1'])):
                continue
            
            # If content_only, skip images in obvious navigation/sidebar/footer areas
            if content_only:
                parent_classes = ' '.join(img.parent.get('class', [])).lower() if img.parent else ''
                parent_id = img.parent.get('id', '').lower() if img.parent else ''
                
                # Skip if parent contains these patterns
                skip_patterns = ['nav', 'sidebar', 'footer', 'header', 'menu', 'widget', 
                               'related', 'recommend', 'advertisement', 'social', 'share']
                if any(pattern in parent_classes or pattern in parent_id for pattern in skip_patterns):
                    continue
            
            seen_urls.add(img_url)
            
            # Get image metadata
            img_data['url'] = img_url
            img_data['alt'] = img.get('alt', '')
            img_data['title'] = img.get('title', '')
            
            # Try to get dimensions
            width = img.get('width')
            height = img.get('height')
            if width and height:
                try:
                    img_data['width'] = int(width)
                    img_data['height'] = int(height)
                except (ValueError, TypeError):
                    pass
            
            # Get srcset for responsive images
            srcset = img.get('srcset')
            if srcset:
                img_data['srcset'] = srcset
            
            images.append(img_data)
        
        # Always check for Open Graph image (usually the featured image)
        og_image = soup.find('meta', property='og:image')
        if not og_image:
            # Look in the full document if we were searching in a container
            og_image = BeautifulSoup(html, 'html.parser').find('meta', property='og:image')
        
        if og_image and og_image.get('content'):
            og_url = urljoin(base_url, og_image['content'])
            if og_url not in seen_urls:
                images.insert(0, {
                    'url': og_url,
                    'alt': 'Open Graph image',
                    'type': 'og_image'
                })
        
        # Check for Twitter card image
        twitter_image = soup.find('meta', attrs={'name': 'twitter:image'})
        if not twitter_image:
            twitter_image = BeautifulSoup(html, 'html.parser').find('meta', attrs={'name': 'twitter:image'})
            
        if twitter_image and twitter_image.get('content'):
            twitter_url = urljoin(base_url, twitter_image['content'])
            if twitter_url not in seen_urls:
                images.insert(0, {
                    'url': twitter_url,
                    'alt': 'Twitter card image',
                    'type': 'twitter_image'
                })
        
        logger.info(f"Extracted {len(images)} images (content_only={content_only})")
        return images
        
    except Exception as e:
        logger.warning(f"Image extraction failed: {e}")
        return []

def filter_images(images, min_width=200, min_height=200, exclude_patterns=None):
    """Filter images based on size and patterns"""
    if not images:
        return []
    
    if exclude_patterns is None:
        exclude_patterns = ['logo', 'icon', 'avatar', 'sprite', 'button']
    
    filtered = []
    for img in images:
        # Check dimensions if available
        if 'width' in img and 'height' in img:
            if img['width'] < min_width or img['height'] < min_height:
                continue
        
        # Check for excluded patterns in URL
        img_url_lower = img['url'].lower()
        if any(pattern in img_url_lower for pattern in exclude_patterns):
            continue
        
        filtered.append(img)
    
    return filtered

def create_success_response(url, content, content_type, metadata=None, images=None, external_links=None, from_cache=False):
    """Create standardized success response"""
    response_data = {
        'success': True,
        'url': url,
        'content_type': content_type,
        'content': content,
        'content_length': len(content),
        'extracted_at': datetime.now().isoformat(),
        'from_cache': from_cache
    }
    
    if metadata:
        response_data['metadata'] = {
            'title': metadata.title or '',
            'author': metadata.author or '',
            'date': str(metadata.date) if metadata.date else '',
            'description': metadata.description or '',
            'sitename': metadata.sitename or '',
            'categories': metadata.categories or [],
            'tags': metadata.tags or []
        }
    
    if images is not None:
        response_data['images'] = {
            'total': len(images),
            'urls': images
        }
    
    if external_links is not None:
        response_data['external_links'] = {
            'total': len(external_links),
            'urls': external_links
        }
    
    return response_data

def create_error_response(error_type, message, url=None):
    """Create standardized error response"""
    return {
        'success': False,
        'error_type': error_type,
        'message': message,
        'url': url,
        'timestamp': datetime.now().isoformat()
    }

def validate_url_detailed(url):
    """Detailed URL validation with specific error messages"""
    if not url or not url.strip():
        return False, "URL is empty or missing"
    
    try:
        parsed = urlparse(url)
        
        if not parsed.scheme:
            return False, "URL missing protocol (http:// or https://)"
        
        if parsed.scheme not in ['http', 'https']:
            return False, f"Unsupported protocol: {parsed.scheme}. Use http:// or https://"
        
        if not parsed.netloc:
            return False, "URL missing domain name"
        
        if parsed.netloc.startswith('.') or parsed.netloc.endswith('.'):
            return False, "Invalid domain format"
            
        return True, "URL is valid"
        
    except Exception as e:
        return False, f"URL parsing error: {str(e)}"

def sanitize_url(url):
    """Clean and validate URL input"""
    if not url:
        return None
    
    url = url.strip()
    
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    if not url.replace('https://', '').replace('http://', '').strip():
        return None
        
    return url

def rate_limit_check(client_ip):
    """Simple rate limiting for responsible scraping"""
    current_time = time.time()
    # Bound memory: drop entries older than an hour once the map grows large
    if len(last_request_times) > 10000:
        stale = [ip for ip, ts in last_request_times.items() if current_time - ts > 3600]
        for ip in stale:
            del last_request_times[ip]
    if client_ip in last_request_times:
        time_since_last = current_time - last_request_times[client_ip]
        if time_since_last < RATE_LIMIT_SECONDS:
            return False
    last_request_times[client_ip] = current_time
    return True

@app.route('/cache/status', methods=['GET'])
def cache_status():
    """Check cache status"""
    active_entries = len(response_cache)
    cache_hit_rate = (request_stats['cache_hits'] / request_stats['total_requests'] * 100) if request_stats['total_requests'] > 0 else 0
    
    return jsonify({
        'cache_entries': active_entries,
        'cache_duration_minutes': CACHE_DURATION_MINUTES,
        'cache_hit_rate': round(cache_hit_rate, 2)
    })

@app.route('/stats', methods=['GET'])
def get_stats():
    """Get scraper performance statistics"""
    uptime = datetime.now() - request_stats['start_time']
    total_requests = request_stats['total_requests']
    
    success_rate = (request_stats['successful_fetches'] / total_requests * 100) if total_requests > 0 else 0
    cache_hit_rate = (request_stats['cache_hits'] / total_requests * 100) if total_requests > 0 else 0
    
    return jsonify({
        'uptime_seconds': int(uptime.total_seconds()),
        'uptime_formatted': str(uptime).split('.')[0],
        'total_requests': total_requests,
        'cache_hits': request_stats['cache_hits'],
        'cache_hit_rate': round(cache_hit_rate, 2),
        'successful_fetches': request_stats['successful_fetches'],
        'failed_fetches': request_stats['failed_fetches'],
        'success_rate': round(success_rate, 2),
        'cached_entries': len(response_cache)
    })

@app.route('/extract', methods=['POST'])
def extract_article():
    request_stats['total_requests'] += 1
    
    try:
        # Rate limiting (X-Forwarded-For is client-controlled; use the leftmost hop)
        forwarded = request.headers.get('X-Forwarded-For', '')
        client_ip = forwarded.split(',')[0].strip() if forwarded else request.remote_addr
        if not rate_limit_check(client_ip):
            error_response = create_error_response("rate_limit", f"Rate limit exceeded. Please wait {RATE_LIMIT_SECONDS} seconds between requests.", None)
            logger.warning(f"Rate limit exceeded for IP: {client_ip}")
            return jsonify(error_response), 429
        
        # Get and validate input (silent=True -> malformed/empty body yields a clean 400)
        data = request.get_json(silent=True)
        if not data:
            error_response = create_error_response("invalid_request", "Request must contain valid JSON", None)
            return jsonify(error_response), 400
        
        raw_url = data.get('url')
        
        if not raw_url:
            error_response = create_error_response("missing_url", "Missing 'url' field in request", None)
            return jsonify(error_response), 400
        
        # Image extraction options
        extract_images_flag = data.get('extract_images', True)
        content_only = data.get('content_only', False)
        filter_images_flag = data.get('filter_images', True)
        min_width = data.get('min_image_width', 200)
        min_height = data.get('min_image_height', 200)
        
        # External links extraction options
        extract_links_flag = data.get('extract_links', False)
        
        # Sanitize and validate URL
        url = sanitize_url(raw_url)
        if not url:
            error_response = create_error_response("invalid_input", "Invalid URL format", raw_url)
            logger.warning(f"Invalid URL after sanitization: {raw_url}")
            return jsonify(error_response), 400
        
        # Detailed URL validation
        is_valid, validation_message = validate_url_detailed(url)
        if not is_valid:
            error_response = create_error_response("invalid_url", validation_message, url)
            logger.warning(f"URL validation failed: {url} - {validation_message}")
            return jsonify(error_response), 400

        # SSRF protection: reject targets that resolve to non-public addresses
        is_safe, safe_reason = is_safe_url(url)
        if not is_safe:
            error_response = create_error_response("blocked_url", safe_reason, url)
            logger.warning(f"Blocked potential SSRF: {url} - {safe_reason}")
            return jsonify(error_response), 400

        if url != raw_url.strip():
            logger.info(f"URL sanitized: '{raw_url}' -> '{url}'")
        
        # Check cache
        cached_response = get_cached_response(url)
        if cached_response:
            logger.info(f"Returning cached response for: {url}")
            cached_response['from_cache'] = True
            return jsonify(cached_response)
            
        logger.info(f"Fetching URL: {url}")
        
        # Fetch HTML using connection pooling session
        try:
            html = fetch_url_with_session(url, timeout=30)
        except Exception as e:
            error_response = create_error_response("fetch_error", f"Failed to fetch URL: {str(e)}", url)
            logger.error(f"Failed to fetch URL {url}: {str(e)}")
            return jsonify(error_response), 500
        
        if not html:
            error_response = create_error_response("no_content", "No HTML content retrieved from URL", url)
            logger.warning(f"No HTML content retrieved for URL: {url}")
            return jsonify(error_response), 404
            
        logger.info(f"HTML length: {len(html)} characters")
        
        # Extract images if requested
        images = []
        if extract_images_flag:
            images = extract_images(html, url, content_only)
            if filter_images_flag and images:
                images = filter_images(images, min_width, min_height)
                logger.info(f"Filtered to {len(images)} images")
        
        # Extract external links if requested
        external_links = []
        if extract_links_flag:
            external_links = extract_external_links(html, url, content_only)
        
        # Extract markdown
        try:
            markdown = extract(html,
                             include_links=True,
                             include_images=True,
                             output_format='markdown',
                             favor_precision=True,
                             favor_recall=False,
                             config=config)
            logger.info(f"Markdown extraction: {'Success' if markdown else 'Failed'}")
            
            # Extract metadata
            try:
                metadata = extract_metadata(html)
                logger.info(f"Metadata extraction: {'Success' if metadata else 'Failed'}")
            except Exception as e:
                logger.warning(f"Metadata extraction failed (non-critical): {e}")
                metadata = None
            
            if markdown:
                logger.info(f"Markdown length: {len(markdown)} characters")
                
                # Create response with images and links
                response_data = create_success_response(url, markdown, 'markdown', metadata, images, external_links)
                
                if metadata:
                    logger.info(f"Metadata included: title='{metadata.title}', author='{metadata.author}'")
                
                # Cache the response
                cache_response(url, response_data)
                
                return jsonify(response_data)
            else:
                # Fallback to plain text
                text = extract(html, config=config)
                if text:
                    logger.info(f"Fallback to plain text successful ({len(text)} characters)")
                    
                    response_data = create_success_response(url, text, 'text', metadata, images, external_links)
                    response_data['note'] = 'Returned plain text as markdown extraction failed'
                    
                    cache_response(url, response_data)
                    
                    return jsonify(response_data)
                else:
                    error_response = create_error_response("extraction_failed", "No extractable content found in the webpage", url)
                    logger.warning(f"No content could be extracted from: {url}")
                    return jsonify(error_response), 404
                    
        except Exception as e:
            error_response = create_error_response("extraction_error", f"Content extraction failed: {str(e)}", url)
            logger.error(f"Markdown extraction failed: {e}")
            return jsonify(error_response), 500
            
    except Exception as e:
        error_response = create_error_response("internal_error", f"Unexpected server error: {str(e)}", locals().get('url'))
        logger.error(f"Unexpected error: {e}")
        return jsonify(error_response), 500

if __name__ == "__main__":
    logger.info(f"Starting scraper service on port {FLASK_PORT}...")
    logger.info(f"Configuration: cache={CACHE_DURATION_MINUTES}min, rate_limit={RATE_LIMIT_SECONDS}s")
    logger.info(f"Connection pool: max_connections=10, pool_size=20")
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
