import asyncio
import sys
sys.path.append('.')
from app.main import BrowserRuntime, FetchRequest

async def main():
    rt = BrowserRuntime()
    await rt.start()
    try:
        payload = FetchRequest(
            url='https://www.zara.com/ca/en/whatever',
            allowed_hosts=['www.zara.com'],
            timeout_ms=30000,
            wait_after_dom_ms=3000,
            max_html_bytes=10000000,
            locale='en-CA',
            timezone='America/Toronto',
            wait_until='domcontentloaded',
        )
        result = await rt.fetch(payload)
        print('status ok', result['finalUrl'])
        print('title', result['title'])
        print('html_len', len(result['html']))
        print('body_preview', result['html'][:4000])
    except Exception as e:
        import traceback
        traceback.print_exc()
    finally:
        await rt.close()

asyncio.run(main())
