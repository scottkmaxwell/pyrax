 #!/usr/bin/env python
# -*- coding: utf-8 -*-

import httplib
import math
import os
import re
import socket
import urllib
import urlparse

from swiftclient import client as _swift_client
from pyrax.cf_wrapper.container import Container
from pyrax.cf_wrapper.storage_object import StorageObject
import pyrax.common.utils as utils
import pyrax.exceptions as exc


CONNECTION_TIMEOUT = 5


import pudb
trace = pudb.set_trace


no_such_container_pattern = re.compile(r"Container GET|HEAD failed: .+/(.+) 404")


def handle_swiftclient_exception(fnc):
    def _wrapped(*args, **kwargs):
        try:
            return fnc(*args, **kwargs)
        except _swift_client.ClientException as e:
            str_error = "%s" % e
            bad_container = no_such_container_pattern.search(str_error)
            if bad_container:
                raise exc.NoSuchContainer("Container '%s' doesn't exist" % bad_container.groups()[0])
            # Not handled; re-raise
            raise
    return _wrapped


class Client(object):
    """
    Wraps the calls to swiftclient with objects representing Containers and StorageObjects.

    These classes allow a developer to work with regular Python objects instead of 
    calling functions.
    """
    # Constants used in metadata headers
    account_meta_prefix = "X-Account-"
    container_meta_prefix = "X-Container-Meta-"
    object_meta_prefix = "X-Object-Meta-"
    cdn_meta_prefix = "X-Cdn-"
    # Defaults for CDN
    cdn_enabled = False
    default_cdn_ttl = 86400
    _container_cache = {}
    # Upload size limit
    max_file_size = 5368709119  # 5GB - 1


    def __init__(self, auth_endpoint, username, api_key, tenant_name,
            preauthurl=None, preauthtoken=None, auth_version="2",
            os_options=None):
         self.connection = None
         self._make_connections(auth_endpoint,
                username, api_key, tenant_name, preauthurl=preauthurl, preauthtoken=preauthtoken,
                auth_version=auth_version, os_options=os_options)


    def _make_connections(self, auth_endpoint, username, api_key, tenant_name,
            preauthurl=None, preauthtoken=None, auth_version="2", os_options=None):
        cdn_url = os_options.pop("object_cdn_url", None)
        self.connection = Connection(auth_endpoint, username, api_key, tenant_name,
                preauthurl=preauthurl, preauthtoken=preauthtoken, auth_version=auth_version,
                os_options=os_options)
        self.connection._make_cdn_connection(cdn_url)


    def _ensure_prefix(self, dct, prfx):
        """
        Returns a copy of the supplied dictionary, prefixing
        any keys that do not begin with the specified prefix accordingly.
        """
        lowprefix = prfx.lower()
        ret = {}
        for k, v in dct.iteritems():
            if not k.lower().startswith(lowprefix):
                k = "%s%s" % (prfx, k)
            ret[k] = v
        return ret


    def _resolve_name(self, val):
        return val if isinstance(val, basestring) else val.name


    @handle_swiftclient_exception
    def get_account_metadata(self):
        headers = self.connection.head_account()
        prfx = self.account_meta_prefix.lower()
        ret = {}
        for hkey, hval in headers.iteritems():
            if hkey.lower().startswith(prfx):
                ret[hkey] = hval
        return ret


    @handle_swiftclient_exception
    def get_container_metadata(self, container):
        """Returns a dictionary containing the metadata for the container."""
        cname = self._resolve_name(container)
        headers = self.connection.head_container(cname)
        prfx = self.container_meta_prefix.lower()
        ret = {}
        for hkey, hval in headers.iteritems():
            if hkey.lower().startswith(prfx):
                ret[hkey] = hval
        return ret


    @handle_swiftclient_exception
    def set_container_metadata(self, container, metadata, clear=False):
        """
        Accepts a dictionary of metadata key/value pairs and updates
        the specified container metadata with them.

        If 'clear' is True, any existing metadata is deleted and only
        the passed metadata is retained. Otherwise, the values passed
        here update the container's metadata.
        """
        # Add the metadata prefix, if needed.
        massaged = self._ensure_prefix(metadata, self.container_meta_prefix)
        cname = self._resolve_name(container)
        new_meta = {}
        if clear:
            curr_meta = self.get_container_metadata(cname)
            for ckey in curr_meta:
                new_meta[ckey] = ""
        new_meta.update(massaged)
        self.connection.post_container(cname, new_meta)


    @handle_swiftclient_exception
    def get_container_cdn_metadata(self, container):
        """Returns a dictionary containing the CDN metadata for the container."""
        cname = self._resolve_name(container)
        response = self.connection.cdn_request("HEAD", [cname])
        headers = response.getheaders()
        # headers is a list of 2-tuples instead of a dict.
        return dict(headers)


    @handle_swiftclient_exception
    def set_container_cdn_metadata(self, container, metadata):
        """
        Accepts a dictionary of metadata key/value pairs and updates
        the specified container metadata with them.

        NOTE: arbitrary metadata headers are not allowed. The only metadata
        you can update are: X-Log-Retention, X-CDN-enabled, and X-TTL.
        """
        ct = self.get_container(container)
        allowed = ("x-log-retention", "x-cdn-enabled", "x-ttl")
        hdrs = {}
        bad = []
        for mkey, mval in metadata.iteritems():
            if mkey.lower() not in allowed:
                bad.append(mkey)
                continue
            hdrs[mkey] = str(mval)
        if bad:
            raise exc.InvalidCDNMetada("The only CDN metadata you can update are: X-Log-Retention, X-CDN-enabled, and X-TTL. "
                    "Received the following illegal item(s): %s" % ", ".join(bad))
        self.connection.cdn_request("POST", [ct.name], hdrs=hdrs)


    @handle_swiftclient_exception
    def get_object_metadata(self, container, obj):
        """Retrieves any metadata for the specified object."""
        cname = self._resolve_name(container)
        oname = self._resolve_name(obj)
        headers = self.connection.head_object(cname, oname)
        prfx = self.object_meta_prefix.lower()
        ret = {}
        for hkey, hval in headers.iteritems():
            if hkey.lower().startswith(prfx):
                ret[hkey] = hval
        return ret


    @handle_swiftclient_exception
    def set_object_metadata(self, container, obj, metadata, clear=False):
        """
        Accepts a dictionary of metadata key/value pairs and updates
        the specified object metadata with them.

        If 'clear' is True, any existing metadata is deleted and only
        the passed metadata is retained. Otherwise, the values passed
        here update the object's metadata.
        """
        # Add the metadata prefix, if needed.
        massaged = self._ensure_prefix(metadata, self.object_meta_prefix)
        cname = self._resolve_name(container)
        oname = self._resolve_name(obj)
        new_meta = {}
        # Note that the API for object POST is the opposite of that for
        # container POST: for objects, all current metadata is deleted,
        # whereas for containers you need to set the values to an empty
        # string to delete them.
        if not clear:
            new_meta = self.get_object_metadata(cname, oname)
        new_meta.update(massaged)
        self.connection.post_object(cname, oname, new_meta)


    @handle_swiftclient_exception
    def create_container(self, name):
        """Creates a container with the specified name."""
        self.connection.put_container(name)
        return self.get_container(name)


    @handle_swiftclient_exception
    def delete_container(self, container, del_objects=False):
        cname = self._resolve_name(container)
        if del_objects:
            objs = self.get_container_object_names(cname)
            for obj in objs:
                self.delete_object(cname, obj)
        self.connection.delete_container(cname)
        return True


    @handle_swiftclient_exception
    def delete_object(self, container, name):
        ct = self.get_container(container)
        oname = self._resolve_name(name)
        self.connection.delete_object(ct.name, oname)
        return True
 

    @handle_swiftclient_exception
    def purge_cdn_object(self, container, name, email_addresses=[]):
        ct = self.get_container(container)
        oname = self._resolve_name(name)
        if not ct.cdn_enabled:
            raise exc.NotCDNEnabled("The object '%s' is not in a CDN-enabled container." % oname)
        hdrs = {}
        if email_addresses:
            if not isinstance(email_addresses, (list, tuple)):
                email_addresses = [email_addresses]
            emls = ", ".join(email_addresses)
            hdrs = {"X-Purge-Email": emls}
        self.connection.cdn_request("DELETE", ct.name, oname, hdrs=hdrs)
        return True


    def get_object(self, container, obj_name):
        """Returns a StorageObject instance for the object in the container."""
        cont = self.get_container(container)
        obj = cont.get_object(self._resolve_name(obj_name))
        return obj


    def store_object(self, container, obj_name, data, content_type=None):
        """
        Creates a new object in the specified container, and populates it with
        the given data.
        """
        cont = self.get_container(container)
        return self.connection.put_object(cont.name, obj_name, contents=data,
                content_type=content_type)


    def copy_object(self, container, obj_name, new_container, new_obj_name=None):
        """
        Copies the object to the new container, optionally giving it a new name.
        If you copy to the same container, you must supply a different name.
        """
        cont = self.get_container(container)
        obj = self.get_object(cont, obj_name)
        new_cont = self.get_container(new_container)
        if new_obj_name is None:
            new_obj_name = obj.name
        hdrs = {"X-Copy-From": "/%s/%s" % (cont.name, obj.name)}
        return self.connection.put_object(new_cont.name, new_obj_name, contents=None,
                headers=hdrs)


    def move_object(self, container, obj_name, new_container, new_obj_name=None):
        """
        Works just like copy_object, except that the source object is deleted
        after a succesful copy.
        """
        new_obj_hash = self.copy_object(container, obj_name, new_container,
                new_obj_name=new_obj_name)
        if new_obj_hash:
            # Copy succeeded; delete the original.
            self.delete_object(container, obj_name)
        return new_obj_hash


    def upload_file(self, container, file_or_path, obj_name=None, content_type=None):
        """
        Uploads the specified file to the container. If no name is supplied, the
        file's name will be used. Either a file path or an open file-like object
        may be supplied.
        """
        cont = self.get_container(container)

        def get_file_size(fileobj):
            """Returns the size of a file-like object."""
            currpos = fileobj.tell()
            fileobj.seek(0, 2)
            total_size = fileobj.tell()
            fileobj.seek(currpos)
            return total_size

        def upload(fileobj):
            fsize = get_file_size(fileobj)
            if fsize < self.max_file_size:
                # We can just upload it as-is.
                return self.connection.put_object(cont.name, obj_name, contents=fileobj,
                        content_type=content_type)
            # Files larger than self.max_file_size must be segmented
            # and uploaded separately.
            num_segments = int(math.ceil(float(fsize) / self.max_file_size))
            digits = int(math.log10(num_segments)) + 1
            # NOTE: This could be greatly improved with threading or other async design.
            for segment in xrange(num_segments):
                sequence = str(segment + 1).zfill(digits)
                seg_name = "%s.%s" % (fname, sequence)
                with utils.SelfDeletingTempfile() as tmpname:
                    with file(tmpname, "wb") as tmp:
                        tmp.write(fileobj.read(self.max_file_size))
                    with file(tmpname, "rb") as tmp:
                        self.connection.put_object(cont.name, seg_name,
                                contents=tmp, content_type=content_type)
            # Upload the manifest
            hdr = {"X-Object-Meta-Manifest": "%s." % fname}
            return self.connection.put_object(cont.name, fname,
                    contents=None, headers=hdr)
            
        ispath = isinstance(file_or_path, basestring)
        if ispath:
            # Make sure it exists
            if not os.path.exists(file_or_path):
                raise exc.FileNotFound("The file '%s' does not exist" % file_or_path)
            fname = os.path.basename(file_or_path)
        else:
            fname = file_or_path.name
        if not obj_name:
            obj_name = fname

        if ispath:
            # Need to wrap the call in a context manager
            with file(file_or_path, "rb") as ff:
                return upload(ff)
        else:
            return upload(file_or_path)


    def fetch_object(self, container, obj_name, include_meta=False, chunk_size=None):
        """
        Fetches the object from storage.

        If 'include_meta' is False, only the bytes representing the
        file is returned.

        Note: if 'chunk_size' is defined, you must fully read the object's
        contents before making another request.
        
        When 'include_meta' is True, what is returned from this method is a 2-tuple:
            Element 0: a dictionary containing metadata about the file.
            Element 1: a stream of bytes representing the object's contents.
        """
        cname = self._resolve_name(container)
        oname = self._resolve_name(obj_name)
        (meta, data) = self.connection.get_object(cname, oname, resp_chunk_size=chunk_size)
        if include_meta:
            return (meta, data)
        else:
            return data


    @handle_swiftclient_exception
    def get_all_containers(self, limit=None, marker=None, **parms):
        hdrs, conts = self.connection.get_container("")
        ret = [Container(self, name=cont["name"], object_count=cont["count"],
                total_bytes=cont["bytes"]) for cont in conts]
        return ret


    @handle_swiftclient_exception
    def get_container(self, container):
        cname = self._resolve_name(container)
        if not cname:
            raise exc.MissingName("No container name specified")
        cont = self._container_cache.get(cname)
        if not cont:
            hdrs = self.connection.head_container(cname)
            cont = Container(self, name=cname, object_count=hdrs.get("x-container-object-count"),
                    total_bytes=hdrs.get("x-container-bytes-used"))
            self._container_cache[cname] = cont
        return cont


    @handle_swiftclient_exception
    def get_container_objects(self, container, marker=None, limit=None, prefix=None,
            delimiter=None, full_listing=False):
        """
        Return a list of StorageObjects representing the objects in the container.
        You can use the marker and limit params to handle pagination, and the prefix
        and delimiter params to filter the objects returned. Also, by default only
        the first 10,000 objects are returned; if you set full_listing to True, all
        objects in the container are returned.
        """
        cname = self._resolve_name(container)
        hdrs, objs = self.connection.get_container(cname, marker=marker, limit=limit,
                prefix=prefix, delimiter=delimiter, full_listing=full_listing)
        cont = self.get_container(cname)
        return [StorageObject(self, container=cont, attdict=obj) for obj in objs
                if "name" in obj]


    @handle_swiftclient_exception
    def get_container_object_names(self, container):
        cname = self._resolve_name(container)
        hdrs, objs = self.connection.get_container(cname)
        cont = self.get_container(cname)
        return [obj["name"] for obj in objs]


    @handle_swiftclient_exception
    def get_info(self):
        """Return tuple for number of containers and total bytes in the account."""
        hdrs = self.connection.head_container("")
        return (hdrs["x-account-container-count"], hdrs["x-account-bytes-used"])


    def get_container_streaming_uri(self, container):
        """Returns the URI for streaming content, or None if CDN is not enabled."""
        cont = self.get_container(container)
        return cont.cdn_streaming_uri


    @handle_swiftclient_exception
    def list_containers(self, limit=None, marker=None, **parms):
        """Returns a list of all container names as strings."""
        hdrs, conts = self.connection.get_container("")
        ret = [cont["name"] for cont in conts]
        return ret


    @handle_swiftclient_exception
    def list_containers_info(self, limit=None, marker=None, **parms):
        """Returns a list of info on Containers.
        
        For each container, a dict containing the following keys is returned:
            name - the name of the container
            count - the number of objects in the container
            bytes - the total bytes in the container
        """
        hdrs, conts = self.connection.get_container("")
        return conts


    @handle_swiftclient_exception
    def list_public_containers(self):
        """Returns a list of all CDN-enabled containers."""
        response = self.connection.cdn_request("GET", [""])
        status = response.status
        if not 200 <= status < 300:
            raise exc.CDNFailed("Bad response: (%s) %s" % (status, response.reason))
        return response.read().splitlines()


    def make_container_public(self, container, ttl=None):
        """Enables CDN access for the specified container."""
        return self._cdn_set_access(container, ttl, True)


    def make_container_private(self, container):
        """
        Disables CDN access to a container. It may still appear public until
        its TTL expires.
        """
        return self._cdn_set_access(container, None, False)


    def _cdn_set_access(self, container, ttl, enabled):
        """Used to enable or disable CDN access on a container."""
        if ttl is None:
            ttl = self.default_cdn_ttl
        ct = self.get_container(container)
        mthd = "POST" if ct.cdn_uri else "PUT"
        hdrs = {"X-CDN-Enabled": "%s" % enabled}
        if enabled:
            hdrs["X-TTL"] = str(ttl)
        response = self.connection.cdn_request(mthd, [ct.name], hdrs=hdrs)
        status = response.status
        if not 200 <= status < 300:
            raise exc.CDNFailed("Bad response: (%s) %s" % (status, response.reason))
        ct.cdn_ttl = ttl
        for hdr in response.getheaders():
            if hdr[0].lower() == "x-cdn-uri":
                ct.cdn_uri = hdr[1]
                break


    def set_cdn_log_retention(self, container, enabled):
        ct = self.get_container(container)
        hdrs = {"X-Log-Retention": enabled}
        response = self.connection.cdn_request("POST", [ct.name], hdrs=hdrs)
        status = response.status
        if not 200 <= status < 300:
            raise exc.CDNFailed("Bad response: (%s) %s" % (status, response.reason))
        ct.cdn_log_retention = enabled


    def set_container_web_index_page(self, container, page):
        """
        Sets the header indicating the index page in a container
        when creating a static website.

        Note: the container must be CDN-enabled for this to have
        any effect.
        """
        hdr = {"X-Container-Meta-Web-Index": page}
        return self.set_container_metadata(container, hdr, clear=False)


    def set_container_web_error_page(self, container, page):
        """
        Sets the header indicating the error page in a container
        when creating a static website.

        Note: the container must be CDN-enabled for this to have
        any effect.
        """
        hdr = {"X-Container-Meta-Web-Error": page}
        return self.set_container_metadata(container, hdr, clear=False)


    def _get_user_agent(self):
        return self.connection.user_agent

    def _set_user_agent(self, val):
        self.connection.user_agent = val

    user_agent = property(_get_user_agent, _set_user_agent)



class Connection(_swift_client.Connection):
    """This class wraps the swiftclient connection, adding support for CDN"""
    def __init__(self, *args, **kwargs):
        super(Connection, self).__init__(*args, **kwargs)
        # Add the user_agent, if not defined
        try:
            self.user_agent
        except AttributeError:
            self.user_agent = "swiftclient"

    def _make_cdn_connection(self, cdn_url=None):
        if cdn_url is not None:
            self.cdn_url = cdn_url
        parsed = urlparse.urlparse(self.cdn_url)
        is_ssl = parsed.scheme == "https"

        # Verify hostnames are valid and parse a port spec (if any)
        match = re.match(r"([a-zA-Z0-9\-\.]+):?([0-9]{2,5})?", parsed.netloc)
        if match:
            (host, port) = match.groups()
        else:
            host = parsed.netloc
            port = None
        if not port:
            port = 443 if is_ssl else 80
        port = int(port)
        path = parsed.path.strip("/")
        conn_class = httplib.HTTPSConnection if is_ssl else httplib.HTTPConnection
        self.cdn_connection = conn_class(host, port, timeout=CONNECTION_TIMEOUT)
        self.cdn_connection.is_ssl = is_ssl


    def cdn_request(self, method, path=[], data="", hdrs=None):
        """
        Given a method (i.e. GET, PUT, POST, etc), a path, data, header and
        metadata dicts, performs an http request against the CDN service.

        Taken directly from the cloudfiles library and modified for use here.
        """
        def quote(val):
            if isinstance(val, unicode):
                val = val.encode("utf-8")
            return urllib.quote(val)

        pth = "/".join([quote(elem) for elem in path])
        uri_path = urlparse.urlparse(self.uri).path
        path = "%s/%s" % (uri_path.rstrip("/"), pth)
        headers = {"Content-Length": str(len(data)),
                "User-Agent": self.user_agent,
                "X-Auth-Token": self.token}
        if isinstance(hdrs, dict):
            headers.update(hdrs)

        def retry_request():
            """Re-connect and re-try a failed request once"""
            self._make_cdn_connection()
            self.cdn_connection.request(method, path, data, headers)
            return self.cdn_connection.getresponse()

        try:
            self.cdn_connection.request(method, path, data, headers)
            response = self.cdn_connection.getresponse()
        except (socket.error, IOError, httplib.HTTPException) as e:
            response = retry_request()
        if response.status == 401:
            self._authenticate()
            headers["X-Auth-Token"] = self.token
            response = retry_request()
        return response


    @property
    def uri(self):
        return self.url