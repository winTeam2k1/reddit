from __future__ import annotations

import base64
import math
import os
import re
import shutil
import struct
import tempfile
import urllib.parse
import zlib
from pathlib import Path


VIEWPORT = {"width": 1600, "height": 1400}
SLICE_HEIGHT = 4000
OUTPUT_DIR = Path("captures")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CHROME_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)


class RedditScreenshotError(RuntimeError):
    pass


def prompt_reddit_url() -> str:
    raw = input("Nhap link Reddit: ").strip()
    if not raw:
        raise RedditScreenshotError("Ban chua nhap link.")
    return normalize_reddit_url(raw)


def normalize_reddit_url(raw: str) -> str:
    candidate = raw.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        candidate = f"https://{candidate.lstrip('/')}"

    parsed = urllib.parse.urlparse(candidate)
    host = parsed.netloc.lower()
    if not host:
        raise RedditScreenshotError("Link khong hop le.")

    if "reddit.com" not in host and host != "redd.it":
        raise RedditScreenshotError("Link phai thuoc reddit.com hoac redd.it.")

    if host.endswith("reddit.com") and host not in {"www.reddit.com", "reddit.com"}:
        host = "www.reddit.com"

    cleaned = parsed._replace(scheme="https", netloc=host, fragment="")
    return urllib.parse.urlunparse(cleaned)


def make_output_path(url: str) -> Path:
    parsed = urllib.parse.urlparse(url)
    parts = [segment for segment in parsed.path.split("/") if segment]
    stem = parts[-1] if parts else "reddit_post"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "reddit_post"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    candidate = OUTPUT_DIR / f"{stem}.png"
    index = 2
    while candidate.exists():
        candidate = OUTPUT_DIR / f"{stem}_{index}.png"
        index += 1
    return candidate


def find_browser_executable() -> str | None:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    return None


def import_playwright():
    try:
        from playwright.sync_api import Error, TimeoutError, sync_playwright
    except ImportError as exc:
        raise RedditScreenshotError(
            "Chua cai Playwright.\n"
            "Hay chay:\n"
            "  .venv/bin/pip install playwright\n"
        ) from exc
    return sync_playwright, TimeoutError, Error


def launch_browser(playwright, *, headless: bool):
    executable_path = find_browser_executable()
    launch_args = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if executable_path:
        launch_args["executable_path"] = executable_path

    try:
        return playwright.chromium.launch(**launch_args)
    except Exception as exc:
        if executable_path:
            raise RedditScreenshotError(
                f"Khong mo duoc browser tai {executable_path}: {exc}"
            ) from exc
        raise RedditScreenshotError(
            "Khong tim thay Chrome/Chromium de chup man hinh."
        ) from exc


def browser_can_show_ui() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def dismiss_common_popups(page) -> None:
    patterns = [
        re.compile(r"accept all", re.I),
        re.compile(r"accept", re.I),
        re.compile(r"close", re.I),
        re.compile(r"not now", re.I),
    ]
    for pattern in patterns:
        locator = page.get_by_role("button", name=pattern)
        try:
            if locator.count():
                locator.first.click(timeout=1200)
                page.wait_for_timeout(300)
        except Exception:
            continue


def expand_post_if_needed(post, page) -> None:
    patterns = [
        re.compile(r"see more", re.I),
        re.compile(r"continue reading", re.I),
        re.compile(r"read more", re.I),
    ]
    for pattern in patterns:
        locator = post.get_by_role("button", name=pattern)
        try:
            if locator.count():
                locator.first.click(timeout=1200)
                page.wait_for_timeout(400)
        except Exception:
            continue


def hide_fixed_overlays(page, post_handle) -> None:
    page.evaluate(
        """
        (post) => {
          if (!post) return;
          const keep = (node) => node === post || node.contains(post) || post.contains(node);
          for (const node of document.body.querySelectorAll("*")) {
            const style = window.getComputedStyle(node);
            if ((style.position === "fixed" || style.position === "sticky") && !keep(node)) {
              node.style.setProperty("display", "none", "important");
            }
          }
          document.documentElement.style.setProperty("scroll-behavior", "auto", "important");
          document.body.style.setProperty("background", "#ffffff", "important");
        }
        """,
        post_handle,
    )


def get_post_bounds(post) -> dict[str, int]:
    bounds = post.evaluate(
        """
        (node) => {
          const rect = node.getBoundingClientRect();
          return {
            left: rect.left + window.scrollX,
            top: rect.top + window.scrollY,
            right: rect.right + window.scrollX,
            bottom: rect.bottom + window.scrollY
          };
        }
        """
    )
    left = max(0, math.floor(bounds["left"]))
    top = max(0, math.floor(bounds["top"]))
    right = math.ceil(bounds["right"])
    bottom = math.ceil(bounds["bottom"])
    return {
        "x": left,
        "y": top,
        "width": max(1, right - left),
        "height": max(1, bottom - top),
    }


def save_png(data_b64: str, path: Path) -> None:
    path.write_bytes(base64.b64decode(data_b64))


def capture_post_slices(page, bounds: dict[str, int], temp_dir: Path) -> list[Path]:
    cdp = page.context.new_cdp_session(page)
    cdp.send("Page.enable")

    paths: list[Path] = []
    remaining = bounds["height"]
    offset = 0
    index = 1

    while remaining > 0:
        current_height = min(SLICE_HEIGHT, remaining)
        payload = cdp.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": True,
                "clip": {
                    "x": bounds["x"],
                    "y": bounds["y"] + offset,
                    "width": bounds["width"],
                    "height": current_height,
                    "scale": 1,
                },
            },
        )
        part_path = temp_dir / f"part_{index:03d}.png"
        save_png(payload["data"], part_path)
        paths.append(part_path)
        remaining -= current_height
        offset += current_height
        index += 1

    return paths


def read_png_rgba(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if not data.startswith(PNG_SIGNATURE):
        raise RedditScreenshotError(f"File khong phai PNG hop le: {path}")

    chunks: list[tuple[bytes, bytes]] = []
    position = len(PNG_SIGNATURE)
    while position < len(data):
        length = struct.unpack(">I", data[position : position + 4])[0]
        position += 4
        chunk_type = data[position : position + 4]
        position += 4
        chunk_data = data[position : position + length]
        position += length + 4
        chunks.append((chunk_type, chunk_data))
        if chunk_type == b"IEND":
            break

    width = height = None
    bytes_per_pixel = None
    compressed_parts: list[bytes] = []
    for chunk_type, chunk_data in chunks:
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, compression, flt, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if (bit_depth, color_type, compression, flt, interlace) == (8, 6, 0, 0, 0):
                bytes_per_pixel = 4
            elif (bit_depth, color_type, compression, flt, interlace) == (8, 2, 0, 0, 0):
                bytes_per_pixel = 3
            else:
                raise RedditScreenshotError(
                    f"PNG dinh dang khong duoc ho tro trong {path}."
                )
        elif chunk_type == b"IDAT":
            compressed_parts.append(chunk_data)

    if width is None or height is None or bytes_per_pixel is None:
        raise RedditScreenshotError(f"Khong doc duoc kich thuoc PNG: {path}")

    raw = zlib.decompress(b"".join(compressed_parts))
    stride = width * bytes_per_pixel
    expected = height * (stride + 1)
    if len(raw) != expected:
        raise RedditScreenshotError(f"PNG bi loi du lieu: {path}")

    output = bytearray(height * stride)
    prior = bytearray(stride)

    for row in range(height):
        row_start = row * (stride + 1)
        filter_type = raw[row_start]
        filtered = raw[row_start + 1 : row_start + 1 + stride]
        current = bytearray(stride)

        if filter_type == 0:
            current[:] = filtered
        elif filter_type == 1:
            for i in range(stride):
                left = current[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                current[i] = (filtered[i] + left) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                current[i] = (filtered[i] + prior[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = current[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                up = prior[i]
                current[i] = (filtered[i] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = current[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                up = prior[i]
                upper_left = prior[i - bytes_per_pixel] if i >= bytes_per_pixel else 0
                current[i] = (filtered[i] + paeth_predictor(left, up, upper_left)) & 0xFF
        else:
            raise RedditScreenshotError(f"PNG dung filter khong ho tro: {filter_type}")

        out_start = row * stride
        output[out_start : out_start + stride] = current
        prior = current

    if bytes_per_pixel == 4:
        return width, height, bytes(output)

    rgba = bytearray(width * height * 4)
    write_index = 0
    for read_index in range(0, len(output), 3):
        rgba[write_index : write_index + 4] = output[read_index : read_index + 3] + b"\xFF"
        write_index += 4

    return width, height, bytes(rgba)


def paeth_predictor(left: int, up: int, upper_left: int) -> int:
    predict = left + up - upper_left
    dist_left = abs(predict - left)
    dist_up = abs(predict - up)
    dist_upper_left = abs(predict - upper_left)
    if dist_left <= dist_up and dist_left <= dist_upper_left:
        return left
    if dist_up <= dist_upper_left:
        return up
    return upper_left


def png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def write_png_rgba(width: int, height: int, rgba: bytes, output_path: Path) -> None:
    stride = width * 4
    raw = bytearray()
    for row in range(height):
        row_start = row * stride
        raw.append(0)
        raw.extend(rgba[row_start : row_start + stride])

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    encoded = (
        PNG_SIGNATURE
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"IDAT", zlib.compress(bytes(raw), level=9))
        + png_chunk(b"IEND", b"")
    )
    output_path.write_bytes(encoded)


def stitch_pngs(paths: list[Path], output_path: Path) -> None:
    combined = bytearray()
    width = None
    total_height = 0

    for path in paths:
        current_width, current_height, rgba = read_png_rgba(path)
        if width is None:
            width = current_width
        elif current_width != width:
            raise RedditScreenshotError("Cac phan anh khong cung chieu rong de ghep.")
        combined.extend(rgba)
        total_height += current_height

    if width is None:
        raise RedditScreenshotError("Khong co anh nao de ghep.")

    write_png_rgba(width, total_height, bytes(combined), output_path)


def ensure_not_blocked(page) -> None:
    body_text = page.locator("body").inner_text(timeout=5_000)
    if "You've been blocked by network security." in body_text:
        raise RedditScreenshotError(
            "Reddit dang chan request tu network hoac browser automation nay."
        )


def take_reddit_screenshot_once(url: str, output_path: Path, *, headless: bool) -> Path:
    sync_playwright, PlaywrightTimeoutError, PlaywrightError = import_playwright()

    with sync_playwright() as playwright:
        browser = launch_browser(playwright, headless=headless)
        context = browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,
            color_scheme="light",
            locale="en-US",
            user_agent=CHROME_USER_AGENT,
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = context.new_page()
        page.set_default_timeout(45_000)
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
              get: () => undefined
            });
            """
        )
        try:
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=8_000)
            except PlaywrightTimeoutError:
                pass

            ensure_not_blocked(page)
            dismiss_common_popups(page)
            post = page.locator("shreddit-post").first
            post.wait_for(state="visible")
            post.scroll_into_view_if_needed(timeout=5_000)
            page.wait_for_timeout(600)
            expand_post_if_needed(post, page)
            post_handle = post.element_handle()
            if post_handle is None:
                raise RedditScreenshotError("Khong lay duoc node cua shreddit-post.")

            hide_fixed_overlays(page, post_handle)
            page.wait_for_timeout(300)
            bounds = get_post_bounds(post)

            with tempfile.TemporaryDirectory(prefix="reddit_capture_") as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                parts = capture_post_slices(page, bounds, temp_dir)
                if len(parts) == 1:
                    output_path.write_bytes(parts[0].read_bytes())
                else:
                    stitch_pngs(parts, output_path)

            return output_path
        except PlaywrightTimeoutError as exc:
            raise RedditScreenshotError(
                "Het thoi gian cho khi tai Reddit hoac tim shreddit-post."
            ) from exc
        except PlaywrightError as exc:
            raise RedditScreenshotError(f"Playwright loi khi chup anh: {exc}") from exc
        finally:
            context.close()
            browser.close()


def take_reddit_screenshot(url: str, output_path: Path) -> Path:
    attempts = [True]
    if browser_can_show_ui():
        attempts.append(False)

    last_error: RedditScreenshotError | None = None
    for headless in attempts:
        try:
            return take_reddit_screenshot_once(url, output_path, headless=headless)
        except RedditScreenshotError as exc:
            last_error = exc
            if "Reddit dang chan request" not in str(exc) or not headless:
                raise

    if last_error is None:
        raise RedditScreenshotError("Khong the chup bai Reddit.")
    raise RedditScreenshotError(
        f"{last_error} Thu thu lai tren mang khac, dang nhap Reddit, hoac chay browser hien hinh."
    )


def main() -> int:
    try:
        url = prompt_reddit_url()
        output_path = make_output_path(url)
        final_path = take_reddit_screenshot(url, output_path)
        print(f"Da luu anh tai: {final_path.resolve()}")
        return 0
    except KeyboardInterrupt:
        print("\nDa huy.")
        return 130
    except RedditScreenshotError as exc:
        print(f"Loi: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
