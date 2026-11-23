"""A realistic test website for speed runs (never point QC at a real site for benchmarks).

  python scripts/bench_site.py --pages 10000 --port 8900

Serves three hosts on 127.0.0.x so QC sees a main site, an image CDN and "other sites":
  127.0.0.1:PORT  the site: /sitemap.xml lists /p/0../p/N-1; every page has ~300 words, 30 links to other listed pages,
                  5 links to category pages (/c/, not in the sitemap), 2 to old addresses (/old/ -> 301 to a page),
                  1 to a removed page (/gone/ -> 404), 3 images on the site itself
  127.0.0.2:PORT  image CDN: 3 more images per page
  127.0.0.3:PORT  "other sites": 3 outbound links per page
Pages answer in 60-140 ms, images and small responses in 15-35 ms, like a typical server behind a CDN.
"""
from __future__ import annotations

import argparse
import asyncio
import random

N = 10000
PORT = 8900


def page(i: int, host: str) -> bytes:
    rnd = random.Random(i)
    links = "".join(f'<li><a href="/p/{(i * 7 + k * 131) % N}">Related item {(i * 7 + k * 131) % N}</a></li>' for k in range(1, 31))
    cats = "".join(f'<a href="/c/{(i + k) % 200}">Category {(i + k) % 200}</a> ' for k in range(5))
    imgs = "".join(f'<img src="/img/{(i + k) % 500}.jpg" alt="Photo {k}" width="300" height="200">' for k in range(3))
    cdn = "".join(f'<img src="http://127.0.0.2:{PORT}/img/{(i * 3 + k) % 1500}.webp" alt="" width="300" height="200">' for k in range(3))
    ext = "".join(f'<a href="http://127.0.0.3:{PORT}/ref/{(i + k * 97) % 300}">Source {k}</a> ' for k in range(3))
    words = " ".join(rnd.choice(("loan", "rate", "offer", "phone", "price", "card", "service", "plan", "store", "delivery"))
                     for _ in range(300))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>Product {i} - Bench Store, prices and offers</title>'
            f'<meta name="description" content="Product {i} at the bench store: prices, offers, delivery and service details for this item.">'
            f'<link rel="canonical" href="http://{host}/p/{i}"></head><body><header><a href="/">Home</a> {cats}</header>'
            f'<main><h1>Product {i}</h1><p>{words}</p>{imgs}{cdn}<ul>{links}</ul>'
            f'<p><a href="/old/{i % 100}">Old offer</a> <a href="/old/{(i + 50) % 100}">Archive</a> <a href="/gone/{i % 50}">Discontinued</a></p>'
            f'<p>{ext}</p></main></body></html>').encode()


PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c63f80f00000101000518d84e0000000049454e44ae426082")


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            method, path, _ = line.decode("latin-1").split(" ", 2)
            host = f"127.0.0.1:{PORT}"
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                if h.lower().startswith(b"host:"):
                    host = h.split(b":", 1)[1].strip().decode()
            path = path.split("?", 1)[0]
            status, ctype, body, extra, delay = 200, "text/html; charset=utf-8", b"", "", random.uniform(0.015, 0.035)
            if path == "/robots.txt":
                body, ctype = f"User-agent: *\nAllow: /\nSitemap: http://{host}/sitemap.xml\n".encode(), "text/plain"
            elif path == "/sitemap.xml":
                body = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        + "".join(f"<url><loc>http://{host}/p/{i}</loc></url>" for i in range(N)) + "</urlset>").encode()
                ctype = "application/xml"
            elif path.startswith("/p/") and path[3:].isdigit() and int(path[3:]) < N:
                body, delay = page(int(path[3:]), host), random.uniform(0.06, 0.14)
            elif path == "/" or path.startswith("/c/") or path.startswith("/ref/"):
                body, delay = b"<!doctype html><html><head><title>Listing</title></head><body><h1>Listing</h1></body></html>", random.uniform(0.06, 0.14)
            elif path.startswith("/old/"):
                status, extra = 301, f"Location: /p/{int(path[5:] or 0) * 37 % N}\r\n"
            elif path.startswith("/img/"):
                body, ctype = PNG, ("image/webp" if path.endswith(".webp") else "image/jpeg")
            else:
                status, body = 404, b"<html><title>Not found</title></html>"
            await asyncio.sleep(delay)
            if method == "HEAD":
                payload = b""
            else:
                payload = body
            reason = {200: "OK", 301: "Moved Permanently", 404: "Not Found"}[status]
            writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n{extra}"
                         f"Connection: keep-alive\r\n\r\n".encode() + payload)
            await writer.drain()
    except (ConnectionError, ValueError):
        pass
    finally:
        writer.close()


async def main() -> None:
    servers = [await asyncio.start_server(handle, ip, PORT, backlog=1024) for ip in ("127.0.0.1", "127.0.0.2", "127.0.0.3")]
    print(f"bench site: http://127.0.0.1:{PORT}/sitemap.xml ({N:,} pages)", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=N)
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args()
    N, PORT = a.pages, a.port
    asyncio.run(main())
