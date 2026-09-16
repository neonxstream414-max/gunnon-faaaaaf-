import os
import sys
import time
import json
import logging
import requests
from pathlib import Path
from typing import Generator, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("gphotos_github_worker")

# Read payload passed from Main VPS
PAYLOAD_STR = os.environ.get("TASK_PAYLOAD", "{}")
try:
    payload = json.loads(PAYLOAD_STR) if PAYLOAD_STR else {}
except Exception as e:
    logger.error(f"Failed to parse TASK_PAYLOAD: {e}")
    payload = {}

TASK_ID = payload.get("task_id") or os.environ.get("TASK_ID")
UNIQUE_ID = payload.get("unique_id") or os.environ.get("UNIQUE_ID")
FILENAME = payload.get("filename") or os.environ.get("FILENAME", "video.mkv")
DOWNLOAD_URL = payload.get("download_url") or os.environ.get("DOWNLOAD_URL", "")
ACCOUNTS = payload.get("accounts") or []
CALLBACK_URL = payload.get("callback_url") or os.environ.get("CALLBACK_URL", "")
API_KEY = payload.get("api_key") or os.environ.get("API_KEY", "")

TEMP_FILE = os.path.join("/tmp", f"worker_{UNIQUE_ID}_{int(time.time())}.tmp")
CHUNK_SIZE = 10 * 1024 * 1024  # 10MB chunks for GPMC


def send_callback(status: str, progress: int = 0, step: str = "", instant_url: str = "", error_msg: str = None):
    if not CALLBACK_URL:
        logger.warning("No CALLBACK_URL provided. Skipping callback.")
        return

    cb_data = {
        "task_id": TASK_ID,
        "unique_id": UNIQUE_ID,
        "status": status,
        "progress": progress,
        "step": step,
        "instant_url": instant_url,
        "error_msg": error_msg,
    }
    logger.info(f"Sending callback: status={status}, step={step}, instant_url={instant_url[:40]}...")
    try:
        headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}
        resp = requests.post(CALLBACK_URL, json=cb_data, headers=headers, timeout=20)
        logger.info(f"Callback HTTP Response: {resp.status_code}")
    except Exception as e:
        logger.error(f"Callback request failed: {e}")


def download_file(url: str, dest_path: str):
    logger.info(f"Downloading file: {FILENAME} from {url[:80]}...")
    send_callback(status="processing", progress=5, step="downloading")
    
    t0 = time.time()
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        total_size = int(r.headers.get("content-length", 0))
        downloaded = 0
        last_report = time.time()
        
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0 and time.time() - last_report > 5:
                        percent = int((downloaded / total_size) * 100)
                        print(f"\rDownload: {downloaded/(1024*1024):.1f}MB / {total_size/(1024*1024):.1f}MB ({percent}%)", end="", flush=True)
                        send_callback(status="processing", progress=int(percent * 0.4), step=f"downloading ({percent}%)")
                        last_report = time.time()

    elapsed = time.time() - t0
    logger.info(f"\nDownload finished in {elapsed:.1f}s. File Size: {os.path.getsize(dest_path)/(1024*1024):.2f} MB")
    send_callback(status="processing", progress=40, step="downloaded")


def chunked_reader(file_path: str) -> Generator[bytes, None, None]:
    with open(file_path, "rb") as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


class LoggingProgress:
    def add_task(self, description="", **kwargs):
        return 1
    def update(self, task_id, description=None, visible=None, **kwargs):
        if description:
            logger.info(f"[gpmc] {description}")
    def reset(self, task_id, **kwargs):
        pass
    def open(self, file, mode="rb", **kwargs):
        return open(file, mode)


def upload_to_gphotos_gpmc(file_path: str, filename: str, auth_data: str) -> Optional[str]:
    from gpmc.client import Client
    from gpmc.hash_handler import calculate_sha1_hash

    client = Client(auth_data=auth_data, timeout=600)
    file_size = os.path.getsize(file_path)

    # 1. SHA1 Hash
    hash_bytes, hash_b64 = calculate_sha1_hash(Path(file_path), LoggingProgress(), None)

    # 2. Check duplicate
    existing = client.api.find_remote_media_by_hash(hash_bytes)
    if existing:
        logger.info(f"File already exists in Google Photos: {existing}")
        return existing

    # 3. Get upload token
    upload_token = client.api.get_upload_token(hash_b64, file_size)
    if not upload_token:
        raise ValueError("Failed to get Google Photos upload token")

    # 4. Upload file chunks
    file_gen = chunked_reader(file_path)
    upload_response = client.api.upload_file(file=file_gen, upload_token=upload_token)
    if not upload_response:
        raise ValueError("upload_file returned empty response")

    # 5. Commit with Pixel XL (Unlimited Free Storage Trick)
    media_key = client.api.commit_upload(
        upload_response_decoded=upload_response,
        file_name=filename,
        sha1_hash=hash_bytes,
        upload_timestamp=int(time.time()),
        model="Pixel XL",
        quality="original",
    )
    return media_key


def main():
    if not DOWNLOAD_URL:
        logger.error("Missing DOWNLOAD_URL!")
        send_callback(status="failed", error_msg="Missing DOWNLOAD_URL")
        sys.exit(1)

    if not ACCOUNTS:
        logger.error("No Google Photos accounts provided in payload!")
        send_callback(status="failed", error_msg="No Google Photos accounts provided")
        sys.exit(1)

    try:
        download_file(DOWNLOAD_URL, TEMP_FILE)
        
        uploaded_copies = []
        total_accounts = len(ACCOUNTS)

        for i, acc in enumerate(ACCOUNTS):
            acc_id = acc.get("id")
            api_key = acc.get("api_key")
            if not api_key:
                continue

            logger.info(f"Uploading to Google Photos Account #{acc_id} ({i+1}/{total_accounts}) with Pixel XL Trick...")
            send_callback(status="processing", progress=40 + int(((i+1)/total_accounts)*50), step=f"uploading copy {i+1}/{total_accounts}")

            try:
                media_key = upload_to_gphotos_gpmc(TEMP_FILE, FILENAME, api_key)
                if media_key:
                    logger.info(f"Upload OK on Account #{acc_id}: media_key={str(media_key)[:30]}...")
                    uploaded_copies.append(f"{acc_id}:{media_key}")
                else:
                    logger.warning(f"Empty media_key on Account #{acc_id}")
            except Exception as e:
                logger.error(f"Upload failed on Account #{acc_id}: {e}")

        if not uploaded_copies:
            raise Exception("Upload failed across all provided Google Photos accounts.")

        final_instant_url = ", ".join(uploaded_copies)
        logger.info(f"🎉 All copies uploaded successfully! Result: {final_instant_url}")
        send_callback(status="completed", progress=100, step="completed", instant_url=final_instant_url)

    except Exception as e:
        logger.error(f"Fatal error in worker: {e}")
        send_callback(status="failed", error_msg=str(e))
        sys.exit(1)
    finally:
        if os.path.exists(TEMP_FILE):
            try:
                os.remove(TEMP_FILE)
            except Exception:
                pass


if __name__ == "__main__":
    main()
