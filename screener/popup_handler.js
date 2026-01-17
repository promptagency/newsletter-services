const puppeteer = require('/opt/screener/node_modules/puppeteer');

async function takeScreenshot(url, outputPath, options = {}) {
    let browser;
    try {
        browser = await puppeteer.launch({
            headless: 'new',
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
                '--disable-web-security',
                '--disable-features=VizDisplayCompositor'
            ]
        });
        
        const page = await browser.newPage();
        
        await page.setUserAgent('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36');
        
        await page.setViewport({
            width: options.width || 1200,
            height: options.height || 800,
            deviceScaleFactor: 1
        });
        
        page.setDefaultTimeout(30000);
        
        if (options.blockAds) {
            await page.setRequestInterception(true);
            page.on('request', (req) => {
                if (req.resourceType() === 'stylesheet' || 
                    req.url().includes('google-analytics') ||
                    req.url().includes('facebook.com') ||
                    req.url().includes('doubleclick') ||
                    req.url().includes('googletagmanager')) {
                    req.abort();
                } else {
                    req.continue();
                }
            });
        }
        
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
