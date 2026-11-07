from run_batch import Throttle
MAX_RETRIES = 5      # total attempts per request
MAX_WAIT = 60        # never wait longer than this many seconds


def retry_wait_seconds(response, attempt):
    """How long to wait after a 429/503: the server's Retry-After, else 2, 4, 8... seconds."""
    value = response.headers.get("Retry-After")
    if value:
        try:
            return min(float(value), MAX_WAIT)
        except ValueError:
            pass  # Retry-After may be a date; fall back to the backoff below
    return min(2 ** attempt, MAX_WAIT)


MAX_RETRIES = 5                      # total attempts per request
THROTTLE = Throttle(REQUEST_DELAY)   # spaces requests out; widens the gap after a 429


def get_with_retry(session, url, **kwargs):
    """GET that slows down and tries again on 'too many requests' (429) or 503."""
    for attempt in range(1, MAX_RETRIES + 1):
        THROTTLE.wait()
        response = session.get(url, **kwargs)
        if response.status_code not in (429, 503) or attempt == MAX_RETRIES:
            return response
        wait = THROTTLE.rejected(response.headers, attempt)
        print(f"Server asked us to slow down (HTTP {response.status_code}); "
              f"waiting {wait:.0f}s (attempt {attempt}/{MAX_RETRIES})")
        time.sleep(wait)
    return response
        wait = THROTTLE.rejected(response.headers, attempt)
        print(f"Server asked us to slow down (HTTP {response.status_code}); "
              f"waiting {wait:.0f}s (attempt {attempt}/{MAX_RETRIES})")
        time.sleep(wait)
    return response
