"""Transport-exception classification shared by the URL checker and the on-page engine
(type-based first; message only as a tie-breaker)."""
import httpx


class SSRFRedirectBlocked(Exception):
    """A redirect hop pointed at a prohibited (private / link-local / metadata) address."""


def classify_request_exception(e: BaseException) -> str:
    """Map a transport exception to a QC V5 error_type using the exception TYPE first and the
    message only as a tie-breaker (httpx DNS failures say e.g. 'Name or service not known')."""
    for exc in (e, getattr(e, "__cause__", None), getattr(e, "__context__", None)):
        if isinstance(exc, SSRFRedirectBlocked):
            return "Blocked (SSRF)"
    if isinstance(e, httpx.TooManyRedirects):
        return "Redirect Loop"
    if isinstance(e, httpx.TimeoutException):
        return "Timeout"
    msg = str(e).lower()
    if "ssl" in msg or "certificate" in msg or "tls" in msg:
        return "SSL Error"
    if any(k in msg for k in ("name or service not known", "nodename nor servname", "getaddrinfo",
                              "name resolution", "no address associated", "name_not_resolved", "dns")):
        return "DNS Error"
    if isinstance(e, httpx.ConnectError) or "connect" in msg or "refused" in msg:
        return "Connection Error"
    if "timeout" in msg or "timed out" in msg:
        return "Timeout"
    return "Network Error"


def _chain(e: BaseException):
    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        yield e
        e = e.__cause__ or e.__context__


def describe_request_exception(e: BaseException) -> str:
    """The precise reason, in words, for a remark: which of DNS not found / DNS temporary failure / connection refused /
    connection timeout / connection reset / response timeout / SSL certificate / SSL handshake / QC's own connection
    pool was busy. The coarse error_type (classify_request_exception) still decides the status bucket."""
    import socket
    import ssl
    for x in _chain(e):
        if isinstance(x, httpx.PoolTimeout):
            return "QC connection pool busy (not the website)"
        if isinstance(x, httpx.ConnectTimeout):
            return "Connection timeout (no answer to connect)"
        if isinstance(x, (httpx.ReadTimeout, httpx.WriteTimeout)):
            return "Response timeout (connected, no answer in time)"
        if isinstance(x, socket.gaierror):
            if getattr(x, "errno", None) in (getattr(socket, "EAI_AGAIN", -3),):
                return "DNS temporary failure (resolver busy or unreachable)"
            return "DNS resolution failure (host name not found)"
        if isinstance(x, ssl.SSLCertVerificationError):
            return "SSL certificate not valid"
        if isinstance(x, ssl.SSLError):
            return "SSL/TLS handshake failed"
        if isinstance(x, ConnectionRefusedError):
            return "Connection refused"
        if isinstance(x, ConnectionResetError) or isinstance(x, httpx.RemoteProtocolError):
            return "Connection reset by the server"
    msg = str(e).lower()
    if "temporary failure in name resolution" in msg or "try again" in msg:
        return "DNS temporary failure (resolver busy or unreachable)"
    if any(k in msg for k in ("name or service not known", "nodename nor servname", "no address associated", "getaddrinfo failed")):
        return "DNS resolution failure (host name not found)"
    if "refused" in msg:
        return "Connection refused"
    if "reset" in msg:
        return "Connection reset by the server"
    if "certificate" in msg:
        return "SSL certificate not valid"
    if "ssl" in msg or "tls" in msg:
        return "SSL/TLS handshake failed"
    if isinstance(e, httpx.TimeoutException) or "timed out" in msg:
        return "Timeout"
    return "Network error"
