from __future__ import unicode_literals

import re

import six
from moto.core.utils import str_to_rfc_1123_datetime
from six.moves.urllib.parse import parse_qs, urlparse, unquote

import xmltodict

from moto.packages.httpretty.core import HTTPrettyRequest
from moto.core.responses import _TemplateEnvironmentMixin
from moto.core.utils import path_url

from moto.s3bucket_path.utils import bucket_name_from_url as bucketpath_bucket_name_from_url, \
    parse_key_name as bucketpath_parse_key_name, is_delete_keys as bucketpath_is_delete_keys

from .exceptions import BucketAlreadyExists, S3ClientError, MissingBucket, MissingKey, InvalidPartOrder, MalformedXML, \
    MalformedACLError, InvalidNotificationARN, InvalidNotificationEvent
from .models import s3_backend, get_canned_acl, FakeGrantee, FakeGrant, FakeAcl, FakeKey, FakeTagging, FakeTagSet, \
    FakeTag
from .utils import bucket_name_from_url, clean_key_name, metadata_from_headers, parse_region_from_url
from xml.dom import minidom


DEFAULT_REGION_NAME = 'us-east-1'


def parse_key_name(pth):
    return pth.lstrip("/")


def is_delete_keys(request, path, bucket_name):
    return path == u'/?delete' or (
        path == u'/' and
        getattr(request, "query_string", "") == "delete"
    )


class ResponseObject(_TemplateEnvironmentMixin):

    def __init__(self, backend):
        super(ResponseObject, self).__init__()
        self.backend = backend

    @property
    def should_autoescape(self):
        return True

    def all_buckets(self):
        # No bucket specified. Listing all buckets
        all_buckets = self.backend.get_all_buckets()
        template = self.response_template(S3_ALL_BUCKETS)
        return template.render(buckets=all_buckets)

    def subdomain_based_buckets(self, request):
        host = request.headers.get('host', request.headers.get('Host'))
        if not host:
            host = urlparse(request.url).netloc

        if (not host or host.startswith('localhost') or host.startswith('localstack') or
                re.match(r'^[^.]+$', host) or re.match(r'^.*\.svc\.cluster\.local$', host)):
            # Default to path-based buckets for (1) localhost, (2) localstack hosts (e.g. localstack.dev),
            # (3) local host names that do not contain a "." (e.g., Docker container host names), or
            # (4) kubernetes host names
            return False

        match = re.match(r'^([^\[\]:]+)(:\d+)?$', host)
        if match:
            match = re.match(r'((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)(\.|$)){4}',
                             match.groups()[0])
            if match:
                return False

        match = re.match(r'^\[(.+)\](:\d+)?$', host)
        if match:
            match = re.match(
                r'^(((?=.*(::))(?!.*\3.+\3))\3?|[\dA-F]{1,4}:)([\dA-F]{1,4}(\3|:\b)|\2){5}(([\dA-F]{1,4}(\3|:\b|$)|\2){2}|(((2[0-4]|1\d|[1-9])?\d|25[0-5])\.?\b){4})\Z',
                match.groups()[0], re.IGNORECASE)
            if match:
                return False

        path_based = (host == 's3.amazonaws.com' or re.match(
            r"s3[\.\-]([^.]*)\.amazonaws\.com", host))
        return not path_based

    def is_delete_keys(self, request, path, bucket_name):
        if self.subdomain_based_buckets(request):
            return is_delete_keys(request, path, bucket_name)
        else:
            return bucketpath_is_delete_keys(request, path, bucket_name)

    def parse_bucket_name_from_url(self, request, url):
        if self.subdomain_based_buckets(request):
            return bucket_name_from_url(url)
        else:
            return bucketpath_bucket_name_from_url(url)

    def parse_key_name(self, request, url):
        if self.subdomain_based_buckets(request):
            return parse_key_name(url)
        else:
            return bucketpath_parse_key_name(url)

    def ambiguous_response(self, request, full_url, headers):
        # Depending on which calling format the client is using, we don't know
        # if this is a bucket or key request so we have to check
        if self.subdomain_based_buckets(request):
            return self.key_response(request, full_url, headers)
        else:
            # Using path-based buckets
            return self.bucket_response(request, full_url, headers)

    def bucket_response(self, request, full_url, headers):
        try:
            response = self._bucket_response(request, full_url, headers)
        except S3ClientError as s3error:
            response = s3error.code, {}, s3error.description

        if isinstance(response, six.string_types):
            return 200, {}, response.encode("utf-8")
        else:
            status_code, headers, response_content = response
            if not isinstance(response_content, six.binary_type):
                response_content = response_content.encode("utf-8")

            return status_code, headers, response_content

    def _bucket_response(self, request, full_url, headers):
        parsed_url = urlparse(full_url)
        querystring = parse_qs(parsed_url.query, keep_blank_values=True)
        method = request.method
        region_name = parse_region_from_url(full_url)

        bucket_name = self.parse_bucket_name_from_url(request, full_url)
        if not bucket_name:
            # If no bucket specified, list all buckets
            return self.all_buckets()

        if hasattr(request, 'body'):
            # Boto
            body = request.body
        else:
            # Flask server
            body = request.data
        if body is None:
            body = b''
        if isinstance(body, six.binary_type):
            body = body.decode('utf-8')
        body = u'{0}'.format(body).encode('utf-8')

        if method == 'HEAD':
            return self._bucket_response_head(bucket_name, headers)
        elif method == 'GET':
            return self._bucket_response_get(bucket_name, querystring, headers)
        elif method == 'PUT':
            return self._bucket_response_put(request, body, region_name, bucket_name, querystring, headers)
        elif method == 'DELETE':
            return self._bucket_response_delete(body, bucket_name, querystring, headers)
        elif method == 'POST':
            return self._bucket_response_post(request, body, bucket_name, headers)
        else:
            raise NotImplementedError(
                "Method {0} has not been impelemented in the S3 backend yet".format(method))

    def _bucket_response_head(self, bucket_name, headers):
        try:
            self.backend.get_bucket(bucket_name)
        except MissingBucket:
            # Unless we do this, boto3 does not raise ClientError on
            # HEAD (which the real API responds with), and instead
            # raises NoSuchBucket, leading to inconsistency in
            # error response between real and mocked responses.
            return 404, {}, ""
        return 200, {}, ""

    def _bucket_response_get(self, bucket_name, querystring, headers):
        if 'uploads' in querystring:
            for unsup in ('delimiter', 'max-uploads'):
                if unsup in querystring:
                    raise NotImplementedError(
                        "Listing multipart uploads with {} has not been implemented yet.".format(unsup))
            multiparts = list(
                self.backend.get_all_multiparts(bucket_name).values())
            if 'prefix' in querystring:
                prefix = querystring.get('prefix', [None])[0]
                multiparts = [
                    upload for upload in multiparts if upload.key_name.startswith(prefix)]
            template = self.response_template(S3_ALL_MULTIPARTS)
            return template.render(
                bucket_name=bucket_name,
                uploads=multiparts)
        elif 'location' in querystring:
            bucket = self.backend.get_bucket(bucket_name)
            template = self.response_template(S3_BUCKET_LOCATION)

            location = bucket.location
            # us-east-1 is different - returns a None location
            if location == DEFAULT_REGION_NAME:
                location = None

            return template.render(location=location)
        elif 'lifecycle' in querystring:
            bucket = self.backend.get_bucket(bucket_name)
            if not bucket.rules:
                template = self.response_template(S3_NO_LIFECYCLE)
                return 404, {}, template.render(bucket_name=bucket_name)
            template = self.response_template(
                S3_BUCKET_LIFECYCLE_CONFIGURATION)
            return template.render(rules=bucket.rules)
        elif 'versioning' in querystring:
            versioning = self.backend.get_bucket_versioning(bucket_name)
            template = self.response_template(S3_BUCKET_GET_VERSIONING)
            return template.render(status=versioning)
        elif 'policy' in querystring:
            policy = self.backend.get_bucket_policy(bucket_name)
            if not policy:
                template = self.response_template(S3_NO_POLICY)
                return 404, {}, template.render(bucket_name=bucket_name)
            return 200, {}, policy
        elif 'website' in querystring:
            website_configuration = self.backend.get_bucket_website_configuration(
                bucket_name)
            if not website_configuration:
                template = self.response_template(S3_NO_BUCKET_WEBSITE_CONFIG)
                return 404, {}, template.render(bucket_name=bucket_name)
            return 200, {}, website_configuration
        elif 'acl' in querystring:
            bucket = self.backend.get_bucket(bucket_name)
            template = self.response_template(S3_OBJECT_ACL_RESPONSE)
            return template.render(obj=bucket)
        elif 'tagging' in querystring:
            bucket = self.backend.get_bucket(bucket_name)
            # "Special Error" if no tags:
            if len(bucket.tagging.tag_set.tags) == 0:
                template = self.response_template(S3_NO_BUCKET_TAGGING)
                return 404, {}, template.render(bucket_name=bucket_name)
            template = self.response_template(S3_BUCKET_TAGGING_RESPONSE)
            return template.render(bucket=bucket)
        elif 'logging' in querystring:
            bucket = self.backend.get_bucket(bucket_name)
            if not bucket.logging:
                template = self.response_template(S3_NO_LOGGING_CONFIG)
                return 200, {}, template.render()
            template = self.response_template(S3_LOGGING_CONFIG)
            return 200, {}, template.render(logging=bucket.logging)
        elif "cors" in querystring:
            bucket = self.backend.get_bucket(bucket_name)
            if len(bucket.cors) == 0:
                template = self.response_template(S3_NO_CORS_CONFIG)
                return 404, {}, template.render(bucket_name=bucket_name)
            template = self.response_template(S3_BUCKET_CORS_RESPONSE)
            return template.render(bucket=bucket)
        elif "notification" in querystring:
            bucket = self.backend.get_bucket(bucket_name)
            if not bucket.notification_configuration:
                return 200, {}, ""
            template = self.response_template(S3_GET_BUCKET_NOTIFICATION_CONFIG)
            return template.render(bucket=bucket)
        elif "accelerate" in querystring:
            bucket = self.backend.get_bucket(bucket_name)
            if bucket.accelerate_configuration is None:
                template = self.response_template(S3_BUCKET_ACCELERATE_NOT_SET)
                return 200, {}, template.render()
            template = self.response_template(S3_BUCKET_ACCELERATE)
            return template.render(bucket=bucket)

        elif 'versions' in querystring:
            delimiter = querystring.get('delimiter', [None])[0]
            encoding_type = querystring.get('encoding-type', [None])[0]
            key_marker = querystring.get('key-marker', [None])[0]
            max_keys = querystring.get('max-keys', [None])[0]
            prefix = querystring.get('prefix', [''])[0]
            version_id_marker = querystring.get('version-id-marker', [None])[0]

            bucket = self.backend.get_bucket(bucket_name)
            versions = self.backend.get_bucket_versions(
                bucket_name,
                delimiter=delimiter,
                encoding_type=encoding_type,
                key_marker=key_marker,
                max_keys=max_keys,
                version_id_marker=version_id_marker,
                prefix=prefix
            )
            latest_versions = self.backend.get_bucket_latest_versions(
                bucket_name=bucket_name
            )
            key_list = []
            delete_marker_list = []
            for version in versions:
                if isinstance(version, FakeKey):
                    key_list.append(version)
                else:
                    delete_marker_list.append(version)
            template = self.response_template(S3_BUCKET_GET_VERSIONS)
            return 200, {}, template.render(
                key_list=key_list,
                delete_marker_list=delete_marker_list,
                latest_versions=latest_versions,
                bucket=bucket,
                prefix='',
                max_keys=1000,
                delimiter='',
                is_truncated='false',
            )
        elif querystring.get('list-type', [None])[0] == '2':
            return 200, {}, self._handle_list_objects_v2(bucket_name, querystring)

        bucket = self.backend.get_bucket(bucket_name)
        prefix = querystring.get('prefix', [None])[0]
        if prefix and isinstance(prefix, six.binary_type):
            prefix = prefix.decode("utf-8")
        delimiter = querystring.get('delimiter', [None])[0]
        max_keys = int(querystring.get('max-keys', [1000])[0])
        marker = querystring.get('marker', [None])[0]
        result_keys, result_folders = self.backend.prefix_query(
            bucket, prefix, delimiter)

        if marker:
            result_keys = self._get_results_from_token(result_keys, marker)

        result_keys, is_truncated, _ = self._truncate_result(result_keys, max_keys)

        template = self.response_template(S3_BUCKET_GET_RESPONSE)
        return 200, {}, template.render(
            bucket=bucket,
            prefix=prefix,
            delimiter=delimiter,
            result_keys=result_keys,
            result_folders=result_folders,
            is_truncated=is_truncated,
            max_keys=max_keys
        )

    def _handle_list_objects_v2(self, bucket_name, querystring):
        template = self.response_template(S3_BUCKET_GET_RESPONSE_V2)
        bucket = self.backend.get_bucket(bucket_name)

        prefix = querystring.get('prefix', [None])[0]
        if prefix and isinstance(prefix, six.binary_type):
            prefix = prefix.decode("utf-8")
        delimiter = querystring.get('delimiter', [None])[0]
        result_keys, result_folders = self.backend.prefix_query(
            bucket, prefix, delimiter)

        fetch_owner = querystring.get('fetch-owner', [False])[0]
        max_keys = int(querystring.get('max-keys', [1000])[0])
        continuation_token = querystring.get('continuation-token', [None])[0]
        start_after = querystring.get('start-after', [None])[0]

        if continuation_token or start_after:
            limit = continuation_token or start_after
            if not delimiter:
                result_keys = self._get_results_from_token(result_keys, limit)
            else:
                result_folders = self._get_results_from_token(result_folders, limit)

        if not delimiter:
            result_keys, is_truncated, next_continuation_token = self._truncate_result(result_keys, max_keys)
        else:
            result_folders, is_truncated, next_continuation_token = self._truncate_result(result_folders, max_keys)

        return template.render(
            bucket=bucket,
            prefix=prefix or '',
            delimiter=delimiter,
            result_keys=result_keys,
            result_folders=result_folders,
            fetch_owner=fetch_owner,
            max_keys=max_keys,
            is_truncated=is_truncated,
            next_continuation_token=next_continuation_token,
            start_after=None if continuation_token else start_after
        )

    def _get_results_from_token(self, result_keys, token):
        continuation_index = 0
        for key in result_keys:
            if (key.name if isinstance(key, FakeKey) else key) > token:
                break
            continuation_index += 1
        return result_keys[continuation_index:]

    def _truncate_result(self, result_keys, max_keys):
        if len(result_keys) > max_keys:
            is_truncated = 'true'
            result_keys = result_keys[:max_keys]
            item = result_keys[-1]
            next_continuation_token = (item.name if isinstance(item, FakeKey) else item)
        else:
            is_truncated = 'false'
            next_continuation_token = None
        return result_keys, is_truncated, next_continuation_token

    def _bucket_response_put(self, request, body, region_name, bucket_name, querystring, headers):
        if not request.headers.get('Content-Length'):
            return 411, {}, "Content-Length required"
        if 'versioning' in querystring:
            ver = re.search('<Status>([A-Za-z]+)</Status>', body.decode())
            if ver:
                self.backend.set_bucket_versioning(bucket_name, ver.group(1))
                template = self.response_template(S3_BUCKET_VERSIONING)
                return template.render(bucket_versioning_status=ver.group(1))
            else:
                return 404, {}, ""
        elif 'lifecycle' in querystring:
            rules = xmltodict.parse(body)['LifecycleConfiguration']['Rule']
            if not isinstance(rules, list):
                # If there is only one rule, xmldict returns just the item
                rules = [rules]
            self.backend.set_bucket_lifecycle(bucket_name, rules)
            return ""
        elif 'policy' in querystring:
            self.backend.set_bucket_policy(bucket_name, body)
            return 'True'
        elif 'acl' in querystring:
            # Headers are first. If not set, then look at the body (consistent with the documentation):
            acls = self._acl_from_headers(request.headers)
            if not acls:
                acls = self._acl_from_xml(body)
            self.backend.set_bucket_acl(bucket_name, acls)
            return ""
        elif "tagging" in querystring:
            tagging = self._bucket_tagging_from_xml(body)
            self.backend.put_bucket_tagging(bucket_name, tagging)
            return ""
        elif 'website' in querystring:
            self.backend.set_bucket_website_configuration(bucket_name, body)
            return ""
        elif "cors" in querystring:
            try:
                self.backend.put_bucket_cors(bucket_name, self._cors_from_xml(body))
                return ""
            except KeyError:
                raise MalformedXML()
        elif "logging" in querystring:
            try:
                self.backend.put_bucket_logging(bucket_name, self._logging_from_xml(body))
                return ""
            except KeyError:
                raise MalformedXML()
        elif "notification" in querystring:
            try:
                self.backend.put_bucket_notification_configuration(bucket_name,
                                                                   self._notification_config_from_xml(body))
                return ""
            except KeyError:
                raise MalformedXML()
            except Exception as e:
                raise e
        elif "accelerate" in querystring:
            try:
                accelerate_status = self._accelerate_config_from_xml(body)
                self.backend.put_bucket_accelerate_configuration(bucket_name, accelerate_status)
                return ""
            except KeyError:
                raise MalformedXML()
            except Exception as e:
                raise e

        else:
            if body:
                # us-east-1, the default AWS region behaves a bit differently
                # - you should not use it as a location constraint --> it fails
                # - querying the location constraint returns None
                try:
                    forced_region = xmltodict.parse(body)['CreateBucketConfiguration']['LocationConstraint']

                    if forced_region == DEFAULT_REGION_NAME:
                        raise S3ClientError(
                            'InvalidLocationConstraint',
                            'The specified location-constraint is not valid'
                        )
                    else:
                        region_name = forced_region
                except KeyError:
                    pass

            try:
                new_bucket = self.backend.create_bucket(
                    bucket_name, region_name)
            except BucketAlreadyExists:
                if region_name == DEFAULT_REGION_NAME:
                    # us-east-1 has different behavior
                    new_bucket = self.backend.get_bucket(bucket_name)
                else:
                    raise

            if 'x-amz-acl' in request.headers:
                # TODO: Support the XML-based ACL format
                self.backend.set_bucket_acl(bucket_name, self._acl_from_headers(request.headers))

            template = self.response_template(S3_BUCKET_CREATE_RESPONSE)
            return 200, {}, template.render(bucket=new_bucket)

    def _bucket_response_delete(self, body, bucket_name, querystring, headers):
        if 'policy' in querystring:
            self.backend.delete_bucket_policy(bucket_name, body)
            return 204, {}, ""
        elif "tagging" in querystring:
            self.backend.delete_bucket_tagging(bucket_name)
            return 204, {}, ""
        elif "cors" in querystring:
            self.backend.delete_bucket_cors(bucket_name)
            return 204, {}, ""
        elif 'lifecycle' in querystring:
            bucket = self.backend.get_bucket(bucket_name)
            bucket.delete_lifecycle()
            return 204, {}, ""

        removed_bucket = self.backend.delete_bucket(bucket_name)

        if removed_bucket:
            # Bucket exists
            template = self.response_template(S3_DELETE_BUCKET_SUCCESS)
            return 204, {}, template.render(bucket=removed_bucket)
        else:
            # Tried to delete a bucket that still has keys
            template = self.response_template(
                S3_DELETE_BUCKET_WITH_ITEMS_ERROR)
            return 409, {}, template.render(bucket=removed_bucket)

    def _bucket_response_post(self, request, body, bucket_name, headers):
        if not request.headers.get('Content-Length'):
            return 411, {}, "Content-Length required"

        if isinstance(request, HTTPrettyRequest):
            path = request.path
        else:
            path = request.full_path if hasattr(request, 'full_path') else path_url(request.url)

        if self.is_delete_keys(request, path, bucket_name):
            return self._bucket_response_delete_keys(request, body, bucket_name, headers)

        # POST to bucket-url should create file from form
        if hasattr(request, 'form'):
            # Not HTTPretty
            form = request.form
        else:
            # HTTPretty, build new form object
            body = body.decode()

            form = {}
            for kv in body.split('&'):
                k, v = kv.split('=')
                form[k] = v

        key = form['key']
        if 'file' in form:
            f = form['file']
        else:
            f = request.files['file'].stream.read()

        new_key = self.backend.set_key(bucket_name, key, f)

        # Metadata
        metadata = metadata_from_headers(form)
        new_key.set_metadata(metadata)

        return 200, {}, ""

    def _bucket_response_delete_keys(self, request, body, bucket_name, headers):
        template = self.response_template(S3_DELETE_KEYS_RESPONSE)

        keys = minidom.parseString(body).getElementsByTagName('Key')
        deleted_names = []
        error_names = []
        if len(keys) == 0:
            raise MalformedXML()

        for k in keys:
            key_name = k.firstChild.nodeValue
            success = self.backend.delete_key(bucket_name, key_name)
            if success:
                deleted_names.append(key_name)
            else:
                error_names.append(key_name)

        return 200, {}, template.render(deleted=deleted_names, delete_errors=error_names)

    def _handle_range_header(self, request, headers, response_content):
        response_headers = {}
        length = len(response_content)
        last = length - 1
        _, rspec = request.headers.get('range').split('=')
        if ',' in rspec:
            raise NotImplementedError(
                "Multiple range specifiers not supported")

        def toint(i):
            return int(i) if i else None

        begin, end = map(toint, rspec.split('-'))
        if begin is not None:  # byte range
            end = last if end is None else min(end, last)
        elif end is not None:  # suffix byte range
            begin = length - min(end, length)
            end = last
        else:
            return 400, response_headers, ""
        if begin < 0 or end > last or begin > min(end, last):
            return 416, response_headers, ""
        response_headers['content-range'] = "bytes {0}-{1}/{2}".format(
            begin, end, length)
        return 206, response_headers, response_content[begin:end + 1]

    def key_response(self, request, full_url, headers):
        response_headers = {}
        try:
            response = self._key_response(request, full_url, headers)
        except S3ClientError as s3error:
            response = s3error.code, {}, s3error.description

        if isinstance(response, six.string_types):
            status_code = 200
            response_content = response
        else:
            status_code, response_headers, response_content = response

        if status_code == 200 and 'range' in request.headers:
            return self._handle_range_header(request, response_headers, response_content)
        return status_code, response_headers, response_content

    def _key_response(self, request, full_url, headers):
        parsed_url = urlparse(full_url)
        query = parse_qs(parsed_url.query, keep_blank_values=True)
        method = request.method

        key_name = self.parse_key_name(request, parsed_url.path)
        bucket_name = self.parse_bucket_name_from_url(request, full_url)

        # Because we patch the requests library the boto/boto3 API
        # requests go through this method but so do
        # `requests.get("https://bucket-name.s3.amazonaws.com/file-name")`
        # Here we deny public access to private files by checking the
        # ACL and checking for the mere presence of an Authorization
        # header.
        if 'Authorization' not in request.headers:
            if hasattr(request, 'url'):
                signed_url = 'Signature=' in request.url
            elif hasattr(request, 'requestline'):
                signed_url = 'Signature=' in request.path
            key = self.backend.get_key(bucket_name, key_name)

            if key:
                if not key.acl.public_read and not signed_url:
                    return 403, {}, ""

        if hasattr(request, 'body'):
            # Boto
            body = request.body
            if hasattr(body, 'read'):
                body = body.read()
        else:
            # Flask server
            body = request.data
        if body is None:
            body = b''

        if method == 'GET':
            return self._key_response_get(bucket_name, query, key_name, headers=request.headers)
        elif method == 'PUT':
            return self._key_response_put(request, body, bucket_name, query, key_name, headers)
        elif method == 'HEAD':
            return self._key_response_head(bucket_name, query, key_name, headers=request.headers)
        elif method == 'DELETE':
            return self._key_response_delete(bucket_name, query, key_name, headers)
        elif method == 'POST':
            return self._key_response_post(request, body, bucket_name, query, key_name, headers)
        else:
            raise NotImplementedError(
                "Method {0} has not been implemented in the S3 backend yet".format(method))

    def _key_response_get(self, bucket_name, query, key_name, headers):
        response_headers = {}
        if query.get('uploadId'):
            upload_id = query['uploadId'][0]
            parts = self.backend.list_multipart(bucket_name, upload_id)
            template = self.response_template(S3_MULTIPART_LIST_RESPONSE)
            return 200, response_headers, template.render(
                bucket_name=bucket_name,
                key_name=key_name,
                upload_id=upload_id,
                count=len(parts),
                parts=parts
            )
        version_id = query.get('versionId', [None])[0]
        if_modified_since = headers.get('If-Modified-Since', None)
        key = self.backend.get_key(
            bucket_name, key_name, version_id=version_id)
        if key is None:
            raise MissingKey(key_name)
        if if_modified_since:
            if_modified_since = str_to_rfc_1123_datetime(if_modified_since)
        if if_modified_since and key.last_modified < if_modified_since:
            return 304, response_headers, 'Not Modified'
        if 'acl' in query:
            template = self.response_template(S3_OBJECT_ACL_RESPONSE)
            return 200, response_headers, template.render(obj=key)
        if 'tagging' in query:
            template = self.response_template(S3_OBJECT_TAGGING_RESPONSE)
            return 200, response_headers, template.render(obj=key)

        response_headers.update(key.metadata)
        response_headers.update(key.response_dict)
        return 200, response_headers, key.value

    def _key_response_put(self, request, body, bucket_name, query, key_name, headers):
        response_headers = {}
        if query.get('uploadId') and query.get('partNumber'):
            upload_id = query['uploadId'][0]
            part_number = int(query['partNumber'][0])
            if 'x-amz-copy-source' in request.headers:
                src = unquote(request.headers.get("x-amz-copy-source")).lstrip("/")
                src_bucket, src_key = src.split("/", 1)

                src_key, src_version_id = src_key.split("?versionId=") if "?versionId=" in src_key else (src_key, None)
                src_range = request.headers.get(
                    'x-amz-copy-source-range', '').split("bytes=")[-1]

                try:
                    start_byte, end_byte = src_range.split("-")
                    start_byte, end_byte = int(start_byte), int(end_byte)
                except ValueError:
                    start_byte, end_byte = None, None

                if self.backend.get_key(src_bucket, src_key, version_id=src_version_id):
                    key = self.backend.copy_part(
                        bucket_name, upload_id, part_number, src_bucket,
                        src_key, src_version_id, start_byte, end_byte)
                else:
                    return 404, response_headers, ""

                template = self.response_template(S3_MULTIPART_UPLOAD_RESPONSE)
                response = template.render(part=key)
            else:
                key = self.backend.set_part(
                    bucket_name, upload_id, part_number, body)
                response = ""
            response_headers.update(key.response_dict)
            return 200, response_headers, response

        storage_class = request.headers.get('x-amz-storage-class', 'STANDARD')
        acl = self._acl_from_headers(request.headers)
        if acl is None:
            acl = self.backend.get_bucket(bucket_name).acl
        tagging = self._tagging_from_headers(request.headers)

        if 'acl' in query:
            key = self.backend.get_key(bucket_name, key_name)
            # TODO: Support the XML-based ACL format
            key.set_acl(acl)
            return 200, response_headers, ""

        if 'tagging' in query:
            tagging = self._tagging_from_xml(body)
            self.backend.set_key_tagging(bucket_name, key_name, tagging)
            return 200, response_headers, ""

        if 'x-amz-copy-source' in request.headers:
            # Copy key
            # you can have a quoted ?version=abc with a version Id, so work on
            # we need to parse the unquoted string first
            src_key = clean_key_name(request.headers.get("x-amz-copy-source"))
            if isinstance(src_key, six.binary_type):
                src_key = src_key.decode('utf-8')
            src_key_parsed = urlparse(src_key)
            src_bucket, src_key = unquote(src_key_parsed.path).\
                lstrip("/").split("/", 1)
            src_version_id = parse_qs(src_key_parsed.query).get(
                'versionId', [None])[0]

            if self.backend.get_key(src_bucket, src_key, version_id=src_version_id):
                self.backend.copy_key(src_bucket, src_key, bucket_name, key_name,
                                      storage=storage_class, acl=acl, src_version_id=src_version_id)
            else:
                return 404, response_headers, ""

            new_key = self.backend.get_key(bucket_name, key_name)
            mdirective = request.headers.get('x-amz-metadata-directive')
            if mdirective is not None and mdirective == 'REPLACE':
                metadata = metadata_from_headers(request.headers)
                new_key.set_metadata(metadata, replace=True)
            template = self.response_template(S3_OBJECT_COPY_RESPONSE)
            response_headers.update(new_key.response_dict)
            return 200, response_headers, template.render(key=new_key)
        streaming_request = hasattr(request, 'streaming') and request.streaming
        closing_connection = headers.get('connection') == 'close'
        if closing_connection and streaming_request:
            # Closing the connection of a streaming request. No more data
            new_key = self.backend.get_key(bucket_name, key_name)
        elif streaming_request:
            # Streaming request, more data
            new_key = self.backend.append_to_key(bucket_name, key_name, body)
        else:
            # Initial data
            new_key = self.backend.set_key(bucket_name, key_name, body,
                                           storage=storage_class)
            request.streaming = True
            metadata = metadata_from_headers(request.headers)
            new_key.set_metadata(metadata)
            new_key.set_acl(acl)
            new_key.website_redirect_location = request.headers.get('x-amz-website-redirect-location')
            new_key.set_tagging(tagging)

        template = self.response_template(S3_OBJECT_RESPONSE)
        response_headers.update(new_key.response_dict)
        return 200, response_headers, template.render(key=new_key)

    def _key_response_head(self, bucket_name, query, key_name, headers):
        response_headers = {}
        version_id = query.get('versionId', [None])[0]
        part_number = query.get('partNumber', [None])[0]
        if part_number:
            part_number = int(part_number)

        if_modified_since = headers.get('If-Modified-Since', None)
        if if_modified_since:
            if_modified_since = str_to_rfc_1123_datetime(if_modified_since)

        key = self.backend.get_key(
            bucket_name,
            key_name,
            version_id=version_id,
            part_number=part_number
        )
        if key:
            response_headers.update(key.metadata)
            response_headers.update(key.response_dict)

            if if_modified_since and key.last_modified < if_modified_since:
                return 304, response_headers, 'Not Modified'
            else:
                return 200, response_headers, ""
        else:
            return 404, response_headers, ""

    def _acl_from_xml(self, xml):
        parsed_xml = xmltodict.parse(xml)
        if not parsed_xml.get("AccessControlPolicy"):
            raise MalformedACLError()

        # The owner is needed for some reason...
        if not parsed_xml["AccessControlPolicy"].get("Owner"):
            # TODO: Validate that the Owner is actually correct.
            raise MalformedACLError()

        # If empty, then no ACLs:
        if parsed_xml["AccessControlPolicy"].get("AccessControlList") is None:
            return []

        if not parsed_xml["AccessControlPolicy"]["AccessControlList"].get("Grant"):
            raise MalformedACLError()

        permissions = [
            "READ",
            "WRITE",
            "READ_ACP",
            "WRITE_ACP",
            "FULL_CONTROL"
        ]

        if not isinstance(parsed_xml["AccessControlPolicy"]["AccessControlList"]["Grant"], list):
            parsed_xml["AccessControlPolicy"]["AccessControlList"]["Grant"] = \
                [parsed_xml["AccessControlPolicy"]["AccessControlList"]["Grant"]]

        grants = self._get_grants_from_xml(parsed_xml["AccessControlPolicy"]["AccessControlList"]["Grant"],
                                           MalformedACLError, permissions)
        return FakeAcl(grants)

    def _get_grants_from_xml(self, grant_list, exception_type, permissions):
        grants = []
        for grant in grant_list:
            if grant.get("Permission", "") not in permissions:
                raise exception_type()

            if grant["Grantee"].get("@xsi:type", "") not in ["CanonicalUser", "AmazonCustomerByEmail", "Group"]:
                raise exception_type()

            # TODO: Verify that the proper grantee data is supplied based on the type.

            grants.append(FakeGrant(
                [FakeGrantee(id=grant["Grantee"].get("ID", ""), display_name=grant["Grantee"].get("DisplayName", ""),
                             uri=grant["Grantee"].get("URI", ""))],
                [grant["Permission"]])
            )

        return grants

    def _acl_from_headers(self, headers):
        canned_acl = headers.get('x-amz-acl', '')
        if canned_acl:
            return get_canned_acl(canned_acl)

        grants = []
        for header, value in headers.items():
            if not header.startswith('x-amz-grant-'):
                continue

            permission = {
                'read': 'READ',
                'write': 'WRITE',
                'read-acp': 'READ_ACP',
                'write-acp': 'WRITE_ACP',
                'full-control': 'FULL_CONTROL',
            }[header[len('x-amz-grant-'):]]

            grantees = []
            for key_and_value in value.split(","):
                key, value = re.match(
                    '([^=]+)="([^"]+)"', key_and_value.strip()).groups()
                if key.lower() == 'id':
                    grantees.append(FakeGrantee(id=value))
                else:
                    grantees.append(FakeGrantee(uri=value))
            grants.append(FakeGrant(grantees, [permission]))

        if grants:
            return FakeAcl(grants)
        else:
            return None

    def _tagging_from_headers(self, headers):
        if headers.get('x-amz-tagging'):
            parsed_header = parse_qs(headers['x-amz-tagging'], keep_blank_values=True)
            tags = []
            for tag in parsed_header.items():
                tags.append(FakeTag(tag[0], tag[1][0]))

            tag_set = FakeTagSet(tags)
            tagging = FakeTagging(tag_set)
            return tagging
        else:
            return FakeTagging()

    def _tagging_from_xml(self, xml):
        parsed_xml = xmltodict.parse(xml, force_list={'Tag': True})

        tags = []
        for tag in parsed_xml['Tagging']['TagSet']['Tag']:
            tags.append(FakeTag(tag['Key'], tag['Value']))

        tag_set = FakeTagSet(tags)
        tagging = FakeTagging(tag_set)
        return tagging

    def _bucket_tagging_from_xml(self, xml):
        parsed_xml = xmltodict.parse(xml)

        tags = []
        # Optional if no tags are being sent:
        if parsed_xml['Tagging'].get('TagSet'):
            # If there is only 1 tag, then it's not a list:
            if not isinstance(parsed_xml['Tagging']['TagSet']['Tag'], list):
                tags.append(FakeTag(parsed_xml['Tagging']['TagSet']['Tag']['Key'],
                                    parsed_xml['Tagging']['TagSet']['Tag']['Value']))
            else:
                for tag in parsed_xml['Tagging']['TagSet']['Tag']:
                    tags.append(FakeTag(tag['Key'], tag['Value']))

        tag_set = FakeTagSet(tags)
        tagging = FakeTagging(tag_set)
        return tagging

    def _cors_from_xml(self, xml):
        parsed_xml = xmltodict.parse(xml)

        if isinstance(parsed_xml["CORSConfiguration"]["CORSRule"], list):
            return [cors for cors in parsed_xml["CORSConfiguration"]["CORSRule"]]

        return [parsed_xml["CORSConfiguration"]["CORSRule"]]

    def _logging_from_xml(self, xml):
        parsed_xml = xmltodict.parse(xml)

        if not parsed_xml["BucketLoggingStatus"].get("LoggingEnabled"):
            return {}

        if not parsed_xml["BucketLoggingStatus"]["LoggingEnabled"].get("TargetBucket"):
            raise MalformedXML()

        if not parsed_xml["BucketLoggingStatus"]["LoggingEnabled"].get("TargetPrefix"):
            parsed_xml["BucketLoggingStatus"]["LoggingEnabled"]["TargetPrefix"] = ""

        # Get the ACLs:
        if parsed_xml["BucketLoggingStatus"]["LoggingEnabled"].get("TargetGrants"):
            permissions = [
                "READ",
                "WRITE",
                "FULL_CONTROL"
            ]
            if not isinstance(parsed_xml["BucketLoggingStatus"]["LoggingEnabled"]["TargetGrants"]["Grant"], list):
                target_grants = self._get_grants_from_xml(
                    [parsed_xml["BucketLoggingStatus"]["LoggingEnabled"]["TargetGrants"]["Grant"]],
                    MalformedXML,
                    permissions
                )
            else:
                target_grants = self._get_grants_from_xml(
                    parsed_xml["BucketLoggingStatus"]["LoggingEnabled"]["TargetGrants"]["Grant"],
                    MalformedXML,
                    permissions
                )

            parsed_xml["BucketLoggingStatus"]["LoggingEnabled"]["TargetGrants"] = target_grants

        return parsed_xml["BucketLoggingStatus"]["LoggingEnabled"]

    def _notification_config_from_xml(self, xml):
        parsed_xml = xmltodict.parse(xml)

        if not len(parsed_xml["NotificationConfiguration"]):
            return {}

        # The types of notifications, and their required fields (apparently lambda is categorized by the API as
        # "CloudFunction"):
        notification_fields = [
            ("Topic", "sns"),
            ("Queue", "sqs"),
            ("CloudFunction", "lambda")
        ]

        event_names = [
            's3:ReducedRedundancyLostObject',
            's3:ObjectCreated:*',
            's3:ObjectCreated:Put',
            's3:ObjectCreated:Post',
            's3:ObjectCreated:Copy',
            's3:ObjectCreated:CompleteMultipartUpload',
            's3:ObjectRemoved:*',
            's3:ObjectRemoved:Delete',
            's3:ObjectRemoved:DeleteMarkerCreated'
        ]

        found_notifications = 0  # Tripwire -- if this is not ever set, then there were no notifications
        for name, arn_string in notification_fields:
            # 1st verify that the proper notification configuration has been passed in (with an ARN that is close
            # to being correct -- nothing too complex in the ARN logic):
            the_notification = parsed_xml["NotificationConfiguration"].get("{}Configuration".format(name))
            if the_notification:
                found_notifications += 1
                if not isinstance(the_notification, list):
                    the_notification = parsed_xml["NotificationConfiguration"]["{}Configuration".format(name)] \
                        = [the_notification]

                for n in the_notification:
                    if not n[name].startswith("arn:aws:{}:".format(arn_string)):
                        raise InvalidNotificationARN()

                    # 2nd, verify that the Events list is correct:
                    assert n["Event"]
                    if not isinstance(n["Event"], list):
                        n["Event"] = [n["Event"]]

                    for event in n["Event"]:
                        if event not in event_names:
                            raise InvalidNotificationEvent()

                    # Parse out the filters:
                    if n.get("Filter"):
                        # Error if S3Key is blank:
                        if not n["Filter"]["S3Key"]:
                            raise KeyError()

                        if not isinstance(n["Filter"]["S3Key"]["FilterRule"], list):
                            n["Filter"]["S3Key"]["FilterRule"] = [n["Filter"]["S3Key"]["FilterRule"]]

                        for filter_rule in n["Filter"]["S3Key"]["FilterRule"]:
                            assert filter_rule["Name"] in ["suffix", "prefix"]
                            assert filter_rule["Value"]

        if not found_notifications:
            return {}

        return parsed_xml["NotificationConfiguration"]

    def _accelerate_config_from_xml(self, xml):
        parsed_xml = xmltodict.parse(xml)
        config = parsed_xml['AccelerateConfiguration']
        return config['Status']

    def _key_response_delete(self, bucket_name, query, key_name, headers):
        if query.get('uploadId'):
            upload_id = query['uploadId'][0]
            self.backend.cancel_multipart(bucket_name, upload_id)
            return 204, {}, ""
        version_id = query.get('versionId', [None])[0]
        self.backend.delete_key(bucket_name, key_name, version_id=version_id)
        template = self.response_template(S3_DELETE_OBJECT_SUCCESS)
        return 204, {}, template.render()

    def _complete_multipart_body(self, body):
        ps = minidom.parseString(body).getElementsByTagName('Part')
        prev = 0
        for p in ps:
            pn = int(p.getElementsByTagName(
                'PartNumber')[0].firstChild.wholeText)
            if pn <= prev:
                raise InvalidPartOrder()
            yield (pn, p.getElementsByTagName('ETag')[0].firstChild.wholeText)

    def _key_response_post(self, request, body, bucket_name, query, key_name, headers):
        if body == b'' and 'uploads' in query:
            metadata = metadata_from_headers(request.headers)
            multipart = self.backend.initiate_multipart(
                bucket_name, key_name, metadata)

            template = self.response_template(S3_MULTIPART_INITIATE_RESPONSE)
            response = template.render(
                bucket_name=bucket_name,
                key_name=key_name,
                upload_id=multipart.id,
            )
            return 200, {}, response

        if query.get('uploadId'):
            body = self._complete_multipart_body(body)
            upload_id = query['uploadId'][0]
            key = self.backend.complete_multipart(bucket_name, upload_id, body)
            template = self.response_template(S3_MULTIPART_COMPLETE_RESPONSE)
            return template.render(
                bucket_name=bucket_name,
                key_name=key.name,
                etag=key.etag,
            )
        elif 'restore' in query:
            es = minidom.parseString(body).getElementsByTagName('Days')
            days = es[0].childNodes[0].wholeText
            key = self.backend.get_key(bucket_name, key_name)
            r = 202
            if key.expiry_date is not None:
                r = 200
            key.restore(int(days))
            return r, {}, ""
        else:
            raise NotImplementedError(
                "Method POST had only been implemented for multipart uploads and restore operations, so far")


S3ResponseInstance = ResponseObject(s3_backend)

S3_ALL_BUCKETS = """<ListAllMyBucketsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01">
  <Owner>
    <ID>bcaf1ffd86f41161ca5fb16fd081034f</ID>
    <DisplayName>webfile</DisplayName>
  </Owner>
  <Buckets>
    {% for bucket in buckets %}
      <Bucket>
        <Name>{{ bucket.name }}</Name>
        <CreationDate>2006-02-03T16:45:09.000Z</CreationDate>
      </Bucket>
    {% endfor %}
 </Buckets>
</ListAllMyBucketsResult>"""

S3_BUCKET_GET_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>{{ bucket.name }}</Name>
  <Prefix>{{ prefix }}</Prefix>
  <MaxKeys>{{ max_keys }}</MaxKeys>
  <Delimiter>{{ delimiter }}</Delimiter>
  <IsTruncated>{{ is_truncated }}</IsTruncated>
  {% for key in result_keys %}
    <Contents>
      <Key>{{ key.name }}</Key>
      <LastModified>{{ key.last_modified_ISO8601 }}</LastModified>
      <ETag>{{ key.etag }}</ETag>
      <Size>{{ key.size }}</Size>
      <StorageClass>{{ key.storage_class }}</StorageClass>
      <Owner>
        <ID>75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a</ID>
        <DisplayName>webfile</DisplayName>
      </Owner>
    </Contents>
  {% endfor %}
  {% if delimiter %}
    {% for folder in result_folders %}
      <CommonPrefixes>
        <Prefix>{{ folder }}</Prefix>
      </CommonPrefixes>
    {% endfor %}
  {% endif %}
  </ListBucketResult>"""

S3_BUCKET_GET_RESPONSE_V2 = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>{{ bucket.name }}</Name>
  <Prefix>{{ prefix }}</Prefix>
  <MaxKeys>{{ max_keys }}</MaxKeys>
  <KeyCount>{{ result_keys | length }}</KeyCount>
{% if delimiter %}
  <Delimiter>{{ delimiter }}</Delimiter>
{% endif %}
  <IsTruncated>{{ is_truncated }}</IsTruncated>
{% if next_continuation_token %}
  <NextContinuationToken>{{ next_continuation_token }}</NextContinuationToken>
{% endif %}
{% if start_after %}
  <StartAfter>{{ start_after }}</StartAfter>
{% endif %}
  {% for key in result_keys %}
    <Contents>
      <Key>{{ key.name }}</Key>
      <LastModified>{{ key.last_modified_ISO8601 }}</LastModified>
      <ETag>{{ key.etag }}</ETag>
      <Size>{{ key.size }}</Size>
      <StorageClass>{{ key.storage_class }}</StorageClass>
      {% if fetch_owner %}
      <Owner>
        <ID>75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a</ID>
        <DisplayName>webfile</DisplayName>
      </Owner>
      {% endif %}
    </Contents>
  {% endfor %}
  {% if delimiter %}
    {% for folder in result_folders %}
      <CommonPrefixes>
        <Prefix>{{ folder }}</Prefix>
      </CommonPrefixes>
    {% endfor %}
  {% endif %}
  </ListBucketResult>"""

S3_BUCKET_CREATE_RESPONSE = """<CreateBucketResponse xmlns="http://s3.amazonaws.com/doc/2006-03-01">
  <CreateBucketResponse>
    <Bucket>{{ bucket.name }}</Bucket>
  </CreateBucketResponse>
</CreateBucketResponse>"""

S3_DELETE_BUCKET_SUCCESS = """<DeleteBucketResponse xmlns="http://s3.amazonaws.com/doc/2006-03-01">
  <DeleteBucketResponse>
    <Code>204</Code>
    <Description>No Content</Description>
  </DeleteBucketResponse>
</DeleteBucketResponse>"""

S3_DELETE_BUCKET_WITH_ITEMS_ERROR = """<?xml version="1.0" encoding="UTF-8"?>
<Error><Code>BucketNotEmpty</Code>
<Message>The bucket you tried to delete is not empty</Message>
<BucketName>{{ bucket.name }}</BucketName>
<RequestId>asdfasdfsdafds</RequestId>
<HostId>sdfgdsfgdsfgdfsdsfgdfs</HostId>
</Error>"""

S3_BUCKET_LOCATION = """<?xml version="1.0" encoding="UTF-8"?>
<LocationConstraint xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{% if location != None %}{{ location }}{% endif %}</LocationConstraint>"""

S3_BUCKET_LIFECYCLE_CONFIGURATION = """<?xml version="1.0" encoding="UTF-8"?>
<LifecycleConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    {% for rule in rules %}
    <Rule>
        <ID>{{ rule.id }}</ID>
        {% if rule.filter %}
        <Filter>
            <Prefix>{{ rule.filter.prefix }}</Prefix>
            {% if rule.filter.tag %}
            <Tag>
                <Key>{{ rule.filter.tag.key }}</Key>
                <Value>{{ rule.filter.tag.value }}</Value>
            </Tag>
            {% endif %}
            {% if rule.filter.and_filter %}
            <And>
                <Prefix>{{ rule.filter.and_filter.prefix }}</Prefix>
                {% for tag in rule.filter.and_filter.tags %}
                <Tag>
                    <Key>{{ tag.key }}</Key>
                    <Value>{{ tag.value }}</Value>
                </Tag>
                {% endfor %}
            </And>
            {% endif %}
        </Filter>
        {% else %}
        <Prefix>{{ rule.prefix if rule.prefix != None }}</Prefix>
        {% endif %}
        <Status>{{ rule.status }}</Status>
        {% if rule.storage_class %}
        <Transition>
            {% if rule.transition_days %}
               <Days>{{ rule.transition_days }}</Days>
            {% endif %}
            {% if rule.transition_date %}
               <Date>{{ rule.transition_date }}</Date>
            {% endif %}
           <StorageClass>{{ rule.storage_class }}</StorageClass>
        </Transition>
        {% endif %}
        {% if rule.expiration_days or rule.expiration_date or rule.expired_object_delete_marker %}
        <Expiration>
            {% if rule.expiration_days %}
               <Days>{{ rule.expiration_days }}</Days>
            {% endif %}
            {% if rule.expiration_date %}
               <Date>{{ rule.expiration_date }}</Date>
            {% endif %}
            {% if rule.expired_object_delete_marker %}
                <ExpiredObjectDeleteMarker>{{ rule.expired_object_delete_marker }}</ExpiredObjectDeleteMarker>
            {% endif %}
        </Expiration>
        {% endif %}
        {% if rule.nvt_noncurrent_days and rule.nvt_storage_class %}
        <NoncurrentVersionTransition>
           <NoncurrentDays>{{ rule.nvt_noncurrent_days }}</NoncurrentDays>
           <StorageClass>{{ rule.nvt_storage_class }}</StorageClass>
        </NoncurrentVersionTransition>
        {% endif %}
        {% if rule.nve_noncurrent_days %}
        <NoncurrentVersionExpiration>
           <NoncurrentDays>{{ rule.nve_noncurrent_days }}</NoncurrentDays>
        </NoncurrentVersionExpiration>
        {% endif %}
        {% if rule.aimu_days %}
        <AbortIncompleteMultipartUpload>
           <DaysAfterInitiation>{{ rule.aimu_days }}</DaysAfterInitiation>
        </AbortIncompleteMultipartUpload>
        {% endif %}
    </Rule>
    {% endfor %}
</LifecycleConfiguration>
"""

S3_BUCKET_VERSIONING = """<?xml version="1.0" encoding="UTF-8"?>
<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Status>{{ bucket_versioning_status }}</Status>
</VersioningConfiguration>
"""

S3_BUCKET_GET_VERSIONING = """<?xml version="1.0" encoding="UTF-8"?>
{% if status is none %}
    <VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>
{% else %}
    <VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <Status>{{ status }}</Status>
    </VersioningConfiguration>
{% endif %}
"""

S3_BUCKET_GET_VERSIONS = """<?xml version="1.0" encoding="UTF-8"?>
<ListVersionsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01">
    <Name>{{ bucket.name }}</Name>
    <Prefix>{{ prefix }}</Prefix>
    <KeyMarker>{{ key_marker }}</KeyMarker>
    <MaxKeys>{{ max_keys }}</MaxKeys>
    <IsTruncated>{{ is_truncated }}</IsTruncated>
    {% for key in key_list %}
    <Version>
        <Key>{{ key.name }}</Key>
        <VersionId>{% if key.version_id is none %}null{% else %}{{ key.version_id }}{% endif %}</VersionId>
        <IsLatest>{% if latest_versions[key.name] == key.version_id %}true{% else %}false{% endif %}</IsLatest>
        <LastModified>{{ key.last_modified_ISO8601 }}</LastModified>
        <ETag>{{ key.etag }}</ETag>
        <Size>{{ key.size }}</Size>
        <StorageClass>{{ key.storage_class }}</StorageClass>
        <Owner>
            <ID>75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a</ID>
            <DisplayName>webfile</DisplayName>
        </Owner>
    </Version>
    {% endfor %}
    {% for marker in delete_marker_list %}
    <DeleteMarker>
        <Key>{{ marker.name }}</Key>
        <VersionId>{{ marker.version_id }}</VersionId>
        <IsLatest>{% if latest_versions[marker.name] == marker.version_id %}true{% else %}false{% endif %}</IsLatest>
        <LastModified>{{ marker.last_modified_ISO8601 }}</LastModified>
        <Owner>
            <ID>75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a</ID>
            <DisplayName>webfile</DisplayName>
        </Owner>
    </DeleteMarker>
    {% endfor %}
</ListVersionsResult>
"""

S3_DELETE_KEYS_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01">
{% for k in deleted %}
<Deleted>
<Key>{{k}}</Key>
</Deleted>
{% endfor %}
{% for k in delete_errors %}
<Error>
<Key>{{k}}</Key>
</Error>
{% endfor %}
</DeleteResult>"""

S3_DELETE_OBJECT_SUCCESS = """<DeleteObjectResponse xmlns="http://s3.amazonaws.com/doc/2006-03-01">
  <DeleteObjectResponse>
    <Code>200</Code>
    <Description>OK</Description>
  </DeleteObjectResponse>
</DeleteObjectResponse>"""

S3_OBJECT_RESPONSE = """<PutObjectResponse xmlns="http://s3.amazonaws.com/doc/2006-03-01">
      <PutObjectResponse>
        <ETag>{{ key.etag }}</ETag>
        <LastModified>{{ key.last_modified_ISO8601 }}</LastModified>
      </PutObjectResponse>
    </PutObjectResponse>"""

S3_OBJECT_ACL_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
    <AccessControlPolicy xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Owner>
        <ID>75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a</ID>
        <DisplayName>webfile</DisplayName>
      </Owner>
      <AccessControlList>
        {% for grant in obj.acl.grants %}
        <Grant>
          {% for grantee in grant.grantees %}
          <Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                   xsi:type="{{ grantee.type }}">
            {% if grantee.uri %}
            <URI>{{ grantee.uri }}</URI>
            {% endif %}
            {% if grantee.id %}
            <ID>{{ grantee.id }}</ID>
            {% endif %}
            {% if grantee.display_name %}
            <DisplayName>{{ grantee.display_name }}</DisplayName>
            {% endif %}
          </Grantee>
          {% endfor %}
          {% for permission in grant.permissions %}
          <Permission>{{ permission }}</Permission>
          {% endfor %}
        </Grant>
        {% endfor %}
      </AccessControlList>
    </AccessControlPolicy>"""

S3_OBJECT_TAGGING_RESPONSE = """\
<?xml version="1.0" encoding="UTF-8"?>
<Tagging xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <TagSet>
    {% for tag in obj.tagging.tag_set.tags %}
    <Tag>
      <Key>{{ tag.key }}</Key>
      <Value>{{ tag.value }}</Value>
    </Tag>
    {% endfor %}
  </TagSet>
</Tagging>"""

S3_BUCKET_TAGGING_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<Tagging>
  <TagSet>
    {% for tag in bucket.tagging.tag_set.tags %}
    <Tag>
      <Key>{{ tag.key }}</Key>
      <Value>{{ tag.value }}</Value>
    </Tag>
    {% endfor %}
  </TagSet>
</Tagging>"""

S3_BUCKET_CORS_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<CORSConfiguration>
  {% for cors in bucket.cors %}
  <CORSRule>
    {% for origin in cors.allowed_origins %}
    <AllowedOrigin>{{ origin }}</AllowedOrigin>
    {% endfor %}
    {% for method in cors.allowed_methods %}
    <AllowedMethod>{{ method }}</AllowedMethod>
    {% endfor %}
    {% if cors.allowed_headers is not none %}
      {% for header in cors.allowed_headers %}
      <AllowedHeader>{{ header }}</AllowedHeader>
      {% endfor %}
    {% endif %}
    {% if cors.exposed_headers is not none %}
      {% for header in cors.exposed_headers %}
      <ExposedHeader>{{ header }}</ExposedHeader>
      {% endfor %}
    {% endif %}
    {% if cors.max_age_seconds is not none %}
    <MaxAgeSeconds>{{ cors.max_age_seconds }}</MaxAgeSeconds>
    {% endif %}
  </CORSRule>
  {% endfor %}
  </CORSConfiguration>
"""

S3_OBJECT_COPY_RESPONSE = """\
<CopyObjectResult xmlns="http://doc.s3.amazonaws.com/2006-03-01">
    <ETag>{{ key.etag }}</ETag>
    <LastModified>{{ key.last_modified_ISO8601 }}</LastModified>
</CopyObjectResult>"""

S3_MULTIPART_INITIATE_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<InitiateMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Bucket>{{ bucket_name }}</Bucket>
  <Key>{{ key_name }}</Key>
  <UploadId>{{ upload_id }}</UploadId>
</InitiateMultipartUploadResult>"""

S3_MULTIPART_UPLOAD_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<CopyPartResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <LastModified>{{ part.last_modified_ISO8601 }}</LastModified>
  <ETag>{{ part.etag }}</ETag>
</CopyPartResult>"""

S3_MULTIPART_LIST_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<ListPartsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Bucket>{{ bucket_name }}</Bucket>
  <Key>{{ key_name }}</Key>
  <UploadId>{{ upload_id }}</UploadId>
  <StorageClass>STANDARD</StorageClass>
  <Initiator>
    <ID>75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a</ID>
    <DisplayName>webfile</DisplayName>
  </Initiator>
  <Owner>
    <ID>75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a</ID>
    <DisplayName>webfile</DisplayName>
  </Owner>
  <StorageClass>STANDARD</StorageClass>
  <PartNumberMarker>1</PartNumberMarker>
  <NextPartNumberMarker>{{ count }}</NextPartNumberMarker>
  <MaxParts>{{ count }}</MaxParts>
  <IsTruncated>false</IsTruncated>
  {% for part in parts %}
  <Part>
    <PartNumber>{{ part.name }}</PartNumber>
    <LastModified>{{ part.last_modified_ISO8601 }}</LastModified>
    <ETag>{{ part.etag }}</ETag>
    <Size>{{ part.size }}</Size>
  </Part>
  {% endfor %}
</ListPartsResult>"""

S3_MULTIPART_COMPLETE_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<CompleteMultipartUploadResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Location>http://{{ bucket_name }}.s3.amazonaws.com/{{ key_name }}</Location>
  <Bucket>{{ bucket_name }}</Bucket>
  <Key>{{ key_name }}</Key>
  <ETag>{{ etag }}</ETag>
</CompleteMultipartUploadResult>
"""

S3_ALL_MULTIPARTS = """<?xml version="1.0" encoding="UTF-8"?>
<ListMultipartUploadsResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Bucket>{{ bucket_name }}</Bucket>
  <KeyMarker></KeyMarker>
  <UploadIdMarker></UploadIdMarker>
  <MaxUploads>1000</MaxUploads>
  <IsTruncated>False</IsTruncated>
  {% for upload in uploads %}
  <Upload>
    <Key>{{ upload.key_name }}</Key>
    <UploadId>{{ upload.id }}</UploadId>
    <Initiator>
      <ID>arn:aws:iam::123456789012:user/user1-11111a31-17b5-4fb7-9df5-b111111f13de</ID>
      <DisplayName>user1-11111a31-17b5-4fb7-9df5-b111111f13de</DisplayName>
    </Initiator>
    <Owner>
      <ID>75aa57f09aa0c8caeab4f8c24e99d10f8e7faeebf76c078efc7c6caea54ba06a</ID>
      <DisplayName>webfile</DisplayName>
    </Owner>
    <StorageClass>STANDARD</StorageClass>
    <Initiated>2010-11-10T20:48:33.000Z</Initiated>
  </Upload>
  {% endfor %}
</ListMultipartUploadsResult>
"""

S3_NO_POLICY = """<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>NoSuchBucketPolicy</Code>
  <Message>The bucket policy does not exist</Message>
  <BucketName>{{ bucket_name }}</BucketName>
  <RequestId>0D68A23BB2E2215B</RequestId>
  <HostId>9Gjjt1m+cjU4OPvX9O9/8RuvnG41MRb/18Oux2o5H5MY7ISNTlXN+Dz9IG62/ILVxhAGI0qyPfg=</HostId>
</Error>
"""

S3_NO_LIFECYCLE = """<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>NoSuchLifecycleConfiguration</Code>
  <Message>The lifecycle configuration does not exist</Message>
  <BucketName>{{ bucket_name }}</BucketName>
  <RequestId>44425877V1D0A2F9</RequestId>
  <HostId>9Gjjt1m+cjU4OPvX9O9/8RuvnG41MRb/18Oux2o5H5MY7ISNTlXN+Dz9IG62/ILVxhAGI0qyPfg=</HostId>
</Error>
"""

S3_NO_BUCKET_TAGGING = """<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>NoSuchTagSet</Code>
  <Message>The TagSet does not exist</Message>
  <BucketName>{{ bucket_name }}</BucketName>
  <RequestId>44425877V1D0A2F9</RequestId>
  <HostId>9Gjjt1m+cjU4OPvX9O9/8RuvnG41MRb/18Oux2o5H5MY7ISNTlXN+Dz9IG62/ILVxhAGI0qyPfg=</HostId>
</Error>
"""

S3_NO_BUCKET_WEBSITE_CONFIG = """<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>NoSuchWebsiteConfiguration</Code>
  <Message>The specified bucket does not have a website configuration</Message>
  <BucketName>{{ bucket_name }}</BucketName>
  <RequestId>44425877V1D0A2F9</RequestId>
  <HostId>9Gjjt1m+cjU4OPvX9O9/8RuvnG41MRb/18Oux2o5H5MY7ISNTlXN+Dz9IG62/ILVxhAGI0qyPfg=</HostId>
</Error>
"""

S3_INVALID_CORS_REQUEST = """<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>NoSuchWebsiteConfiguration</Code>
  <Message>The specified bucket does not have a website configuration</Message>
  <BucketName>{{ bucket_name }}</BucketName>
  <RequestId>44425877V1D0A2F9</RequestId>
  <HostId>9Gjjt1m+cjU4OPvX9O9/8RuvnG41MRb/18Oux2o5H5MY7ISNTlXN+Dz9IG62/ILVxhAGI0qyPfg=</HostId>
</Error>
"""

S3_NO_CORS_CONFIG = """<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>NoSuchCORSConfiguration</Code>
  <Message>The CORS configuration does not exist</Message>
  <BucketName>{{ bucket_name }}</BucketName>
  <RequestId>44425877V1D0A2F9</RequestId>
  <HostId>9Gjjt1m+cjU4OPvX9O9/8RuvnG41MRb/18Oux2o5H5MY7ISNTlXN+Dz9IG62/ILVxhAGI0qyPfg=</HostId>
</Error>
"""

S3_LOGGING_CONFIG = """<?xml version="1.0" encoding="UTF-8"?>
<BucketLoggingStatus xmlns="http://doc.s3.amazonaws.com/2006-03-01">
  <LoggingEnabled>
    <TargetBucket>{{ logging["TargetBucket"] }}</TargetBucket>
    <TargetPrefix>{{ logging["TargetPrefix"] }}</TargetPrefix>
    {% if logging.get("TargetGrants") %}
    <TargetGrants>
      {% for grant in logging["TargetGrants"] %}
      <Grant>
        <Grantee xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                 xsi:type="{{ grant.grantees[0].type }}">
          {% if grant.grantees[0].uri %}
          <URI>{{ grant.grantees[0].uri }}</URI>
          {% endif %}
          {% if grant.grantees[0].id %}
          <ID>{{ grant.grantees[0].id }}</ID>
          {% endif %}
          {% if grant.grantees[0].display_name %}
          <DisplayName>{{ grant.grantees[0].display_name }}</DisplayName>
          {% endif %}
        </Grantee>
        <Permission>{{ grant.permissions[0] }}</Permission>
      </Grant>
      {% endfor %}
    </TargetGrants>
    {% endif %}
  </LoggingEnabled>
</BucketLoggingStatus>
"""

S3_NO_LOGGING_CONFIG = """<?xml version="1.0" encoding="UTF-8"?>
<BucketLoggingStatus xmlns="http://doc.s3.amazonaws.com/2006-03-01" />
"""

S3_GET_BUCKET_NOTIFICATION_CONFIG = """<?xml version="1.0" encoding="UTF-8"?>
<NotificationConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  {% for topic in bucket.notification_configuration.topic %}
  <TopicConfiguration>
    <Id>{{ topic.id }}</Id>
    <Topic>{{ topic.arn }}</Topic>
    {% for event in topic.events %}
    <Event>{{ event }}</Event>
    {% endfor %}
    {% if topic.filters %}
      <Filter>
        <S3Key>
          {% for rule in topic.filters["S3Key"]["FilterRule"] %}
          <FilterRule>
            <Name>{{ rule["Name"] }}</Name>
            <Value>{{ rule["Value"] }}</Value>
          </FilterRule>
          {% endfor %}
        </S3Key>
      </Filter>
    {% endif %}
  </TopicConfiguration>
  {% endfor %}
  {% for queue in bucket.notification_configuration.queue %}
  <QueueConfiguration>
    <Id>{{ queue.id }}</Id>
    <Queue>{{ queue.arn }}</Queue>
    {% for event in queue.events %}
    <Event>{{ event }}</Event>
    {% endfor %}
    {% if queue.filters %}
      <Filter>
        <S3Key>
          {% for rule in queue.filters["S3Key"]["FilterRule"] %}
          <FilterRule>
            <Name>{{ rule["Name"] }}</Name>
            <Value>{{ rule["Value"] }}</Value>
          </FilterRule>
          {% endfor %}
        </S3Key>
      </Filter>
    {% endif %}
  </QueueConfiguration>
  {% endfor %}
  {% for cf in bucket.notification_configuration.cloud_function %}
  <CloudFunctionConfiguration>
    <Id>{{ cf.id }}</Id>
    <CloudFunction>{{ cf.arn }}</CloudFunction>
    {% for event in cf.events %}
    <Event>{{ event }}</Event>
    {% endfor %}
    {% if cf.filters %}
      <Filter>
        <S3Key>
          {% for rule in cf.filters["S3Key"]["FilterRule"] %}
          <FilterRule>
            <Name>{{ rule["Name"] }}</Name>
            <Value>{{ rule["Value"] }}</Value>
          </FilterRule>
          {% endfor %}
        </S3Key>
      </Filter>
    {% endif %}
  </CloudFunctionConfiguration>
  {% endfor %}
</NotificationConfiguration>
"""

S3_BUCKET_ACCELERATE = """
<AccelerateConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Status>{{ bucket.accelerate_configuration }}</Status>
</AccelerateConfiguration>
"""

S3_BUCKET_ACCELERATE_NOT_SET = """
<AccelerateConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>
"""
