import ipaddress


def is_safe_url(url):
    """Validate that a URL is safe for fetching: must be HTTPS, not local/private."""
    if not url.startswith("https://") and not url.startswith("http://"):
        return False, f"Only HTTP(S) URLs are allowed, got: {url.split(':')[0] if ':' in url else url}"

    host = url.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]

    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False, f"Localhost URLs are blocked: {url}"

    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_unspecified:
            return False, f"Private/reserved IP addresses are blocked: {url}"
    except ValueError:
        pass

    for suffix in (".local", ".internal", ".private", ".lan", ".home"):
        if host.endswith(suffix):
            return False, f"Private hostname blocked: {url}"

    return True, url
