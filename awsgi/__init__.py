from base64 import b64encode, b64decode
from io import BytesIO
import itertools
import collections
import sys
import gzip

ONE_MTU_SIZE = 1400

try:
    # Python 3
    from urllib.parse import urlencode

    # Convert bytes to str, if required
    def convert_str(s):
        return s.decode('utf-8') if isinstance(s, bytes) else s

    # Convert str to bytes, if required
    def convert_byte(b):
        return b.encode('utf-8', errors='strict') if (
            isinstance(b, str)) else b
except ImportError:
    # Python 2
    from urllib import urlencode

    # No conversion required
    def convert_str(s):
        return s

    # Convert str to bytes, if required
    def convert_byte(b):
        return b.encode('utf-8', errors='strict') if (
            isinstance(b, (str, unicode))) else b


__all__ = 'response',


def convert_base64(s):
    return b64encode(s).decode('ascii')


class StartResponse(object):
    def __init__(self, base64_content_types=None, use_gzip=False):
        '''
        Args:
            base64_content_types (set): Set of HTTP Content-Types which should
            return a base64 encoded body. Enables returning binary content from
            API Gateway.
        '''
        self.status = 500
        self.status_line = '500 Internal Server Error'
        self.headers = []
        self.use_gzip = use_gzip
        self.chunks = collections.deque()
        self.base64_content_types = set(base64_content_types or []) or set()

    def __call__(self, status, headers, exc_info=None):
        self.status_line = status
        self.status = int(status.split()[0])
        self.headers[:] = headers
        return self.chunks.append

    def use_binary_response(self, headers, body):
        content_type = headers.get('Content-Type')

        if content_type and ';' in content_type:
            content_type = content_type.split(';')[0]
        return content_type in self.base64_content_types

    def use_gzip_response(self, headers, body):
        content_type = headers.get('Content-Type')
        return self.use_gzip and content_type in {
            "application/javascript",
            "application/json",
            "text/css",
            "text/html",
            "text/plain",
            "text/html",
            "image/svg+xml",
            "font/otf",
            "font/ttf"
        } and len(body) > ONE_MTU_SIZE

    def build_body(self, headers, output):
        totalbody = b''.join(itertools.chain(
            self.chunks, output,
        ))

        is_gzip = self.use_gzip_response(headers, totalbody)
        is_b64 = self.use_binary_response(headers, totalbody)
        print(f"IS_GZIP = {is_gzip}")
        print(f"is_b64 = {is_b64}")
        if is_gzip:
            totalbody = gzip.compress(totalbody)
            headers["Content-Encoding"] = "gzip"
            is_b64 = True

        if is_b64:
            converted_output = convert_base64(totalbody)
        else:
            converted_output = convert_str(totalbody)

        return {
            'isBase64Encoded': is_b64,
            'body': converted_output,
        }

    def response(self, output):
        headers = dict(self.headers)

        rv = {
            'statusCode': self.status,
            'headers': headers,
        }
        rv.update(self.build_body(headers, output))
        return rv


class StartResponse_GW(StartResponse):
    def response(self, output):
        rv = super(StartResponse_GW, self).response(output)

        rv['statusCode'] = str(rv['statusCode'])

        return rv


class StartResponse_ELB(StartResponse):
    def response(self, output):
        rv = super(StartResponse_ELB, self).response(output)

        rv['statusCode'] = int(rv['statusCode'])
        rv['statusDescription'] = self.status_line

        return rv


def environ(event, context):
    # Check if format version is in v2, used for determining where to retrieve http method and path
    is_v2 = '2.0' in event.get('version', {})

    body = event.get('body', '') or ''

    if event.get('isBase64Encoded', False):
        body = b64decode(body)
    # FIXME: Flag the encoding in the headers
    body = convert_byte(body)

    environ = {
        # Get http method from within requestContext.http field in V2 format
        'REQUEST_METHOD': event['requestContext']['http']['method'] if is_v2 else event['httpMethod'],
        'SCRIPT_NAME': '',
        'SERVER_NAME': '',
        'SERVER_PORT': '',
        # Get path from within requestContext.http field in V2 format
        'PATH_INFO': event['requestContext']['http']['path'] if is_v2 else event['path'],
        # Use get() to access queryStringParameter field without throwing error if it doesn't exist
        'QUERY_STRING': urlencode(event.get('queryStringParameters') or {}),
        'REMOTE_ADDR': '127.0.0.1',
        'CONTENT_LENGTH': str(len(body)),
        'HTTP': 'on',
        'SERVER_PROTOCOL': 'HTTP/1.1',
        'wsgi.version': (1, 0),
        'wsgi.input': BytesIO(body),
        'wsgi.errors': sys.stderr,
        'wsgi.multithread': False,
        'wsgi.multiprocess': False,
        'wsgi.run_once': False,
        'wsgi.url_scheme': '',
        'awsgi.event': event,
        'awsgi.context': context,
    }
    headers = event.get('headers', {}) or {}
    for k, v in headers.items():
        k = k.upper().replace('-', '_')

        if k == 'CONTENT_TYPE':
            environ['CONTENT_TYPE'] = v
        elif k == 'ACCEPT_ENCODING':
            environ['ACCEPT_ENCODING'] = v
        elif k == 'HOST':
            environ['SERVER_NAME'] = v
        elif k == 'X_FORWARDED_FOR':
            environ['REMOTE_ADDR'] = v.split(', ')[0]
        elif k == 'X_FORWARDED_PROTO':
            environ['wsgi.url_scheme'] = v
        elif k == 'X_FORWARDED_PORT':
            environ['SERVER_PORT'] = v

        environ['HTTP_' + k] = v

    return environ


def select_impl(event, context):
    if 'elb' in event.get('requestContext', {}):
        return environ, StartResponse_ELB
    else:
        return environ, StartResponse_GW


def response(app, event, context, base64_content_types=None):

    environ, StartResponse = select_impl(event, context)

    use_gzip = bool("gzip" in event.get("headers", {}).get('accept-encoding', ""))
    sr = StartResponse(base64_content_types=base64_content_types, use_gzip=use_gzip)
    output = app(environ(event, context), sr)
    response = sr.response(output)

    return response
