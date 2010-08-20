#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright © 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.

import base64
import logging
try:
    import json
except ImportError:
    import simplejson as json

import pymongo.json_util 
import web

from pulp.server.api.user import UserApi
from pulp.server.api.consumer import ConsumerApi
from pulp.server.certificate import Certificate
from pulp.server.pexceptions import PulpException
from pulp.server.webservices import http
import pulp.server.password_util as password_util

log = logging.getLogger(__name__)

userApi = UserApi()
consumerApi = ConsumerApi()


class RoleCheck(object):
    '''decorator class to check Roles of caller.  Copied and modified from:
       
       http://wiki.python.org/moin/PythonDecoratorLibrary#DifferentDecoratorForms
    '''
    
    def __init__(self, *dec_args, **dec_kw):
        '''The decorator arguments are passed here.  Save them for runtime.'''
        self.dec_args = dec_args
        self.dec_kw = dec_kw
        
    def __call__(self, f):
        def check_roles(*fargs, **kw):
            '''
              Strip off the decorator arguments so we can use those to check the
              Roles of the current caller.

              Note: the first argument cannot be "self" because we get a parse error
              "takes at least 1 argument" unless the instance is actually included in
              the argument list, which is redundant.  If this wraps a class instance,
              the "self" will be the first argument.
            '''
            
            # Check the roles
            log.debug("\n\nRole checking start, function: %s" % str(f))
            roles = {'consumer':None, 'admin': None, 'consumer_id': None}
            for key in self.dec_kw.keys():
                log.debug("Role Name [%s], check? [%s]" % (key, self.dec_kw[key]))
                roles[key] = self.dec_kw[key]
            
            admin_access_granted = False
            consumer_access_granted = False
            
            # Admin role trumps any other checking.  do it first
            if (roles['admin']):
                ## If not using cert check uname and password
                # TODO: Implement uname/pass checking
                log.debug("Checking username/pass")
                try:
                    admin_access_granted = self.check_username_pass(*fargs)
                    log.debug("admin access granted: %s" % admin_access_granted)
                except PulpException, pe:
                    # TODO: Figure out how to re-use the same return function in base.py
                    http.status_unauthorized()
                    http.header('Content-Type', 'application/json')
                    return json.dumps(pe.value, default=pymongo.json_util.default)
            
            ## Check cert
            if (roles['consumer'] or roles['consumer_id']):
                consumer_access_granted = self.check_consumer(roles['consumer_id'], *fargs)
                log.debug("consumer_access_granted? %s " % consumer_access_granted)
            
            log.debug("AAG: %s, CAG: %s" % (admin_access_granted, consumer_access_granted))
            if (not admin_access_granted and not consumer_access_granted):
                log.debug("Access denied.")
                http.status_unauthorized()
                http.header('Content-Type', 'application/json')
                return json.dumps("Authorization Failure.  Check your username and password or your Certificate", 
                                  default=pymongo.json_util.default)
            # Access granted, proceed
            log.debug("check_roles : Access granted")
            # Does this wrap a class instance?
            if fargs and getattr(fargs[0], '__class__', None):
                instance, fargs = fargs[0], fargs[1:]
                # call the method with just the fargs and kw for the original method
                ret=f(instance, *fargs, **kw)
            else:
                # just send in the give args and kw
                ret=f(*(fargs), **kw)
            return ret

        # Save wrapped function reference
        self.f = f
        check_roles.__name__ = f.__name__
        check_roles.__dict__.update(f.__dict__)
        check_roles.__doc__ = f.__doc__
        return check_roles

    def check_username_pass(self, *fargs):
        environment = web.ctx.environ
        auth_string = environment.get('HTTP_AUTHORIZATION', None)
        if (auth_string is not None and auth_string.startswith("Basic")):
            logging.root.setLevel(logging.DEBUG)
            logging.root.addHandler(logging.StreamHandler())
            log.debug("auth_string string: %s" % auth_string)
            encoded_auth = auth_string.split(" ")[1]
            auth_string = base64.decodestring(encoded_auth)
            uname_pass = auth_string.split(":")
            username = uname_pass[0]
            password = uname_pass[1]
            user = userApi.user(username)
            if (user is None):
                raise PulpException("User with login [%s] does not exist" 
                                    % username)
            log.debug("Username: %s hashed password: %s" % (username, password))
            log.debug("Stored user password: %s" % user['password'])
            goodPassword = password_util.check_password(user['password'], password) 
            log.debug("Good Password? [%s]" % goodPassword)
            return goodPassword
        return False
    
    def check_consumer(self, check_id=False, *fargs): 
        ## This is where we will extract CERT fields
        environment = web.ctx.environ
        # print("Env: %s" % environment)
        for key in environment.keys():
            if (key.startswith('SSL_')):
                value = str(environment.get(key, None))
                log.debug("SSL k: " + key + ", v: " + value)
        cs = environment.get('SSL_CLIENT_CERT', None)
        good_certificate = False
        if cs != None:
            idcert = Certificate(content=cs)
            log.debug("parsed ID CERT: %s" % idcert)
            subject = idcert.subject()
            consumer_cert_uid = subject.get('CN', None)
            if (consumer_cert_uid is None):
                log.error("Consumer UID not found in certificate.  " + \
                          "Not a valid Consumer certificate")
                good_certificate = False
                return good_certificate 
            log.error("Consumer UID: %s " % consumer_cert_uid)
            # Check that it is a valid consumer in our db
            consumer = consumerApi.consumer(consumer_cert_uid)
            if not consumer:
                log.error("Consumer with id: %s does not exist" % 
                          consumer_cert_uid)
                good_certificate = False
                return good_certificate 
                
            # Check the consumer_id matches
            log.debug("check_id: %s" % check_id) 
            if check_id:
                log.debug("Checking ID in cert vs param passed in")            
                for arg in fargs:
                    log.debug("Arg [%s]" % arg)
                    if (arg == consumer_cert_uid):
                        good_certificate = True
                        break
            else:
                good_certificate = True
            if (not good_certificate):
                log.error("Certificate UID doesnt match the consumer UID you passed in") 
            else:
                log.error("Certificate UID matched.  continue")

        return good_certificate
            
