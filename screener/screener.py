from flask import Flask, request, jsonify, send_from_directory
from werkzeug.exceptions import NotFound
import subprocess
import os
import hashlib
import time
import socket
import ipaddress
import hmac
from urllib.parse import urlparse
from datetime import datetime, timedelta
from PIL import Image
import logging

# Configuration from environment variables
FLASK_PORT = int(os.getenv('SCREENER_PORT', '5002'))
SCREENSHOT_DIR = os.getenv('SCREENSHOT_DIR', '/tmp/screener_screenshots')
CACHE_DURATION_MINUTES = int(os.getenv('SCREENER_CACHE_DURATION', '60'))
CLEANUP_OLDER_THAN_HOURS = int(os.getenv('SCREENER_CLEANUP_HOURS', '24'))
LOG_FILE = os.getenv('SCREENER_LOG_FILE', '/tmp/screener.log')
NODE_MODULES_PATH = os.getenv('NODE_MODULES_PATH', '/app/node_modules')
PUPPETEER_SCRIPT = os.getenv('PUPPETEER_SCRIPT', '/app/popup_handler.js')
API_KEY = os.getenv('API_KEY', '').strip()

# Configure logging
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

app = Flask(__name__)

# Endpoints that stay open so container healthchecks work without a key
PUBLIC_PATHS = {'/health'}

@app.before_request
def require_api_key():
    """Require a valid X-API-Key header when API_KEY is configured."""
    if not API_KEY:
        return  # authentication disabled
    if request.path in PUBLIC_PATHS:
        return
    provided = request.headers.get('X-API-Key', '')
    if not (provided and hmac.compare_digest(provided, API_KEY)):
        return jsonify({'success': False, 'error': 'Missing or invalid API key'}), 401

# Ensure screenshot directory exists
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# Simple in-memory cache for screenshots
screenshot_cache = {}

# Performance tracking
request_stats = {
    'total_requests': 0,
    'cache_hits': 0,
    'screenshots_taken': 0,
    'failed_screenshots': 0,
    'start_time': datetime.now()
}

def is_safe_url(url):
    """SSRF guard: allow only http/https URLs that resolve to public IP addresses.

    Blocks file:// (local file read via the browser), internal hosts, and the
    cloud metadata endpoint from being passed to Puppeteer.
    """
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
        addr_info = socket.getaddrinfo(hostname, None)
    except Exception:
        return False, "Hostname could not be resolved"

    for info in addr_info:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or
                ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, "URL resolves to a non-public address"

    return True, "URL is safe"

def cleanup_old_screenshots():
    """Remove screenshots older than specified hours"""
    try:
        cutoff_time = time.time() - (CLEANUP_OLDER_THAN_HOURS * 3600)
        removed_count = 0
        
        for filename in os.listdir(SCREENSHOT_DIR):
            filepath = os.path.join(SCREENSHOT_DIR, filename)
            if os.path.isfile(filepath):
                if os.path.getmtime(filepath) < cutoff_time:
                    os.remove(filepath)
                    removed_count += 1
        
        if removed_count > 0:
            logger.info(f"Cleaned up {removed_count} old screenshots")
            
    except Exception as e:
        logger.warning(f"Screenshot cleanup failed: {e}")

def get_cache_key(url, width, height, full_page, format_type, quality):
    """Generate cache key for screenshot"""
    key_string = f"{url}_{width}_{height}_{full_page}_{format_type}_{quality}"
    return hashlib.md5(key_string.encode()).hexdigest()

def get_cached_response(url, width, height, full_page, format_type, quality):
    """Check if we have a cached screenshot"""
    cache_key = get_cache_key(url, width, height, full_page, format_type, quality)
    
    if cache_key in screenshot_cache:
        cached_item = screenshot_cache[cache_key]
        
        # Check if cache is still valid
        if datetime.now() < cached_item['expires']:
            # Check if files still exist
            all_files_exist = all(
                os.path.exists(filepath) 
                for filepath in cached_item['data']['files'].values()
            )
            
            if all_files_exist:
                request_stats['cache_hits'] += 1
                return cached_item['data']
            else:
                # Files were deleted, remove from cache
                del screenshot_cache[cache_key]
        else:
            # Cache expired
            del screenshot_cache[cache_key]
    
    return None

def cache_response(url, width, height, full_page, format_type, quality, response_data):
    """Cache screenshot response"""
    cache_key = get_cache_key(url, width, height, full_page, format_type, quality)
    screenshot_cache[cache_key] = {
        'data': response_data,
        'expires': datetime.now() + timedelta(minutes=CACHE_DURATION_MINUTES),
        'cached_at': datetime.now()
    }

def convert_screenshot(png_path, output_format, quality=85):
    """Convert PNG to other formats"""
    try:
        img = Image.open(png_path)
        
        # Convert RGBA to RGB if saving as JPEG
        if output_format.lower() in ['jpeg', 'jpg'] and img.mode == 'RGBA':
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            rgb_img.paste(img, mask=img.split()[3])
            img = rgb_img
        
        output_path = png_path.replace('.png', f'.{output_format}')
        
        if output_format.lower() in ['jpeg', 'jpg']:
            img.save(output_path, 'JPEG', quality=quality, optimize=True)
        elif output_format.lower() == 'webp':
            img.save(output_path, 'WEBP', quality=quality)
        else:
            img.save(output_path, output_format.upper())
        
        return output_path
        
    except Exception as e:
        logger.error(f"Format conversion failed: {e}")
        return None

def create_thumbnail(image_path, max_width=300, max_height=200):
    """Create a thumbnail from an image"""
    try:
        img = Image.open(image_path)
        img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        
        # Always save thumbnails as JPEG
        if img.mode == 'RGBA':
            rgb_img = Image.new('RGB', img.size, (255, 255, 255))
            rgb_img.paste(img, mask=img.split()[3])
            img = rgb_img
        
        thumbnail_path = image_path.replace('.png', '_thumb.jpg').replace('.jpg', '_thumb.jpg').replace('.jpeg', '_thumb.jpg')
        img.save(thumbnail_path, 'JPEG', quality=80, optimize=True)
        
        return thumbnail_path
        
    except Exception as e:
        logger.error(f"Thumbnail creation failed: {e}")
        return None

def check_dependencies():
    """Check if required dependencies are available"""
    try:
        # Check Node.js
        result = subprocess.run(['node', '--version'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return False, "Node.js not found"
        
        node_version = result.stdout.strip()
        
        # Check if Puppeteer script exists
        if not os.path.exists(PUPPETEER_SCRIPT):
            return False, f"Puppeteer script not found at {PUPPETEER_SCRIPT}"
        
        # Check if node_modules exists
        if not os.path.exists(NODE_MODULES_PATH):
            return False, f"Node modules not found at {NODE_MODULES_PATH}"
        
        return True, f"Dependencies OK (Node: {node_version})"
        
    except Exception as e:
        return False, f"Dependency check failed: {str(e)}"

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    deps_ok, deps_message = check_dependencies()
    
    return jsonify({
        'status': 'healthy' if deps_ok else 'degraded',
        'service': 'screener',
        'dependencies': deps_message,
        'screenshot_dir': SCREENSHOT_DIR,
        'cache_entries': len(screenshot_cache)
    })

@app.route('/status', methods=['GET'])
def get_status():
    """Get service statistics"""
    uptime = datetime.now() - request_stats['start_time']
    total_requests = request_stats['total_requests']
    
    success_rate = (request_stats['screenshots_taken'] / total_requests * 100) if total_requests > 0 else 0
    cache_hit_rate = (request_stats['cache_hits'] / total_requests * 100) if total_requests > 0 else 0
    
    return jsonify({
        'uptime_seconds': int(uptime.total_seconds()),
        'uptime_formatted': str(uptime).split('.')[0],
        'total_requests': total_requests,
        'cache_hits': request_stats['cache_hits'],
        'cache_hit_rate': round(cache_hit_rate, 2),
        'screenshots_taken': request_stats['screenshots_taken'],
        'failed_screenshots': request_stats['failed_screenshots'],
        'success_rate': round(success_rate, 2),
        'cached_entries': len(screenshot_cache)
    })

@app.route('/screenshot/<filename>', methods=['GET'])
def serve_screenshot(filename):
    """Serve a screenshot file (send_from_directory guards against path traversal)"""
    try:
        return send_from_directory(SCREENSHOT_DIR, filename)
    except NotFound:
        return jsonify({'error': 'Screenshot not found'}), 404
    except Exception as e:
        logger.error(f"Error serving screenshot: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/screenshot', methods=['POST'])
def take_screenshot():
    """Take a screenshot of a URL"""
    request_stats['total_requests'] += 1
    
    try:
        # Cleanup old screenshots periodically
        if request_stats['total_requests'] % 10 == 0:
            cleanup_old_screenshots()
        
        data = request.get_json()
        
        if not data or 'url' not in data:
            return jsonify({
                'success': False,
                'error': 'Missing URL in request body'
            }), 400
        
        url = data['url']

        # SSRF protection: only allow public http(s) targets
        # (blocks file://, localhost, internal ranges, and metadata endpoints)
        is_safe, safe_reason = is_safe_url(url)
        if not is_safe:
            logger.warning(f"Blocked potential SSRF: {url} - {safe_reason}")
            return jsonify({
                'success': False,
                'error': f'URL not allowed: {safe_reason}'
            }), 400

        # Screenshot options with format support and type conversion
        try:
            width = int(data.get('width', 1200))
            height = int(data.get('height', 800))
            quality = int(data.get('quality', 85))
        except ValueError:
            return jsonify({
                'success': False,
                'error': 'Width, height, and quality must be numbers'
            }), 400
        
        full_page = data.get('full_page', False)
        format_type = data.get('format', 'png').lower()
        create_thumb = data.get('thumbnail', True)
        hide_elements = data.get('hide_elements', True)
        block_ads = data.get('block_ads', False)
        
        # Validate format
        if format_type not in ['png', 'jpeg', 'jpg', 'webp']:
            return jsonify({
                'success': False,
                'error': 'Invalid format. Use: png, jpeg, jpg, or webp'
            }), 400
        
        # Validate quality range
        if not 1 <= quality <= 100:
            return jsonify({
                'success': False,
                'error': 'Quality must be between 1 and 100'
            }), 400
        
        # Check cache first
        cached_response = get_cached_response(url, width, height, full_page, format_type, quality)
        if cached_response:
            logger.info(f"Returning cached screenshot for: {url}")
            cached_response['from_cache'] = True
            return jsonify(cached_response)
        
        # Generate unique filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        base_filename = f"screener_{url_hash}_{timestamp}"
        png_filepath = os.path.join(SCREENSHOT_DIR, f"{base_filename}.png")
        
        logger.info(f"Taking screenshot of: {url}")
        
        # Take screenshot using enhanced Puppeteer script
        try:
            cmd = [
                'node', PUPPETEER_SCRIPT,
                url, png_filepath,
                str(width), str(height), str(full_page).lower(),
                str(block_ads).lower(), str(hide_elements).lower()
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ, 'NODE_PATH': NODE_MODULES_PATH}
            )
            
            if result.returncode != 0:
                error_msg = result.stderr or result.stdout or "Unknown error"
                logger.error(f"Screenshot failed: {error_msg}")
                request_stats['failed_screenshots'] += 1
                return jsonify({
                    'success': False,
                    'error': 'Screenshot generation failed',
                    'details': error_msg
                }), 500
            
            if not os.path.exists(png_filepath):
                logger.error(f"Screenshot file not created: {png_filepath}")
                request_stats['failed_screenshots'] += 1
                return jsonify({
                    'success': False,
                    'error': 'Screenshot file was not created'
                }), 500
            
            request_stats['screenshots_taken'] += 1
            logger.info(f"Screenshot saved: {png_filepath}")
            
        except subprocess.TimeoutExpired:
            logger.error(f"Screenshot timeout for URL: {url}")
            request_stats['failed_screenshots'] += 1
            return jsonify({
                'success': False,
                'error': 'Screenshot generation timeout (60s)'
            }), 500
        except Exception as e:
            logger.error(f"Screenshot process error: {e}")
            request_stats['failed_screenshots'] += 1
            return jsonify({
                'success': False,
                'error': f'Screenshot process failed: {str(e)}'
            }), 500
        
        # Prepare response with file paths and sizes
        files = {'png': png_filepath}
        file_sizes = {'png': os.path.getsize(png_filepath)}
        
        # Convert to requested format if not PNG
        if format_type != 'png':
            converted_path = convert_screenshot(png_filepath, format_type, quality)
            if converted_path:
                files[format_type] = converted_path
                file_sizes[format_type] = os.path.getsize(converted_path)
                logger.info(f"Converted to {format_type}: {converted_path}")
        
        # Create thumbnail if requested
        thumbnail_path = None
        if create_thumb:
            # Create thumbnail from the primary format
            primary_file = files.get(format_type, png_filepath)
            thumbnail_path = create_thumbnail(primary_file)
            if thumbnail_path:
                files['thumbnail'] = thumbnail_path
                file_sizes['thumbnail'] = os.path.getsize(thumbnail_path)
                logger.info(f"Thumbnail created: {thumbnail_path}")
        
        # Determine primary file to return
        primary_file = files.get(format_type, png_filepath)
        
        response_data = {
            'success': True,
            'url': url,
            'format': format_type,
            'quality': quality if format_type in ['jpeg', 'jpg', 'webp'] else None,
            'width': width,
            'height': height,
            'full_page': full_page,
            'timestamp': timestamp,
            'files': files,
            'file_sizes': file_sizes,
            'primary_file': primary_file,
            'thumbnail_created': thumbnail_path is not None,
            'from_cache': False
        }
        
        # Cache the response
        cache_response(url, width, height, full_page, format_type, quality, response_data)
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        request_stats['failed_screenshots'] += 1
        return jsonify({
            'success': False,
            'error': f'Internal server error: {str(e)}'
        }), 500

if __name__ == "__main__":
    logger.info(f"Starting Screener service on port {FLASK_PORT}...")
    logger.info(f"Configuration: cache={CACHE_DURATION_MINUTES}min, cleanup={CLEANUP_OLDER_THAN_HOURS}h")
    logger.info(f"Screenshot directory: {SCREENSHOT_DIR}")
    logger.info(f"Puppeteer script: {PUPPETEER_SCRIPT}")
    logger.info(f"Node modules: {NODE_MODULES_PATH}")
    
    # Check dependencies on startup
    deps_ok, deps_message = check_dependencies()
    if deps_ok:
        logger.info(f"Dependencies check: {deps_message}")
    else:
        logger.warning(f"Dependencies issue: {deps_message}")
        logger.warning("Service will start but screenshots may fail")
    
    # Ensure screenshot directory exists
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
