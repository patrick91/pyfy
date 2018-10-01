import os
import sys
import json
import socket
import base64
import pprint
import pickle
import secrets
import logging
import warnings
import dateutil
import datetime
from time import sleep
from urllib import parse
from functools import wraps

from requests import Request, Session, Response
from requests.exceptions import HTTPError, Timeout, ProxyError, RetryError
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util import Retry

__name__ = 'pyfy'
__about__ = "Lightweight python wrapper for Spotify's web API"
__url__ = 'https://github.com/omarryhan/spyfy'
__version_info__ = ('0', '0', '3d')
__version__ = '.'.join(__version_info__)
__author__ = 'Omar Ryhan'
__author_email__ = 'omarryhan@gmail.com'
__maintainer__ = 'Omar Ryhan'
__license__ = 'MIT'
__copyright__ = '(c) 2018 by Omar Ryhan'
__all__ = [
    'SpotifyError',
    'ApiError',
    'AuthError',
    'ClientCredentials',
    'UserCredentials',
    'Client'
]

BACKOFF_FACTOR = 0.1
TOKEN_EXPIRED_MSG = 'The access token expired'
_DEBUG = False  # If true, client will log every request and response in a pretty printed format
BASE_URI = 'https://api.spotify.com/v1'
OAUTH_TOKEN_URL = 'https://accounts.spotify.com/api/token'
OAUTH_AUTHORIZE_URL = 'https://accounts.spotify.com/authorize'
ALL_SCOPES = [
    'streaming',  # Playback
    'app-remote-control',
    'user-follow-modify',  # Follow
    'user-follow-read',
    'playlist-read-private',  # Playlists
    'playlist-modify-private',
    'playlist-read-collaborative',
    'playlist-modify-public',
    'user-modify-playback-state',  # Spotify Connect
    'user-read-playback-state',
    'user-read-currently-playing',
    'user-read-private',  # Users
    'user-read-birthdate',
    'user-read-email',
    'user-library-read',  # Library
    'user-library-modify',
    'user-top-read',  # Listening History
    'user-read-recently-played'
]


logger = logging.getLogger(__name__)
if _DEBUG:
    logger.setLevel(logging.DEBUG)


# TODO: Implement cache https://developer.spotify.com/documentation/web-api/#conditional-requests
# TODO: Check client._caller flow
# TODO: Test user refresh tokens
# TODO: Test client always raises error if not http 2**
# TODO: Revoke token


class SpotifyError(Exception):
    ''' RFC errors https://tools.ietf.org/html/rfc6749#section-5.2 '''
    def _build_super_msg(self, msg, http_res, http_req, e):
        if not http_req and not http_res and not e:
            return msg
        elif getattr(http_res, 'status_code') == 404 and http_req:  # If bad request or not found, show url and data
            body = http_req.data or http_req.json
            return 'Error msg = {}\nHTTP Error = Resource not found .\nRequest URL = {}\nRequest body = {}'.format(
                msg,
                http_req.url, pprint.pformat(body)
            )
        elif getattr(http_res, 'status_code') == 400 and http_req:  # If bad request or not found, show url and data
            body = http_req.data or http_req.json
            return 'Error msg = {}\nHTTP Error = Bad Request.\Request URL = {}\nRequest body = {}\nRequest headers = {}'.format(
                msg,
                http_req.url, pprint.pformat(body),
                pprint.pformat(http_req.headers)
            )
        elif getattr(http_res, 'status_code') == 401 and http_req:  # If unauthorized, only show headers
            return 'Error msg = {}\nHTTP Error = Unauthorized. Request headers = {}'.format(
                msg,
                pprint.pformat(http_req.headers)
            )
        return {
            'msg': msg,
            'http_response': http_res,
            'http_request': http_req,
            'original exception': e
        }


class ApiError(SpotifyError):
    def __init__(self, msg, http_response=None, http_request=None, e=None):
        ''' https://developer.spotify.com/documentation/web-api/#response-schema // regular error object '''
        self.msg = msg
        self.http_response = http_response
        self.http_request = http_request
        self.code = getattr(http_response, 'status_code', None)
        super_msg = self._build_super_msg(msg, http_response, http_request, e)
        super(ApiError, self).__init__(super_msg)


class AuthError(SpotifyError):
    ''' https://developer.spotify.com/documentation/web-api/#response-schema // authentication error object '''
    def __init__(self, msg, http_response=None, http_request=None, e=None):
        self.msg = msg
        self.http_response = http_response
        self.http_request = http_request
        self.code = getattr(http_response, 'status_code', None)
        super_msg = self._build_super_msg(msg, http_response, http_request, e)
        super(AuthError, self).__init__(super_msg)


class _Creds:
    def __init__(self, *args, **kwargs):
        raise TypeError('_Creds class isn\'nt calleable')

    def save_to_file(self, path=os.path.dirname(os.path.abspath(__file__)), name=None):
        if name is None:
            name = socket.gethostname() + "_" + "Spotify_" + self.__class__.__name__
        path = os.path.join(path, name)
        with open(path, 'wb') as creds_file:
            pickle.dump(self, creds_file, pickle.HIGHEST_PROTOCOL)

    def load_from_file(self, path=os.path.dirname(os.path.abspath(__file__)), name=None):
        if name is None:
            name = socket.gethostname() + "_" + "Spotify_" + self.__class__.__name__
        path = os.path.join(path, name)
        with open(path, 'rb') as creds_file:
            self = pickle.load(creds_file)

    def _delete_pickle(self, path=os.path.dirname(os.path.abspath(__file__)), name=None):
        ''' BE CAREFUL!! THIS WILL PERMENANTLY DELETE ONE OF YOUR FILES IF USED INCORRECTLY
            It is recommended you leave the defaults if you're using this library for personal use only '''
        if name is None:
            name = socket.gethostname() + "_" + "Spotify_" + self.__class__.__name__
        path = os.path.join(path, name)
        os.remove(path)

    @property
    def access_is_expired(self):
        if isinstance(self.expiry, datetime.datetime):
            return (self.expiry <= datetime.datetime.now())
        return None


def _create_secret(bytes_length=32):
    return secrets.base64.standard_b64encode(secrets.token_bytes(bytes_length)).decode('utf-8')


class ClientCredentials(_Creds):
    def __init__(self, client_id=None, client_secret=None, scopes=ALL_SCOPES, redirect_uri='http://localhost', show_dialog='false'):
        '''
        Parameters:
            show_dialog: if set to false, Spotify will not show a new authorization request if user is already authorized
        '''
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self.redirect_uri = redirect_uri
        self.show_dialog = show_dialog

        self.access_token = None  # For client credentials oauth flow
        self.expiry = None  # For client credentials oauth flow

    def load_from_env(self):
        self.client_id = os.environ['SPOTIFY_CLIENT_ID']
        self.client_secret = os.environ['SPOTIFY_CLIENT_SECRET']
        self.redirect_uri = os.environ['SPOTIFY_REDIRECT_URI']

    @property
    def is_oauth_ready(self):
        if self.client_id and self.redirect_uri and self.scopes and self.show_dialog is not None:
            return True
        return False


class UserCredentials(_Creds):
    def __init__(self, access_token=None, refresh_token=None, scopes=[], expiry=None, user_id=None, state=_create_secret()):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expiry = expiry  # expiry date. Not to be confused with expires in
        self.user_id = user_id
        self.state = state

    def load_from_env(self):
        self.access_token = os.environ['SPOTIFY_ACCESS_TOKEN']
        self.user_id = os.getenv('SPOTIFY_USER_ID', None)
        self.refresh_token = os.getenv('SPOTIFY_REFRESH_TOKEN', None)


def _set_empty_user_creds_if_none(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        self = args[0]
        if self.user_creds is None:
            self._user_creds = UserCredentials()
        self._caller = self.user_creds
        return f(*args, **kwargs)
    return wrapper


def _set_empty_client_creds_if_none(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        self = args[0]
        if self.client_creds is None:
            self.client_creds = ClientCredentials()
        return f(*args, **kwargs)
    return wrapper


def _require_user_id(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        self = args[0]
        if not self.user_creds.user_id and self.user_creds.access_token:
            id_ = self._request_user_id(self.user_creds)
            self.user_creds.user_id = id_
        return f(*args, **kwargs, user_id=self.user_creds.user_id)
    return wrapper


class Client:
    def __init__(self, client_creds=ClientCredentials(), user_creds=None, ensure_user_auth=False, proxies={}, timeout=7, max_retries=10, enforce_state_check=True):
        '''
        Parameters:
            client_creds: A client credentials model
            user_creds: A user credentials model
            ensure_user_auth: Whether or not to fail if user_creds provided where invalid and not refresheable
            proxies: socks or http proxies # http://docs.python-requests.org/en/master/user/advanced/#proxies & http://docs.python-requests.org/en/master/user/advanced/#socks
            timeout: Seconds before request raises a timeout error
            max_retries: Max retries before a request fails
            enforce_state_check: Check for a CSRF-token-like string. Helps verifying the identity of a callback sender thus avoiding CSRF attacks. Optional
        '''
        # The two main credentials model
        self.client_creds = client_creds
        self._user_creds = user_creds

        # Request defaults
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = self._create_session(max_retries, proxies)

        # Api defaults
        self.enforce_state_check = enforce_state_check

        # You shouldn't need to manually change this flag.
        # It's bound to be equal to either the client_creds object or user_creds object depending on which was last authorized
        self._caller = None

        # Others
        self.ensure_user_auth = ensure_user_auth
        if hasattr(user_creds, 'access_token') and ensure_user_auth:  # Attempt user authorization upon client instantiation
            self._caller = self._user_creds
            self._check_authorization()

    def _create_session(self, max_retries, proxies):
        sess = Session()
        # Retry only on idemportent requests and only when too many requests
        retries = Retry(total=max_retries, backoff_factor=BACKOFF_FACTOR, status_forcelist=[429], method_whitelist=['GET', 'UPDATE', 'DELETE'])
        http_adapter = HTTPAdapter(max_retries=retries)
        sess.mount('http://', http_adapter)
        sess.proxies.update(proxies)  
        return sess

    def _check_authorization(self):
        ''' checks whether the credentials provided are valid or not by making and api call that requires no scope but still requires authorization '''
        test_url = BASE_URI + '/search?' + parse.urlencode(dict(q='Hey spotify am I authorized', type='artist'))  # Hey%20spotify%2C%20am%20I%20authorized%3F&type=artist'
        try:
            self._send_authorized_request(Request(method='GET', url=test_url))
        except AuthError as e:
            raise e

    def _send_authorized_request(self, r):
        if getattr(self._caller, 'access_is_expired', None) is True:  # True if expired and None if there's no expiry set
            self._refresh_token()
        r.headers.update(self._access_authorization_header)
        return self._send_request(r)

    def _send_request(self, r):
        prepped = r.prepare()
        if _DEBUG:
            #pprint.pprint({'REQUEST': r.__dict__})
            logger.debug(pprint.pformat({'REQUEST': r.__dict__}))
        try:
            res = self._session.send(prepped, timeout=self.timeout)
            if _DEBUG:
                #pprint.pprint({'RESPONSE': res.__dict__})
                logger.debug(pprint.pformat({'RESPONSE': res.__dict__}))
            res.raise_for_status()
        except Timeout as e:
            raise ApiError('Request timed out.\nTry increasing the client\'s timeout period', http_response=None, http_request=r, e=e)
        except HTTPError as e:
            #if res.status_code == 429:  # If too many requests
            if res.status_code == 401:
                if res.json().get('error', None).get('message', None) == TOKEN_EXPIRED_MSG:
                    old_auth_header = r.headers['Authorization']
                    self._refresh_token()  # Should either raise an error or refresh the token
                    new_auth_header = self._access_authorization_header
                    assert new_auth_header != old_auth_header  # Assert header is changed to avoid 
                    r.headers.update(new_auth_header)
                    return self._send_request(r)
                else:
                    msg = res.json().get('error_description', None) or res.json()
                    raise AuthError(msg=msg, http_response=res, http_request=r, e=e)
            else:
                msg = res.json() or None
                raise ApiError(msg=msg, http_response=res, http_request=r, e=e)
        else:
            return res

    def authorize_client_creds(self, client_creds=None):
        ''' https://developer.spotify.com/documentation/general/guides/authorization-guide/ 
            Authorize with client credentials oauth flow i.e. Only with client secret and client id.
            This will give you limited functionality '''
        if client_creds:
            if self.client_creds:
                warnings.warn('Overwriting existing client_creds object')
            self.client_creds = client_creds
        if not self.client_creds or not self.client_creds.client_id or not self.client_creds.client_secret:
            raise AuthError('No client credentials set')
        data = {'grant_type': 'client_credentials'}
        headers = self._client_authorization_header
        try:
            r = Request(method='POST', url=OAUTH_TOKEN_URL, headers=headers, data=data)
            res = self._send_request(r)
        except ApiError as e:
            raise AuthError(msg='Failed to authenticate with client credentials', http_response=e.http_response, http_request=r, e=e)
        else:
            new_creds_json = res.json()
            new_creds_model = self._client_json_to_object(new_creds_json)
            self._update_client_creds_with(new_creds_model)
            self._caller = self.client_creds
            self._check_authorization()

    @property
    def user_creds(self):
        return self._user_creds

    @user_creds.setter
    def user_creds(self, user_creds):
        ''' if user is set, do: '''
        self._user_creds = user_creds
        self._caller = self._user_creds
        if self.ensure_user_auth:
            self._check_authorization()

    @property
    def is_oauth_ready(self):
        return self.client_creds.is_oauth_ready

    @property
    @_set_empty_user_creds_if_none
    def oauth_uri(self):
        ''' Generate OAuth URI for authentication '''
        params = {
            'client_id': self.client_creds.client_id,
            'response_type': 'code',
            'redirect_uri': self.client_creds.redirect_uri,
            'scopes': ' '.join(self.client_creds.scopes),
        }
        if self.enforce_state_check:
            if self.user_creds.state is None:
                warnings.warn('No user state provided. Returning URL without a state!')
            else:
                params.update({'state': self.user_creds.state})
        params = parse.urlencode(params)
        return f'{OAUTH_AUTHORIZE_URL}?{params}'

    @property
    def is_active(self):
        ''' Check if user_creds are valid '''
        if self._caller is None:
            return False
        try:
            self._check_authorization()
        except AuthError:
            return False
        else:
            return True

    def _refresh_token(self):
        if self._caller is self.user_creds:
            return self._refresh_user_token()
        elif self._caller is self.client_creds:
            return self.authorize_client_creds()
        else:
            raise AuthError('No caller to refresh token for')

    def _refresh_user_token(self):
        if not self.user_creds.refresh_token:
            raise AuthError(msg='Access token expired and couldn\'t find a refresh token to refresh it')
        data = {
            'grant_type': 'refresh_token',
            'refresh_token': self.user_creds.refresh_token
        }
        headers = {**self._client_authorization_header, **self._form_url_encoded_type_header}
        res = self._send_request(Request(method='POST', url=OAUTH_TOKEN_URL, headers=headers, data=data)).json()
        new_creds_obj = self._user_json_to_object(res)
        self._update_user_creds_with(new_creds_obj)

    @_set_empty_user_creds_if_none
    def build_user_credentials(self, grant, state=None, set_user_creds=True, update_user_creds=True, fetch_user_id=True):
        ''' Second part of OAuth authorization code flow, Raises an
            - state: State returned from oauth callback
            - set_user_creds: Whether or not to set the user created to the client as the current active user
            - update_user_creds: If set to yes, it will update the attributes of the client's current user if set. Else, it will overwrite the existing one. Must have set_user_creds.
            - fetch_user_id: if yes, it will call the /me endpoint and try to fetch the user id, which will be needed to fetch user owned resources
            '''
        # Check for equality of states
        if state is not None:
            if state != getattr(self.user_creds, 'state', None):
                res = Response()
                res.status_code = 401
                raise AuthError(msg='States do not match or state not provided', http_response=res)

        # Get user creds
        user_creds_json = self._request_user_creds(grant).json()
        user_creds_model = self._user_json_to_object(user_creds_json)

        # Update user id
        if fetch_user_id:
            id_ = self._request_user_id(user_creds_model)
            user_creds_model.user_id = id_

        # Update user creds
        if update_user_creds and set_user_creds:
            self._update_user_creds_with(user_creds_model)
            return self.user_creds

        # Set user creds
        if set_user_creds:
            return self.user_creds
        return user_creds_model

    def _request_user_creds(self, grant):
        data = {
            'grant_type': 'authorization_code',
            'code': grant,
            'redirect_uri': self.client_creds.redirect_uri
        }
        headers = {**self._client_authorization_header, **self._form_url_encoded_type_header}
        return self._send_request(Request(method='POST', url=OAUTH_TOKEN_URL, headers=headers, data=data))

    def _request_user_id(self, user_creds):
        ''' not using client.me as it uses the _send_authorized_request method which generates its auth headers from self._caller attribute.
        The developer won't necessarily need to set the user's credentials as the self._caller after building them. Ergo, the need for this method''' 
        header = {'Authorization': 'Bearer {}'.format(user_creds.access_token)}
        url = BASE_URI + '/me'
        res = self._send_request(Request(method='GET', url=url, headers=header)).json()
        return res['id']

    def _update_user_creds_with(self, user_creds_object):
        for key, value in user_creds_object.__dict__.items():
            if value is not None:
                #if isinstance(value, dict) and isinstance(getattr(self.user_creds, key), dict):  # if dicts, merge
                #    setattr(self.user_creds, key, {**getattr(self.user_creds, key), **value})
                #elif isinstance(value, list) and isinstance(getattr(self.user_creds, key), list):  # if lists, extend
                #    getattr(self.user_creds, key).extend(value)
                #else:
                setattr(self.user_creds, key, value)

    @_set_empty_client_creds_if_none
    def _update_client_creds_with(self, client_creds_object):
        for key, value in client_creds_object.__dict__.items():
            if value is not None:
                setattr(self.client_creds, key, value)

    def _user_json_to_object(self, json_response):
        return UserCredentials(
            access_token=json_response['access_token'],
            scopes=json_response['scope'].split(' '),
            expiry=datetime.datetime.now() + datetime.timedelta(seconds=json_response['expires_in']),
            refresh_token=json_response.get('refresh_token', None)
        )

    @staticmethod
    def _client_json_to_object(json_response):
        creds = ClientCredentials()
        creds.access_token = json_response['access_token']
        creds.expiry = datetime.datetime.now() + datetime.timedelta(seconds=json_response['expires_in'])
        return creds

    @staticmethod
    def _convert_to_iso_date(date):
        return dateutil.parser.parse(date)
    
    @staticmethod
    def convert_from_iso_date(date):
        if not isinstance(date, datetime.datetime):
            raise TypeError('date must be of type datetime.datetime')
        return date.isoformat()
        
    @property
    def _json_content_type_header(self):
        return {'Content-Type': 'application/json'}

    @property
    def _form_url_encoded_type_header(self):
        return {'Content-Type': 'application/x-www-form-urlencoded'}

    @property
    def _client_authorization_header(self):
        if self.client_creds.client_id and self.client_creds.client_secret:
            # Took me a whole day to figure out that the colon is supposed to be encoded :'(
            utf_header = self.client_creds.client_id + ':' + self.client_creds.client_secret
            return {'Authorization': 'Basic {}'.format(base64.b64encode(utf_header.encode()).decode())}
        else:
            raise AttributeError('No client credentials found to make an authorization header')

    @property
    def _client_authorization_data(self):
        return {
            'client_id': self.client_creds.client_id,
            'client_sectet': self.client_creds.client_secret
        }

    @property
    def _access_authorization_header(self):
        if self._caller:
            return {'Authorization': 'Bearer {}'.format(self._caller.access_token)}
        else:
            raise ApiError(msg='Call Requires an authorized caller, either client or user')

    @staticmethod
    def _safe_add_query_param(url, query):
        ''' Removes None variables from query, then attaches it to the original url '''
        # Check if url and query are proper types
        if not isinstance(query, dict) or not isinstance(url, str):
            raise TypeError('Queries must be an instance of a dict and url must be an instance of string in order to be properly encoded')
        # Remove bad params
        bad_params = [None, tuple(), dict(), list()]
        safe_query = {}
        for k, v in query.items():
            if v not in bad_params:
                safe_query[k] = v
        # Add safe query to url
        if safe_query:
            url = url + '?'
        return url + parse.urlencode(safe_query)

    #@staticmethod
    #def _to_string_boolean(boolean):
    #    if boolean is True:
    #        return 'true'
    #    elif boolean is False:
    #        return 'false'
    #    else:
    #        raise ValueError('{} must be a boolean or None'.format(boolean))

    @staticmethod
    def _json_safe_dict(data):
        safe_types = [float, str, int, bool]
        safe_json = {}
        for k, v in data.items():
            if type(v) in safe_types:
                safe_json[k] = v
        return safe_json

    ############################################################### RESOURCES ###################################################################

    def get_album(self, album_id, market=None):
        url = BASE_URI + '/albums/' + album_id
        params = dict(market=market)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_albums(self, album_ids, market=None):
        url = BASE_URI + '/albums/'
        params = dict(ids=','.join(album_ids), market=market)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_albums_tracks(self, albums_id, market=None, limit=None, offset=None):
        url = BASE_URI + '/' + albums_id + '/tracks'
        params = dict(market=market, limit=limit, offset=offset)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_artists_albums(self, artist_id, include_groups=None, market=None, limit=None, offset=None):
        url = BASE_URI + '/artists' + artist_id + '/albums'
        params = dict(include_groups=include_groups, market=market, limit=limit, offset=offset)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_artists_related_artists(self, artist_id):
        url = BASE_URI + '/artists' + artist_id + '/related-artists'
        params = {}
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_artists_top_tracks(self, artist_id, country=None):
        url = BASE_URI + '/artists' + artist_id + '/top-tracks'
        params = dict(country=country)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_artist(self, artist_id):
        url = BASE_URI + '/artists' + artist_id
        params = dict()
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_artists(self, artist_ids):
        url = BASE_URI + '/artists'
        params = dict(ids=','.join(artist_ids))
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_available_genre_seeds(self):
        r = Request(method='GET', url=BASE_URI + '/recommendations/available-genre-seeds')
        return self._send_authorized_request(r).json()

    def get_categories(self, country=None, locale=None, limit=None, offset=None):
        url = BASE_URI + '/browse/categories'
        params = dict(country=country, locale=locale, limit=limit, offset=offset)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_category(self, category_id, country=None, locale=None):
        url = BASE_URI + '/browse/categories/' + category_id
        params = dict(country=country, locale=locale)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()  

    def get_categories_playlist(self, category_id, country=None, limit=None, offset=None):
        url = BASE_URI + '/browse/categories/' + category_id + '/playlists'
        params = dict(country=country, limit=limit, offset=offset)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_featured_playlists(self, country=None, locale=None, timestamp=None, limit=None, offset=None):
        timestamp = self._convert_to_iso_date(timestamp)
        url = BASE_URI + '/browse/featured-playlists'
        params = dict(country=country, locale=locale, timestamp=timestamp, limit=limit, offset=offset)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_new_releases(self, country=None, limit=None, offset=None):
        url = BASE_URI + '/browser/new-releases'
        params = dict(country=country, limit=limit, offset=offset)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_recommendations(
        self,
        limit=None,
        market=None,
        seed_artists=None,
        seed_genres=None,
        seed_tracks=None,
        min_acousticness=None,
        max_acousticness=None,
        target_acousticness=None,
        min_danceability=None,
        max_danceability=None,
        target_danceability=None,
        min_duration_ms=None,
        max_duration_ms=None,
        target_duration_ms=None,
        min_energy=None,
        max_energy=None,
        target_energy=None,
        min_instrumentalness=None,
        max_instrumentalness=None,
        target_instrumentalness=None,
        min_key=None,
        max_key=None,
        target_key=None,
        min_liveness=None,
        max_liveness=None,
        target_liveness=None,
        min_loudness=None,
        max_loudness=None,
        target_loudness=None,
        min_mode=None,
        max_mode=None,
        target_mode=None,
        min_popularity=None,
        max_popularity=None,
        target_popularity=None,
        min_speechiness=None,
        max_speechiness=None,
        target_speechiness=None,
        min_tempo=None,
        max_tempo=None,
        target_tempo=None,
        min_time_signature=None,
        max_time_signature=None,
        target_time_signature=None,
        min_valence=None,
        max_valence=None,
        target_valence=None
    ):
        url = BASE_URI + '/recommendations'
        params = dict(
            limit=limit,
            market=market,
            seed_artists=seed_artists,
            seed_genres=seed_genres,
            seed_tracks=seed_tracks,
            min_acousticness=min_acousticness,
            max_acousticness=max_acousticness,
            target_acousticness=target_acousticness,
            min_danceability=min_danceability,
            max_danceability=max_danceability,
            target_danceability=target_danceability,
            min_duration_ms=min_duration_ms,
            max_duration_ms=max_duration_ms,
            target_duration_ms=target_duration_ms,
            min_energy=min_energy,
            max_energy=max_energy,
            target_energy=target_energy,
            min_instrumentalness=min_instrumentalness,
            max_instrumentalness=max_instrumentalness,
            target_instrumentalness=target_instrumentalness,
            min_key=min_key,
            max_key=max_key,
            target_key=target_key,
            min_liveness=min_liveness,
            max_liveness=max_liveness,
            target_liveness=target_liveness,
            min_loudness=min_loudness,
            max_loudness=max_loudness,
            target_loudness=target_loudness,
            min_mode=min_mode,
            max_mode=max_mode,
            target_mode=target_mode,
            min_popularity=min_popularity,
            max_popularity=max_popularity,
            target_popularity=target_popularity,
            min_speechiness=min_speechiness,
            max_speechiness=max_speechiness,
            target_speechiness=target_speechiness,
            min_tempo=min_tempo,
            max_tempo=max_tempo,
            target_tempo=target_tempo,
            min_time_signature=min_time_signature,
            max_time_signature=max_time_signature,
            target_time_signature=target_time_signature,
            min_valence=min_valence,
            max_valence=max_valence,
            target_valence=target_valence
        )
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def unfollow_artists(self, artist_ids):
        url = BASE_URI + '/me/following'
        params = dict(type='artist', ids=','.split(artist_ids))
        r = Request(method='DELETE', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def unfollow_users(self, user_ids):
        url = BASE_URI + '/me/following'
        params = dict(type='user', ids=','.split(user_ids))
        r = Request(method='DELETE', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def unfollow_playlist(self, playlist_id):
        url = BASE_URI + '/playlists/' + playlist_id + '/followers'
        params = {}
        r = Request(method='DELETE', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_users_followed_users(self, after=None, limit=None):
        url = BASE_URI + '/me/following'
        params = dict(type='user', after=after, limit=limit)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_users_followed_artists(self, after=None, limit=None):
        url = BASE_URI + '/me/following'
        params = dict(type='artist', after=after, limit=limit)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def check_user_follows_users(self, user_ids):
        url = BASE_URI + '/me/following/contains'
        params = dict(type='user', ids=','.split(user_ids))
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def check_user_follows_artists(self, artist_ids):
        url = BASE_URI + '/me/following/contains'
        params = dict(type='artist', ids=','.solit(artist_ids))
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def check_user_follows_playlist(self, user_ids, playlist_id):
        url = BASE_URI + '/playlists/' + playlist_id + '/followers/contains'
        params = dict(ids=','.split(user_ids))
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def follow_artists(self, artist_ids):       
        url = BASE_URI + '/me/following'
        params = dict(type='artist', ids=','.split(artist_ids))
        r = Request(method='PUT', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def follow_users(self, user_ids):       
        url = BASE_URI + '/me/following'
        params = dict(type='user', ids=','.split(user_ids))
        r = Request(method='PUT', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json() 

    def follow_playlist(self, playlist_id, public=None):
        url = BASE_URI + '/playlists/' + playlist_id + '/followers'
        params = {}
        data = self._json_safe_dict(dict(public=public))
        r = Request(method='PUT', url=self._safe_add_query_param(url, params), json=data)
        return self._send_authorized_request(r).json()

    def delete_user_albums(self, album_ids):
        url = BASE_URI + '/me/albums'
        params = dict(ids=','.split(album_ids))
        r = Request(method='DELETE', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def delete_user_tracks(self, track_ids):
        url = BASE_URI + '/me/tracks'
        params = dict(ids=','.split(track_ids))
        r = Request(method='DELETE', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()
    
    def check_user_owns_albums(self, album_ids):
        url = BASE_URI + '/me/albums/contains'
        params = dict(ids=','.split(album_ids))
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def check_user_owns_tracks(self, track_ids):
        url = BASE_URI + '/me/tracks/contains'
        params = dict(ids=','.split(track_ids))
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_users_albums(self, limit=None, offset=None):
        url = BASE_URI + '/me/albums'
        params = dict(limit=limit, offset=offset)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def get_users_tracks(self, market=None, limit=None, offset=None):
        url = BASE_URI + '/me/tracks'
        params = dict(market=market, limit=limit, offset=offset)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def save_album_for_user(self, album_ids):
        url = BASE_URI + '/me/albums'
        params = dict(ids=album_ids)
        r = Request(method='PUT', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    def save_track_for_user(self, track_ids):
        url = BASE_URI + '/me/tracks'
        params = dict(ids=track_ids)
        r = Request(method='GET', url=self._safe_add_query_param(url, params))
        return self._send_authorized_request(r).json()

    @property
    def me(self):
        r = Request(method='GET', url=BASE_URI + '/me')
        return self._send_authorized_request(r).json()

    @property
    def playlists(self):
        r = Request(method='GET', url=BASE_URI + '/me/playlists')
        return self._send_authorized_request(r).json()

    @property
    @_require_user_id
    def user_platlists(self, user_id):
        r = Request(method='GET', url=BASE_URI + '/users/' + user_id + '/playlists')
        return self._send_authorized_request(r).json()

    @property
    def tracks(self):
        r = Request(method='GET', url=BASE_URI + '/me/tracks')
        return self._send_authorized_request(r).json()

    @property
    def random_tracks(self):
        r = Request(method='GET', url=BASE_URI + '/tracks')
        return self._send_authorized_request(r).json()

    @property
    def categories(self):
        r = Request(method='GET', url=BASE_URI + '/browse/categories')
        return self._send_authorized_request(r).json()