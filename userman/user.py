" Userman: User handlers. "

import logging

import tornado.web
import pycountry

from . import constants
from . import settings
from . import utils
from .saver import DocumentSaver
from .requesthandler import RequestHandler


class UserSaver(DocumentSaver):

    doctype = constants.USER

    def initialize(self):
        self['status'] = constants.PENDING
        self['services'] = []
        self['apikeys'] = {}
        self['teams'] = []
        self['created'] = utils.timestamp()

    def check_email(self, value):
        """Raise ValueError if given email value has wrong format.
        Raise KeyError if the value conflicts with another."""
        if not value:
            raise ValueError('email must be a non-empty value')
        if value == self.doc.get('email'): return
        if '/' in value:
            raise ValueError("slash '/' disallowed in email")
        parts = value.split('@')
        if len(parts) != 2:
            raise ValueError("at-sign '@' not used correcly in email")
        if len(parts[1].split('.')) < 2:
            raise ValueError('invalid domain name part in email')
        if len(list(self.db.view('user/email')[value])) > 0:
            raise KeyError("email already in use")

    def check_username(self, value):
        """Raise ValueError if the given username has wrong format.
        Raise KeyError if the value conflicts with another."""
        if not value: return
        if value == self.doc.get('username'): return
        if '/' in value:
            raise ValueError("slash '/' disallowed in username")
        if '@' in value:
            raise ValueError("at-sign '@' disallowed in username")
        if len(list(self.db.view('user/username')[value])) > 0:
            raise KeyError('username already in use')

    def convert_email(self, value):
        "Convert email value to lower case."
        return value.lower()

    def check_password(self, value):
        "Check password quality."
        utils.check_password_quality(value)

    def convert_password(self, value):
        return utils.hashed_password(value)

    def check_status(self, value):
        "Check status value."
        if value not in constants.STATUSES:
            raise ValueError('invalid status value')

    def convert_services(self, value):
        "Set the list of services the user may access."
        old_services = set(self.doc.get('services', []))
        for service in value:
            if service not in old_services:
                self.set_apikey(service)
        return sorted(value)
        
    def set_apikey(self, servicename):
        "Reset the API key for the service of the given name."
        apikeys = self.doc.get('apikeys', {}).copy()
        apikeys[servicename] = dict(value=utils.get_iuid(),
                                    modified=utils.timestamp())
        self['apikeys'] = apikeys
        

class UserMixin(object):

    def may_access_user(self, user):
        if not self.current_user: return False
        if self.is_admin(): return True
        if user['email'] == self.current_user['email']: return True
        return False

    def check_access_user(self, user):
        if not self.may_access_user(user):
            raise tornado.web.HTTPError(403, 'you may not access user')

    def get_user_services(self, user):
        result = set([self.get_service(s) for s in user['services']])
        for row in self.db.view('service/name'):
            service = self.get_service(row.key)
            if service.get('public'):
                result.add(service)
        return sorted(result, cmp=utils.cmp_name)


class User(UserMixin, RequestHandler):
    "Display a user account."

    @tornado.web.authenticated
    def get(self, email):
        user = self.get_user(email)
        self.check_access_user(user)
        services = [self.get_service(n) for n in user['services']]
        teams = [self.get_team(n) for n in user['teams']]
        leading = [t for t in teams if email in t['leaders']]
        self.render('user.html',
                    user=user,
                    services=services,
                    teams=teams,
                    leading=leading,
                    logs=self.get_logs(user['_id']))


class UserEdit(UserMixin, RequestHandler):
    "Edit a user account."

    @tornado.web.authenticated
    def get(self, email):
        user = self.get_user(email)
        self.check_access_user(user)
        teams = [self.get_team(n) for n in user['teams']]
        leading = [t for t in teams if email in t['leaders']]
        self.render('user_edit.html',
                    user=user,
                    services=self.get_all_services(),
                    teams=teams,
                    leading=leading,
                    countries=sorted([c.name for c in pycountry.countries]))

    @tornado.web.authenticated
    def post(self, email):
        self.check_xsrf_cookie()
        user = self.get_user(email)
        self.check_access_user(user)
        with UserSaver(doc=user, rqh=self) as saver:
            if self.is_admin():
                role = self.get_argument('role', None)
                if role in constants.ROLES:
                    saver['role'] = role
                saver['services'] = self.get_arguments('service')
            saver['username'] = self.get_argument('username', None)
            saver['fullname'] = self.get_argument('fullname')
            saver['department'] = self.get_argument('department', None)
            saver['university'] = self.get_argument('university', None)
            saver['country'] = self.get_argument('country')
        self.redirect(self.reverse_url('user', user['email']))


class UserCreate(RequestHandler):
    """Create a user account, and send an email
     to the administrator requesting review."""

    def get(self):
        self.render('user_create.html',
                    countries=sorted([c.name for c in pycountry.countries]))

    def post(self):
        self.check_xsrf_cookie()
        # Some fields initialized by UserSaver
        with UserSaver(rqh=self) as saver:
            saver['email'] = self.get_argument('email')
            saver['username'] = self.get_argument('username', None)
            saver['role'] = constants.USER
            saver['fullname'] = self.get_argument('fullname')
            saver['department'] = self.get_argument('department', None)
            saver['university'] = self.get_argument('university', None)
            saver['country'] = self.get_argument('country')
        self.redirect(self.reverse_url('user_activate'))


class UserApprove(RequestHandler):
    "Approve a user account; email the activation code."

    @tornado.web.authenticated
    def post(self, name):
        self.check_xsrf_cookie()
        self.check_admin()
        user = self.get_user(name)
        if user['status'] != constants.PENDING:
            raise tornado.web.HTTPError(409, 'account not pending')
        with UserSaver(doc=user, rqh=self) as saver:
            code = utils.get_iuid()
            deadline = utils.timestamp(days=settings['ACTIVATION_DEADLINE'])
            saver['activation'] = dict(code=code, deadline=deadline)
            saver['status'] = constants.APPROVED
        self.send_email(user,
                        'Userman account activation',
                        settings['ACTIVATION_EMAIL_TEXT'].format(
                            url=self.get_absolute_url('user_activate'),
                            email=user['email'],
                            code=code))
        self.redirect(self.reverse_url('user', user['email']))


class UserBlock(RequestHandler):
    "Block a user account."

    @tornado.web.authenticated
    def post(self, name):
        self.check_xsrf_cookie()
        self.check_admin()
        user = self.get_user(name)
        if user['status'] != constants.BLOCKED:
            if user['role'] == constants.ADMIN:
                raise tornado.web.HTTPError(409, 'cannot block admin account')
            with UserSaver(doc=user, rqh=self) as saver:
                saver['status'] = constants.BLOCKED
        self.redirect(self.reverse_url('user', user['email']))


class UserUnblock(RequestHandler):
    "Unblock a user account."

    @tornado.web.authenticated
    def post(self, name):
        self.check_xsrf_cookie()
        self.check_admin()
        user = self.get_user(name)
        if user['status'] != constants.ACTIVE:
            with UserSaver(doc=user, rqh=self) as saver:
                saver['status'] = constants.ACTIVE
        self.redirect(self.reverse_url('user', user['email']))


class UserActivate(RequestHandler):
    "Activate the user account, setting the password."

    def get(self):
        self.render('user_activate.html',
                    error=None,
                    email=self.get_argument('email', ''),
                    activation_code=self.get_argument('activation_code', ''))

    def post(self):
        self.check_xsrf_cookie()
        email = self.get_argument('email', None)
        activation_code = self.get_argument('activation_code', None)
        try:
            if not email:
                raise ValueError('missing email')
            if not activation_code:
                raise ValueError('missing activation code')
            password = self.get_argument('password', '')
            utils.check_password_quality(password)
            confirm_password = self.get_argument('confirm_password', '')
            if password != confirm_password:
                raise ValueError('passwords do not match')
            message = 'no such user, or invalid or expired activation code'
            try:
                user = self.get_user(email)
            except:
                raise ValueError(message)
            activation = user.get('activation', dict())
            if activation.get('code') != activation_code:
                raise ValueError(message)
            if activation.get('deadline', '') < utils.timestamp():
                raise ValueError(message)
            with UserSaver(doc=user, rqh=self) as saver:
                del saver['activation']
                saver['password'] = password
                saver['status'] = constants.ACTIVE
            self.set_secure_cookie(constants.USER_COOKIE_NAME, email)
            self.redirect(self.reverse_url('user', email))
        except ValueError, msg:
            self.render('user_activate.html',
                        error=str(msg),
                        email=email,
                        activation_code=activation_code)


class UserReset(RequestHandler):
    "Reset the user password, sending out an activation code."

    def get(self):
        self.render('user_reset.html')

    def post(self):
        self.check_xsrf_cookie()
        try:
            user = self.get_user(self.get_argument('email'))
            if user.get('status') not in (constants.APPROVED, constants.ACTIVE):
                raise ValueError('account status not active')
            with UserSaver(doc=user, rqh=self) as saver:
                code = utils.get_iuid()
                deadline = utils.timestamp(days=settings['ACTIVATION_DEADLINE'])
                saver['activation'] = dict(code=code, deadline=deadline)
                saver['status'] = constants.ACTIVE
            self.send_email(user,
                            'Userman account password reset',
                            settings['RESET_EMAIL_TEXT'].format(
                                url=self.get_absolute_url('user_activate'),
                                email=user['email'],
                                code=code))
        except (tornado.web.HTTPError, ValueError), msg:
            logging.debug("account reset error: %s", msg)
        self.redirect(self.reverse_url('home'))


class UserApiKey(RequestHandler):
    "Set a new API key for a service for the user."

    @tornado.web.authenticated
    def post(self, name):
        self.check_xsrf_cookie()
        user = self.get_user(name)
        service = self.get_service(self.get_argument('service'))
        with UserSaver(doc=user, rqh=self) as saver:
            saver.set_apikey(service['name'])
        self.redirect(self.reverse_url('user', user['email']))


class Users(RequestHandler):
    "List of all user accounts."

    @tornado.web.authenticated
    def get(self):
        self.check_admin()
        users = [r.doc for r in self.db.view('user/email', include_docs=True)]
        self.render('users.html', users=users)


class UsersPending(RequestHandler):
    "List of pending user accounts."

    @tornado.web.authenticated
    def get(self):
        self.check_admin()
        users = [r.doc for r in self.db.view('user/pending', include_docs=True)]
        self.render('users_pending.html', users=users)


class UsersBlocked(RequestHandler):
    "List of blocked user accounts."

    @tornado.web.authenticated
    def get(self):
        self.check_admin()
        users = [r.doc for r in self.db.view('user/blocked', include_docs=True)]
        self.render('users_blocked.html', users=users)
