const dns = require('dns').promises;
const net = require('net');

// --- SSRF guard: mirror the scraper's is_safe_url so in-browser navigations
// (including server-issued redirects) can never reach internal addresses. ---
function isPublicIp(ip) {
    const kind = net.isIP(ip);
    if (kind === 4) {
        const o = ip.split('.').map(Number);
        if (o.length !== 4 || o.some(n => Number.isNaN(n) || n < 0 || n > 255)) return false;
        const [a, b] = o;
        if (a === 0 || a === 10 || a === 127) return false;      // this-net, private, loopback
        if (a === 169 && b === 254) return false;                // link-local + cloud metadata
        if (a === 172 && b >= 16 && b <= 31) return false;       // private
        if (a === 192 && b === 168) return false;                // private
        if (a === 100 && b >= 64 && b <= 127) return false;      // CGNAT
        if (a === 192 && b === 0) return false;                  // protocol assignments
        if (a === 198 && (b === 18 || b === 19)) return false;   // benchmarking
        if (a >= 224) return false;                              // multicast + reserved
        return true;
    }
    if (kind === 6) {
        const s = ip.toLowerCase().split('%')[0];
        if (s === '::1' || s === '::') return false;             // loopback / unspecified
        const mapped = s.match(/^::ffff:(\d+\.\d+\.\d+\.\d+)$/); // IPv4-mapped
        if (mapped) return isPublicIp(mapped[1]);
        const head = s.split(':')[0];
        if (/^fe[89ab]/.test(head)) return false;                // fe80::/10 link-local
        if (/^f[cd]/.test(head)) return false;                   // fc00::/7 unique-local
        if (/^ff/.test(head)) return false;                      // ff00::/8 multicast
        return true;
    }
    return false;                                                // not a valid IP literal
}

async function urlIsSafe(urlStr) {
    let u;
    try { u = new URL(urlStr); } catch (e) { return false; }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
    let addrs;
    try {
        addrs = (await dns.lookup(u.hostname, { all: true })).map(r => r.address);
    } catch (e) {
        return false;
    }
    return addrs.length > 0 && addrs.every(isPublicIp);
}

async function takeScreenshot(url, outputPath, options = {}) {
    const puppeteer = require('/app/node_modules/puppeteer');
    let browser;
    try {
        browser = await puppeteer.launch({
            headless: true,
            executablePath: '/usr/bin/chromium',
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disable-software-rasterizer',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-features=VizDisplayCompositor'
            ]
        });
        
        const page = await browser.newPage();
        
        await page.setUserAgent('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36');
        
        await page.setViewport({
            width: options.width || 1200,
            height: options.height || 800,
            deviceScaleFactor: 1
        });
        
        page.setDefaultTimeout(30000);
        
        // Always intercept so we can validate every navigation (the initial load
        // and any server-issued redirect) against the SSRF policy before it is
        // fetched. This closes the gap where a public URL 302-redirects the
        // browser to an internal/metadata host.
        await page.setRequestInterception(true);
        page.on('request', async (req) => {
            try {
                if (req.isNavigationRequest() && !(await urlIsSafe(req.url()))) {
                    console.error(`Blocked non-public navigation: ${req.url()}`);
                    return req.abort('blockedbyclient');
                }
                if (options.blockAds && (
                    req.resourceType() === 'stylesheet' ||
                    req.url().includes('google-analytics') ||
                    req.url().includes('facebook.com') ||
                    req.url().includes('doubleclick') ||
                    req.url().includes('googletagmanager'))) {
                    return req.abort();
                }
                return req.continue();
            } catch (e) {
                // Never leave an intercepted request hanging.
                try { return req.abort(); } catch (_) { return; }
            }
        });
        
        console.log(`Navigating to: ${url}`);
        
        await page.goto(url, { 
            waitUntil: 'networkidle2',
            timeout: 30000 
        });
        
        await new Promise(resolve => setTimeout(resolve, 2000));
        
        await handlePopups(page);
        
        if (options.hideElements) {
            await hideDistractingElements(page);
        }
        
        await new Promise(resolve => setTimeout(resolve, 1000));
        
        console.log('Taking screenshot...');
        
        await page.screenshot({
            path: outputPath,
            type: 'png',
            fullPage: options.fullPage || false
        });
        
        console.log(`Screenshot saved to: ${outputPath}`);
        
    } catch (error) {
        console.error('Screenshot failed:', error.message);
        process.exit(1);
    } finally {
        if (browser) {
            await browser.close();
        }
    }
}

async function handlePopups(page) {
    console.log('Handling popups...');
    
    const popupSelectors = [
        '[id*="cookie"] button',
        '[class*="cookie"] button',
        '[data-testid*="cookie"] button',
        'button[aria-label*="cookie"]',
        'button[aria-label*="Accept"]',
        'button[aria-label*="Agree"]',
        '[class*="newsletter"] button[aria-label*="close"]',
        '[class*="modal"] button[aria-label*="close"]',
        '[class*="popup"] button[aria-label*="close"]',
        'button[aria-label="Close"]',
        'button[title="Close"]',
        'button.close',
        '.modal-close',
        '.popup-close',
        '[data-dismiss="modal"]'
    ];
    
    for (const selector of popupSelectors) {
        try {
            await page.waitForSelector(selector, { timeout: 1000 });
            await page.click(selector);
            console.log(`Dismissed popup: ${selector}`);
            await new Promise(resolve => setTimeout(resolve, 500));
        } catch (error) {
            continue;
        }
    }
    
    try {
        await page.keyboard.press('Escape');
        await new Promise(resolve => setTimeout(resolve, 500));
    } catch (error) {
        // ESC didn't work
    }
}

async function hideDistractingElements(page) {
    console.log('Hiding distracting elements...');
    
    const hideCSS = `
        [class*="cookie"],
        [id*="cookie"],
        [class*="banner"],
        [class*="popup"],
        [class*="modal"]:not([class*="content"]),
        [class*="overlay"],
        [class*="newsletter"],
        [class*="subscription"],
        [class*="chat"],
        [class*="support"],
        .advertisement,
        .ads,
        .ad-container,
        #advertisement,
        .sticky-header,
        .floating-header,
        .social-sharing,
        .share-buttons {
            display: none !important;
            visibility: hidden !important;
            opacity: 0 !important;
        }
    `;
    
    await page.addStyleTag({ content: hideCSS });
}

if (require.main === module) {
    const args = process.argv.slice(2);
    if (args.length < 2) {
        console.error('Usage: node popup_handler.js <url> <output_path> [width] [height] [fullPage] [blockAds] [hideElements]');
        process.exit(1);
    }

    const [url, outputPath, width, height, fullPage, blockAds, hideElements] = args;

    const options = {
        width: width ? parseInt(width) : 1200,
        height: height ? parseInt(height) : 800,
        fullPage: fullPage === 'true',
        blockAds: blockAds === 'true',
        hideElements: hideElements === 'true'
    };

    takeScreenshot(url, outputPath, options);
}

module.exports = { isPublicIp, urlIsSafe };
