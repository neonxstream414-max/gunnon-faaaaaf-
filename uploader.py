import os
import sys
import time
import json
import logging
import requests
import httpx
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("worker")

FILE_ID = os.environ.get("FILE_ID", "")
FILENAME = os.environ.get("FILENAME", "video.mp4")
SOURCE_URL = os.environ.get("SOURCE_URL", "")
CALLBACK_URL = os.environ.get("CALLBACK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
COOKIES_JSON = os.environ.get("GPHOTOS_COOKIES_JSON", "[]")

DOWNLOAD_PATH = os.path.join("/tmp", FILENAME)

def download_file(url: str, dest_path: str):
    logger.info(f"Starting 1Gbps download for: {FILENAME} from {url[:60]}...")
    t0 = time.time()
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total_size = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024): # 4MB chunks
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        percent = (downloaded / total_size) * 100
                        print(f"\rDownloaded: {downloaded/(1024*1024):.1f}MB / {total_size/(1024*1024):.1f}MB ({percent:.1f}%)", end="", flush=True)
    
    elapsed = time.time() - t0
    logger.info(f"\nDownload completed in {elapsed:.1f}s (Size: {os.path.getsize(dest_path)/(1024*1024):.2f} MB)")

def upload_to_gphotos_via_selenium(file_path: str) -> str:
    logger.info("Setting up Headless Chrome Uploader...")
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)

    try:
        driver.get("https://photos.google.com")
        time.sleep(2)

        # Inject Google Cookies
        cookies = json.loads(COOKIES_JSON)
        for c in cookies:
            # Clean unsupported cookie fields
            c.pop("sameSite", None)
            c.pop("storeId", None)
            try:
                driver.add_cookie(c)
            except Exception:
                pass

        driver.get("https://photos.google.com")
        time.sleep(3)

        # Trigger file upload input
        file_input = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
        if not file_input:
            # Try finding upload button
            upload_btn = driver.find_elements(By.XPATH, "//*[contains(text(), 'Upload') or contains(@aria-label, 'Upload')]")
            if upload_btn:
                upload_btn[0].click()
                time.sleep(1)
            file_input = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")

        if not file_input:
            raise Exception("Upload input element not found in Google Photos!")

        logger.info("Uploading file to Google Photos...")
        file_input[0].send_keys(file_path)

        # Wait for upload progress to finish
        t_start = time.time()
        while time.time() - t_start < 1800: # Max 30 mins upload wait
            time.sleep(3)
            # Check for upload complete dialog / share button
            body_text = driver.find_element(By.TAG_NAME, "body").text
            if "Uploaded" in body_text or "Upload complete" in body_text or "Share" in body_text:
                logger.info("Upload completed successfully!")
                break
        
        # Click Share -> Create Link
        time.sleep(2)
        share_btns = driver.find_elements(By.CSS_SELECTOR, "button[aria-label*='Share'], button[data-tooltip*='Share']")
        if share_btns:
            share_btns[0].click()
            time.sleep(1.5)

        create_links = driver.find_elements(By.XPATH, "//*[contains(text(), 'Create link') or contains(text(), 'Get link')]")
        if create_links:
            create_links[0].click()
            time.sleep(2)

        # Confirm if modal appears
        confirms = driver.find_elements(By.XPATH, "//button[contains(., 'Create link')]")
        if confirms:
            confirms[-1].click()
            time.sleep(2)

        import re
        m = re.findall(r'https://photos\.app\.goo\.gl/[a-zA-Z0-9]+', driver.page_source)
        if not m:
            m = re.findall(r'https://photos\.google\.com/share/[a-zA-Z0-9_\-]+', driver.page_source)
        
        if m:
            return m[0]
        else:
            raise Exception("Could not extract generated share URL from Google Photos!")
    finally:
        driver.quit()

def send_callback(share_url: str):
    if not CALLBACK_URL:
        logger.warning("No CALLBACK_URL specified, printing share URL:")
        print(f"RESULT_SHARE_URL={share_url}")
        return

    logger.info(f"Sending callback to {CALLBACK_URL}...")
    resp = requests.post(
        CALLBACK_URL,
        json={
            "file_id": FILE_ID,
            "instant_url": share_url,
            "secret": WEBHOOK_SECRET,
            "status": "completed"
        },
        timeout=15
    )
    logger.info(f"Callback response: {resp.status_code} - {resp.text}")

def main():
    if not SOURCE_URL:
        logger.error("Missing SOURCE_URL!")
        sys.exit(1)

    try:
        download_file(SOURCE_URL, DOWNLOAD_PATH)
        share_url = upload_to_gphotos_via_selenium(DOWNLOAD_PATH)
        logger.info(f"🎉 Success! Generated Public Share URL: {share_url}")
        send_callback(share_url)
    except Exception as e:
        logger.error(f"Fatal error during processing: {e}")
        if CALLBACK_URL:
            requests.post(CALLBACK_URL, json={"file_id": FILE_ID, "status": "failed", "error": str(e), "secret": WEBHOOK_SECRET})
        sys.exit(1)
    finally:
        if os.path.exists(DOWNLOAD_PATH):
            os.remove(DOWNLOAD_PATH)

if __name__ == "__main__":
    main()
