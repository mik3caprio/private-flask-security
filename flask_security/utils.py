# -*- coding: utf-8 -*-
"""
    flask.ext.security.utils
    ~~~~~~~~~~~~~~~~~~~~~~~~

    Flask-Security utils module

    :copyright: (c) 2012 by Matt Wright.
    :license: MIT, see LICENSE for more details.
"""

import base64
import hashlib
import hmac
import sys

try:
    from urlparse import urlsplit
except ImportError:  # pragma: no cover
    from urllib.parse import urlsplit

from contextlib import contextmanager
from datetime import datetime, timedelta

from flask import url_for, flash, current_app, request, session, render_template
from flask.ext.login import login_user as _login_user, logout_user as _logout_user
from flask.ext.mail import Message
from flask.ext.principal import Identity, AnonymousIdentity, identity_changed
from itsdangerous import BadSignature, SignatureExpired
from werkzeug.local import LocalProxy

from .signals import user_registered, login_instructions_sent, reset_password_instructions_sent

# Convenient references
_security = LocalProxy(lambda: current_app.extensions['security'])

_datastore = LocalProxy(lambda: _security.datastore)

_pwd_context = LocalProxy(lambda: _security.pwd_context)

_sendgrid = LocalProxy(lambda: current_app.extensions['sendgrid'])

PY3 = sys.version_info[0] == 3

if PY3:  # pragma: no cover
    string_types = str,  # pragma: no flakes
    text_type = str  # pragma: no flakes
else:  # pragma: no cover
    string_types = basestring,  # pragma: no flakes
    text_type = unicode  # pragma: no flakes


def login_user(user, remember=None):
    """Performs the login routine.

    :param user: The user to login
    :param remember: Flag specifying if the remember cookie should be set. Defaults to ``False``
    """

    if remember is None:
        remember = config_value('DEFAULT_REMEMBER_ME')

    if not _login_user(user, remember):  # pragma: no cover
        return False

    if _security.trackable:
        if 'X-Forwarded-For' not in request.headers:
            remote_addr = request.remote_addr or 'untrackable'
        else:
            remote_addr = request.headers.getlist("X-Forwarded-For")[0]

        old_current_login, new_current_login = user.current_login_at, datetime.utcnow()
        old_current_ip, new_current_ip = user.current_login_ip, remote_addr

        user.last_login_at = old_current_login or new_current_login
        user.current_login_at = new_current_login
        user.last_login_ip = old_current_ip or new_current_ip
        user.current_login_ip = new_current_ip
        user.login_count = user.login_count + 1 if user.login_count else 1

        _datastore.put(user)

    identity_changed.send(current_app._get_current_object(),
                          identity=Identity(user.id))
    return True


def logout_user():
    """Logs out the current. This will also clean up the remember me cookie if it exists."""

    for key in ('identity.name', 'identity.auth_type'):
        session.pop(key, None)
    identity_changed.send(current_app._get_current_object(),
                          identity=AnonymousIdentity())
    _logout_user()


def get_hmac(password):
    """Returns a Base64 encoded HMAC+SHA512 of the password signed with the salt specified
    by ``SECURITY_PASSWORD_SALT``.

    :param password: The password to sign
    """
    salt = _security.password_salt

    if salt is None:
        raise RuntimeError(
            'The configuration value `SECURITY_PASSWORD_SALT` must '
            'not be None when the value of `SECURITY_PASSWORD_HASH` is '
            'set to "%s"' % _security.password_hash)

    h = hmac.new(encode_string(salt), encode_string(password), hashlib.sha512)
    return base64.b64encode(h.digest())


def verify_password(password, password_hash):
    """Returns ``True`` if the password matches the supplied hash.

    :param password: A plaintext password to verify
    :param password_hash: The expected hash value of the password (usually from your database)
    """
    if _security.password_hash != 'plaintext':
        password = get_hmac(password)

    return _pwd_context.verify(password, password_hash)


def verify_and_update_password(password, user):
    """Returns ``True`` if the password is valid for the specified user. Additionally, the hashed
    password in the database is updated if the hashing algorithm happens to have changed.

    :param password: A plaintext password to verify
    :param user: The user to verify against
    """

    if _pwd_context.identify(user.password) != 'plaintext':
        password = get_hmac(password)
    verified, new_password = _pwd_context.verify_and_update(password, user.password)
    if verified and new_password:
        user.password = encrypt_password(password)
        _datastore.put(user)
    return verified


def encrypt_password(password):
    """Encrypts the specified plaintext password using the configured encryption options.

    :param password: The plaintext password to encrypt
    """
    if _security.password_hash == 'plaintext':
        return password
    signed = get_hmac(password).decode('ascii')
    return _pwd_context.encrypt(signed)


def encode_string(string):
    """Encodes a string to bytes, if it isn't already.

    :param string: The string to encode"""

    if isinstance(string, text_type):
        string = string.encode('utf-8')
    return string


def md5(data):
    return hashlib.md5(encode_string(data)).hexdigest()


def do_flash(message, category=None):
    """Flash a message depending on if the `FLASH_MESSAGES` configuration
    value is set.

    :param message: The flash message
    :param category: The flash message category
    """
    if config_value('FLASH_MESSAGES'):
        flash(message, category)


def get_url(endpoint_or_url):
    """Returns a URL if a valid endpoint is found. Otherwise, returns the
    provided value.

    :param endpoint_or_url: The endpoint name or URL to default to
    """
    try:
        return url_for(endpoint_or_url)
    except:
        return endpoint_or_url


def get_security_endpoint_name(endpoint):
    return '%s.%s' % (_security.blueprint_name, endpoint)


def url_for_security(endpoint, **values):
    """Return a URL for the security blueprint

    :param endpoint: the endpoint of the URL (name of the function)
    :param values: the variable arguments of the URL rule
    :param _external: if set to `True`, an absolute URL is generated. Server
      address can be changed via `SERVER_NAME` configuration variable which
      defaults to `localhost`.
    :param _anchor: if provided this is added as anchor to the URL.
    :param _method: if provided this explicitly specifies an HTTP method.
    """
    endpoint = get_security_endpoint_name(endpoint)
    return url_for(endpoint, **values)


def validate_redirect_url(url):
    if url is None or url.strip() == '':
        return False
    url_next = urlsplit(url)
    url_base = urlsplit(request.host_url)
    if (url_next.netloc or url_next.scheme) and url_next.netloc != url_base.netloc:
        return False
    return True


def get_post_action_redirect(config_key, declared=None):
    urls = [
        get_url(request.args.get('next')),
        get_url(request.form.get('next')),
        find_redirect(config_key)
    ]
    if declared:
        urls.insert(0, declared)
    for url in urls:
        if validate_redirect_url(url):
            return url


def get_post_login_redirect(declared=None):
    return get_post_action_redirect('SECURITY_POST_LOGIN_VIEW', declared)


def get_post_register_redirect(declared=None):
    return get_post_action_redirect('SECURITY_POST_REGISTER_VIEW', declared)


def find_redirect(key):
    """Returns the URL to redirect to after a user logs in successfully.

    :param key: The session or application configuration key to search for
    """
    rv = (get_url(session.pop(key.lower(), None)) or
          get_url(current_app.config[key.upper()] or None) or '/')
    return rv


def get_config(app):
    """Conveniently get the security configuration for the specified
    application without the annoying 'SECURITY_' prefix.

    :param app: The application to inspect
    """
    items = app.config.items()
    prefix = 'SECURITY_'

    def strip_prefix(tup):
        return (tup[0].replace('SECURITY_', ''), tup[1])

    return dict([strip_prefix(i) for i in items if i[0].startswith(prefix)])


def get_message(key, **kwargs):
    rv = config_value('MSG_' + key)
    return rv[0] % kwargs, rv[1]


def config_value(key, app=None, default=None):
    """Get a Flask-Security configuration value.

    :param key: The configuration key without the prefix `SECURITY_`
    :param app: An optional specific application to inspect. Defaults to Flask's
                `current_app`
    :param default: An optional default value if the value is not set
    """
    app = app or current_app
    return get_config(app).get(key.upper(), default)


def get_max_age(key, app=None):
    td = get_within_delta(key + '_WITHIN', app)
    return td.seconds + td.days * 24 * 3600


def get_within_delta(key, app=None):
    """Get a timedelta object from the application configuration following
    the internal convention of::

        <Amount of Units> <Type of Units>

    Examples of valid config values::

        5 days
        10 minutes

    :param key: The config value key without the 'SECURITY_' prefix
    :param app: Optional application to inspect. Defaults to Flask's
                `current_app`
    """
    txt = config_value(key, app=app)
    values = txt.split()
    return timedelta(**{values[1]: int(values[0])})


def send_mail(subject, recipient, template, **context):
    """Send an email via Sendgrid extension.

    :param subject: Email subject
    :param recipient: Email recipient
    :param template: The name of the email template
    :param context: The context to render the template with
    """
    import sendgrid
    from sendgrid import SendGridError, SendGridClientError, SendGridServerError

    context.setdefault('security', _security)
    context.update(_security._run_ctx_processor('mail'))

    message = sendgrid.Mail()
    message.set_subject(subject)
    message.set_from(app.config["SENDGRID_EMAIL_FROM"])

    message.add_to('<' + recipient + '>')

    ctx = ('security/email', template)

    email_body = render_template('%s/%s.txt' % ctx, **context)
    message.set_text(email_body)

    html_email_body = render_template('%s/%s.html' % ctx, **context)
    message.set_html(html_email_body)

    if _security._send_mail_task:
        _security._send_mail_task(message)

        return

    # By default, .send method returns a tuple (http_status_code, message), 
    # however you can pass raise_errors=True to SendGridClient constructor, 
    # then .send method will raise SendGridClientError for 4xx errors, and 
    # SendGridServerError for 5xx errors.
    try:
        status, msg = app.config["SENDGRID_CLIENT"].send(message)

        emailStatus = True
    except sendgrid.SendGridClientError:
        emailStatus = False
    except sendgrid.SendGridServerError:
        emailStatus = False

    return emailStatus


def get_token_status(token, serializer, max_age=None):
    """Get the status of a token.

    :param token: The token to check
    :param serializer: The name of the seriailzer. Can be one of the
                       following: ``confirm``, ``login``, ``reset``
    :param max_age: The name of the max age config option. Can be on of
                    the following: ``CONFIRM_EMAIL``, ``LOGIN``, ``RESET_PASSWORD``
    """
    serializer = getattr(_security, serializer + '_serializer')
    max_age = get_max_age(max_age)
    user, data = None, None
    expired, invalid = False, False

    try:
        data = serializer.loads(token, max_age=max_age)
    except SignatureExpired:
        d, data = serializer.loads_unsafe(token)
        expired = True
    except (BadSignature, TypeError, ValueError):
        invalid = True

    if data:
        user = _datastore.find_user(id=data[0])

    expired = expired and (user is not None)
    return expired, invalid, user


def get_identity_attributes(app=None):
    app = app or current_app
    attrs = app.config['SECURITY_USER_IDENTITY_ATTRIBUTES']
    try:
        attrs = [f.strip() for f in attrs.split(',')]
    except AttributeError:
        pass
    return attrs


@contextmanager
def capture_passwordless_login_requests():
    login_requests = []

    def _on(app, **data):
        login_requests.append(data)

    login_instructions_sent.connect(_on)

    try:
        yield login_requests
    finally:
        login_instructions_sent.disconnect(_on)


@contextmanager
def capture_registrations():
    """Testing utility for capturing registrations.

    :param confirmation_sent_at: An optional datetime object to set the
                                 user's `confirmation_sent_at` to
    """
    registrations = []

    def _on(app, **data):
        registrations.append(data)

    user_registered.connect(_on)

    try:
        yield registrations
    finally:
        user_registered.disconnect(_on)


@contextmanager
def capture_reset_password_requests(reset_password_sent_at=None):
    """Testing utility for capturing password reset requests.

    :param reset_password_sent_at: An optional datetime object to set the
                                   user's `reset_password_sent_at` to
    """
    reset_requests = []

    def _on(app, **data):
        reset_requests.append(data)

    reset_password_instructions_sent.connect(_on)

    try:
        yield reset_requests
    finally:
        reset_password_instructions_sent.disconnect(_on)
